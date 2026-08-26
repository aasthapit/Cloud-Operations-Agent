"""Entry point: python -m cloudops.mcp_servers.observability"""

from cloudops.common.logging import setup_logging
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import setup_telemetry
from cloudops.mcp_servers.observability.server import SERVICE, build_server
from cloudops.mcp_servers.shared import WorldHolder, serve


def main() -> None:
    setup_telemetry(SERVICE)
    setup_logging(SERVICE)
    settings = get_settings()
    holder = WorldHolder(settings.config_dir)
    serve(build_server(holder), settings.cloudops_mcp_observability_port, holder)


if __name__ == "__main__":
    main()
