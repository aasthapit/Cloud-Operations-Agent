"""OpenShift backend behaviour against canned Kubernetes payloads.

Two layers, both hermetic by default (NFR-QE-1):

1. Unit tests over LiveOpenShiftBackend driven by tests/fakes.py: the pure
   helpers, the fleet registry, the vanilla-Kubernetes n/a contract, and the
   readings the check batteries address by dotted path.
2. A live_smoke-marked test that talks to the real kind fleet and is skipped
   unless CLOUDOPS_LIVE_SMOKE=1, so `make test` never depends on a running
   cluster.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fakes import (
    APP_LABEL,
    APP_NS,
    DEGRADED,
    HEALTHY,
    UNREACHABLE,
    ClusterFixture,
    FakeFleet,
    hpa,
    node,
    pdb,
    pod,
)
from registry_fixtures import seeded_registry  # noqa: F401 - fixture import

from cloudops.mcp_servers.kube import VANILLA_K8S_REASON
from cloudops.mcp_servers.live_fleet import LiveFleet
from cloudops.mcp_servers.openshift.live import (
    LiveOpenShiftBackend,
    _ago,
    _parse_quantity,
    _split_image,
)

# LiveFleet reads its cluster records from MongoDB now, so every test in this
# module needs the seeded in-memory registry standing behind it. Autouse
# rather than per-test, because the dependency is the module's baseline
# rather than the subject of any one test.
pytestmark = pytest.mark.usefixtures("seeded_registry")

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
        ("registry.example.internal:5000/payments",
         ("registry.example.internal:5000/payments", "latest")),
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


def test_registry_resolves_names_aliases_and_labels() -> None:
    fleet = LiveFleet()
    assert set(fleet.names()) == {
        "acm-hub-1", "acm-hub-2", "acm-spoke-1a", "acm-spoke-1b",
        "acm-spoke-2a", "acm-spoke-2b",
    }
    assert fleet.context(HEALTHY) == "kind-acm-spoke-1a"
    assert fleet.resolve_cluster("s2a")["matches"][0]["name"] == DEGRADED
    assert fleet.resolve_cluster("role=hub")["count"] == 2
    assert fleet.list_clusters(environment="prod")["total"] == 3
    assert fleet.resolve_cluster("nope-not-here")["suggestion"]


def test_registry_entry_hides_placement_hints() -> None:
    entry = LiveFleet().get_app_registry_entry("payments-api")
    assert entry["found"] is True
    # `instances` is registry SEED data; placement is verified, never read here.
    assert "instances" not in entry


def test_app_label_matches_what_live_prep_deploys() -> None:
    """The manifests set app.kubernetes.io/name; verification queries it."""
    fleet = LiveFleet()
    for app_label in ("payments-api", "inventory-sync"):
        assert fleet.app_by_label(app_label) is not None
    manifests = Path(__file__).resolve().parents[2] / "deploy" / "live"
    deployed = "\n".join(p.read_text() for p in manifests.glob("3*.yaml"))
    assert "app.kubernetes.io/name: payments-api" in deployed
    assert "app.kubernetes.io/name: inventory-sync" in deployed


def test_unknown_cluster_raises(ocp: LiveOpenShiftBackend) -> None:
    with pytest.raises(ValueError, match="unknown cluster"):
        ocp.get_nodes("not-in-the-fleet")


# ---------------------------------------------------------------------------
# the vanilla-Kubernetes n/a contract
# ---------------------------------------------------------------------------


def test_openshift_only_tools_report_not_applicable(ocp: LiveOpenShiftBackend) -> None:
    for method in ("get_cluster_version", "get_cluster_operators",
                   "get_machine_config_pools", "get_pending_csrs"):
        result = getattr(ocp, method)(HEALTHY)
        assert result["applicable"] is False, method
        assert result["not_applicable_reason"] == VANILLA_K8S_REASON, method


def test_not_applicable_results_are_health_neutral(ocp: LiveOpenShiftBackend) -> None:
    version = ocp.get_cluster_version(HEALTHY)
    assert (version["failing"], version["available"], version["progressing"]) == (False, True, False)
    assert version["version"] == "v1.33.7"  # the observed reading stays useful
    operators = ocp.get_cluster_operators(HEALTHY)
    assert operators["critical_degraded"] == [] and operators["unavailable"] == []
    pools = ocp.get_machine_config_pools(HEALTHY)
    assert not any((pools["any_degraded"], pools["any_updating"], pools["any_paused"]))
    assert pools["summary"] == VANILLA_K8S_REASON  # visible in the attestation table
    assert ocp.get_pending_csrs(HEALTHY)["pending_count"] == 0


# ---------------------------------------------------------------------------
# cluster state
# ---------------------------------------------------------------------------


def test_cluster_info_reports_unreachable_rather_than_raising(
    ocp: LiveOpenShiftBackend
) -> None:
    """Reachability IS the reading: an errored tool would lose the difference
    between 'down' and 'could not be asked'."""
    assert ocp.get_cluster_info(UNREACHABLE)["reachable"] is False
    assert ocp.get_cluster_info(HEALTHY)["reachable"] is True


def test_nodes_separate_not_ready_from_cordoned(ocp: LiveOpenShiftBackend, world: Any) -> None:
    world[HEALTHY].nodes = [
        node("cp-1"),
        node("worker-1", role="worker", ready=False),
        node("worker-2", role="worker", unschedulable=True),
    ]
    result = ocp.get_nodes(HEALTHY)
    assert [n["name"] for n in result["not_ready"]] == ["worker-1"]
    assert [n["name"] for n in result["cordoned"]] == ["worker-2"]
    assert (result["total"], result["ready"]) == (3, 1)


def test_capacity_is_computed_from_nodes_and_pods(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_capacity(HEALTHY)
    # Two nodes at 4 cpu / 8Gi / 110 pods; the fixture's pods request 100m each.
    assert (result["cpu_allocatable_cores"], result["pod_capacity"]) == (8.0, 220)
    assert result["cpu_requests_cores"] == pytest.approx(0.4)
    assert result["cpu_requests_ratio"] == pytest.approx(0.05)
    assert result["fits_minus_one_node"] is True
    assert "of allocatable across 2 node(s)" in result["summary"]


def test_capacity_refuses_to_pass_the_guard_on_one_node(
    ocp: LiveOpenShiftBackend, world: Any
) -> None:
    """A single-node cluster genuinely cannot survive losing a node; saying it
    fits would be a comfortable lie."""
    world[HEALTHY].nodes = [node("cp-1")]
    result = ocp.get_capacity(HEALTHY)
    assert result["fits_minus_one_node"] is False
    assert "single-node cluster" in result["summary"]


def test_namespaces_are_listed(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_namespaces(HEALTHY)
    assert APP_NS in {n["name"] for n in result["namespaces"]}
    assert result["total"] == len(result["namespaces"])


# ---------------------------------------------------------------------------
# placement verification
# ---------------------------------------------------------------------------


def test_verify_placement_confirms_a_real_placement(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.verify_placement(HEALTHY, APP_NS, APP_LABEL)
    assert (result["verified"], result["reachable"]) == (True, True)
    assert (result["pod_count"], result["ready_count"]) == (2, 2)


def test_verify_placement_separates_absent_from_unreachable(
    ocp: LiveOpenShiftBackend
) -> None:
    absent = ocp.verify_placement(HEALTHY, "kube-system", APP_LABEL)
    assert (absent["reachable"], absent["verified"]) == (True, False)

    missing_ns = ocp.verify_placement(HEALTHY, "no-such-namespace", APP_LABEL)
    assert (missing_ns["reachable"], missing_ns["verified"]) == (True, False)
    assert "404" in missing_ns["reason"]

    down = ocp.verify_placement(UNREACHABLE, APP_NS, APP_LABEL)
    assert (down["reachable"], down["verified"]) == (False, False)


def test_verify_placement_counts_unready_pods_as_present(
    ocp: LiveOpenShiftBackend
) -> None:
    """A crash-looping app is deployed there; verification answers 'is it
    placed', and readiness is reported alongside rather than instead."""
    result = ocp.verify_placement(DEGRADED, APP_NS, APP_LABEL)
    assert result["verified"] is True
    assert (result["pod_count"], result["ready_count"]) == (2, 0)


