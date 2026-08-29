"""Fleet registry doubles: one in-memory Mongo, one hand-rolled contract.

Two of them, because they answer different questions:

- ``seeded_registry`` installs a mongomock database loaded by the REAL seeder
  from a config plane, so everything above the driver - queries, the registry
  MCP server, LiveFleet's cluster resolution - runs its production code over
  in-memory data. This is the preferred double: nothing about the registry is
  reimplemented, so nothing can drift.
- ``FakeRegistry`` reimplements the ``reg__*`` result SHAPES over
  applications.yaml, with no Mongo at all. It stays for the unit tests that
  only need a gateway-shaped answer without standing anything up, and for
  ``build_registry_server``, which serves that contract on a port.

Preferring the first in end-to-end work and the second in unit work is the
whole rule: fidelity where a contract is being pinned, speed where it is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings

REGISTRY_TOOLS = (
    "resolve_entity", "find_placements", "list_apps_on_cluster",
    "blast_radius", "get_app", "list_lobs",
)


@contextmanager
def seeded_registry(config_dir: Path | None = None, name: str = "cloudops_test") -> Iterator[Any]:
    """A mongomock database seeded from ``config_dir``, installed globally.

    mongomock rather than a container so callers stay hermetic (NFR-QE-1), and
    the real seeder rather than hand-written documents so the fixtures and the
    production write path cannot drift: everything served from here is data
    ``make mongo-seed`` would have produced from the same config plane.
    """
    import mongomock  # dev-only dependency; imported where it is used

    from cloudops.registry.db import set_database
    from cloudops.registry.seed import seed

    client: Any = mongomock.MongoClient()
    db = client[name]
    set_database(db)
    try:
        seed(config_dir or get_settings().config_dir)
        yield db
    finally:
        set_database(None)


class FakeRegistry:
    """The reg__* contract served straight from applications.yaml.

    What a caller depends on is the SHAPE of the registry's answers, so this
    serves exactly that shape from config/fleet/applications.yaml, which is
    the same seed the real registry is loaded from.
    """

    def __init__(self, config_dir: Path | None = None) -> None:
        root = config_dir or get_settings().config_dir
        apps = load_yaml(root / "fleet" / "applications.yaml")["applications"]
        self.apps = {str(a["application"]): a for a in apps}
        self.placements = [
            {
                "app_id": str(a["application"]), "application": str(a["application"]),
                "app_label": str(a.get("app_label", a["application"])),
                "cluster": i["cluster"], "namespace": i["namespace"],
                "environment": i["environment"],
                "lob": str(a.get("owner_groups", ["unknown"])[0]),
            }
            for a in apps for i in a.get("instances", [])
        ]

    def find_placements(self, **filters: Any) -> dict[str, Any]:
        app_id = filters.get("app_id")
        rows = [
            p for p in self.placements
            if (app_id is None or app_id in (p["app_id"], p["app_label"]))
            and all(filters.get(k) in (None, p[k])
                    for k in ("cluster", "namespace", "environment", "lob"))
        ]
        return {"count": len(rows), "placements": rows}

    def resolve_entity(self, query: str, kind_hint: str | None = None) -> dict[str, Any]:
        q = query.strip().lower()
        matches = [
            {"kind": "app", "id": p["app_id"], "score": 1.0 if q == p["app_id"].lower() else 0.6,
             "detail": {"app_label": p["app_label"], "lob": p["lob"]}}
            for p in {p["app_id"]: p for p in self.placements}.values()
            if q in p["app_id"].lower()
        ]
        if kind_hint:
            matches = [m for m in matches if m["kind"] == kind_hint]
        return {"query": query, "matches": matches,
                "suggestion": None if matches else "no fleet entity matched"}

    def list_apps_on_cluster(self, cluster: str, environment: str | None = None) -> dict[str, Any]:
        rows = self.find_placements(cluster=cluster, environment=environment)["placements"]
        return {"cluster": cluster, "apps": sorted({p["app_id"] for p in rows}),
                "namespaces": sorted({p["namespace"] for p in rows}),
                "lobs": sorted({p["lob"] for p in rows})}

    def blast_radius(self, **scope: Any) -> dict[str, Any]:
        rows = self.find_placements(**scope)["placements"]
        apps = sorted({p["app_id"] for p in rows})
        return {
            "scope": {k: v for k, v in scope.items() if v is not None},
            "apps": apps, "namespaces": sorted({p["namespace"] for p in rows}),
            "lobs": sorted({p["lob"] for p in rows}),
            "environments": sorted({p["environment"] for p in rows}),
            "summary": f"{len(apps)} application(s) affected",
        }

    def get_app(self, app_id: str) -> dict[str, Any]:
        entry = self.apps.get(app_id)
        return {"app_id": app_id, "found": entry is not None,
                **({k: v for k, v in entry.items() if k != "instances"} if entry else {})}

    def list_lobs(self) -> dict[str, Any]:
        counts: dict[str, set[str]] = {}
        for p in self.placements:
            counts.setdefault(p["lob"], set()).add(p["app_id"])
        return {"lobs": [{"lob": k, "app_count": len(v)} for k, v in sorted(counts.items())]}


def build_registry_server(port: int, registry: FakeRegistry | None = None) -> Any:
    """A FastMCP server serving the reg__* contract from FakeRegistry.

    Stood up on a kernel-assigned port and registered in a copy of the real
    config plane, this is registered the same way the production registry MCP
    is; only the store behind the tools differs.
    """
    from mcp.server.fastmcp import FastMCP

    reg = registry or FakeRegistry()
    mcp = FastMCP("registry-mcp", instructions="Fleet registry lookups.",
                  host="127.0.0.1", port=port, streamable_http_path="/mcp",
                  stateless_http=True)

    @mcp.tool()
    def resolve_entity(query: str, kind_hint: str | None = None) -> dict[str, Any]:
        """Resolve free text to fleet entities (app, cluster, namespace, lob)."""
        return reg.resolve_entity(query, kind_hint)

    @mcp.tool()
    def find_placements(
        app_id: str | None = None, cluster: str | None = None,
        namespace: str | None = None, environment: str | None = None,
        lob: str | None = None,
    ) -> dict[str, Any]:
        """Registry placement candidates matching any subset of filters."""
        return reg.find_placements(app_id=app_id, cluster=cluster, namespace=namespace,
                                   environment=environment, lob=lob)

    @mcp.tool()
    def list_apps_on_cluster(cluster: str, environment: str | None = None) -> dict[str, Any]:
        """Applications, namespaces and LOBs registered on one cluster."""
        return reg.list_apps_on_cluster(cluster, environment)

    @mcp.tool()
    def blast_radius(
        cluster: str | None = None, namespace: str | None = None, lob: str | None = None,
    ) -> dict[str, Any]:
        """What is affected if this scope goes down."""
        return reg.blast_radius(cluster=cluster, namespace=namespace, lob=lob)

    @mcp.tool()
    def get_app(app_id: str) -> dict[str, Any]:
        """The registry entry for one application."""
        return reg.get_app(app_id)

    @mcp.tool()
    def list_lobs() -> dict[str, Any]:
        """Distinct lines of business with application counts."""
        return reg.list_lobs()

    return mcp
