"""Turn a scenario's declared fleet and registry into a bootable world.

Two artifacts come out of a scenario:

- a ``{cluster: ClusterFixture}`` map, built from the shared canned fleet in
  ``cloudops.testkit`` plus whatever the scenario declares on top. Cluster
  KINDS are the vocabulary, and each one is defined here exactly once, so
  "degraded-crashloop" means the same thing in every suite.
- a config plane on disk: a copy of the committed ``config/`` with the
  registry MCP pointed at this run's port and, when the scenario overrides
  it, a rewritten ``fleet/applications.yaml``. That one file is both what the
  orchestrator reads for ownership and what the seeder loads into the
  scenario's in-memory Mongo, so a scenario cannot describe an application
  the registry disagrees with.

The kinds, and what each one is for:

  healthy             two ready nodes; the verdict every other kind is read against
  degraded-crashloop  a NotReady worker, and its application pods crash-looping:
                      the platform-and-application case triage exists to separate
  cordoned            an unschedulable worker -> maintenance, not degraded
  pressure            a worker reporting MemoryPressure -> a warn signal that
                      must NOT flip the verdict
  unreachable         an API server that refuses the connection -> unattestable,
                      which is the verdict that means "could not be asked"
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from cloudops.common.config import load_yaml
from cloudops.evals.suite import Case, ClusterSpec, Fleet, NamespaceSpec, Registry
from cloudops.testkit import (
    ClusterFixture,
    default_world,
    deployment,
    event,
    node,
    pod,
    point_registry_at,
    replicaset,
)
from cloudops.testkit.services import CONFIG_DIR


def _nodes_for(kind: str) -> list[dict[str, Any]]:
    if kind == "degraded-crashloop":
        return [node("cp-1"), node("worker-1", role="worker", ready=False)]
    if kind == "cordoned":
        return [node("cp-1"), node("worker-1", role="worker", unschedulable=True)]
    if kind == "pressure":
        return [node("cp-1"),
                node("worker-1", role="worker", pressure=("MemoryPressure",))]
    return [node("cp-1"), node("worker-1", role="worker")]


def _fill_namespace(
    fixture: ClusterFixture, name: str, spec: NamespaceSpec, crashloop: bool
) -> None:
    label = spec.app_label
    ns = fixture.ns(name)
    if crashloop:
        ns.pods = [pod(f"{label}-1", label, ready=False, crashloop=True)]
        ns.pods += [pod(f"{label}-{i + 2}", label, phase="Pending", ready=False)
                    for i in range(max(spec.replicas - 1, 0))]
        ns.deployments = [deployment(label, label, replicas=spec.replicas, ready=0)]
        ns.replicasets = [replicaset(f"{label}-abc", label, replicas=spec.replicas, ready=0)]
        ns.events = [event("BackOff", f"{label}-1", count=12,
                           message="Back-off restarting failed container app")]
    else:
        ns.pods = [pod(f"{label}-{i + 1}", label) for i in range(spec.replicas)]
        ns.deployments = [deployment(label, label, replicas=spec.replicas,
                                     ready=spec.replicas)]
        ns.replicasets = [replicaset(f"{label}-abc", label, replicas=spec.replicas,
                                     ready=spec.replicas)]


def build_cluster(spec: ClusterSpec) -> ClusterFixture:
    """One cluster fixture from one declared cluster."""
    if spec.kind == "unreachable":
        # Nothing else on the spec matters: the API server does not answer, so
        # no namespace of it is observable. That IS the reading (FR-ATT-5).
        return ClusterFixture(reachable=False)
    fixture = ClusterFixture(nodes=_nodes_for(spec.kind))
    fixture.ns("kube-system")
    for name, ns_spec in spec.namespaces.items():
        crashloop = (ns_spec.crashloop if ns_spec.crashloop is not None
                     else spec.kind == "degraded-crashloop")
        _fill_namespace(fixture, name, ns_spec, crashloop)
    return fixture


def build_world(fleet: Fleet) -> dict[str, ClusterFixture]:
    """The scenario's ``{cluster: ClusterFixture}`` map."""
    world = default_world() if fleet.base == "default" else {}
    for name, spec in fleet.clusters.items():
        built = build_cluster(spec)
        existing = world.get(name)
        if existing is not None and spec.kind != "unreachable":
            # Additive on the canned fleet: a scenario that only names extra
            # namespaces keeps the shared cluster's shape, so "add ssop-prod
            # to the hub" does not silently redefine the hub's nodes.
            if not spec.namespaces:
                continue
            for ns_name, ns_fixture in built.namespaces.items():
                if ns_name != "kube-system":
                    existing.namespaces[ns_name] = ns_fixture
            continue
        world[name] = built
    return world


def _applications(registry: Registry) -> dict[str, Any] | None:
    """The scenario's applications.yaml content, or None to keep the committed one."""
    if registry.base == "committed" and not registry.apps:
        return None
    base: list[dict[str, Any]] = []
    if registry.base == "committed":
        base = list(load_yaml(CONFIG_DIR / "fleet" / "applications.yaml")["applications"])
    by_name = {str(a["application"]): a for a in base}
    for app in registry.apps:
        by_name[app.application] = app.as_registry_entry()
    return {"version": 1, "applications": [by_name[k] for k in sorted(by_name)]}


def build_config_plane(case: Case, dest: Path, registry_port: int) -> Path:
    """A per-scenario copy of the config plane, on disk and ready to boot.

    Everything except the registry downstream URL and (when overridden) the
    application catalog is the committed file, so a scenario still exercises
    the shipped batteries, prompts, and messages.
    """
    root = dest / "config"
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(CONFIG_DIR, root)
    point_registry_at(root, registry_port)
    applications = _applications(case.registry)
    if applications is not None:
        (root / "fleet" / "applications.yaml").write_text(
            yaml.safe_dump(applications, sort_keys=False), encoding="utf-8"
        )
    return root


def stack_key(case: Case, mode: str) -> str:
    """What makes two cases able to share one booted stack.

    Same fleet, same registry, same inference target, same mode: then the
    servers, the config plane and the seeded Mongo are identical and only the
    conversation differs. Booting five services per case would otherwise
    dominate the run time of a suite that deliberately holds its world still.
    """
    payload = json.dumps(
        {
            "mode": mode,
            "fleet": case.fleet.model_dump(mode="json"),
            "registry": case.registry.model_dump(mode="json"),
            "inference_api_base": case.inference_api_base,
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:12]
