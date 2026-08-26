"""Live observability backend (M3 scope; interface complete, query path real).

Same contract rule as the OpenShift live backend: shapes identical to the
mock World. query_instant is implemented against a Thanos/Prometheus
compatible HTTP API when CLOUDOPS_THANOS_URL is configured; the summary
tools land in M3 (they are PromQL compositions over the same client).
"""

from __future__ import annotations

from typing import Any

import httpx

from cloudops.common.settings import get_settings


class LiveObservabilityBackend:
    def __init__(self) -> None:
        self._settings = get_settings()

    def query_instant(self, promql: str) -> dict[str, Any]:
        url = self._settings.cloudops_thanos_url
        if not url:
            raise RuntimeError(
                "live backend needs CLOUDOPS_THANOS_URL in the environment "
                "(or run with CLOUDOPS_BACKEND_MODE=mock)"
            )
        headers = {}
        if self._settings.cloudops_thanos_token:
            headers["Authorization"] = f"Bearer {self._settings.cloudops_thanos_token}"
        with httpx.Client(base_url=url, headers=headers, timeout=20.0) as client:
            resp = client.get("/api/v1/query", params={"query": promql})
            resp.raise_for_status()
            data = resp.json()
        return {
            "promql": promql,
            "note": "live thanos result",
            "result": data.get("data", {}).get("result", []),
        }

    def __getattr__(self, name: str) -> Any:
        def _not_implemented(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(
                f"live observability backend: {name} lands in phase M3; "
                "run with CLOUDOPS_BACKEND_MODE=mock for the full toolset"
            )

        return _not_implemented
