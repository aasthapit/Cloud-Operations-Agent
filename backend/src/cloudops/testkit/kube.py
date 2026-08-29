"""Canned Kubernetes API payloads and the client that serves them.

``fake_kube`` returns a REAL ``KubeClient`` whose httpx transport is an
``httpx.MockTransport`` answering from a ``ClusterFixture``. Every code path
above the socket - selector fallback, list parsing, quantity arithmetic,
error handling - is the production one; only the wire is canned. Building the
client through ``__new__`` skips the kubeconfig read, which is the only part
of ``KubeClient`` a double has no business exercising.

The builders below are deliberately small and positional-friendly: a fixture
is meant to be readable as a sentence ("two ready nodes, one crash-looping
pod"), because the fixtures ARE the specification of what each verdict means.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from cloudops.mcp_servers.kube import KubeClient

NAME_LABEL = "app.kubernetes.io/name"


# ---------------------------------------------------------------------------
# Kubernetes payload builders
# ---------------------------------------------------------------------------


def node(
    name: str, *, ready: bool = True, unschedulable: bool = False,
    role: str = "control-plane", cpu: str = "4", memory: str = "8Gi", pods: str = "110",
    pressure: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One node. ``pressure`` names conditions to report True (MemoryPressure,
    DiskPressure, PIDPressure, NetworkUnavailable), which is what the nodes
    check reads for its warn-level pressure rule."""
    conditions = [{
        "type": "Ready", "status": "True" if ready else "False",
        "reason": "KubeletReady" if ready else "KubeletNotReady",
        "lastTransitionTime": "2026-08-27T10:00:00Z",
    }]
    conditions += [
        {"type": condition, "status": "True", "reason": condition,
         "lastTransitionTime": "2026-08-27T10:00:00Z"}
        for condition in pressure
    ]
    return {
        "metadata": {"name": name, "labels": {f"node-role.kubernetes.io/{role}": ""}},
        "spec": {"unschedulable": unschedulable},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory, "pods": pods},
            "conditions": conditions,
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
    readyz_failing: list[str] = field(default_factory=list)
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
        if path == "/readyz":
            subchecks = ["ping", "etcd", "informer-sync"]
            lines = [
                (f"[-]{name} failed: reason withheld" if name in fixture.readyz_failing
                 else f"[+]{name} ok")
                for name in sorted(set(subchecks) | set(fixture.readyz_failing))
            ]
            failed = bool(fixture.readyz_failing)
            lines.append("readyz check failed" if failed else "readyz check passed")
            return httpx.Response(500 if failed else 200, content="\n".join(lines),
                                  headers={"content-type": "text/plain"})
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
