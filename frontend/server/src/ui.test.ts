/**
 * SPA console config tests (Console UX): merges a partial console.yaml
 * over the built-in defaults, and degrades cleanly when the file is
 * missing or malformed.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { config } from "./config.js";
import { DEFAULT_UI_CONFIG, loadUiConfig } from "./ui.js";

let tmpDir: string;
let originalConfigDir: string;

beforeEach(() => {
  originalConfigDir = config.configDir;
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cloudops-ui-"));
  fs.mkdirSync(path.join(tmpDir, "ui"), { recursive: true });
  config.configDir = tmpDir;
});

afterEach(() => {
  config.configDir = originalConfigDir;
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe("loadUiConfig", () => {
  it("returns the built-in defaults when console.yaml is missing", () => {
    expect(loadUiConfig()).toEqual(DEFAULT_UI_CONFIG);
  });

  it("returns the built-in defaults when console.yaml is malformed", () => {
    fs.writeFileSync(path.join(tmpDir, "ui", "console.yaml"), "not: [valid: yaml");
    expect(loadUiConfig()).toEqual(DEFAULT_UI_CONFIG);
  });

  it("overrides only the fields the file sets", () => {
    fs.writeFileSync(
      path.join(tmpDir, "ui", "console.yaml"),
      ["version: 1", "metaPollMs: 9000", "composer:", "  placeholder: custom placeholder"].join("\n"),
    );
    const cfg = loadUiConfig();
    expect(cfg.metaPollMs).toBe(9000);
    expect(cfg.activityLogCap).toBe(DEFAULT_UI_CONFIG.activityLogCap);
    expect(cfg.composer.placeholder).toBe("custom placeholder");
    expect(cfg.composer.emptyStateProse).toBe(DEFAULT_UI_CONFIG.composer.emptyStateProse);
  });

  it("reads the full shape from a complete file", () => {
    fs.writeFileSync(
      path.join(tmpDir, "ui", "console.yaml"),
      [
        "version: 1",
        "metaPollMs: 1000",
        "activityLogCap: 50",
        "composer:",
        "  placeholder: ask away",
        "  emptyStateProse: prose here",
        "  emptyStateExamples:",
        "    - one",
        "    - two",
      ].join("\n"),
    );
    expect(loadUiConfig()).toEqual({
      metaPollMs: 1000,
      activityLogCap: 50,
      composer: { placeholder: "ask away", emptyStateProse: "prose here", emptyStateExamples: ["one", "two"] },
    });
  });
});
