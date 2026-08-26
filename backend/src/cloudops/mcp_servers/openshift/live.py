"""Live OpenShift backend (M3 scope; interface complete, one happy path).

Design contract: method names and RESULT SHAPES are identical to
cloudops.mockfleet.World (decision D6). The check batteries and the agent
cannot tell which backend answered.

MVP status (PRD section 15, in-scope item 5): the interface exists with a
config-guarded single-cluster happy path (get_cluster_info /
get_cluster_version against a real API server using CLOUDOPS_OCP_TOKEN).
Every other method raises a clear error naming the phase that will
implement it, so a misconfigured live deployment fails loudly instead of
answering wrongly.

Credentials come from the environment only (FR-MCP-7): CLOUDOPS_OCP_TOKEN
for bearer auth; cluster API URLs come from config/fleet/fleet.yaml.
"""

from __future__ import annotations

from typing import Any

import httpx

from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings


class LiveOpenShiftBackend:
    """Real-cluster backend. Read-only by construction (NFR-SEC-1): only GET
    requests are ever issued."""

    def __init__(self) -> None:
        self._settings = get_settings()

    def _api_url(self, cluster: str) -> str:
        fleet = load_yaml(self._settings.config_dir / "fleet" / "fleet.yaml")
        for c in fleet.get("clusters", []):
            if c["name"] == cluster:
                return str(c["api_url"])
        raise ValueError(f"cluster {cluster!r} has no api_url in fleet.yaml")

    def _get(self, cluster: str, path: str) -> dict[str, Any]:
        token = self._settings.cloudops_ocp_token
        if not token:
            raise RuntimeError(
                "live backend needs CLOUDOPS_OCP_TOKEN in the environment "
                "(or run with CLOUDOPS_BACKEND_MODE=mock)"
            )
        with httpx.Client(
            base_url=self._api_url(cluster),
            headers={"Authorization": f"Bearer {token}"},
            verify=True,
            timeout=15.0,
        ) as client:
            resp = client.get(path)
            resp.raise_for_status()
            return resp.json()

    # -- implemented happy path ---------------------------------------------

    def get_cluster_info(self, cluster: str) -> dict[str, Any]:
        version = self._get(cluster, "/apis/config.openshift.io/v1/clusterversions/version")
        history = version.get("status", {}).get("history", [])
        return {
            "cluster": cluster,
            "reachable": True,
            "latency_ms": None,
            "version": history[0].get("version") if history else None,
            "channel": version.get("spec", {}).get("channel"),
            "api_url": self._api_url(cluster),
            "console_url": None,
            "region": None,
            "environment": None,
            "labels": {},
        }

    def get_cluster_version(self, cluster: str) -> dict[str, Any]:
        cv = self._get(cluster, "/apis/config.openshift.io/v1/clusterversions/version")
        conditions = {c["type"]: c for c in cv.get("status", {}).get("conditions", [])}

        def cond(name: str) -> bool:
            return conditions.get(name, {}).get("status") == "True"

        history = cv.get("status", {}).get("history", [])
        return {
            "cluster": cluster,
            "version": history[0].get("version") if history else None,
            "desired_version": cv.get("status", {}).get("desired", {}).get("version"),
            "channel": cv.get("spec", {}).get("channel"),
            "available": cond("Available"),
            "progressing": cond("Progressing"),
            "failing": cond("Failing"),
            "progressing_message": conditions.get("Progressing", {}).get("message"),
            "history": [
                {"version": h.get("version"), "state": h.get("state"),
                 "completed_at": h.get("completionTime")}
                for h in history[:5]
            ],
        }

    # -- M3 stubs ------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        def _not_implemented(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                f"live OpenShift backend: {name} lands in phase M3; "
                "run with CLOUDOPS_BACKEND_MODE=mock for the full toolset"
            )

        return _not_implemented
