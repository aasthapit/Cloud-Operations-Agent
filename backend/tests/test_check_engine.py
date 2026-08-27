"""Check engine unit tests: rule evaluation, verdict derivation, status maps."""

import pytest
from conftest import CONFIG_DIR, WorldGateway

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
        current = attestation("prod-east-2", ClusterVerdict.DEGRADED, ["cluster-operators", "etcd"])
        delta = attestation_delta(previous, current)
        assert delta is not None
        assert delta.note == "new: etcd"


@pytest.fixture(scope="session")
def battery() -> AttestationBattery:
    return AttestationBattery.model_validate(
        load_yaml(CONFIG_DIR / "checks" / "health_attestation.yaml")
    )


class TestAttestationAgainstTheMockWorld:
    """G3 / FR-ATT-5: the battery, run for real against the scenario faults."""

    @pytest.mark.asyncio
    async def test_watchdog_absent_cluster_is_unattestable(self, battery, world):
        """prod-eu-1 has no Watchdog in config/mock/scenario.yaml: monitoring
        cannot be trusted, so the verdict is unattestable, never healthy."""
        [att] = await run_attestation(battery, ["prod-eu-1"], WorldGateway(world), "v1")
        assert att.verdict is ClusterVerdict.UNATTESTABLE
        assert any(c.id == "watchdog-present" and c.status == CheckStatus.UNATTESTABLE
                   for c in att.checks)
        assert any("watchdog" in s.lower() or "monitoring" in s.lower() for s in att.signals)

    @pytest.mark.asyncio
    async def test_degraded_and_healthy_clusters_still_separate(self, battery, world):
        gateway = WorldGateway(world)
        results = {a.cluster: a for a in
                   await run_attestation(battery, ["prod-east-2", "prod-east-1"], gateway, "v1")}
        assert results["prod-east-2"].verdict is ClusterVerdict.DEGRADED
        assert results["prod-east-1"].verdict is ClusterVerdict.HEALTHY


class TestReportStatus:
    def section(self, checks):
        return SectionResult(section=1, title="t", source="checks",
                             status=CheckStatus.PASS, checks=checks)

    def test_mapping(self):
        assert report_status([self.section([result(CheckStatus.PASS)])]) == "healthy"
        assert report_status([self.section([result(CheckStatus.WARN)])]) == "at_risk"
        assert report_status([self.section([result(CheckStatus.FAIL, "warning")])]) == "at_risk"
        assert report_status([self.section([result(CheckStatus.FAIL, "critical")])]) == "critical"
