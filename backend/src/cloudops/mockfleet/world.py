"""Deterministic synthetic fleet state.

Everything here is a pure function of three config files (fleet.yaml,
applications.yaml, scenario.yaml) plus fixed seeds, so mock-mode runs and
E2E tests are exactly reproducible. Anything not scripted in scenario.yaml
is healthy by construction.

Method return shapes ARE the MCP tool contracts: the mock and live backends
must return identical shapes (decision D6), and the check batteries in
config/checks/*.yaml address these fields by dotted path. Change a shape
here and you must change the battery and the live backend with it.
"""

from __future__ import annotations

import difflib
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cloudops.common.config import load_yaml

# Operators every OCP cluster runs; the critical tier mirrors the research
# notes (docs/research/openshift-health-checks-notes.md, A2).
CRITICAL_OPERATORS = [
    "etcd", "kube-apiserver", "kube-controller-manager", "kube-scheduler",
    "ingress", "dns", "network", "authentication", "machine-config", "monitoring",
]
OTHER_OPERATORS = [
    "console", "image-registry", "marketplace", "monitoring-plugin", "node-tuning",
    "openshift-apiserver", "openshift-controller-manager", "operator-lifecycle-manager",
    "service-ca", "storage", "cloud-credential", "cluster-autoscaler", "config-operator",
    "csi-snapshot-controller", "etcd-backup", "insights", "kube-storage-version-migrator",
    "machine-api", "machine-approver", "network-diagnostics", "olm", "route-controller",
    "baremetal",
]
ALL_OPERATORS = CRITICAL_OPERATORS + OTHER_OPERATORS


def _now() -> datetime:
    return datetime.now(UTC)


