"""Typed accessors over the fleet registry collections.

These functions ARE the registry's contract. The `reg__*` MCP tools are thin
wrappers around them, and the live fleet reads its cluster records through
them, so there is exactly one implementation of "what does the registry say"
no matter who is asking.

Three collections, one document shape each (declared in
cloudops.registry.db.INDEXES and written by cloudops.registry.seed):

- ``placements``  {app_id, application, app_label, cluster, namespace,
                   environment, lob} - one document per permutation. This is
                   the join table every fleet-shaped question runs through.
- ``clusters``    {name, api_url, console_url, environment, region, ring,
                   aliases[], labels{}, auth{}} - identity plus how to
                   authenticate to it.
- ``apps``        {app_id, application, app_label, owner_groups[], lob, tier,
                   description, ...} - the application registry.

Resolution semantics deliberately mirror
cloudops.mcp_servers.live_fleet.LiveFleet.resolve_cluster: exact, then alias,
then substring, then difflib. The agent prompt and the console are written
against that behaviour, so widening it to apps, namespaces, and LOBs must not
change how it feels. Scores are banded (1.0 exact, 0.9 alias, 0.8 substring,
fuzzy scaled to at most 0.7) so a fuzzy hit can never outrank a real one.

Credential discipline: ``auth`` is stripped from every result these accessors
hand to a tool (``public_cluster``). Only ``get_cluster`` returns it, and its
one caller is the Kubernetes client factory (FR-MCP-7, NFR-LOG-3).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from cloudops.registry.db import get_db

# Fields of a cluster document that are safe to hand to a tool result.
PUBLIC_CLUSTER_FIELDS = (
    "name", "api_url", "console_url", "environment", "region", "ring", "aliases", "labels",
)

# Score bands. Ordered so a fuzzy match can never outrank a substring match,
# and a substring match can never outrank an alias or an exact hit.
SCORE_EXACT = 1.0
SCORE_ALIAS = 0.9
SCORE_SUBSTRING = 0.8
FUZZY_SCALE = 0.7
FUZZY_CUTOFF = 0.6

KIND_ORDER = {"app": 0, "cluster": 1, "namespace": 2, "lob": 3}


class RegistryQueryError(ValueError):
    """The caller asked for something the registry cannot answer as posed."""


def public_cluster(doc: dict[str, Any]) -> dict[str, Any]:
    """A cluster record with credentials removed, for tool results."""
    return {key: doc.get(key) for key in PUBLIC_CLUSTER_FIELDS}


def _strip(cursor: Any) -> list[dict[str, Any]]:
    return [dict(doc) for doc in cursor]


# ---------------------------------------------------------------------------
# clusters
# ---------------------------------------------------------------------------


def list_clusters(
    environment: str | None = None, region: str | None = None, ring: str | None = None
) -> list[dict[str, Any]]:
    """Cluster records (credentials included) matching the given filters."""
    query = {k: v for k, v in
             {"environment": environment, "region": region, "ring": ring}.items() if v is not None}
    return _strip(get_db().clusters.find(query, {"_id": 0}).sort("name", 1))


def get_cluster(name: str) -> dict[str, Any] | None:
    """One cluster record INCLUDING its ``auth`` block, or None.

    The auth block is why this is separate from list_clusters: only the
    Kubernetes client factory may see it, and it must never reach a result.
    """
    doc = get_db().clusters.find_one({"name": name}, {"_id": 0})
    return dict(doc) if doc else None


# ---------------------------------------------------------------------------
# apps and placements
# ---------------------------------------------------------------------------


def get_app(app_id: str) -> dict[str, Any]:
    """The registry entry for an application, with where it is placed.

    Accepts the short app id, the application name, or the pod app label,
    because the agent reaches this tool from all three (a user says "SSOP",
    a report says "payments-api", a pod label says "payments-api").
    """
    apps = get_db().apps
    needle = app_id.strip()
    doc = apps.find_one({"app_id": needle}, {"_id": 0})
    if doc is None:
        lowered = needle.lower()
        for candidate in _strip(apps.find({}, {"_id": 0})):
            keys = {str(candidate.get(k, "")).lower()
                    for k in ("app_id", "application", "app_label")}
            if lowered in keys:
                doc = candidate
                break
    if doc is None:
        return {"app_id": app_id, "found": False}
    placements = find_placements(app_id=str(doc["app_id"]))["placements"]
    return {
        **doc,
        "found": True,
        "placements": placements,
        "clusters": sorted({p["cluster"] for p in placements}),
        "namespaces": sorted({p["namespace"] for p in placements}),
    }


def find_placements(
    app_id: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    environment: str | None = None,
    lob: str | None = None,
) -> dict[str, Any]:
    """Registry placements matching every filter given (AND semantics).

    Filters are case-insensitive exact matches, not fuzzy: callers resolve
    ambiguous text with resolve_entity first, so that a placement query can
    never silently widen (FR-MCP-2).
    """
    query: dict[str, Any] = {}
    for field_name, value in (
        ("app_id", app_id), ("cluster", cluster), ("namespace", namespace),
        ("environment", environment), ("lob", lob),
    ):
        if value is not None and str(value).strip():
            query[field_name] = {"$regex": f"^{_escape(str(value).strip())}$", "$options": "i"}
    rows = _strip(get_db().placements.find(query, {"_id": 0}))
    rows.sort(key=lambda r: (r.get("app_id", ""), r.get("cluster", ""), r.get("namespace", "")))
    return {"count": len(rows), "placements": rows}


def _escape(text: str) -> str:
    """Regex-escape a filter value: registry names may contain dots and dashes."""
    return "".join("\\" + c if c in ".^$*+?()[]{}|\\" else c for c in text)


def list_apps_on_cluster(cluster: str, environment: str | None = None) -> dict[str, Any]:
    """Everything the registry believes runs on one cluster.

    Registry truth only. A caller reporting on it must still verify each
    placement against the cluster API (FR-CTX-2): the registry proposes, the
    cluster confirms.
    """
    rows = find_placements(cluster=cluster, environment=environment)["placements"]
    by_app: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_app.setdefault(row["app_id"], {
            "app_id": row["app_id"],
            "application": row.get("application"),
            "app_label": row.get("app_label"),
            "lob": row.get("lob"),
            "namespaces": [],
            "environments": [],
        })
        for key, value in (("namespaces", row.get("namespace")),
                           ("environments", row.get("environment"))):
            if value and value not in entry[key]:
                entry[key].append(value)
    for entry in by_app.values():
        entry["namespaces"].sort()
        entry["environments"].sort()
    return {
        "cluster": cluster,
        "environment": environment,
        "count": len(by_app),
        "apps": [by_app[k] for k in sorted(by_app)],
        "namespaces": sorted({r["namespace"] for r in rows if r.get("namespace")}),
        "lobs": sorted({r["lob"] for r in rows if r.get("lob")}),
    }


def blast_radius(
    cluster: str | None = None, namespace: str | None = None, lob: str | None = None
) -> dict[str, Any]:
    """What is affected if a cluster, a namespace, or a whole LOB goes away.

    At least one scope dimension is required: "what breaks if nothing breaks"
    is not a question, and answering it with the entire fleet would be a
    confidently wrong answer of exactly the kind FR-MCP-2 forbids.
    """
    scope = {k: v for k, v in
             {"cluster": cluster, "namespace": namespace, "lob": lob}.items() if v}
    if not scope:
        raise RegistryQueryError(
            "blast_radius needs at least one of cluster, namespace, or lob")
    rows = find_placements(cluster=cluster, namespace=namespace, lob=lob)["placements"]
    apps = {
        r["app_id"]: {"app_id": r["app_id"], "application": r.get("application"),
                      "lob": r.get("lob")}
        for r in rows
    }
    clusters = sorted({r["cluster"] for r in rows if r.get("cluster")})
    namespaces = sorted({r["namespace"] for r in rows if r.get("namespace")})
    lobs = sorted({r["lob"] for r in rows if r.get("lob")})
    environments = sorted({r["environment"] for r in rows if r.get("environment")})

    # Enrich apps with the registry facts an operator needs to triage the
    # impact (who owns it, how critical) without a second tool call.
    if apps:
        for doc in _strip(get_db().apps.find({"app_id": {"$in": list(apps)}}, {"_id": 0})):
            apps[str(doc["app_id"])].update({
                "tier": doc.get("tier"),
                "criticality": doc.get("criticality"),
                "owner_groups": list(doc.get("owner_groups") or []),
            })

    scope_text = ", ".join(f"{k}={v}" for k, v in scope.items())
    summary = (
        f"{len(apps)} application(s) across {len(namespaces)} namespace(s) and "
        f"{len(lobs)} line(s) of business are placed in scope {scope_text}"
        if rows else f"the registry has no placements in scope {scope_text}"
    )
    return {
        "scope": scope,
        "placement_count": len(rows),
        "apps": [apps[k] for k in sorted(apps)],
        "clusters": clusters,
        "namespaces": namespaces,
        "lobs": lobs,
        "environments": environments,
        "summary": summary,
    }


def list_lobs() -> dict[str, Any]:
    """Distinct lines of business with their application and cluster counts."""
    rows = _strip(get_db().placements.find({}, {"_id": 0}))
    apps_by_lob: dict[str, set[str]] = {}
    clusters_by_lob: dict[str, set[str]] = {}
    for row in rows:
        lob = row.get("lob")
        if not lob:
            continue
        apps_by_lob.setdefault(lob, set()).add(row["app_id"])
        clusters_by_lob.setdefault(lob, set()).add(row["cluster"])
    # Apps with no placement yet still belong to a LOB; the registry should
    # not hide them just because nothing is deployed.
    for doc in _strip(get_db().apps.find({}, {"_id": 0, "app_id": 1, "lob": 1})):
        if doc.get("lob"):
            apps_by_lob.setdefault(str(doc["lob"]), set()).add(str(doc["app_id"]))
    lobs = [
        {
            "lob": lob,
            "app_count": len(apps_by_lob[lob]),
            "apps": sorted(apps_by_lob[lob]),
            "cluster_count": len(clusters_by_lob.get(lob, set())),
        }
        for lob in sorted(apps_by_lob)
    ]
    return {"count": len(lobs), "lobs": lobs}


# ---------------------------------------------------------------------------
# fuzzy resolution across every kind
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Candidate:
    """One resolvable entity plus the strings that name it."""

    kind: str
    id: str
    detail: dict[str, Any]
    exact: set[str] = field(default_factory=set)
    aliases: set[str] = field(default_factory=set)

    def score(self, query: str) -> float:
        if query in self.exact:
            return SCORE_EXACT
        if query in self.aliases:
            return SCORE_ALIAS
        haystack = self.exact | self.aliases
        if any(query in name for name in haystack):
            return SCORE_SUBSTRING
        close = difflib.get_close_matches(query, sorted(haystack), n=1, cutoff=FUZZY_CUTOFF)
        if close:
            ratio = difflib.SequenceMatcher(None, query, close[0]).ratio()
            return round(ratio * FUZZY_SCALE, 3)
        return 0.0


def _candidates() -> list[_Candidate]:
    db = get_db()
    apps = _strip(db.apps.find({}, {"_id": 0}))
    clusters = _strip(db.clusters.find({}, {"_id": 0}))
    placements = _strip(db.placements.find({}, {"_id": 0}))

    out: list[_Candidate] = []
    for app in apps:
        app_id = str(app["app_id"])
        out.append(_Candidate(
            kind="app", id=app_id,
            detail={"app_id": app_id, "application": app.get("application"),
                    "app_label": app.get("app_label"), "lob": app.get("lob"),
                    "tier": app.get("tier"), "description": app.get("description")},
            exact={app_id.lower()},
            aliases={str(app.get(k)).lower() for k in ("application", "app_label") if app.get(k)},
        ))
    for cluster in clusters:
        name = str(cluster["name"])
        out.append(_Candidate(
            kind="cluster", id=name,
            detail=public_cluster(cluster),
            exact={name.lower()},
            aliases={str(a).lower() for a in cluster.get("aliases") or []},
        ))

    namespaces: dict[str, dict[str, set[str]]] = {}
    lobs: dict[str, set[str]] = {}
    for row in placements:
        namespace = row.get("namespace")
        if namespace:
            entry = namespaces.setdefault(namespace, {"clusters": set(), "apps": set()})
            entry["clusters"].add(row["cluster"])
            entry["apps"].add(row["app_id"])
        if row.get("lob"):
            lobs.setdefault(row["lob"], set()).add(row["app_id"])
    for namespace, entry in namespaces.items():
        out.append(_Candidate(
            kind="namespace", id=namespace,
            detail={"namespace": namespace, "clusters": sorted(entry["clusters"]),
                    "apps": sorted(entry["apps"])},
            exact={namespace.lower()},
        ))
    for lob, app_ids in lobs.items():
        out.append(_Candidate(
            kind="lob", id=lob,
            detail={"lob": lob, "app_count": len(app_ids), "apps": sorted(app_ids)},
            exact={lob.lower()},
        ))
    return out


def resolve_entity(
    query: str, kind_hint: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """Resolve free text to fleet entities across apps, clusters, namespaces, LOBs.

    This is the front door for "is app SSOP down?" style questions: the agent
    resolves the noun first and only then asks a cluster anything. Several
    matches mean the caller must pick or ask the user, never guess (FR-MCP-2),
    which is why every match carries its own score instead of the resolver
    picking a winner.
    """
    needle = query.strip().lower()
    if not needle:
        return {"query": query, "matches": [],
                "suggestion": "empty query; name an application, cluster, namespace, or LOB"}

    matches: list[dict[str, Any]] = []
    for candidate in _candidates():
        if kind_hint and candidate.kind != kind_hint:
            continue
        score = candidate.score(needle)
        if score > 0:
            matches.append({"kind": candidate.kind, "id": candidate.id,
                            "score": score, "detail": candidate.detail})
    matches.sort(key=lambda m: (-float(m["score"]), KIND_ORDER.get(str(m["kind"]), 9), str(m["id"])))
    matches = matches[:limit]

    suggestion = None
    if not matches:
        suggestion = (
            f"nothing in the registry matched {query!r}"
            + (f" as a {kind_hint}" if kind_hint else "")
            + "; try list_lobs, or find_placements with a cluster or environment filter"
        )
    elif len({m["kind"] for m in matches}) > 1 or len(matches) > 1:
        suggestion = "several candidates matched; confirm which one before querying a cluster"
    return {"query": query, "kind_hint": kind_hint, "count": len(matches),
            "matches": matches, "suggestion": suggestion}
