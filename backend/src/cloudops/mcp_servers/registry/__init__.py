"""The fleet registry MCP server package (`reg__*`, port 8013).

Thin by design: every answer comes from cloudops.registry, which the live
fleet reads too, so the tools and the cluster clients can never disagree
about what the registry says.
"""
