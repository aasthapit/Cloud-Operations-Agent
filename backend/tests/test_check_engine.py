"""Check engine unit tests: rule evaluation, verdict derivation, status maps."""

from cloudops.agent.checks import (
    _lookup,
    _rule_triggers,
    derive_verdict,
    evaluate_check,
    report_status,
)
from cloudops.agent.models import (
    CheckDef,
    CheckEvidence,
    CheckResult,
    CheckStatus,
    ClusterVerdict,
    RuleDef,
    SectionResult,
)


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


class TestReportStatus:
    def section(self, checks):
        return SectionResult(section=1, title="t", source="checks",
                             status=CheckStatus.PASS, checks=checks)

    def test_mapping(self):
        assert report_status([self.section([result(CheckStatus.PASS)])]) == "healthy"
        assert report_status([self.section([result(CheckStatus.WARN)])]) == "at_risk"
        assert report_status([self.section([result(CheckStatus.FAIL, "warning")])]) == "at_risk"
        assert report_status([self.section([result(CheckStatus.FAIL, "critical")])]) == "critical"