# ---------------------------------------------------------------------------
# autoscaling
# ---------------------------------------------------------------------------


def test_autoscaling_reads_hpa_and_pdb(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_autoscaling(DEGRADED, APP_NS, APP_LABEL)
    assert result["hpa"]["maxed_out"] is True
    assert (result["hpa"]["current"], result["hpa"]["max"]) == (6, 6)
    assert result["hpa"]["desired"] == 8
    assert result["pdb"]["blocked"] is True
    assert result["pdb"]["expected_pods"] == 2


def test_autoscaling_excludes_fixed_size_hpas_from_maxed(
    ocp: LiveOpenShiftBackend, world: Any
) -> None:
    """min == max is a fixed-size deployment with extra steps, not an
    autoscaler out of room (KubeHpaMaxedOut makes the same exclusion)."""
    world[DEGRADED].ns(APP_NS).horizontalpodautoscalers = [
        hpa("payments-api", APP_LABEL, "payments-api", minimum=3, maximum=3, current=3)
    ]
    result = ocp.get_autoscaling(DEGRADED, APP_NS, APP_LABEL)
    assert result["hpa"]["at_max"] is True
    assert result["hpa"]["maxed_out"] is False


def test_autoscaling_is_honest_about_namespace_wide_fallback(
    ocp: LiveOpenShiftBackend, world: Any
) -> None:
    """Nothing in the namespace names the app, so the rollup covers the whole
    namespace and the summary says so instead of overclaiming."""
    ns = world[DEGRADED].ns(APP_NS)
    ns.horizontalpodautoscalers = [hpa("other", None, "other-app")]
    ns.poddisruptionbudgets = [pdb("other", None, allowed=0)]
    result = ocp.get_autoscaling(DEGRADED, APP_NS, APP_LABEL)
    assert result["hpas"][0]["app_match"] is False
    assert "namespace-wide" in result["hpa_summary"]
    assert "namespace-wide" in result["pdb_summary"]


def test_autoscaling_reports_absence_plainly(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_autoscaling(HEALTHY, "kube-system", APP_LABEL)
    assert result["hpa"]["present"] is False and result["pdb"]["present"] is False
    assert result["hpa_summary"] == "no HPA"
    assert result["pdb_summary"] == "no PodDisruptionBudget"


# ---------------------------------------------------------------------------
# namespace and application state
# ---------------------------------------------------------------------------


def test_workloads_surface_container_level_pathology(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_workloads(DEGRADED, APP_NS, APP_LABEL)
    assert result["replicas_summary"] == "0/2 ready"
    assert result["replicas_mismatch"] == ["payments-api"]
    assert result["pods"]["crashloop"] == ["payments-api-1"]
    assert result["pods"]["pending"] == ["payments-api-2"]
    reasons = {i["pod"]: i["reason"] for i in result["pods"]["container_issues"]}
    assert reasons["payments-api-1"] == "CrashLoopBackOff"


def test_workloads_are_clean_on_the_healthy_cluster(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_workloads(HEALTHY, APP_NS, APP_LABEL)
    assert result["replicas_summary"] == "2/2 ready"
    assert result["replicas_mismatch"] == [] and result["pods"]["crashloop"] == []


def test_selector_falls_back_to_the_bare_app_label(
    ocp: LiveOpenShiftBackend, world: Any
) -> None:
    """Workloads predating the app.kubernetes.io convention must still resolve
    rather than silently returning nothing."""
    legacy = world[HEALTHY].ns("legacy")
    legacy.pods = [pod("legacy-1", "legacy-app")]
    legacy.pods[0]["metadata"]["labels"] = {"app": "legacy-app"}
    assert ocp.verify_placement(HEALTHY, "legacy", "legacy-app")["verified"] is True


def test_events_are_ranked_by_count(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_events(DEGRADED, APP_NS)
    assert result["warning_count"] == 1
    assert result["warnings"][0]["reason"] == "BackOff"
    assert result["warnings"][0]["object"] == "pod/payments-api-1"


def test_configuration_returns_names_only(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_configuration(HEALTHY, APP_NS, APP_LABEL)
    assert result["secrets"] == ["payments-db"]
    assert result["configmaps"] == ["payments-api-config"]
    assert "data" not in result


def test_quotas_flag_the_near_limit_entries(ocp: LiveOpenShiftBackend, world: Any) -> None:
    from fakes import quota

    world[HEALTHY].ns(APP_NS).resourcequotas = [quota("compute", "cpu", "95", "100")]
    result = ocp.get_quotas(HEALTHY, APP_NS)
    assert [q["resource"] for q in result["near_limit"]] == ["cpu"]


def test_pvc_fill_is_unknown_not_invented(ocp: LiveOpenShiftBackend) -> None:
    result = ocp.get_pvcs(HEALTHY, APP_NS, APP_LABEL)
    assert result["near_full"] == []
    assert result["summary"] == "no persistent volumes"


# ---------------------------------------------------------------------------
# the fake transport itself
# ---------------------------------------------------------------------------


def test_fake_kube_refuses_paths_it_does_not_model() -> None:
    """A silent {} from the double would let a backend query the wrong path
    and still pass its test."""
    from fakes import FakeKubeError

    fleet = FakeFleet({HEALTHY: ClusterFixture()})
    with pytest.raises(FakeKubeError):
        fleet.client(HEALTHY).get("/apis/made/up/path")


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
