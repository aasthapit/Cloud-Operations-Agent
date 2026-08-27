# Cloud Operations Agent

AI-assisted first-level triage for a fleet of hundreds of OpenShift clusters.
It answers the question every application team asks first: "is it my app, or is it the platform?"

Every conversation follows a fixed discipline, enforced in code, configured in YAML:
attest the health of every in-scope cluster, resolve who the user is and where their application actually runs, produce the organization's 18-section Application 360 Report deterministically, and only then let the LLM narrate and investigate.

Full product definition: [docs/PRD.md](docs/PRD.md) (canonical) and the review artifacts (PRD + user flows) linked from the project session.

## Quickstart (mock mode, fully local)

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+, and [Ollama](https://ollama.com) serving a tool-capable model:

```bash
ollama pull gpt-oss:20b   # default; qwen3:4b is the documented fast alternative
```

Then:

```bash
make setup   # uv sync + npm install
make dev     # all six services; Ctrl-C stops everything
```

Open http://localhost:5173, pick a dev identity in the masthead, and ask:

- "Why is payments-api flaky in prod?" (app developer: zero-question triage; one cluster attests degraded)
- "Is my stuff healthy?" (platform SRE: exactly one clarifying question)
- "attest prod-east-2" (direct cluster attestation)

No cluster access, no cloud credentials: mock mode serves a deterministic synthetic fleet (180 clusters) with scripted faults from [config/mock/scenario.yaml](config/mock/scenario.yaml).

## Services

| Service | Port | Run alone |
|---|---|---|
| Ops console (Vite dev) | 5173 | `cd frontend && npm run dev:web` |
| Console BFF (Express) | 8080 | `cd frontend && npm run dev:server` |
| Agent (ADK, A2A) | 8001 | `cd backend && uv run python -m cloudops.agent` |
| MCP gateway | 8010 | `cd backend && uv run python -m cloudops.gateway` |
| OpenShift MCP | 8011 | `cd backend && uv run python -m cloudops.mcp_servers.openshift` |
| Observability MCP | 8012 | `cd backend && uv run python -m cloudops.mcp_servers.observability` |

Environment knobs live in [.env.example](.env.example) (copy to `.env`; empty means sane local defaults).

## The config plane (hot reload)

Everything behavioral lives under [config/](config/) and applies without restarts:

- `agent/system_prompt.md`, `agent/routing.md`, `agent/skills/*` - re-read on every LLM invocation; edit and the next message behaves differently.
- `checks/health_attestation.yaml`, `checks/app360.yaml` - the check batteries; validated on save, atomic swap, last known good on a bad edit. The schema is documented at the top of each file.
- `gateway/servers.yaml` - registered MCP servers. Adding a cloud domain is one entry here plus optional checks and a skill file; no code changes.
- `fleet/fleet.yaml`, `fleet/applications.yaml` - cluster registry and application catalog.
- `identity/users.yaml` - dev personas (production swaps in JWT validation at the BFF, same claim shape).
- `mock/scenario.yaml` - the mock world's fault script; edit to change the demo story live.
- `models.yaml` - inference provider/model (binds at agent start) and agent tuning: TTLs, budgets (hot).

Ollama is reached through its OpenAI-compatible endpoint by default (`provider: openai-compat`), so pointing at a hosted OpenAI-compatible gateway later is an env change, not a code change.

### Model choice and latency

The model lives in [config/models.yaml](config/models.yaml) under `inference`, alongside the provider, temperature, and output cap.
The file hot-reloads, but `inference.*` binds when the agent process starts, because the ADK agent holds its model instance; the `agent.*` knobs below it (TTL, tool budget, auto-report) are re-read every turn.
So changing the model means saving the file and restarting the agent, while changing a budget takes effect on the next message.

`gpt-oss:20b` is the default because triage narration is the part users read, and it is the local model that keeps its reasoning out of the visible answer and its tool names straight.
The cost is latency: a full narrative takes roughly 15-60 s on a developer laptop, and the first token can arrive well after the 2 s target in NFR-PERF-2.
That target does not hold on a 20B model running locally, and this is an accepted deviation rather than a defect: the deterministic phases (context, attestation, Application 360) stream their cards long before the narrative starts, so the console fills with evidence while the model is still thinking.
A hosted inference endpoint behind the same `openai-compat` provider is the path back inside the target, and it needs no code change.

`qwen3:4b` is the fast alternative when you are iterating on prompts or flows and want turns in seconds.
Its caveat is a thinking leak: this build ignores the `/no_think` soft switch the agent appends, so its chain-of-thought prose can open the visible answer.
Keep it for development, not for demos or evaluation.

`provider: fake` (or `CLOUDOPS_FAKE_LLM=1`, which selects it without editing committed config) swaps in a deterministic in-process model that answers instantly, never calls a tool, and never touches the network.
It exists so the headless end-to-end test can exercise the whole chain without Ollama; it is not useful for anything a human reads.

## Telemetry

One user turn forms one distributed trace: console, BFF, A2A, orchestrator phases, every check, every gateway call, every MCP tool, every LLM call, with `thread.id` on spans and logs throughout.

Without an exporter the stack still runs; trace ids appear in logs for correlation.
Logs are structured, redacted (bearer tokens, PEM blocks, secret-shaped env values, key-based masking), and stack traces never reach the client: errors carry a correlation id instead.

### Tracing with Jaeger

[deploy/docker-compose.yaml](deploy/docker-compose.yaml) brings up an OTLP collector fanning out to a Jaeger all-in-one; it is the only part of this project that wants Docker, and it is optional.

```bash
docker compose -f deploy/docker-compose.yaml up -d    # or: make telemetry-up
```

Then point the services at the collector and restart the stack, because the exporter is read at process start:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 make dev
```

`OTEL_EXPORTER_OTLP_ENDPOINT` is the variable documented in [.env.example](.env.example), and `http://localhost:4318` is the collector's OTLP/HTTP port from the compose file; put it in your `.env` to avoid prefixing every run.
Open the Jaeger UI at http://localhost:16686 and search the `cloudops.agent` service.
Each user turn is one trace spanning all five services, tagged with `thread.id`, so you can pick a conversation out of the list and read the whole turn: context resolution, each check battery, each proxied gateway call, each MCP tool, and the LLM call.
`make telemetry-down` stops the collector and Jaeger; the stack keeps running without them.

## Development

```bash
make check    # ruff + mypy + tsc + pytest (62 tests)
make test
```

The suite is hermetic: it needs no Ollama, no Docker, and no pre-running service, and it never binds a dev port.
[backend/tests/test_e2e_triage.py](backend/tests/test_e2e_triage.py) boots both MCP servers and the gateway in-process on kernel-assigned ports, drives a full triage turn through the fake model, and asserts the typed payloads the console consumes.

Layout: [backend/src/cloudops/](backend/src/cloudops/) (agent, gateway, MCP servers, shared infra, mock fleet), [frontend/](frontend/) (Express BFF + React SPA), [docs/](docs/) (PRD, user flows, research notes, source checklist transcription).

Branches: `main` (documents + approved milestones), `develop` (integration), `UAT` (cut from develop for product-owner review).
