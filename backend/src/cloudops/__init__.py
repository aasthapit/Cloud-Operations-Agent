"""Cloud Operations Agent backend.

Services (each runnable as `python -m cloudops.<service>`):
  cloudops.agent            - ADK triage agent exposed over A2A
  cloudops.gateway          - MCP aggregation gateway
  cloudops.mcp_servers.openshift      - OpenShift domain MCP server
  cloudops.mcp_servers.registry       - MongoDB fleet registry MCP server

Shared layers:
  cloudops.common     - settings, hot-reload config, logging, redaction, telemetry
"""
