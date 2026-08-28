"""LiteLLM model construction (decision D8; config/models.yaml).

Three provider styles, two of them against the same local Ollama server:

  openai-compat (default): Ollama's native OpenAI-compatible endpoint at
    <OLLAMA_API_BASE>/v1. Production swaps in a hosted OpenAI-compatible
    gateway by changing the base URL, no code change.
  ollama-chat: LiteLLM's ollama_chat provider, the ADK-documented fallback
    when a model misbehaves in tool loops over the OpenAI surface.
  fake: the hermetic test seam (NFR-QE-1). No network, no inference server,
    deterministic output. Selected by `inference.provider: fake` or by
    CLOUDOPS_FAKE_LLM=1, which lets a test flip the seam without editing
    committed config.

models.yaml is AUTHORITATIVE: provider, model, temperature and
max_output_tokens have no code-side defaults. A missing key raises
ModelConfigError naming it, rather than quietly booting against some other
model than the one the operator configured.

Note on hot reload: `inference.*` (provider/model/temperature) binds at
agent START because the ADK agent holds its model instance; `agent.*` keys
in the same file ARE read fresh every turn. models.yaml documents this.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import structlog
from google.adk.models._capabilities import LlmCapabilities
from google.adk.models.base_llm import BaseLlm
from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from cloudops.common.config import load_yaml
from cloudops.common.settings import get_settings

log = structlog.get_logger("cloudops.model")

FAKE_NARRATIVE_PREFIX = "Deterministic triage narrative."


class FakeLlm(BaseLlm):
    """A hermetic stand-in for the analyst's model (NFR-QE-1).

    It exists so the whole triage flow - orchestrator phases, check engine,
    gateway, MCP servers, event stream - can be exercised in a test without
    Ollama, without a network, and without the 15-60 s of a real local
    narrative. It answers with one deterministic text derived from the
    request and NEVER emits a function call, so the analyst's tool loop
    terminates on the first turn.

    Streaming contract (BaseLlm.generate_content_async): with stream=True
    yield partial chunks and then one aggregated final response with
    partial unset; with stream=False yield only that final response. The
    final response is identical either way, which is what the ADK flow
    relies on when it persists the turn.
    """

    def __init__(self, model: str = "fake/deterministic") -> None:
        super().__init__(model=model)

    @property
    def capabilities(self) -> LlmCapabilities:
        # Declared outright: BaseLlm's default routes through a deprecated
        # name-based fallback that warns.
        return LlmCapabilities()

    @staticmethod
    def _last_user_text(llm_request: LlmRequest) -> str:
        for content in reversed(llm_request.contents or []):
            if content.role and content.role != "user":
                continue
            text = " ".join(
                p.text for p in (content.parts or []) if isinstance(p.text, str)
            ).strip()
            if text:
                return text
        return ""

    def render(self, llm_request: LlmRequest) -> str:
        """The deterministic answer for a request; separate so tests can
        assert the contract without driving the async generator."""
        user_text = self._last_user_text(llm_request)
        return f"{FAKE_NARRATIVE_PREFIX} Request: {user_text}".strip() if user_text \
            else FAKE_NARRATIVE_PREFIX

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        text = self.render(llm_request)
        if stream:
            # Split on whitespace so the partial chunks reassemble to exactly
            # `text`; the flow only forwards partials in SSE mode.
            chunks = text.split(" ")
            for i, chunk in enumerate(chunks):
                piece = chunk if i == 0 else " " + chunk
                yield LlmResponse(
                    content=types.Content(role="model", parts=[types.Part(text=piece)]),
                    partial=True,
                )
        yield LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            partial=False,
        )


def _fake_requested(provider: str) -> bool:
    """The env switch wins over config so a test never edits committed YAML."""
    return provider == "fake" or os.environ.get("CLOUDOPS_FAKE_LLM", "") == "1"


class ModelConfigError(RuntimeError):
    """config/models.yaml is missing a key the agent cannot invent."""


def _required(cfg: dict[str, Any], key: str) -> Any:
    """A models.yaml value with no code-side default.

    models.yaml is authoritative for what the agent talks to and how. A code
    fallback here would let a typo'd or truncated config boot silently against
    the wrong model, which is far worse than refusing to start.
    """
    value = cfg.get(key)
    if value is None:
        raise ModelConfigError(
            f"config/models.yaml is missing `inference.{key}`; it is required "
            "and has no code default"
        )
    return value


def build_model(config_dir: Path) -> BaseLlm:
    cfg = load_yaml(config_dir / "models.yaml").get("inference", {})
    settings = get_settings()
    provider = str(_required(cfg, "provider"))
    if _fake_requested(provider):
        log.info("model.configured", provider="fake", model="fake/deterministic")
        return FakeLlm()
    model = str(_required(cfg, "model"))
    kwargs: dict[str, Any] = {
        "temperature": _required(cfg, "temperature"),
        "max_tokens": _required(cfg, "max_output_tokens"),
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
