/**
 * Console state: one reducer over the BFF's stream events.
 * The chat is a list of typed items (turns, phase ticks, inline cards);
 * the rails render the latest context/attestation; the activity log keeps
 * an append-only trail of phases, cards, and states (FR-UI-1).
 */
import type {
  App360Report,
  AttestationReport,
  ClarifyPayload,
  ContextPayload,
  LogLine,
  PhasePayload,
  StreamEvent,
} from "./types";

export type ChatItem =
  | { kind: "user"; text: string }
  | {
      kind: "agent";
      text: string;
      /** Reasoning-model deliberation for this turn (never exported;
       * export.ts reads only the typed report payloads). */
      thought?: string;
      /** True while thought chunks are still arriving for this turn; the
       * "Thinking …" block renders live only while this is true. */
      thoughtStreaming?: boolean;
    }
  | { kind: "phase"; payload: PhasePayload }
  | { kind: "app360"; report: App360Report }
  | { kind: "clarify"; payload: ClarifyPayload }
  | { kind: "error"; payload: { correlation_id: string; message?: string; phase?: string; reason?: string } };

export interface ConsoleState {
  items: ChatItem[];
  contextId: string | null;
  context: ContextPayload | null;
  attestation: AttestationReport | null;
  lastApp360: App360Report | null;
  busy: boolean;
  logs: LogLine[];
  /** After a clarify card, the same turn's remaining text is the plain-text
   * fallback of the very same question (for card-less A2A clients); the
   * console renders the card, so that text is suppressed until turn end. */
  suppressText: boolean;
  /** GET /api/ui's activityLogCap (config/ui/console.yaml), set once via
   * the "configure" action after boot; log() below reads it fresh. */
  logCap: number;
}

export const initialState: ConsoleState = {
  items: [],
  contextId: null,
  context: null,
  attestation: null,
  lastApp360: null,
  busy: false,
  logs: [],
  suppressText: false,
  logCap: 200,
};

export type Action =
  | { type: "send"; text: string }
  | { type: "event"; event: StreamEvent }
  | { type: "reset" }
  | { type: "configure"; logCap: number };

function now(): string {
  return new Date().toISOString().slice(11, 23);
}

function log(state: ConsoleState, tag: string, text: string, tone: LogLine["tone"] = "dim"): LogLine[] {
  const cap = state.logCap > 0 ? state.logCap : 200;
  return [...state.logs.slice(-(cap - 1)), { at: now(), tag, text, tone }];
}

export function reducer(state: ConsoleState, action: Action): ConsoleState {
  switch (action.type) {
    case "reset":
      // logCap comes from config, not from the conversation, so a reset
      // must not lose it.
      return { ...initialState, logCap: state.logCap, logs: log(state, "ui", "new thread") };
    case "send":
      return {
        ...state,
        busy: true,
        suppressText: false,
        items: [...state.items, { kind: "user", text: action.text }],
        logs: log(state, "ui", `send: ${action.text.slice(0, 60)}`),
      };
    case "configure":
      return { ...state, logCap: action.logCap };
    case "event":
      return onEvent(state, action.event);
    default:
      return state;
  }
}

function appendText(items: ChatItem[], delta: string): ChatItem[] {
  const last = items[items.length - 1];
  if (last && last.kind === "agent") {
    // Real narrative text means the thinking phase for this turn is over,
    // even if the "Thinking …" block is still expanded from streaming.
    return [...items.slice(0, -1), { ...last, text: last.text + delta, thoughtStreaming: false }];
  }
  if (!delta.trim()) return items; // don't open a bubble for pure whitespace
  return [...items, { kind: "agent", text: delta }];
}

/** Thought chunks accumulate on the same agent item as the narrative that
 * follows them, but in their own field so nothing can conflate the two. */
function appendThought(items: ChatItem[], delta: string): ChatItem[] {
  const last = items[items.length - 1];
  if (last && last.kind === "agent") {
    return [...items.slice(0, -1), { ...last, thought: (last.thought ?? "") + delta, thoughtStreaming: true }];
  }
  return [...items, { kind: "agent", text: "", thought: delta, thoughtStreaming: true }];
}

function onEvent(state: ConsoleState, event: StreamEvent): ConsoleState {
  switch (event.type) {
    case "meta":
      return { ...state, contextId: event.contextId };
    case "text":
      if (state.suppressText) return state;
      return { ...state, items: appendText(state.items, event.delta) };
    case "thought":
      return { ...state, items: appendThought(state.items, event.text) };
    case "phase": {
      const p = event.payload;
      const tone = p.status === "start" ? "dim" : "ok";
      return {
        ...state,
        items: [...state.items, { kind: "phase", payload: p }],
        logs: log(state, "phase", `${p.phase} ${p.status}`, tone),
      };
    }
    case "context":
      return {
        ...state,
        context: event.payload,
        logs: log(
          state, "ok",
          event.payload.scope === "cluster"
            ? `context: cluster ${event.payload.clusters[0]}`
            : `context: ${event.payload.application} ${event.payload.environment}`,
          "ok",
        ),
      };
    case "clarify":
      return {
        ...state,
        suppressText: true,
        items: [...state.items, { kind: "clarify", payload: event.payload }],
        logs: log(state, "ask", `clarify: ${event.payload.kind}`, "warn"),
      };
    case "attestation": {
      const verdicts = event.payload.clusters
        .map((c) => `${c.cluster}=${c.verdict}`)
        .join(" ");
      const worst = event.payload.clusters.some((c) => c.verdict === "degraded" || c.verdict === "unattestable");
      return {
        ...state,
        attestation: event.payload,
        logs: log(state, "attest", verdicts, worst ? "crit" : "ok"),
      };
    }
    case "app360":
      return {
        ...state,
        lastApp360: event.payload,
        items: [...state.items, { kind: "app360", report: event.payload }],
        logs: log(
          state, "app360",
          `${event.payload.application}@${event.payload.cluster} ${event.payload.overall_status}`,
          event.payload.overall_status === "healthy" ? "ok" : "warn",
        ),
      };
    case "error":
      return {
        ...state,
        items: [...state.items, { kind: "error", payload: event.payload }],
        logs: log(state, "error", event.payload.correlation_id, "crit"),
      };
    case "state":
      return { ...state, logs: log(state, "a2a", event.state.replace("TASK_STATE_", "").toLowerCase()) };
    case "done": {
      // Defensive: a turn that ends mid-thought (no narrative followed)
      // must still collapse out of the streaming "Thinking …" state.
      const last = state.items[state.items.length - 1];
      const items =
        last && last.kind === "agent" && last.thoughtStreaming
          ? [...state.items.slice(0, -1), { ...last, thoughtStreaming: false }]
          : state.items;
      return { ...state, busy: false, items };
    }
    default:
      return state;
  }
}
