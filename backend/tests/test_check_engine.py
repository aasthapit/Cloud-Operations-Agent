"""Check engine unit tests: rule evaluation, verdict derivation, status maps."""

import pytest
from conftest import CONFIG_DIR
from fakes import DEGRADED, HEALTHY, UNREACHABLE, node
from registry_fixtures import seeded_registry  # noqa: F401 - fixture import

from cloudops.agent.checks import (
    _lookup,
    _rule_triggers,
    attestation_delta,
    derive_verdict,
    evaluate_check,
    report_status,
    run_attestation,
)
from cloudops.agent.models import (
    AttestationBattery,
    CheckDef,
    CheckEvidence,
    CheckResult,
    CheckStatus,
    ClusterAttestation,
    ClusterVerdict,
    RuleDef,
    SectionResult,
)
from cloudops.common.config import load_yaml

# LiveFleet resolves cluster records through the MongoDB registry since the
# live cutover, so the fleet doubles in this module need the seeded mongomock
# registry standing behind them.
pytestmark = pytest.mark.usefixtures("seeded_registry")


def rule(path: str, op: str, value=None, outcome="fail", reason="r") -> RuleDef:
    return RuleDef(path=path, op=op, value=value, outcome=outcome, reason=reason)


def check(rules, severity="critical", observed=None) -> CheckDef:
    return CheckDef(id="c", name="C", severity=severity, tool="t", args={},
                    observed=observed, rules=rules)


def result(status: CheckStatus, severity: str = "critical") -> CheckResult:
    return CheckResult(id="x", name="X", severity=severity, status=status,
                       evidence=CheckEvidence(tool="t", args={}, timestamp="now"))


class TestLookup:
    def test_nested_and_list_index(self):
        data = {"pods": {"crashloop": ["a"]}, "pools": [{"name": "worker"}]}
        assert _lookup(data, "pods.crashloop") == ["a"]
        assert _lookup(data, "pools.0.name") == "worker"

    def test_missing_returns_none(self):
        assert _lookup({"a": 1}, "a.b.c") is None
        assert _lookup({}, "nope") is None


class TestRules:
    def test_comparisons(self):
        data = {"n": 5, "flag": True, "items": [], "names": ["x"]}
        assert _rule_triggers(rule("n", "gt", 3), data)
        assert not _rule_triggers(rule("n", "gt", 5), data)
        assert _rule_triggers(rule("n", "gte", 5), data)
        assert _rule_triggers(rule("flag", "truthy"), data)
        assert _rule_triggers(rule("items", "empty"), data)
        assert _rule_triggers(rule("names", "not_empty"), data)
        assert _rule_triggers(rule("missing", "absent"), data)

    def test_numeric_op_on_non_number_never_triggers(self):
        # A missing or non-numeric path must not satisfy gt/lt (unknown != breach).
        assert not _rule_triggers(rule("missing", "gt", 0), {})
        assert not _rule_triggers(rule("s", "gt", 0), {"s": "high"})

    def test_unknown_op_never_triggers(self):
        assert not _rule_triggers(rule("n", "matches_vibe", 1), {"n": 1})


class TestEvaluateCheck:
    def test_pass_when_no_rule_triggers(self):
        r = evaluate_check(check([rule("bad", "truthy")]), {"bad": False}, None, 1.0)
        assert r.status == CheckStatus.PASS
        assert r.evidence.triggered_rules == []

    def test_worst_outcome_wins(self):
        rules = [
            rule("a", "truthy", outcome="warn"),
            rule("b", "truthy", outcome="fail"),
            rule("c", "truthy", outcome="maintenance"),
        ]
        r = evaluate_check(check(rules), {"a": 1, "b": 1, "c": 1}, None, 1.0)
        assert r.status == CheckStatus.FAIL
        assert len(r.evidence.triggered_rules) == 3

    def test_unattestable_beats_fail(self):
        rules = [rule("a", "truthy", outcome="fail"),
                 rule("b", "falsy", outcome="unattestable")]
        r = evaluate_check(check(rules), {"a": 1, "b": False}, None, 1.0)
        assert r.status == CheckStatus.UNATTESTABLE

    def test_tool_error_is_error_not_pass(self):
        r = evaluate_check(check([rule("a", "truthy")]), None, "boom", 1.0)
        assert r.status == CheckStatus.ERROR
        assert r.evidence.error == "boom"

    def test_observed_pair_and_truncation(self):
        r = evaluate_check(check([], observed="ready/total"), {"ready": 3, "total": 5}, None, 1.0)
        assert r.observed == "3/5"


