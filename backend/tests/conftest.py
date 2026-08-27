"""Shared fixtures and the one test double the whole suite leans on.

Tests must stay hermetic (NFR-QE-1): no gateway, no MCP servers, no Ollama,
no ports. WorldGateway makes that cheap for anything that talks to the tool
tier, because every gateway tool is `<domain>__<world method>` with matching
keyword arguments - exactly the mapping the two mock MCP servers implement -
so dispatching straight into the mock World is faithful, not a mock of a mock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cloudops.common.config import load_yaml
from cloudops.mockfleet import World

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class WorldGateway:
    """A GatewayClient-shaped facade over the mock World."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> WorldGateway:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def call(
        self, tool: str, args: dict[str, Any], timeout_s: float = 30.0
    ) -> dict[str, Any]:
        self.calls.append((tool, args))
        _, _, name = tool.partition("__")
        method = getattr(self.world, name, None)
        if method is None:
            raise AssertionError(f"unexpected tool {tool}")
        result: dict[str, Any] = method(**args)
        return result


@pytest.fixture(scope="session")
def world() -> World:
    return World.from_config_dir(CONFIG_DIR)


@pytest.fixture(scope="session")
def registry() -> dict[str, Any]:
    registry_data: dict[str, Any] = load_yaml(CONFIG_DIR / "fleet" / "applications.yaml")
    return registry_data
