"""OpenShift MCP server.

Tool surface (FR-MCP-1..3): fleet resolver plus read-only cluster and
namespace state. Every cluster-scoped tool takes an explicit, resolved
cluster name and errors on unknown clusters instead of guessing (FR-MCP-2).

One backend: LiveOpenShiftBackend, talking to real cluster APIs. Result
shapes are the contract the check batteries in config/checks/*.yaml address
by dotted path.

Test seam: build_server accepts a pre-built backend. Tests hand it a
LiveOpenShiftBackend wired to a fake fleet whose KubeClients answer from
canned Kubernetes payloads, so the E2E harness boots this very server
without touching a cluster.

Secrets discipline: get_configuration returns Secret NAMES only, never data
(NFR-LOG-3).
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from cloudops.common.settings import get_settings
from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend
from cloudops.mcp_servers.shared import instrumented

SERVICE = "cloudops.mcp.openshift"


def build_server(backend: LiveOpenShiftBackend | None = None) -> FastMCP:
    settings = get_settings()
    mcp = FastMCP(
        "openshift-mcp",
        instructions=(
            "Read-only OpenShift fleet state. Always resolve a cluster with "
            "resolve_cluster before calling cluster-scoped tools."
        ),
        host="127.0.0.1",
        port=settings.cloudops_mcp_openshift_port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    ocp = backend or LiveOpenShiftBackend()
    span = instrumented(SERVICE)

    # -- fleet resolution ---------------------------------------------------

    @mcp.tool()
    @span
    def resolve_cluster(
        query: str = Field(description="Cluster name, alias, label selector (key=value), or fuzzy text"),
    ) -> dict[str, Any]:
        """Resolve a query to cluster identities across the fleet of hundreds.
        Returns exact matches first; several candidates mean the caller must
        pick or ask, never guess."""
        return ocp.resolve_cluster(query)

    @mcp.tool()
    @span
    def list_clusters(
        environment: str | None = Field(default=None, description="Filter: prod | nonprod"),
        region: str | None = Field(default=None, description="Filter: e.g. us-east"),
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Page through the fleet registry, optionally filtered."""
        return ocp.list_clusters(environment, region, page, page_size)

    # -- cluster state ------------------------------------------------------

    @mcp.tool()
    @span
    def get_cluster_info(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """Cluster identity, reachability, and endpoints."""
        return ocp.get_cluster_info(cluster)

    @mcp.tool()
    @span
    def get_cluster_version(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """ClusterVersion conditions (Available/Progressing/Failing) and update history."""
        return ocp.get_cluster_version(cluster)

    @mcp.tool()
    @span
    def get_cluster_operators(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """ClusterOperator health, pre-summarized: degraded, critical_degraded,
        progressing, unavailable."""
        return ocp.get_cluster_operators(cluster)

    @mcp.tool()
    @span
    def get_nodes(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """Node readiness, pressure conditions, and cordoned (maintenance) nodes."""
        return ocp.get_nodes(cluster)

    @mcp.tool()
    @span
    def get_machine_config_pools(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """MachineConfigPool rollout state: the OCP signal that nodes are rolling."""
        return ocp.get_machine_config_pools(cluster)

    @mcp.tool()
    @span
    def get_pending_csrs(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """Pending certificate signing requests."""
        return ocp.get_pending_csrs(cluster)

    # -- namespace / application state --------------------------------------

    @mcp.tool()
    @span
    def get_workloads(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """Workloads, replicas, rollout state, and pod pathology (crashloops,
        image pull errors, OOM kills, restart churn, probes) for one app."""
        return ocp.get_workloads(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_events(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
    ) -> dict[str, Any]:
        """Recent Warning events in a namespace (the 'why' after a metric check fails)."""
        return ocp.get_events(cluster, namespace)

    @mcp.tool()
    @span
    def get_quotas(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
    ) -> dict[str, Any]:
        """ResourceQuota usage and LimitRanges."""
        return ocp.get_quotas(cluster, namespace)

    @mcp.tool()
    @span
    def get_network(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """Services, routes with TLS termination and cert expiry, NetworkPolicies, DNS."""
        return ocp.get_network(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_pvcs(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """PersistentVolumeClaim binding, capacity, and growth."""
        return ocp.get_pvcs(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_configuration(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """ConfigMap and Secret REFERENCES for an app. Secret names and
        metadata only, never secret data."""
        return ocp.get_configuration(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_security_posture(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """ServiceAccount, SCC/PSA compliance, exposure level, TLS status."""
        return ocp.get_security_posture(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_app_registry_entry(
        application: str = Field(description="Application name in the org registry"),
    ) -> dict[str, Any]:
        """The application registry entry: owners, criticality, SLA/SLO,
        runbooks, escalation, dependencies, backup policy."""
        return ocp.get_app_registry_entry(application)

    @mcp.tool()
    @span
    def get_namespaces(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """Namespace inventory for a cluster: what exists to look inside."""
        return ocp.get_namespaces(cluster)

    @mcp.tool()
    @span
    def get_capacity(cluster: str = Field(description="Resolved cluster name")) -> dict[str, Any]:
        """Pod resource requests versus node allocatable across the cluster,
        computed from the Kubernetes API (no metrics pipeline involved)."""
        return ocp.get_capacity(cluster)

    @mcp.tool()
    @span
    def verify_placement(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """Confirm from the cluster itself that an application actually runs in
        this namespace. The registry proposes placements; this verifies them.
        An unreachable cluster returns reachable=false rather than erroring."""
        return ocp.verify_placement(cluster, namespace, app_label)

    @mcp.tool()
    @span
    def get_autoscaling(
        cluster: str = Field(description="Resolved cluster name"),
        namespace: str = Field(description="Namespace"),
        app_label: str = Field(description="app.kubernetes.io/name label value"),
    ) -> dict[str, Any]:
        """HorizontalPodAutoscalers and PodDisruptionBudgets in a namespace:
        min/max/current replicas and whether the HPA is pinned at max, plus
        allowed disruptions and expected pods per budget."""
        return ocp.get_autoscaling(cluster, namespace, app_label)

    return mcp
