# AGENT.md - how the components talk to each other

This document describes every interface in the system, hop by hop, in enough detail to re-point the stack at a production OpenShift Container Platform (OCP) fleet, a hosted inference backend, and a real identity provider.
It is the companion to [docs/design/LIVE-CUTOVER.md](docs/design/LIVE-CUTOVER.md) (why the architecture looks like this) and the [README](README.md) (how to run it).

## 1. System at a glance

```
Browser (React SPA, :5173 dev / served by BFF in prod)
   |  REST + Server-Sent Events (SSE)
   v
Console BFF (Express, :8080)                 config/identity/users.yaml (dev) or OIDC JWKS
   |  A2A 1.0 (JSON-RPC over HTTP, streamed)
   v
Agent (Google ADK orchestrator, :8001)       config/agent/*, config/checks/*, config/models.yaml
   |  MCP (streamable HTTP)
   v
MCP gateway (:8010)                          config/gateway/servers.yaml + gateway.yaml
   |  MCP (streamable HTTP), namespaced tools
   +--> OpenShift MCP (:8011)  --HTTPS-->  every cluster's Kubernetes API server
   +--> Fleet registry MCP (:8013)  --mongodb://-->  MongoDB (:27017)
```

Every hop is an independent process with its own entry point, so each component can be scaled, replaced, or stood up alone.
All inter-service URLs default to 127.0.0.1 and are overridable through the environment (see [.env.example](.env.example)).

| Component | Entry point | Port env var |
|---|---|---|
| Web SPA | `frontend/web` (Vite dev) or built into the BFF | - |
| BFF | `frontend/server/src/index.ts` | `CLOUDOPS_BFF_PORT` (8080) |
| Agent | `python -m cloudops.agent` | `CLOUDOPS_AGENT_PORT` (8001) |
| Gateway | `python -m cloudops.gateway` | `CLOUDOPS_GATEWAY_PORT` (8010) |
| OpenShift MCP | `python -m cloudops.mcp_servers.openshift` | `CLOUDOPS_MCP_OPENSHIFT_PORT` (8011) |
| Registry MCP | `python -m cloudops.mcp_servers.registry` | `CLOUDOPS_MCP_REGISTRY_PORT` (8013) |
| MongoDB | `make mongo-up` (container) or a managed instance | `CLOUDOPS_MONGO_URL` |

## 2. Console SPA <-> BFF

The SPA is a thin renderer; every decision it makes is driven by data the BFF serves.

REST surface (all JSON):

