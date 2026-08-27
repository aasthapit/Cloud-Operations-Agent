"""In-process service harness for the headless tests (NFR-QE-1).

Everything here boots real service code inside the test event loop: the
domain MCP servers' Starlette apps, the gateway's `build_app()`, and real
streamable-HTTP MCP clients between them. Nothing shells out, nothing needs
Docker or Ollama, and no dev port is ever bound - ports come from the
kernel via a bind-0 probe.

Two constraints drive the shape of this module:

- `get_settings()` is an lru_cache singleton, so env has to be set BEFORE
  anything reads it and the cache has to be cleared on both sides of a
  test. `service_env` does both.
- Readiness is polled on an observable condition (the socket accepts, the
  gateway advertises its catalog), never slept on, so a slow machine
  makes the test slower rather than flaky.
"""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.routing import Mount

from cloudops.common.settings import get_settings

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


def free_port() -> int:
    """A port the kernel just handed out, so tests never touch 8001/8010-8012."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def service_env(monkeypatch: Any, **env: str) -> Iterator[None]:
    """Set CLOUDOPS_* env and reset the settings singleton around the test."""
    get_settings.cache_clear()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@asynccontextmanager
async def serve_asgi(app: Any, port: int) -> AsyncIterator[None]:
    """Run an ASGI app on 127.0.0.1:port for the body of the block.

    `lifespan="on"` is required: the MCP session manager and the config
    watchers only start from the app's lifespan, and a mounted sub-app's
    lifespan does not run on its own.
    """
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    task = asyncio.create_task(server.serve())
    await wait_until(lambda: server.started, what=f"uvicorn on :{port}")
    try:
        yield
    finally:
        server.should_exit = True
        await task


@asynccontextmanager
async def serve_fastmcp(mcp_server: Any, port: int) -> AsyncIterator[None]:
    """Serve a FastMCP server the way cloudops.mcp_servers.shared.serve does,
    minus the config watcher (a test rebuilds config rather than editing it)."""

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield

    app = Starlette(routes=[Mount("/", app=mcp_server.streamable_http_app())], lifespan=lifespan)
    async with serve_asgi(app, port):
        yield


@asynccontextmanager
async def mcp_client(url: str) -> AsyncIterator[ClientSession]:
    """An initialized MCP client session against a streamable-HTTP endpoint."""
    async with (
        streamablehttp_client(url) as (read, write, _sid),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def wait_until(
    predicate: Callable[[], Any], what: str, timeout_s: float = 20.0, interval_s: float = 0.02
) -> None:
    """Poll a condition to a deadline. Bounded polling, never a bare sleep:
    a slow machine costs time here instead of a flaky assertion."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


async def wait_for_gateway_tools(url: str, minimum: int) -> list[str]:
    """Wait until the gateway advertises at least `minimum` tools.

    Downstream supervisors connect on their own tasks, so the catalog fills
    in shortly after the gateway starts serving; this is the observable
    signal that the whole chain is up.
    """
    names: list[str] = []

    async def ready() -> bool:
        nonlocal names
        try:
            async with mcp_client(url) as session:
                names = [t.name for t in (await session.list_tools()).tools]
        except Exception:  # noqa: BLE001 - still booting; keep polling
            return False
        return len(names) >= minimum

    await wait_until(ready, what=f"{minimum} tools at {url}")
    return names
