"""The fleet double and the canned cluster world every scenario starts from.

``FakeFleet`` is a real ``LiveFleet`` - it resolves cluster names, aliases and
environments through the MongoDB registry exactly as production does - that
hands out a ``FakeKube`` per cluster instead of an HTTPS client. So a caller
exercises the real resolution path and the real OpenShift backend, and only
the cluster's socket is canned.

``default_world`` is the shared canned fleet: one healthy spoke, one degraded
spoke, one cordoned spoke, one unreachable cluster, and two quiet ones. It is
deliberately small - it exists to reach every verdict the attestation battery
can produce, not to model an organization.
"""

from __future__ import annotations

from cloudops.mcp_servers.kube import KubeClient
from cloudops.mcp_servers.live_fleet import LiveFleet
from cloudops.testkit.kube import (
    ClusterFixture,
    deployment,
    event,
    fake_kube,
    hpa,
    node,
    pdb,
    pod,
    quota,
    replicaset,
)

HEALTHY = "acm-spoke-1a"
DEGRADED = "acm-spoke-2a"
MAINTENANCE = "acm-spoke-1b"
UNREACHABLE = "acm-hub-2"
APP_NS = "payments-prod"
APP_LABEL = "payments-api"


class FakeFleet(LiveFleet):
    """LiveFleet over the seeded cluster registry, handing out FakeKube clients."""

    def __init__(self, clusters: dict[str, ClusterFixture]) -> None:
        super().__init__()
        self._fixtures = clusters
        self._fakes: dict[str, KubeClient] = {}

    def fixture(self, cluster: str) -> ClusterFixture:
        return self._fixtures.setdefault(cluster, ClusterFixture())

    def client(self, cluster: str) -> KubeClient:
        self.entry(cluster)  # keep unknown-cluster behaviour honest
        if cluster not in self._fakes:
            self._fakes[cluster] = fake_kube(self.fixture(cluster))
        return self._fakes[cluster]

    def version(self, cluster: str) -> str | None:
        return self.fixture(cluster).version if self.fixture(cluster).reachable else None


def default_world() -> dict[str, ClusterFixture]:
    """One healthy spoke, one degraded spoke, one unreachable cluster.

    Enough to reach every cluster verdict the battery can produce: healthy,
    degraded (a NotReady node), maintenance (a cordoned node) and unattestable
    (an API server that does not answer).
    """
    healthy = ClusterFixture(nodes=[node("cp-1"), node("worker-1", role="worker")])
    payments = healthy.ns(APP_NS)
    payments.pods = [pod("payments-api-1", APP_LABEL, configmaps=("payments-api-config",),
                         secrets=("payments-db",)),
                     pod("payments-api-2", APP_LABEL)]
    payments.deployments = [deployment("payments-api", APP_LABEL)]
    payments.replicasets = [replicaset("payments-api-abc", APP_LABEL)]
    payments.resourcequotas = [quota("compute", "cpu", "1", "10")]
    payments.horizontalpodautoscalers = [hpa("payments-api", APP_LABEL, "payments-api")]
    payments.poddisruptionbudgets = [pdb("payments-api", APP_LABEL)]
    # The retail applications the registry also claims here, so a resolution
    # test can exercise an app other than payments-api end to end.
    for app, ns_name in (("catalog", "catalog-prod"), ("checkout", "checkout-prod")):
        retail = healthy.ns(ns_name)
        retail.pods = [pod(f"{app}-1", app)]
        retail.deployments = [deployment(app, app, replicas=1, ready=1)]
    healthy.ns("kube-system")
    healthy.ns("default")

    degraded = ClusterFixture(
        nodes=[node("cp-1"), node("worker-1", role="worker", ready=False)])
    broken = degraded.ns(APP_NS)
    broken.pods = [pod("payments-api-1", APP_LABEL, ready=False, crashloop=True),
                   pod("payments-api-2", APP_LABEL, phase="Pending", ready=False)]
    broken.deployments = [deployment("payments-api", APP_LABEL, ready=0)]
    broken.replicasets = [replicaset("payments-api-abc", APP_LABEL, ready=0)]
    broken.events = [event("BackOff", "payments-api-1", count=12,
                           message="Back-off restarting failed container app")]
    # HPA pinned at max, PDB with nothing to give: the two autoscaling findings
    # the App 360 battery still asserts now that metrics are gone.
    broken.horizontalpodautoscalers = [
        hpa("payments-api", APP_LABEL, "payments-api", current=6, desired=8)]
    broken.poddisruptionbudgets = [pdb("payments-api", APP_LABEL, allowed=0)]
    degraded.ns("kube-system")

    maintenance = ClusterFixture(
        nodes=[node("cp-1"), node("worker-1", role="worker", unschedulable=True)])
    maintenance.ns("logistics-dev").pods = [pod("inventory-sync-1", "inventory-sync")]
    maintenance.ns("logistics-dev").deployments = [
        deployment("inventory-sync", "inventory-sync", replicas=1, ready=1)]

    hub = ClusterFixture()
    audit = hub.ns("audit-prod")
    audit.pods = [pod("audit-log-1", "audit-log")]
    audit.deployments = [deployment("audit-log", "audit-log", replicas=1, ready=1)]

    return {
        HEALTHY: healthy,
        DEGRADED: degraded,
        MAINTENANCE: maintenance,
        "acm-spoke-2b": ClusterFixture(),
        "acm-hub-1": hub,
        UNREACHABLE: ClusterFixture(reachable=False),
    }
