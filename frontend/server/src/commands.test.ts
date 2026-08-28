/**
 * Command-palette source tests (Console UX): built-ins, /persona gating,
 * and skill commands parsed from a temp agent.yaml + skill fixture so the
 * real config plane is never required.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { buildCommands } from "./commands.js";
import { config } from "./config.js";

let tmpDir: string;
let originalConfigDir: string;

beforeEach(() => {
  originalConfigDir = config.configDir;
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cloudops-commands-"));
  fs.mkdirSync(path.join(tmpDir, "agent", "skills"), { recursive: true });
  fs.mkdirSync(path.join(tmpDir, "identity"), { recursive: true });
  fs.writeFileSync(
    path.join(tmpDir, "agent", "skills", "cluster-health-triage.md"),
    "# Skill: cluster health triage\n\nHow to interpret an attestation result and carry it into the conversation.\n",
  );
  fs.writeFileSync(
    path.join(tmpDir, "agent", "skills", "disabled-skill.md"),
    "# Skill: disabled skill\n\nShould never appear.\n",
  );
  fs.writeFileSync(
    path.join(tmpDir, "agent", "agent.yaml"),
    [
      "persona_file: system_prompt.md",
      "routing_file: routing.md",
      "skills:",
      "  - file: skills/cluster-health-triage.md",
      "    enabled: true",
      "  - file: skills/disabled-skill.md",
      "    enabled: false",
    ].join("\n"),
  );
  fs.writeFileSync(
    path.join(tmpDir, "identity", "users.yaml"),
    [
      "version: 1",
      "users:",
      "  - sub: guest",
      "    name: Guest",
      "    email: guest@example.internal",
      "    groups: []",
      "  - sub: app-developer",
      "    name: App developer",
      "    email: app.developer@example.internal",
      "    groups: [payments-eng]",
    ].join("\n"),
  );
  config.configDir = tmpDir;
});

afterEach(() => {
  config.configDir = originalConfigDir;
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe("buildCommands", () => {
  it("always includes /clear and /attest", () => {
    const commands = buildCommands(false);
    expect(commands.find((c) => c.name === "/clear")).toMatchObject({ kind: "client" });
    expect(commands.find((c) => c.name === "/attest")).toMatchObject({ kind: "message", args: "<cluster>" });
  });

  it("omits /persona outside dev auth mode", () => {
    const commands = buildCommands(false);
    expect(commands.find((c) => c.name === "/persona")).toBeUndefined();
  });

  it("includes /persona with the available subs in dev auth mode", () => {
    const commands = buildCommands(true);
    const persona = commands.find((c) => c.name === "/persona");
    expect(persona?.kind).toBe("client");
    expect(persona?.description).toContain("guest");
    expect(persona?.description).toContain("app-developer");
  });

  it("adds one command per enabled skill, skipping disabled skills", () => {
    const commands = buildCommands(false);
    const skillCmd = commands.find((c) => c.name === "/cluster-health-triage");
    expect(skillCmd).toBeDefined();
    expect(skillCmd?.kind).toBe("message");
    expect(skillCmd?.description).toBe("cluster health triage");
    expect(skillCmd?.template).toContain("Use the cluster health triage skill:");
    expect(commands.find((c) => c.name === "/disabled-skill")).toBeUndefined();
  });

  it("degrades to built-ins only when agent.yaml is missing", () => {
    fs.rmSync(path.join(tmpDir, "agent", "agent.yaml"));
    const commands = buildCommands(false);
    expect(commands.map((c) => c.name)).toEqual(["/clear", "/attest"]);
  });
});
