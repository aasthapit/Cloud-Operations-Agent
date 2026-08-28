"""Fleet registry MCP server (`reg__*`).

Tool surface: resolve ambiguous human text to fleet entities, and answer the
registry-shaped questions that no cluster API can answer - where an
application is placed, what else shares a cluster with it, what a line of
business spans, what the blast radius of losing something is.

Where it sits. The registry PROPOSES and the cluster API CONFIRMS (FR-CTX-2).
Nothing here reads a cluster, so nothing here is evidence that an application
is actually running; a caller takes these placements to `ocp__*` tools and
verifies them. Saying so plainly in the server instructions matters, because
the failure mode this design guards against is an agent reporting registry
belief as observed fact.

Namespacing: FastMCP tool names here are UNPREFIXED (`resolve_entity`), and
the gateway exposes them as `reg__resolve_entity` from the `reg` entry in
config/gateway/servers.yaml. That is the same split the OpenShift server
uses, and it keeps the prefix a deployment decision rather than something
baked into the server.

Availability: MongoDB being down is an operational fact, not a crash. Every
tool is wrapped so an unreachable registry returns
``{"error": "registry unavailable", "detail": ...}`` and a malformed request
returns ``{"error": "invalid arguments", "detail": ...}``. The server stays
up and the agent can say honestly that the registry could not be consulted.

Credentials: cluster records carry an `auth` block. It is stripped by
cloudops.registry.queries.public_cluster before anything reaches a result, so
no tool on this server can leak one (FR-MCP-7, NFR-LOG-3).
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field
from pymongo.errors import PyMongoError

from cloudops.common.settings import get_settings
from cloudops.mcp_servers.shared import instrumented
from cloudops.registry import queries
from cloudops.registry.db import RegistryUnavailable
from cloudops.registry.queries import RegistryQueryError

SERVICE = "cloudops.mcp.registry"

ERROR_UNAVAILABLE = "registry unavailable"
ERROR_ARGUMENTS = "invalid arguments"


def guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn registry failures into an honest result instead of a stack trace.

    Applied OUTSIDE the span decorator so the failure is still recorded on
    the span and in the log line before it is converted; the caller gets a
    payload, the operator gets the trace.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except RegistryQueryError as exc:
            return {"error": ERROR_ARGUMENTS, "detail": str(exc)}
        except (RegistryUnavailable, PyMongoError) as exc:
            return {"error": ERROR_UNAVAILABLE, "detail": str(exc)}

    return wrapper


def build_server() -> FastMCP:
    settings = get_settings()
    mcp = FastMCP(
        "registry-mcp",
        instructions=(
            "The organization's fleet registry: applications, clusters, namespaces, "
            "and lines of business. Resolve ambiguous text with resolve_entity before "
            "any other call. These are registry BELIEFS about placement, not "
            "observations: verify them against the cluster with the ocp__ tools "
            "before reporting an application as running or down."
        ),
        host="127.0.0.1",
        port=settings.cloudops_mcp_registry_port,
        streamable_http_path="/mcp",
        stateless_http=True,
    )

    span = instrumented(SERVICE)

    # -- resolution ---------------------------------------------------------

    @mcp.tool()
    @guarded
    @span
    def resolve_entity(
        query: str = Field(description="Free text: an app id or name, cluster name or alias, namespace, or line of business"),
        kind_hint: str | None = Field(
            default=None, description="Restrict to one kind: app | cluster | namespace | lob"),
    ) -> dict[str, Any]:
        """Resolve ambiguous text to fleet entities, best match first.

        Matching is exact, then alias, then substring, then fuzzy, and every
        match carries its score. Several candidates mean the caller must pick
        or ask the user, never guess."""
        return queries.resolve_entity(query, kind_hint)

    # -- placements ---------------------------------------------------------

    @mcp.tool()
    @guarded
    @span
    def find_placements(
        app_id: str | None = Field(default=None, description="Resolved application id, e.g. SSOP"),
        cluster: str | None = Field(default=None, description="Resolved cluster name"),
        namespace: str | None = Field(default=None, description="Namespace"),
        environment: str | None = Field(default=None, description="prod | nonprod"),
        lob: str | None = Field(default=None, description="Line of business"),
    ) -> dict[str, Any]:
        """Where the registry believes applications are placed. Every filter
        given is ANDed; filters are exact, so resolve ambiguous text first."""
        return queries.find_placements(app_id, cluster, namespace, environment, lob)

    @mcp.tool()
    @guarded
    @span
    def list_apps_on_cluster(
        cluster: str = Field(description="Resolved cluster name"),
        environment: str | None = Field(default=None, description="Filter: prod | nonprod"),
    ) -> dict[str, Any]:
        """Applications, namespaces, and lines of business registered on one
        cluster. Registry belief; verify before reporting it as running."""
        return queries.list_apps_on_cluster(cluster, environment)

    @mcp.tool()
    @guarded
    @span
    def blast_radius(
        cluster: str | None = Field(default=None, description="Cluster whose loss is being assessed"),
        namespace: str | None = Field(default=None, description="Namespace whose loss is being assessed"),
        lob: str | None = Field(default=None, description="Line of business whose loss is being assessed"),
    ) -> dict[str, Any]:
        """What is affected if a cluster, namespace, or line of business goes
        down: the applications, namespaces, LOBs, and environments in scope.
        At least one of the three arguments is required."""
        return queries.blast_radius(cluster, namespace, lob)

    # -- application registry -----------------------------------------------

    @mcp.tool()
    @guarded
    @span
    def get_app(
        app_id: str = Field(description="Application id, name, or pod app label"),
    ) -> dict[str, Any]:
        """The application registry entry: owners, criticality, SLA/SLO,
        runbooks, escalation, dependencies, backup policy, plus its registered
        placements. Returns found: false for an unknown application."""
        return queries.get_app(app_id)

    @mcp.tool()
    @guarded
    @span
    def list_lobs() -> dict[str, Any]:
        """Every line of business with its application and cluster counts."""
        return queries.list_lobs()

    return mcp
