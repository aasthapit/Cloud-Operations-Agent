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
| Fleet registry MCP | 8013 | `make run-registry-mcp` |
| MongoDB (fleet registry) | 27017 | `make mongo-up` |

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

## Fleet registry service

The fleet registry is the organization's answer to "who exists and where does it run": applications, clusters, namespaces, and lines of business.
It is the one part of this stack that holds state rather than configuration, so it lives in MongoDB rather than in the hot-reloaded config tree, and it is served by its own MCP server on port 8013 under the `reg` namespace.

Bring it up and load it:

```bash
make mongo-up            # mongo:8 as container cloudops-mongo on 127.0.0.1:27017
make mongo-seed          # config/fleet/*.yaml -> clusters, apps, placements
make run-registry-mcp    # the reg__* MCP server, standalone on :8013
```

`make mongo-seed` is idempotent: it upserts by natural key (`clusters.name`, `apps.app_id`, `placements.app_id+cluster+namespace`), so running it twice is indistinguishable from running it once.
It is a seeder and not a sync, so a document you add to Mongo directly is never deleted by a later seed run.
The YAML under [config/fleet/](config/fleet/) is therefore seed fixture data; MongoDB is the runtime truth, and a registry write is visible to the very next read without any file watching.

Tools, all namespaced `reg__` by [config/gateway/servers.yaml](config/gateway/servers.yaml):

| Tool | Answers |
|---|---|
| `reg__resolve_entity` | free text to entities across apps, clusters, namespaces, LOBs ("is app SSOP down?") |
| `reg__find_placements` | where an application is registered, filtered by cluster, namespace, environment, or LOB |
| `reg__list_apps_on_cluster` | what shares a cluster with something |
| `reg__blast_radius` | what is affected if a cluster, namespace, or line of business goes down |
| `reg__get_app` | the application registry entry plus its placements |
| `reg__list_lobs` | every line of business with app and cluster counts |

Resolution goes exact, then alias, then substring, then fuzzy, and every match carries its own score, so several candidates mean the caller confirms rather than guesses.
These are registry BELIEFS, never observations: a placement is verified against the cluster API before anything is reported as running or down.

If MongoDB is unreachable the server still starts and every tool returns `{"error": "registry unavailable", "detail": ...}`.
An agent that cannot consult the registry says so; it does not invent a fleet.

### Cluster credentials

A cluster record carries an `auth` block, which is the only thing in the registry that is ever a secret.
Three forms are supported, and all three are stripped from every tool result:

- `{"type": "kubeconfig", "context": "kind-acm-spoke-1a"}` - the local kind fleet, where credentials stay in the kubeconfig on disk and never enter the database.
- `{"type": "token", "token": "sha256~..."}` - a bearer token, with optional `insecure_skip_tls_verify` and an inline `ca` PEM.
- `{"type": "basic", "username": ..., "password": ...}` - exchanged for a bearer token through the cluster's own OAuth server, since OpenShift does not accept basic auth on the API server.

The basic exchange discovers `/.well-known/oauth-authorization-server`, runs the `openshift-challenging-client` implicit flow, and reads the token out of the 302's URL fragment.
Tokens are cached per cluster until they expire and re-exchanged on a 401, so a rotated credential heals on the next call instead of failing forever.
Only the `kubeconfig` form belongs in committed YAML; put token and basic records into Mongo directly.

### Environment

| Variable | Default | Meaning |
|---|---|---|
| `CLOUDOPS_MONGO_URL` | `mongodb://127.0.0.1:27017` | registry connection string; a deployed instance carries its credentials here, never in `config/` |
| `CLOUDOPS_MONGO_DB` | `cloudops` | registry database name |
| `CLOUDOPS_MCP_REGISTRY_PORT` | `8013` | registry MCP server port |

## Live mode against a local kind fleet

