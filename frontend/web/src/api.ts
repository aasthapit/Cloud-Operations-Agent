/**
 * BFF API access: personas, meta, and the chat SSE stream.
 * The stream reader parses `data:` frames into StreamEvent objects and
 * hands them to the caller's callback in arrival order.
 */
import type { ConsoleMeta, Persona, SlashCommand, StreamEvent, UiConfig } from "./types";

/** Fallback if GET /api/ui is unreachable; mirrors frontend/server/src/ui.ts
 * DEFAULT_UI_CONFIG so the console degrades to the same copy either way. */
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

/**
 * In oidc mode a real deployment obtains the token from its login flow; for
 * local testing, paste one into sessionStorage under "cloudops.token" and
 * every API call carries it. Dev mode sends no Authorization header at all.
 */
function authHeaders(): Record<string, string> {
  try {
    const token = sessionStorage.getItem("cloudops.token");
    return token ? { Authorization: `Bearer ${token}` } : {};
  } catch {
    return {};
  }
}

export async function fetchUsers(): Promise<Persona[]> {
  const res = await fetch("/api/users");
  const body = (await res.json()) as { users: Persona[] };
  return body.users;
}

export async function fetchMeta(): Promise<ConsoleMeta> {
  const res = await fetch("/api/meta");
  return (await res.json()) as ConsoleMeta;
}

export async function fetchCommands(): Promise<SlashCommand[]> {
  const res = await fetch("/api/commands");
  const body = (await res.json()) as { commands: SlashCommand[] };
  return body.commands;
}

export async function fetchUiConfig(): Promise<UiConfig> {
  const res = await fetch("/api/ui");
  if (!res.ok) return DEFAULT_UI_CONFIG;
  return (await res.json()) as UiConfig;
}

/** Verified identity in oidc mode; { mode: "dev" } otherwise. */
export async function fetchMe(): Promise<{ mode: string; claims?: Persona }> {
  const res = await fetch("/api/me", { headers: authHeaders() });
  if (!res.ok) return { mode: "oidc" };
  return (await res.json()) as { mode: string; claims?: Persona };
}

export async function streamChat(
  message: string,
  userSub: string,
  contextId: string | null,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ message, userSub, contextId: contextId ?? undefined }),
  });
  if (!res.ok || !res.body) {
    onEvent({
      type: "error",
      payload: { correlation_id: "-", message: `console backend returned HTTP ${res.status}` },
    });
    onEvent({ type: "done" });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
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
      try {
        onEvent(JSON.parse(data) as StreamEvent);
      } catch {
        // tolerate malformed frames rather than killing the stream
      }
    }
  }
}
