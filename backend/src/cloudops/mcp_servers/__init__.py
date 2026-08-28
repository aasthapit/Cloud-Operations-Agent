"""Domain MCP servers.

Each server is an independent process speaking MCP over streamable HTTP and
is registered with the gateway via config/gateway/servers.yaml. There is one
backend: live cluster APIs read through cloudops.mcp_servers.kube. Tool
result shapes are the contract the check batteries in config/checks/*.yaml
address by dotted path.
"""
