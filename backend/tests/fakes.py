"""Hermetic test doubles for the live backend (NFR-QE-1).

Nothing in the suite may need a cluster, a Mongo, an inference server, or a
port. Three doubles carry that:

- ``FakeKube`` is a REAL ``KubeClient`` whose httpx transport is an
  ``httpx.MockTransport`` serving canned Kubernetes API payloads. Every code
  path under test - selector fallback, list parsing, quantity arithmetic,
  error handling - is the production one; only the socket is fake. Building
  the client through ``__new__`` skips the kubeconfig read, which is the only
  part of ``KubeClient`` a test has no business exercising.
- ``FakeFleet`` is a ``LiveFleet`` reading the committed fleet.yaml (so the
  cluster names, aliases and environments under test are the real ones) that
  hands out a per-cluster ``FakeKube``.
- ``FakeGateway`` is a ``GatewayClient``-shaped facade dispatching ``ocp__*``
  into a ``LiveOpenShiftBackend`` over those fakes and ``reg__*`` into
  ``FakeRegistry``. Both halves answer the way the real MCP servers do, so a
  test that talks to it is testing the tool contract, not a mock of a mock.

The canned world is deliberately small and opinionated: one healthy spoke,
one degraded spoke, one unreachable cluster. That is enough to exercise every
verdict the battery can reach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from cloudops.agent.gateway_client import ToolCallError
from cloudops.common.config import load_yaml
from cloudops.mcp_servers.kube import KubeClient
from cloudops.mcp_servers.live_fleet import LiveFleet
from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

HEALTHY = "acm-spoke-1a"
DEGRADED = "acm-spoke-2a"
UNREACHABLE = "acm-hub-2"
APP_NS = "payments-prod"
APP_LABEL = "payments-api"

NAME_LABEL = "app.kubernetes.io/name"


# ---------------------------------------------------------------------------
# Kubernetes payload builders
# ---------------------------------------------------------------------------


def node(
    name: str, *, ready: bool = True, unschedulable: bool = False,
    role: str = "control-plane", cpu: str = "4", memory: str = "8Gi", pods: str = "110",
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {f"node-role.kubernetes.io/{role}": ""}},
        "spec": {"unschedulable": unschedulable},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory, "pods": pods},
            "conditions": [{
                "type": "Ready", "status": "True" if ready else "False",
                "reason": "KubeletReady" if ready else "KubeletNotReady",
                "lastTransitionTime": "2026-08-27T10:00:00Z",
            }],
        },
    }


def pod(
    name: str, app_label: str, *, phase: str = "Running", ready: bool = True,
    crashloop: bool = False, cpu_request: str = "100m", memory_request: str = "128Mi",
    configmaps: tuple[str, ...] = (), secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    statuses = [{
        "name": "app", "ready": ready and not crashloop, "restartCount": 5 if crashloop else 0,
        "state": ({"waiting": {"reason": "CrashLoopBackOff", "message": "back-off restarting"}}
                  if crashloop else {"running": {}}),
        # A far-future finishedAt keeps the "restarts in the last hour" window
        # open however long after the fixture was written the suite runs.
        "lastState": ({"terminated": {"reason": "Error", "exitCode": 1,
                                      "finishedAt": "2999-01-01T00:00:00Z"}}
                      if crashloop else {}),
    }]
    return {
        "metadata": {"name": name, "namespace": "", "labels": {NAME_LABEL: app_label},
                     "creationTimestamp": "2026-08-01T00:00:00Z"},
        "spec": {
            "serviceAccountName": "default",
            "containers": [{
                "name": "app", "image": "busybox:1.36",
                "resources": {"requests": {"cpu": cpu_request, "memory": memory_request}},
                "envFrom": [{"configMapRef": {"name": c}} for c in configmaps],
            }],
            "volumes": [{"name": s, "secret": {"secretName": s}} for s in secrets],
        },
        "status": {"phase": phase, "containerStatuses": statuses},
    }


def deployment(
    name: str, app_label: str, *, replicas: int = 2, ready: int = 2,
    progressing_reason: str = "NewReplicaSetAvailable",
) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {NAME_LABEL: app_label},
                     "creationTimestamp": "2026-08-01T00:00:00Z"},
        "spec": {"replicas": replicas, "strategy": {"type": "RollingUpdate"},
                 "template": {"spec": {"containers": [{"image": "busybox:1.36"}]}}},
        "status": {"readyReplicas": ready, "availableReplicas": ready,
                   "updatedReplicas": replicas,
                   "conditions": [{"type": "Progressing", "status": "True",
                                   "reason": progressing_reason}]},
    }


def replicaset(name: str, app_label: str, *, revision: int = 1,
               replicas: int = 2, ready: int = 2) -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {NAME_LABEL: app_label},
                     "creationTimestamp": "2026-08-01T00:00:00Z",
                     "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
        "spec": {"replicas": replicas,
                 "template": {"spec": {"containers": [{"image": "busybox:1.36"}]}}},
        "status": {"readyReplicas": ready},
    }


def event(reason: str, obj: str, *, count: int = 1, message: str = "") -> dict[str, Any]:
    return {
        "metadata": {"name": f"{obj}.{reason}"},
        "type": "Warning", "reason": reason, "count": count,
        "lastTimestamp": "2026-08-27T10:00:00Z", "message": message,
        "involvedObject": {"kind": "Pod", "name": obj},
    }


def namespace(name: str) -> dict[str, Any]:
    return {"metadata": {"name": name, "labels": {}, "creationTimestamp": "2026-08-01T00:00:00Z"},
            "status": {"phase": "Active"}}


def hpa(name: str, app_label: str | None, target: str, *, minimum: int = 2,
        maximum: int = 6, current: int = 2, desired: int = 2) -> dict[str, Any]:
    labels = {NAME_LABEL: app_label} if app_label else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {"minReplicas": minimum, "maxReplicas": maximum,
                 "scaleTargetRef": {"kind": "Deployment", "name": target}},
        "status": {"currentReplicas": current, "desiredReplicas": desired},
    }


def pdb(name: str, app_label: str | None, *, allowed: int = 1,
        expected: int = 2) -> dict[str, Any]:
    selector = {NAME_LABEL: app_label} if app_label else {}
    return {
        "metadata": {"name": name, "labels": {}},
        "spec": {"selector": {"matchLabels": selector}},
        "status": {"disruptionsAllowed": allowed, "expectedPods": expected,
                   "currentHealthy": expected, "desiredHealthy": expected - 1},
    }


def quota(name: str, resource: str, used: str, hard: str) -> dict[str, Any]:
    return {"metadata": {"name": name},
            "status": {"hard": {resource: hard}, "used": {resource: used}}}


# ---------------------------------------------------------------------------
# the canned cluster world
# ---------------------------------------------------------------------------


@dataclass
class NamespaceFixture:
    pods: list[dict[str, Any]] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)
    replicasets: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)
    ingresses: list[dict[str, Any]] = field(default_factory=list)
    networkpolicies: list[dict[str, Any]] = field(default_factory=list)
    persistentvolumeclaims: list[dict[str, Any]] = field(default_factory=list)
    resourcequotas: list[dict[str, Any]] = field(default_factory=list)
    limitranges: list[dict[str, Any]] = field(default_factory=list)
    horizontalpodautoscalers: list[dict[str, Any]] = field(default_factory=list)
    poddisruptionbudgets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ClusterFixture:
    """One cluster's canned API surface."""

    nodes: list[dict[str, Any]] = field(default_factory=lambda: [node("cp-1")])
    namespaces: dict[str, NamespaceFixture] = field(default_factory=dict)
    version: str = "v1.33.7"
    reachable: bool = True

    def ns(self, name: str) -> NamespaceFixture:
        return self.namespaces.setdefault(name, NamespaceFixture())


