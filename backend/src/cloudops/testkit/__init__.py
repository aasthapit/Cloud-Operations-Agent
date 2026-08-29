"""Test doubles and the in-process service harness, as a first-class package.

The doubles started under ``backend/tests/``. They are here now because two
consumers need them and one implementation of a double is the only way the
two cannot disagree: the pytest suite (``backend/tests/``) and the scenario
eval harness (``cloudops.evals``) both build their worlds from exactly these
fixtures, so a scenario that passes an eval and a test that passes CI are
talking about the same fleet.

Shipping them inside ``cloudops`` is a deliberate trade. The alternative -
a second backend written for testing - is how a double and the thing it
doubles drift apart. Nothing here is imported by a service at runtime; the
only dependency that is not a production one (mongomock) is imported inside
the function that needs it.

What is here:

- ``kube``      canned Kubernetes payload builders and ``ClusterFixture``
- ``fleet``     ``FakeFleet`` and ``default_world``, the shared canned fleet
- ``registry``  ``seeded_registry`` (mongomock + the real seeder),
                ``FakeRegistry``, and a registry MCP server over it
- ``gateway``   ``FakeGateway``, the portless tool-contract facade
- ``services``  boot real MCP servers and the gateway on kernel-assigned ports
"""

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
    seeded_registry,
)
from cloudops.testkit.services import (
    CONFIG_DIR,
    REPO_ROOT,
    config_dir_with_registry,
    free_port,
    mcp_client,
    point_registry_at,
    serve_asgi,
    serve_fastmcp,
    service_env,
    wait_for_gateway_tools,
    wait_until,
)

__all__ = [
    "APP_LABEL",
    "APP_NS",
    "CONFIG_DIR",
    "DEGRADED",
    "HEALTHY",
    "MAINTENANCE",
    "NAME_LABEL",
    "REGISTRY_TOOLS",
    "REPO_ROOT",
    "UNREACHABLE",
    "ClusterFixture",
    "FakeFleet",
    "FakeGateway",
    "FakeKubeError",
    "FakeRegistry",
    "NamespaceFixture",
    "build_registry_server",
    "config_dir_with_registry",
    "default_world",
    "deployment",
    "event",
    "fake_kube",
    "free_port",
    "hpa",
    "mcp_client",
    "namespace",
    "node",
    "pdb",
    "pod",
    "point_registry_at",
    "quota",
    "replicaset",
    "seeded_registry",
    "serve_asgi",
    "serve_fastmcp",
    "service_env",
    "wait_for_gateway_tools",
    "wait_until",
]
