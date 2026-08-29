"""LLM judges for the qualities no assertion can reach.

Three things about a narrative cannot be checked deterministically, and all
three are what a user actually reads:

  groundedness   is every factual claim supported by the deterministic
                 payloads and tool results this turn produced
  completeness   did it answer the question the user actually asked
  protocol_tone  did it stay prose - no raw JSON dumps, no invented tool
                 names, no fences it was told never to write

Design decisions worth defending:

- The judge speaks to the SAME openai-compatible endpoint the agent uses,
  read from ``config/models.yaml``, but through a small direct httpx client.
  Dragging ADK into the judge would put the thing under test in the path of
  its own measurement, and an ADK agent brings sessions, tools, and a system
  instruction none of which a judge wants.
- Prompts live in ``backend/evals/judges/*.md`` as config, not code, for the
  same reason the agent's prompts do: judging criteria are product policy and
  should be editable without a Python change.
- Placeholders are ``{{name}}`` and substituted literally, never through
  ``str.format``, because judge prompts are full of JSON braces.
- Output is forced to JSON and parsed defensively, with exactly one retry on
  an unparseable answer. A judge that cannot be parsed twice is a failed
  metric with its raw text attached, never a crash and never a silent pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import structlog

from cloudops.common.config import load_yaml, read_prompt
from cloudops.common.settings import get_settings

log = structlog.get_logger("cloudops.evals.judge")

JUDGE_METRICS = ("groundedness", "completeness", "protocol_tone")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

RETRY_NUDGE = (
    "Your previous answer was not valid JSON. Reply with the JSON object only: "
    "no prose, no markdown fence, no explanation."
)


class JudgeUnavailable(RuntimeError):
    """The judge endpoint could not be reached or configured."""


@dataclass
class JudgeVerdict:
    metric: str
    score: float
    reason: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    raw: str = ""

    def evidence(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"reason": self.reason}
        if self.claims:
            payload["claims"] = self.claims[:12]
        if self.score == 0.0 and self.raw:
            # A zero is either a damning verdict or a judge malfunction; the
            # raw tail is what tells the reader which, so a zero always
            # carries it.
            payload["judge_raw_tail"] = self.raw[-500:]
        return payload


@dataclass
class JudgeConfig:
    api_base: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout_s: float = 300.0


def judge_config(config_dir: Path | None = None) -> JudgeConfig:
    """The judge's endpoint, from the same models.yaml the agent binds to.

    An optional ``judge:`` block overrides the model (a bigger, slower model
    is a reasonable grader for a smaller, faster analyst); everything else
    falls back to ``inference``, so by default the judge and the thing it
    judges are configured in one place.
    """
    settings = get_settings()
    cfg = load_yaml((config_dir or settings.config_dir) / "models.yaml")
    inference = cfg.get("inference") or {}
    judge = cfg.get("judge") or {}
    api_base = str(judge.get("api_base") or settings.ollama_api_base).rstrip("/")
    if not api_base.endswith("/v1"):
        api_base += "/v1"
    model = str(judge.get("model") or inference.get("model") or "")
    if not model:
        raise JudgeUnavailable(
            "config/models.yaml names no model for the judge (judge.model or inference.model)"
        )
    return JudgeConfig(
        api_base=api_base,
        model=model,
        temperature=float(judge.get("temperature", 0.0)),
        max_tokens=int(judge.get("max_output_tokens", 4096)),
        timeout_s=float(judge.get("timeout_seconds", 300)),
    )


def render(prompt: str, **values: str) -> str:
    """Substitute ``{{name}}`` placeholders literally."""
    for key, value in values.items():
        prompt = prompt.replace("{{" + key + "}}", value)
    return prompt


def parse_verdict(metric: str, text: str) -> JudgeVerdict | None:
    """Pull the verdict object out of a model answer, or None if it is not there."""
    match = _JSON_RE.search(text or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "score" not in payload:
        return None
    try:
        score = float(payload["score"])
    except (TypeError, ValueError):
        return None
    claims = payload.get("claims") or payload.get("verdicts") or []
    return JudgeVerdict(
        metric=metric,
        score=max(0.0, min(1.0, score)),
        reason=str(payload.get("reason") or payload.get("explanation") or "")[:600],
        claims=[c for c in claims if isinstance(c, dict)],
        raw=text[:2000],
    )


class Judge:
    """A direct openai-compat client that grades one narrative at a time."""

    def __init__(self, config: JudgeConfig, prompts_dir: Path) -> None:
        self.config = config
        self.prompts_dir = prompts_dir

    def prompt_for(self, metric: str) -> str:
        path = self.prompts_dir / f"{metric}.md"
        if not path.exists():
            raise JudgeUnavailable(f"no judge prompt at {path}")
        return read_prompt(path)

    async def score(
        self, metric: str, *, question: str, narrative: str, evidence: dict[str, Any]
    ) -> JudgeVerdict:
        """One graded metric. Never raises for a bad answer: an unparseable
        judge is a zero with its raw text attached, which is a visible failure
        rather than a quiet pass."""
        prompt = render(
            self.prompt_for(metric),
            question=question,
            narrative=narrative or "(the turn produced no narrative)",
            evidence=json.dumps(evidence, ensure_ascii=False, indent=2)[:24000],
        )
        messages = [
            {"role": "system", "content": "You are a strict evaluation judge. "
                                          "You reply with a single JSON object and nothing else."},
            {"role": "user", "content": prompt},
        ]
        for attempt in (1, 2):
            text = await self._complete(messages)
            verdict = parse_verdict(metric, text)
            if verdict is not None:
                return verdict
            log.warning("evals.judge_unparseable", metric=metric, attempt=attempt)
            messages = [*messages, {"role": "assistant", "content": text[:2000]},
                        {"role": "user", "content": RETRY_NUDGE}]
        return JudgeVerdict(metric=metric, score=0.0,
                            reason="the judge did not return parseable JSON in two attempts",
                            raw=text[:2000])

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        body = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
            try:
                response = await client.post(
                    f"{self.config.api_base}/chat/completions",
                    json=body,
                    # Ollama ignores the key; an OpenAI-compatible gateway
                    # takes it from the environment in a deployed run.
                    headers={"Authorization": "Bearer ollama-local"},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise JudgeUnavailable(f"judge endpoint unreachable: {exc}") from exc
        payload = response.json()
        choices = payload.get("choices") or [{}]
        message = choices[0].get("message") or {}
        content = str(message.get("content") or "")
        if not content.strip():
            # Reasoning models can spend the whole budget thinking and hand
            # back an empty content with the deliberation in a side field;
            # that text sometimes carries the JSON and always carries the
            # diagnosis, so it beats an empty string either way.
            content = str(message.get("reasoning")
                          or message.get("reasoning_content") or "")
            log.warning("evals.judge_empty_content",
                        finish_reason=choices[0].get("finish_reason"),
                        fallback_chars=len(content))
        return content
