"""Domain MCP servers (OpenShift, observability).

Each server is an independent process speaking MCP over streamable HTTP,
registered with the gateway via config/gateway/servers.yaml. Both follow
the same shape: FastMCP tool definitions that delegate to a backend chosen
by CLOUDOPS_BACKEND_MODE (mock: the shared synthetic World; live: real
cluster APIs / Thanos). Tool result shapes are identical in both modes
(decision D6).
"""