Mock mode is the default and stays fully working; live mode is opt-in and reads real clusters instead of the synthetic world.
The reference live fleet is six local [kind](https://kind.sigs.k8s.io) clusters named `acm-hub-1`, `acm-hub-2`, `acm-spoke-1a`, `acm-spoke-1b`, `acm-spoke-2a` and `acm-spoke-2b`, registered under the `live:` section of [config/fleet/fleet.yaml](config/fleet/fleet.yaml), which maps each fleet name to a kubeconfig context.

```bash
make live-prep     # idempotent: monitoring stack + demo workloads on all six clusters
make live-smoke    # exercise both live backends against the fleet; binds no ports
```

`make live-prep` server-side applies the plain manifests in [deploy/live/](deploy/live/): a `monitoring` namespace per cluster with kube-state-metrics and a single-replica Prometheus (emptyDir storage) that scrapes kube-state-metrics, the API server, the kubelet and cAdvisor, and itself, and loads one always-firing `Watchdog` alert so the attestation's dead man's switch is legitimately satisfied.
It also deploys the demo workloads placement discovery has to find: `payments-api` in `payments-prod` on `acm-spoke-1a` (healthy, 2 replicas) and on `acm-spoke-2a` (2 replicas whose `ledger-sync` container exits non-zero, so the pods really are in CrashLoopBackOff), plus `inventory-sync` in `logistics-dev` on `acm-spoke-1b`.
The script is scoped to those six contexts and refuses any other cluster, and re-running it is a no-op apart from re-hashing the Prometheus config.

Turn live mode on with one environment variable, which both MCP servers read per call:

```bash
CLOUDOPS_BACKEND_MODE=live make dev
```

Credentials come from the kubeconfig on disk (`KUBECONFIG`, else `~/.kube/config`) and never from committed config.
Every request is a GET, Secret data is never fetched (only names), and each cluster's Prometheus is reached through the API server's service proxy, so live mode needs no port-forward, no NodePort, and no second credential.

### What is not applicable on vanilla Kubernetes

kind clusters have no OpenShift APIs, so `get_cluster_version`, `get_cluster_operators`, `get_machine_config_pools` and `get_pending_csrs` have nothing real to answer.
Rather than erroring or inventing state, each returns the mock result shape with health-neutral values plus `applicable: false` and the reason `not applicable: vanilla Kubernetes cluster (no OpenShift APIs)`.
No attestation rule triggers on those values, so the four checks land as plain passes and a healthy kind cluster attests **healthy** rather than degraded or unattestable, with no change to the committed battery.
Metrics the light monitoring stack does not collect are reported as `null` next to a `*_available` flag, never as a plausible-looking number: an unknown reading must not silently become a healthy one.
Two readings are honestly unflattering on this fleet: every cluster is single-node, so the capacity check's minus-one-node guard cannot pass and raises a warning row, and the demo workloads expose no HTTP metrics, so golden signals report `instrumented: false`.

### Pointing at real OpenShift later

Replace the entries under `live:` in `fleet.yaml` with your clusters' names and kubeconfig contexts; nothing else in the registry changes.
Then implement the four OpenShift-only methods in [backend/src/cloudops/mcp_servers/openshift/live.py](backend/src/cloudops/mcp_servers/openshift/live.py) against `config.openshift.io/v1` and `machineconfiguration.openshift.io/v1`, returning the same shapes with `applicable: true`, and the existing battery starts evaluating them without edits.
For a fleet with ACM and Thanos, point `LiveObservabilityBackend` at the aggregation endpoint instead of fanning out per cluster: the queries are already scoped by the `cluster` label, so that is a change of transport, not of shape.

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
make check    # ruff + mypy + tsc + pytest (118 tests)
make test
```

The suite is hermetic: it needs no Ollama, no Docker, and no pre-running service, and it never binds a dev port.
The one test that talks to real clusters is marked `live_smoke` and skips unless `CLOUDOPS_LIVE_SMOKE=1`, so `make test` is unaffected by whether the kind fleet is running.
[backend/tests/test_e2e_triage.py](backend/tests/test_e2e_triage.py) boots both MCP servers and the gateway in-process on kernel-assigned ports, drives a full triage turn through the fake model, and asserts the typed payloads the console consumes.

Layout: [backend/src/cloudops/](backend/src/cloudops/) (agent, gateway, MCP servers, shared infra, mock fleet), [frontend/](frontend/) (Express BFF + React SPA), [docs/](docs/) (PRD, user flows, research notes, source checklist transcription).

Branches: `main` (documents + approved milestones), `develop` (integration), `UAT` (cut from develop for product-owner review).
