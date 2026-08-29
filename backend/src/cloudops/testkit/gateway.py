"""A GatewayClient-shaped facade over the live backend and the registry.

For callers that want the tool contract without a port: ``ocp__*`` dispatches
into a real ``LiveOpenShiftBackend`` over ``FakeFleet``, ``reg__*`` into
``FakeRegistry``. Both halves answer the way the real MCP servers do, so a
caller that talks to this is exercising the tool contract, not a mock of a
mock. Every call is recorded on ``calls`` so a test can assert what was asked.
"""

from __future__ import annotations

from typing import Any

from cloudops.agent.gateway_client import ToolCallError
from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend
from cloudops.testkit.fleet import FakeFleet, default_world
from cloudops.testkit.registry import FakeRegistry


class FakeGateway:
    """A GatewayClient-shaped facade over the live backend and the registry."""

    def __init__(self, fleet: FakeFleet | None = None,
                 registry: FakeRegistry | None = None) -> None:
        self.fleet = fleet or FakeFleet(default_world())
        self.ocp = LiveOpenShiftBackend(self.fleet)
        self.registry = registry or FakeRegistry()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> FakeGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(
        self, tool: str, args: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((tool, args))
        prefix, _, name = tool.partition("__")
        target = {"ocp": self.ocp, "reg": self.registry}.get(prefix)
        method = getattr(target, name, None) if target is not None else None
        if method is None:
            raise ToolCallError(f"unknown tool {tool}")
        result: dict[str, Any] = method(**args)
        return result
