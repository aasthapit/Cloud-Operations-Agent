"""The fleet registry: MongoDB-backed truth about apps, clusters, and placements.

Why this is a library and not just an MCP server: three consumers need the
same answers and must never disagree about them - the `reg__*` MCP tools, the
live fleet (which reads cluster records and their credentials to build
Kubernetes clients), and the seeder. Putting the queries here means the
registry has one implementation and the servers are thin.

Why MongoDB rather than the YAML under config/fleet/: this is the one part of
the stack that holds state rather than configuration. Placements change as
deployments move, cluster credentials rotate, and none of that belongs in a
git-committed, hot-reloaded config tree (FR-CFG-4). The YAML stays as seed
fixtures; `python -m cloudops.registry.seed` loads it, and Mongo is the hot
store from then on - no file watching, because a write to Mongo is already
visible to the next read.

Layout:
- ``db``       connection, index creation, RegistryUnavailable
- ``queries``  the typed accessors that are the registry's contract
- ``seed``     config/fleet/*.yaml -> collections, idempotent
"""

from cloudops.registry.db import RegistryUnavailable, ensure_indexes, get_db, set_database
from cloudops.registry.queries import (
    RegistryQueryError,
    blast_radius,
    find_placements,
    get_app,
    get_cluster,
    list_apps_on_cluster,
    list_clusters,
    list_lobs,
    public_cluster,
    resolve_entity,
)

__all__ = [
    "RegistryQueryError",
    "RegistryUnavailable",
    "blast_radius",
    "ensure_indexes",
    "find_placements",
    "get_app",
    "get_cluster",
    "get_db",
    "list_apps_on_cluster",
    "list_clusters",
    "list_lobs",
    "public_cluster",
    "resolve_entity",
    "set_database",
]
