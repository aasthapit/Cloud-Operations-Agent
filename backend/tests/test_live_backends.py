"""Live-backend tests.

Two layers, both hermetic by default (NFR-QE-1):

1. Unit tests over the live backends with a fake KubeClient, asserting the
   thing that actually matters about live mode: every result carries the same
   keys as the mock World's result for the same tool (decision D6), and the
   OpenShift-only tools land as a pass, not a failure, on vanilla Kubernetes.
2. A live_smoke-marked module that talks to the real kind fleet and is skipped
   unless CLOUDOPS_LIVE_SMOKE=1, so `make test` never depends on a running
   cluster.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from conftest import CONFIG_DIR

from cloudops.agent.checks import derive_verdict, evaluate_check
from cloudops.agent.models import AttestationBattery, ClusterVerdict
from cloudops.common.config import load_yaml
from cloudops.mcp_servers.kube import VANILLA_K8S_REASON
from cloudops.mcp_servers.live_fleet import LiveFleet
from cloudops.mcp_servers.observability.live import LiveObservabilityBackend
from cloudops.mcp_servers.openshift.live import (
    LiveOpenShiftBackend,
    _ago,
    _parse_quantity,
    _split_image,
)

CLUSTER = "acm-spoke-1a"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeKube:
    """A KubeClient stand-in answering from a canned {path: payload} map.

    Paths are matched by prefix so query parameters (label selectors) do not
    have to be replayed exactly; the backends' selector logic is exercised
    separately by the live smoke run.
    """

    server = "https://127.0.0.1:6443"

    def __init__(self, payloads: dict[str, Any], prom: dict[str, Any] | None = None) -> None:
        self.payloads = payloads
        self.prom_payloads = prom or {}

    def get(self, path: str, **_params: Any) -> dict[str, Any]:
        for prefix, payload in self.payloads.items():
            if path.startswith(prefix):
                return dict(payload)
        return {"items": []}

    def get_text(self, path: str, **_params: Any) -> str:
        return str(self.payloads.get(path + ":text", ""))

    def items(self, path: str, **params: Any) -> list[dict[str, Any]]:
        return list(self.get(path, **params).get("items") or [])

    def prom(self, subpath: str, **params: Any) -> dict[str, Any]:
        query = str(params.get("query", ""))
        for key, payload in self.prom_payloads.items():
            if key in query or key == subpath:
                return payload
        return {"data": {"result": []}}


class FakeFleet(LiveFleet):
    """LiveFleet reading the committed config but handing out a FakeKube."""

    def __init__(self, client: FakeKube) -> None:
        super().__init__()
        self._fake = client

    def client(self, cluster: str) -> Any:
        self.entry(cluster)  # keep unknown-cluster behaviour honest
        return self._fake

    def version(self, cluster: str) -> str | None:
        return "v1.33.7"


def node(name: str, ready: bool = True, unschedulable: bool = False,
         role: str = "control-plane") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "labels": {f"node-role.kubernetes.io/{role}": ""}},
        "spec": {"unschedulable": unschedulable},
        "status": {"conditions": [
            {"type": "Ready", "status": "True" if ready else "False",
             "reason": "KubeletReady" if ready else "KubeletNotReady",
             "lastTransitionTime": "2026-08-27T10:00:00Z"},
        ]},
    }


@pytest.fixture
def kube() -> FakeKube:
    return FakeKube(
        payloads={
            "/version": {"gitVersion": "v1.33.7"},
            "/api/v1/nodes": {"items": [node("cp-1")]},
            "/api/v1/namespaces/payments-prod/pods": {"items": [{
                "metadata": {"name": "payments-api-1"},
                "spec": {"serviceAccountName": "default", "containers": [{
                    "name": "payments-api",
                    "envFrom": [{"configMapRef": {"name": "payments-api-config"}}],
                }], "volumes": [{"name": "creds", "secret": {"secretName": "payments-db"}}]},
                "status": {"phase": "Running", "containerStatuses": [
                    {"name": "payments-api", "ready": True, "restartCount": 0,
                     "state": {"running": {}}},
                    {"name": "ledger-sync", "ready": False, "restartCount": 5,
                     "state": {"waiting": {"reason": "CrashLoopBackOff",
                                           "message": "back-off restarting"}},
                     "lastState": {"terminated": {"reason": "Error", "exitCode": 1,
                                                  "finishedAt": "2999-01-01T00:00:00Z"}}},
                ]},
            }]},
            "/apis/apps/v1/namespaces/payments-prod/deployments": {"items": [{
                "metadata": {"name": "payments-api", "creationTimestamp": "2026-08-01T00:00:00Z"},
                "spec": {"replicas": 2, "strategy": {"type": "RollingUpdate"},
                         "template": {"spec": {"containers": [{"image": "busybox:1.36"}]}}},
                "status": {"readyReplicas": 0, "availableReplicas": 0, "updatedReplicas": 2,
                           "conditions": [{"type": "Progressing", "status": "True",
                                           "reason": "NewReplicaSetAvailable"}]},
            }]},
            "/apis/apps/v1/namespaces/payments-prod/replicasets": {"items": [{
                "metadata": {"name": "payments-api-1", "creationTimestamp": "2026-08-01T00:00:00Z",
                             "annotations": {"deployment.kubernetes.io/revision": "1"}},
                "spec": {"replicas": 2, "template": {"spec": {"containers": [
                    {"image": "busybox:1.36"}]}}},
                "status": {"readyReplicas": 0},
            }]},
            "/apis/apps/v1/namespaces/kube-system/deployments/coredns": {
                "status": {"readyReplicas": 2}},
            "/api/v1/namespaces/payments-prod/events": {"items": [{
                "reason": "BackOff", "count": 12, "lastTimestamp": "2026-08-27T10:00:00Z",
                "message": "Back-off restarting failed container ledger-sync",
                "involvedObject": {"kind": "Pod", "name": "payments-api-1"},
            }]},
            "/api/v1/namespaces/payments-prod": {"metadata": {"labels": {}}},
            "/readyz:text": "[+]etcd ok\n[+]ping ok\n",
        },
        prom={
            "alerts": {"data": {"alerts": [
                {"state": "firing", "labels": {"alertname": "Watchdog", "severity": "none"},
                 "annotations": {"summary": "always firing"}},
            ]}},
            "up{job=\"kube-state-metrics\"}": {"data": {"result": [
                {"metric": {}, "value": [0, "1"]}]}},
            "kube_pod_labels": {"data": {"result": [
                {"metric": {"namespace": "payments-prod", "pod": "payments-api-1"},
                 "value": [0, "1"]}]}},
        },
    )


@pytest.fixture
def ocp(kube: FakeKube) -> LiveOpenShiftBackend:
    return LiveOpenShiftBackend(FakeFleet(kube))


@pytest.fixture
def obs(kube: FakeKube) -> LiveObservabilityBackend:
    return LiveObservabilityBackend(FakeFleet(kube))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# Free-form maps whose KEYS are data, not structure, so shape parity does not
# apply inside them.
OPEN_MAPS = {"labels", "roles"}


def assert_shape_superset(live: dict[str, Any], mock: dict[str, Any], where: str = "") -> None:
    """Every key the mock world returns must exist in the live result.

    Live results may carry EXTRA keys (per-container detail, availability
    flags); they may never drop one, because the check batteries address the
    mock shape by dotted path.
    """
    missing = sorted(set(mock) - set(live))
    assert not missing, f"live result{where} is missing mock keys: {missing}"
    for key, mock_value in mock.items():
        if key not in OPEN_MAPS and isinstance(mock_value, dict) and isinstance(live.get(key), dict):
            assert_shape_superset(live[key], mock_value, f"{where}.{key}")


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [("500m", 0.5), ("2", 2.0), ("64Gi", 64 * 1024**3), ("1M", 1_000_000), ("nonsense", 0.0)],
)
def test_parse_quantity(text: str, expected: float) -> None:
    assert _parse_quantity(text) == expected


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("busybox:1.36", ("busybox", "1.36")),
        ("registry.example.internal:5000/payments", ("registry.example.internal:5000/payments", "latest")),
        ("payments@sha256:abc", ("payments", "sha256:abc")),
    ],
)
def test_split_image(image: str, expected: tuple[str, str]) -> None:
    assert _split_image(image) == expected


def test_ago_handles_missing_timestamps() -> None:
    assert _ago(None) == "unknown"
    assert _ago("not-a-timestamp") == "unknown"


# ---------------------------------------------------------------------------
# fleet registry
# ---------------------------------------------------------------------------


def test_live_registry_resolves_names_aliases_and_labels() -> None:
    fleet = LiveFleet()
    assert set(fleet.names()) == {
        "acm-hub-1", "acm-hub-2", "acm-spoke-1a", "acm-spoke-1b",
        "acm-spoke-2a", "acm-spoke-2b",
    }
    assert fleet.context("acm-spoke-1a") == "kind-acm-spoke-1a"
    assert fleet.resolve_cluster("s2a")["matches"][0]["name"] == "acm-spoke-2a"
    assert fleet.resolve_cluster("role=hub")["count"] == 2
    assert fleet.list_clusters(environment="prod")["total"] == 3
    assert fleet.resolve_cluster("nope-not-here")["suggestion"]


def test_live_registry_leaves_mock_fleet_alone(world: Any) -> None:
    """The live section must be invisible to mock mode."""
    assert world.resolve_cluster("acm-spoke-1a")["count"] == 0
    assert "acm-spoke-1a" not in [c["name"] for c in world.list_clusters(page_size=200)["clusters"]]


def test_registry_entry_hides_mock_placement_hints() -> None:
    entry = LiveFleet().get_app_registry_entry("payments-api")
    assert entry["found"] is True
    assert "instances" not in entry


def test_app_label_matches_what_live_prep_deploys() -> None:
    """The manifests set app.kubernetes.io/name; placement discovery queries it."""
    fleet = LiveFleet()
    for app_label in ("payments-api", "inventory-sync"):
        assert fleet.app_by_label(app_label) is not None
    manifests = (Path(__file__).resolve().parents[2] / "deploy" / "live")
    deployed = "\n".join(p.read_text() for p in manifests.glob("3*.yaml"))
    assert "app.kubernetes.io/name: payments-api" in deployed
    assert "app.kubernetes.io/name: inventory-sync" in deployed


# ---------------------------------------------------------------------------
# shape parity with the mock world
# ---------------------------------------------------------------------------


def test_openshift_live_shapes_match_the_mock_world(ocp: LiveOpenShiftBackend, world: Any) -> None:
    cases = [
        ("get_cluster_info", (CLUSTER,), ("prod-east-1",)),
        ("get_cluster_version", (CLUSTER,), ("prod-east-1",)),
        ("get_cluster_operators", (CLUSTER,), ("prod-east-1",)),
        ("get_machine_config_pools", (CLUSTER,), ("prod-east-1",)),
        ("get_pending_csrs", (CLUSTER,), ("prod-east-1",)),
        ("get_nodes", (CLUSTER,), ("prod-east-1",)),
        ("get_workloads", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
        ("get_events", (CLUSTER, "payments-prod"), ("prod-east-1", "payments-prod")),
        ("get_quotas", (CLUSTER, "payments-prod"), ("prod-east-1", "payments-prod")),
        ("get_network", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
        ("get_pvcs", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
        ("get_configuration", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
        ("get_security_posture", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
    ]
    for method, live_args, mock_args in cases:
        assert_shape_superset(
            getattr(ocp, method)(*live_args), getattr(world, method)(*mock_args), f" [{method}]")


def test_observability_live_shapes_match_the_mock_world(
    obs: LiveObservabilityBackend, world: Any
) -> None:
    cases = [
        ("get_firing_alerts", (CLUSTER,), ("prod-east-1",)),
        ("get_etcd_health", (CLUSTER,), ("prod-east-1",)),
        ("get_apiserver_slo", (CLUSTER,), ("prod-east-1",)),
        ("get_capacity_summary", (CLUSTER,), ("prod-east-1",)),
        ("get_golden_signals", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
        ("get_workload_usage", (CLUSTER, "payments-prod", "payments-api"),
         ("prod-east-1", "payments-prod", "payments-api")),
        ("get_dashboard_links", (CLUSTER, "payments-prod"), ("prod-east-1", "payments-prod")),
        ("find_app_placements", ("payments-api",), ("payments-api",)),
    ]
    for method, live_args, mock_args in cases:
        assert_shape_superset(
            getattr(obs, method)(*live_args), getattr(world, method)(*mock_args), f" [{method}]")


# ---------------------------------------------------------------------------
# the vanilla-Kubernetes n/a contract
# ---------------------------------------------------------------------------


def test_openshift_only_tools_report_not_applicable(ocp: LiveOpenShiftBackend) -> None:
    for method in ("get_cluster_version", "get_cluster_operators",
                   "get_machine_config_pools", "get_pending_csrs"):
        result = getattr(ocp, method)(CLUSTER)
        assert result["applicable"] is False, method
        assert result["not_applicable_reason"] == VANILLA_K8S_REASON, method


def test_not_applicable_results_are_health_neutral(ocp: LiveOpenShiftBackend) -> None:
    version = ocp.get_cluster_version(CLUSTER)
    assert (version["failing"], version["available"], version["progressing"]) == (False, True, False)
    assert version["version"] == "v1.33.7"  # the observed reading stays useful
    operators = ocp.get_cluster_operators(CLUSTER)
    assert operators["critical_degraded"] == [] and operators["unavailable"] == []
    pools = ocp.get_machine_config_pools(CLUSTER)
    assert not any((pools["any_degraded"], pools["any_updating"], pools["any_paused"]))
    assert pools["summary"] == VANILLA_K8S_REASON  # visible in the attestation table
    assert ocp.get_pending_csrs(CLUSTER)["pending_count"] == 0


def test_vanilla_cluster_attests_healthy(
    ocp: LiveOpenShiftBackend, obs: LiveObservabilityBackend
) -> None:
    """The whole point of the n/a contract: a healthy kind cluster with
    Prometheus and a firing Watchdog attests healthy, not degraded and not
    unattestable, without any change to the committed battery."""
    battery = AttestationBattery.model_validate(
        load_yaml(CONFIG_DIR / "checks" / "health_attestation.yaml"))
    backends = {"ocp": ocp, "obs": obs}
    results = []
    for check in battery.checks:
        prefix, _, tool = check.tool.partition("__")
        args = {k: (CLUSTER if v == "{{ cluster }}" else v) for k, v in check.args.items()}
        data = getattr(backends[prefix], tool)(**args)
        results.append(evaluate_check(check.model_copy(update={"args": args}), data, None, 1.0))

    verdict, _signals = derive_verdict(results)
    assert verdict == ClusterVerdict.HEALTHY
    statuses = {r.id: r.status.value for r in results}
    for check_id in ("cluster-version", "cluster-operators", "machine-config-pools", "pending-csrs"):
        assert statuses[check_id] == "pass", (check_id, statuses[check_id])


# ---------------------------------------------------------------------------
# real-data behaviours worth pinning
# ---------------------------------------------------------------------------


def test_workloads_surface_container_level_pathology(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_workloads(CLUSTER, "payments-prod", "payments-api")
    assert result["replicas_summary"] == "0/2 ready"
    assert result["replicas_mismatch"] == ["payments-api"]
    assert result["pods"]["crashloop"] == ["payments-api-1"]
    reasons = {i["container"]: i["reason"] for i in result["pods"]["container_issues"]}
    assert reasons["ledger-sync"] == "CrashLoopBackOff"


def test_configuration_returns_names_only(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_configuration(CLUSTER, "payments-prod", "payments-api")
    assert result["secrets"] == ["payments-db"]
    assert "data" not in result


def test_watchdog_is_a_trust_signal_not_a_finding(obs: LiveObservabilityBackend) -> None:
    alerts = obs.get_firing_alerts(CLUSTER)
    assert alerts["watchdog_present"] is True
    assert alerts["monitoring_healthy"] is True
    assert alerts["critical"] == [] and alerts["warning"] == []


def test_missing_metrics_are_reported_as_unknown_not_zero(obs: LiveObservabilityBackend) -> None:
    slo = obs.get_apiserver_slo(CLUSTER)
    assert slo["error_rate_5xx"] is None
    assert slo["metric_available"] is False
    assert slo["healthy"] is True  # unknown must never fail the check
    signals = obs.get_golden_signals(CLUSTER, "payments-prod", "payments-api")
    assert signals["instrumented"] is False
    assert signals["request_rate"] is None and signals["latency_breach"] is False


def test_etcd_health_comes_from_the_apiserver_readiness_probe(
    obs: LiveObservabilityBackend
) -> None:
    health = obs.get_etcd_health(CLUSTER)
    assert health["has_leader"] is True and health["quorum"] is True
    assert health["fsync_p99_ms"] is None and health["fsync_metric_available"] is False


def test_placement_is_discovered_from_kube_pod_labels(obs: LiveObservabilityBackend) -> None:
    result = obs.find_app_placements("payments-api")
    assert {p["cluster"] for p in result["placements"]} == set(LiveFleet().names())
    row = result["placements"][0]
    assert row["namespace"] == "payments-prod" and row["pod_count"] == 1
    assert row["application"] == "payments-api"


def test_raw_query_routes_on_the_cluster_label(obs: LiveObservabilityBackend) -> None:
    scoped = obs.query_instant('kube_pod_labels{cluster="acm-spoke-2b"}')
    assert "acm-spoke-2b" in scoped["note"]
    unscoped = obs.query_instant("kube_pod_labels")
    assert "no cluster=" in unscoped["note"]


# ---------------------------------------------------------------------------
# live smoke: real clusters, opt in
# ---------------------------------------------------------------------------


@pytest.mark.live_smoke
@pytest.mark.skipif(
    os.environ.get("CLOUDOPS_LIVE_SMOKE") != "1",
    reason="needs the local kind fleet; run `make live-smoke` or set CLOUDOPS_LIVE_SMOKE=1",
)
def test_live_smoke_against_the_real_fleet() -> None:
    from cloudops.mcp_servers.live_smoke import main

    assert main() == 0
