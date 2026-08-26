"""The MCP gateway (FR-GW-1..7).

Architecture:
- A low-level MCP `Server` (schemas must pass through verbatim, which the
  FastMCP decorator API cannot do) served over streamable HTTP in stateless
  mode.
- One supervisor task per enabled downstream server. The supervisor owns the
  client-session context (anyio requires enter/exit in the same task),
  refreshes that server's slice of the tool catalog after every (re)connect,
  and reconnects with backoff when the transport dies. Tool CALLS run
  concurrently from request-handler tasks over the supervised session,
  which the MCP v1 client supports.
- Namespacing: downstream tool `foo` on prefix `ocp` is advertised as
  `ocp__foo` with its input/output schemas untouched (FR-GW-2).
- Policy: per-server allowlist plus global denylist decide what is
  advertised and callable (FR-GW-3, NFR-SEC-3). Timeouts cancel and report
  slow calls (FR-GW-7).
- Every proxied call emits an audit log line and an OTel span; incoming
  traceparent / X-Thread-Id headers are extracted by ASGI middleware so
  gateway spans join the agent's trace (NFR-OBS-1..2, FR-GW-5).
- servers.yaml hot-reloads: enabling/disabling/adding a server starts or
  stops its supervisor without a restart (FR-GW-4).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import mcp.types as types
import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from opentelemetry import context as otel_context
from opentelemetry import trace
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from cloudops.common.config import HotConfig
from cloudops.common.redact import redact_obj
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import extract_context
from cloudops.gateway.registry import ServerEntry, ServersConfig

log = structlog.get_logger("cloudops.gateway")
tracer = trace.get_tracer("cloudops.gateway")

# Per-request metadata extracted by the ASGI middleware; visible to the tool
# handlers because stateless streamable HTTP handles each POST in-request.
_request_thread: contextvars.ContextVar[str] = contextvars.ContextVar("thread_id", default="-")
_request_user: contextvars.ContextVar[str] = contextvars.ContextVar("user_sub", default="-")


class Downstream:
    """A supervised connection to one downstream MCP server."""

    RECONNECT_DELAY_S = 2.0

    def __init__(self, entry: ServerEntry, on_catalog: Any) -> None:
        self.entry = entry
        self._on_catalog = on_catalog  # callback(prefix, list[types.Tool] | None)
        self.session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"downstream:{self.entry.prefix}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self._on_catalog(self.entry.prefix, None)

    async def _supervise(self) -> None:
        """Connect, publish catalog, hold the connection; reconnect on death."""
        while not self._stop.is_set():
            try:
                async with AsyncExitStack() as stack:
                    read, write, _get_sid = await stack.enter_async_context(
                        streamablehttp_client(self.entry.url)
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listed = await session.list_tools()
                    self.session = session
                    await self._on_catalog(self.entry.prefix, listed.tools)
                    log.info(
                        "downstream.connected", prefix=self.entry.prefix,
                        url=self.entry.url, tools=len(listed.tools),
                    )
                    # Hold the context open until stop or transport death.
                    await self._stop.wait()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # ExceptionGroup teardown on transport death
                self.session = None
                await self._on_catalog(self.entry.prefix, None)
                log.warning(
                    "downstream.disconnected", prefix=self.entry.prefix,
                    error=str(exc)[:300], retry_in_s=self.RECONNECT_DELAY_S,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.RECONNECT_DELAY_S)
                except TimeoutError:
                    continue
        self.session = None


class Gateway:
    """Catalog + routing + policy over all supervised downstreams."""

    def __init__(self, config: HotConfig[ServersConfig]) -> None:
        self._config = config
        self._downstreams: dict[str, Downstream] = {}
        # alias -> (prefix, original tool name)
        self._routes: dict[str, tuple[str, str]] = {}
        self._tools: dict[str, list[types.Tool]] = {}  # prefix -> aliased tools
        self._lock = asyncio.Lock()

    # -- catalog ------------------------------------------------------------

    async def _publish_catalog(self, prefix: str, tools: list[types.Tool] | None) -> None:
        entry = next((s for s in self._config.value.servers if s.prefix == prefix), None)
        async with self._lock:
            self._routes = {a: r for a, r in self._routes.items() if r[0] != prefix}
            self._tools.pop(prefix, None)
            if tools is None or entry is None:
                return
            allowed = set(entry.allow_tools)
            denied = set(self._config.value.deny_tools)
            aliased: list[types.Tool] = []
            for tool in tools:
                if tool.name not in allowed or tool.name in denied:
                    continue
                alias = f"{prefix}__{tool.name}"
                self._routes[alias] = (prefix, tool.name)
                # Schema passthrough: only the name changes (FR-GW-2).
                aliased.append(tool.model_copy(update={"name": alias}))
            self._tools[prefix] = aliased

    def list_tools(self) -> list[types.Tool]:
        return [t for tools in self._tools.values() for t in tools]

    # -- lifecycle ----------------------------------------------------------

    async def apply_config(self, cfg: ServersConfig) -> None:
        """Converge running supervisors to the (re)loaded registry (FR-GW-4)."""
        wanted = {s.prefix: s for s in cfg.enabled_servers()}
        for prefix in list(self._downstreams):
            current = self._downstreams[prefix]
            entry = wanted.get(prefix)
            if entry is None or entry.url != current.entry.url:
                await current.stop()
                del self._downstreams[prefix]
                log.info("downstream.stopped", prefix=prefix)
        for prefix, entry in wanted.items():
            if prefix not in self._downstreams:
                ds = Downstream(entry, self._publish_catalog)
                self._downstreams[prefix] = ds
                ds.start()
            else:
                # Policy-only change (allowlist/timeout): re-publish catalog.
                self._downstreams[prefix].entry = entry
                session = self._downstreams[prefix].session
                if session is not None:
                    listed = await session.list_tools()
                    await self._publish_catalog(prefix, listed.tools)

    async def shutdown(self) -> None:
        for ds in list(self._downstreams.values()):
            await ds.stop()
        self._downstreams.clear()

    # -- proxying -----------------------------------------------------------

    async def call(self, alias: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Route one namespaced call downstream with policy, timeout, audit."""
        start = time.perf_counter()
        thread_id = _request_thread.get()
        route = self._routes.get(alias)

        def audit(outcome: str) -> None:
            log.info(
                "gateway.audit", tool=alias, outcome=outcome,
                duration_ms=round((time.perf_counter() - start) * 1000, 1),
                thread_id=thread_id, user_sub=_request_user.get(),
                args=redact_obj(_summarize(arguments)),
            )

        def error(text: str) -> types.CallToolResult:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=text)], isError=True
            )

        with tracer.start_as_current_span(f"gateway.call {alias}") as span:
            span.set_attribute("mcp.tool", alias)
            span.set_attribute("thread.id", thread_id)
            if route is None:
                audit("refused_unknown")
                return error(f"Unknown or not-allowed tool: {alias}")
            prefix, original = route
            ds = self._downstreams.get(prefix)
            if ds is None or ds.session is None:
                audit("downstream_unavailable")
                return error(f"Downstream server '{prefix}' is not connected; try again shortly")
            timeout = ds.entry.timeout_seconds
            try:
                result = await asyncio.wait_for(
                    ds.session.call_tool(original, arguments), timeout=timeout
                )
            except TimeoutError:
                audit("timeout")
                span.set_attribute("error", True)
                return error(f"Tool {alias} exceeded the {timeout}s gateway timeout and was cancelled")
            except Exception as exc:  # transport death mid-call, etc. (FR-GW-6)
                audit("transport_error")
                span.set_attribute("error", True)
                return error(f"Tool {alias} failed at the gateway: {type(exc).__name__}")
            audit("error" if result.isError else "ok")
            span.set_attribute("mcp.is_error", bool(result.isError))
            return result


