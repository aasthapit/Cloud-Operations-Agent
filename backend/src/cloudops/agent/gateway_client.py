"""MCP client used by the deterministic check engine.

The analyst LlmAgent reaches the gateway through ADK's McpToolset; the
check engine uses this thinner client instead because it needs (a) raw
CallToolResult access for rule evaluation and (b) per-conversation headers:
one session is opened per battery run with X-Thread-Id / X-User-Sub /
traceparent set at connect, which is how gateway audit lines and spans join
the conversation (NFR-OBS-2, FR-GW-5).
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from datetime import timedelta
from types import TracebackType
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from cloudops.common.settings import get_settings
from cloudops.common.telemetry import inject_headers


class ToolCallError(Exception):
    """A tool returned is_error or the transport failed."""


class GatewayClient:
    """One short-lived session against the gateway, headers bound to a thread."""

    def __init__(self, thread_id: str, user_sub: str) -> None:
        self._url = get_settings().cloudops_gateway_url
        self._headers = inject_headers({"X-Thread-Id": thread_id, "X-User-Sub": user_sub})
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> GatewayClient:
        self._stack = AsyncExitStack()
        read, write, _sid = await self._stack.enter_async_context(
            streamablehttp_client(self._url, headers=self._headers)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    async def call(self, tool: str, args: dict[str, Any], timeout_s: float = 30.0) -> dict[str, Any]:
        """Call a namespaced gateway tool; return its structured result.

        Raises ToolCallError with the downstream message on error results so
        the check engine can mark the check `error` (never `pass`)."""
        assert self._session is not None, "GatewayClient used outside its context"
        result = await self._session.call_tool(
            tool, args, read_timeout_seconds=timedelta(seconds=timeout_s)
        )
        if result.isError:
            text = result.content[0].text if result.content and hasattr(result.content[0], "text") else "tool error"
            raise ToolCallError(text)
        if result.structuredContent is not None:
            # FastMCP wraps non-object returns as {"result": ...}; ours are objects.
            return result.structuredContent
        # Fallback for downstreams without structured output: parse first text block.
        for block in result.content or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ToolCallError(f"unstructured, non-JSON tool result from {tool}") from exc
        raise ToolCallError(f"empty result from {tool}")