class TestVerdicts:
    def test_healthy(self):
        v, _ = derive_verdict([result(CheckStatus.PASS)])
        assert v == ClusterVerdict.HEALTHY

    def test_critical_fail_degrades(self):
        v, signals = derive_verdict([result(CheckStatus.FAIL, "critical")])
        assert v == ClusterVerdict.DEGRADED
        assert signals

    def test_warning_severity_fail_does_not_degrade(self):
        v, _ = derive_verdict([result(CheckStatus.FAIL, "warning")])
        assert v == ClusterVerdict.HEALTHY

    def test_maintenance_without_fail(self):
        v, _ = derive_verdict([result(CheckStatus.MAINTENANCE), result(CheckStatus.PASS)])
        assert v == ClusterVerdict.MAINTENANCE

    def test_unattestable_caps_everything(self):
        v, _ = derive_verdict([
            result(CheckStatus.UNATTESTABLE),
            result(CheckStatus.FAIL, "critical"),
        ])
        assert v == ClusterVerdict.UNATTESTABLE


def attestation(cluster: str, verdict: ClusterVerdict, failing: list[str]) -> ClusterAttestation:
    checks = [
        CheckResult(id=cid, name=cid, severity="critical", status=CheckStatus.FAIL,
                    evidence=CheckEvidence(tool="t", args={}, timestamp="now"))
        for cid in failing
    ] + [
        CheckResult(id="always-fine", name="X", severity="critical", status=CheckStatus.PASS,
                    evidence=CheckEvidence(tool="t", args={}, timestamp="now"))
    ]
    return ClusterAttestation(cluster=cluster, verdict=verdict, checks=checks)


class TestAttestationDelta:
    """G1 / F5: what changed between two attestations of the same cluster."""

    def test_first_attestation_has_no_delta(self):
        current = attestation("prod-east-2", ClusterVerdict.DEGRADED, ["cluster-operators"])
        assert attestation_delta(None, current) is None

    def test_identical_attestation_stays_silent(self):
        previous = attestation("prod-east-2", ClusterVerdict.DEGRADED, ["cluster-operators"])
        current = attestation("prod-east-2", ClusterVerdict.DEGRADED, ["cluster-operators"])
        assert attestation_delta(previous, current) is None

    def test_verdict_flip_names_the_signals_that_cleared(self):
        previous = attestation("prod-east-2", ClusterVerdict.DEGRADED,
                               ["cluster-operators", "nodes"])
        current = attestation("prod-east-2", ClusterVerdict.HEALTHY, [])
        delta = attestation_delta(previous, current)
        assert delta is not None
        assert (delta.from_verdict, delta.to_verdict) == ("degraded", "healthy")
        assert delta.note == "cleared: cluster-operators, nodes"
        assert delta.summary().startswith("prod-east-2: degraded -> healthy")
        # The wire shape the console renders (F5 done tick).
        assert delta.model_dump(mode="json", by_alias=True) == {
            "cluster": "prod-east-2", "from": "degraded", "to": "healthy",
            "note": "cleared: cluster-operators, nodes",
        }

    def test_new_signal_without_a_verdict_flip_still_reports(self):
        previous = attestation("prod-east-2", ClusterVerdict.DEGRADED, ["cluster-operators"])
        current = attestation("prod-east-2", ClusterVerdict.DEGRADED,
                              ["cluster-operators", "capacity"])
        delta = attestation_delta(previous, current)
        assert delta is not None
        assert delta.note == "new: capacity"


