"""Live OpenShift backend: real cluster state over the Kubernetes API.

Design contract: method names and RESULT SHAPES are identical to
cloudops.mockfleet.World (decision D6). The check batteries in
config/checks/*.yaml address these fields by dotted path and cannot tell
which backend answered.

Vanilla Kubernetes contract. The reference live fleet is kind, not OpenShift,
so ClusterVersion, ClusterOperator, MachineConfigPool and the OpenShift
node-approver CSR semantics simply do not exist there. Those four tools do
NOT error: they return the mock shape with health-neutral values plus
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
        """Namespace inventory. No MCP tool exposes this yet; live-smoke and
        manual investigation both want it, and context resolution will."""
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

    # -- namespace / application state ---------------------------------------

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
            # Additive detail: the mock world carries no per-container view,
            # and the live one is worth keeping for the evidence trail.
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
                # Volume fill needs kubelet volume stats; metrics belong to the
                # observability backend, so this stays honestly unknown here.
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
