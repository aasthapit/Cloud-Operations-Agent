"""MongoDB connection and index management for the fleet registry.

Role: the single place that knows a MongoDB exists. Everything above this
module (the accessors in ``queries``, the seeder, the registry MCP server,
the live fleet) sees collections and dictionaries, never a driver.

Why the connection is lazy and cached: the MCP servers boot without a
database and must keep serving even when the registry is down - an honest
``{"error": "registry unavailable"}`` per tool call beats a process that
refuses to start. So nothing connects at import time, and a failure to reach
MongoDB raises RegistryUnavailable, which the tool layer turns into that
payload rather than a stack trace.

Why the server-selection timeout is short: pymongo's 30s default would make
an unreachable registry look like a hang to the agent's tool loop. Three
seconds is long enough for a local container and short enough that "the
registry is down" is a fast, legible answer.

Indexes are created on first use rather than by a migration step, because
the collections are small, ``create_index`` is idempotent, and the seeder and
the servers must both work against a fresh empty database.

Test seam: ``set_database`` installs an in-memory mongomock database so the
registry tests exercise the real query code without a container.
"""

from __future__ import annotations

from typing import Any

import structlog
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from cloudops.common.settings import get_settings

log = structlog.get_logger("cloudops.registry")

Db = Database[dict[str, Any]]

# An unreachable registry must fail fast, not hang the agent's tool loop.
SERVER_SELECTION_TIMEOUT_MS = 3000

# Per collection: the natural key (also the unique index) and the secondary
# fields every accessor filters on. Keeping this table next to the connection
# means the document shape is declared in exactly one place.
INDEXES: dict[str, dict[str, Any]] = {
    "placements": {
        "unique": [("app_id", 1), ("cluster", 1), ("namespace", 1)],
        "single": ["app_id", "cluster", "namespace", "environment", "lob"],
    },
    "clusters": {
        "unique": [("name", 1)],
        "single": ["environment", "region", "ring", "aliases"],
    },
    "apps": {
        "unique": [("app_id", 1)],
        "single": ["application", "app_label", "lob"],
    },
}


class RegistryUnavailable(RuntimeError):
    """MongoDB could not be reached or refused the operation.

    Raised by everything in this package instead of driver exceptions, so the
    tool layer has one thing to catch and one honest message to report.
    """


_client: MongoClient[dict[str, Any]] | None = None
_db: Db | None = None
_indexed: set[int] = set()


def set_database(db: Db | None) -> None:
    """Install (or clear) the database handle. Tests use this with mongomock."""
    global _db, _client
    if _db is not None:
        _indexed.discard(id(_db))
    if _client is not None:
        _client.close()
        _client = None
    _db = db


def ensure_indexes(db: Db) -> None:
    """Create the registry's indexes, once per database handle.

    Idempotent by construction: ``create_index`` on an existing index is a
    no-op in MongoDB. The guard set only avoids the round trips.
    """
    if id(db) in _indexed:
        return
    try:
        for name, spec in INDEXES.items():
            collection = db[name]
            collection.create_index(spec["unique"], unique=True, name=f"{name}_natural_key")
            for field in spec["single"]:
                collection.create_index(field, name=f"{name}_{field}")
    except PyMongoError as exc:
        raise RegistryUnavailable(str(exc)) from exc
    _indexed.add(id(db))


def get_db() -> Db:
    """The registry database, connecting on first use.

    Raises RegistryUnavailable when MongoDB cannot be reached. The first
    index creation is what actually forces the connection, because pymongo
    constructs a client without touching the network.
    """
    global _client, _db
    if _db is None:
        settings = get_settings()
        _client = MongoClient(
            settings.cloudops_mongo_url,
            serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
            tz_aware=True,
        )
        _db = _client[settings.cloudops_mongo_db]
        # The URL can carry credentials, so log the database name only.
        log.info("registry.connecting", database=settings.cloudops_mongo_db)
    ensure_indexes(_db)
    return _db
