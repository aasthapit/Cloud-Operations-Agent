"""Process configuration from environment variables.

Precedence contract (documented in .env.example): environment variable >
config/*.yaml > code default. This module only carries process-level knobs
(ports, endpoints, mode); behavioral configuration lives in the hot-reload
config plane under CLOUDOPS_CONFIG_DIR and is loaded via cloudops.common.config.

Secrets (tokens, kubeconfigs) are environment-only by design (FR-CFG-4);
they are looked up here and must never be logged (see cloudops.common.redact,
which scrubs the VALUES of secret-shaped environment variables from all logs).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings shared by all backend services."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- global ---
    cloudops_config_dir: Path = Path("./config")
    cloudops_env: str = "dev"  # dev | prod (controls log formatting)
    cloudops_log_level: str = "info"
    cloudops_backend_mode: str = "mock"  # mock | live

    # --- service ports (localhost by default, NFR-SEC-4) ---
    cloudops_agent_port: int = 8001
    cloudops_gateway_port: int = 8010
    cloudops_mcp_openshift_port: int = 8011
    cloudops_mcp_observability_port: int = 8012

    # --- inter-service URLs ---
    cloudops_gateway_url: str = "http://localhost:8010/mcp"
    cloudops_agent_a2a_url: str = "http://localhost:8001"

    # --- inference ---
    ollama_api_base: str = "http://localhost:11434"

    # --- live-mode credentials (never logged; see redact.py) ---
    cloudops_ocp_token: str = ""
    cloudops_thanos_url: str = ""
    cloudops_thanos_token: str = ""

    @property
    def config_dir(self) -> Path:
        """Absolute config-plane root.

        Relative paths resolve against the repo root (parent of backend/),
        so services behave the same whether launched from the repo root or
        from backend/.
        """
        p = self.cloudops_config_dir
        if p.is_absolute():
            return p
        for base in (Path.cwd(), Path.cwd().parent):
            candidate = (base / p).resolve()
            if candidate.is_dir():
                return candidate
        return (Path.cwd() / p).resolve()

    @property
    def is_dev(self) -> bool:
        return self.cloudops_env.lower() != "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton accessor. Process-level env is immutable at runtime."""
    return Settings()