class World:
    """One coherent synthetic fleet, derived from config."""

    def __init__(self, fleet: dict[str, Any], apps: dict[str, Any], scenario: dict[str, Any]) -> None:
        self._clusters: dict[str, dict[str, Any]] = {}
        self._alias_index: dict[str, str] = {}
        self._apps: dict[str, dict[str, Any]] = {}
        self._cluster_faults: dict[str, dict[str, Any]] = scenario.get("clusters") or {}
        self._app_faults: dict[tuple[str, str], dict[str, Any]] = {
            (f["application"], f["cluster"]): f for f in (scenario.get("apps") or [])
        }

        for c in fleet.get("clusters", []):
            self._add_cluster(dict(c))
        self._synthesize(fleet.get("synthetic") or {})

        for a in apps.get("applications", []):
            self._apps[a["application"]] = a

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _add_cluster(self, c: dict[str, Any]) -> None:
        c.setdefault("aliases", [])
        c.setdefault("labels", {})
        c.setdefault("ring", c.get("environment", "prod"))
        self._clusters[c["name"]] = c
        self._alias_index[c["name"].lower()] = c["name"]
        for alias in c["aliases"]:
            self._alias_index[str(alias).lower()] = c["name"]

    def _synthesize(self, spec: dict[str, Any]) -> None:
        """Fill the registry to fleet scale with deterministic healthy clusters."""
        count = int(spec.get("count", 0))
        if count <= 0:
            return
        rng = random.Random(spec.get("seed", 1))
        envs = spec.get("environments", ["prod", "nonprod"])
        regions = spec.get("regions", ["us-east"])
        prefix = spec.get("name_prefix", "ocp")
        per_key: dict[str, int] = {}
        for _ in range(count):
            env = rng.choice(envs)
            region = rng.choice(regions)
            key = f"{prefix}-{env}-{region}"
            per_key[key] = per_key.get(key, 0) + 1
            name = f"{key}-{per_key[key]:02d}"
            self._add_cluster({
                "name": name,
                "environment": env,
                "region": region,
                "ring": env,
                "version": spec.get("version", "4.16.9"),
                "api_url": f"https://api.{name}.example.internal:6443",
                "console_url": f"https://console.{name}.example.internal",
                "synthetic": True,
            })

    @classmethod
    def from_config_dir(cls, config_dir: Path) -> World:
        return cls(
            fleet=load_yaml(config_dir / "fleet" / "fleet.yaml"),
            apps=load_yaml(config_dir / "fleet" / "applications.yaml"),
            scenario=load_yaml(config_dir / "mock" / "scenario.yaml"),
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _cluster(self, name: str) -> dict[str, Any]:
        c = self._clusters.get(name)
        if c is None:
            raise ValueError(f"unknown cluster: {name!r}; use resolve_cluster first")
        return c

    def _fault(self, cluster: str) -> dict[str, Any]:
        return self._cluster_faults.get(cluster) or {}

    def _app_fault(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        for (app, cl), fault in self._app_faults.items():
            if cl == cluster and fault.get("namespace") == namespace:
                entry = self._apps.get(app)
                if entry and entry.get("app_label") == app_label:
                    return fault
        return {}

    def _rng(self, *key: str) -> random.Random:
        return random.Random("mockfleet:" + ":".join(key))

    def _instances_in(self, cluster: str, namespace: str) -> list[tuple[str, dict[str, Any]]]:
        """(application, instance) pairs placed in a cluster+namespace."""
        out = []
        for app, entry in self._apps.items():
            for inst in entry.get("instances", []):
                if inst["cluster"] == cluster and inst["namespace"] == namespace:
                    out.append((app, inst))
        return out

    # ------------------------------------------------------------------
    # OpenShift MCP: fleet resolution
    # ------------------------------------------------------------------

    def resolve_cluster(self, query: str) -> dict[str, Any]:
        """Name, alias, label selector (k=v), or fuzzy text -> candidates."""
        q = query.strip().lower()
        matches: list[str] = []
        if q in self._alias_index:
            matches = [self._alias_index[q]]
        elif "=" in q:
            key, _, val = q.partition("=")
            matches = [
                name for name, c in self._clusters.items()
                if str(c.get("labels", {}).get(key.strip(), "")).lower() == val.strip()
                or str(c.get(key.strip(), "")).lower() == val.strip()
            ]
        else:
            matches = [name for name in self._clusters if q in name.lower()]
            if not matches:
                matches = difflib.get_close_matches(q, list(self._clusters), n=5, cutoff=0.6)
        matches = sorted(matches)[:10]
        return {
            "query": query,
            "count": len(matches),
            "matches": [self._cluster_summary(m) for m in matches],
            "suggestion": None if matches else "no cluster matched; try list_clusters with an environment or region filter",
        }

    def _cluster_summary(self, name: str) -> dict[str, Any]:
        c = self._cluster(name)
        return {
            "name": name,
            "environment": c.get("environment"),
            "region": c.get("region"),
            "ring": c.get("ring"),
            "version": c.get("version"),
            "aliases": c.get("aliases", []),
            "labels": c.get("labels", {}),
        }

    def list_clusters(
        self, environment: str | None = None, region: str | None = None,
        page: int = 1, page_size: int = 50,
    ) -> dict[str, Any]:
        names = [
            n for n, c in sorted(self._clusters.items())
            if (environment is None or c.get("environment") == environment)
            and (region is None or c.get("region") == region)
        ]
        start = max(page - 1, 0) * page_size
        return {
            "total": len(names),
            "page": page,
            "page_size": page_size,
            "clusters": [self._cluster_summary(n) for n in names[start : start + page_size]],
        }

    # ------------------------------------------------------------------
    # OpenShift MCP: cluster state
    # ------------------------------------------------------------------

    def get_cluster_info(self, cluster: str) -> dict[str, Any]:
        c = self._cluster(cluster)
        rng = self._rng("latency", cluster)
        return {
            "cluster": cluster,
            "reachable": True,
            "latency_ms": rng.randint(40, 180),
            "version": c.get("version"),
            "channel": f"stable-{str(c.get('version', '4.16'))[:4]}",
            "api_url": c.get("api_url"),
            "console_url": c.get("console_url"),
            "region": c.get("region"),
            "environment": c.get("environment"),
            "labels": c.get("labels", {}),
        }

    def get_cluster_version(self, cluster: str) -> dict[str, Any]:
        c = self._cluster(cluster)
        fault = self._fault(cluster)
        upgrading_to = fault.get("upgrading_to")
        version = str(c.get("version"))
        history: list[dict[str, Any]] = [{"version": version, "state": "Completed",
                    "completed_at": (_now() - timedelta(days=11)).isoformat()}]
        if upgrading_to:
            history.insert(0, {"version": upgrading_to, "state": "Partial", "completed_at": None})
        return {
            "cluster": cluster,
            "version": version,
            "desired_version": upgrading_to or version,
            "channel": f"stable-{version[:4]}",
            "available": True,
            "progressing": bool(upgrading_to),
            "failing": bool(fault.get("clusterversion_failing", False)),
            "progressing_message": f"Working towards {upgrading_to}" if upgrading_to else None,
            "history": history,
        }

    def get_cluster_operators(self, cluster: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._fault(cluster)
        degraded = list(fault.get("cluster_operators_degraded") or [])
        if fault.get("monitoring_degraded"):
            degraded.append({"name": "monitoring", "since": "recent",
                             "message": "prometheus-k8s pods not ready"})
        degraded_names = [d["name"] for d in degraded]
        progressing = []
        if fault.get("upgrading_to"):
            progressing = ["kube-apiserver", "kube-controller-manager", "config-operator"]
        return {
            "cluster": cluster,
            "total": len(ALL_OPERATORS),
            "available_count": len(ALL_OPERATORS),  # degraded operators are still Available in this world
            "degraded": degraded,
            "critical_degraded": [n for n in degraded_names if n in CRITICAL_OPERATORS],
            "progressing": progressing,
            "unavailable": [],
        }

    def get_nodes(self, cluster: str) -> dict[str, Any]:
        c = self._cluster(cluster)
        fault = self._fault(cluster)
        rng = self._rng("nodes", cluster)
        workers = rng.randint(6, 40) if c.get("environment") == "prod" else rng.randint(3, 12)
        total = 3 + workers  # 3 masters
        cordoned_count = int(fault.get("cordoned_nodes", 0))
        # MCP-updating clusters cordon nodes as they roll; a plain upgrade cordons one.
        if not cordoned_count and fault.get("upgrading_to"):
            cordoned_count = 1
        cordoned = [
            {"name": f"worker-{i:02d}.{cluster}", "mcp": "worker"} for i in range(cordoned_count)
        ]
        not_ready = [
            {"name": f"worker-{90 + i}.{cluster}", "since": "10m", "reason": "KubeletNotReady"}
            for i in range(int(fault.get("not_ready_nodes", 0)))
        ]
        return {
            "cluster": cluster,
            "total": total,
            "ready": total - len(not_ready) - len(cordoned),
            "not_ready": not_ready,
            "cordoned": cordoned,
            "pressure": list(fault.get("node_pressure") or []),
            "roles": {"master": 3, "worker": workers},
        }

    def get_machine_config_pools(self, cluster: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._fault(cluster)
        pools: list[dict[str, Any]] = []
        for name in ("master", "worker"):
            pools.append({
                "name": name, "updated": True, "updating": False, "degraded": False,
                "paused": False, "machine_count": 3 if name == "master" else 6,
                "ready_count": 3 if name == "master" else 6,
                "updated_count": 3 if name == "master" else 6, "degraded_count": 0,
            })
        for override in fault.get("machine_config_pools") or []:
            for pool in pools:
                if pool["name"] == override.get("name"):
                    pool.update(override)
                    if pool.get("updating"):
                        pool["updated"] = False
        any_updating = any(p["updating"] for p in pools)
        any_degraded = any(p["degraded"] for p in pools)
        any_paused = any(p["paused"] for p in pools)
        summary = ", ".join(
            f"{p['name']} {'updating ' + str(p['updated_count']) + '/' + str(p['machine_count']) if p['updating'] else ('degraded' if p['degraded'] else 'updated')}"
            for p in pools
        )
        return {
            "cluster": cluster, "pools": pools, "any_updating": any_updating,
            "any_degraded": any_degraded, "any_paused": any_paused, "summary": summary,
        }

    def get_pending_csrs(self, cluster: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._fault(cluster)
        pending = [
            {"name": f"csr-{i:04x}", "type": "kubernetes.io/kubelet-serving", "age_minutes": 42}
            for i in range(int(fault.get("pending_csrs", 0)))
        ]
        return {"cluster": cluster, "pending_count": len(pending), "pending": pending}

    # ------------------------------------------------------------------
    # OpenShift MCP: namespace / application state
    # ------------------------------------------------------------------

    def get_workloads(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._app_fault(cluster, namespace, app_label)
        env = self._cluster(cluster).get("environment")
        desired = int(fault.get("pods_desired", 6 if env == "prod" else 2))
        ready = int(fault.get("pods_ready", desired))
        crashloop = bool(fault.get("crashloop"))
        oomkilled = bool(fault.get("oomkilled"))
        restarts = int(fault.get("restarts_last_hour", 0))
        rng = self._rng("workloads", cluster, namespace, app_label)
        image_tag = f"1.{rng.randint(20, 30)}.{rng.randint(0, 9)}"
        suffix = f"{rng.randrange(16**4):04x}"
        pod_names = [f"{app_label}-{suffix}-{i}" for i in range(desired)]

        workload = {
            "name": app_label, "kind": "Deployment", "desired": desired, "ready": ready,
            "available": ready, "updated": desired, "strategy": "RollingUpdate",
            "image": f"registry.example.internal/{app_label}",
            "image_tag": image_tag, "rollout_healthy": ready == desired,
            "age_days": rng.randint(30, 400),
        }
        cron_missed = int(fault.get("cronjob_missed_schedules", 0))
        workloads = [workload]
        if cron_missed:
            workloads.append({
                "name": f"{app_label}-schedule", "kind": "CronJob", "desired": 1, "ready": 1,
                "available": 1, "updated": 1, "strategy": "-",
                "image": f"registry.example.internal/{app_label}", "image_tag": image_tag,
                "rollout_healthy": True, "age_days": rng.randint(30, 400),
                "missed_schedules_last_5": cron_missed,
            })

        history = [
            {"revision": 3, "image_tag": image_tag, "deployed_at": (_now() - timedelta(days=2)).isoformat(), "status": "complete"},
            {"revision": 2, "image_tag": f"1.{rng.randint(10, 19)}.4", "deployed_at": (_now() - timedelta(days=16)).isoformat(), "status": "complete"},
        ]
        mismatch = [workload["name"]] if ready < desired else []
        crashloop_pods = pod_names[:2] if crashloop else []
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "workloads": workloads,
            "replicas_mismatch": mismatch,
            "replicas_summary": f"{ready}/{desired} ready",
            "rollout_summary": "no rollout in progress",
            "rollouts_in_progress": [],
            "failed_rollouts": [],
            "single_replica_workloads": [w["name"] for w in workloads if w["kind"] == "Deployment" and w["desired"] == 1 and env == "prod"],
            "release_summary": f"rev 3 ({image_tag}) deployed 2d ago",
            "history": history,
            "pods": {
                "total": desired,
                "running": ready,
                "pending": pod_names[ready:] if ready < desired and not crashloop else [],
                "crashloop": crashloop_pods,
                "image_pull_errors": [],
                "oomkilled_recent": crashloop_pods if oomkilled else [],
                "restarts_last_hour": restarts,
                "probes_failing": crashloop_pods,
            },
        }

    def get_events(self, cluster: str, namespace: str) -> dict[str, Any]:
        self._cluster(cluster)
        warnings: list[dict[str, Any]] = []
        for app, _inst in self._instances_in(cluster, namespace):
            entry = self._apps[app]
            fault = self._app_fault(cluster, namespace, entry.get("app_label", app))
            if fault.get("crashloop"):
                warnings.append({"reason": "BackOff", "object": f"pod/{entry['app_label']}-*",
                                 "count": 47, "last_seen": "30s",
                                 "message": "Back-off restarting failed container"})
            if fault.get("oomkilled"):
                warnings.append({"reason": "OOMKilling", "object": f"pod/{entry['app_label']}-*",
                                 "count": 2, "last_seen": "12m",
                                 "message": "Memory cgroup out of memory: Killed process"})
            if fault.get("cronjob_missed_schedules"):
                warnings.append({"reason": "FailedNeedsStart", "object": f"cronjob/{entry['app_label']}-schedule",
                                 "count": fault["cronjob_missed_schedules"], "last_seen": "1h",
                                 "message": "Cannot determine if job needs to be started: too many missed start times"})
        return {"cluster": cluster, "namespace": namespace,
                "warnings": warnings, "warning_count": len(warnings)}

    def get_quotas(self, cluster: str, namespace: str) -> dict[str, Any]:
        self._cluster(cluster)
        rng = self._rng("quota", cluster, namespace)
        used_ratio = round(rng.uniform(0.35, 0.7), 2)
        quotas = [
            {"name": "compute", "resource": "requests.cpu", "used": f"{int(used_ratio * 20)}", "hard": "20", "ratio": used_ratio},
            {"name": "compute", "resource": "requests.memory", "used": f"{int(used_ratio * 64)}Gi", "hard": "64Gi", "ratio": used_ratio},
        ]
        return {
            "cluster": cluster, "namespace": namespace, "quotas": quotas,
            "near_limit": [q for q in quotas if float(str(q["ratio"])) > 0.9],
            "limit_ranges": ["default-limits"],
            "summary": f"cpu {int(used_ratio * 100)}% / mem {int(used_ratio * 100)}% of quota",
        }

    def get_network(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        rng = self._rng("network", cluster, namespace, app_label)
        expiry_days = rng.randint(60, 320)
        routes = [{
            "name": app_label, "host": f"{app_label}-{namespace}.apps.{cluster}.example.internal",
            "tls_termination": "edge", "cert_expiry_days": expiry_days,
        }]
        return {
            "cluster": cluster, "namespace": namespace,
            "services": [{"name": app_label, "type": "ClusterIP", "ports": [8080]}],
            "routes": routes,
            "routes_summary": f"1 route, cert expires in {expiry_days}d",
            "expiring_certs": [r["name"] for r in routes if int(str(r["cert_expiry_days"])) < 30],
            "network_policies_count": 2,
            "dns_healthy": True,
        }

    def get_pvcs(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        # Most demo apps are stateless; audit-log carries a PVC for realism.
        pvcs = []
        if app_label == "audit-log":
            pvcs = [{"name": "audit-buffer", "status": "Bound", "storage_class": "gp3-csi",
                     "capacity_gb": 50, "used_ratio": 0.44, "growth_trend": "stable"}]
        return {
            "cluster": cluster, "namespace": namespace, "pvcs": pvcs,
            "unbound": [p["name"] for p in pvcs if p["status"] != "Bound"],
            "near_full": [p["name"] for p in pvcs if float(str(p["used_ratio"])) > 0.9],
            "summary": f"{len(pvcs)} PVC(s)" if pvcs else "no persistent volumes",
        }

    def get_configuration(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        return {
            "cluster": cluster, "namespace": namespace,
            "configmaps": [f"{app_label}-config", f"{app_label}-feature-flags"],
            # Names only, never data: NFR-LOG-3.
            "secrets": [f"{app_label}-db-credentials", f"{app_label}-api-keys"],
            "env_from": [f"configmap/{app_label}-config"],
            "mounted_volumes": [f"secret/{app_label}-db-credentials"],
            "summary": "2 ConfigMaps, 2 Secrets (names only), 1 mounted secret volume",
        }

    def get_security_posture(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        return {
            "cluster": cluster, "namespace": namespace,
            "service_account": f"{app_label}-sa",
            "psa_compliant": True,
            "scc": "restricted-v2",
            "exposure": "internal",
            "tls_cert_status": "valid",
            "summary": f"sa {app_label}-sa, restricted-v2, PSA compliant",
        }

    def get_app_registry_entry(self, application: str) -> dict[str, Any]:
        entry = self._apps.get(application)
        if entry is None:
            return {"application": application, "found": False}
        # Copy without mock-only placement hints; placement is discovered
        # through obs__find_app_placements (FR-CTX-2).
        out = {k: v for k, v in entry.items() if k != "instances"}
        out["found"] = True
        return out

    # ------------------------------------------------------------------
    # Observability MCP
    # ------------------------------------------------------------------

    def find_app_placements(self, app_label: str) -> dict[str, Any]:
        placements = []
        for app, entry in self._apps.items():
            if entry.get("app_label") != app_label:
                continue
            for inst in entry.get("instances", []):
                fault = self._app_fault(inst["cluster"], inst["namespace"], app_label)
                env = inst.get("environment")
                pod_count = int(fault.get("pods_desired", 6 if env == "prod" else 2))
                placements.append({
                    "cluster": inst["cluster"], "namespace": inst["namespace"],
                    "environment": env, "pod_count": pod_count, "application": app,
                })
        return {"app_label": app_label, "placements": placements}

    def get_firing_alerts(self, cluster: str, namespace: str | None = None) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._fault(cluster)
        critical: list[dict[str, Any]] = []
        warning: list[dict[str, Any]] = []
        for alert in fault.get("firing_alerts") or []:
            bucket = critical if alert.get("severity") == "critical" else warning
            bucket.append({k: v for k, v in alert.items() if k != "severity"})
        # Namespace-scoped app alerts derived from app faults.
        for (app, cl), afault in self._app_faults.items():
            if cl != cluster:
                continue
            ns = afault.get("namespace")
            if namespace is not None and ns != namespace:
                continue
            if afault.get("crashloop"):
                critical.append({"name": "KubePodCrashLooping", "namespace": ns,
                                 "since": "22m", "summary": f"{app} pods crash-looping"})
            if afault.get("error_rate", 0) > 0.05:
                warning.append({"name": "AppErrorBudgetBurn", "namespace": ns,
                                "since": "18m", "summary": f"{app} error rate elevated"})
        if namespace is not None:
            critical = [a for a in critical if a.get("namespace") == namespace]
            warning = [a for a in warning if a.get("namespace") == namespace]
        watchdog_present = not fault.get("watchdog_absent", False)
        monitoring_healthy = not fault.get("monitoring_degraded", False)
        return {
            "cluster": cluster, "namespace": namespace,
            "critical": critical, "warning": warning, "info_count": 1,
            "watchdog_present": watchdog_present,
            "monitoring_healthy": monitoring_healthy,
            "cert_expiry_alerts": list(fault.get("cert_expiry_alerts") or []),
            "summary": f"{len(critical)} critical, {len(warning)} warning firing",
        }

    def get_etcd_health(self, cluster: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._fault(cluster)
        rng = self._rng("etcd", cluster)
        return {
            "cluster": cluster,
            "has_leader": not fault.get("etcd_no_leader", False),
            "quorum": not fault.get("etcd_quorum_lost", False),
            "members_expected": 3,
            "members_up": 3 - int(fault.get("etcd_members_down", 0)),
            "leader_changes_15m": 0,
            "fsync_p99_ms": float(fault.get("etcd_fsync_p99_ms", round(rng.uniform(1.5, 6.0), 1))),
        }

    def get_apiserver_slo(self, cluster: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._fault(cluster)
        rng = self._rng("apiserver", cluster)
        error_rate = float(fault.get("apiserver_error_rate", round(rng.uniform(0.0001, 0.004), 4)))
        return {
            "cluster": cluster,
            "error_rate_5xx": error_rate,
            "burn_rate_1h": round(error_rate / 0.01, 2),
            "healthy": error_rate <= 0.01,
        }

    def get_capacity_summary(self, cluster: str) -> dict[str, Any]:
        c = self._cluster(cluster)
        rng = self._rng("capacity", cluster)
        cpu = round(rng.uniform(0.45, 0.72), 2)
        mem = round(rng.uniform(0.4, 0.68), 2)
        nodes = self.get_nodes(cluster)
        pod_capacity = nodes["total"] * 250
        pod_count = int(pod_capacity * rng.uniform(0.2, 0.5))
        fault = self._fault(cluster)
        fits = not fault.get("overcommitted", False)
        return {
            "cluster": cluster,
            "cpu_requests_ratio": cpu, "memory_requests_ratio": mem,
            "pod_count": pod_count, "pod_capacity": pod_capacity,
            "pod_ratio": round(pod_count / pod_capacity, 2),
            "fits_minus_one_node": fits,
            "summary": f"cpu req {int(cpu * 100)}%, mem req {int(mem * 100)}% of allocatable ({c.get('environment')})",
        }

    def get_golden_signals(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._app_fault(cluster, namespace, app_label)
        rng = self._rng("golden", cluster, namespace, app_label)
        slo_ms = 400
        for entry in self._apps.values():
            if entry.get("app_label") == app_label and "latency" in str(entry.get("slo", "")):
                # crude parse of "p95 latency < 400ms" style SLO strings
                for token in str(entry["slo"]).replace(",", " ").split():
                    if token.endswith("ms") and token[:-2].isdigit():
                        slo_ms = int(token[:-2])
        base_latency = slo_ms * rng.uniform(0.45, 0.7)
        drift = 1 + float(fault.get("latency_drift_pct", 0)) / 100.0
        p95 = round(base_latency * drift, 1)
        error_rate = float(fault.get("error_rate", round(rng.uniform(0.0005, 0.006), 4)))
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "request_rate": round(rng.uniform(40, 400), 1),
            "error_rate": error_rate,
            "p95_latency_ms": p95,
            "latency_slo_ms": slo_ms,
            "latency_breach": p95 > slo_ms,
            "instrumented": True,
        }

    def get_workload_usage(self, cluster: str, namespace: str, app_label: str) -> dict[str, Any]:
        self._cluster(cluster)
        fault = self._app_fault(cluster, namespace, app_label)
        rng = self._rng("usage", cluster, namespace, app_label)
        limits_mi = 512
        mem_ratio = float(fault.get("memory_p95_ratio", round(rng.uniform(0.45, 0.75), 2)))
        p95_mi = int(limits_mi * mem_ratio)
        throttling = float(fault.get("throttling_ratio", round(rng.uniform(0.0, 0.08), 2)))
        hpa_present = app_label in {"payments-api", "checkout", "pricing"}
        at_max = bool(fault.get("hpa_at_max", False))
        return {
            "cluster": cluster, "namespace": namespace, "app_label": app_label,
            "cpu": {"requests_millicores": 500, "limits_millicores": 1000,
                    "usage_ratio_of_requests": round(rng.uniform(0.3, 0.8), 2)},
            "memory": {"requests_mi": 256, "limits_mi": limits_mi,
                       "p95_usage_mi": p95_mi, "usage_ratio_of_limit": mem_ratio},
            "memory_summary": f"p95 {p95_mi}Mi of {limits_mi}Mi limit ({int(mem_ratio * 100)}%)",
            "throttling_ratio": throttling,
            "hpa": {"present": hpa_present, "min": 2, "max": 8,
                    "current": 8 if at_max else 4, "at_max": at_max,
                    "min_eq_max": False, "maxed_out": at_max},
            "hpa_summary": ("HPA 8/8 at max" if at_max else "HPA 4/8") if hpa_present else "no HPA",
            "pdb": {"present": True, "disruptions_allowed": 0 if fault.get("crashloop") else 1,
                    "blocked": bool(fault.get("crashloop"))},
            "pdb_summary": "0 disruptions allowed" if fault.get("crashloop") else "1 disruption allowed",
        }

    def get_dashboard_links(self, cluster: str, namespace: str) -> dict[str, Any]:
        base = "https://grafana.example.internal/d"
        return {"links": [
            {"title": "Namespace overview", "url": f"{base}/k8s-ns/{cluster}/{namespace}"},
            {"title": "Workload golden signals", "url": f"{base}/golden/{cluster}/{namespace}"},
            {"title": "Cluster health", "url": f"{base}/cluster/{cluster}"},
        ]}

    def query_instant(self, promql: str) -> dict[str, Any]:
        """A deliberately small PromQL façade for interactive investigation.

        Mock mode recognizes a handful of metric families and answers from
        world state; anything else returns an empty result with a note so
        the LLM knows to fall back to the purpose-built tools. Live mode
        proxies the query to Thanos verbatim.
        """
        recognized = {
            "kube_pod_container_status_restarts_total": "restart counters",
            "container_memory_working_set_bytes": "memory working set",
            "kube_pod_labels": "app placement series",
            "ALERTS": "firing alerts",
        }
        for metric, kind in recognized.items():
            if metric in promql:
                return {
                    "promql": promql,
                    "note": f"mock evaluator matched {kind}; use the purpose-built tools for richer summaries",
                    "result": [{"metric": {"__name__": metric}, "value": [int(_now().timestamp()), "1"]}],
                }
        return {
            "promql": promql,
            "note": "mock evaluator does not model this series; try get_golden_signals / get_workload_usage / get_firing_alerts",
            "result": [],
        }
