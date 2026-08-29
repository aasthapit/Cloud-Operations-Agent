"""The seeded in-memory fleet registry, as a pytest fixture.

The double itself is ``cloudops.testkit.seeded_registry``: mongomock rather
than a container so the suite stays hermetic (NFR-QE-1), and the REAL seeder
rather than hand-written documents so the fixtures and the production write
path cannot drift. Everything a test queries here is data
``make mongo-seed`` would have produced from the committed config/fleet/*.yaml.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from conftest import CONFIG_DIR

from cloudops.testkit import seeded_registry as seeded_registry_cm


@pytest.fixture
def seeded_registry() -> Iterator[Any]:
    """A mongomock database seeded from config/fleet/, installed globally."""
    with seeded_registry_cm(CONFIG_DIR) as db:
        yield db