def _summarize(arguments: dict[str, Any]) -> dict[str, Any]:
    """Compact argument summary for audit lines (values truncated)."""
    return {k: (v if not isinstance(v, str) or len(v) <= 120 else v[:117] + "...") for k, v in arguments.items()}


def build_app() -> Starlette:
    """Assemble the gateway ASGI app."""
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    settings = get_settings()
    servers_cfg: HotConfig[ServersConfig] = HotConfig(
        settings.config_dir / "gateway" / "servers.yaml",
        validator=ServersConfig.model_validate,
    )
    gateway = Gateway(servers_cfg)
    servers_cfg._on_reload = gateway.apply_config  # wire after both exist

    server: Server = Server("cloudops-gateway")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return gateway.list_tools()

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # Returning CallToolResult directly passes structuredContent and
        # isError through verbatim (supported by the v1 low-level server).
        return await gateway.call(name, arguments or {})

    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    async def mcp_endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI wrapper: extract trace context + thread metadata, then hand
        off to the MCP session manager."""
        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        token_ctx = otel_context.attach(extract_context(headers))
        t1 = _request_thread.set(headers.get("x-thread-id", "-"))
        t2 = _request_user.set(headers.get("x-user-sub", "-"))
        try:
            await manager.handle_request(scope, receive, send)
        finally:
            _request_thread.reset(t1)
            _request_user.reset(t2)
            otel_context.detach(token_ctx)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        from cloudops.common.config import watch_configs

        async with manager.run():
            await gateway.apply_config(servers_cfg.value)
            watcher = asyncio.create_task(watch_configs(servers_cfg))
            log.info("gateway.serving", port=settings.cloudops_gateway_port)
            try:
                yield
            finally:
                watcher.cancel()
                await gateway.shutdown()

    return Starlette(routes=[Mount("/mcp", app=mcp_endpoint)], lifespan=lifespan)
