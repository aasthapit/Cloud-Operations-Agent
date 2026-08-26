"""The typed-payload envelope between agent and console.

Structured payloads (context, attestation report, App 360 report, phase
progress, clarification options) travel INSIDE the A2A text stream as fenced
JSON blocks:

    ```cloudops-<kind>
    { ...payload... }
    ```

Why fences instead of a custom part type: they survive every A2A hop and
history store untouched, degrade gracefully in any plain-text client, and
the BFF can extract them with a regex and re-emit typed SSE events while
stripping them from the narrative stream (FR-UI-2). The analyst LLM is
instructed to ignore these blocks; its grounding arrives via its
instruction, not by parsing cards.

Kinds: phase | context | clarify | attestation | app360 | error
"""

from __future__ import annotations

import json
import re
from typing import Any

FENCE_RE = re.compile(r"```cloudops-([a-z0-9_]+)\n(.*?)\n```", re.DOTALL)


def fence(kind: str, payload: Any) -> str:
    """Wrap a payload for the stream. Payload must be JSON-serializable
    (pydantic models: pass model_dump(mode="json"))."""
    return f"```cloudops-{kind}\n{json.dumps(payload, ensure_ascii=False)}\n```"


def extract_fences(text: str) -> tuple[str, list[tuple[str, Any]]]:
    """Split a text into (narrative_without_fences, [(kind, payload), ...]).
    Used by tests; the BFF implements the same contract in TypeScript."""
    found: list[tuple[str, Any]] = []
    for match in FENCE_RE.finditer(text):
        try:
            found.append((match.group(1), json.loads(match.group(2))))
        except json.JSONDecodeError:
            continue
    return FENCE_RE.sub("", text).strip(), found
