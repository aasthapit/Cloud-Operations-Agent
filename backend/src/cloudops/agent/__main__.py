"""Entry point: python -m cloudops.agent"""

import uvicorn

from cloudops.common.logging import setup_logging
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import setup_telemetry


def main() -> None:
    # Telemetry BEFORE any ADK import path runs, so ADK's GenAI spans land on
    # our provider (its own setup no-ops when a global provider exists).
    setup_telemetry("cloudops.agent")
    setup_logging("cloudops.agent")
    from cloudops.agent.a2a_app import build_app

    settings = get_settings()
    uvicorn.run(build_app(), host="127.0.0.1", port=settings.cloudops_agent_port, log_level="warning")


if __name__ == "__main__":
    main()
