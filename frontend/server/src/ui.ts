/**
 * SPA console configuration (`GET /api/ui`), extracted out of the web
 * bundle so the poll interval, log cap, and composer copy are config-plane
 * edits, not code changes (PRD "Config and prompt extraction").
 *
 * Hot-read per request, like users.yaml: a missing or malformed file
 * degrades to the built-in defaults rather than failing the SPA's boot,
 * mirrored by the same defaults on the client for when the endpoint itself
 * is unreachable.
 */
import fs from "node:fs";
import path from "node:path";

import YAML from "yaml";

import { config } from "./config.js";
import { logger } from "./logger.js";

export interface UiConfig {
  metaPollMs: number;
  activityLogCap: number;
  composer: {
    placeholder: string;
    emptyStateProse: string;
    emptyStateExamples: string[];
  };
}

export const DEFAULT_UI_CONFIG: UiConfig = {
  metaPollMs: 5000,
  activityLogCap: 200,
  composer: {
    placeholder: "Ask a question…",
    emptyStateProse:
      "Ask about an application or a cluster; I attest platform health before every answer.",
    emptyStateExamples: ["Why is payments-api flaky in prod?", "attest prod-east-2"],
  },
};

export function loadUiConfig(): UiConfig {
  const file = path.join(config.configDir, "ui", "console.yaml");
  try {
    const raw = (YAML.parse(fs.readFileSync(file, "utf8")) ?? {}) as Partial<UiConfig>;
    return {
      metaPollMs: raw.metaPollMs ?? DEFAULT_UI_CONFIG.metaPollMs,
      activityLogCap: raw.activityLogCap ?? DEFAULT_UI_CONFIG.activityLogCap,
      composer: {
        placeholder: raw.composer?.placeholder ?? DEFAULT_UI_CONFIG.composer.placeholder,
        emptyStateProse: raw.composer?.emptyStateProse ?? DEFAULT_UI_CONFIG.composer.emptyStateProse,
        emptyStateExamples:
          raw.composer?.emptyStateExamples ?? DEFAULT_UI_CONFIG.composer.emptyStateExamples,
      },
    };
  } catch (err) {
    logger.warn({ err }, "ui.console_yaml_unavailable");
    return DEFAULT_UI_CONFIG;
  }
}
