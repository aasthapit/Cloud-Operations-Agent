/**
 * Slash-command palette source (`GET /api/commands`, PRD "Console UX").
 *
 * Built-ins (`/clear`, `/attest`, `/persona`) are known literally by the
 * web client, which hardcodes their argument handling; this module only
 * needs to say whether `/persona` applies (dev auth mode) and what subs it
 * offers. Skill commands are different: their prefill text depends on the
 * skill file's own prose, which the client never reads, so each skill
 * command carries a `template` string the client can drop straight into
 * the composer for editing.
 */
import fs from "node:fs";
import path from "node:path";

import YAML from "yaml";

import { loadUsers } from "./auth.js";
import { config } from "./config.js";
import { logger } from "./logger.js";

export interface CommandDef {
  name: string;
  /** Placeholder shown after the name, e.g. "<cluster>". */
  args?: string;
  description: string;
  kind: "client" | "message";
  /** Prefill text for a kind:"message" command whose content the client
   * cannot construct itself (skills); omitted for /attest, whose
   * "attest <cluster>" template is a client-side literal. */
  template?: string;
}

interface AgentYaml {
  skills?: Array<{ file: string; enabled?: boolean }>;
}

/** First `#` heading (title, "Skill: " prefix stripped) and the first
 * non-empty line after it (the skill's own one-line summary). */
function skillHeading(contents: string): { title: string; summary: string } {
  const lines = contents.split("\n");
  const headingIdx = lines.findIndex((l) => l.trim().startsWith("#"));
  const headingLine = headingIdx >= 0 ? lines[headingIdx] : "";
  const rawTitle = headingLine.replace(/^#+\s*/, "").trim();
  const title = rawTitle.replace(/^skill:\s*/i, "").trim() || rawTitle || "skill";
  const summary =
    headingIdx >= 0
      ? (lines.slice(headingIdx + 1).find((l) => l.trim().length > 0) ?? "").trim()
      : "";
  return { title, summary };
}

/** One command per enabled skill in config/agent/agent.yaml. */
function loadSkillCommands(): CommandDef[] {
  const agentYamlPath = path.join(config.configDir, "agent", "agent.yaml");
  let doc: AgentYaml;
  try {
    doc = (YAML.parse(fs.readFileSync(agentYamlPath, "utf8")) ?? {}) as AgentYaml;
  } catch (err) {
    logger.warn({ err }, "commands.agent_yaml_unavailable");
    return [];
  }
  const commands: CommandDef[] = [];
  for (const entry of doc.skills ?? []) {
    if (entry.enabled === false) continue;
    const skillPath = path.join(config.configDir, "agent", entry.file);
    let contents: string;
    try {
      contents = fs.readFileSync(skillPath, "utf8");
    } catch (err) {
      logger.warn({ err, file: entry.file }, "commands.skill_file_unavailable");
      continue;
    }
    const { title, summary } = skillHeading(contents);
    const name = "/" + path.basename(entry.file, path.extname(entry.file));
    commands.push({
      name,
      description: title,
      kind: "message",
      template: summary ? `Use the ${title} skill: ${summary}` : `Use the ${title} skill:`,
    });
  }
  return commands;
}

/**
 * Full command list. `isDevAuth` gates `/persona`, which programmatically
 * switches the persona picker and only makes sense while the picker itself
 * is showing (dev auth mode; FR-UI-4).
 */
export function buildCommands(isDevAuth: boolean): CommandDef[] {
  const commands: CommandDef[] = [
    { name: "/clear", description: "Clear the conversation and start a new thread", kind: "client" },
    { name: "/attest", args: "<cluster>", description: "Attest a cluster's health", kind: "message" },
  ];
  if (isDevAuth) {
    let subs: string[] = [];
    try {
      subs = loadUsers().map((u) => u.sub);
    } catch (err) {
      logger.warn({ err }, "commands.users_unavailable");
    }
    commands.push({
      name: "/persona",
      args: subs.length > 0 ? `<${subs.join("|")}>` : "<sub>",
      description: `Switch the active persona (dev mode only). Available: ${subs.join(", ") || "none configured"}`,
      kind: "client",
    });
  }
  commands.push(...loadSkillCommands());
  return commands;
}
