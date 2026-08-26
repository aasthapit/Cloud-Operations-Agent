"""Entry point: python -m cloudops.gateway"""

import uvicorn

from cloudops.common.logging import setup_logging
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import setup_telemetry
from cloudops.gateway.app import build_app


def main() -> None:
    setup_telemetry("cloudops.gateway")
    setup_logging("cloudops.gateway")
    settings = get_settings()
    uvicorn.run(build_app(), host="127.0.0.1", port=settings.cloudops_gateway_port, log_level="warning")


if __name__ == "__main__":
    main()
