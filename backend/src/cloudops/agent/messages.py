"""Conversational copy, read from the config plane (FR-CFG-2).

The deterministic phases speak in the runtime's own voice - onboarding,
clarification questions, the analyst's tool-loop guidance - and that copy is
product wording, not logic. It lives in config/agent/messages.yaml and is
read fresh on every use, the same per-invocation path agent.yaml takes, so an
edit lands on the next message with no restart.

Failure is loud by design. A missing key, an unreadable file, or a template
whose placeholder the call site does not supply logs at error and renders a
visible marker. There is deliberately no built-in copy to fall back to: a
silent duplicate in code is how the config plane stops being the source of
truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from cloudops.common.config import load_yaml

log = structlog.get_logger("cloudops.messages")

MISSING = "[missing message: {key}]"


def _lookup(data: Any, key: str) -> Any:
    current = data
    for part in key.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def message(config_dir: Path, key: str, **values: Any) -> str:
    """Render one message by dotted key, with str.format placeholders."""
    try:
        data = load_yaml(config_dir / "agent" / "messages.yaml")
    except Exception as exc:  # noqa: BLE001 - a broken config file must not kill a turn
        log.error("messages.unreadable", key=key, error=str(exc)[:200])
        return MISSING.format(key=key)
    template = _lookup(data, key)
    if not isinstance(template, str):
        log.error("messages.missing_key", key=key)
        return MISSING.format(key=key)
    try:
        return template.format(**values).strip()
    except (KeyError, IndexError) as exc:
        log.error("messages.bad_placeholder", key=key, error=str(exc)[:200])
        return MISSING.format(key=key)