@pytest.fixture(scope="session")
def battery() -> AttestationBattery:
    return AttestationBattery.model_validate(
        load_yaml(CONFIG_DIR / "checks" / "health_attestation.yaml")
    )


class TestAttestationAgainstTheFleet:
    """G3 / FR-ATT-5: the committed battery, run for real through the check
    engine against canned cluster APIs. These are the verdicts the agent
    reaches, not a restatement of the rules."""

    @pytest.mark.asyncio
    async def test_healthy_cluster_attests_healthy(self, battery, gateway):
        [att] = await run_attestation(battery, [HEALTHY], gateway, "v1")
        assert att.verdict is ClusterVerdict.HEALTHY
        assert att.signals == []
        # The four OpenShift-only tools land as plain passes on vanilla
        # Kubernetes rather than dragging the verdict down (the n/a contract).
        statuses = {c.id: c.status for c in att.checks}
        for check_id in ("cluster-version", "cluster-operators",
                         "machine-config-pools", "pending-csrs"):
            assert statuses[check_id] is CheckStatus.PASS, check_id

    @pytest.mark.asyncio
    async def test_not_ready_node_degrades_the_cluster(self, battery, gateway):
        [att] = await run_attestation(battery, [DEGRADED], gateway, "v1")
        assert att.verdict is ClusterVerdict.DEGRADED
        assert any("NotReady" in s for s in att.signals)

    @pytest.mark.asyncio
    async def test_cordoned_node_is_maintenance_not_damage(self, battery, gateway):
        [att] = await run_attestation(battery, ["acm-spoke-1b"], gateway, "v1")
        assert att.verdict is ClusterVerdict.MAINTENANCE

    @pytest.mark.asyncio
    async def test_unreachable_cluster_is_unattestable(self, battery, gateway):
        """The battery's only unattestable outcome after the Prometheus checks
        were removed: a cluster that did not answer was not attested, and must
        not be reported as degraded (which would claim knowledge) either."""
        [att] = await run_attestation(battery, [UNREACHABLE], gateway, "v1")
        assert att.verdict is ClusterVerdict.UNATTESTABLE
        assert any(c.id == "api-reachability" and c.status is CheckStatus.UNATTESTABLE
                   for c in att.checks)

    @pytest.mark.asyncio
    async def test_verdicts_stay_separate_across_clusters(self, battery, gateway):
        results = {a.cluster: a for a in
                   await run_attestation(battery, [DEGRADED, HEALTHY], gateway, "v1")}
        assert results[DEGRADED].verdict is ClusterVerdict.DEGRADED
        assert results[HEALTHY].verdict is ClusterVerdict.HEALTHY

    @pytest.mark.asyncio
    async def test_capacity_check_reads_real_requests(self, battery, gateway, world):
        """The capacity check now computes from nodes and pods, so shrinking
        the cluster to one node must trip the minus-one-node guard."""
        world[HEALTHY].nodes = [node("cp-1")]
        [att] = await run_attestation(battery, [HEALTHY], gateway, "v1")
        capacity = next(c for c in att.checks if c.id == "capacity")
        assert capacity.status is CheckStatus.WARN
        assert "single-node cluster" in capacity.observed
        # A warning-severity failure informs without flipping the verdict.
        assert att.verdict is ClusterVerdict.HEALTHY


class TestReportStatus:
    def section(self, checks):
        return SectionResult(section=1, title="t", source="checks",
                             status=CheckStatus.PASS, checks=checks)

    def test_mapping(self):
        assert report_status([self.section([result(CheckStatus.PASS)])]) == "healthy"
        assert report_status([self.section([result(CheckStatus.WARN)])]) == "at_risk"
        assert report_status([self.section([result(CheckStatus.FAIL, "warning")])]) == "at_risk"
        assert report_status([self.section([result(CheckStatus.FAIL, "critical")])]) == "critical"
