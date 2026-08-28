"""Entry point: python -m cloudops.mcp_servers.openshift"""

from cloudops.common.logging import setup_logging
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import setup_telemetry
from cloudops.mcp_servers.openshift.server import SERVICE, build_server
from cloudops.mcp_servers.shared import serve


def main() -> None:
    setup_telemetry(SERVICE)
    setup_logging(SERVICE)
    settings = get_settings()
    serve(build_server(), settings.cloudops_mcp_openshift_port)


if __name__ == "__main__":
    main()
