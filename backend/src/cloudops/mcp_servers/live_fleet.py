"""Live-mode fleet registry: cluster records -> ready-made KubeClients.

Role: the one place the live backends ask "which clusters exist, and how do I
reach this one". Both live backends share it, so the OpenShift and
observability servers always agree about the fleet.

Where the records come from: MongoDB, through cloudops.registry. That is the
live-cutover change (docs/design/LIVE-CUTOVER.md, "OpenShift MCP changes").
The `live:` section of config/fleet/fleet.yaml is now only the SEED for those
records - `make mongo-seed` loads it once and Mongo is the runtime truth from
then on.

Why there is no watcher here any more. The yaml path re-read the file on
every access so an edit landed without a restart (FR-CFG-3). Mongo needs no
equivalent: every accessor queries it fresh, so a record written by an
operator - a rotated password, a new cluster - is visible to the very next
call. Hot behaviour is preserved; the mechanism is just a query instead of a
file read.

Credentials: a cluster record carries an `auth` block. It reaches exactly one
place, the KubeClient constructor, and is stripped from everything this class
returns (see `summary`), so no tool result can carry one (FR-MCP-7).

Resolver semantics deliberately mirror cloudops.mockfleet.World.resolve_cluster
(exact name or alias, then key=value label selector, then substring, then
fuzzy) because the agent prompt and the console are written against that
behaviour, not against a backend.
"""

from __future__ import annotations

import difflib
from typing import Any

from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings
from cloudops.mcp_servers.kube import KubeClient
from cloudops.registry import list_clusters as registry_clusters


class LiveFleet:
    """The live cluster registry plus a per-cluster client cache."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._clients: dict[str, KubeClient] = {}
        self._versions: dict[str, str | None] = {}

    # -- registry ------------------------------------------------------------

    def _registry(self) -> dict[str, dict[str, Any]]:
        """Cluster records by name, queried fresh so registry writes land
        without a restart (the Mongo equivalent of FR-CFG-3's hot reload)."""
        return {str(doc["name"]): dict(doc) for doc in registry_clusters()}

    def names(self) -> list[str]:
        return sorted(self._registry())

    def entry(self, cluster: str) -> dict[str, Any]:
        entry = self._registry().get(cluster)
        if entry is None:
            raise ValueError(
                f"unknown cluster: {cluster!r}; use resolve_cluster first "
                "(clusters live in the MongoDB registry; seed it with `make mongo-seed`)"
            )
        return entry

    def context(self, cluster: str) -> str:
        """The kubeconfig context a cluster is reached through.

        Only meaningful for kubeconfig-auth records; token and basic records
        have no context, and callers should use `client` instead.
        """
        auth = self.entry(cluster).get("auth") or {}
        context = auth.get("context")
        if not context:
            raise ValueError(
                f"cluster {cluster!r} does not authenticate through a kubeconfig context "
                f"(auth type {auth.get('type', 'unset')!r})"
            )
        return str(context)

    def client(self, cluster: str) -> KubeClient:
        """A KubeClient for this cluster, built from its `auth` block.

        Cached by cluster name rather than by context, because token and
        basic records have no context to key on and two clusters can share
        one anyway.
        """
        if cluster not in self._clients:
            self._clients[cluster] = KubeClient(self.entry(cluster))
        return self._clients[cluster]

    # -- resolution ----------------------------------------------------------

    def version(self, cluster: str) -> str | None:
        """Server gitVersion, cached: a cluster's Kubernetes version does not
        change under a running process, and resolve_cluster would otherwise
        hit every candidate's API on every keystroke."""
        if cluster not in self._versions:
            try:
                self._versions[cluster] = str(self.client(cluster).get("/version").get("gitVersion"))
            except Exception:  # noqa: BLE001 - an unreachable cluster still resolves
                self._versions[cluster] = None
        return self._versions[cluster]

    def summary(self, cluster: str) -> dict[str, Any]:
        entry = self.entry(cluster)
        return {
            "name": cluster,
            "environment": entry.get("environment"),
            "region": entry.get("region"),
            "ring": entry.get("ring"),
            "version": self.version(cluster),
            "aliases": list(entry.get("aliases") or []),
            "labels": dict(entry.get("labels") or {}),
        }

    def resolve_cluster(self, query: str) -> dict[str, Any]:
        registry = self._registry()
        alias_index = {name.lower(): name for name in registry}
        for name, entry in registry.items():
            for alias in entry.get("aliases") or []:
                alias_index[str(alias).lower()] = name

        q = query.strip().lower()
        if q in alias_index:
            matches = [alias_index[q]]
        elif "=" in q:
            key, _, val = q.partition("=")
            key, val = key.strip(), val.strip()
            matches = [
                name for name, e in registry.items()
                if str((e.get("labels") or {}).get(key, "")).lower() == val
                or str(e.get(key, "")).lower() == val
            ]
        else:
            matches = [name for name in registry if q in name.lower()]
            if not matches:
                matches = difflib.get_close_matches(q, list(registry), n=5, cutoff=0.6)
        matches = sorted(matches)[:10]
        return {
            "query": query,
            "count": len(matches),
            "matches": [self.summary(m) for m in matches],
            "suggestion": None if matches else
            "no cluster matched; try list_clusters with an environment or region filter",
        }

    def list_clusters(
        self, environment: str | None = None, region: str | None = None,
        page: int = 1, page_size: int = 50,
    ) -> dict[str, Any]:
        registry = self._registry()
        names = [
            n for n, e in sorted(registry.items())
            if (environment is None or e.get("environment") == environment)
            and (region is None or e.get("region") == region)
        ]
        start = max(page - 1, 0) * page_size
        return {
            "total": len(names),
            "page": page,
            "page_size": page_size,
            "clusters": [self.summary(n) for n in names[start : start + page_size]],
        }

    # -- application registry ------------------------------------------------

    def applications(self) -> dict[str, dict[str, Any]]:
        apps = load_yaml(self._settings.config_dir / "fleet" / "applications.yaml") or {}
        return {str(a["application"]): dict(a) for a in apps.get("applications") or []}

    def app_by_label(self, app_label: str) -> tuple[str, dict[str, Any]] | None:
        for name, entry in self.applications().items():
            if entry.get("app_label") == app_label:
                return name, entry
        return None

    def get_app_registry_entry(self, application: str) -> dict[str, Any]:
        entry = self.applications().get(application)
        if entry is None:
            return {"application": application, "found": False}
        # `instances` and `live_placements` are registry SEED hints; live
        # placement is always discovered from the cluster (FR-CTX-2) or asked
        # of the registry service, never read from here.
        out: dict[str, Any] = {
            k: v for k, v in entry.items() if k not in ("instances", "live_placements")
        }
        out["found"] = True
        return out
