"""Deterministic scorers: one expectation in, one {metric, pass, detail} out.

Every scorer reads only the captured turn, never the stack, so scoring is
pure and a scorecard can be re-derived from a capture. Two rules shape the
set:

- A metric is named for the CONTRACT it protects, not for the field it reads
  (``protocol.no_model_fences``, ``context.environment_assumed``), because the
  failure line in the report has to say what broke, not where a dictionary
  lookup went.
- ``protocol.no_model_fences`` runs on every turn whether a scenario asks for
  it or not. It is invariant 1 in AGENT.md section 10 - only the deterministic
  runtime emits ``cloudops-*`` fences - and an invariant that has to be opted
  into is not one.

Mode matters in exactly one place: expectations under a scenario's
``live_only`` block are scored only when a real model wrote the narrative. In
fake mode the analyst is ``FakeLlm``, whose answer is one fixed sentence, so
asserting prose there would pin the double instead of the product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from cloudops.evals.capture import TurnRecord
from cloudops.evals.suite import Expect, Mention, Mode, TurnSpec


@dataclass
class Metric:
    metric: str
    passed: bool
    detail: str
    kind: str = "deterministic"  # deterministic | judge
    score: float | None = None
    threshold: float | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "metric": self.metric, "pass": self.passed,
            "detail": self.detail, "kind": self.kind,
        }
        if self.score is not None:
            payload["score"] = round(self.score, 3)
            payload["threshold"] = self.threshold
        if self.evidence:
            payload["evidence"] = self.evidence
        return payload


def _check(metric: str, passed: bool, detail: str) -> Metric:
    return Metric(metric=metric, passed=passed, detail=detail)


def _matches(record: TurnRecord, mention: Mention) -> bool:
    if mention.regex:
        return re.search(mention.pattern, record.narrative, re.I | re.S) is not None
    return mention.pattern.lower() in record.narrative.lower()


def _excerpt(text: str, limit: int = 400) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + " ..."


def score_turn(turn: TurnSpec, record: TurnRecord, mode: Mode) -> list[Metric]:
    """Every deterministic metric for one turn."""
    expect = turn.expect
    metrics: list[Metric] = [_no_model_fences(record)]

    if expect.outcome is not None:
        actual = record.outcome()
        metrics.append(_check(
            "outcome", actual == expect.outcome,
            f"expected {expect.outcome}, got {actual}",
        ))

    metrics.extend(_context(expect, record))
    metrics.extend(_attestation(expect, record))
    metrics.extend(_app360(expect, record))
    metrics.extend(_placements(expect, record))
    metrics.extend(_phases(expect, record))
    metrics.extend(_fence_kinds(expect, record))
    metrics.extend(_clarify(expect, record))
    metrics.extend(_tool_budget(expect, record))
    metrics.extend(_error_fence(expect, record))
    metrics.extend(_mentions(expect.narrative_must_mention,
                             expect.narrative_must_not_mention, record, ""))
    if mode == "live":
        metrics.extend(_mentions(expect.live_only.narrative_must_mention,
                                 expect.live_only.narrative_must_not_mention, record, "live."))
    return metrics


def _no_model_fences(record: TurnRecord) -> Metric:
    """Contract invariant 1: the model narrates, the runtime emits fences.

    A fence block the model quotes back verbatim from its own context is an
    echo, not authorship, and is reported without failing: it carries no
    payload the runtime did not already emit. What fails is a typed payload
    the runtime never wrote.
    """
    offending = sorted(set(record.model_fence_kinds))
    echoed = sorted(set(record.echoed_fence_kinds))
    detail = ("no cloudops fence was authored outside the deterministic runtime"
              if not offending else
              f"the model authored fences: {', '.join(offending)}")
    if echoed:
        detail += f" (echoed back from context, not authored: {', '.join(echoed)})"
    return _check("protocol.no_model_fences", not offending, detail)


def _context(expect: Expect, record: TurnRecord) -> list[Metric]:
    if not expect.context:
        return []
    context = record.one("context")
    if context is None:
        return [_check("context.emitted", False,
                       f"expected one context fence, got {len(record.of('context'))}")]
    metrics = []
    for key, wanted in expect.context.items():
        actual = context.get(key)
        metrics.append(_check(
            f"context.{key}", actual == wanted, f"expected {wanted!r}, got {actual!r}"))
    return metrics


def _attestation(expect: Expect, record: TurnRecord) -> list[Metric]:
    if not expect.attestation:
        return []
    report = record.one("attestation")
    if report is None:
        return [_check("attestation.emitted", False, "no attestation fence on this turn")]
    verdicts = {c["cluster"]: c["verdict"] for c in report.get("clusters", [])}
    return [
        _check(f"attestation.{cluster}", verdicts.get(cluster) == verdict,
               f"expected {verdict}, got {verdicts.get(cluster)!r}")
        for cluster, verdict in expect.attestation.items()
    ]


def _app360(expect: Expect, record: TurnRecord) -> list[Metric]:
    spec = expect.app360
    reports = record.of("app360")
    metrics = []
    if spec.emitted is not None:
        metrics.append(_check(
            "app360.emitted", bool(reports) == spec.emitted,
            f"expected emitted={spec.emitted}, got {len(reports)} report(s)"))
    if spec.count is not None:
        metrics.append(_check(
            "app360.count", len(reports) == spec.count,
            f"expected {spec.count} report(s), got {len(reports)}"))
    if spec.overall_status is not None:
        actual = [r.get("overall_status") for r in reports]
        metrics.append(_check(
            "app360.overall_status", spec.overall_status in actual,
            f"expected a report with overall_status {spec.overall_status!r}, got {actual}"))
    # A report that does not carry all 18 sections is not the organization's
    # Application 360 (FR-360-1); worth pinning wherever one is expected.
    if reports and spec.emitted is not False:
        sizes = [len(r.get("sections", [])) for r in reports]
        metrics.append(_check(
            "app360.sections", all(size == 18 for size in sizes),
            f"expected 18 sections per report, got {sizes}"))
    return metrics


def _placements(expect: Expect, record: TurnRecord) -> list[Metric]:
    if not expect.placements:
        return []
    context = record.one("context")
    instances = (context or {}).get("instances", [])
    metrics = []
    for wanted in expect.placements:
        found = next(
            (i for i in instances
             if i.get("cluster") == wanted.cluster
             and (wanted.namespace is None or i.get("namespace") == wanted.namespace)),
            None,
        )
        label = f"placement.{wanted.cluster}" + (f"/{wanted.namespace}" if wanted.namespace else "")
        if found is None:
            metrics.append(_check(label, False, f"no resolved instance on {wanted.cluster}"))
            continue
        checks = {k: v for k, v in (
            ("verified", wanted.verified), ("reachable", wanted.reachable),
            ("pod_count", wanted.pod_count)) if v is not None}
        mismatched = {k: (v, found.get(k)) for k, v in checks.items() if found.get(k) != v}
        metrics.append(_check(
            label, not mismatched,
            "verified against the cluster as declared" if not mismatched else
            "; ".join(f"{k}: expected {want!r}, got {got!r}"
                      for k, (want, got) in mismatched.items())))
    return metrics


def _phases(expect: Expect, record: TurnRecord) -> list[Metric]:
    if expect.phases is None:
        return []
    actual = record.phases()
    return [_check("phases", actual == expect.phases,
                   f"expected {expect.phases}, got {actual}")]


def _fence_kinds(expect: Expect, record: TurnRecord) -> list[Metric]:
    if expect.fence_kinds is None:
        return []
    actual = sorted(set(record.kinds()))
    wanted = sorted(set(expect.fence_kinds))
    return [_check("fence_kinds", actual == wanted, f"expected {wanted}, got {actual}")]


def _clarify(expect: Expect, record: TurnRecord) -> list[Metric]:
    if expect.clarify_options is None:
        return []
    clarify = record.one("clarify")
    if clarify is None:
        return [_check("clarify.options", False, "no clarify fence on this turn")]
    options = clarify.get("options", [])
    question = str(clarify.get("question", "")).strip()
    return [
        _check("clarify.options", len(options) == expect.clarify_options,
               f"expected {expect.clarify_options} options, got {len(options)}: {options}"),
        _check("clarify.question", bool(question), f"question={question!r}"),
    ]


def _tool_budget(expect: Expect, record: TurnRecord) -> list[Metric]:
    if expect.max_tool_calls is None:
        return []
    used = len(record.analyst_tool_calls)
    names = [c.tool for c in record.analyst_tool_calls]
    return [_check("tool_calls.max", used <= expect.max_tool_calls,
                   f"analyst made {used} tool call(s) (limit {expect.max_tool_calls}): {names}")]


def _error_fence(expect: Expect, record: TurnRecord) -> list[Metric]:
    if expect.error is None:
        return []
    errors = record.of("error")
    if not errors:
        return [_check("error.emitted", False, "expected an error fence, got none")]
    payload = errors[0]
    metrics = [_check("error.emitted", True, f"error fence: {payload}")]
    if expect.error.phase is not None:
        metrics.append(_check(
            "error.phase", payload.get("phase") == expect.error.phase,
            f"expected phase {expect.error.phase!r}, got {payload.get('phase')!r}"))
    if expect.error.reason is not None:
        metrics.append(_check(
            "error.reason", payload.get("reason") == expect.error.reason,
            f"expected reason {expect.error.reason!r}, got {payload.get('reason')!r}"))
    if expect.error.correlation_id:
        correlation_id = str(payload.get("correlation_id", ""))
        metrics.append(_check(
            "error.correlation_id", bool(correlation_id),
            f"correlation_id={correlation_id!r}"))
    return metrics


def _mentions(
    must: list[Mention], must_not: list[Mention], record: TurnRecord, prefix: str
) -> list[Metric]:
    metrics = []
    for mention in must:
        hit = _matches(record, mention)
        metrics.append(Metric(
            metric=f"{prefix}narrative.mentions[{mention.pattern}]", passed=hit,
            detail="found in the narrative" if hit else "missing from the narrative",
            evidence={} if hit else {"narrative": _excerpt(record.narrative)},
        ))
    for mention in must_not:
        hit = _matches(record, mention)
        metrics.append(Metric(
            metric=f"{prefix}narrative.avoids[{mention.pattern}]", passed=not hit,
            detail="absent from the narrative" if not hit else "present in the narrative",
            evidence={"narrative": _excerpt(record.narrative)} if hit else {},
        ))
    return metrics
