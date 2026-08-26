"""Hot-reloading instruction assembly (FR-CFG-2's per-invocation path).

The analyst's system instruction is rebuilt from the config plane on EVERY
LLM invocation: persona + routing + enabled skills (config/agent/agent.yaml
decides which) + per-turn grounding produced by the deterministic phases.
Editing any of these files changes behavior on the very next message with
no restart and no watcher.
"""

from __future__ import annotations

from pathlib import Path

from cloudops.common.config import load_yaml, read_prompt

# Rendered narrative rules the model must know about the wire protocol.
_PROTOCOL_NOTE = """
# Output protocol

Structured report cards were already delivered to the console by the runtime
as ```cloudops-...``` fenced blocks in earlier turns. Never write such fenced
blocks yourself and never quote their raw JSON; refer to their contents in
prose. Ground every factual claim in the GROUNDING DATA section below or in
a tool result from this conversation.
"""


def assemble_instruction(
    config_dir: Path, grounding_text: str, task_hint: str, conversation_text: str = ""
) -> str:
    """Compose the full system instruction for one invocation."""
    agent_cfg = load_yaml(config_dir / "agent" / "agent.yaml")
    agent_dir = config_dir / "agent"

    parts: list[str] = [
        read_prompt(agent_dir / agent_cfg.get("persona_file", "system_prompt.md")),
        read_prompt(agent_dir / agent_cfg.get("routing_file", "routing.md")),
    ]
    for skill in agent_cfg.get("skills", []):
        if skill.get("enabled", True):
            skill_path = agent_dir / skill["file"]
            if skill_path.exists():
                parts.append(read_prompt(skill_path))

    parts.append(_PROTOCOL_NOTE)
    if conversation_text:
        parts.append(f"# Conversation so far\n\n{conversation_text}")
    if task_hint:
        parts.append(f"# This turn\n\n{task_hint}")
    if grounding_text:
        parts.append(f"# GROUNDING DATA (produced by the deterministic phases)\n\n{grounding_text}")
    return "\n\n---\n\n".join(p.strip() for p in parts if p.strip())
