"""Re-export shim: the doubles now live in ``cloudops.testkit``.

They moved because the scenario eval harness needs the same fixtures the
suite uses, and two implementations of a double are two chances to disagree.
This module keeps the historical ``from fakes import ...`` spelling working
so a test reads the same as it always did.
"""

from __future__ import annotations

from cloudops.testkit.fleet import (
    APP_LABEL,
    APP_NS,
    DEGRADED,
    HEALTHY,
    MAINTENANCE,
    UNREACHABLE,
    FakeFleet,
    default_world,
)
from cloudops.testkit.gateway import FakeGateway
from cloudops.testkit.kube import (
    NAME_LABEL,
    ClusterFixture,
    FakeKubeError,
    NamespaceFixture,
    deployment,
    event,
    fake_kube,
    hpa,
    namespace,
    node,
    pdb,
    pod,
    quota,
    replicaset,
)
from cloudops.testkit.registry import (
    REGISTRY_TOOLS,
    FakeRegistry,
    build_registry_server,
)
from cloudops.testkit.services import CONFIG_DIR

__all__ = [
    "APP_LABEL",
    "APP_NS",
    "CONFIG_DIR",
    "DEGRADED",
    "HEALTHY",
    "MAINTENANCE",
    "NAME_LABEL",
    "REGISTRY_TOOLS",
    "UNREACHABLE",
    "ClusterFixture",
    "FakeFleet",
    "FakeGateway",
    "FakeKubeError",
    "FakeRegistry",
    "NamespaceFixture",
    "build_registry_server",
    "default_world",
    "deployment",
    "event",
    "fake_kube",
    "hpa",
    "namespace",
    "node",
    "pdb",
    "pod",
    "quota",
    "replicaset",
]
