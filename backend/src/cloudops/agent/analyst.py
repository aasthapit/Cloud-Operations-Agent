"""The analyst LlmAgent: narrative synthesis and interactive investigation.

The deterministic orchestrator runs first and hands this agent its grounding
through session state; the analyst then narrates and answers follow-ups with
gateway tools (FR-CHAT-1). Three seams worth knowing:

- instruction: a provider callable, so persona/routing/skills re-read from
  the config plane on every invocation (hot reload) and the per-turn
  grounding is injected from state.
- McpToolset -> gateway: header_provider stamps traceparent + X-Thread-Id +
  X-User-Sub on the MCP connection per invocation, joining gateway audit
  lines and spans to the conversation trace.
- before_tool_callback: enforces agent.max_tool_iterations per turn
  (FR-CHAT-5); past the budget the tool call is answered with an error
  payload instead of executing, which the model sees and must surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog
from google.adk.agents import LlmAgent
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from cloudops.agent.model_factory import agent_tuning, build_model
from cloudops.agent.prompts import assemble_instruction
from cloudops.common.settings import get_settings
from cloudops.common.telemetry import inject_headers

log = structlog.get_logger("cloudops.analyst")


def _instruction_provider(config_dir: Path):
    def provider(ctx: ReadonlyContext) -> str:
        grounding = str(ctx.state.get("grounding_text", ""))
        task_hint = str(ctx.state.get("task_hint", ""))
        return assemble_instruction(config_dir, grounding, task_hint)

    return provider


def _header_provider(ctx: ReadonlyContext) -> dict[str, str]:
    """Per-invocation MCP headers: trace context + conversation identity."""
    thread_id = str(ctx.state.get("thread_id", "")) or "-"
    user_sub = str(ctx.state.get("user_sub", "")) or "-"
    return inject_headers({"X-Thread-Id": thread_id, "X-User-Sub": user_sub})


def _tool_budget_callback(tool: Any, args: dict[str, Any], tool_context: Any) -> dict[str, Any] | None:
    """FR-CHAT-5: hard per-turn tool budget, read fresh so it hot-reloads."""
    limit = int(agent_tuning(get_settings().config_dir).get("max_tool_iterations", 12))
    count = int(tool_context.state.get("temp:tool_calls", 0)) + 1
    tool_context.state["temp:tool_calls"] = count
    if count > limit:
        log.warning("analyst.tool_budget_exhausted", tool=getattr(tool, "name", "?"), limit=limit)
        return {
            "error": (
                f"Tool budget exhausted ({limit} calls this turn). Answer with the "
                "evidence you already have and tell the user which check you would run next."
            )
        }
    return None


def build_analyst() -> LlmAgent:
    settings = get_settings()
    toolset = McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=settings.cloudops_gateway_url),
        header_provider=_header_provider,
        # The gateway allowlist is the real policy choke point (NFR-SEC-3);
        # no extra filtering here keeps "add a domain" config-only.
    )
    return LlmAgent(
        name="analyst",
        description="Narrates deterministic triage results and investigates follow-ups with fleet tools",
        model=build_model(settings.config_dir),
        instruction=_instruction_provider(settings.config_dir),
        tools=[toolset],
        before_tool_callback=_tool_budget_callback,
    )
