"""OpenShift backend: real cluster state over the Kubernetes API.

Design contract: RESULT SHAPES are the contract. The check batteries in
config/checks/*.yaml address these fields by dotted path, so a shape change
here is a battery change there.

Vanilla Kubernetes contract. The reference live fleet is kind, not OpenShift,
so ClusterVersion, ClusterOperator, MachineConfigPool and the OpenShift
node-approver CSR semantics simply do not exist there. Those four tools do
NOT error: they return the full result shape with health-neutral values plus
``applicable: false`` and a reason (cloudops.mcp_servers.kube.not_applicable),
so the attestation battery's rules never trigger, the checks land as plain
passes, and the cluster verdict stays honest instead of going degraded or
unattestable over a question the cluster cannot be asked. Point the registry
at a real OpenShift cluster and these four grow real implementations without
any battery change.

Credentials come from the kubeconfig on disk only (FR-MCP-7); every request
is a GET (NFR-SEC-1); Secret DATA is never read, only names (NFR-LOG-3).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from cloudops.mcp_servers.kube import KubeClient, not_applicable
from cloudops.mcp_servers.live_fleet import LiveFleet

# Kubernetes reports node conditions as a list; these are the pressure ones
# whose True state the attestation battery treats as a warning signal.
PRESSURE_CONDITIONS = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")

_UNIT_MULTIPLIERS = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
    "m": 0.001,
}


def _parse_quantity(value: Any) -> float:
    """Kubernetes resource quantity ('500m', '64Gi', '2') as a float in base
    units. Unparseable values return 0.0 rather than raising: one odd quota
    entry must not sink a whole tool call."""
    text = str(value).strip()
    for suffix, mult in sorted(_UNIT_MULTIPLIERS.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * mult
            except ValueError:
                return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ago(value: str | None) -> str:
    """Compact age string ('45s', '12m', '3h', '2d') for event and node rows."""
    ts = _parse_ts(value)
    if ts is None:
        return "unknown"
    seconds = max((datetime.now(UTC) - ts).total_seconds(), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def _age_days(value: str | None) -> int:
    ts = _parse_ts(value)
    return 0 if ts is None else int((datetime.now(UTC) - ts).total_seconds() // 86400)


def _split_image(image: str) -> tuple[str, str]:
    """('registry/repo', 'tag'). Digest references keep the digest as the tag."""
    if "@" in image:
        repo, _, digest = image.partition("@")
        return repo, digest
    repo, sep, tag = image.rpartition(":")
    if not sep or "/" in tag:  # no tag, just a registry port or a bare repo
        return image, "latest"
    return repo, tag


def _labels_name(metadata: dict[str, Any]) -> str | None:
    """The fleet's app identity label on any object, with the pre-convention
    `app` label as a fallback."""
    labels = metadata.get("labels") or {}
    name = labels.get("app.kubernetes.io/name") or labels.get("app")
    return str(name) if name is not None else None


def _prefer_matching(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Rows whose labels name the app, or all of them when none do.

    The second element says which happened, so the caller can label the
    rollup as app-scoped or namespace-scoped instead of overclaiming.
    """
    matching = [r for r in rows if r["app_match"]]
    return (matching, True) if matching else (rows, False)


def _scope_summary(text: str, present: bool, exact: bool) -> str:
    if not present or exact:
        return text
    return text + " (namespace-wide; no object labelled for this app)"


