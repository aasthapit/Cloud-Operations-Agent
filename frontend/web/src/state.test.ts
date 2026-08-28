/**
 * Reducer tests (Console UX): thought accumulation stays off the narrative
 * field, a reset clears thoughts along with everything else, and the
 * activity log respects the configured cap.
 */
import { describe, expect, it } from "vitest";

import { initialState, reducer } from "./state";
import type { ChatItem } from "./state";

describe("thought accumulation", () => {
  it("accumulates thought chunks separately from narrative text", () => {
    let state = reducer(initialState, { type: "send", text: "why is it down" });
    state = reducer(state, { type: "event", event: { type: "thought", text: "checking placements" } });
    state = reducer(state, { type: "event", event: { type: "thought", text: "... looks fine" } });

    const agent = state.items.find((i): i is Extract<ChatItem, { kind: "agent" }> => i.kind === "agent");
    expect(agent?.thought).toBe("checking placements... looks fine");
    expect(agent?.text).toBe("");
    expect(agent?.thoughtStreaming).toBe(true);
  });

  it("narrative text arriving after a thought stops the streaming flag but keeps the thought", () => {
    let state = reducer(initialState, { type: "send", text: "why is it down" });
    state = reducer(state, { type: "event", event: { type: "thought", text: "checking placements" } });
    state = reducer(state, { type: "event", event: { type: "text", delta: "It looks healthy." } });

    const agent = state.items.find((i): i is Extract<ChatItem, { kind: "agent" }> => i.kind === "agent");
    expect(agent?.thought).toBe("checking placements");
    expect(agent?.text).toBe("It looks healthy.");
    expect(agent?.thoughtStreaming).toBe(false);
  });

  it("a turn ending mid-thought (done with no narrative) still clears thoughtStreaming", () => {
    let state = reducer(initialState, { type: "send", text: "why is it down" });
    state = reducer(state, { type: "event", event: { type: "thought", text: "still thinking" } });
    state = reducer(state, { type: "event", event: { type: "done" } });

    const agent = state.items.find((i): i is Extract<ChatItem, { kind: "agent" }> => i.kind === "agent");
    expect(agent?.thoughtStreaming).toBe(false);
    expect(agent?.thought).toBe("still thinking");
  });

  it("reset clears thoughts along with the rest of the transcript", () => {
    let state = reducer(initialState, { type: "send", text: "why is it down" });
    state = reducer(state, { type: "event", event: { type: "thought", text: "checking placements" } });
    state = reducer(state, { type: "reset" });

    expect(state.items).toEqual([]);
    expect(state.items.some((i) => i.kind === "agent")).toBe(false);
  });

  it("reset preserves the configured log cap", () => {
    let state = reducer(initialState, { type: "configure", logCap: 5 });
    state = reducer(state, { type: "reset" });
    expect(state.logCap).toBe(5);
  });
});

describe("activity log cap", () => {
  it("keeps the log within the configured cap", () => {
    let state = reducer(initialState, { type: "configure", logCap: 3 });
    for (let i = 0; i < 10; i++) {
      state = reducer(state, { type: "send", text: `turn ${i}` });
    }
    expect(state.logs.length).toBeLessThanOrEqual(3);
    expect(state.logs.at(-1)?.text).toContain("turn 9");
  });
});
