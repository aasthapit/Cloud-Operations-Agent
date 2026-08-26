"""Structured logging for every backend service (NFR-OBS-3, NFR-LOG-*).

- structlog with contextvars binding: bind thread.id / user.sub once per
  request and every subsequent log line in that task carries them.
- Trace correlation: a processor injects trace_id/span_id from the current
  OpenTelemetry span so logs and traces join on one id.
- Redaction: cloudops.common.redact scrubs every event (the LAST processor
  before rendering, so nothing escapes).
- Rendering: pretty console lines in dev, single-line JSON in prod
  (CLOUDOPS_ENV=prod), matching what log shippers expect.

Stdlib logging (uvicorn, ADK, litellm) is routed through the same processor
chain so third-party lines are also correlated and redacted.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace

from cloudops.common.redact import structlog_redactor
from cloudops.common.settings import get_settings


def _inject_trace_ids(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach the active span's ids so one grep joins logs to a trace."""
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def setup_logging(service: str) -> structlog.stdlib.BoundLogger:
    """Configure process-wide logging; returns the service's root logger."""
    settings = get_settings()
    level = getattr(logging, settings.cloudops_log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_trace_ids,
        structlog_redactor,  # keep last before rendering: nothing escapes it
    ]

    renderer: Any
    if settings.is_dev:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, ADK, litellm, mcp) through the same
    # formatter so their lines are redacted and trace-correlated too.
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        foreign_pre_chain=shared_processors,
    )
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Noisy third-party loggers stay at WARNING unless we are debugging.
    for noisy in ("httpx", "httpcore", "LiteLLM", "watchfiles"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    return structlog.get_logger(service)


def bind_thread(thread_id: str | None = None, user_sub: str | None = None) -> None:
    """Bind per-conversation context; every later log line in this task
    carries it (NFR-OBS-2's log half)."""
    fields: dict[str, str] = {}
    if thread_id:
        fields["thread.id"] = thread_id
    if user_sub:
        fields["user.sub"] = user_sub
    if fields:
        structlog.contextvars.bind_contextvars(**fields)
