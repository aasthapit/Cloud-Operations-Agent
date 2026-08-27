"""Live-mode smoke test: exercise both live backends against the real fleet.

Role: prove that CLOUDOPS_BACKEND_MODE=live answers with real cluster data
before anyone starts the stack. Runs the backends IN PROCESS, so it binds no
ports and needs neither the gateway nor the agent.

    make live-smoke          (or: uv run python -m cloudops.mcp_servers.live_smoke)

The last block runs the real attestation battery from
config/checks/health_attestation.yaml through the real check engine against
the live backends, so the printed verdict is the verdict the agent would
reach. Exit status is non-zero if any row fails.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from cloudops.agent.checks import derive_verdict, evaluate_check
from cloudops.agent.models import AttestationBattery
from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings
from cloudops.mcp_servers.live_fleet import LiveFleet
from cloudops.mcp_servers.observability.live import LiveObservabilityBackend
from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend

HEALTHY_SPOKE = "acm-spoke-1a"
DEGRADED_SPOKE = "acm-spoke-2a"


class Table:
    """Compact pass/fail table with a non-zero exit on the first failure."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.rows.append((name, ok, detail))
        return ok

    def run(self, name: str, fn: Any) -> Any:
        """Run one probe, turning an exception into a failed row rather than a
        traceback: a smoke run should report every check it managed."""
        try:
            ok, detail, value = fn()
        except Exception as exc:  # noqa: BLE001 - the failure IS the result here
            self.check(name, False, f"{type(exc).__name__}: {exc}"[:110])
            return None
        self.check(name, ok, detail)
        return value

    def render(self) -> int:
        width = max(len(r[0]) for r in self.rows)
        print()
        for name, ok, detail in self.rows:
            mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
            print(f"  {mark}  {name.ljust(width)}  {detail}")
        failed = sum(1 for _, ok, _ in self.rows if not ok)
        print(f"\n  {len(self.rows) - failed}/{len(self.rows)} checks passed\n")
        return 1 if failed else 0


