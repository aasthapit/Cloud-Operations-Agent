"""MCP aggregation gateway.

One streamable-HTTP MCP endpoint fronting N downstream MCP servers
(config/gateway/servers.yaml). Pure aggregation and policy (decision D2):
namespacing, allow/deny lists, timeouts, audit, telemetry. No domain logic,
no conversation state.
"""