_CORE_KINDS = {
    "pods", "events", "services", "persistentvolumeclaims",
    "resourcequotas", "limitranges",
}
_GROUPED_KINDS = {
    "deployments", "replicasets", "ingresses", "networkpolicies",
    "horizontalpodautoscalers", "poddisruptionbudgets",
}


def _matches(item: dict[str, Any], selector: str | None) -> bool:
    """Honour the two label selectors the backend ever sends."""
    if not selector or "=" not in selector:
        return True
    key, _, value = selector.partition("=")
    return (item.get("metadata", {}).get("labels") or {}).get(key) == value


class FakeKubeError(AssertionError):
    """A path the fixture does not model, surfaced loudly rather than as {}."""


def fake_kube(fixture: ClusterFixture) -> KubeClient:
    """A real KubeClient whose transport answers from ``fixture``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if not fixture.reachable:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        selector = request.url.params.get("labelSelector")
        body = _route(fixture, path, selector)
        if body is None:
            return httpx.Response(404, json={"kind": "Status", "code": 404, "message": path})
        return httpx.Response(200, content=json.dumps(body),
                              headers={"content-type": "application/json"})

    client = KubeClient.__new__(KubeClient)
    client.context = "fake"
    client.server = "https://fake.cluster:6443"
    client._client = httpx.Client(  # noqa: SLF001 - constructing the double IS the point
        base_url=client.server, transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    return client


def _route(fixture: ClusterFixture, path: str, selector: str | None) -> Any:
    parts = [p for p in path.split("/") if p]
    if path == "/version":
        return {"gitVersion": fixture.version}
    if path == "/api/v1/nodes":
        return {"items": fixture.nodes}
    if path == "/api/v1/namespaces":
        return {"items": [namespace(n) for n in sorted(fixture.namespaces)]}
    if path == "/api/v1/pods":  # cluster-wide list, used by get_capacity
        return {"items": [
            dict(p, metadata={**p["metadata"], "namespace": name})
            for name, ns in fixture.namespaces.items() for p in ns.pods
        ]}
    if path.startswith("/apis/apps/v1/namespaces/kube-system/deployments/"):
        return {"status": {"readyReplicas": 2}}

    # /api/v1/namespaces/<ns>[/<kind>] and /apis/<group>/<v>/namespaces/<ns>/<kind>
    if "namespaces" in parts:
        idx = parts.index("namespaces")
        if len(parts) <= idx + 1:
            return None
        name = parts[idx + 1]
        ns = fixture.namespaces.get(name)
        if ns is None:
            return None
        if len(parts) == idx + 2:
            return namespace(name)
        kind = parts[idx + 2]
        if kind not in _CORE_KINDS | _GROUPED_KINDS:
            raise FakeKubeError(f"unmodelled resource kind in {path!r}")
        items = getattr(ns, kind)
        return {"items": [i for i in items if _matches(i, selector)]}
    raise FakeKubeError(f"unmodelled path {path!r}")


# ---------------------------------------------------------------------------
# fleet and registry doubles
# ---------------------------------------------------------------------------


class FakeFleet(LiveFleet):
    """LiveFleet over the committed fleet.yaml, handing out FakeKube clients."""

    def __init__(self, clusters: dict[str, ClusterFixture]) -> None:
        super().__init__()
        self._fixtures = clusters
        self._fakes: dict[str, KubeClient] = {}

    def fixture(self, cluster: str) -> ClusterFixture:
        return self._fixtures.setdefault(cluster, ClusterFixture())

    def client(self, cluster: str) -> KubeClient:
        self.entry(cluster)  # keep unknown-cluster behaviour honest
        if cluster not in self._fakes:
            self._fakes[cluster] = fake_kube(self.fixture(cluster))
        return self._fakes[cluster]

    def version(self, cluster: str) -> str | None:
        return self.fixture(cluster).version if self.fixture(cluster).reachable else None


class FakeRegistry:
    """The reg__* contract over the committed seed data.

    The real registry lives in Mongo and is another worker's package; what the
    agent depends on is the SHAPE of its answers, so the double serves exactly
    that shape from config/fleet/applications.yaml, which is the same seed the
    real registry is loaded from.
    """

    def __init__(self, config_dir: Path = CONFIG_DIR) -> None:
        apps = load_yaml(config_dir / "fleet" / "applications.yaml")["applications"]
        self.apps = {str(a["application"]): a for a in apps}
        self.placements = [
            {
                "app_id": str(a["application"]), "application": str(a["application"]),
                "app_label": str(a.get("app_label", a["application"])),
                "cluster": i["cluster"], "namespace": i["namespace"],
                "environment": i["environment"],
                "lob": str(a.get("owner_groups", ["unknown"])[0]),
            }
            for a in apps for i in a.get("instances", [])
        ]

    def find_placements(self, **filters: Any) -> dict[str, Any]:
        app_id = filters.get("app_id")
        rows = [
            p for p in self.placements
            if (app_id is None or app_id in (p["app_id"], p["app_label"]))
            and all(filters.get(k) in (None, p[k])
                    for k in ("cluster", "namespace", "environment", "lob"))
        ]
        return {"count": len(rows), "placements": rows}

    def resolve_entity(self, query: str, kind_hint: str | None = None) -> dict[str, Any]:
        q = query.strip().lower()
        matches = [
            {"kind": "app", "id": p["app_id"], "score": 1.0 if q == p["app_id"].lower() else 0.6,
             "detail": {"app_label": p["app_label"], "lob": p["lob"]}}
            for p in {p["app_id"]: p for p in self.placements}.values()
            if q in p["app_id"].lower()
        ]
        if kind_hint:
            matches = [m for m in matches if m["kind"] == kind_hint]
        return {"query": query, "matches": matches,
                "suggestion": None if matches else "no fleet entity matched"}

    def list_apps_on_cluster(self, cluster: str, environment: str | None = None) -> dict[str, Any]:
        rows = self.find_placements(cluster=cluster, environment=environment)["placements"]
        return {"cluster": cluster, "apps": sorted({p["app_id"] for p in rows}),
                "namespaces": sorted({p["namespace"] for p in rows}),
                "lobs": sorted({p["lob"] for p in rows})}

    def blast_radius(self, **scope: Any) -> dict[str, Any]:
        rows = self.find_placements(**scope)["placements"]
        apps = sorted({p["app_id"] for p in rows})
        return {
            "scope": {k: v for k, v in scope.items() if v is not None},
            "apps": apps, "namespaces": sorted({p["namespace"] for p in rows}),
            "lobs": sorted({p["lob"] for p in rows}),
            "environments": sorted({p["environment"] for p in rows}),
            "summary": f"{len(apps)} application(s) affected",
        }

    def get_app(self, app_id: str) -> dict[str, Any]:
        entry = self.apps.get(app_id)
        return {"app_id": app_id, "found": entry is not None,
                **({k: v for k, v in entry.items() if k != "instances"} if entry else {})}

    def list_lobs(self) -> dict[str, Any]:
        counts: dict[str, set[str]] = {}
        for p in self.placements:
            counts.setdefault(p["lob"], set()).add(p["app_id"])
        return {"lobs": [{"lob": k, "app_count": len(v)} for k, v in sorted(counts.items())]}


class FakeGateway:
    """A GatewayClient-shaped facade over the live backend and the registry."""

    def __init__(self, fleet: FakeFleet | None = None,
                 registry: FakeRegistry | None = None) -> None:
        self.fleet = fleet or FakeFleet(default_world())
        self.ocp = LiveOpenShiftBackend(self.fleet)
        self.registry = registry or FakeRegistry()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(
        self, tool: str, args: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((tool, args))
        prefix, _, name = tool.partition("__")
        target = {"ocp": self.ocp, "reg": self.registry}.get(prefix)
        method = getattr(target, name, None) if target is not None else None
        if method is None:
            raise ToolCallError(f"unknown tool {tool}")
        result: dict[str, Any] = method(**args)
        return result


# ---------------------------------------------------------------------------
# the default world
# ---------------------------------------------------------------------------


def default_world() -> dict[str, ClusterFixture]:
    """One healthy spoke, one degraded spoke, one unreachable cluster.

    Enough to reach every cluster verdict the battery can produce: healthy,
    degraded (a NotReady node), maintenance (a cordoned node) and unattestable
    (an API server that does not answer).
    """
    healthy = ClusterFixture(nodes=[node("cp-1"), node("worker-1", role="worker")])
    payments = healthy.ns(APP_NS)
    payments.pods = [pod("payments-api-1", APP_LABEL, configmaps=("payments-api-config",),
                         secrets=("payments-db",)),
                     pod("payments-api-2", APP_LABEL)]
    payments.deployments = [deployment("payments-api", APP_LABEL)]
    payments.replicasets = [replicaset("payments-api-abc", APP_LABEL)]
    payments.resourcequotas = [quota("compute", "cpu", "1", "10")]
    payments.horizontalpodautoscalers = [hpa("payments-api", APP_LABEL, "payments-api")]
    payments.poddisruptionbudgets = [pdb("payments-api", APP_LABEL)]
    # The retail applications the registry also claims here, so a resolution
    # test can exercise an app other than payments-api end to end.
    for app, ns_name in (("catalog", "catalog-prod"), ("checkout", "checkout-prod")):
        retail = healthy.ns(ns_name)
        retail.pods = [pod(f"{app}-1", app)]
        retail.deployments = [deployment(app, app, replicas=1, ready=1)]
    healthy.ns("kube-system")
    healthy.ns("default")

    degraded = ClusterFixture(
        nodes=[node("cp-1"), node("worker-1", role="worker", ready=False)])
    broken = degraded.ns(APP_NS)
    broken.pods = [pod("payments-api-1", APP_LABEL, ready=False, crashloop=True),
                   pod("payments-api-2", APP_LABEL, phase="Pending", ready=False)]
    broken.deployments = [deployment("payments-api", APP_LABEL, ready=0)]
    broken.replicasets = [replicaset("payments-api-abc", APP_LABEL, ready=0)]
    broken.events = [event("BackOff", "payments-api-1", count=12,
                           message="Back-off restarting failed container app")]
    # HPA pinned at max, PDB with nothing to give: the two autoscaling findings
    # the App 360 battery still asserts now that metrics are gone.
    broken.horizontalpodautoscalers = [
        hpa("payments-api", APP_LABEL, "payments-api", current=6, desired=8)]
    broken.poddisruptionbudgets = [pdb("payments-api", APP_LABEL, allowed=0)]
    degraded.ns("kube-system")

    maintenance = ClusterFixture(
        nodes=[node("cp-1"), node("worker-1", role="worker", unschedulable=True)])
    maintenance.ns("logistics-dev").pods = [pod("inventory-sync-1", "inventory-sync")]
    maintenance.ns("logistics-dev").deployments = [
        deployment("inventory-sync", "inventory-sync", replicas=1, ready=1)]

    hub = ClusterFixture()
    audit = hub.ns("audit-prod")
    audit.pods = [pod("audit-log-1", "audit-log")]
    audit.deployments = [deployment("audit-log", "audit-log", replicas=1, ready=1)]

    return {
        HEALTHY: healthy,
        DEGRADED: degraded,
        "acm-spoke-1b": maintenance,
        "acm-spoke-2b": ClusterFixture(),
        "acm-hub-1": hub,
        UNREACHABLE: ClusterFixture(reachable=False),
    }


# ---------------------------------------------------------------------------
# a registry MCP server for the in-process E2E harness
# ---------------------------------------------------------------------------

REGISTRY_TOOLS = (
    "resolve_entity", "find_placements", "list_apps_on_cluster",
    "blast_radius", "get_app", "list_lobs",
)


def build_registry_server(port: int, registry: FakeRegistry | None = None) -> Any:
    """A FastMCP server serving the reg__* contract from FakeRegistry.

    The real registry MCP is backed by Mongo and lands separately; what the
    agent and the gateway depend on is the tool NAMES and result shapes, so
    the E2E harness stands this up on a kernel-assigned port and registers it
    the same way the real one is registered.
    """
    from mcp.server.fastmcp import FastMCP

    reg = registry or FakeRegistry()
    mcp = FastMCP("registry-mcp", instructions="Fleet registry lookups.",
                  host="127.0.0.1", port=port, streamable_http_path="/mcp",
                  stateless_http=True)

    @mcp.tool()
    def resolve_entity(query: str, kind_hint: str | None = None) -> dict[str, Any]:
        """Resolve free text to fleet entities (app, cluster, namespace, lob)."""
        return reg.resolve_entity(query, kind_hint)

    @mcp.tool()
    def find_placements(
        app_id: str | None = None, cluster: str | None = None,
        namespace: str | None = None, environment: str | None = None,
        lob: str | None = None,
    ) -> dict[str, Any]:
        """Registry placement candidates matching any subset of filters."""
        return reg.find_placements(app_id=app_id, cluster=cluster, namespace=namespace,
                                   environment=environment, lob=lob)

    @mcp.tool()
    def list_apps_on_cluster(cluster: str, environment: str | None = None) -> dict[str, Any]:
        """Applications, namespaces and LOBs registered on one cluster."""
        return reg.list_apps_on_cluster(cluster, environment)

    @mcp.tool()
    def blast_radius(
        cluster: str | None = None, namespace: str | None = None, lob: str | None = None,
    ) -> dict[str, Any]:
        """What is affected if this scope goes down."""
        return reg.blast_radius(cluster=cluster, namespace=namespace, lob=lob)

    @mcp.tool()
    def get_app(app_id: str) -> dict[str, Any]:
        """The registry entry for one application."""
        return reg.get_app(app_id)

    @mcp.tool()
    def list_lobs() -> dict[str, Any]:
        """Distinct lines of business with application counts."""
        return reg.list_lobs()

    return mcp
