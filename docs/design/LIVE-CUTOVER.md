# Live Cutover Design (M2): mocks out, MongoDB registry in

Status: approved plan for the live-cutover milestone (2026-08-28).
Mock mode is preserved for review on branch `archive/mock-mode` (cut at 6bc832c) and then removed from develop.

## Goals

1. One backend: live cluster telemetry only. Delete mock mode, the mockfleet package, `config/mock/`, and every `CLOUDOPS_BACKEND_MODE` branch.
2. Remove the Prometheus/observability MCP server. Everything it answered that the Kubernetes/OCP API can answer moves to `ocp__*` tools; everything it answered that only metrics can answer is dropped from the check batteries (honestly, not faked).
3. New MongoDB-backed fleet registry service (its own MCP server) that resolves ambiguous text to fleet entities and answers blast-radius questions.
4. Cluster credentials (username/password against API endpoints) live in MongoDB, not kubeconfig-only.
5. Every MCP server stands up separately; config is file-based (prompts and conversational copy included), structured so a MongoDB provider can replace files later.
6. Persona baseline: an unregistered user is the default; personas are switchable tactically.
7. Console UX: clear conversation, model thought streams, slash commands.

## MongoDB

- Runs as a Docker container `cloudops-mongo` on 127.0.0.1:27017 (compose file in `deploy/mongo/`). Database `cloudops`.
- Env: `CLOUDOPS_MONGO_URL` (default `mongodb://127.0.0.1:27017`), `CLOUDOPS_MONGO_DB` (default `cloudops`).
- Collections:
  - `placements`: one document per permutation `{app_id, application, app_label, cluster, namespace, environment, lob}`. Indexed on each of app_id/cluster/namespace/environment/lob.
  - `clusters`: `{name, api_url, console_url, environment, region, ring, aliases[], labels{}, auth{}}` where `auth` is one of:
    - `{type: "basic", username, password}` - exchanged for a bearer token via the cluster's OAuth server (`/.well-known/oauth-authorization-server` discovery, `openshift-challenging-client` implicit flow); token cached with TTL, re-exchanged on 401.
    - `{type: "token", token}`
    - `{type: "kubeconfig", context}` - the local kind dev fleet path.
  - `apps`: application registry `{app_id, application, app_label, owner_groups[], lob, tier, description}`.
- Seeding: `make mongo-seed` runs `backend/src/cloudops/registry/seed.py`, which loads `config/fleet/*.yaml` seed data idempotently (upsert by natural key). YAML files become seed fixtures; runtime truth is Mongo.
- Prompts stay in `config/agent/` files for now; the config loader keeps a single choke point (`cloudops.common.config`) so a Mongo-backed provider can be slotted in later without touching call sites.

## Registry MCP server (`reg__*`)

New package `backend/src/cloudops/mcp_servers/registry/` (FastMCP, port 8013, `CLOUDOPS_MCP_REGISTRY_PORT`), standalone via `python -m cloudops.mcp_servers.registry`, registered in `config/gateway/servers.yaml` with namespace `reg`. Backed by a shared data-access lib `backend/src/cloudops/registry/` (pymongo) that the OpenShift MCP also uses for cluster records.

Tools (result shapes are the contract; keep them stable):

- `reg__resolve_entity(query, kind_hint?)` -> `{query, matches: [{kind: app|cluster|namespace|lob, id, score, detail{}}], suggestion?}`. Fuzzy resolution across app ids/names, cluster names/aliases, namespaces, LOBs ("is app SSOP down?" -> app SSOP).
- `reg__find_placements(app_id?, cluster?, namespace?, environment?, lob?)` -> `{count, placements: [{app_id, application, app_label, cluster, namespace, environment, lob}]}`.
- `reg__list_apps_on_cluster(cluster, environment?)` -> apps + namespaces + lobs on that cluster.
- `reg__blast_radius(cluster?, namespace?, lob?)` -> `{scope, apps[], namespaces[], lobs[], environments[], summary}` (what is affected if X goes down).
- `reg__get_app(app_id)` -> registry entry (replaces `ocp__get_app_registry_entry` as source of truth; the ocp tool delegates or is removed).
- `reg__list_lobs()` -> distinct LOBs with app counts.

## Placement and context resolution without Prometheus

