"""Live smoke test: exercise the OpenShift backend against the real fleet.

Role: prove the stack answers with real cluster data before anyone starts it.
Runs the backend IN PROCESS, so it binds no ports and needs neither the
gateway nor the agent.

    make live-smoke          (or: uv run python -m cloudops.mcp_servers.live_smoke)

There is no Prometheus here: every reading below comes from a Kubernetes API
server. The registry probes go through the reg__* data-access lib, and they
SKIP rather than fail when Mongo is not up, so the smoke run is still useful
on a machine that has the kind fleet but not the registry.

The last block runs the real attestation battery from
config/checks/health_attestation.yaml through the real check engine against
the live backend, so the printed verdict is the verdict the agent would
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
from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend

HEALTHY_SPOKE = "acm-spoke-1a"
DEGRADED_SPOKE = "acm-spoke-2a"
APP_NAMESPACE = "payments-prod"
APP_LABEL = "payments-api"


class Skipped(Exception):
    """A probe that could not run because its dependency is absent.

    Distinct from a failure: the registry lands in parallel with this file, so
    "Mongo is not up" must read as a gap in coverage, not as a broken fleet.
    """


class Table:
    """Compact pass/fail/skip table with a non-zero exit on the first failure."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.rows.append((name, "PASS" if ok else "FAIL", detail))
        return ok

    def skip(self, name: str, detail: str) -> None:
        self.rows.append((name, "SKIP", detail))

    def run(self, name: str, fn: Any) -> Any:
        """Run one probe, turning an exception into a failed row rather than a
        traceback: a smoke run should report every check it managed."""
        try:
            ok, detail, value = fn()
        except Skipped as exc:
            self.skip(name, str(exc)[:110])
            return None
        except Exception as exc:  # noqa: BLE001 - the failure IS the result here
            self.check(name, False, f"{type(exc).__name__}: {exc}"[:110])
            return None
        self.check(name, ok, detail)
        return value

    def render(self) -> int:
        width = max(len(r[0]) for r in self.rows)
        colors = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m"}
        print()
        for name, status, detail in self.rows:
            print(f"  {colors[status]}{status}\033[0m  {name.ljust(width)}  {detail}")
        failed = sum(1 for _, status, _ in self.rows if status == "FAIL")
        skipped = sum(1 for _, status, _ in self.rows if status == "SKIP")
        passed = len(self.rows) - failed - skipped
        print(f"\n  {passed}/{len(self.rows) - skipped} checks passed"
              f"{f', {skipped} skipped' if skipped else ''}\n")
        return 1 if failed else 0


def _registry() -> Any:
    """The registry data-access lib, or Skipped when it cannot be used.

    Imported lazily and by name so this module keeps running on a checkout
    where the registry package has not landed yet.
    """
    try:
        from cloudops.registry import Registry  # type: ignore[attr-defined]
    except ImportError as exc:
        raise Skipped(f"registry lib unavailable ({exc})") from exc
    try:
        return Registry()
    except Exception as exc:  # noqa: BLE001 - Mongo down is a skip, not a failure
        raise Skipped(f"registry unreachable ({type(exc).__name__})") from exc


