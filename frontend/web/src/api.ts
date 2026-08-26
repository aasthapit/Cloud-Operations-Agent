/**
 * BFF API access: personas, meta, and the chat SSE stream.
 * The stream reader parses `data:` frames into StreamEvent objects and
 * hands them to the caller's callback in arrival order.
 */
import type { Persona, StreamEvent } from "./types";

export async function fetchUsers(): Promise<Persona[]> {
  const res = await fetch("/api/users");
  const body = (await res.json()) as { users: Persona[] };
  return body.users;
}

export async function fetchMeta(): Promise<{ mode: string; env: string; configVersion: string }> {
  const res = await fetch("/api/meta");
  return (await res.json()) as { mode: string; env: string; configVersion: string };
}

export async function streamChat(
  message: string,
  userSub: string,
  contextId: string | null,
  onEvent: (event: StreamEvent) => void,
): Promise<void> {
  const res = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
