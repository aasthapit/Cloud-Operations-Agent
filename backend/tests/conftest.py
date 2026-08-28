"""Shared fixtures over the test doubles in tests/fakes.py.

Tests stay hermetic (NFR-QE-1): no cluster, no Mongo, no gateway, no MCP
servers, no Ollama, no ports. The doubles live only under tests/ - production
code has one backend, and a second one shipped for testing is how the two
drift apart.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fakes import ClusterFixture, FakeFleet, FakeGateway, FakeRegistry, default_world

from cloudops.common.config import load_yaml
from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture
def world() -> dict[str, ClusterFixture]:
    """The canned cluster world; mutate it in a test to reshape a cluster."""
    return default_world()


@pytest.fixture
def fleet(world: dict[str, ClusterFixture]) -> FakeFleet:
    return FakeFleet(world)


@pytest.fixture
def ocp(fleet: FakeFleet) -> LiveOpenShiftBackend:
    return LiveOpenShiftBackend(fleet)


@pytest.fixture
def gateway(fleet: FakeFleet) -> FakeGateway:
    return FakeGateway(fleet)


@pytest.fixture(scope="session")
def app_registry() -> FakeRegistry:
    return FakeRegistry(CONFIG_DIR)


@pytest.fixture(scope="session")
def registry() -> dict[str, Any]:
    """The application registry as the orchestrator loads it."""
    registry_data: dict[str, Any] = load_yaml(CONFIG_DIR / "fleet" / "applications.yaml")
    return registry_data
