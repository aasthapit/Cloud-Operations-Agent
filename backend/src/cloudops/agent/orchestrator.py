"""The triage orchestrator: decision D1 made code.

A custom ADK BaseAgent whose _run_async_impl enforces the turn lifecycle
from the PRD (Figure 3) and the user-flows spec (turn state machine):

  1. identity gate      no claims -> onboarding guidance, nothing runs (FR-ID-4)
  2. context gate       resolve or ask exactly ONE question (FR-CTX-3..5)
  3. attestation gate   every in-scope cluster attested fresh (TTL) before
                        anything else (FR-ATT-1, FR-ATT-7)
  4. Application 360    automatic on first resolution (FR-360-1)
  5. analyst            the LLM narrates and investigates, grounded

The PHASES are code; the CONTENT of each phase (which checks, thresholds,
prompts) is hot-reloaded configuration (D3). Batteries are re-read every
turn with last-known-good fallback, so a battery edit lands on the next
message and a broken edit never takes the agent down (FR-CFG-2/3).

Phases 1 to 4 are deterministic and are what the console renders as cards,
so phase 5 is the only one allowed to fail softly: when inference is
unreachable the turn still COMPLETES, carrying the cards plus a notice with
a correlation id instead of the narrative (F8). Re-attestation additionally
carries a per-cluster verdict delta, and a change leads the narrative (F5).

Typed payloads (context, reports, clarifications, phase progress) stream as
cloudops fences (see protocol.py); the analyst never invents them.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from opentelemetry import trace
from pydantic import PrivateAttr

from cloudops.agent import checks as check_engine
from cloudops.agent import context as ctx_resolution
from cloudops.agent.context import Claims, Clarify, Onboarding, Resolved
from cloudops.agent.gateway_client import GatewayClient
from cloudops.agent.model_factory import agent_tuning
from cloudops.agent.models import (
    App360Battery,
    AttestationBattery,
    AttestationChange,
    AttestationReport,
    ClusterAttestation,
    ClusterVerdict,
    ResolvedContext,
)
from cloudops.agent.protocol import fence
from cloudops.common.config import config_version, load_yaml
from cloudops.common.logging import bind_thread
from cloudops.common.settings import get_settings

log = structlog.get_logger("cloudops.orchestrator")
tracer = trace.get_tracer("cloudops.orchestrator")


class TriageOrchestrator(BaseAgent):
    """Deterministic root agent; the analyst LlmAgent is its only sub-agent."""

    model_config = {"arbitrary_types_allowed": True}

    analyst: LlmAgent

    # last-known-good config caches (FR-CFG-3), keyed by file name
    _config_cache: dict[str, Any] = PrivateAttr(default_factory=dict)

    def __init__(self, analyst: LlmAgent) -> None:
        super().__init__(
            name="triage_orchestrator",
            description="First-level OpenShift fleet triage: attest, resolve, report, investigate",
            analyst=analyst,  # type: ignore[call-arg]  # pydantic field on this subclass
            sub_agents=[analyst],
        )

    # ------------------------------------------------------------------
    # config plane access (fresh-read with last known good)
    # ------------------------------------------------------------------

    def _load(self, relpath: str, validator: Any | None = None) -> Any:
        path = get_settings().config_dir / relpath
        try:
            raw = load_yaml(path)
            value = validator(raw) if validator else raw
            self._config_cache[relpath] = value
            return value
        except Exception as exc:  # noqa: BLE001 - keep last known good (FR-CFG-3)
            if relpath in self._config_cache:
                log.warning("config.reload_rejected", file=relpath, error=str(exc)[:200])
                return self._config_cache[relpath]
            raise

    # ------------------------------------------------------------------
    # event helpers
    # ------------------------------------------------------------------

    def _event(self, text: str, state_delta: dict[str, Any] | None = None) -> Event:
        return Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta=state_delta or {}),
        )

    def _phase(self, phase: str, status: str, **extra: Any) -> str:
        return fence("phase", {"phase": phase, "status": status,
                               "at": datetime.now(UTC).isoformat(), **extra})

    # ------------------------------------------------------------------
    # the turn
    # ------------------------------------------------------------------

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        try:
            async for event in self._turn(ctx):
                yield event
        except Exception as exc:  # noqa: BLE001 - never leak stack traces to clients (NFR-LOG-2)
            span = trace.get_current_span()
            trace_id = format(span.get_span_context().trace_id, "032x")
            log.exception("orchestrator.turn_failed")
            yield self._event(
                fence("error", {"correlation_id": trace_id, "phase": "orchestration"})
                + f"\nSomething went wrong on my side ({type(exc).__name__}). "
                f"Correlation id `{trace_id}`; the platform team can pull the full trace with it."
            )

    async def _turn(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        settings = get_settings()
        thread_id = ctx.session.id

        # --- identity ---------------------------------------------------
        meta: dict[str, Any] = {}
        run_config = getattr(ctx, "run_config", None)
        if run_config is not None and getattr(run_config, "custom_metadata", None):
            meta = run_config.custom_metadata.get("a2a_metadata") or {}
        claims_dict = meta.get("claims") or state.get("claims") or {}
        claims = Claims.from_metadata(claims_dict)
        bind_thread(thread_id=thread_id, user_sub=claims.sub or None)
        base_delta = {"claims": claims_dict, "thread_id": thread_id, "user_sub": claims.sub}

        texts: list[str] = []
        if ctx.user_content and ctx.user_content.parts:
            for part in ctx.user_content.parts:
                part_text = getattr(part, "text", None)
                if isinstance(part_text, str):
                    texts.append(part_text)
        user_text = " ".join(texts)

        tuning = agent_tuning(settings.config_dir)
        registry = self._load("fleet/applications.yaml")
        prior_dict = state.get("context")
        prior = ResolvedContext.model_validate(prior_dict) if prior_dict else None

        async with GatewayClient(thread_id, claims.sub or "-") as gc:
            # --- context gate -------------------------------------------
            with tracer.start_as_current_span("agent.phase.context_resolution"):
                outcome, pending = await ctx_resolution.resolve(
                    claims, user_text, registry, gc, prior, state.get("pending_clarify")
                )

            if isinstance(outcome, Onboarding):
                yield self._event(outcome.message, {**base_delta, "pending_clarify": None})
                return

            if isinstance(outcome, Clarify):
                payload = {"question": outcome.question, "options": outcome.options, "kind": outcome.kind}
                yield self._event(
                    fence("clarify", payload) + "\n" + outcome.question + "\n"
                    + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(outcome.options)),
                    {**base_delta, "pending_clarify": pending},
                )
                return

            assert isinstance(outcome, Resolved)
            context = outcome.context
            # FR-CTX-7: a mid-thread scope change (different application,
            # environment, or a switch to cluster scope) must not carry the
            # previous scope's Application 360 into this turn's grounding.
            # The attestation cache stays: it is keyed per cluster with its
            # own TTL, so newly in-scope clusters attest and known ones keep
            # their history, which is what the F5 delta is computed from.
            scope_changed = prior is not None and _scope_key(prior) != _scope_key(context)
            context_delta: dict[str, Any] = {
                **base_delta,
                "context": context.model_dump(mode="json"),
                "pending_clarify": None,
            }
            if scope_changed:
                context_delta |= {"app360_key": None, "app360_compact": []}
            yield self._event(fence("context", context.model_dump(mode="json")), context_delta)

            # --- attestation gate ---------------------------------------
            att_battery: AttestationBattery = self._load(
                "checks/health_attestation.yaml", AttestationBattery.model_validate
            )
            att_version = config_version([settings.config_dir / "checks" / "health_attestation.yaml"])
            ttl = float(tuning.get("attestation_ttl_seconds", 300))
            cache: dict[str, Any] = dict(state.get("attestation_cache") or {})
            by_cluster: dict[str, Any] = dict(cache.get("by_cluster") or {})
            now = time.time()

            needed = [
                c for c in context.clusters
                if c not in by_cluster
                or now - float(by_cluster[c].get("epoch", 0)) > ttl
                or by_cluster[c].get("battery_version") != att_version
            ]
            changes: list[AttestationChange] = []
            if needed:
                yield self._event(self._phase("attestation", "start", clusters=needed))
                fresh = await check_engine.run_attestation(att_battery, needed, gc, att_version)
                for att in fresh:
                    # A re-attestation (TTL expiry, battery change, or a scope
                    # that pulled this cluster back in) is the only place the
                    # F5 delta can be computed: state still holds the report
                    # this one replaces.
                    stored = (by_cluster.get(att.cluster) or {}).get("report")
                    previous = ClusterAttestation.model_validate(stored) if stored else None
                    delta = check_engine.attestation_delta(previous, att)
                    if delta is not None:
                        changes.append(delta)
                    by_cluster[att.cluster] = {
                        "verdict": att.verdict.value, "epoch": now,
                        "battery_version": att_version,
                        "report": att.model_dump(mode="json"),
                    }

            in_scope = [
                ClusterAttestation.model_validate(by_cluster[c]["report"])
                for c in context.clusters if c in by_cluster
            ]
            att_report = AttestationReport(
                clusters=in_scope, changes=[c.summary() for c in changes]
            )
            cache = {"by_cluster": by_cluster}
            yield self._event(
                fence("attestation", att_report.model_dump(mode="json"))
                + ("\n" + self._phase(
                    "attestation", "done",
                    verdicts={a.cluster: a.verdict.value for a in in_scope},
                    changes=[c.model_dump(mode="json", by_alias=True) for c in changes],
                )),
                {"attestation_cache": cache},
            )

            # --- Application 360 ----------------------------------------
            app360_compact: list[dict[str, Any]] = (
                [] if scope_changed else list(state.get("app360_compact") or [])
            )
            first_report_this_turn = False
            if context.scope == "app" and bool(tuning.get("auto_app360", True)):
                app_battery: App360Battery = self._load(
                    "checks/app360.yaml", App360Battery.model_validate
                )
                a360_version = config_version([settings.config_dir / "checks" / "app360.yaml"])
                key = f"{context.application}:{context.environment}:{a360_version}"
                if scope_changed or state.get("app360_key") != key:
                    first_report_this_turn = True
                    app360_compact = []
                    yield self._event(self._phase(
                        "app360", "start", application=context.application,
                        instances=[i.model_dump() for i in context.instances[:3]],
                    ))
                    for instance in context.instances[:3]:  # cap per FR-360-8 pragmatics
                        host_att = next(
                            (a for a in in_scope if a.cluster == instance.cluster), None
                        )
                        report = await check_engine.run_app360(
                            app_battery, context.application or "", context.app_label or "",
                            instance, host_att, gc, a360_version,
                        )
                        app360_compact.append(_compact_app360(report))
                        yield self._event(fence("app360", report.model_dump(mode="json")))
                    yield self._event(
                        self._phase("app360", "done"),
                        {"app360_key": key, "app360_compact": app360_compact},
                    )

        # --- ground and hand off to the analyst -------------------------
        if context.scope == "cluster":
            task = (
                "The user asked for a direct cluster attestation. Summarize the verdict "
                "in 2-3 sentences, then use ONE OR TWO tool calls at most to report what "
                "changed recently on this cluster (cluster version history, machine "
                "config pool state, recent events), then answer."
            )
        elif first_report_this_turn:
            task = (
                "First resolution in this thread. The deterministic phases already "
                "gathered ALL the evidence you need; it is in GROUNDING DATA below. "
                "Do NOT call any tools this turn. Write, in order: (1) per-cluster "
                "attestation summary, at most three sentences each; (2) the Application "
                "360 narrative fields (executive summary, findings for failing/warning "
                "sections, numbered recommendations, final assessment reason); (3) a "
                "direct answer to the user's question with the single most useful next "
                "step. Be explicit about platform-attributable vs application-"
                "attributable causes."
            )
        else:
            task = (
                "Follow-up turn. Answer the user's question; call a tool only when the "
                "grounding data cannot answer it. "
                + ("The attestation changed since the last check; open with that change, "
                   "in one sentence, before you answer the question. "
                   if changes else "")
                + "Ground every claim in tool results or the grounding data."
            )

        yield self._event(
            self._phase("narrative", "start"),
            {"grounding_text": _grounding_text(context, in_scope, changes, app360_compact),
             "task_hint": task,
             "conversation_text": _transcript(ctx, user_text)},
        )
        # F8: the analyst tier is the only part of a turn that depends on an
        # inference backend, so it is the only part allowed to fail softly.
        # The deterministic cards are already on the wire; losing the prose
        # must read as a degraded answer, not as a crashed turn.
        try:
            with tracer.start_as_current_span("agent.phase.narrative"):
                async for event in self.analyst.run_async(ctx):
                    yield event
        except Exception as exc:  # noqa: BLE001 - degrade, never propagate (F8, D1)
            yield self._narrative_degraded(exc)

    def _narrative_degraded(self, exc: BaseException) -> Event:
        """The F8 notice: cards stand, analysis unavailable, correlation id."""
        trace_id = format(trace.get_current_span().get_span_context().trace_id, "032x")
        unreachable = _is_connection_error(exc)
        reason = "inference_unreachable" if unreachable else "narrative_failed"
        log.exception("orchestrator.narrative_failed", reason=reason, correlation_id=trace_id)
        opening = (
            "I could not reach the inference backend that writes up these results"
            if unreachable
            else "The narrative step failed while writing up these results"
        )
        return self._event(
            fence("error", {"phase": "narrative", "correlation_id": trace_id, "reason": reason})
            + f"\n{opening}, so there is no written analysis this turn. Everything above "
            "still stands: the resolved context, the cluster health attestation, and any "
            "Application 360 sections come from the deterministic check engine, not from "
            "the model, and they were produced normally. Read the cards directly, or ask "
            f"again once inference is back. Correlation id `{trace_id}` maps to the full "
            "trace of this turn."
        )


def _scope_key(context: ResolvedContext) -> tuple[Any, ...]:
    """What makes two turns the same triage scope (FR-CTX-7)."""
    return (context.scope, context.application, context.environment, tuple(context.clusters))


# Connection-shaped failures across the stacks the analyst can sit on: stdlib,
# httpx, aiohttp, litellm. Matched by name (and through the __cause__ chain)
# so this module never has to import an inference client to classify one.
_CONNECTION_ERROR_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError", "ClientConnectorError",
    "ClientConnectionError", "ConnectError", "ConnectTimeout", "ConnectTimeoutError",
    "PoolTimeout", "ReadTimeout", "ReadTimeoutError", "ServerDisconnectedError",
    "ServiceUnavailableError", "Timeout", "TransportError", "WriteTimeout",
})


def _is_connection_error(exc: BaseException) -> bool:
    """Is this 'the inference backend is not there' rather than 'it broke'?

    Worth distinguishing: unreachable earns a calmer, more accurate sentence
    for the far more common case (backend down or misconfigured port).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        if type(current).__name__ in _CONNECTION_ERROR_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def _grounding_text(
    context: ResolvedContext,
    attestations: list[ClusterAttestation],
    changes: list[AttestationChange],
    app360_compact: list[dict[str, Any]],
) -> str:
    """The analyst's per-turn grounding: JSON evidence, led by any directive.

    Two directives can precede the payload, and both are ordering decisions
    the model must not be free to make: a verdict change leads the answer
    (F5), and an unattestable cluster caps what may be claimed at all
    (FR-ATT-5).
    """
    payload = {
        "resolved_context": context.model_dump(mode="json"),
        "attestation": {
            a.cluster: {"verdict": a.verdict.value, "signals": a.signals[:6]}
            for a in attestations
        },
        "attestation_changes": [c.model_dump(mode="json", by_alias=True) for c in changes],
        "app360": app360_compact,
    }
    lead: list[str] = []
    if changes:
        lead.append(
            "CHANGE SINCE THE LAST ATTESTATION (state this first, in one sentence, "
            "before you answer the question): " + "; ".join(c.summary() for c in changes) + "."
        )
    unattestable = [a.cluster for a in attestations if a.verdict == ClusterVerdict.UNATTESTABLE]
    if unattestable:
        lead.append(
            "CONFIDENCE CAP for " + ", ".join(unattestable) + ": the attestation came back "
            "unattestable, which means the monitoring pipeline itself could not be trusted, "
            "so platform health there CANNOT be confirmed. Say that plainly. Do not call "
            "the cluster healthy and do not call it unhealthy; assert no cluster-level "
            "conclusion about it at all. Treat any application finding on it as unverified "
            "platform context, and put repairing monitoring first among the next steps."
        )
    return "\n\n".join([*lead, json.dumps(payload, ensure_ascii=False)])


