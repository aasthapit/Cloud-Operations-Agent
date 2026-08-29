"""What one turn actually emitted, captured from the real code path.

A ``TurnRecord`` is the evidence every scorer reads. It carries the fenced
payloads, the fence-free narrative, the phase ticks in order, and the tool
calls the gateway saw - and, importantly, WHO authored each text, because one
of the load-bearing contract invariants is that only the deterministic runtime
writes ``cloudops-*`` fences (AGENT.md section 10, invariant 1). A fence in an
analyst-authored event is a protocol violation, and it can only be noticed by
keeping the author around.

``ToolCallRecorder`` is a pure-ASGI middleware in front of the gateway: it
buffers each request body, parses the JSON-RPC envelope, and records every
``tools/call``. It sits at the gateway rather than inside the agent so it sees
BOTH callers - the deterministic check engine and the analyst's tool loop -
and it never alters the request. The two are told apart afterwards by when
they happened: the orchestrator closes its gateway session before the
narrative phase starts, so a call recorded at or after the ``narrative start``
tick belongs to the model.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from cloudops.agent.protocol import FENCE_RE, extract_fences

ORCHESTRATOR_AUTHOR = "triage_orchestrator"


@dataclass
class ToolCall:
    tool: str
    args: dict[str, Any]
    at: float


class ToolCallRecorder:
    """ASGI middleware recording every MCP ``tools/call`` through the gateway."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.calls: list[ToolCall] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []

        async def recording_receive() -> Any:
            message = await receive()
            if message.get("type") == "http.request":
                chunks.append(message.get("body", b"") or b"")
                if not message.get("more_body"):
                    self._record(b"".join(chunks))
            return message

        await self.app(scope, recording_receive, send)

    def _record(self, body: bytes) -> None:
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return  # not a JSON-RPC frame; nothing to record and nothing to fail
        for frame in payload if isinstance(payload, list) else [payload]:
            if not isinstance(frame, dict) or frame.get("method") != "tools/call":
                continue
            params = frame.get("params") or {}
            self.calls.append(ToolCall(
                tool=str(params.get("name", "?")),
                args=dict(params.get("arguments") or {}),
                at=time.monotonic(),
            ))

    def snapshot(self) -> int:
        return len(self.calls)

    def since(self, index: int) -> list[ToolCall]:
        return self.calls[index:]


@dataclass
class TurnRecord:
    """One user message and everything the stack emitted in response."""

    index: int
    user: str
    fences: list[tuple[str, Any]] = field(default_factory=list)
    model_fence_kinds: list[str] = field(default_factory=list)
    echoed_fence_kinds: list[str] = field(default_factory=list)
    narrative: str = ""
    orchestrator_text: str = ""
    analyst_text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    analyst_tool_calls: list[ToolCall] = field(default_factory=list)
    duration_s: float = 0.0
    _runtime_blocks: set[str] = field(default_factory=set, repr=False)

    def add(self, author: str, text: str) -> None:
        stripped, found = extract_fences(text)
        if author == ORCHESTRATOR_AUTHOR:
            self._runtime_blocks |= {m.group(0) for m in FENCE_RE.finditer(text)}
            self.fences.extend(found)
        else:
            # An ECHO - a fence block byte-identical to one the runtime already
            # emitted this turn - is the model quoting its own context back,
            # which is what a model whose prompt carries the transcript does.
            # A fence the runtime never wrote is the model AUTHORING a typed
            # payload, which is the violation the invariant is about, and the
            # two are worth telling apart rather than lumping together. Echoes
            # stay out of `fences` so a payload is never counted twice.
            for match in FENCE_RE.finditer(text):
                kind = match.group(1)
                if match.group(0) in self._runtime_blocks:
                    self.echoed_fence_kinds.append(kind)
                    continue
                self.model_fence_kinds.append(kind)
                self.fences.extend(p for p in found if p[0] == kind)
        if not stripped:
            return
        self.narrative = f"{self.narrative}\n{stripped}".strip()
        if author == ORCHESTRATOR_AUTHOR:
            self.orchestrator_text = f"{self.orchestrator_text}\n{stripped}".strip()
        else:
            self.analyst_text = f"{self.analyst_text}\n{stripped}".strip()

    # -- accessors the scorers use -----------------------------------------

    def of(self, kind: str) -> list[Any]:
        return [payload for k, payload in self.fences if k == kind]

    def one(self, kind: str) -> Any | None:
        payloads = self.of(kind)
        return payloads[0] if len(payloads) == 1 else None

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.fences]

    def phases(self) -> list[str]:
        return [f"{p['phase']}:{p['status']}" for p in self.of("phase")]

    def outcome(self) -> str:
        """The turn's decision, in the vocabulary a scenario writes.

        Named rather than inferred field by field because it is the single
        thing a scenario most wants to assert: did this question resolve, ask
        exactly one question, or land in onboarding.
        """
        clarify = self.of("clarify")
        if clarify:
            return f"clarify:{clarify[0].get('kind', '?')}"
        if self.of("context"):
            return "resolved"
        return "onboarding"

    def evidence(self) -> dict[str, Any]:
        """The grounding an LLM judge is allowed to check the narrative against.

        Exactly the deterministic payloads, nothing else: if a claim in the
        prose is not supported by these, it was not grounded.
        """
        return {
            "context": self.of("context"),
            "attestation": self.of("attestation"),
            "app360": self.of("app360"),
            "clarify": self.of("clarify"),
            "tool_calls": [{"tool": c.tool, "args": c.args} for c in self.analyst_tool_calls],
        }
