"""The shared synthetic fleet (mock mode's source of truth, FR-MCP-6).

Both mock MCP backends (OpenShift and observability) answer every tool from
ONE World instance built from config/fleet/*.yaml + config/mock/scenario.yaml,
so the two servers can never tell inconsistent stories.
"""

from cloudops.mockfleet.world import World

__all__ = ["World"]