def main() -> int:
    settings = get_settings()
    fleet = LiveFleet()
    ocp = LiveOpenShiftBackend(fleet)
    t = Table()

    print(f"live smoke against {len(fleet.names())} clusters "
          f"(config: {settings.config_dir})")

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

    def namespaces() -> tuple[bool, str, Any]:
        result = ocp.get_namespaces(HEALTHY_SPOKE)
        names = {n["name"] for n in result["namespaces"]}
        return APP_NAMESPACE in names, f"{result['total']} namespaces", result

    t.run(f"namespaces {HEALTHY_SPOKE}", namespaces)

    # -- registry: candidates, then live verification -----------------------

    def registry_resolve() -> tuple[bool, str, Any]:
        result = _registry().resolve_entity(APP_LABEL)
        matches = result.get("matches", [])
        kinds = sorted({m["kind"] for m in matches})
        return bool(matches), f"{len(matches)} match(es), kinds {', '.join(kinds) or '-'}", result

    t.run(f"registry resolve {APP_LABEL}", registry_resolve)

    candidates = t.run("registry placements payments-api", lambda: _registry_placements(APP_LABEL))
    t.run("registry blast radius", lambda: _blast_radius(HEALTHY_SPOKE))

    # Placement verification runs whether or not the registry answered: when it
    # did not, the fleet's own clusters are the candidate list, which is what
    # context resolution falls back to reporting anyway.
    def verify() -> tuple[bool, str, Any]:
        pairs = (
            [(p["cluster"], p["namespace"]) for p in candidates]
            if candidates else [(HEALTHY_SPOKE, APP_NAMESPACE), (DEGRADED_SPOKE, APP_NAMESPACE)]
        )
        results = [ocp.verify_placement(c, ns, APP_LABEL) for c, ns in pairs]
        confirmed = [r for r in results if r["verified"]]
        detail = ", ".join(
            f"{r['cluster']}/{r['namespace']} {r['ready_count']}/{r['pod_count']} ready"
            for r in results
        )
        return len(confirmed) == 2, detail, results

    t.run(f"verify placement {APP_LABEL}", verify)

    def verify_absent() -> tuple[bool, str, Any]:
        """A namespace the app does not run in must come back verified=false,
        reachable=true. Absence and unreachability are different answers."""
        result = ocp.verify_placement(HEALTHY_SPOKE, "kube-system", APP_LABEL)
        ok = result["reachable"] and not result["verified"]
        return ok, f"kube-system: verified={result['verified']}", result

    t.run("verify placement rejects a wrong namespace", verify_absent)

    # -- capacity and autoscaling -------------------------------------------

    def capacity() -> tuple[bool, str, Any]:
        result = ocp.get_capacity(HEALTHY_SPOKE)
        ok = result["cpu_requests_ratio"] is not None and result["pod_count"] > 0
        return ok, result["summary"], result

    t.run("capacity", capacity)

    def autoscaling() -> tuple[bool, str, Any]:
        result = ocp.get_autoscaling(DEGRADED_SPOKE, APP_NAMESPACE, APP_LABEL)
        # The kind fleet deploys neither an HPA nor a PDB, so "none present"
        # is the correct answer; what is asserted is that both API groups
        # answered rather than erroring.
        return True, f"{result['hpa_summary']}; {result['pdb_summary']}", result

    t.run("autoscaling", autoscaling)

    # -- workload pathology -------------------------------------------------

    def healthy_app() -> tuple[bool, str, Any]:
        result = ocp.get_workloads(HEALTHY_SPOKE, APP_NAMESPACE, APP_LABEL)
        ok = result["replicas_summary"] == "2/2 ready" and not result["pods"]["crashloop"]
        return ok, f"{result['replicas_summary']}, no crashloops", result

    t.run(f"workloads {HEALTHY_SPOKE}", healthy_app)

    def degraded_app() -> tuple[bool, str, Any]:
        result = ocp.get_workloads(DEGRADED_SPOKE, APP_NAMESPACE, APP_LABEL)
        issues = [i for i in result["pods"]["container_issues"] if i["container"] == "ledger-sync"]
        ok = bool(result["replicas_mismatch"]) and bool(issues)
        reasons = sorted({str(i["reason"]) for i in issues})
        return ok, f"{result['replicas_summary']}, ledger-sync {'/'.join(reasons)}", result

    t.run(f"workloads {DEGRADED_SPOKE}", degraded_app)

    def events() -> tuple[bool, str, Any]:
        result = ocp.get_events(DEGRADED_SPOKE, APP_NAMESPACE)
        return result["warning_count"] > 0, (
            f"{result['warning_count']} Warning events, top reason "
            f"{result['warnings'][0]['reason'] if result['warnings'] else '-'}"
        ), result

    t.run(f"events {DEGRADED_SPOKE}", events)

    # -- the real attestation battery, end to end ---------------------------

    for cluster in (HEALTHY_SPOKE, DEGRADED_SPOKE):
        def attest(cluster: str = cluster) -> tuple[bool, str, Any]:
            verdict, signals = live_attestation(cluster, ocp, settings.config_dir)
            note = f"verdict {verdict.value}"
            if signals:
                note += f" ({len(signals)} signal(s): {signals[0][:60]})"
            return verdict.value in ("healthy", "maintenance"), note, verdict

        t.run(f"attestation {cluster}", attest)

    return t.render()


def _registry_placements(app_id: str) -> tuple[bool, str, Any]:
    result = _registry().find_placements(app_id=app_id)
    rows = list(result.get("placements", []))
    detail = ", ".join(f"{p['cluster']}/{p['namespace']}" for p in rows) or "none"
    return len(rows) >= 1, detail, rows


def _blast_radius(cluster: str) -> tuple[bool, str, Any]:
    result = _registry().blast_radius(cluster=cluster)
    return bool(result.get("apps")), str(result.get("summary", ""))[:110], result


def live_attestation(cluster: str, ocp: Any, config_dir: Any) -> tuple[Any, list[str]]:
    """Run the committed attestation battery against the live backend.

    Deliberately reuses the real engine (evaluate_check / derive_verdict) and
    the real battery file: a smoke test that reimplemented the rules would
    prove nothing about what the agent will conclude.
    """
    battery = AttestationBattery.model_validate(
        load_yaml(config_dir / "checks" / "health_attestation.yaml"))
    results = []
    for check in battery.checks:
        _, _, tool = check.tool.partition("__")
        args = {k: (cluster if v == "{{ cluster }}" else v) for k, v in check.args.items()}
        start = time.perf_counter()
        try:
            data, error = getattr(ocp, tool)(**args), None
        except Exception as exc:  # noqa: BLE001 - an errored check is a check result
            data, error = None, str(exc)[:200]
        rendered = check.model_copy(update={"args": args})
        results.append(evaluate_check(rendered, data, error, (time.perf_counter() - start) * 1000))
    return derive_verdict(results)


if __name__ == "__main__":
    sys.exit(main())
