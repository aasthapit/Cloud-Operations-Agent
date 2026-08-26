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
"""

from __future__ import annotations

import asyncio

from starlette.applications import Starlette

from cloudops.agent.analyst import build_analyst
from cloudops.agent.orchestrator import TriageOrchestrator
from cloudops.common.settings import get_settings


def build_app() -> Starlette:
    from a2a.types import AgentCapabilities
    from google.adk.a2a.utils.agent_card_builder import AgentCardBuilder
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    settings = get_settings()
    orchestrator = TriageOrchestrator(analyst=build_analyst())

    # The default card builder leaves capabilities.streaming unset, and the
    # 1.0 request handler then refuses SendStreamingMessage outright; build
    # the card ourselves with streaming declared (the BFF streams every turn).
    card = asyncio.run(
        AgentCardBuilder(
            agent=orchestrator,
            rpc_url=f"http://localhost:{settings.cloudops_agent_port}/",
            capabilities=AgentCapabilities(streaming=True),
        ).build()
    )
    return to_a2a(orchestrator, host="localhost", port=settings.cloudops_agent_port, agent_card=card)
