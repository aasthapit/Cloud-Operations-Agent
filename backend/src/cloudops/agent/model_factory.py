"""LiteLLM model construction (decision D8; config/models.yaml).

Two provider styles against the same local Ollama server:

  openai-compat (default): Ollama's native OpenAI-compatible endpoint at
    <OLLAMA_API_BASE>/v1. Production swaps in a hosted OpenAI-compatible
    gateway by changing the base URL, no code change.
  ollama-chat: LiteLLM's ollama_chat provider, the ADK-documented fallback
    when a model misbehaves in tool loops over the OpenAI surface.

Note on hot reload: `inference.*` (provider/model/temperature) binds at
agent START because the ADK agent holds its model instance; `agent.*` keys
in the same file ARE read fresh every turn. models.yaml documents this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from google.adk.models.lite_llm import LiteLlm

from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings

log = structlog.get_logger("cloudops.model")


def build_model(config_dir: Path) -> LiteLlm:
    cfg = load_yaml(config_dir / "models.yaml").get("inference", {})
    settings = get_settings()
    provider = cfg.get("provider", "openai-compat")
    model = cfg.get("model", "qwen3:4b")
    kwargs: dict[str, Any] = {
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_output_tokens", 4096),
    }
    if provider == "ollama-chat":
        spec = f"ollama_chat/{model}"
        kwargs["api_base"] = settings.ollama_api_base
    else:  # openai-compat (default): Ollama as a mock OpenAI endpoint
        spec = f"openai/{model}"
        kwargs["api_base"] = settings.ollama_api_base.rstrip("/") + "/v1"
        # Ollama ignores the key but the OpenAI client requires one; this is
        # a placeholder, not a credential.
        kwargs["api_key"] = "ollama-local"
    log.info("model.configured", provider=provider, model=model, api_base=kwargs.get("api_base"))
    return LiteLlm(model=spec, **kwargs)


def agent_tuning(config_dir: Path) -> dict[str, Any]:
    """The hot-read agent.* knobs (TTL, loop budget, auto_app360)."""
    return load_yaml(config_dir / "models.yaml").get("agent", {})
