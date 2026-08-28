"""Entry point: python -m cloudops.mcp_servers.registry

No WorldHolder and no config watcher: MongoDB is the hot store, so a write
is visible to the next read and there is no file to watch.
"""

from cloudops.common.logging import setup_logging
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import setup_telemetry
from cloudops.mcp_servers.registry.server import SERVICE, build_server
from cloudops.mcp_servers.shared import serve


def main() -> None:
    setup_telemetry(SERVICE)
    setup_logging(SERVICE)
    settings = get_settings()
    serve(build_server(), settings.cloudops_mcp_registry_port, None)


if __name__ == "__main__":
    main()
