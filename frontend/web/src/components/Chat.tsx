/**
 * The AI Assistant column: streamed markdown turns, phase ticks, inline
 * report cards, clarification quick-picks (FR-UI-5), and the input row.
 */
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { ChatItem } from "../state";
import { ReportCard } from "./ReportCard";

/**
 * Local models pepper their markdown with literal <br> tags, which our
 * renderer (correctly, NFR-SEC-5: no raw HTML) shows as text. Normalize
 * them: inside GFM table rows a real newline would split the row, so use
 * "; " there; elsewhere a paragraph break preserves the intent.
 */
function normalizeModelMarkdown(text: string): string {
  return text
    .split("\n")
    .map((line) =>
      line.replace(/<br\s*\/?>/gi, line.trimStart().startsWith("|") ? "; " : "\n\n"),
    )
    .join("\n");
}

/** Verdict deltas vs the previous attestation, carried on the done tick (F5). */
type VerdictChange = { cluster: string; from: string; to: string; note?: string };

function PhaseLine(props: { payload: Record<string, unknown> }) {
  const { phase, status } = props.payload as { phase: string; status: string };
  const detail =
    phase === "attestation" && status === "start"
      ? ` - ${((props.payload.clusters as string[]) ?? []).join(", ")}`
      : phase === "attestation" && status === "done"
        ? ` - ${Object.entries((props.payload.verdicts as Record<string, string>) ?? {})
            .map(([c, v]) => `${c}: ${v}`)
            .join(", ")}`
        : "";
  const changes = (props.payload.changes as VerdictChange[] | undefined) ?? [];
  return (
    <div className="phase-line">
      {status === "start" ? <span className="spin">⟳</span> : <span className="tick">✓</span>}
      {phase} {status}
      {detail}
      {changes.length > 0 && (
        <strong title={changes.map((c) => c.note).filter(Boolean).join("; ")}>
          {" "}
          changed:{" "}
          {changes
            .map((c) =>
              // Same verdict means the signal set moved (checks cleared or appeared);
              // "degraded → degraded" reads like a bug, so say what actually happened.
              c.from === c.to ? `${c.cluster} still ${c.to}${c.note ? ` (${c.note})` : ""}` : `${c.cluster} ${c.from} → ${c.to}`,
            )
            .join(", ")}
        </strong>
      )}
    </div>
  );
}

export function Chat(props: {
  items: ChatItem[];
  busy: boolean;
  onSend: (text: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [props.items]);

  const send = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || props.busy) return;
    setDraft("");
    props.onSend(trimmed);
  };

  return (
    <div className="chatcol">
      <div className="card">
        <div className="hd">
          AI Assistant
          <span className={`pill ${props.busy ? "streaming" : "idle"}`}>
            {props.busy ? "streaming" : "idle"}
          </span>
        </div>
        <div className="turns" ref={scroller}>
          {props.items.length === 0 && (
            <div className="bubble agent">
              <div className="who">Agent</div>
              Ask about an application or a cluster; I attest platform health before every answer.
              Try: <em>Why is payments-api flaky in prod?</em> or <em>attest prod-east-2</em>.
            </div>
          )}
          {props.items.map((item, i) => {
            switch (item.kind) {
              case "user":
                return (
                  <div className="bubble user" key={i}>
                    <div className="who">You</div>
                    {item.text}
                  </div>
                );
              case "agent":
                return (
                  <div className="bubble agent" key={i}>
                    <div className="who">Agent</div>
                    <ReactMarkdown remarkPlugins={[remarkGfm]}>{normalizeModelMarkdown(item.text)}</ReactMarkdown>
                  </div>
                );
              case "phase":
                return <PhaseLine payload={item.payload} key={i} />;
              case "app360":
                return <ReportCard report={item.report} key={i} />;
              case "clarify":
                return (
                  <div className="bubble agent" key={i}>
                    <div className="who">Agent</div>
                    {item.payload.question}
                    <div className="quick">
                      {item.payload.options.map((option) => (
                        <button key={option} onClick={() => send(option)} disabled={props.busy}>
                          {option}
                        </button>
                      ))}
                    </div>
                  </div>
                );
              case "error":
                return (
                  <div className="bubble agent" key={i}>
                    <div className="who">Agent</div>
                    <span className="pill error">error</span>{" "}
                    {/* Agent error fences carry no message field; the prose arrives as a separate narrative item. */}
                    {(item.payload.message as string | undefined) ??
                      (item.payload.phase === "narrative"
                        ? "Narrative analysis is unavailable for this turn; the cards above stand."
                        : "This turn could not be completed.")}{" "}
                    <span className="mono">correlation {item.payload.correlation_id}</span>
                  </div>
                );
              default:
                return null;
            }
          })}
        </div>
        <form
          className="inrow"
          onSubmit={(e) => {
            e.preventDefault();
            send(draft);
          }}
        >
          <input
            value={draft}
            placeholder="Ask a question…"
            onChange={(e) => setDraft(e.target.value)}
            disabled={props.busy}
          />
          <button type="submit" disabled={props.busy || !draft.trim()}>
            Send
          </button>
        </form>
      </div>
    </div>
  );
}