`agent/context.py` placement flow becomes: candidates from `reg__find_placements`, then LIVE verification of each candidate via `ocp__verify_placement(cluster, namespace, app_label)` (new tool: pods matching the selector in that namespace; returns pod_count, ready_count, reachable). Unreachable clusters yield `verified: false` placements, reported honestly. The FR-CTX-2 principle (never trust the registry alone) is preserved: the registry proposes, the cluster API confirms.

## OpenShift MCP changes

- `LiveFleet` reads cluster records from Mongo (`registry` lib) instead of `fleet.yaml live:`; per-cluster `KubeClient` is built from the record's `auth` block (basic -> OAuth exchange; token; kubeconfig context). `kube.py` loses `prom()`.
- New tools: `ocp__verify_placement`, `ocp__get_capacity` (requests vs allocatable from nodes+pods), `ocp__get_autoscaling` (HPA from autoscaling/v2 + PDB from policy/v1), `ocp__get_namespaces` (already implemented on the backend, expose it).
- OpenShift-only tools keep the `applicable: false` contract on vanilla clusters.

## Check battery changes

- `health_attestation.yaml`: keep api-reachability, cluster-version, cluster-operators, nodes-ready, mcp-rollout, pending-csrs. Replace `capacity` with `ocp__get_capacity`. Drop etcd, firing-alerts, watchdog-present, apiserver-slo, cert-expiry (Prometheus-native; removed, not faked).
- `app360.yaml`: port hpa-maxed and pdb-disruptions to `ocp__get_autoscaling`. Drop app-alerts, error-rate, latency, memory-headroom, cpu-throttling. Section 9 keeps an honest "metrics not available in this deployment" registry/narrative note rather than fabricated readings.
- Delete `config/agent/skills/prometheus-query-crafting.md`; add a fleet-registry skill teaching the `reg__*` tools and blast-radius phrasing.

## Config and prompt extraction

- Conversational copy out of code into hot-reloaded `config/agent/messages.yaml` (onboarding/clarify templates from context.py, `_PROTOCOL_NOTE` from prompts.py as `config/agent/protocol_note.md`, analyst tool-loop guidance).
- Behavioral constants into config: gateway reconnect delay (gateway.yaml, new), kube client timeout, gateway_client default timeout, model/temperature/max_tokens fallbacks (models.yaml is authoritative; code fallbacks fail loudly instead), MCP bind host, BFF body limit + status timeout, web meta-poll interval + log cap (served to the SPA via `GET /api/ui` from `config/ui/console.yaml`).
- `.env.example` rewritten: mongo URL, no backend mode, no thanos vars.

## Console UX

- Clear conversation: a "New thread" control in the chat header dispatching the existing `reset` action (persona preserved); also a `/clear` slash command.
- Thought streams: BFF forwards `adk_thought` parts as SSE `thought` events instead of dropping them; the web app renders a collapsible, dimmed "Thinking" block per turn (default collapsed, streaming live).
- Slash commands: typing `/` in the composer opens a command palette. `GET /api/commands` on the BFF serves: built-ins (`/clear`, `/attest <cluster>`, `/persona <sub>` dev-mode only) plus one command per enabled skill in `config/agent/agent.yaml` (sends a task-hint message invoking that skill). Quick-pick buttons bypass the palette.
- Persona baseline: `users.yaml` gains a `guest` persona (no groups); the BFF/web default selection is `guest` so the out-of-box experience is the unregistered-user onboarding; the masthead picker sets personas tactically.

## Service layout after cutover

agent 8001, gateway 8010, ocp-mcp 8011, registry-mcp 8013, BFF 8080, web 5173, mongo 27017. Observability MCP (8012) is gone. Each MCP server runs standalone (`make run-ocp-mcp`, `make run-registry-mcp`, documented in README).

## Testing strategy

- Test doubles live only under `backend/tests/`: a `FakeKube` transport (canned Kubernetes API JSON via httpx MockTransport) replaces the mock World for unit/E2E tests; `mongomock` (or an ephemeral real Mongo when available) backs registry tests. FakeLlm stays (`provider: fake`) - it fakes inference, not telemetry.
- `test_world_and_context.py`, `test_check_engine.py`, `test_orchestrator.py`, `test_e2e_triage.py`, `conftest.py` are rewritten against FakeKube + registry fixtures.
- `live_smoke` rewritten: no Prometheus checks; adds registry resolution, blast radius, placement verification, capacity, autoscaling against the kind fleet + real Mongo.
