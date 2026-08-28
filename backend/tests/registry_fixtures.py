"""An in-memory fleet registry for tests.

mongomock rather than a container so the suite stays hermetic (NFR-QE-1), and
the REAL seeder rather than hand-written documents so the fixtures and the
production write path cannot drift: every test below queries data that
`make mongo-seed` would have produced from the committed config/fleet/*.yaml.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import mongomock
import pytest
from conftest import CONFIG_DIR

from cloudops.registry.db import set_database
from cloudops.registry.seed import seed


@pytest.fixture
def seeded_registry() -> Iterator[Any]:
    """A mongomock database seeded from config/fleet/, installed globally."""
    client = mongomock.MongoClient()
    db = client["cloudops_test"]
    set_database(db)
    seed(CONFIG_DIR)
    yield db
    set_database(None)
