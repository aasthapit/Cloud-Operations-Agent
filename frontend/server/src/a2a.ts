/**
 * Hand-rolled A2A 1.0 client for the agent hop (decision D4).
 *
 * Wire facts this file encodes (verified against a2a-sdk source during
 * research; see PRD 5.2):
 * - JSON-RPC method SendStreamingMessage at the RPC root, protojson
 *   camelCase, SSE response where each `data:` line is a JSON-RPC envelope
 *   whose result is oneof {task|message|statusUpdate|artifactUpdate}.
 * - The `A2A-Version: 1.0` header is REQUIRED; a missing header is
 *   interpreted as protocol 0.3 and refused by the 1.0 handler.
 * - contextId is the conversation thread id (maps to the ADK session).
 * - Request-level params.metadata carries the identity claims (FR-ID-1);
 *   the agent reads it via RunConfig.custom_metadata.
 */
import { randomUUID } from "node:crypto";

import { injectTraceHeaders } from "./telemetry.js";

export interface A2AClaims {
  sub: string;
  name: string;
  email: string;
  groups: string[];
}

export interface A2AStreamEvent {
  kind: "task" | "message" | "statusUpdate" | "artifactUpdate";
  taskId?: string;
  contextId?: string;
  state?: string;
  texts: string[];
}

/** Pull contextId/taskId/state/text parts out of one result envelope. */
function normalizeResult(result: Record<string, unknown>): A2AStreamEvent | null {
  const partTexts = (parts: unknown): string[] =>
    Array.isArray(parts)
      ? parts.map((p) => (p && typeof p === "object" ? String((p as { text?: string }).text ?? "") : "")).filter(Boolean)
      : [];

  if (result.task && typeof result.task === "object") {
    const task = result.task as Record<string, unknown>;
    return {
      kind: "task",
      taskId: task.id as string,
      contextId: task.contextId as string,
      state: (task.status as Record<string, unknown> | undefined)?.state as string,
      texts: [],
    };
  }
  if (result.statusUpdate && typeof result.statusUpdate === "object") {
    const u = result.statusUpdate as Record<string, unknown>;
    const status = (u.status ?? {}) as Record<string, unknown>;
    const message = (status.message ?? {}) as Record<string, unknown>;
    return {
      kind: "statusUpdate",
      taskId: u.taskId as string,
      contextId: u.contextId as string,
      state: status.state as string,
      texts: partTexts(message.parts),
    };
  }
  if (result.artifactUpdate && typeof result.artifactUpdate === "object") {
    const u = result.artifactUpdate as Record<string, unknown>;
    const artifact = (u.artifact ?? {}) as Record<string, unknown>;
    return {
      kind: "artifactUpdate",
      taskId: u.taskId as string,
      contextId: u.contextId as string,
      texts: partTexts(artifact.parts),
    };
  }
  if (result.message && typeof result.message === "object") {
    const m = result.message as Record<string, unknown>;
    return { kind: "message", contextId: m.contextId as string, texts: partTexts(m.parts) };
  }
  return null;
}

/**
 * Send one user turn and yield normalized stream events.
 * Throws on transport or JSON-RPC error; the caller maps that to the UI's
 * error event with a correlation id (NFR-LOG-2).
 */
export async function* sendStreamingMessage(
  agentUrl: string,
  message: string,
  claims: A2AClaims,
  contextId?: string,
): AsyncGenerator<A2AStreamEvent> {
  const body = {
    jsonrpc: "2.0",
    id: randomUUID(),
    method: "SendStreamingMessage",
    params: {
      message: {
        messageId: randomUUID(),
        role: "ROLE_USER",
        parts: [{ text: message }],
        ...(contextId ? { contextId } : {}),
      },
      metadata: { claims },
    },
  };

  const headers = injectTraceHeaders({
    "Content-Type": "application/json",
    Accept: "text/event-stream",
    "A2A-Version": "1.0",
  });

  const resp = await fetch(agentUrl.replace(/\/$/, "") + "/", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`agent A2A endpoint returned HTTP ${resp.status}`);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    // sse-starlette emits CRLF line endings; normalize after concatenation
    // so a CRLF split across chunks still collapses correctly.
    buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
    // SSE frames are separated by a blank line; a frame may hold multiple
    // `data:` lines that concatenate.
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const data = frame
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (!data) continue;
      let envelope: Record<string, unknown>;
      try {
        envelope = JSON.parse(data) as Record<string, unknown>;
      } catch {
        continue; // tolerate keep-alive noise
      }
      if (envelope.error) {
        const err = envelope.error as { message?: string };
        throw new Error(`A2A error: ${err.message ?? "unknown"}`);
      }
      const event = normalizeResult((envelope.result ?? {}) as Record<string, unknown>);
      if (event) yield event;
    }
  }
}
