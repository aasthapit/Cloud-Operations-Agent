"""Live-mode fleet registry: fleet cluster name -> kubeconfig context.

Role: the one place that reads the `live:` section of config/fleet/fleet.yaml
and hands out ready-made KubeClients. Both live backends share it, so the
OpenShift and observability servers always agree about which clusters exist
and which context each one is reached through.

Seam with mock mode: the `live:` section is invisible to the mock World
builder, and this module is only constructed by the live backends, so nothing
here can move a mock-mode result.

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


class LiveFleet:
    """The live cluster registry plus a per-cluster client cache."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._clients: dict[str, KubeClient] = {}
        self._versions: dict[str, str | None] = {}

    # -- registry ------------------------------------------------------------

    def _registry(self) -> dict[str, dict[str, Any]]:
        """Re-read on every access so a fleet.yaml edit lands without a restart,
        matching the mock world's hot-reload behaviour (FR-CFG-3)."""
        fleet = load_yaml(self._settings.config_dir / "fleet" / "fleet.yaml") or {}
        entries = (fleet.get("live") or {}).get("clusters") or []
        return {str(e["name"]): dict(e) for e in entries}

    def names(self) -> list[str]:
        return sorted(self._registry())

    def entry(self, cluster: str) -> dict[str, Any]:
        entry = self._registry().get(cluster)
        if entry is None:
            raise ValueError(
                f"unknown cluster: {cluster!r}; use resolve_cluster first "
                "(live mode reads the `live:` section of fleet.yaml)"
            )
        return entry

    def context(self, cluster: str) -> str:
        return str(self.entry(cluster)["context"])

    def client(self, cluster: str) -> KubeClient:
        context = self.context(cluster)
        if context not in self._clients:
            self._clients[context] = KubeClient(context)
        return self._clients[context]

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
        # `instances` is a mock-world placement hint; live placement is always
        # discovered from kube_pod_labels (FR-CTX-2), never read from here.
        out: dict[str, Any] = {k: v for k, v in entry.items() if k != "instances"}
        out["found"] = True
        return out
