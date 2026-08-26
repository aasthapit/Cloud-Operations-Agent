"""Typed view of config/gateway/servers.yaml (hot-reloaded)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ServerEntry(BaseModel):
    """One registered downstream MCP server."""

    prefix: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    url: str
    enabled: bool = True
    timeout_seconds: float = 30.0
    # Explicit allowlist: tools not listed are refused and never advertised
    # (FR-GW-3). An empty list means "expose nothing", never "expose all".
    allow_tools: list[str] = Field(default_factory=list)


class ServersConfig(BaseModel):
    version: int = 1
    servers: list[ServerEntry] = Field(default_factory=list)
    deny_tools: list[str] = Field(default_factory=list)

    def enabled_servers(self) -> list[ServerEntry]:
        return [s for s in self.servers if s.enabled]
