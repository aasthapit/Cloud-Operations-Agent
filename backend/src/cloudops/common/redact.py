"""The single redaction layer (NFR-LOG-1, NFR-LOG-3).

Everything that leaves a service as a log line, audit record, error message,
or span attribute passes through here. Two complementary strategies:

1. Pattern scrubbing: known secret shapes (bearer tokens, Authorization
   headers, PEM blocks, kubeconfig fragments, password/token/key=value
   pairs) are replaced wherever they appear in free text.
2. Exact-value scrubbing: at import time we collect the VALUES of every
   secret-shaped environment variable (name matching TOKEN/SECRET/PASSWORD/
   KEY/CREDENTIAL) and scrub those exact strings anywhere they appear.
   This is what makes the acceptance-criterion canary test deterministic:
   plant CLOUDOPS_CANARY_SECRET in the environment, grep the logs, find
   nothing.

Key-based masking: dictionary keys that look secret have their whole value
masked regardless of shape.
"""

from __future__ import annotations

import os
import re
from typing import Any

MASK = "[REDACTED]"

# Environment variable NAMES whose values must never appear anywhere.
_SECRET_ENV_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|APIKEY|API_KEY|CREDENTIAL|PRIVATE)", re.I)

# Dict keys whose values are masked wholesale.
_SECRET_KEY = re.compile(
    r"^(authorization|proxy-authorization|cookie|set-cookie)$|"
    r"(token|secret|password|passwd|apikey|api_key|credential|private_key|client_secret)",
    re.I,
)

# Free-text secret shapes.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Authorization / bearer headers wherever they got interpolated into text.
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+\s+\S+|\S+)"), r"\1" + MASK),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{8,}"), "Bearer " + MASK),
    # PEM blocks (keys and certs) collapse to a marker.
    (
        re.compile(r"-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----"),
        "-----PEM " + MASK + "-----",
    ),
    # key=value / key: value pairs for secret-shaped keys in free text.
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|token|secret|password|passwd|client[_-]?secret)\s*[:=]\s*)"
            r"([\"']?)[^\s\"',;]{4,}\2"
        ),
        r"\1\2" + MASK + r"\2",
    ),
    # kubeconfig credential fields.
    (re.compile(r"(?i)\b(client-key-data|client-certificate-data|certificate-authority-data)\s*:\s*\S+"), r"\1: " + MASK),
]


def _collect_env_secret_values() -> list[str]:
    """Values of secret-shaped env vars, longest first so substrings of a
    longer secret cannot survive after the longer one is scrubbed."""
    values = [
        v
        for k, v in os.environ.items()
        if _SECRET_ENV_NAME.search(k) and isinstance(v, str) and len(v) >= 6
    ]
    return sorted(set(values), key=len, reverse=True)


_ENV_SECRET_VALUES = _collect_env_secret_values()


def refresh_env_secrets() -> None:
    """Re-collect env secret values (tests mutate the environment)."""
    global _ENV_SECRET_VALUES
    _ENV_SECRET_VALUES = _collect_env_secret_values()


def redact_text(text: str) -> str:
    """Scrub secret shapes and known secret values from a string."""
    if not text:
        return text
    for value in _ENV_SECRET_VALUES:
        if value in text:
            text = text.replace(value, MASK)
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_obj(obj: Any, _depth: int = 0) -> Any:
    """Recursively scrub a structure (for audit records and span attributes).

    Depth-limited so a pathological self-referencing structure cannot hang
    a log call; anything deeper than 12 levels is stringified and scrubbed.
    """
    if _depth > 12:
        return redact_text(str(obj))
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and _SECRET_KEY.search(key):
                out[key] = MASK
            else:
                out[key] = redact_obj(value, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        seq = [redact_obj(v, _depth + 1) for v in obj]
        return seq if isinstance(obj, list) else tuple(seq)
    return obj


def structlog_redactor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog processor: scrub every event before it is rendered."""
    return redact_obj(event_dict)