def _transcript(ctx: InvocationContext, current_user_text: str) -> str:
    """A fence-stripped, size-bounded transcript for the analyst's prompt.

    The analyst runs with include_contents='none' (the raw history carries
    tens of KB of report JSON that would wreck a small local model's prefill
    time), so this curated view is its only conversational memory: user
    turns and the analyst's own final texts, most recent last.
    """
    from cloudops.agent.protocol import extract_fences

    lines: list[str] = []
    for event in getattr(ctx.session, "events", []) or []:
        content = getattr(event, "content", None)
        if content is None or not getattr(content, "parts", None):
            continue
        text = " ".join(
            p.text for p in content.parts if isinstance(getattr(p, "text", None), str)
        ).strip()
        if not text:
            continue
        author = getattr(event, "author", "")
        if author == "user":
            lines.append(f"User: {text[:600]}")
        elif author == "analyst" and not getattr(event, "partial", False):
            stripped, _ = extract_fences(text)
            if stripped:
                lines.append(f"You: {stripped[:800]}")
    lines.append(f"User: {current_user_text[:600]}")
    return "\n".join(lines[-12:])


def _compact_app360(report: Any) -> dict[str, Any]:
    """The narrative-grounding projection of a full report (token-lean)."""
    problems = []
    for section in report.sections:
        for check in section.checks:
            if check.status.value in ("fail", "warn", "error", "unattestable", "maintenance"):
                problems.append({
                    "section": section.section, "check": check.id,
                    "status": check.status.value, "observed": check.observed,
                    "reason": check.reason,
                })
    return {
        "instance": f"{report.namespace} @ {report.cluster} ({report.environment})",
        "application": report.application,
        "overall_status": report.overall_status,
        "problems": problems,
    }


def build_config_dir_paths(config_dir: Path) -> list[Path]:
    """Files whose hash forms the console's config-version chip."""
    return [
        config_dir / "checks" / "health_attestation.yaml",
        config_dir / "checks" / "app360.yaml",
        config_dir / "agent" / "system_prompt.md",
        config_dir / "agent" / "routing.md",
        config_dir / "agent" / "agent.yaml",
        config_dir / "models.yaml",
    ]
