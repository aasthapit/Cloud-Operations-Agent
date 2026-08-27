/**
 * The console BFF (backend-for-frontend): the SPA's single origin.
 *
 * Responsibilities (PRD 5.1): serve the SPA, expose the dev identity list,
 * relay chat turns to the agent over A2A with identity claims stamped into
 * request metadata (FR-ID-1/2), and normalize the A2A stream + cloudops
 * fences into simple typed SSE events for the browser. No triage logic
 * lives here (FR-UI-6 stays honest because the browser only ever sees this
 * API).
 */
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import express from "express";

import { sendStreamingMessage, type A2AClaims } from "./a2a.js";
import { AuthError, authConfig, loadUsers, resolveClaims } from "./auth.js";
import { config } from "./config.js";
import { logger } from "./logger.js";
import { FenceParser } from "./normalize.js";
import { setupTelemetry, tracer } from "./telemetry.js";

setupTelemetry();
const app = express();
app.use(express.json({ limit: "256kb" }));

/** Config-version chip: same file set the agent hashes (user flow F9). */
function configVersion(): string {
  const files = [
    "checks/health_attestation.yaml",
    "checks/app360.yaml",
    "agent/system_prompt.md",
    "agent/routing.md",
    "agent/agent.yaml",
    "models.yaml",
  ].sort();
  const h = createHash("sha1");
  for (const f of files) {
    try {
      h.update(fs.readFileSync(path.join(config.configDir, f)));
    } catch {
      h.update(`missing:${f}`);
    }
  }
  return h.digest("hex").slice(0, 7);
}

app.get("/healthz", (_req, res) => {
  res.json({ ok: true });
});

app.get("/api/users", (_req, res) => {
  if (!config.isDev() || authConfig().mode !== "dev") {
    // The picker is a dev-mode affordance only (FR-UI-4); oidc mode takes
    // identity exclusively from the verified bearer token.
    res.json({ users: [] });
    return;
  }
  try {
    res.json({ users: loadUsers() });
  } catch (err) {
    logger.error({ err }, "users.load_failed");
    res.status(500).json({ error: "could not load identity personas" });
  }
});

/** Who the verified token says the caller is (oidc mode; FR-ID-3). */
app.get("/api/me", async (req, res) => {
  if (authConfig().mode !== "oidc") {
    res.json({ mode: "dev" });
    return;
  }
  try {
    const claims = await resolveClaims({ authorization: req.headers.authorization });
    res.json({ mode: "oidc", claims });
  } catch (err) {
    const publicMessage = err instanceof AuthError ? err.publicMessage : "authentication failed";
    res.status(401).json({ error: publicMessage });
  }
});

interface AgentStatus {
  config_version: string;
  batteries: { attestation: { checks: number }; app360: { checks: number } };
  last_error: { file: string; message: string; at: string } | null;
}

/**
 * Agent-side config status (FR-CFG-3): version, battery counts, and the
 * last rejected reload. Short timeout because /api/meta is polled by the
 * rail; when the agent is down the console still gets file-derived values.
 */
async function agentStatus(): Promise<AgentStatus | null> {
  try {
    const res = await fetch(new URL("/status", config.agentUrl), {
      signal: AbortSignal.timeout(1500),
    });
    if (!res.ok) return null;
    return (await res.json()) as AgentStatus;
  } catch (err) {
    logger.debug({ err }, "meta.agent_status_unavailable");
    return null;
  }
}

app.get("/api/meta", async (_req, res) => {
  const status = await agentStatus();
  res.json({
    mode: config.backendMode,
    env: config.env,
    authMode: authConfig().mode,
    // The agent hashes the same file set; its answer wins when it is up so
    // the chip and the reload error come from one reading of the plane.
    configVersion: status?.config_version ?? configVersion(),
    agentReachable: status !== null,
    batteries: status
      ? { attestation: status.batteries.attestation.checks, app360: status.batteries.app360.checks }
      : null,
    reloadError: status?.last_error ?? null,
  });
});

/**
 * One chat turn: POST {message, userSub, contextId?} -> SSE of UI events.
 * Event types: meta | phase | context | clarify | attestation | app360 |
 * error | text | state | done (see web/src/types.ts).
 */
app.post("/api/chat/stream", async (req, res) => {
  const { message, userSub, contextId } = req.body as {
    message?: string;
    userSub?: string;
    contextId?: string;
  };
  if (!message || typeof message !== "string") {
    res.status(400).json({ error: "message is required" });
    return;
  }

  // FR-ID-3's seam, now a config toggle: dev resolves the picker's persona,
  // oidc verifies the bearer token; the claim shape is identical either way.
  let claims: A2AClaims;
  try {
    claims = await resolveClaims({
      authorization: req.headers.authorization,
      userSub,
    });
  } catch (err) {
    const publicMessage = err instanceof AuthError ? err.publicMessage : "authentication failed";
    res.status(401).json({ error: publicMessage });
    return;
  }

  res.writeHead(200, {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });
  const send = (event: Record<string, unknown>) => {
    res.write(`data: ${JSON.stringify(event)}\n\n`);
  };

  const span = tracer().startSpan("bff.chat_stream", {
    attributes: { "thread.id": contextId ?? "new", "user.sub": claims.sub || "-" },
  });
  const parser = new FenceParser();
  let announcedContext = false;
  const started = Date.now();
  try {
    for await (const event of sendStreamingMessage(config.agentUrl, message, claims, contextId)) {
      if (!announcedContext && event.contextId) {
        announcedContext = true;
        send({ type: "meta", contextId: event.contextId });
      }
      // artifactUpdate re-aggregates text already streamed through working
      // statusUpdates (verified against the ADK A2A executor's stream);
      // forwarding it would duplicate the narrative.
      if (event.kind === "artifactUpdate") continue;
      for (const text of event.texts) {
        for (const ui of parser.push(text)) {
          if (ui.type === "text") send({ type: "text", delta: ui.delta });
          else send({ type: ui.kind, payload: ui.payload });
        }
      }
      if (event.kind === "statusUpdate" && event.state && event.state !== "TASK_STATE_WORKING") {
        send({ type: "state", state: event.state });
      }
    }
    for (const ui of parser.flush()) {
      if (ui.type === "text") send({ type: "text", delta: ui.delta });
    }
    logger.info(
      { thread: contextId, user: claims.sub, duration_ms: Date.now() - started },
      "chat.turn_complete",
    );
  } catch (err) {
    // Correlation id, never a stack trace, to the client (NFR-LOG-2).
    const correlationId = span.spanContext().traceId;
    logger.error({ err, correlationId }, "chat.turn_failed");
    send({
      type: "error",
      payload: {
        correlation_id: correlationId,
        message: "The agent could not complete this turn. The correlation id maps to the full trace.",
      },
    });
  } finally {
    span.end();
    send({ type: "done" });
    res.end();
  }
});

// Production: serve the built SPA from web/dist (single origin).
const webDist = path.resolve(import.meta.dirname, "../../web/dist");
if (fs.existsSync(webDist)) {
  app.use(express.static(webDist));
  app.get(/^\/(?!api\/).*/, (_req, res) => {
    res.sendFile(path.join(webDist, "index.html"));
  });
}

app.listen(config.port, "127.0.0.1", () => {
  logger.info({ port: config.port, mode: config.backendMode }, "bff.serving");
});
