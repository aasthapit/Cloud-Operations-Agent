"""The hermetic fake-LLM seam (NFR-QE-1, G7a).

These tests pin the two things the headless E2E depends on: build_model
honors both selection routes, and FakeLlm satisfies the BaseLlm streaming
contract exactly (partial chunks that reassemble into the final response).
"""

from pathlib import Path

import pytest
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from cloudops.agent.model_factory import FAKE_NARRATIVE_PREFIX, FakeLlm, build_model

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def request(*texts: str) -> LlmRequest:
    return LlmRequest(
        model="fake/deterministic",
        contents=[
            types.Content(role="user", parts=[types.Part(text=t)]) for t in texts
        ],
    )


class TestSelection:
    def test_env_switch_selects_fake(self, monkeypatch):
        monkeypatch.setenv("CLOUDOPS_FAKE_LLM", "1")
        assert isinstance(build_model(CONFIG_DIR), FakeLlm)

    def test_committed_config_still_builds_litellm(self, monkeypatch):
        monkeypatch.delenv("CLOUDOPS_FAKE_LLM", raising=False)
        model = build_model(CONFIG_DIR)
        assert isinstance(model, BaseLlm)
        assert not isinstance(model, FakeLlm)

    def test_provider_key_selects_fake(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLOUDOPS_FAKE_LLM", raising=False)
        (tmp_path / "models.yaml").write_text("inference:\n  provider: fake\n")
        assert isinstance(build_model(tmp_path), FakeLlm)


class TestDeterministicOutput:
    def test_derived_from_the_last_user_text(self):
        rendered = FakeLlm().render(request("first question", "why is payments-api flaky?"))
        assert rendered.startswith(FAKE_NARRATIVE_PREFIX)
        assert "why is payments-api flaky?" in rendered

    def test_empty_request_still_answers(self):
        assert FakeLlm().render(request()) == FAKE_NARRATIVE_PREFIX

    def test_repeatable(self):
        req = request("attest prod-east-2")
        assert FakeLlm().render(req) == FakeLlm().render(req)


class TestStreamingContract:
    @pytest.mark.asyncio
    async def test_non_streaming_yields_exactly_one_final(self):
        responses = [r async for r in FakeLlm().generate_content_async(request("hi"), stream=False)]
        assert len(responses) == 1
        assert responses[0].partial is False
        assert responses[0].content.parts[0].text == FakeLlm().render(request("hi"))

    @pytest.mark.asyncio
    async def test_streaming_yields_partials_then_the_same_final(self):
        req = request("is prod-east-2 healthy?")
        responses = [r async for r in FakeLlm().generate_content_async(req, stream=True)]
        assert len(responses) > 1
        assert all(r.partial for r in responses[:-1])
        final = responses[-1]
        assert final.partial is False
        # The aggregated final must equal the concatenated partials, which is
        # what the ADK flow assumes when it persists the turn.
        assert "".join(r.content.parts[0].text for r in responses[:-1]) == \
            final.content.parts[0].text

    @pytest.mark.asyncio
    async def test_never_emits_a_function_call(self):
        req = request("run every check you have")
        async for response in FakeLlm().generate_content_async(req, stream=True):
            for part in response.content.parts:
                assert part.function_call is None
