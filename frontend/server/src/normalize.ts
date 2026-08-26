/**
 * The cloudops fence parser: agent text stream -> typed UI events.
 *
 * Contract (backend/src/cloudops/agent/protocol.py is the source of truth):
 * structured payloads arrive as ```cloudops-<kind> fenced JSON blocks inside
 * the A2A text stream. This parser is INCREMENTAL because analyst tokens
 * stream in fragments: a fence may be split across chunks, so text is only
 * released once we know it is not the beginning of a fence.
 */

export type UiEvent =
  | { type: "text"; delta: string }
  | { type: "card"; kind: string; payload: unknown };

const FENCE_OPEN = "```cloudops-";
const FENCE_CLOSE = "\n```";

export class FenceParser {
  private buffer = "";

  /** Feed one chunk; get the UI events that are now unambiguous. */
  push(chunk: string): UiEvent[] {
    this.buffer += chunk;
    const events: UiEvent[] = [];
    for (;;) {
      const start = this.buffer.indexOf(FENCE_OPEN);
      if (start < 0) {
        // No fence start: release everything except a tail that could be
        // the beginning of one ("`", "``", "```cloudops" ...).
        const hold = this.holdbackLength();
        const release = this.buffer.slice(0, this.buffer.length - hold);
        if (release) events.push({ type: "text", delta: release });
        this.buffer = this.buffer.slice(this.buffer.length - hold);
        return events;
      }
      if (start > 0) {
        events.push({ type: "text", delta: this.buffer.slice(0, start) });
        this.buffer = this.buffer.slice(start);
        continue;
      }
      // Buffer begins with a fence opening; wait for the closing fence.
      const nl = this.buffer.indexOf("\n");
      if (nl < 0) return events; // still reading the info line
      const close = this.buffer.indexOf(FENCE_CLOSE, nl);
      if (close < 0) return events; // fence body still streaming
      const kind = this.buffer.slice(FENCE_OPEN.length, nl).trim();
      const body = this.buffer.slice(nl + 1, close);
      this.buffer = this.buffer.slice(close + FENCE_CLOSE.length);
      try {
        events.push({ type: "card", kind, payload: JSON.parse(body) });
      } catch {
        // Malformed fence: surface it as text so nothing is silently lost.
        events.push({ type: "text", delta: body });
      }
    }
  }

  /** Flush at end of turn: whatever is held cannot be a fence anymore. */
  flush(): UiEvent[] {
    const rest = this.buffer;
    this.buffer = "";
    return rest ? [{ type: "text", delta: rest }] : [];
  }

  /** How many trailing chars might be an incomplete FENCE_OPEN prefix. */
  private holdbackLength(): number {
    const max = Math.min(FENCE_OPEN.length - 1, this.buffer.length);
    for (let n = max; n > 0; n--) {
      if (this.buffer.endsWith(FENCE_OPEN.slice(0, n))) return n;
    }
    return 0;
  }
}
