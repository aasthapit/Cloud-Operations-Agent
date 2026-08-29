# Cloud Operations Agent

AI-assisted first-level triage for a fleet of hundreds of OpenShift clusters.
It answers the question every application team asks first: "is it my app, or is it the platform?"

Every conversation follows a fixed discipline, enforced in code, configured in YAML:
attest the health of every in-scope cluster, resolve who the user is and where their application actually runs, produce the organization's 18-section Application 360 Report deterministically, and only then let the LLM narrate and investigate.

Full product definition: [docs/PRD.md](docs/PRD.md) (canonical) and the review artifacts (PRD + user flows) linked from the project session.

## Quickstart (local kind fleet)

Prerequisites: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+, and [Ollama](https://ollama.com) serving a tool-capable model:

```bash
ollama pull gpt-oss:20b   # default; qwen3:4b is the documented fast alternative
```

Then:

```bash
make setup   # uv sync + npm install
make dev     # every service; Ctrl-C stops everything
```

Open http://localhost:5173, pick a dev identity in the masthead, and ask:

- "Why is payments-api flaky in prod?" (app developer: zero-question triage against the healthy spoke)
- "Is my stuff healthy?" (platform SRE: exactly one clarifying question)
- "attest acm-spoke-2a" (direct cluster attestation; the crash-looping spoke)

There is one backend: real cluster telemetry.
Bring the local kind fleet up first with `make live-prep` (see "The local kind fleet" below); without reachable clusters the agent reports every cluster as unattestable, honestly, rather than inventing state.

## Services

| Service | Port | Run alone |
|---|---|---|
| Ops console (Vite dev) | 5173 | `cd frontend && npm run dev:web` |
| Console BFF (Express) | 8080 | `cd frontend && npm run dev:server` |
| Agent (ADK, A2A) | 8001 | `cd backend && uv run python -m cloudops.agent` |
| MCP gateway | 8010 | `cd backend && uv run python -m cloudops.gateway` |
| OpenShift MCP | 8011 | `cd backend && uv run python -m cloudops.mcp_servers.openshift` |
| Fleet registry MCP | 8013 | `make run-registry-mcp` |
| MongoDB (fleet registry) | 27017 | `make mongo-up` |

Environment knobs live in [.env.example](.env.example) (copy to `.env`; empty means sane local defaults).

## The config plane (hot reload)

Everything behavioral lives under [config/](config/) and applies without restarts:

- `agent/system_prompt.md`, `agent/routing.md`, `agent/skills/*`, `agent/protocol_note.md` - re-read on every LLM invocation; edit and the next message behaves differently.
- `agent/messages.yaml` - the conversational copy the runtime speaks in its own voice (onboarding, clarification questions, tool-loop guidance). Read fresh per use; a missing key is logged loudly rather than silently falling back to a duplicate string in code.
- `checks/health_attestation.yaml`, `checks/app360.yaml` - the check batteries; validated on save, atomic swap, last known good on a bad edit. The schema is documented at the top of each file.
- `gateway/servers.yaml` - registered MCP servers. Adding a cloud domain is one entry here plus optional checks and a skill file; no code changes.
- `gateway/gateway.yaml` - gateway supervisor behavior (downstream reconnect cadence). Read at gateway boot, not hot: the value is captured by connection loops that outlive a request.
- `fleet/fleet.yaml`, `fleet/applications.yaml` - cluster registry and application catalog.
- `identity/users.yaml` - dev personas (production swaps in JWT validation at the BFF, same claim shape).
- `models.yaml` - inference provider/model (binds at agent start) and agent tuning: TTLs, budgets (hot). It is authoritative: `provider`, `model`, `temperature` and `max_output_tokens` have no code-side defaults, and a missing key raises an error naming it rather than booting against some other model.

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

## The local kind fleet

The reference fleet is six local [kind](https://kind.sigs.k8s.io) clusters named `acm-hub-1`, `acm-hub-2`, `acm-spoke-1a`, `acm-spoke-1b`, `acm-spoke-2a` and `acm-spoke-2b`, registered under the `live:` section of [config/fleet/fleet.yaml](config/fleet/fleet.yaml), which maps each fleet name to a kubeconfig context.

```bash
make live-prep     # idempotent: demo workloads on all six clusters
make live-smoke    # exercise the backend against the fleet; binds no ports
```

`make live-prep` server-side applies the plain manifests in [deploy/live/](deploy/live/).
It still lays down the per-cluster monitoring namespace those manifests carry; nothing reads it any more (see "No metrics pipeline"), and it is kept only so a future metrics domain has somewhere to land.
It deploys the demo workloads placement verification has to confirm: `payments-api` in `payments-prod` on `acm-spoke-1a` (healthy, 2 replicas) and on `acm-spoke-2a` (2 replicas whose `ledger-sync` container exits non-zero, so the pods really are in CrashLoopBackOff), plus `inventory-sync` in `logistics-dev` on `acm-spoke-1b`.
The script is scoped to those six contexts and refuses any other cluster, and re-running it is a no-op.

Credentials come from the kubeconfig on disk (`KUBECONFIG`, else `~/.kube/config`) and never from committed config.
Every request is a GET and Secret data is never fetched, only names.

### No metrics pipeline

This deployment reads the Kubernetes API and nothing else.
There is no Prometheus, Thanos or Grafana behind the agent, so firing alerts, request error rates, latency percentiles, CPU throttling and memory headroom cannot be read.
Those checks were REMOVED from the batteries rather than faked: [config/checks/health_attestation.yaml](config/checks/health_attestation.yaml) dropped etcd, firing alerts, the Watchdog dead man's switch, the API server SLO and certificate-expiry alerts, and [config/checks/app360.yaml](config/checks/app360.yaml) dropped application alerts, error rate, latency, memory headroom and CPU throttling.
Sections 9 and 12 of the Application 360 report state that absence in words; nothing invents a number.

What survives is what the API can answer honestly.
Capacity headroom is computed from node `status.allocatable` against the resource requests of non-terminal pods - the same arithmetic the scheduler does - by `ocp__get_capacity`.
Autoscaling headroom and disruption budgets come from `ocp__get_autoscaling` (HPA `autoscaling/v2`, PDB `policy/v1`), because those are API objects rather than metrics.

Dropping the Watchdog check also removed the attestation battery's only `unattestable` outcome, so `api-reachability` now carries it: a cluster whose API server did not answer was not attested at all, which is a different claim from "attested and unhealthy" and keeps the FR-ATT-5 confidence cap meaningful.

### Placement: the registry proposes, the cluster confirms

Where an application runs is a two-step contract (FR-CTX-2), never a single lookup.
`reg__find_placements` returns the fleet registry's candidates, and `ocp__verify_placement` then asks each candidate cluster whether pods matching the app label are actually there.
A candidate the cluster denies is a stale registry row and is dropped from the report; a candidate whose cluster did not answer is kept and flagged, because an unreachable cluster proves nothing about whether the workload is running.
When every candidate is denied, the agent says the registry entry looks stale and names what each cluster answered.

### What is not applicable on vanilla Kubernetes

kind clusters have no OpenShift APIs, so `get_cluster_version`, `get_cluster_operators`, `get_machine_config_pools` and `get_pending_csrs` have nothing real to answer.
Rather than erroring or inventing state, each returns the full result shape with health-neutral values plus `applicable: false` and the reason `not applicable: vanilla Kubernetes cluster (no OpenShift APIs)`.
No attestation rule triggers on those values, so the four checks land as plain passes and a healthy kind cluster attests **healthy** rather than degraded or unattestable, with no change to the committed battery.
One reading is honestly unflattering on this fleet: the clusters are small, so the capacity check's minus-one-node guard can fail and raise a warning row rather than claim headroom that is not there.

### Pointing at real OpenShift later

Replace the entries under `live:` in `fleet.yaml` with your clusters' names and kubeconfig contexts; nothing else in the registry changes.
Then implement the four OpenShift-only methods in [backend/src/cloudops/mcp_servers/openshift/live.py](backend/src/cloudops/mcp_servers/openshift/live.py) against `config.openshift.io/v1` and `machineconfiguration.openshift.io/v1`, returning the same shapes with `applicable: true`, and the existing battery starts evaluating them without edits.

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
make check    # ruff + mypy + tsc + pytest (170 tests)
make test
make eval     # scenario evals against the fake model (see "Evaluating the agent")
```

The suite is hermetic: it needs no Ollama, no Docker, and no pre-running service, and it never binds a dev port.
The one test that talks to real clusters is marked `live_smoke` and skips unless `CLOUDOPS_LIVE_SMOKE=1`, so `make test` is unaffected by whether the kind fleet is running.
[backend/tests/test_e2e_triage.py](backend/tests/test_e2e_triage.py) boots both MCP servers and the gateway in-process on kernel-assigned ports, drives a full triage turn through the fake model, and asserts the typed payloads the console consumes.

The test doubles and the in-process service harness live in [backend/src/cloudops/testkit/](backend/src/cloudops/testkit/) rather than under `tests/`, because the eval harness below builds its worlds from exactly the same fixtures.
One implementation of a double is the only way two consumers of it cannot disagree.

Layout: [backend/src/cloudops/](backend/src/cloudops/) (agent, gateway, MCP servers, testkit, evals, shared infra), [frontend/](frontend/) (Express BFF + React SPA), [docs/](docs/) (PRD, user flows, research notes, source checklist transcription).

## Evaluating the agent

The test suite asks whether the code does what it was written to do.
The eval harness asks a different question: does the agent BEHAVE, across a conversation, the way the product says it should.

```bash
make eval        # every suite against the fake model: hermetic, ~1 minute, the CI gate
make eval-live   # the same suites against the model in config/models.yaml, with LLM judges
```

Each scenario boots the whole backend in-process on kernel-assigned ports - the OpenShift MCP over canned cluster fixtures, the production fleet registry MCP over an in-memory MongoDB the real seeder loaded, the real gateway, the real orchestrator and check engine, the real analyst - and sends the scenario's turns down the same path an A2A request takes.
Nothing about the route is simulated; in fake mode the only substitution is the model.

Scenarios are YAML under [backend/evals/suites/](backend/evals/suites/), and the schema is documented at the top of [context-resolution.yaml](backend/evals/suites/context-resolution.yaml).
A case names a persona, a fleet, an application registry, and a conversation, and each turn declares what must come out of it: the resolved context fields, the attestation verdict per cluster, whether an Application 360 report landed, the phase sequence, the analyst's tool budget, placement verification outcomes, and narrative content.
Adding coverage is a YAML edit.

The two modes differ in what can be asserted, not in what runs.
In `fake` mode the analyst is the deterministic `FakeLlm`, so its answer is one fixed sentence and only deterministic metrics are scored; this is the gate CI runs, and it needs no Ollama, no Docker, and no network.
In `live` mode the analyst is the configured model, so the scenario's `live_only` expectations apply and three LLM judges score the narrative: groundedness (is every factual claim supported by the evidence that turn produced), completeness (did it answer the question), and protocol tone (prose, no raw JSON, no invented tool names).
The judge talks to the same OpenAI-compatible endpoint through a small direct client, and its prompts are config, not code, in [backend/evals/judges/](backend/evals/judges/).

Outputs land in `backend/evals/out/` (gitignored): `scorecard.json` per suite, case, turn and metric, and `report.md` with a summary table and, for every failure, the offending narrative excerpt next to the evidence it should have been grounded in.
The exit code is nonzero when any case fails.

One expectation is always on and never declared in a scenario: only the deterministic runtime may emit `cloudops-*` fences.
It is the first of the contract invariants in [AGENT.md](AGENT.md), and an invariant that has to be opted into is not one.

Branches: `main` (documents + approved milestones), `develop` (integration), `UAT` (cut from develop for product-owner review).