class LiveOpenShiftBackend:
    """Real-cluster backend. Read-only by construction (NFR-SEC-1)."""

    def __init__(self, fleet: LiveFleet | None = None) -> None:
        self._fleet = fleet or LiveFleet()

    def _client(self, cluster: str) -> KubeClient:
        return self._fleet.client(cluster)

    # -- fleet resolution ----------------------------------------------------

    def resolve_cluster(self, query: str) -> dict[str, Any]:
        return self._fleet.resolve_cluster(query)

    def list_clusters(
        self, environment: str | None = None, region: str | None = None,
        page: int = 1, page_size: int = 50,
    ) -> dict[str, Any]:
        return self._fleet.list_clusters(environment, region, page, page_size)

    def get_app_registry_entry(self, application: str) -> dict[str, Any]:
        return self._fleet.get_app_registry_entry(application)

    # -- cluster state -------------------------------------------------------

    def get_cluster_info(self, cluster: str) -> dict[str, Any]:
        entry = self._fleet.entry(cluster)
        client = self._client(cluster)
        start = time.perf_counter()
        reachable, version = True, None
        try:
            version = str(client.get("/version").get("gitVersion"))
        except (httpx.HTTPError, OSError):
            # Reachability IS the reading here: an unreachable cluster must
            # surface as reachable=false, not as a tool error, or the battery
            # loses the difference between "down" and "could not be asked".
            reachable = False
        readyz = self._readyz(client) if reachable else {
            "readyz_probed": False, "readyz_ok": None, "readyz_failing": [],
            "readyz_summary": "not probed: cluster unreachable",
        }
        return {
            "cluster": cluster,
            "reachable": reachable,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1),
            "version": version,
            "channel": None,  # no update channels outside OpenShift
            "api_url": client.server,
            "console_url": entry.get("console_url"),
            "region": entry.get("region"),
            "environment": entry.get("environment"),
            "labels": dict(entry.get("labels") or {}),
            **readyz,
        }

    @staticmethod
    def _readyz(client: KubeClient) -> dict[str, Any]:
        """Component-level control-plane readiness from ``/readyz?verbose``.

        The verbose form lists every named sub-check (``[+]etcd ok`` /
        ``[-]etcd failed``), so an unhealthy control plane names WHAT is
        failing instead of only that it is. Two honesty rules:

        - a failing readyz answers HTTP 500 with the breakdown still in the
          body, so the 500 is parsed, not treated as transport failure;
        - the endpoint is authorization-gated (a nonResourceURL get), and a
          401/403 reads as "not permitted", never as unhealthy - missing
          permission is not evidence about the cluster.
        """
        try:
            text = client.get_text("/readyz", verbose="true")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                return {
                    "readyz_probed": False, "readyz_ok": None, "readyz_failing": [],
                    "readyz_summary": (
                        f"readyz not permitted ({exc.response.status_code}); "
                        "grant get on the /readyz nonResourceURL for sub-check detail"
                    ),
                }
            text = exc.response.text
        except (httpx.HTTPError, OSError):
            return {
                "readyz_probed": False, "readyz_ok": None, "readyz_failing": [],
                "readyz_summary": "readyz did not answer",
            }
        checks = [line for line in text.splitlines() if line.startswith(("[+]", "[-]"))]
        failing = sorted(
            line[3:].split(" ", 1)[0].removesuffix(":") for line in checks
            if line.startswith("[-]")
        )
        ok = bool(checks) and not failing
        summary = (
            f"readyz: {len(checks) - len(failing)}/{len(checks)} sub-checks ok"
            + (f"; failing: {', '.join(failing)}" if failing else "")
            if checks else "readyz answered without a sub-check breakdown"
        )
        return {
            "readyz_probed": True, "readyz_ok": ok if checks else None,
            "readyz_failing": failing, "readyz_summary": summary,
        }

    def get_cluster_version(self, cluster: str) -> dict[str, Any]:
        """No ClusterVersion on vanilla Kubernetes: report the server version
        as the observed reading, with neutral conditions."""
        version = self._fleet.version(cluster)
        return not_applicable(
            cluster=cluster,
            version=version,
            desired_version=version,
            channel=None,
            available=True,
            progressing=False,
            failing=False,
            progressing_message=None,
            history=[{"version": version, "state": "Completed", "completed_at": None}],
        )

    def get_cluster_operators(self, cluster: str) -> dict[str, Any]:
        """No ClusterOperator API: zero operators, zero degraded."""
        self._fleet.entry(cluster)
        return not_applicable(
            cluster=cluster, total=0, available_count=0,
            degraded=[], critical_degraded=[], progressing=[], unavailable=[],
        )

    def get_machine_config_pools(self, cluster: str) -> dict[str, Any]:
        """No MachineConfig operator: no pools, nothing rolling."""
        self._fleet.entry(cluster)
        result = not_applicable(
            cluster=cluster, pools=[], any_updating=False,
            any_degraded=False, any_paused=False,
        )
        # `summary` is what the battery renders as this check's observed
        # reading, so the n/a note shows in the attestation table itself and
        # not only in the tool payload.
        result["summary"] = result["not_applicable_reason"]
        return result

    def get_pending_csrs(self, cluster: str) -> dict[str, Any]:
        """certificates.k8s.io exists on vanilla Kubernetes, but the pending-CSR
        signal this check trusts is the OpenShift machine-approver backlog,
        which does not."""
        self._fleet.entry(cluster)
        return not_applicable(cluster=cluster, pending_count=0, pending=[])

    def get_nodes(self, cluster: str) -> dict[str, Any]:
        nodes = self._client(cluster).items("/api/v1/nodes")
        not_ready: list[dict[str, Any]] = []
        cordoned: list[dict[str, Any]] = []
        pressure: list[dict[str, Any]] = []
        roles: dict[str, int] = {}
        ready_count = 0

        for node in nodes:
            name = node["metadata"]["name"]
            labels = node["metadata"].get("labels") or {}
            role = "master" if any(
                key in labels for key in
                ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master")
            ) else "worker"
            roles[role] = roles.get(role, 0) + 1

            conditions = {c["type"]: c for c in node.get("status", {}).get("conditions", [])}
            is_ready = conditions.get("Ready", {}).get("status") == "True"
            unschedulable = bool(node.get("spec", {}).get("unschedulable"))
            if unschedulable:
                cordoned.append({"name": name, "mcp": role})
            if not is_ready:
                cond = conditions.get("Ready", {})
                not_ready.append({
                    "name": name,
                    "since": _ago(cond.get("lastTransitionTime")),
                    "reason": cond.get("reason") or "Unknown",
                })
            elif not unschedulable:
                ready_count += 1
            for cond_type in PRESSURE_CONDITIONS:
                if conditions.get(cond_type, {}).get("status") == "True":
                    pressure.append({
                        "name": name, "condition": cond_type,
                        "since": _ago(conditions[cond_type].get("lastTransitionTime")),
                    })

        return {
            "cluster": cluster,
            "total": len(nodes),
            "ready": ready_count,
            "not_ready": not_ready,
            "cordoned": cordoned,
            "pressure": pressure,
            "roles": roles,
        }

    def get_namespaces(self, cluster: str) -> dict[str, Any]:
        """Namespace inventory: what exists on this cluster to look inside."""
        items = self._client(cluster).items("/api/v1/namespaces")
        return {
            "cluster": cluster,
            "total": len(items),
            "namespaces": [
                {"name": ns["metadata"]["name"],
                 "phase": ns.get("status", {}).get("phase"),
                 "age_days": _age_days(ns["metadata"].get("creationTimestamp"))}
                for ns in items
            ],
        }

    def get_capacity(self, cluster: str) -> dict[str, Any]:
        """Requests versus allocatable across the cluster, from the Kubernetes
        API alone.

        Requests are summed over the CONTAINERS of non-terminal pods (Succeeded
        and Failed pods hold no reservation), allocatable over node status. That
        is the same arithmetic the scheduler does, so the answer is a fact about
        the cluster rather than a metrics-pipeline reading; the result keys
        mirror the retired Prometheus capacity summary so the attestation rule
        is unchanged apart from the tool name.
        """
        client = self._client(cluster)
        nodes = client.items("/api/v1/nodes")
        cpu_alloc = mem_alloc = pod_capacity = 0.0
        biggest_node = 0.0
        for node in nodes:
            allocatable = node.get("status", {}).get("allocatable") or {}
            node_cpu = _parse_quantity(allocatable.get("cpu", 0))
            cpu_alloc += node_cpu
            mem_alloc += _parse_quantity(allocatable.get("memory", 0))
            pod_capacity += _parse_quantity(allocatable.get("pods", 0))
            biggest_node = max(biggest_node, node_cpu)

        pods = client.items("/api/v1/pods", fieldSelector="status.phase!=Succeeded")
        live_pods = [p for p in pods if p.get("status", {}).get("phase") != "Failed"]
        cpu_req = mem_req = 0.0
        for pod in live_pods:
            spec = pod.get("spec", {})
            for container in list(spec.get("containers") or []):
                requests = (container.get("resources") or {}).get("requests") or {}
                cpu_req += _parse_quantity(requests.get("cpu", 0))
                mem_req += _parse_quantity(requests.get("memory", 0))

        cpu_ratio = round(cpu_req / cpu_alloc, 4) if cpu_alloc else None
        mem_ratio = round(mem_req / mem_alloc, 4) if mem_alloc else None
        pod_count = len(live_pods)
        pod_ratio = round(pod_count / pod_capacity, 4) if pod_capacity else None
        # The minus-one-node guard asks whether requests still fit after losing
        # the largest node. On a single-node cluster the answer is genuinely
        # no; reporting True there would be a comfortable lie.
        fits = (cpu_alloc - biggest_node) >= cpu_req if nodes else None
        summary = (
            f"cpu req {int((cpu_ratio or 0) * 100)}%, mem req {int((mem_ratio or 0) * 100)}% "
            f"of allocatable across {len(nodes)} node(s)"
        )
        if len(nodes) <= 1:
            summary += "; single-node cluster, so the minus-one-node guard cannot pass"
        return {
            "cluster": cluster,
            "nodes": len(nodes),
            "cpu_requests_cores": round(cpu_req, 3),
            "cpu_allocatable_cores": round(cpu_alloc, 3),
            "cpu_requests_ratio": cpu_ratio,
            "memory_requests_mi": round(mem_req / 1024**2),
            "memory_allocatable_mi": round(mem_alloc / 1024**2),
            "memory_requests_ratio": mem_ratio,
            "pod_count": pod_count,
            "pod_capacity": int(pod_capacity),
            "pod_ratio": pod_ratio,
            "fits_minus_one_node": fits,
            "summary": summary,
        }

    # -- namespace / application state ---------------------------------------

    def verify_placement(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        """Does this app actually run here? The registry proposes, the cluster
        API confirms (FR-CTX-2).

        Never raises. An unreachable cluster, a namespace that does not exist
        and a namespace with no matching pods are three different honest
        answers, and context resolution has to tell them apart rather than
        lose the whole turn to one bad candidate.
        """
        result: dict[str, Any] = {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "reachable": True, "pod_count": 0, "ready_count": 0, "verified": False,
        }
        try:
            client = self._client(cluster)
            selector = self._selector(client, namespace, app_label)
            pods = client.items(
                f"/api/v1/namespaces/{namespace}/pods", labelSelector=selector)
        except httpx.HTTPStatusError as exc:
            # The API server ANSWERED - a missing namespace, or a forbidden
            # one. That is "the app is not there", not "the cluster is down",
            # and context resolution treats those two very differently.
            result["reason"] = f"HTTP {exc.response.status_code} for namespace {namespace}"
            return result
        except Exception as exc:  # noqa: BLE001 - unreachable IS the reading here
            result["reachable"] = False
            result["reason"] = f"{type(exc).__name__}: {exc}"[:200]
            return result
        ready = 0
        for pod in pods:
            statuses = list(pod.get("status", {}).get("containerStatuses") or [])
            if statuses and all(c.get("ready") for c in statuses):
                ready += 1
        result["pod_count"] = len(pods)
        result["ready_count"] = ready
        result["verified"] = len(pods) > 0
        return result

    def get_autoscaling(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        """HorizontalPodAutoscalers (autoscaling/v2) and PodDisruptionBudgets
        (policy/v1) for a namespace.

        Neither object carries the app label reliably, so the namespace is
        listed in full and each row is marked ``app_match`` when its own labels,
        its scale target, or its pod selector names the app. The rolled-up
        ``hpa``/``pdb`` blocks prefer matching rows and fall back to the whole
        namespace, saying which happened in the summary rather than pretending
        the filter was exact.
        """
        client = self._client(cluster)
        hpas: list[dict[str, Any]] = []
        for item in client.items(
            f"/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers"
        ):
            meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
            target = str((spec.get("scaleTargetRef") or {}).get("name", ""))
            minimum = spec.get("minReplicas")
            maximum = spec.get("maxReplicas")
            current = status.get("currentReplicas")
            desired = status.get("desiredReplicas")
            at_max = bool(current is not None and maximum is not None and current >= maximum)
            hpas.append({
                "name": meta["name"],
                "target": target,
                "app_match": _labels_name(meta) == app_label or target == app_label,
                "min": int(minimum) if minimum is not None else None,
                "max": int(maximum) if maximum is not None else None,
                "current": int(current) if current is not None else None,
                "desired": int(desired) if desired is not None else None,
                "at_max": at_max,
                "min_eq_max": bool(minimum is not None and minimum == maximum),
                # KubeHpaMaxedOut excludes min==max: a fixed-size HPA is a
                # deployment with extra steps, not an autoscaler out of room.
                "maxed": at_max and minimum != maximum,
            })

        pdbs: list[dict[str, Any]] = []
        for item in client.items(
            f"/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets"
        ):
            meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
            match_labels = (spec.get("selector") or {}).get("matchLabels") or {}
            allowed = status.get("disruptionsAllowed")
            pdbs.append({
                "name": meta["name"],
                "app_match": app_label in (
                    match_labels.get("app.kubernetes.io/name"), match_labels.get("app"),
                    _labels_name(meta),
                ),
                "disruptions_allowed": int(allowed) if allowed is not None else None,
                "expected_pods": int(status.get("expectedPods", 0)),
                "current_healthy": int(status.get("currentHealthy", 0)),
                "desired_healthy": int(status.get("desiredHealthy", 0)),
                "blocked": bool(allowed is not None and allowed == 0),
            })

        hpa_rows, hpa_exact = _prefer_matching(hpas)
        pdb_rows, pdb_exact = _prefer_matching(pdbs)
        hpa = {
            "present": bool(hpa_rows),
            "min": hpa_rows[0]["min"] if hpa_rows else None,
            "max": hpa_rows[0]["max"] if hpa_rows else None,
            "current": hpa_rows[0]["current"] if hpa_rows else None,
            "desired": hpa_rows[0]["desired"] if hpa_rows else None,
            "at_max": any(h["at_max"] for h in hpa_rows),
            "min_eq_max": all(h["min_eq_max"] for h in hpa_rows) if hpa_rows else False,
            "maxed_out": any(h["maxed"] for h in hpa_rows),
        }
        allowed_values = [p["disruptions_allowed"] for p in pdb_rows
                          if p["disruptions_allowed"] is not None]
        pdb = {
            "present": bool(pdb_rows),
            "disruptions_allowed": min(allowed_values) if allowed_values else None,
            "expected_pods": max((p["expected_pods"] for p in pdb_rows), default=0),
            "blocked": any(p["blocked"] for p in pdb_rows),
        }
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "hpas": hpas, "pdbs": pdbs,
            "hpa": hpa, "pdb": pdb,
            "hpa_summary": _scope_summary(
                f"HPA {hpa['current']}/{hpa['max']} replicas" if hpa["present"] else "no HPA",
                bool(hpa_rows), hpa_exact,
            ),
            "pdb_summary": _scope_summary(
                f"{pdb['disruptions_allowed']} disruption(s) allowed"
                if pdb["present"] else "no PodDisruptionBudget",
                bool(pdb_rows), pdb_exact,
            ),
        }

    def _selector(self, client: KubeClient, namespace: str, app_label: str) -> str:
        """app.kubernetes.io/name is the fleet convention; fall back to the
        bare `app` label so workloads that predate the convention resolve
        instead of silently returning nothing."""
        primary = f"app.kubernetes.io/name={app_label}"
        pods = client.items(f"/api/v1/namespaces/{namespace}/pods", labelSelector=primary)
        return primary if pods else f"app={app_label}"

    def get_workloads(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        client = self._client(cluster)
        selector = self._selector(client, namespace, app_label)
        deployments = client.items(
            f"/apis/apps/v1/namespaces/{namespace}/deployments", labelSelector=selector)
        pods = client.items(f"/api/v1/namespaces/{namespace}/pods", labelSelector=selector)
        replicasets = client.items(
            f"/apis/apps/v1/namespaces/{namespace}/replicasets", labelSelector=selector)

        workloads: list[dict[str, Any]] = []
        rollouts_in_progress: list[str] = []
        failed_rollouts: list[str] = []
        for dep in deployments:
            spec, status = dep.get("spec", {}), dep.get("status", {})
            desired = int(spec.get("replicas", 0))
            ready = int(status.get("readyReplicas", 0))
            containers = spec.get("template", {}).get("spec", {}).get("containers", [])
            image, tag = _split_image(containers[0]["image"]) if containers else ("", "")
            conditions = {c["type"]: c for c in status.get("conditions", [])}
            progressing = conditions.get("Progressing", {})
            if (progressing.get("status") == "True"
                    and progressing.get("reason") != "NewReplicaSetAvailable"):
                rollouts_in_progress.append(dep["metadata"]["name"])
            if progressing.get("reason") == "ProgressDeadlineExceeded":
                failed_rollouts.append(dep["metadata"]["name"])
            workloads.append({
                "name": dep["metadata"]["name"], "kind": "Deployment",
                "desired": desired, "ready": ready,
                "available": int(status.get("availableReplicas", 0)),
                "updated": int(status.get("updatedReplicas", 0)),
                "strategy": spec.get("strategy", {}).get("type", "RollingUpdate"),
                "image": image, "image_tag": tag,
                "rollout_healthy": ready == desired,
                "age_days": _age_days(dep["metadata"].get("creationTimestamp")),
            })

        history = []
        for rs in sorted(
            replicasets,
            key=lambda r: int((r["metadata"].get("annotations") or {}).get(
                "deployment.kubernetes.io/revision", 0)),
            reverse=True,
        )[:5]:
            containers = rs.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            _, tag = _split_image(containers[0]["image"]) if containers else ("", "")
            history.append({
                "revision": int((rs["metadata"].get("annotations") or {}).get(
                    "deployment.kubernetes.io/revision", 0)),
                "image_tag": tag,
                "deployed_at": rs["metadata"].get("creationTimestamp"),
                "status": "complete" if int(rs.get("status", {}).get("readyReplicas", 0))
                          == int(rs.get("spec", {}).get("replicas", 0)) else "superseded",
            })

        desired_total = sum(int(w["desired"]) for w in workloads)
        ready_total = sum(int(w["ready"]) for w in workloads)
        latest = history[0] if history else None
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "workloads": workloads,
            "replicas_mismatch": [w["name"] for w in workloads if w["ready"] != w["desired"]],
            "replicas_summary": f"{ready_total}/{desired_total} ready",
            "rollout_summary": (
                "rollout in progress: " + ", ".join(rollouts_in_progress)
                if rollouts_in_progress else "no rollout in progress"
            ),
            "rollouts_in_progress": rollouts_in_progress,
            "failed_rollouts": failed_rollouts,
            "single_replica_workloads": [
                w["name"] for w in workloads
                if w["desired"] == 1 and self._fleet.entry(cluster).get("environment") == "prod"
            ],
            "release_summary": (
                f"rev {latest['revision']} ({latest['image_tag']}) deployed "
                f"{_ago(latest['deployed_at'])} ago" if latest else "no rollout history"
            ),
            "history": history,
            "pods": self._pod_pathology(pods),
        }

    @staticmethod
    def _pod_pathology(pods: list[dict[str, Any]]) -> dict[str, Any]:
        """Container-level truth: waiting and terminated reasons, restart
        counts, probe failures. This is the evidence the analyst narrates, so
        every list carries pod names rather than counts."""
        running, pending = 0, []
        crashloop: list[str] = []
        image_pull: list[str] = []
        oomkilled: list[str] = []
        probes_failing: list[str] = []
        issues: list[dict[str, Any]] = []
        restarts_recent = 0
        hour_ago = datetime.now(UTC).timestamp() - 3600

        for pod in pods:
            name = pod["metadata"]["name"]
            status = pod.get("status", {})
            phase = status.get("phase")
            statuses = list(status.get("containerStatuses") or [])
            all_ready = bool(statuses) and all(c.get("ready") for c in statuses)
            if phase == "Running" and all_ready:
                running += 1
            elif phase in ("Pending", "Unknown"):
                pending.append(name)

            for cs in statuses:
                state = cs.get("state") or {}
                waiting = state.get("waiting") or {}
                terminated = state.get("terminated") or {}
                last_terminated = (cs.get("lastState") or {}).get("terminated") or {}
                reason = (waiting.get("reason") or terminated.get("reason")
                          or last_terminated.get("reason"))
                if waiting.get("reason") == "CrashLoopBackOff" and name not in crashloop:
                    crashloop.append(name)
                if (waiting.get("reason") in ("ImagePullBackOff", "ErrImagePull")
                        and name not in image_pull):
                    image_pull.append(name)
                if last_terminated.get("reason") == "OOMKilled" and name not in oomkilled:
                    oomkilled.append(name)
                if not cs.get("ready") and state.get("running") and name not in probes_failing:
                    probes_failing.append(name)
                finished = _parse_ts(last_terminated.get("finishedAt"))
                if finished is not None and finished.timestamp() >= hour_ago:
                    restarts_recent += int(cs.get("restartCount", 0))
                if reason is not None:
                    issues.append({
                        "pod": name, "container": cs.get("name"), "reason": reason,
                        "restarts": int(cs.get("restartCount", 0)),
                        "message": (waiting.get("message") or terminated.get("message")
                                    or last_terminated.get("message") or "")[:200],
                        "exit_code": terminated.get("exitCode", last_terminated.get("exitCode")),
                    })

        return {
            "total": len(pods),
            "running": running,
            "pending": pending,
            "crashloop": crashloop,
            "image_pull_errors": image_pull,
            "oomkilled_recent": oomkilled,
            "restarts_last_hour": restarts_recent,
            "probes_failing": probes_failing,
            # Per-container detail: the evidence trail the analyst narrates from.
            "container_issues": issues[:20],
        }

    def get_events(self, cluster: str, namespace: str) -> dict[str, Any]:
        events = self._client(cluster).items(
            f"/api/v1/namespaces/{namespace}/events", fieldSelector="type=Warning")
        warnings = []
        for event in events:
            obj = event.get("involvedObject", {})
            warnings.append({
                "reason": event.get("reason"),
                "object": f"{str(obj.get('kind', '')).lower()}/{obj.get('name')}",
                "count": int(event.get("count", 1)),
                "last_seen": _ago(event.get("lastTimestamp") or event.get("eventTime")),
                "message": str(event.get("message", ""))[:300],
            })
        warnings.sort(key=lambda w: int(str(w["count"])), reverse=True)
        return {"cluster": cluster, "namespace": namespace,
                "warnings": warnings[:25], "warning_count": len(warnings)}

    def get_quotas(self, cluster: str, namespace: str) -> dict[str, Any]:
        client = self._client(cluster)
        quotas: list[dict[str, Any]] = []
        for rq in client.items(f"/api/v1/namespaces/{namespace}/resourcequotas"):
            status = rq.get("status", {})
            hard = status.get("hard") or {}
            used = status.get("used") or {}
            for resource, hard_value in hard.items():
                hard_f = _parse_quantity(hard_value)
                used_value = used.get(resource, "0")
                quotas.append({
                    "name": rq["metadata"]["name"], "resource": resource,
                    "used": str(used_value), "hard": str(hard_value),
                    "ratio": round(_parse_quantity(used_value) / hard_f, 2) if hard_f else 0.0,
                })
        limit_ranges = [lr["metadata"]["name"]
                        for lr in client.items(f"/api/v1/namespaces/{namespace}/limitranges")]
        near = [q for q in quotas if float(str(q["ratio"])) > 0.9]
        return {
            "cluster": cluster, "namespace": namespace, "quotas": quotas,
            "near_limit": near, "limit_ranges": limit_ranges,
            "summary": (
                f"{len(quotas)} quota entries, {len(near)} above 90 percent"
                if quotas else "no ResourceQuota in this namespace"
            ),
        }

    def get_network(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        client = self._client(cluster)
        selector = self._selector(client, namespace, app_label)
        services = [
            {"name": svc["metadata"]["name"],
             "type": svc.get("spec", {}).get("type", "ClusterIP"),
             "ports": [p.get("port") for p in svc.get("spec", {}).get("ports", [])]}
            for svc in client.items(f"/api/v1/namespaces/{namespace}/services",
                                    labelSelector=selector)
        ]
        # Vanilla Kubernetes has no Route. Ingress fills the same slot in the
        # result, labelled with its real kind so nobody is misled about what
        # was queried, and with cert expiry left unknown: nothing here
        # terminates TLS, so any number would be invented.
        routes = []
        for ing in client.items(f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses",
                                labelSelector=selector):
            tls = ing.get("spec", {}).get("tls") or []
            for rule in ing.get("spec", {}).get("rules") or [{}]:
                routes.append({
                    "name": ing["metadata"]["name"],
                    "kind": "Ingress",
                    "host": rule.get("host"),
                    "tls_termination": "edge" if tls else "none",
                    "cert_expiry_days": None,
                })
        policies = client.items(
            f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies")
        return {
            "cluster": cluster, "namespace": namespace,
            "services": services,
            "routes": routes,
            "routes_summary": (
                f"{len(routes)} Ingress rule(s); cert expiry not observable in this cluster"
                if routes else "no Ingress for this app"
            ),
            "expiring_certs": [],
            "network_policies_count": len(policies),
            "dns_healthy": self._dns_healthy(client),
        }

    @staticmethod
    def _dns_healthy(client: KubeClient) -> bool:
        """Cluster DNS readiness read from the coredns/kube-dns Deployment
        rather than asserted."""
        for name in ("coredns", "kube-dns"):
            try:
                dep = client.get(f"/apis/apps/v1/namespaces/kube-system/deployments/{name}")
            except httpx.HTTPError:
                continue
            return int(dep.get("status", {}).get("readyReplicas", 0)) >= 1
        return False

    def get_pvcs(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        client = self._client(cluster)
        selector = self._selector(client, namespace, app_label)
        pvcs = []
        for pvc in client.items(f"/api/v1/namespaces/{namespace}/persistentvolumeclaims",
                                labelSelector=selector):
            capacity = (pvc.get("status", {}).get("capacity") or {}).get("storage", "0")
            pvcs.append({
                "name": pvc["metadata"]["name"],
                "status": pvc.get("status", {}).get("phase"),
                "storage_class": pvc.get("spec", {}).get("storageClassName"),
                "capacity_gb": round(_parse_quantity(capacity) / 1024**3, 1),
                # Volume fill needs kubelet volume stats, which this deployment
                # does not collect, so it stays honestly unknown.
                "used_ratio": None,
                "growth_trend": "unknown",
            })
        return {
            "cluster": cluster, "namespace": namespace, "pvcs": pvcs,
            "unbound": [p["name"] for p in pvcs if p["status"] != "Bound"],
            "near_full": [],
            "summary": f"{len(pvcs)} PVC(s)" if pvcs else "no persistent volumes",
        }

    def get_configuration(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        """ConfigMap and Secret REFERENCES only. Secret data is never fetched,
        so it cannot leak into a log or a report (NFR-LOG-3)."""
        client = self._client(cluster)
        selector = self._selector(client, namespace, app_label)
        pods = client.items(f"/api/v1/namespaces/{namespace}/pods", labelSelector=selector)
        configmaps: set[str] = set()
        secrets: set[str] = set()
        env_from: set[str] = set()
        mounted: set[str] = set()

        for pod in pods:
            spec = pod.get("spec", {})
            containers = list(spec.get("containers") or []) + list(spec.get("initContainers") or [])
            for container in containers:
                for source in container.get("envFrom") or []:
                    if "configMapRef" in source:
                        configmaps.add(source["configMapRef"]["name"])
                        env_from.add("configmap/" + source["configMapRef"]["name"])
                    if "secretRef" in source:
                        secrets.add(source["secretRef"]["name"])
                        env_from.add("secret/" + source["secretRef"]["name"])
                for env in container.get("env") or []:
                    ref = env.get("valueFrom") or {}
                    if "configMapKeyRef" in ref:
                        configmaps.add(ref["configMapKeyRef"]["name"])
                    if "secretKeyRef" in ref:
                        secrets.add(ref["secretKeyRef"]["name"])
            for volume in spec.get("volumes") or []:
                if "configMap" in volume:
                    configmaps.add(volume["configMap"]["name"])
                    mounted.add("configmap/" + volume["configMap"]["name"])
                if "secret" in volume:
                    secrets.add(volume["secret"]["secretName"])
                    mounted.add("secret/" + volume["secret"]["secretName"])

        return {
            "cluster": cluster, "namespace": namespace,
            "configmaps": sorted(configmaps),
            "secrets": sorted(secrets),
            "env_from": sorted(env_from),
            "mounted_volumes": sorted(mounted),
            "summary": (
                f"{len(configmaps)} ConfigMaps, {len(secrets)} Secrets (names only), "
                f"{len(mounted)} mounted volume(s)"
            ),
        }

    def get_security_posture(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        client = self._client(cluster)
        selector = self._selector(client, namespace, app_label)
        pods = client.items(f"/api/v1/namespaces/{namespace}/pods", labelSelector=selector)
        service_account = pods[0]["spec"].get("serviceAccountName", "default") if pods else "unknown"
        ns_labels = (client.get(f"/api/v1/namespaces/{namespace}")
                     .get("metadata", {}).get("labels") or {})
        enforce = ns_labels.get("pod-security.kubernetes.io/enforce")
        services = client.items(f"/api/v1/namespaces/{namespace}/services", labelSelector=selector)
        exposed = any(s.get("spec", {}).get("type") in ("LoadBalancer", "NodePort")
                      for s in services)
        ingresses = client.items(
            f"/apis/networking.k8s.io/v1/namespaces/{namespace}/ingresses", labelSelector=selector)
        tls = any(i.get("spec", {}).get("tls") for i in ingresses)
        external = bool(exposed or ingresses)
        return {
            "cluster": cluster, "namespace": namespace,
            "service_account": service_account,
            # A namespace with no Pod Security admission label runs at the
            # cluster default; that is not evidence of non-compliance, so the
            # flag stays true and the summary says what was actually seen.
            "psa_compliant": enforce in (None, "baseline", "restricted"),
            "scc": None,  # SecurityContextConstraints are OpenShift-only
            "exposure": "external" if external else "internal",
            "tls_cert_status": "terminated at ingress" if tls else "no TLS configured",
            "summary": (
                f"sa {service_account}, pod-security enforce={enforce or 'cluster default'}, "
                f"{'externally reachable' if external else 'cluster-internal'}"
            ),
        }