- `GET /api/meta` - environment, auth mode, config version, whether the agent is reachable, and check-battery counts (proxied from the agent's `GET /status`, with a local hash fallback when the agent is down).
  The SPA polls it (interval from `/api/ui`) to drive the rail's config pill.
- `GET /api/users` - the dev persona list from `config/identity/users.yaml`; empty outside dev auth mode.
- `GET /api/me` - the verified identity in OIDC mode.
- `GET /api/commands` - the slash-command palette: built-ins (`/clear`, `/attest <cluster>`, `/persona <sub>` in dev mode) plus one entry per enabled skill in `config/agent/agent.yaml`.
  Shape: `{commands: [{name, args?, description, kind: "client"|"message", template?}]}`.
  `client` commands execute in the SPA (reset thread, switch persona); `message` commands prefill or send text through the normal chat path.
- `GET /api/ui` - presentation config from `config/ui/console.yaml` (meta poll interval, activity-log cap, composer copy), hot-read per request, always 200 with defaults on error.

Chat is one endpoint: `POST /api/chat/stream` with body `{message, userSub, contextId?}` answered as an SSE stream.
Event types, in the order a healthy turn produces them:

| SSE event | Meaning |
|---|---|
| `meta` | `contextId` (the A2A thread id) - the SPA persists it per tab and echoes it on the next turn |
| `phase` | Deterministic-phase progress (`context`, `attestation`, `app360`, `narrative`, each with `start`/`done`) |
| `context` | The resolved scope card (user, application, environment, clusters, namespaces, flags) |
| `clarify` | One question with enumerated options; the SPA renders quick-pick buttons that re-enter the same send path |
| `attestation` | The full cluster attestation report card |
| `app360` | The 18-section Application 360 report card |
| `thought` | A chunk of the model's reasoning (see below) |
| `text` | A chunk of the narrative |
| `state` | Orchestrator state snapshot for the activity log |
| `error` | A structured failure with `correlation_id` and the phase it happened in |
| `done` | End of turn |

Thought streams: Agent Development Kit (ADK) marks reasoning parts with A2A part metadata `adk_thought`.
The BFF (`frontend/server/src/a2a.ts`) separates those parts and forwards them as `thought` events instead of dropping them; the SPA accumulates them per turn into a collapsible "Thinking" block that is excluded from exports.

Identity: in dev auth mode the SPA sends `userSub` and the BFF resolves it against `users.yaml`; in OIDC mode the BFF verifies the `Authorization: Bearer` JSON Web Token and **ignores** `userSub` (the token is the identity).
Either way the BFF emits the same claim shape downstream, so nothing past the BFF knows which auth mode is active.

## 3. BFF <-> Agent (A2A)

The agent is exposed via ADK's `to_a2a()` as an Agent-to-Agent (A2A) 1.0 server.
Interface facts the BFF client depends on:

- JSON-RPC methods are PascalCase and every request carries the `A2A-Version: 1.0` header.
- The BFF calls the streaming message method and reads incremental task updates; the A2A `contextId` is the conversation thread id and maps 1:1 to the ADK session.
- Identity claims travel in request `params.metadata.claims` (`{sub, name, email, groups[]}`); the agent reads them via `RunConfig.custom_metadata` and treats them as the only identity truth for the turn (FR-ID-1).
- The response text stream is multiplexed: prose is narrative, and typed payloads are embedded as fenced blocks (next section).
- Reasoning parts arrive with part metadata `adk_thought: true`.
- SSE parsing in the BFF is CRLF-safe and deduplicates repeated `artifactUpdate` frames.

The agent also serves plain `GET /status` (read-only) with the config version hash, per-battery check counts, and the last config reload error; the BFF proxies it into `/api/meta`.

### The cloudops fence protocol

[backend/src/cloudops/agent/protocol.py](backend/src/cloudops/agent/protocol.py) is the wire contract between the agent and any UI.
Typed payloads are emitted inside the A2A text stream as fenced JSON:

````
```cloudops-<kind>
{ ...payload... }
```
````

Kinds: `phase`, `context`, `clarify`, `attestation`, `app360`, `error`.
The BFF's normalizer strips fences out of the prose and re-emits each as its typed SSE event; anything outside a fence is narrative `text`.
The analyst model is explicitly instructed (via `config/agent/protocol_note.md`) never to write fences itself; only the deterministic runtime emits them.
A new UI (Slack bot, CLI, another console) needs exactly two things: an A2A client and this fence parser.

## 4. Inside the agent

The agent is a custom ADK `BaseAgent` orchestrator that runs deterministic phases before any model narration (decision D1: health attestation is enforced by the runtime, not by prompting).

Turn lifecycle:

1. **Context resolution** ([backend/src/cloudops/agent/context.py](backend/src/cloudops/agent/context.py)) - pure decision logic, no LLM.
   Claims -> candidate applications (owner_groups intersection) -> placement.
   Placement is a two-step contract (FR-CTX-2): `reg__find_placements` proposes candidates from the registry, then `ocp__verify_placement` confirms each against the cluster API.
   Environment scoping happens on registry candidates before verification (a down cluster must not silently narrow an application's environments), with a configurable default environment (FR-CTX-8).
   Outcomes: `Onboarding` (message), `Clarify` (one question, enumerated options), or `Resolved` (scope card).
2. **Attestation** - the check engine runs `config/checks/health_attestation.yaml` against every in-scope cluster through the gateway, with a per-thread verdict cache (TTL from `models.yaml`).
   Verdicts: `healthy | maintenance | degraded | unattestable` (an API server that cannot be reached is unattestable, not failed).
3. **Application 360** - `config/checks/app360.yaml` builds the 18-section report per instance.
4. **Narrative** - the analyst LLM gets a rebuilt system instruction (persona + routing + enabled skills + protocol note + grounding data assembled per invocation from `config/agent/`), a curated fence-free transcript, and the gateway toolset with a tool budget.
   Tool errors and hallucinated tool names are returned to the model as recoverable error payloads instead of crashing the turn.

Check batteries are declarative YAML: each check names a gateway tool, addresses result fields by dotted path, and maps rules to severities; they hot-reload with last-known-good semantics.
All user-facing runtime copy (onboarding, clarify templates, tool-loop guidance) lives in `config/agent/messages.yaml`.
Inference is provider-agnostic through LiteLLM: `config/models.yaml` `inference.provider: openai-compat` points at any OpenAI-compatible endpoint (local Ollama today, a hosted gateway in production); `provider: fake` (`CLOUDOPS_FAKE_LLM=1`) is a deterministic in-process model for hermetic tests.

## 5. Agent <-> Gateway <-> MCP servers

The gateway is the single Model Context Protocol (MCP) endpoint the agent knows (`CLOUDOPS_GATEWAY_URL`).
It stays domain-neutral policy: namespacing, allowlists, audit logging, and downstream supervision, nothing else.

- `config/gateway/servers.yaml` (hot-reloaded) registers each downstream server with a `prefix`, URL, timeout, and `allow_tools` list.
  A downstream tool `find_placements` under prefix `reg` is exposed to the agent as `reg__find_placements`; tools not allowlisted do not exist as far as the agent can see.
- The gateway supervises connections and reconnects on a cadence from `config/gateway/gateway.yaml`; start order between gateway and servers is not load-bearing.
- Adding a new domain (for example a ticketing MCP) is one `servers.yaml` entry plus optional checks and a skill file; no code changes.

Both domain servers are FastMCP apps served over streamable HTTP and run standalone (`make run-ocp-mcp`, `make run-registry-mcp`); they never import each other.

## 6. Registry MCP <-> MongoDB

The fleet registry answers "who exists and where is it placed" and is the one stateful component; MongoDB is the runtime truth and `config/fleet/*.yaml` is only its seed fixture (`make mongo-seed`, idempotent upserts by natural key, never a destructive sync).

Collections (database `CLOUDOPS_MONGO_DB`, default `cloudops`):

- `placements` - one document per permutation `{app_id, application, app_label, cluster, namespace, environment, lob}`; the answer set for placement and blast-radius questions.
- `clusters` - `{name, api_url, console_url, environment, region, ring, aliases[], labels{}, auth{}}`; the fleet inventory AND the credential store (auth block detailed in section 7).
- `apps` - `{app_id, application, app_label, owner_groups[], lob, tier, description, ...}`; ownership (claims' groups match `owner_groups`) and the registry-sourced Application 360 sections.

Tools (namespaced `reg__` by the gateway):

| Tool | Contract |
|---|---|
| `resolve_entity(query, kind_hint?)` | Free text -> scored matches across apps, clusters, namespaces, lines of business; exact 1.0 > alias 0.9 > substring 0.8 > fuzzy <= 0.7 so a fuzzy hit never outranks a real one |
| `find_placements(app_id?, cluster?, namespace?, environment?, lob?)` | AND-ed case-insensitive exact filters; `app_id` deliberately matches short code, display name, or pod label |
| `list_apps_on_cluster(cluster, environment?)` | What shares a cluster |
| `blast_radius(cluster?, namespace?, lob?)` | Affected apps, namespaces, LOBs, environments, plus a summary sentence |
| `get_app(app_id)` | Registry entry + placements; accepts id, name, or label |
| `list_lobs()` | LOBs with app and cluster counts |

Registry answers are labelled beliefs, never observations; the OpenShift MCP is what confirms them.
If MongoDB is unreachable every tool returns `{"error": "registry unavailable", "detail": ...}` and the server keeps serving; the agent then says it cannot consult the registry instead of inventing a fleet.
The `auth` block never appears in any tool result.

## 7. OpenShift MCP <-> the fleet

[backend/src/cloudops/mcp_servers/live_fleet.py](backend/src/cloudops/mcp_servers/live_fleet.py) resolves cluster names/aliases/labels from the Mongo `clusters` collection (queried fresh per access, so registry writes are hot) and hands out one cached `KubeClient` per cluster.
[backend/src/cloudops/mcp_servers/kube.py](backend/src/cloudops/mcp_servers/kube.py) turns one cluster record into a read-only HTTPS client; only GET requests are ever issued (NFR-SEC-1) and Secret data is never read, only names.

Cluster record `auth` forms:

- `{"type": "kubeconfig", "context": "<ctx>"}` - dev fleets; credentials stay in the kubeconfig on disk (`KUBECONFIG`, else `~/.kube/config`), supporting client certs, tokens, and token files (no exec plugins).
- `{"type": "token", "token": "..."}` - a service-account or personal bearer token stored in the record, with optional `insecure_skip_tls_verify` and inline `ca` PEM.
- `{"type": "basic", "username": "...", "password": "..."}` - the production OCP shape.
  OpenShift's API server does not accept basic auth, so the client discovers `{api_url}/.well-known/oauth-authorization-server`, runs the `openshift-challenging-client` implicit flow with the credentials, and reads `access_token`/`expires_in` from the 302 Location fragment.
  Tokens are cached per `(api_url, username)` with a 60 second expiry margin and re-exchanged once on a 401, so rotated credentials self-heal on the next call.

Tool families on the server (namespaced `ocp__`):

- Fleet resolution: `resolve_cluster`, `list_clusters`, `get_app_registry_entry`.
- Cluster state: `get_cluster_info` (reachability IS the reading; an unreachable cluster returns `reachable: false`, never a tool error; it also probes `/readyz?verbose` and reports the named failing control-plane sub-checks as `readyz_failing`, with 401/403 read as "not permitted", never as unhealthy), `get_nodes`, `get_namespaces`, `get_capacity` (requests vs allocatable computed from nodes and pods, with the minus-one-node guard), and the OpenShift-only four: `get_cluster_version`, `get_cluster_operators`, `get_machine_config_pools`, `get_pending_csrs`.
- App state: `get_workloads` (deployments, rollout history, pod pathology with named pods), `verify_placement` (`{reachable, pod_count, ready_count, verified}`; API-answered denials are `reachable: true, verified: false`, transport failures are `reachable: false`), `get_events`, `get_quotas`, `get_network`, `get_pvcs`, `get_configuration` (references only, never Secret data), `get_security_posture`, `get_autoscaling` (HorizontalPodAutoscaler + PodDisruptionBudget from the Kubernetes API).

The `applicable: false` contract: on a vanilla Kubernetes cluster (the kind dev fleet) the four OpenShift-only tools return the normal result shape with health-neutral values plus `applicable: false` and a reason, so attestation rules never fire on an API the cluster does not have.
Point the same tools at a real OCP cluster and they are the seam where real ClusterVersion/ClusterOperator/MachineConfigPool/CertificateSigningRequest reads go, with zero battery changes.

## 8. Production cutover playbook

The system was built so that going to production is data and config, not architecture.
The work, per surface:

**Fleet (the biggest one).**
Insert one `clusters` document per production cluster: real `api_url`, `environment`/`region`/`ring`/`labels` taxonomy, and `auth: {type: "basic", username, password}` records (write them into Mongo directly or through your secret-sync tooling; they must never enter the committed config tree, FR-CFG-4).
Load the real `placements` and `apps` collections from your CMDB or deployment inventory (the seeder is a reference for shape and idempotent upsert semantics; replace its YAML source with your system of record).
Then give the four OpenShift-only tools their real implementations in [backend/src/cloudops/mcp_servers/openshift/live.py](backend/src/cloudops/mcp_servers/openshift/live.py) (each currently returns the neutral `applicable: false` shape and documents what to read), and re-enable the corresponding attestation rules in `config/checks/health_attestation.yaml` (version skew, degraded operators, MachineConfigPool rollouts, pending certificate signing requests).
TLS: put the fleet CA bundle in each cluster record (`ca` PEM) rather than `insecure_skip_tls_verify`.

**Agent backend / inference.**
Point `config/models.yaml` `inference` at the hosted OpenAI-compatible endpoint (`api_base`, `model`, api key via environment); the provider abstraction means no code change.
`inference.*` binds at agent start; the `agent.*` tuning block (attestation TTL, tool budget) is hot.
Latency note: the deterministic phases stream their cards independently of model speed, so the console fills with evidence while the narrative is still generating.

**Identity.**
Set `CLOUDOPS_AUTH_MODE=oidc` with `CLOUDOPS_OIDC_JWKS_URL` (or inline JWKS), issuer, audience, and the groups-claim name.
The persona picker disappears; the bearer token becomes the identity end to end; the claim shape downstream is unchanged.
Group names in tokens must line up with `apps.owner_groups` values.

**MongoDB.**
Use a managed/replicated instance; credentials ride in `CLOUDOPS_MONGO_URL` only.
The registry lib creates its indexes idempotently on first use.
Prompts-in-Mongo is a planned follow-on: all prompt and message loading already flows through `cloudops.common.config`, so a Mongo-backed provider replaces file reads at one choke point without touching call sites.

**Topology and scale-out.**
Each MCP server, the gateway, the agent, and the BFF are independent processes with environment-driven URLs; run them as separate deployments and change the four URL variables.
The gateway is the policy pinch point: per-domain allowlists and timeouts in `servers.yaml` are your blast-radius control for new tool surfaces.
MCP servers currently bind loopback by design (NFR-SEC-4); fronting them with a mesh or authenticated ingress is deployment work, not code work.
Everything emits OpenTelemetry when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; every turn carries a `correlation_id` that surfaces in error fences and logs.

**What is intentionally absent.**
There is no metrics/Prometheus path any more: checks that only metrics could answer (alert feeds, latency SLOs, container-level usage) were removed rather than faked, and Application 360's observability section states that honestly.
If production wants them back, the shape is a new observability MCP behind the gateway (one `servers.yaml` entry + battery checks + a skill file), not a change to any existing component.

## 9. Eval harness

Every interface above is a claim about behavior, and the eval harness ([backend/src/cloudops/evals/](backend/src/cloudops/evals/)) is where those claims get exercised as scenarios rather than as unit assertions.
It plugs into the interfaces, not around them.

- **Identity (section 3).** A scenario's `persona` is the claim shape `{sub, name, email, groups[]}` and it is seeded exactly where the A2A hop seeds it, `RunConfig.custom_metadata["a2a_metadata"]["claims"]`, so ownership resolution is exercised through the production identity seam.
- **The fence protocol (section 3).** The harness consumes the agent's output the way the BFF does: it parses `cloudops-*` fences out of the text stream and treats the rest as narrative.
  That makes the fence contract testable from the outside, and it is how the always-on invariant is scored - fences are attributed to their emitting event's author, so a typed payload the model wrote is visible as a failure.
- **The gateway (section 5).** An ASGI recorder in front of the real gateway logs every `tools/call`, which is what the analyst's tool budget is scored against.
  The deterministic phases' calls and the model's are told apart by the `narrative` phase tick, since the orchestrator closes its gateway session before the analyst runs.
- **The MCP servers (sections 6 and 7).** The registry MCP is the production server over an in-memory MongoDB the real seeder loaded from the scenario's own config plane; the OpenShift MCP is the production server over a `LiveOpenShiftBackend` whose only substitution is the cluster socket (`build_server(backend=...)`, the seam section 7 already describes).
- **The config plane (section 4).** Each scenario gets a copy of the committed `config/`, with only the registry downstream URL and (when the scenario overrides it) `fleet/applications.yaml` rewritten.
  Batteries, prompts, messages and gateway policy are the shipped files, so a scenario measures the shipped configuration.
- **Inference (section 8).** `--mode fake` selects the `provider: fake` seam; `--mode live` uses whatever `config/models.yaml` names.
  The LLM judges reach the same OpenAI-compatible endpoint through a small direct client rather than through ADK, so the thing under test is not in the path of its own measurement.

Suites are YAML under `backend/evals/suites/` with the schema documented in `context-resolution.yaml`; judge prompts are Markdown under `backend/evals/judges/`.
Adding coverage for a new domain is the same shape as adding the domain itself: a `servers.yaml` entry, a battery, a skill file - and a suite.

## 10. Contract invariants worth protecting

These are the load-bearing rules; break them and distant components fail in quiet ways.

1. Only the deterministic runtime emits `cloudops-*` fences; the model narrates in prose.
2. The registry proposes, the cluster confirms; nothing is ever reported as running from registry data alone.
3. Unreachable is not unhealthy: transport failure surfaces as `reachable: false` / `unattestable`, never as a degraded verdict or a tool exception.
4. Credentials live in the environment, the kubeconfig, or the Mongo `auth` block; never in committed config, never in logs (redaction scrubs secret-shaped values), never in tool results.
5. Every read to a cluster is a GET; Secret names may be listed, Secret data may not be fetched.
6. Tool allowlists in `servers.yaml` are the source of truth for what the model can do; a tool not listed there does not exist.
7. The claim shape `{sub, name, email, groups[]}` is identical in dev and OIDC modes; downstream code must never branch on auth mode.
