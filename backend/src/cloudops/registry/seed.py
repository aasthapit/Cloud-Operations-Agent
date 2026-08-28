"""Load config/fleet/*.yaml into the MongoDB fleet registry.

Run it with ``make mongo-seed`` (or ``python -m cloudops.registry.seed``).

Why a seeder rather than a migration: the YAML under config/fleet/ is seed
FIXTURE data, not runtime truth. MongoDB is the hot store - a placement moves
by writing to Mongo, not by editing a file and waiting for a watcher - so the
YAML only has to be able to recreate a believable fleet from nothing. Running
the seeder twice must therefore be indistinguishable from running it once,
which is why every write is an upsert on the collection's natural key:

    clusters    name
    apps        app_id
    placements  (app_id, cluster, namespace)

Deliberately NOT a sync: documents that vanish from the YAML are left alone.
An operator who adds a cluster directly to Mongo (the expected path once this
is deployed) must not have it deleted by the next seed run.

Placements come from each application's `instances`, used as written: since
the mock fleet's removal those rows name the live fleet's clusters directly.
An instance naming a cluster the fleet does not know is skipped with a
warning rather than seeded, because a registry row nothing can verify would
only ever resolve to "stale registry row" answers.

Credentials: cluster `auth` blocks are built here but never printed. The
local fleet is all `{type: "kubeconfig", context: ...}`, which names a
context and holds no secret; token and basic auth arrive by editing Mongo
directly or by putting an explicit `auth` block behind a ${VAR} reference,
never as literal committed secrets (FR-CFG-4).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pymongo.errors import PyMongoError

from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings
from cloudops.registry.db import RegistryUnavailable, get_db

# Application-entry keys that describe placement rather than the application
# itself; they never reach the `apps` collection.
PLACEMENT_KEYS = ("instances",)


def _cluster_documents(fleet: dict[str, Any]) -> list[dict[str, Any]]:
    """The `live:` clusters section of fleet.yaml as cluster records."""
    out = []
    for entry in (fleet.get("live") or {}).get("clusters") or []:
        auth = dict(entry.get("auth") or {})
        if not auth:
            # The local reference fleet is kind: identity comes from the
            # kubeconfig context, so the record names the context and holds
            # no credential of its own.
            auth = {"type": "kubeconfig", "context": str(entry["context"])}
        out.append({
            "name": str(entry["name"]),
            "api_url": entry.get("api_url"),
            "console_url": entry.get("console_url"),
            "environment": entry.get("environment"),
            "region": entry.get("region"),
            "ring": entry.get("ring"),
            "aliases": [str(a) for a in entry.get("aliases") or []],
            "labels": dict(entry.get("labels") or {}),
            "auth": auth,
        })
    return out


def _app_documents(apps_yaml: dict[str, Any]) -> list[dict[str, Any]]:
    """applications.yaml entries as application-registry records."""
    out = []
    for entry in apps_yaml.get("applications") or []:
        doc = {k: v for k, v in entry.items() if k not in PLACEMENT_KEYS}
        doc["app_id"] = str(entry["app_id"])
        doc["application"] = str(entry["application"])
        doc["app_label"] = str(entry.get("app_label") or entry["application"])
        doc["lob"] = entry.get("lob")
        # `tier` is the registry's importance band. The fixtures express that
        # as `criticality`; keep both rather than inventing a third word.
        doc["tier"] = entry.get("tier") or entry.get("criticality")
        doc["description"] = entry.get("description")
        doc["owner_groups"] = [str(g) for g in entry.get("owner_groups") or []]
        out.append(doc)
    return out


def _placement_documents(
    apps_yaml: dict[str, Any], clusters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One placement document per (app, cluster, namespace) permutation."""
    known = {str(c["name"]) for c in clusters}
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in apps_yaml.get("applications") or []:
        base = {
            "app_id": str(entry["app_id"]),
            "application": str(entry["application"]),
            "app_label": str(entry.get("app_label") or entry["application"]),
            "lob": entry.get("lob"),
        }
        for instance in entry.get("instances") or []:
            cluster = str(instance.get("cluster"))
            if cluster not in known:
                print(
                    f"skipping placement {base['app_id']} on {cluster!r}: "
                    "not a cluster the fleet registry knows",
                    file=sys.stderr,
                )
                continue
            key = (base["app_id"], cluster, str(instance.get("namespace")))
            out[key] = {**base, "cluster": key[1], "namespace": key[2],
                        "environment": instance.get("environment")}
    return [out[k] for k in sorted(out)]


def _upsert(collection: Any, documents: list[dict[str, Any]], key_fields: tuple[str, ...]
            ) -> tuple[int, int]:
    """Upsert by natural key. Returns (inserted, updated)."""
    inserted = updated = 0
    for doc in documents:
        key = {k: doc[k] for k in key_fields}
        result = collection.update_one(key, {"$set": doc}, upsert=True)
        if result.upserted_id is not None:
            inserted += 1
        elif result.modified_count:
            updated += 1
    return inserted, updated


def seed(config_dir: Path | None = None) -> dict[str, Any]:
    """Load the fixtures into MongoDB. Returns a per-collection summary."""
    root = config_dir or get_settings().config_dir
    fleet = load_yaml(root / "fleet" / "fleet.yaml") or {}
    apps_yaml = load_yaml(root / "fleet" / "applications.yaml") or {}

    clusters = _cluster_documents(fleet)
    apps = _app_documents(apps_yaml)
    placements = _placement_documents(apps_yaml, clusters)

    db = get_db()
    summary: dict[str, Any] = {}
    for name, documents, key_fields in (
        ("clusters", clusters, ("name",)),
        ("apps", apps, ("app_id",)),
        ("placements", placements, ("app_id", "cluster", "namespace")),
    ):
        inserted, updated = _upsert(db[name], documents, key_fields)
        summary[name] = {
            "seeded": len(documents),
            "inserted": inserted,
            "updated": updated,
            "total": db[name].count_documents({}),
        }
    return summary


def main() -> int:
    settings = get_settings()
    try:
        summary = seed()
    except (RegistryUnavailable, PyMongoError) as exc:
        print(f"registry unavailable: {exc}", file=sys.stderr)
        print("start MongoDB first: make mongo-up", file=sys.stderr)
        return 1
    # The URL may embed credentials, so name the database, never the URL.
    print(f"seeded database {settings.cloudops_mongo_db!r} from {settings.config_dir}")
    for name in ("clusters", "apps", "placements"):
        row = summary[name]
        print(
            f"  {name:<11} {row['seeded']:>4} from fixtures "
            f"({row['inserted']} inserted, {row['updated']} updated), "
            f"{row['total']} in collection"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
