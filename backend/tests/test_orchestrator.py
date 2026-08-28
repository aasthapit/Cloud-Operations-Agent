"""Turn-lifecycle tests for the deterministic orchestrator.

These drive _run_async_impl directly against the FakeGateway (canned cluster
APIs plus the registry contract) and a stubbed analyst, applying each event's
state_delta the way the ADK runner does, so a whole turn (context,
attestation, App 360, grounding, narrative hand-off) is exercised without a
real gateway, an inference backend, or a port.

Covered: the F5 re-attestation delta (G1), the F8 narrative degrade (G2), the
FR-ATT-5 unattestable confidence cap (G3), and FR-CTX-7 scope invalidation
(G4).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fakes import APP_NS, DEGRADED, HEALTHY, UNREACHABLE
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService, Session
from google.genai import types
from registry_fixtures import seeded_registry  # noqa: F401 - fixture import

from cloudops.agent import orchestrator as orchestrator_module
from cloudops.agent.models import (
    AttestationChange,
    CheckEvidence,
    CheckResult,
    CheckStatus,
    ClusterAttestation,
    ClusterVerdict,
    ResolvedContext,
)
from cloudops.agent.orchestrator import TriageOrchestrator, _grounding_text, _is_connection_error
from cloudops.agent.protocol import extract_fences

# LiveFleet resolves cluster records through the MongoDB registry since the
# live cutover, so the fleet doubles in this module need the seeded mongomock
# registry standing behind them.
pytestmark = pytest.mark.usefixtures("seeded_registry")

PAYMENTS_CLAIMS = {"sub": "app-developer", "name": "App Developer", "groups": ["payments-eng"]}


class StubAnalyst(LlmAgent):
    """The LLM tier, stubbed: no model, no toolset, failure on demand."""

    fail_with: str = ""  # "" | "connection" | "generic"

    async def run_async(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        if self.fail_with == "connection":
            raise ConnectionError("[Errno 61] Connection refused: localhost:11434")
        if self.fail_with == "generic":
            raise RuntimeError("the model emitted an unparseable tool call")
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text="narrative")]),
        )


class Turn:
    """One consumed turn: its events, plus the fences and deltas it carried."""

    def __init__(self, events: list[Event]) -> None:
        self.events = events
        self.fences: list[tuple[str, Any]] = []
        self.state_deltas: list[dict[str, Any]] = []
        for event in events:
            text = " ".join(
                p.text for p in (event.content.parts if event.content else [])
                if isinstance(getattr(p, "text", None), str)
            )
            _, found = extract_fences(text)
            self.fences.extend(found)
            if event.actions and event.actions.state_delta:
                self.state_deltas.append(dict(event.actions.state_delta))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.fences]

    def of(self, kind: str) -> list[Any]:
        return [payload for k, payload in self.fences if k == kind]

    def phase(self, phase: str, status: str) -> dict[str, Any]:
        matches = [p for p in self.of("phase") if p["phase"] == phase and p["status"] == status]
        assert matches, f"no {phase} {status} tick in {self.of('phase')}"
        return dict(matches[-1])

    def grounding(self) -> str:
        for delta in reversed(self.state_deltas):
            if "grounding_text" in delta:
                return str(delta["grounding_text"])
        raise AssertionError("the turn never handed grounding to the analyst")

    def narrative_text(self) -> str:
        return " ".join(
            extract_fences(" ".join(
                p.text for p in (event.content.parts if event.content else [])
                if isinstance(getattr(p, "text", None), str)
            ))[0]
            for event in self.events
        )


class Conversation:
    """A session plus the runner behavior the orchestrator depends on."""

    def __init__(self, gateway: Any, analyst: LlmAgent, claims: dict[str, Any]) -> None:
        self.orchestrator = TriageOrchestrator(analyst=analyst)
        self.gateway = gateway
        self.session = Session(
            id="thread-1", app_name="cloudops", user_id=str(claims["sub"]),
            state={"claims": claims},
        )
        self._turns = 0

    async def say(self, text: str) -> Turn:
        self._turns += 1
        ctx = InvocationContext(
            session_service=InMemorySessionService(),
            invocation_id=f"inv-{self._turns}",
            agent=self.orchestrator,
            session=self.session,
            user_content=types.Content(role="user", parts=[types.Part(text=text)]),
        )
        self.session.events.append(
            Event(author="user", content=types.Content(role="user", parts=[types.Part(text=text)]))
        )
        events: list[Event] = []
        async for event in self.orchestrator._run_async_impl(ctx):
            events.append(event)
            # The ADK runner applies state deltas before pulling the next
            # event; the orchestrator's later phases read that state back.
            if event.actions and event.actions.state_delta:
                self.session.state.update(event.actions.state_delta)
            self.session.events.append(event)
        return Turn(events)


@pytest.fixture
def conversation(gateway, monkeypatch):
    """Factory: a conversation whose gateway is the FakeGateway."""

    def build(analyst: LlmAgent | None = None, claims: dict[str, Any] | None = None) -> Conversation:
        convo = Conversation(gateway, analyst or StubAnalyst(name="analyst"), claims or PAYMENTS_CLAIMS)
        monkeypatch.setattr(
            orchestrator_module, "GatewayClient", lambda *a, **k: convo.gateway
        )
        return convo

    return build


def _stale_entry(cluster: str, verdict: str, failing: tuple[str, ...] = ()) -> dict[str, Any]:
    """A cached attestation old enough that the TTL forces a re-run (F5)."""
    report = ClusterAttestation(
        cluster=cluster, verdict=ClusterVerdict(verdict), signals=[],
        checks=[
            CheckResult(id=cid, name=cid, severity="critical", status=CheckStatus.FAIL,
                        evidence=CheckEvidence(tool="t", args={}, timestamp="now"))
            for cid in failing
        ],
        battery_version="stale",
    )
    return {
        "verdict": verdict, "epoch": time.time() - 100_000,
        "battery_version": "stale", "report": report.model_dump(mode="json"),
    }


class TestAttestationDeltaInATurn:
    """G1 / F5: a stale re-attestation must say what changed."""

    @pytest.mark.asyncio
    async def test_first_attestation_reports_no_changes(self, conversation):
        turn = await conversation().say("why is payments-api flaky in prod?")
        assert turn.phase("attestation", "done")["changes"] == []
        assert turn.of("attestation")[0]["changes"] == []
        assert "CHANGE SINCE THE LAST ATTESTATION" not in turn.grounding()

    @pytest.mark.asyncio
    async def test_stale_reattestation_reports_the_verdict_delta(self, conversation):
        convo = conversation()
        # The thread last saw the app's host cluster degraded on a NotReady
        # node, long enough ago that the TTL has expired; it is healthy now.
        convo.session.state["attestation_cache"] = {
            "by_cluster": {HEALTHY: _stale_entry(HEALTHY, "degraded", ("nodes",))}
        }
        turn = await convo.say("why is payments-api flaky in prod?")

        changes = turn.phase("attestation", "done")["changes"]
        assert [c["cluster"] for c in changes] == [HEALTHY]
        assert (changes[0]["from"], changes[0]["to"]) == ("degraded", "healthy")
        assert changes[0]["note"] == "cleared: nodes"
        assert turn.of("attestation")[0]["changes"] == [
            f"{HEALTHY}: degraded -> healthy (cleared: nodes)"
        ]

    @pytest.mark.asyncio
    async def test_change_leads_the_analyst_grounding(self, conversation):
        convo = conversation()
        convo.session.state["attestation_cache"] = {
            "by_cluster": {HEALTHY: _stale_entry(HEALTHY, "degraded", ("nodes",))}
        }
        grounding = (await convo.say("any update on payments-api in prod?")).grounding()
        assert grounding.startswith("CHANGE SINCE THE LAST ATTESTATION")
        assert f"{HEALTHY}: degraded -> healthy" in grounding


class TestGroundingText:
    """The two directives that may precede the evidence payload."""

    def _context(self) -> ResolvedContext:
        return ResolvedContext(scope="cluster", clusters=[UNREACHABLE])

    def _attestation(self, verdict: ClusterVerdict) -> ClusterAttestation:
        return ClusterAttestation(cluster=UNREACHABLE, verdict=verdict,
                                  signals=["api-reachability: did not respond"], checks=[])

    def test_no_lead_when_nothing_changed(self):
        text = _grounding_text(self._context(), [self._attestation(ClusterVerdict.HEALTHY)], [], [])
        assert "CHANGE SINCE THE LAST ATTESTATION" not in text
        assert "CONFIDENCE CAP" not in text
        assert json.loads(text)["attestation_changes"] == []

    def test_change_line_present_only_when_changed(self):
        change = AttestationChange(cluster=UNREACHABLE, from_verdict="degraded",
                                   to_verdict="healthy", note="cleared: nodes")
        text = _grounding_text(
            self._context(), [self._attestation(ClusterVerdict.HEALTHY)], [change], []
        )
        assert text.startswith("CHANGE SINCE THE LAST ATTESTATION")
        assert f"{UNREACHABLE}: degraded -> healthy (cleared: nodes)" in text

    def test_unattestable_cluster_caps_confidence(self):
        text = _grounding_text(
            self._context(), [self._attestation(ClusterVerdict.UNATTESTABLE)], [], []
        )
        assert f"CONFIDENCE CAP for {UNREACHABLE}" in text
        assert "CANNOT be confirmed" in text


class TestUnattestableTurn:
    """G3 / FR-ATT-5, end to end through a direct attestation turn. A cluster
    whose API did not answer was not attested; the turn must say so and cap
    what may be claimed, never call it degraded or healthy."""

    @pytest.mark.asyncio
    async def test_attesting_an_unreachable_cluster(self, conversation):
        turn = await conversation().say(f"attest {UNREACHABLE}")

        report = turn.of("attestation")[0]
        assert [c["verdict"] for c in report["clusters"]] == ["unattestable"]
        assert turn.phase("attestation", "done")["verdicts"] == {UNREACHABLE: "unattestable"}
        signals = " ".join(report["clusters"][0]["signals"]).lower()
        assert "did not respond" in signals

        grounding = turn.grounding()
        assert f"CONFIDENCE CAP for {UNREACHABLE}" in grounding
        assert "assert no cluster-level conclusion" in grounding


class TestNarrativeDegrade:
    """G2 / F8: losing the inference tier degrades the turn, never fails it."""

    @pytest.mark.asyncio
    async def test_unreachable_inference_keeps_the_cards_and_completes(self, conversation):
        convo = conversation(StubAnalyst(name="analyst", fail_with="connection"))
        turn = await convo.say("why is payments-api flaky in prod?")

        # Everything deterministic still reached the client, in order.
        kinds = turn.kinds()
        assert kinds.index("context") < kinds.index("attestation") < kinds.index("app360")
        assert kinds.index("app360") < kinds.index("error")

        [error] = turn.of("error")
        assert error["phase"] == "narrative"
        assert error["reason"] == "inference_unreachable"
        assert error["correlation_id"]
        # The generic turn handler never ran: no orchestration-phase error.
        assert all(e["phase"] == "narrative" for e in turn.of("error"))

        text = turn.narrative_text()
        assert "no written analysis" in text
        assert error["correlation_id"] in text
        assert "Traceback" not in text

    @pytest.mark.asyncio
    async def test_other_narrative_failures_are_classified_separately(self, conversation):
        convo = conversation(StubAnalyst(name="analyst", fail_with="generic"))
        turn = await convo.say(f"attest {DEGRADED}")
        assert turn.of("error")[0]["reason"] == "narrative_failed"

    def test_connection_error_classification(self):
        class APIConnectionError(Exception):
            pass

        assert _is_connection_error(ConnectionError("refused"))
        assert _is_connection_error(TimeoutError("timed out"))
        assert _is_connection_error(APIConnectionError("litellm cannot reach the backend"))
        wrapped = RuntimeError("completion failed")
        wrapped.__cause__ = ConnectionError("refused")
        assert _is_connection_error(wrapped)
        assert not _is_connection_error(ValueError("bad prompt"))


class TestScopeChangeInvalidation:
    """G4 / FR-CTX-7: a new scope re-runs its own phases."""

    @pytest.mark.asyncio
    async def test_environment_switch_reattests_and_rebuilds_app360(self, conversation):
        convo = conversation()
        first = await convo.say("why is payments-api flaky in prod?")
        assert {c["cluster"] for c in first.of("app360")} == {HEALTHY}

        second = await convo.say("switch to nonprod")
        assert second.of("context")[0]["environment"] == "nonprod"
        assert second.phase("attestation", "start")["clusters"] == [DEGRADED]
        assert [c["cluster"] for c in second.of("app360")] == [DEGRADED]
        # The prod report must not linger in the grounding for the new scope.
        grounded = json.loads(second.grounding())
        assert [a["instance"] for a in grounded["app360"]] == [
            f"{APP_NS} @ {DEGRADED} (nonprod)"
        ]
        assert set(grounded["attestation"]) == {DEGRADED}

    @pytest.mark.asyncio
    async def test_cluster_scope_switch_drops_the_stale_app360(self, conversation):
        convo = conversation()
        await convo.say("why is payments-api flaky in prod?")
        turn = await convo.say(f"attest {UNREACHABLE}")

        assert turn.of("context")[0]["scope"] == "cluster"
        assert turn.of("app360") == []
        grounded = json.loads(turn.grounding().split("\n\n")[-1])
        assert grounded["app360"] == []
        assert convo.session.state["app360_key"] is None
