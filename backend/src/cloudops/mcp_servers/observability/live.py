"""Live observability backend: each cluster's own in-cluster Prometheus.

Same contract rule as the live OpenShift backend: result SHAPES are identical
to cloudops.mockfleet.World (decision D6), so the check batteries address the
same dotted paths whichever backend answered.

Transport: every query goes through the Kubernetes API server's service proxy
(KubeClient.prom), so live mode needs no port-forward, no NodePort, and no
second credential. Fleet-wide questions fan out across the registry's clusters
rather than hitting one aggregation layer; swapping in a real Thanos later is
a change of transport, not of shape.

Honesty rule: the reference fleet runs a deliberately light monitoring stack.
Where a metric family is not collected the value is None and a
``*_available`` flag says so. Nothing is ever filled in with a plausible
number, because a fabricated reading is worse than a missing one in an
attestation.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from cloudops.mcp_servers.kube import KubeClient
from cloudops.mcp_servers.live_fleet import LiveFleet

_CLUSTER_LABEL = re.compile(r'cluster\s*=\s*"([^"]+)"')


def _scalar(result: list[dict[str, Any]]) -> float | None:
    """First sample of an instant-vector result as a float, or None when the
    query matched nothing (which is the honest answer, not zero)."""
    if not result:
        return None
    try:
        return float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return round(numerator / float(denominator), 4)


class LiveObservabilityBackend:
    """Prometheus-backed observability for the live fleet."""

    def __init__(self, fleet: LiveFleet | None = None) -> None:
        self._fleet = fleet or LiveFleet()

    def _client(self, cluster: str) -> KubeClient:
        return self._fleet.client(cluster)

    def _query(self, cluster: str, promql: str) -> list[dict[str, Any]]:
        """Instant query. A cluster with no reachable Prometheus yields an
        empty result rather than an exception: one blind cluster must not
        abort a fleet-wide answer."""
        try:
            data = self._client(cluster).prom("query", query=promql)
        except (httpx.HTTPError, OSError, ValueError):
            return []
        return list((data.get("data") or {}).get("result") or [])

    def _value(self, cluster: str, promql: str) -> float | None:
        return _scalar(self._query(cluster, promql))

    # -- placement (FR-CTX-2) ------------------------------------------------

    def find_app_placements(self, app_label: str) -> dict[str, Any]:
        """Where the app actually runs, discovered from kube_pod_labels on
        every cluster in the registry. The application registry is consulted
        only for the application's NAME, never for its placement."""
        registry = self._fleet.app_by_label(app_label)
        application = registry[0] if registry else app_label
        promql = f'kube_pod_labels{{label_app_kubernetes_io_name="{app_label}"}}'

        placements: list[dict[str, Any]] = []
        unreachable: list[str] = []
        for cluster in self._fleet.names():
            series = self._query(cluster, promql)
            if not series:
                try:
                    self._client(cluster).prom("query", query="up")
                except (httpx.HTTPError, OSError, ValueError):
                    unreachable.append(cluster)
                continue
            per_namespace: dict[str, int] = {}
            for sample in series:
                namespace = sample.get("metric", {}).get("namespace")
                if namespace:
                    per_namespace[namespace] = per_namespace.get(namespace, 0) + 1
            environment = self._fleet.entry(cluster).get("environment")
            for namespace, count in sorted(per_namespace.items()):
                placements.append({
                    "cluster": cluster, "namespace": namespace,
                    "environment": environment, "pod_count": count,
                    "application": application,
                })
        result: dict[str, Any] = {"app_label": app_label, "placements": placements}
        if unreachable:
            # Absence of a placement on an unreachable cluster is not evidence
            # of absence; say which clusters could not answer.
            result["clusters_unreachable"] = unreachable
        return result

    # -- cluster signals -----------------------------------------------------

    def get_firing_alerts(self, cluster: str, namespace: str | None = None) -> dict[str, Any]:
        self._fleet.entry(cluster)
        try:
            data = self._client(cluster).prom("alerts")
            alerts = list((data.get("data") or {}).get("alerts") or [])
            reachable = True
        except (httpx.HTTPError, OSError, ValueError):
            alerts, reachable = [], False

        critical: list[dict[str, Any]] = []
        warning: list[dict[str, Any]] = []
        cert_expiry: list[dict[str, Any]] = []
        info_count = 0
        watchdog_present = False

        for alert in alerts:
            if alert.get("state") != "firing":
                continue
            labels = alert.get("labels") or {}
            name = labels.get("alertname", "")
            if name == "Watchdog":
                # The dead man's switch is a monitoring-trust signal, never a
                # finding: the battery excludes it from the alert buckets.
                watchdog_present = True
                continue
            row = {
                "name": name,
                "namespace": labels.get("namespace"),
                "since": alert.get("activeAt"),
                "summary": (alert.get("annotations") or {}).get(
                    "summary", (alert.get("annotations") or {}).get("description", "")),
            }
            if namespace is not None and row["namespace"] != namespace:
                continue
            if "cert" in name.lower():
                cert_expiry.append(row)
            severity = labels.get("severity")
            if severity == "critical":
                critical.append(row)
            elif severity == "warning":
                warning.append(row)
            else:
                info_count += 1

        # Monitoring is trustworthy when Prometheus answered AND its own
        # object-state exporter is up; otherwise cluster signals may be stale.
        ksm_up = self._value(cluster, 'max(up{job="kube-state-metrics"})') if reachable else None
        return {
            "cluster": cluster, "namespace": namespace,
            "critical": critical, "warning": warning, "info_count": info_count,
            "watchdog_present": watchdog_present,
            "monitoring_healthy": bool(reachable and ksm_up == 1.0),
            "cert_expiry_alerts": cert_expiry,
            "summary": (
                f"{len(critical)} critical, {len(warning)} warning firing"
                if reachable else "prometheus unreachable"
            ),
        }

    def get_etcd_health(self, cluster: str) -> dict[str, Any]:
        """etcd health from the API server's own readiness probe.

        A kind control plane keeps etcd's metrics port bound to localhost, so
        the pod network cannot scrape it. /readyz?verbose is the honest source
        available from outside: it carries the API server's live etcd check.
        WAL fsync latency has no such fallback and stays None.
        """
        self._fleet.entry(cluster)
        client = self._client(cluster)
        try:
            readyz = client.get_text("/readyz", verbose="")
            etcd_ok = "[+]etcd ok" in readyz
        except (httpx.HTTPError, OSError):
            etcd_ok = False
        control_plane = [
            n for n in client.items("/api/v1/nodes")
            if "node-role.kubernetes.io/control-plane" in (n["metadata"].get("labels") or {})
        ]
        expected = len(control_plane) or 1
        return {
            "cluster": cluster,
            "has_leader": etcd_ok,
            "quorum": etcd_ok,
            "members_expected": expected,
            "members_up": expected if etcd_ok else 0,
            "leader_changes_15m": None,
            "fsync_p99_ms": None,
            "source": "apiserver /readyz etcd check",
            "fsync_metric_available": False,
        }

    def get_apiserver_slo(self, cluster: str) -> dict[str, Any]:
        self._fleet.entry(cluster)
        total = self._value(cluster, 'sum(rate(apiserver_request_total[5m]))')
        errors = self._value(cluster, 'sum(rate(apiserver_request_total{code=~"5.."}[5m]))')
        error_rate = _ratio(errors, total)
        return {
            "cluster": cluster,
            "error_rate_5xx": error_rate,
            "burn_rate_1h": round(error_rate / 0.01, 2) if error_rate is not None else None,
            # Unknown is not unhealthy: a missing metric must not fail the
            # check, and the availability flag says which case this is.
            "healthy": error_rate is None or error_rate <= 0.01,
            "metric_available": error_rate is not None,
        }

    def get_capacity_summary(self, cluster: str) -> dict[str, Any]:
        self._fleet.entry(cluster)
        cpu_alloc = self._value(cluster, 'sum(kube_node_status_allocatable{resource="cpu"})')
        mem_alloc = self._value(cluster, 'sum(kube_node_status_allocatable{resource="memory"})')
        cpu_req = self._value(cluster, 'sum(kube_pod_container_resource_requests{resource="cpu"})')
        mem_req = self._value(cluster, 'sum(kube_pod_container_resource_requests{resource="memory"})')
        pod_count = self._value(cluster, 'count(kube_pod_info)')
        pod_capacity = self._value(cluster, 'sum(kube_node_status_allocatable{resource="pods"})')
        biggest_node = self._value(cluster, 'max(kube_node_status_allocatable{resource="cpu"})')
        node_count = self._value(cluster, 'count(kube_node_info)')

        # The minus-one-node guard asks whether requests still fit after losing
        # the largest node. On a single-node cluster the answer is genuinely
        # no; reporting True there would be a comfortable lie.
        fits = None
        if None not in (cpu_alloc, cpu_req, biggest_node):
            fits = (float(cpu_alloc or 0) - float(biggest_node or 0)) >= float(cpu_req or 0)

        cpu_ratio = _ratio(cpu_req, cpu_alloc)
        mem_ratio = _ratio(mem_req, mem_alloc)
        summary = (
            f"cpu req {int((cpu_ratio or 0) * 100)}%, mem req {int((mem_ratio or 0) * 100)}% "
            f"of allocatable across {int(node_count or 0)} node(s)"
        )
        if node_count is not None and node_count <= 1:
            summary += "; single-node cluster, so the minus-one-node guard cannot pass"
        return {
            "cluster": cluster,
            "cpu_requests_ratio": cpu_ratio,
            "memory_requests_ratio": mem_ratio,
            "pod_count": int(pod_count) if pod_count is not None else None,
            "pod_capacity": int(pod_capacity) if pod_capacity is not None else None,
            "pod_ratio": _ratio(pod_count, pod_capacity),
            "fits_minus_one_node": fits,
            "summary": summary,
        }

    # -- workload signals ----------------------------------------------------

    def _pod_regex(self, cluster: str, namespace: str, app_label: str) -> str | None:
        """Pod names carrying the app label, as a PromQL alternation.

        cAdvisor series carry no app labels, so the pod set is resolved once
        from kube_pod_labels and then applied to the metric queries.
        """
        series = self._query(
            cluster,
            f'kube_pod_labels{{namespace="{namespace}",label_app_kubernetes_io_name="{app_label}"}}',
        )
        pods = sorted({s.get("metric", {}).get("pod", "") for s in series} - {""})
        return "|".join(pods) if pods else None

    def get_golden_signals(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        """Request rate, error rate and p95 latency come from the application's
        own HTTP instrumentation. The reference fleet's demo workloads expose
        none, so those three report None with instrumented=false rather than
        inventing a curve."""
        self._fleet.entry(cluster)
        slo_ms = self._latency_slo_ms(app_label)
        selector = f'{{namespace="{namespace}"}}'
        total = self._value(cluster, f"sum(rate(http_requests_total{selector}[5m]))")
        errors = self._value(cluster, f'sum(rate(http_requests_total{{namespace="{namespace}",code=~"5.."}}[5m]))')
        p95 = self._value(
            cluster,
            f"histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{selector}[5m])))",
        )
        p95_ms = round(p95 * 1000, 1) if p95 is not None else None
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "request_rate": round(total, 2) if total is not None else None,
            "error_rate": _ratio(errors, total),
            "p95_latency_ms": p95_ms,
            "latency_slo_ms": slo_ms,
            "latency_breach": bool(p95_ms is not None and slo_ms is not None and p95_ms > slo_ms),
            "instrumented": total is not None,
            "note": None if total is not None else "no HTTP request metrics exposed by this workload",
        }

    def _latency_slo_ms(self, app_label: str) -> int | None:
        """Parse 'p95 latency < 400ms' style SLO strings from the registry."""
        entry = self._fleet.app_by_label(app_label)
        if entry is None:
            return None
        for token in str(entry[1].get("slo", "")).replace(",", " ").split():
            if token.endswith("ms") and token[:-2].isdigit():
                return int(token[:-2])
        return None

    def get_workload_usage(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._fleet.entry(cluster)
        pods = self._pod_regex(cluster, namespace, app_label)
        scope = f'namespace="{namespace}",pod=~"{pods}"' if pods else f'namespace="{namespace}"'

        cpu_req = self._value(
            cluster, f'sum(kube_pod_container_resource_requests{{{scope},resource="cpu"}})')
        cpu_lim = self._value(
            cluster, f'sum(kube_pod_container_resource_limits{{{scope},resource="cpu"}})')
        mem_req = self._value(
            cluster, f'sum(kube_pod_container_resource_requests{{{scope},resource="memory"}})')
        mem_lim = self._value(
            cluster, f'sum(kube_pod_container_resource_limits{{{scope},resource="memory"}})')
        cpu_usage = self._value(
            cluster, f'sum(rate(container_cpu_usage_seconds_total{{{scope},container!=""}}[5m]))')
        mem_p95 = self._value(
            cluster,
            f'max(quantile_over_time(0.95, container_memory_working_set_bytes{{{scope},container!=""}}[1h]))',
        )
        throttled = self._value(
            cluster, f'sum(rate(container_cpu_cfs_throttled_periods_total{{{scope}}}[5m]))')
        periods = self._value(
            cluster, f'sum(rate(container_cpu_cfs_periods_total{{{scope}}}[5m]))')

        mem_p95_mi = round(mem_p95 / 1024**2) if mem_p95 is not None else None
        mem_lim_mi = round(mem_lim / 1024**2) if mem_lim is not None else None
        hpa = self._hpa(cluster, namespace, app_label)
        pdb = self._pdb(cluster, namespace)
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "cpu": {
                "requests_millicores": round(cpu_req * 1000) if cpu_req is not None else None,
                "limits_millicores": round(cpu_lim * 1000) if cpu_lim is not None else None,
                "usage_ratio_of_requests": _ratio(cpu_usage, cpu_req),
            },
            "memory": {
                "requests_mi": round(mem_req / 1024**2) if mem_req is not None else None,
                "limits_mi": mem_lim_mi,
                "p95_usage_mi": mem_p95_mi,
                "usage_ratio_of_limit": _ratio(mem_p95, mem_lim),
            },
            "memory_summary": (
                f"p95 {mem_p95_mi}Mi of {mem_lim_mi}Mi limit"
                if mem_p95_mi is not None and mem_lim_mi else "memory usage metric unavailable"
            ),
            "throttling_ratio": _ratio(throttled, periods),
            "hpa": hpa,
            "hpa_summary": (
                f"HPA {hpa['current']}/{hpa['max']}" if hpa["present"] else "no HPA"
            ),
            "pdb": pdb,
            "pdb_summary": (
                f"{pdb['disruptions_allowed']} disruption(s) allowed"
                if pdb["present"] else "no PodDisruptionBudget"
            ),
        }

    def _hpa(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        scope = f'namespace="{namespace}",horizontalpodautoscaler="{app_label}"'
        minimum = self._value(cluster, f"max(kube_horizontalpodautoscaler_spec_min_replicas{{{scope}}})")
        maximum = self._value(cluster, f"max(kube_horizontalpodautoscaler_spec_max_replicas{{{scope}}})")
        current = self._value(cluster, f"max(kube_horizontalpodautoscaler_status_current_replicas{{{scope}}})")
        present = maximum is not None
        at_max = bool(current is not None and maximum is not None and current >= maximum)
        return {
            "present": present,
            "min": int(minimum) if minimum is not None else None,
            "max": int(maximum) if maximum is not None else None,
            "current": int(current) if current is not None else None,
            "at_max": at_max,
            "min_eq_max": bool(present and minimum == maximum),
            "maxed_out": at_max,
        }

    def _pdb(self, cluster: str, namespace: str) -> dict[str, Any]:
        allowed = self._value(
            cluster,
            f'min(kube_poddisruptionbudget_status_pod_disruptions_allowed{{namespace="{namespace}"}})',
        )
        return {
            "present": allowed is not None,
            "disruptions_allowed": int(allowed) if allowed is not None else None,
            "blocked": bool(allowed is not None and allowed == 0),
        }

    # -- raw query surface ---------------------------------------------------

    def _cluster_for_query(self, promql: str) -> tuple[str, str | None]:
        """Pick which cluster's Prometheus answers a raw query.

        Fleet convention scopes queries by the `cluster` external label, so an
        explicit cluster="..." selector routes the query. Without one there is
        no fleet-wide aggregator to ask, so the first registered cluster
        answers and the note says so.
        """
        match = _CLUSTER_LABEL.search(promql)
        names = self._fleet.names()
        if match and match.group(1) in names:
            return match.group(1), None
        if not names:
            raise ValueError("live fleet registry is empty; nothing to query")
        return names[0], (
            f"no cluster=\"...\" selector in the query; answered by {names[0]} only. "
            "Each cluster runs its own Prometheus in this fleet."
        )

    def query_instant(self, promql: str) -> dict[str, Any]:
        cluster, note = self._cluster_for_query(promql)
        try:
            data = self._client(cluster).prom("query", query=promql)
        except httpx.HTTPStatusError as exc:
            return {"promql": promql, "note": f"prometheus rejected the query on {cluster}: "
                    f"{exc.response.text[:200]}", "result": []}
        return {
            "promql": promql,
            "note": note or f"instant query answered by {cluster}",
            "result": list((data.get("data") or {}).get("result") or []),
        }

    def query_range(self, promql: str, start: str, end: str, step: str = "60s") -> dict[str, Any]:
        """Range query over the same proxy path. Not wired to an MCP tool yet;
        it exists so a range-capable tool is a server-side change only."""
        cluster, note = self._cluster_for_query(promql)
        data = self._client(cluster).prom(
            "query_range", query=promql, start=start, end=end, step=step)
        return {
            "promql": promql,
            "note": note or f"range query answered by {cluster}",
            "result": list((data.get("data") or {}).get("result") or []),
        }

    def get_dashboard_links(self, cluster: str, namespace: str) -> dict[str, Any]:
        """No Grafana in the local reference fleet. Returning an empty list with
        a reason beats returning links that 404."""
        self._fleet.entry(cluster)
        return {
            "links": [],
            "note": (
                "no Grafana deployed in this fleet; Prometheus is reachable only "
                "through the API server service proxy"
            ),
        }
