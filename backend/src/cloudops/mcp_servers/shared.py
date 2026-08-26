"""Shared plumbing for the domain MCP servers.

- WorldHolder: the mock backend, hot-rebuilding the synthetic World whenever
  fleet.yaml / applications.yaml / scenario.yaml change (last known good on
  a bad edit, matching FR-CFG-3). Each server process holds its OWN World;
  consistency between processes is guaranteed by determinism, not sharing.
- instrumented: decorator giving every tool an OTel span plus a structured
  log line (tool name, duration, outcome) with redaction applied by the
  logging layer.
- serve: builds the Starlette app around a FastMCP server and runs uvicorn
  with a lifespan that (a) runs the MCP session manager (mounted sub-app
  lifespans do NOT run automatically in Starlette; skipping this breaks the
  first request) and (b) runs the config watcher.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from opentelemetry import trace
from starlette.applications import Starlette
from starlette.routing import Mount

from cloudops.common.settings import get_settings
from cloudops.mockfleet import World

log = structlog.get_logger("cloudops.mcp")


class WorldHolder:
    """Holds the current mock World and rebuilds it on config changes."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.world = World.from_config_dir(config_dir)  # boot must succeed: fail fast

    def _watch_paths(self) -> list[str]:
        return [
            str(self.config_dir / "fleet" / "fleet.yaml"),
            str(self.config_dir / "fleet" / "applications.yaml"),
            str(self.config_dir / "mock" / "scenario.yaml"),
        ]

    async def watch(self) -> None:
        from watchfiles import awatch

        async for _changes in awatch(*self._watch_paths()):
            try:
                self.world = World.from_config_dir(self.config_dir)
                log.info("mockfleet.reloaded")
            except Exception as exc:  # noqa: BLE001 - keep last known good world
                log.warning("mockfleet.reload_rejected", error=str(exc))


def _caller_context() -> tuple[Any, str]:
    """Trace context and thread id sent by the gateway in the request's _meta.

    The gateway's downstream HTTP session is long-lived, so per-call context
    cannot ride HTTP headers; it arrives as extra fields on the MCP request
    meta instead (traceparent, x-thread-id). Returns (otel_context | None,
    thread_id) and never raises: a direct (gateway-less) caller simply gets
    a fresh root span.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx

        meta = request_ctx.get().meta
        extra = dict(meta.model_extra or {}) if meta is not None else {}
    except LookupError:
        extra = {}
    carrier = {str(k).lower(): str(v) for k, v in extra.items()}
    ctx = None
    if "traceparent" in carrier:
        from cloudops.common.telemetry import extract_context

        ctx = extract_context(carrier)
    return ctx, carrier.get("x-thread-id", "-")


def instrumented(service: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Wrap a tool function with a span + log line, parented to the calling
    turn's trace via request meta (NFR-OBS-1..2). Applied INSIDE @mcp.tool()
    so FastMCP still sees the original signature for schema derivation."""

    tracer = trace.get_tracer(service)

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            parent, thread_id = _caller_context()
            with tracer.start_as_current_span(f"mcp.tool {fn.__name__}", context=parent) as span:
                span.set_attribute("mcp.tool", fn.__name__)
                span.set_attribute("thread.id", thread_id)
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    span.record_exception(exc)
                    log.warning("tool.error", tool=fn.__name__, thread_id=thread_id, error=str(exc))
                    raise
                log.info(
                    "tool.call", tool=fn.__name__, thread_id=thread_id,
                    duration_ms=round((time.perf_counter() - start) * 1000, 1),
                )
                return result

        return wrapper

    return deco


def serve(mcp_server: Any, port: int, holder: WorldHolder | None) -> None:
    """Run a FastMCP server over streamable HTTP with config watching."""

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        watcher: asyncio.Task[None] | None = None
        if holder is not None and get_settings().cloudops_backend_mode == "mock":
            watcher = asyncio.create_task(holder.watch())
        async with mcp_server.session_manager.run():
            log.info("mcp.serving", port=port, mode=get_settings().cloudops_backend_mode)
            try:
                yield
            finally:
                if watcher is not None:
                    watcher.cancel()

    app = Starlette(routes=[Mount("/", app=mcp_server.streamable_http_app())], lifespan=lifespan)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
