"""Observability MCP server.

Tool surface (FR-MCP-4): PromQL access, fleet-wide app placement discovery,
firing alerts (Watchdog visible so the agent can attest the monitoring
pipeline itself), golden-signal and usage summaries, Grafana links.

Fleet convention: every result and query is scoped by the `cluster`
external label (the ACM/Thanos pattern from the research notes). Mock mode
answers from the shared synthetic World; live mode queries a Thanos
endpoint (M3).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from cloudops.common.settings import get_settings
from cloudops.mcp_servers.observability.live import LiveObservabilityBackend
from cloudops.mcp_servers.shared import WorldHolder, instrumented

SERVICE = "cloudops.mcp.observability"


def build_server(holder: WorldHolder) -> FastMCP:
    settings = get_settings()
    mcp = FastMCP(
        "observability-mcp",
        instructions=(
            "Fleet observability: Prometheus/Thanos summaries scoped by the "
            "`cluster` label. Prefer the purpose-built summary tools over raw "
            "PromQL."
        ),
        host="127.0.0.1",
        port=settings.cloudops_mcp_observability_port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    live = LiveObservabilityBackend()

    def backend() -> Any:
        return holder.world if settings.cloudops_backend_mode == "mock" else live

    span = instrumented(SERVICE)

    @mcp.tool()
    @span
    def find_app_placements(
        app_label: str = Field(description="app.kubernetes.io/name label value to locate fleet-wide"),
    ) -> dict[str, Any]:
        """Where an application actually runs: (cluster, namespace,
        environment, pod_count) discovered from kube_pod_labels series
        across the whole fleet. This is the source of truth for placement
        (FR-CTX-2), never the registry."""
        return backend().find_app_placements(app_label)

    @mcp.tool()
    @span
    def get_firing_alerts(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str | None = Field(default=None, description="Optional namespace filter"),
    ) -> dict[str, Any]:
        """Firing alerts by severity, plus the two monitoring-trust signals:
        watchdog_present (dead man's switch) and monitoring_healthy. An
        absent Watchdog means the cluster is unattestable, not healthy."""
        return backend().get_firing_alerts(cluster, namespace)

    @mcp.tool()
    @span
    def get_etcd_health(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """etcd leadership, quorum, member count, and WAL fsync p99."""
        return backend().get_etcd_health(cluster)

    @mcp.tool()
    @span
    def get_apiserver_slo(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """API server 5xx error rate and error-budget burn."""
        return backend().get_apiserver_slo(cluster)

    @mcp.tool()
    @span
    def get_capacity_summary(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """Cluster capacity: requests vs allocatable (minus-one-node guard),
        pod count vs capacity."""
        return backend().get_capacity_summary(cluster)

    @mcp.tool()
    @span
    def get_golden_signals(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """Golden signals for one workload: request rate, error rate, p95
        latency vs its SLO target."""
        return backend().get_golden_signals(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_workload_usage(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """Resource posture for one workload: memory p95 vs limit, CPU
        throttling ratio, HPA state, PDB disruption budget."""
        return backend().get_workload_usage(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def query_instant(
        promql: str = Field(description="Instant PromQL query; always scope with {cluster=..., namespace=...}"),
    ) -> dict[str, Any]:
        """Escape hatch for ad-hoc investigation. Mock mode recognizes a small
        set of metric families and says so in `note`; live mode proxies to
        Thanos verbatim."""
        return backend().query_instant(promql)

    @mcp.tool()
    @span
    def get_dashboard_links(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
    ) -> dict[str, Any]:
        """Grafana deep links for the cluster and namespace."""
        return backend().get_dashboard_links(cluster, namespace)

    return mcp
