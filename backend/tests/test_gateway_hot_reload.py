"""Gateway registry hot reload (FR-GW-4, gap G8).

Adding, disabling, or re-enabling a downstream MCP server must converge the
gateway's supervisors and its advertised catalog without a restart. This
exercises that against a real downstream: a trivial FastMCP server served
in-process on a kernel-assigned port, registered through a temporary
servers.yaml.

The reload is driven by rewriting the file and awaiting HotConfig.reload()
rather than by waiting on the watchfiles watcher. It is the same code path
the watcher takes (validate, atomic swap, on_reload -> apply_config, which
is how gateway.build_app wires the two), minus the filesystem-event timing
that would make the assertion a race.

A second case boots the gateway's real ASGI app over the same registry and
lists tools through an MCP client, so the served surface is covered too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from harness import free_port, mcp_client, serve_asgi, serve_fastmcp, service_env, wait_until
from mcp.server.fastmcp import FastMCP

from cloudops.common.config import HotConfig
from cloudops.gateway.app import Gateway
from cloudops.gateway.registry import ServersConfig

PREFIX = "demo"


def build_downstream(port: int) -> FastMCP:
    """A two-tool MCP server standing in for a newly registered cloud domain."""
    mcp = FastMCP(
        "demo-mcp", host="127.0.0.1", port=port,
        streamable_http_path="/mcp", stateless_http=True,
    )

    @mcp.tool()
    def ping() -> dict[str, str]:
        """Liveness probe."""
        return {"status": "ok"}

    @mcp.tool()
    def forbidden() -> dict[str, str]:
        """Not on the allowlist; must never be advertised (FR-GW-3)."""
        return {"status": "leaked"}

    return mcp


def write_registry(path: Path, url: str, *, enabled: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version: 1\n"
        "servers:\n"
        f"  - prefix: {PREFIX}\n"
        "    name: Demo MCP\n"
        f'    url: "{url}"\n'
        f"    enabled: {'true' if enabled else 'false'}\n"
        "    timeout_seconds: 10\n"
        "    allow_tools:\n"
        "      - ping\n"
        "deny_tools: []\n",
        encoding="utf-8",
    )


@pytest_asyncio.fixture(loop_scope="function")
async def downstream() -> AsyncIterator[str]:
    """A running downstream MCP server; yields its streamable-HTTP URL."""
    port = free_port()
    async with serve_fastmcp(build_downstream(port), port):
        yield f"http://127.0.0.1:{port}/mcp"


async def tools_settle(gateway: Gateway, expected: set[str]) -> None:
    """Supervisors converge on their own tasks; wait for the catalog to match."""
    await wait_until(
        lambda: {t.name for t in gateway.list_tools()} == expected,
        what=f"gateway catalog == {sorted(expected) or '(empty)'}",
    )


@pytest.mark.asyncio
async def test_disable_and_reenable_without_restart(tmp_path: Path, downstream: str) -> None:
    registry = tmp_path / "gateway" / "servers.yaml"
    write_registry(registry, downstream, enabled=True)

    config: HotConfig[ServersConfig] = HotConfig(registry, validator=ServersConfig.model_validate)
    gateway = Gateway(config)
    config._on_reload = gateway.apply_config  # the wiring build_app performs
    try:
        await gateway.apply_config(config.value)
        # Namespaced, and the non-allowlisted tool never appears (FR-GW-2/3).
        await tools_settle(gateway, {f"{PREFIX}__ping"})

        write_registry(registry, downstream, enabled=False)
        assert await config.reload()
        await tools_settle(gateway, set())

        write_registry(registry, downstream, enabled=True)
        assert await config.reload()
        await tools_settle(gateway, {f"{PREFIX}__ping"})
    finally:
        await gateway.shutdown()


@pytest.mark.asyncio
async def test_a_rejected_edit_keeps_the_running_catalog(tmp_path: Path, downstream: str) -> None:
    """FR-CFG-3: an invalid registry edit is refused, the fleet keeps serving."""
    registry = tmp_path / "gateway" / "servers.yaml"
    write_registry(registry, downstream, enabled=True)

    config: HotConfig[ServersConfig] = HotConfig(registry, validator=ServersConfig.model_validate)
    gateway = Gateway(config)
    config._on_reload = gateway.apply_config
    try:
        await gateway.apply_config(config.value)
        await tools_settle(gateway, {f"{PREFIX}__ping"})

        registry.write_text("version: 1\nservers:\n  - prefix: 9bad\n", encoding="utf-8")
        assert await config.reload() is False
        assert {t.name for t in gateway.list_tools()} == {f"{PREFIX}__ping"}
    finally:
        await gateway.shutdown()


@pytest.mark.asyncio
async def test_registered_tools_are_callable_through_the_served_gateway(
    tmp_path: Path, downstream: str
) -> None:
    """The same registry, through the gateway's real ASGI app."""
    write_registry(tmp_path / "gateway" / "servers.yaml", downstream, enabled=True)
    gateway_port = free_port()

    with service_env(CLOUDOPS_CONFIG_DIR=str(tmp_path),
                     CLOUDOPS_GATEWAY_PORT=str(gateway_port)):
        from cloudops.gateway.app import build_app

        url = f"http://127.0.0.1:{gateway_port}/mcp"
        async with serve_asgi(build_app(), gateway_port):

            async def advertised() -> set[str]:
                async with mcp_client(url) as session:
                    return {t.name for t in (await session.list_tools()).tools}

            async def ping_is_advertised() -> bool:
                return await advertised() == {f"{PREFIX}__ping"}

            # The supervisor connects on its own task after the app starts.
            await wait_until(ping_is_advertised, what=f"{PREFIX}__ping advertised at {url}")

            async with mcp_client(url) as session:
                result = await session.call_tool(f"{PREFIX}__ping", {})
                assert not result.isError
                refused = await session.call_tool(f"{PREFIX}__forbidden", {})
                assert refused.isError
