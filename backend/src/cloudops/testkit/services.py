"""In-process service harness (NFR-QE-1).

Everything here boots real service code inside the caller's event loop: the
domain MCP servers' Starlette apps, the gateway's ``build_app()``, and real
streamable-HTTP MCP clients between them. Nothing shells out, nothing needs
Docker or Ollama, and no dev port is ever bound - ports come from the kernel
via a bind-0 probe.

Two constraints drive the shape of this module:

- ``get_settings()`` is an lru_cache singleton, so env has to be set BEFORE
  anything reads it and the cache has to be cleared on both sides of a run.
  ``service_env`` does both, and restores the previous environment on exit so
  consecutive runs in one process stay isolated.
- Readiness is polled on an observable condition (the socket accepts, the
  gateway advertises its catalog), never slept on, so a slow machine makes a
  run slower rather than flaky.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from starlette.applications import Starlette
from starlette.routing import Mount

from cloudops.common.settings import get_settings
from cloudops.testkit.registry import REGISTRY_TOOLS

REPO_ROOT = Path(__file__).resolve().parents[4]
CONFIG_DIR = REPO_ROOT / "config"


def config_dir_with_registry(tmp_path: Path, registry_port: int) -> Path:
    """A copy of the committed config plane with the registry MCP registered.

    A harness serves its registry MCP on a kernel-assigned port, and the
    gateway learns about downstreams only from config/gateway/servers.yaml.
    Copying the whole plane keeps every other file (batteries, prompts, fleet,
    applications) exactly the committed one, so the run still exercises the
    shipped configuration.
    """
    root = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, root)
    point_registry_at(root, registry_port)
    return root


def point_registry_at(config_root: Path, registry_port: int) -> None:
    """Rewrite a config plane's `reg` downstream URL to a chosen port."""
    servers_path = config_root / "gateway" / "servers.yaml"
    servers = yaml.safe_load(servers_path.read_text())
    entry = next((s for s in servers["servers"] if s["prefix"] == "reg"), None)
    if entry is None:
        entry = {"prefix": "reg", "name": "Registry MCP", "enabled": True,
                 "timeout_seconds": 30, "allow_tools": list(REGISTRY_TOOLS)}
        servers["servers"].append(entry)
    entry["url"] = f"http://127.0.0.1:{registry_port}/mcp"
    servers_path.write_text(yaml.safe_dump(servers, sort_keys=False))


def free_port() -> int:
    """A port the kernel just handed out, so runs never touch 8001/8010-8013."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def service_env(**env: str) -> Iterator[None]:
    """Set CLOUDOPS_* env and reset the settings singleton around the block.

    The previous values are restored on exit (and keys that did not exist are
    removed), so one process can boot several differently-configured stacks in
    sequence - which is exactly what a scenario-driven eval run does.
    """
    previous = {key: os.environ.get(key) for key in env}
    get_settings.cache_clear()
    os.environ.update(env)
    get_settings.cache_clear()
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


def _clear_sse_shutdown_latch() -> None:
    """Undo sse_starlette's process-global shutdown latch.

    sse_starlette drains SSE responses gracefully when uvicorn stops, and it
    tracks that with a CLASS attribute, `AppStatus.should_exit`. It is set
    once and never cleared, on the assumption that a process hosts one server
    for its lifetime. A test session (or an eval run) hosts many: the first
    server we stop latches it, and from then on every EventSourceResponse in
    the process ends before writing a body - which is the transport MCP
    streamable HTTP runs on, so every later MCP request hangs or dies with an
    incomplete chunked read, in whatever run happens to come next.

    Clearing it around every start and stop keeps servers isolated. Graceful
    drain stays enabled, so shutdown still ends open streams instead of
    blocking on them.
    """
    from sse_starlette.sse import AppStatus

    AppStatus.should_exit = False


@asynccontextmanager
async def serve_asgi(app: Any, port: int) -> AsyncIterator[None]:
    """Run an ASGI app on 127.0.0.1:port for the body of the block.

    `lifespan="on"` is required: the MCP session manager and the config
    watchers only start from the app's lifespan, and a mounted sub-app's
    lifespan does not run on its own.
    """
    _clear_sse_shutdown_latch()
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
        _clear_sse_shutdown_latch()


@asynccontextmanager
async def serve_fastmcp(mcp_server: Any, port: int) -> AsyncIterator[None]:
    """Serve a FastMCP server the way cloudops.mcp_servers.shared.serve does,
    minus the config watcher (a harness rebuilds config rather than editing it)."""

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
