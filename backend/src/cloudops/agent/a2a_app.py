"""A2A wiring: the triage agent as a standard A2A server (decision D4).

`to_a2a()` (ADK) builds the Starlette app: agent card at
/.well-known/agent-card.json, JSON-RPC endpoint speaking A2A 1.0 (plus 0.3
compat) at /. The BFF is the client.

Contract notes the BFF relies on (verified against ADK/a2a-sdk source
during research):
- A2A contextId IS the ADK session id: thread continuity for free.
- Request-level params.metadata reaches the orchestrator as
  RunConfig.custom_metadata["a2a_metadata"]; the BFF sends
  {"claims": {sub, name, email, groups}} there (FR-ID-1).

Beside the A2A surface this module appends one read-only GET /status route
(config version, battery check counts, last rejected reload) so the console
can SURFACE a refused config edit rather than leaving it in the logs
(FR-CFG-3, gap G6). The BFF proxies it from /api/meta.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cloudops.agent.analyst import build_analyst
from cloudops.agent.models import App360Battery, AttestationBattery
from cloudops.agent.orchestrator import TriageOrchestrator
from cloudops.common.config import HotConfig, config_version
from cloudops.common.settings import get_settings

# The file set whose content hash is the console's config-version chip
# (frontend/server/src/index.ts hashes exactly these; keep the lists aligned).
STATUS_CONFIG_FILES = (
    "checks/health_attestation.yaml",
    "checks/app360.yaml",
    "agent/system_prompt.md",
    "agent/routing.md",
    "agent/agent.yaml",
    "models.yaml",
)


class ConfigStatus:
    """Validation observer over the check batteries, for GET /status.

    The orchestrator keeps its own last-known-good cache (it reads the
    batteries fresh per turn); these HotConfig instances exist only so the
    status route can report the CURRENT counts and, when a save was
    refused, which file and why. Reloads are pulled on request: the route
    is polled every few seconds and HotConfig skips unchanged bytes, so no
    watcher task is needed here.
    """

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.attestation: HotConfig[AttestationBattery] = HotConfig(
            config_dir / "checks" / "health_attestation.yaml",
            AttestationBattery.model_validate,
        )
        self.app360: HotConfig[App360Battery] = HotConfig(
            config_dir / "checks" / "app360.yaml", App360Battery.model_validate
        )

    async def payload(self) -> dict[str, Any]:
        await self.attestation.reload()
        await self.app360.reload()
        att, a360 = self.attestation.value, self.app360.value
        # Newest rejection wins when both files are refused.
        errors = [e for e in (self.attestation.last_error, self.app360.last_error) if e]
        errors.sort(key=lambda e: e.at, reverse=True)
        return {
            "config_version": config_version([self.config_dir / f for f in STATUS_CONFIG_FILES]),
            "batteries": {
                "attestation": {"version": att.version, "checks": len(att.checks)},
                "app360": {
                    "version": a360.version,
                    "sections": len(a360.sections),
                    "checks": sum(len(s.checks) for s in a360.sections),
                },
            },
            "last_error": errors[0].as_dict() if errors else None,
        }


def attach_status_route(app: Starlette, config_dir: Path) -> None:
    """Append GET /status to an existing app (the A2A JSON-RPC root is '/')."""
    status = ConfigStatus(config_dir)

    async def endpoint(_request: Request) -> JSONResponse:
        return JSONResponse(await status.payload())

    app.routes.append(Route("/status", endpoint, methods=["GET"]))


def build_app() -> Starlette:
    from a2a.types import AgentCapabilities
    from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    settings = get_settings()
    orchestrator = TriageOrchestrator(analyst=build_analyst())

    # The default card builder leaves capabilities.streaming unset, and the
    # 1.0 request handler then refuses SendStreamingMessage outright; build
    # the card ourselves with streaming declared (the BFF streams every turn).
    #
    # Card building lists the analyst's MCP tools, which means connecting to
    # the gateway; under `make dev` all services boot simultaneously, so
    # retry briefly instead of imposing a boot order.
    builder = AgentCardBuilder(
        agent=orchestrator,
        rpc_url=f"http://localhost:{settings.cloudops_agent_port}/",
        capabilities=AgentCapabilities(streaming=True),
    )

    async def build_card_with_retry():
        last_error: Exception | None = None
        for _attempt in range(15):
            try:
                return await builder.build()
            except Exception as exc:  # noqa: BLE001 - gateway may still be booting
                last_error = exc
                await asyncio.sleep(2)
        raise RuntimeError(f"could not build the agent card (is the gateway up?): {last_error}")

    card = asyncio.run(build_card_with_retry())
    app = to_a2a(orchestrator, host="localhost", port=settings.cloudops_agent_port, agent_card=card)
    attach_status_route(app, settings.config_dir)
    return app
