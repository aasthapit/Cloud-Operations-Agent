"""The config-driven check engine (decisions D1 + D3).

Executes the batteries defined in config/checks/*.yaml deterministically:
no LLM anywhere in this module. The engine:

1. Renders each check's templated args ({{ cluster }}, {{ namespace }}, ...).
2. De-duplicates identical (tool, args) pairs per run and fetches them
   concurrently through the gateway.
3. Evaluates each check's declarative rules against the structured result;
   the check status is the worst triggered outcome
   (unattestable > fail > maintenance > warn), `pass` when none trigger,
   and `error` when the tool itself failed (unknown is never healthy).
4. Derives the cluster verdict / report status per the PRD mappings
   (10.1 verdict precedence, FR-360-5).

Every result carries its full evidence trail for UI drill-down
(FR-ATT-6/9, FR-360-9).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from opentelemetry import trace

from cloudops.agent.gateway_client import GatewayClient, ToolCallError
from cloudops.agent.models import (
    App360Battery,
    App360Report,
    AppInstance,
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

log = structlog.get_logger("cloudops.checks")
tracer = trace.get_tracer("cloudops.checks")

_OUTCOME_PRECEDENCE = {
    CheckStatus.UNATTESTABLE: 4,
    CheckStatus.FAIL: 3,
    CheckStatus.MAINTENANCE: 2,
    CheckStatus.WARN: 1,
}


# ---------------------------------------------------------------------------
# rule evaluation
# ---------------------------------------------------------------------------


def _lookup(data: Any, path: str) -> Any:
    """Dotted-path lookup ('pods.crashloop', 'pools.0.name'). Missing -> None."""
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        else:
            return None
    return current


def _rule_triggers(rule: RuleDef, data: Any) -> bool:
    value = _lookup(data, rule.path)
    op, expected = rule.op, rule.value
    match op:
        case "eq":
            return value == expected
        case "ne":
            return value != expected
        case "gt" | "gte" | "lt" | "lte":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
            return {
                "gt": value > expected, "gte": value >= expected,
                "lt": value < expected, "lte": value <= expected,
            }[op]
        case "in":
            return value in (expected or [])
        case "not_in":
            return value not in (expected or [])
        case "empty":
            return not value
        case "not_empty":
            return bool(value)
        case "truthy":
            return bool(value)
        case "falsy":
            return not bool(value)
        case "exists":
            return value is not None
        case "absent":
            return value is None
        case _:
            # Unknown operator = config bug; be loud in logs, never silently pass.
            log.warning("checks.unknown_op", op=op, path=rule.path)
            return False


def _render_args(args: dict[str, Any], variables: dict[str, str]) -> dict[str, Any]:
    def render(v: Any) -> Any:
        if isinstance(v, str):
            for key, val in variables.items():
                v = v.replace("{{ " + key + " }}", val).replace("{{" + key + "}}", val)
        return v

    return {k: render(v) for k, v in args.items()}


def _observed(check: CheckDef, data: Any) -> str:
    """Render the observed reading for the UI row. Supports 'a/b' pairs."""
    if not check.observed:
        return ""
    if "/" in check.observed and not check.observed.startswith("http"):
        left, _, right = check.observed.partition("/")
        lv, rv = _lookup(data, left.strip()), _lookup(data, right.strip())
        if lv is not None or rv is not None:
            return f"{lv}/{rv}"
    value = _lookup(data, check.observed)
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        text = json.dumps(value, ensure_ascii=False)
        return text if len(text) <= 120 else text[:117] + "..."
    return str(value)


def evaluate_check(check: CheckDef, data: dict[str, Any] | None, error: str | None, duration_ms: float) -> CheckResult:
    """Turn one tool result (or failure) into a CheckResult with evidence."""
    now = datetime.now(UTC).isoformat()
    if error is not None or data is None:
        return CheckResult(
            id=check.id, name=check.name, severity=check.severity,
            status=CheckStatus.ERROR, observed="", reason=error or "no result",
            duration_ms=duration_ms,
            evidence=CheckEvidence(tool=check.tool, args=check.args, timestamp=now,
                                   runbook=check.runbook, error=error),
        )
    triggered: list[tuple[RuleDef, Any]] = [
        (rule, _lookup(data, rule.path)) for rule in check.rules if _rule_triggers(rule, data)
    ]
    status = CheckStatus.PASS if check.severity != "info" else CheckStatus.INFO
    reason = ""
    if triggered:
        worst = max(triggered, key=lambda t: _OUTCOME_PRECEDENCE[CheckStatus(t[0].outcome.value)])
        status = CheckStatus(worst[0].outcome.value)
        reason = worst[0].reason
    return CheckResult(
        id=check.id, name=check.name, severity=check.severity, status=status,
        observed=_observed(check, data), reason=reason, duration_ms=duration_ms,
        evidence=CheckEvidence(
            tool=check.tool, args=check.args, timestamp=now, runbook=check.runbook,
            triggered_rules=[
                {"path": r.path, "op": r.op, "value": r.value,
                 "observed": obs, "outcome": r.outcome.value, "reason": r.reason}
                for r, obs in triggered
            ],
        ),
    )


# ---------------------------------------------------------------------------
# batched tool fetching
# ---------------------------------------------------------------------------


class _Fetcher:
    """De-duplicates identical (tool, args) calls within one battery run and
    fetches the unique set concurrently."""

    def __init__(self, client: GatewayClient, timeout_s: float) -> None:
        self._client = client
        self._timeout = timeout_s
        self.results: dict[str, tuple[dict[str, Any] | None, str | None, float]] = {}

    @staticmethod
    def key(tool: str, args: dict[str, Any]) -> str:
        return tool + ":" + json.dumps(args, sort_keys=True)

    async def fetch_all(self, calls: dict[str, tuple[str, dict[str, Any]]]) -> None:
        async def one(key: str, tool: str, args: dict[str, Any]) -> None:
            start = time.perf_counter()
            try:
                data = await self._client.call(tool, args, timeout_s=self._timeout)
                self.results[key] = (data, None, (time.perf_counter() - start) * 1000)
            except (ToolCallError, Exception) as exc:  # noqa: BLE001 - one bad tool never aborts a battery (FR-GW-6 mirror)
                self.results[key] = (None, str(exc)[:300], (time.perf_counter() - start) * 1000)

        await asyncio.gather(*(one(k, t, a) for k, (t, a) in calls.items()))


# ---------------------------------------------------------------------------
# attestation battery
# ---------------------------------------------------------------------------


def derive_verdict(checks: list[CheckResult]) -> tuple[ClusterVerdict, list[str]]:
    """PRD 10.1 precedence. `error` checks add a signal but do not flip the
    verdict on their own (unknown != broken; unknown is also != healthy,
    which the signals row communicates)."""
    signals: list[str] = []
    unattestable = [c for c in checks if c.status == CheckStatus.UNATTESTABLE]
    critical_fails = [c for c in checks if c.status == CheckStatus.FAIL and c.severity == "critical"]
    maintenance = [c for c in checks if c.status == CheckStatus.MAINTENANCE]
    warns = [c for c in checks if c.status in (CheckStatus.WARN, CheckStatus.FAIL) and c not in critical_fails]
    errors = [c for c in checks if c.status == CheckStatus.ERROR]

    for c in unattestable + critical_fails + maintenance + warns:
        signals.append(f"{c.id}: {c.reason}" if c.reason else c.id)
    for c in errors:
        signals.append(f"{c.id}: check errored ({(c.evidence.error or '')[:80]})")

    if unattestable:
        return ClusterVerdict.UNATTESTABLE, signals
    if critical_fails:
        return ClusterVerdict.DEGRADED, signals
    if maintenance:
        return ClusterVerdict.MAINTENANCE, signals
    return ClusterVerdict.HEALTHY, signals


async def run_attestation(
    battery: AttestationBattery, clusters: list[str], client: GatewayClient, battery_version: str,
) -> list[ClusterAttestation]:
    """Run the attestation battery against every in-scope cluster concurrently."""
    timeout = float(battery.defaults.get("timeout_seconds", 20))

    async def attest(cluster: str) -> ClusterAttestation:
        start = time.perf_counter()
        with tracer.start_as_current_span("agent.phase.attestation") as span:
            span.set_attribute("cluster", cluster)
            variables = {"cluster": cluster}
            rendered = {c.id: _render_args(c.args, variables) for c in battery.checks}
            fetcher = _Fetcher(client, timeout)
            unique = {
                _Fetcher.key(c.tool, rendered[c.id]): (c.tool, rendered[c.id])
                for c in battery.checks
            }
            await fetcher.fetch_all(unique)
            results = []
            for check in battery.checks:
                data, error, duration = fetcher.results[_Fetcher.key(check.tool, rendered[check.id])]
                rendered_check = check.model_copy(update={"args": rendered[check.id]})
                result = evaluate_check(rendered_check, data, error, duration)
                span.set_attribute(f"check.{check.id}", result.status.value)
                results.append(result)
            verdict, signals = derive_verdict(results)
            span.set_attribute("verdict", verdict.value)
            log.info("attestation.done", cluster=cluster, verdict=verdict.value,
                     checks=len(results), duration_ms=round((time.perf_counter() - start) * 1000, 1))
            return ClusterAttestation(
                cluster=cluster, verdict=verdict, signals=signals, checks=results,
                battery_version=battery_version,
                attested_at=datetime.now(UTC).isoformat(),
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
            )

    return list(await asyncio.gather(*(attest(c) for c in clusters)))


# ---------------------------------------------------------------------------
# Application 360 battery
# ---------------------------------------------------------------------------


def _section_rollup(checks: list[CheckResult]) -> CheckStatus:
    order = [CheckStatus.FAIL, CheckStatus.UNATTESTABLE, CheckStatus.ERROR,
             CheckStatus.WARN, CheckStatus.MAINTENANCE, CheckStatus.MANUAL,
             CheckStatus.REGISTRY, CheckStatus.INFO, CheckStatus.PASS]
    for status in order:
        if any(c.status == status for c in checks):
            return status
    return CheckStatus.PASS


def report_status(sections: list[SectionResult]) -> str:
    """FR-360-5 mapping."""
    all_checks = [c for s in sections for c in s.checks]
    if any(c.status == CheckStatus.FAIL and c.severity == "critical" for c in all_checks):
        return "critical"
    if any(c.status in (CheckStatus.FAIL, CheckStatus.WARN) for c in all_checks):
        return "at_risk"
    return "healthy"


async def run_app360(
    battery: App360Battery,
    application: str,
    app_label: str,
    instance: AppInstance,
    attestation: ClusterAttestation | None,
    client: GatewayClient,
    battery_version: str,
) -> App360Report:
    """Run the App 360 battery for one (application, cluster, namespace)."""
    timeout = float(battery.defaults.get("timeout_seconds", 20))
    variables = {
        "cluster": instance.cluster, "namespace": instance.namespace,
        "app_label": app_label, "application": application,
        "environment": instance.environment,
    }

    with tracer.start_as_current_span("agent.phase.app360") as span:
        span.set_attribute("application", application)
        span.set_attribute("cluster", instance.cluster)

        # Fetch the registry entry once for every registry-sourced section.
        registry: dict[str, Any] = {}
        try:
            registry = await client.call("ocp__get_app_registry_entry", {"application": application}, timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("app360.registry_error", error=str(exc)[:200])

        # Batch every check across sections into one concurrent fetch.
        rendered: dict[tuple[int, str], dict[str, Any]] = {}
        unique: dict[str, tuple[str, dict[str, Any]]] = {}
        for section in battery.sections:
            for check in section.checks:
                args = _render_args(check.args, variables)
                rendered[(section.section, check.id)] = args
                unique[_Fetcher.key(check.tool, args)] = (check.tool, args)
        fetcher = _Fetcher(client, timeout)
        await fetcher.fetch_all(unique)

        sections: list[SectionResult] = []
        for sdef in battery.sections:
            checks: list[CheckResult] = []
            for check in sdef.checks:
                args = rendered[(sdef.section, check.id)]
                data, error, duration = fetcher.results[_Fetcher.key(check.tool, args)]
                checks.append(evaluate_check(check.model_copy(update={"args": args}), data, error, duration))
            # Registry facts render as rows so nothing silently disappears.
            facts = {}
            for path in sdef.registry_fields:
                value = _lookup(registry, path)
                facts[path] = value if value is not None else "not in registry"
            # Section 11 reuses the host cluster's attestation (source: attestation).
            if sdef.source == "attestation" and attestation is not None:
                checks.insert(0, CheckResult(
                    id="host-cluster-attestation",
                    name=f"Host cluster {instance.cluster}",
                    severity="critical",
                    status={
                        ClusterVerdict.HEALTHY: CheckStatus.PASS,
                        ClusterVerdict.MAINTENANCE: CheckStatus.MAINTENANCE,
                        ClusterVerdict.DEGRADED: CheckStatus.FAIL,
                        ClusterVerdict.UNATTESTABLE: CheckStatus.UNATTESTABLE,
                    }[attestation.verdict],
                    observed=attestation.verdict.value,
                    reason="; ".join(attestation.signals[:3]),
                    evidence=CheckEvidence(
                        tool="attestation", args={"cluster": instance.cluster},
                        timestamp=attestation.attested_at,
                    ),
                ))
            status = _section_rollup(checks) if (checks or sdef.source in ("checks", "attestation")) else (
                CheckStatus.REGISTRY if sdef.source == "registry" else CheckStatus.INFO
            )
            sections.append(SectionResult(
                section=sdef.section, title=sdef.title, source=sdef.source,
                status=status, checks=checks, registry_facts=facts,
                manual_items=sdef.manual_items,
            ))

        overall = report_status(sections)
        span.set_attribute("overall_status", overall)
        log.info("app360.done", application=application, cluster=instance.cluster,
                 status=overall, sections=len(sections))
        return App360Report(
            application=application, app_label=app_label,
            cluster=instance.cluster, namespace=instance.namespace,
            environment=instance.environment, overall_status=overall,
            sections=sections, battery_version=battery_version,
        )
