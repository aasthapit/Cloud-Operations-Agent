"""Re-export shim: the in-process service harness now lives in
``cloudops.testkit.services``.

One behavioural change came with the move: ``service_env`` no longer takes a
pytest ``monkeypatch``. It saves and restores the environment itself, because
the eval harness boots several differently-configured stacks in one process
and has no monkeypatch to borrow.
"""

from __future__ import annotations

from cloudops.testkit.services import (
    CONFIG_DIR,
    REPO_ROOT,
    config_dir_with_registry,
    free_port,
    mcp_client,
    point_registry_at,
    serve_asgi,
    serve_fastmcp,
    service_env,
    wait_for_gateway_tools,
    wait_until,
)

__all__ = [
    "CONFIG_DIR",
    "REPO_ROOT",
    "config_dir_with_registry",
    "free_port",
    "mcp_client",
    "point_registry_at",
    "serve_asgi",
    "serve_fastmcp",
    "service_env",
    "wait_for_gateway_tools",
    "wait_until",
]
