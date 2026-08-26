# Cloud Operations Agent

AI-assisted first-level triage for a fleet of hundreds of OpenShift clusters.
It answers the question every application team asks first: "is it my app, or is it the platform?"

Every conversation follows a fixed discipline, enforced in code, configured in YAML:
attest the health of every in-scope cluster, resolve who the user is and where their application actually runs, produce the organization's 18-section Application 360 Report deterministically, and only then let the LLM narrate and investigate.

Full product definition: [docs/PRD.md](docs/PRD.md) (canonical) and the review artifacts (PRD + user flows) linked from the project session.

## Quickstart (mock mode, fully local)

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+, and [Ollama](https://ollama.com) serving a tool-capable model:

```bash
ollama pull qwen3:4b   # default; gpt-oss:20b is the documented quality escalation
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

## Telemetry

One user turn forms one distributed trace: console, BFF, A2A, orchestrator phases, every check, every gateway call, every MCP tool, every LLM call, with `thread.id` on spans and logs throughout.

```bash
make telemetry-up                                   # collector + Jaeger (docker)
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 make dev
open http://localhost:16686                          # find traces by service cloudops.agent
```

Without the exporter the stack still runs; trace ids appear in logs for correlation.
Logs are structured, redacted (bearer tokens, PEM blocks, secret-shaped env values, key-based masking), and stack traces never reach the client: errors carry a correlation id instead.

## Development

```bash
make check    # ruff + mypy + tsc + pytest (44 tests)
make test
```

Layout: [backend/src/cloudops/](backend/src/cloudops/) (agent, gateway, MCP servers, shared infra, mock fleet), [frontend/](frontend/) (Express BFF + React SPA), [docs/](docs/) (PRD, user flows, research notes, source checklist transcription).

Branches: `main` (documents + approved milestones), `develop` (integration), `UAT` (cut from develop for product-owner review).