def main() -> int:
    settings = get_settings()
    fleet = LiveFleet()
    ocp = LiveOpenShiftBackend(fleet)
    obs = LiveObservabilityBackend(fleet)
    t = Table()

    print(f"live smoke against {len(fleet.names())} clusters "
          f"(config: {settings.config_dir}, mode: {settings.cloudops_backend_mode})")

    # -- fleet resolution ---------------------------------------------------

    def resolve() -> tuple[bool, str, Any]:
        result = ocp.resolve_cluster(HEALTHY_SPOKE)
        match = result["matches"][0] if result["matches"] else {}
        ok = result["count"] == 1 and match.get("name") == HEALTHY_SPOKE
        return ok, f"{result['count']} match, version {match.get('version')}", result

    t.run(f"resolve {HEALTHY_SPOKE}", resolve)

    def resolve_by_label() -> tuple[bool, str, Any]:
        result = ocp.resolve_cluster("role=spoke")
        names = [m["name"] for m in result["matches"]]
        return result["count"] == 4, f"role=spoke -> {', '.join(names)}", result

    t.run("resolve by label", resolve_by_label)

    # -- per-cluster reachability and n/a contract --------------------------

    for cluster in fleet.names():
        def probe(cluster: str = cluster) -> tuple[bool, str, Any]:
            info = ocp.get_cluster_info(cluster)
            nodes = ocp.get_nodes(cluster)
            ok = bool(info["reachable"]) and nodes["ready"] >= 1
            return ok, (f"{info['version']} | {nodes['ready']}/{nodes['total']} nodes ready "
                        f"| {info['latency_ms']}ms"), nodes

        t.run(f"cluster {cluster}", probe)

    def na_contract() -> tuple[bool, str, Any]:
        results = {
            "ClusterVersion": ocp.get_cluster_version(HEALTHY_SPOKE),
            "ClusterOperators": ocp.get_cluster_operators(HEALTHY_SPOKE),
            "MachineConfigPools": ocp.get_machine_config_pools(HEALTHY_SPOKE),
            "PendingCSRs": ocp.get_pending_csrs(HEALTHY_SPOKE),
        }
        ok = all(r["applicable"] is False and r["not_applicable_reason"] for r in results.values())
        return ok, f"{', '.join(results)} report applicable=false", results

    t.run("OpenShift-only tools n/a", na_contract)

    # -- observability ------------------------------------------------------

    def watchdog() -> tuple[bool, str, Any]:
        missing = [c for c in fleet.names() if not obs.get_firing_alerts(c)["watchdog_present"]]
        return not missing, (
            "Watchdog firing on all six clusters" if not missing
            else "missing on " + ", ".join(missing)
        ), missing

    t.run("Watchdog dead man's switch", watchdog)

    def prometheus_query() -> tuple[bool, str, Any]:
        result = obs.query_instant(f'up{{cluster="{HEALTHY_SPOKE}"}}')
        # The selector routes the query; Prometheus itself does not stamp the
        # external label onto local query results, so the count is the signal.
        return len(result["result"]) >= 3, f"{len(result['result'])} up series", result

    t.run("Prometheus instant query", prometheus_query)

    def placement() -> tuple[bool, str, Any]:
        result = obs.find_app_placements("payments-api")
        clusters = sorted(p["cluster"] for p in result["placements"])
        ok = clusters == sorted([HEALTHY_SPOKE, DEGRADED_SPOKE])
        pods = sum(int(p["pod_count"]) for p in result["placements"])
        return ok, f"{', '.join(clusters)} ({pods} pods)", result

    t.run("placement discovery payments-api", placement)

    def placement_other() -> tuple[bool, str, Any]:
        result = obs.find_app_placements("inventory-sync")
        rows = [f"{p['cluster']}/{p['namespace']}" for p in result["placements"]]
        return len(rows) == 1, ", ".join(rows) or "not found", result

    t.run("placement discovery inventory-sync", placement_other)

    def capacity() -> tuple[bool, str, Any]:
        result = obs.get_capacity_summary(HEALTHY_SPOKE)
        ok = result["cpu_requests_ratio"] is not None and result["pod_count"] is not None
        return ok, result["summary"], result

    t.run("capacity summary", capacity)

    # -- workload pathology -------------------------------------------------

    def healthy_app() -> tuple[bool, str, Any]:
        result = ocp.get_workloads(HEALTHY_SPOKE, "payments-prod", "payments-api")
        ok = result["replicas_summary"] == "2/2 ready" and not result["pods"]["crashloop"]
        return ok, f"{result['replicas_summary']}, no crashloops", result

    t.run(f"workloads {HEALTHY_SPOKE}", healthy_app)

    def degraded_app() -> tuple[bool, str, Any]:
        result = ocp.get_workloads(DEGRADED_SPOKE, "payments-prod", "payments-api")
        issues = [i for i in result["pods"]["container_issues"] if i["container"] == "ledger-sync"]
        ok = bool(result["replicas_mismatch"]) and bool(issues)
        reasons = sorted({str(i["reason"]) for i in issues})
        return ok, f"{result['replicas_summary']}, ledger-sync {'/'.join(reasons)}", result

    t.run(f"workloads {DEGRADED_SPOKE}", degraded_app)

    def events() -> tuple[bool, str, Any]:
        result = ocp.get_events(DEGRADED_SPOKE, "payments-prod")
        return result["warning_count"] > 0, (
            f"{result['warning_count']} Warning events, top reason "
            f"{result['warnings'][0]['reason'] if result['warnings'] else '-'}"
        ), result

    t.run(f"events {DEGRADED_SPOKE}", events)

    # -- the real attestation battery, end to end ---------------------------

    for cluster in (HEALTHY_SPOKE, DEGRADED_SPOKE):
        def attest(cluster: str = cluster) -> tuple[bool, str, Any]:
            verdict, signals = live_attestation(cluster, ocp, obs, settings.config_dir)
            note = f"verdict {verdict.value}"
            if signals:
                note += f" ({len(signals)} signal(s): {signals[0][:60]})"
            return verdict.value in ("healthy", "maintenance"), note, verdict

        t.run(f"attestation {cluster}", attest)

    return t.render()


def live_attestation(cluster: str, ocp: Any, obs: Any, config_dir: Any) -> tuple[Any, list[str]]:
    """Run the committed attestation battery against the live backends.

    Deliberately reuses the real engine (evaluate_check / derive_verdict) and
    the real battery file: a smoke test that reimplemented the rules would
    prove nothing about what the agent will conclude.
    """
    battery = AttestationBattery.model_validate(
        load_yaml(config_dir / "checks" / "health_attestation.yaml"))
    backends = {"ocp__": ocp, "obs__": obs}
    results = []
    for check in battery.checks:
        prefix, _, tool = check.tool.partition("__")
        backend = backends[prefix + "__"]
        args = {k: (cluster if v == "{{ cluster }}" else v) for k, v in check.args.items()}
        start = time.perf_counter()
        try:
            data, error = getattr(backend, tool)(**args), None
        except Exception as exc:  # noqa: BLE001 - an errored check is a check result
            data, error = None, str(exc)[:200]
        rendered = check.model_copy(update={"args": args})
        results.append(evaluate_check(rendered, data, error, (time.perf_counter() - start) * 1000))
    return derive_verdict(results)


if __name__ == "__main__":
    sys.exit(main())
