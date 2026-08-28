/**
 * The AI Assistant column: streamed markdown turns, phase ticks, inline
 * report cards, clarification quick-picks (FR-UI-5), a slash-command
 * palette, and the input row.
 */
import { Fragment, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import type { ChatItem } from "../state";
import type { SlashCommand, UiConfig } from "../types";
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

/**
 * The per-turn "Thinking" block: dimmed, italic, monospace-adjacent, above
 * the narrative. Collapsed by default; forced open with a shimmer while
 * `streaming` is true so the console never looks stalled during a long
 * reasoning pass, then collapses to a "Show thinking (n chars)" toggle.
 */
function ThoughtBlock(props: { thought: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!props.thought) return null;
  const expanded = props.streaming || open;
  const chars = props.thought.length;
  const lines = props.thought.split("\n").length;
  return (
    <div className="thought">
      <button
        type="button"
        className="thought-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={expanded}
      >
        {props.streaming ? (
          <span className="thought-shimmer">Thinking …</span>
        ) : open ? (
          "Hide thinking"
        ) : (
          `Show thinking (${chars} chars, ${lines} line${lines === 1 ? "" : "s"})`
        )}
      </button>
      {expanded && <div className="thought-body">{props.thought}</div>}
    </div>
  );
}

/** Command name typed so far, and its argument remainder if the composer
 * already has "<name> <remainder>". Empty remainder means no arg yet. */
function splitCommand(draft: string, name: string): string | null {
  if (draft === name) return "";
  if (draft.startsWith(name + " ")) return draft.slice(name.length + 1).trim();
  return null;
}

function CommandPalette(props: {
  items: SlashCommand[];
  activeIndex: number;
  onSelect: (cmd: SlashCommand) => void;
  onHover: (index: number) => void;
}) {
  if (props.items.length === 0) return null;
  return (
    <div className="palette" role="listbox">
      {props.items.map((c, i) => (
        <div
          key={c.name}
          role="option"
          aria-selected={i === props.activeIndex}
          className={`palette-item ${i === props.activeIndex ? "active" : ""}`}
          onMouseEnter={() => props.onHover(i)}
          // mousedown (not click) fires before the input blurs, so the
          // selection lands before focus/composer state settles.
          onMouseDown={(e) => {
            e.preventDefault();
            props.onSelect(c);
          }}
        >
          <span className="palette-name">
            {c.name}
            {c.args ? ` ${c.args}` : ""}
          </span>
          <span className="palette-desc">{c.description}</span>
        </div>
      ))}
    </div>
  );
}

export function Chat(props: {
  items: ChatItem[];
  busy: boolean;
  onSend: (text: string) => void;
  commands: SlashCommand[];
  onNewThread: () => void;
  onSelectPersona: (sub: string) => void;
  composer: UiConfig["composer"];
}) {
  const [draft, setDraft] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteIndex, setPaletteIndex] = useState(0);
  const scroller = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // Follow the stream only while the user is already near the bottom;
    // yanking them back down on every chunk makes reading a tall report
    // mid-stream impossible. In the stacked (narrow) layout the chat has
    // no inner scrollbar at all and the PAGE is the scroll container, so
    // follow whichever one actually scrolls.
    const el = scroller.current;
    if (!el) return;
    const innerScrolls = el.scrollHeight - el.clientHeight > 4;
    if (innerScrolls) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      if (nearBottom) el.scrollTo({ top: el.scrollHeight });
    } else {
      const doc = document.documentElement;
      const nearBottom = doc.scrollHeight - window.scrollY - window.innerHeight < 160;
      if (nearBottom) window.scrollTo({ top: doc.scrollHeight });
    }
  }, [props.items]);

  const send = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || props.busy) return;
    // Slash commands go through this same path whether typed by hand or
    // dropped in by the palette, so both routes get identical handling.
    if (trimmed === "/clear" || trimmed.startsWith("/clear ")) {
      setDraft("");
      setPaletteOpen(false);
      props.onNewThread();
      return;
    }
    const personaArg = splitCommand(trimmed, "/persona");
    if (personaArg !== null && personaArg) {
      setDraft("");
      setPaletteOpen(false);
      props.onSelectPersona(personaArg);
      return;
    }
    // A command typed out by hand gets the same treatment the palette gives
    // it: /attest becomes the agent's "attest <cluster>" phrasing, and a
    // message command's template is dropped in for review. The agent never
    // sees a raw "/..." string.
    const attestArg = splitCommand(trimmed, "/attest");
    if (attestArg !== null) {
      setPaletteOpen(false);
      if (attestArg) {
        setDraft("");
        props.onSend(`attest ${attestArg}`);
      } else {
        setDraft("attest <cluster> ");
        inputRef.current?.focus();
      }
      return;
    }
    const typedName = trimmed.split(/\s+/)[0].toLowerCase();
    const typedCommand = trimmed.startsWith("/")
      ? props.commands.find((c) => c.name === typedName)
      : undefined;
    if (typedCommand) {
      setPaletteOpen(false);
      setDraft(typedCommand.template ? `${typedCommand.template} ` : `${typedCommand.description} `);
      inputRef.current?.focus();
      return;
    }
    setDraft("");
    setPaletteOpen(false);
    props.onSend(trimmed);
  };

  const paletteItems = props.commands.filter((c) => c.name.toLowerCase().startsWith(draft.toLowerCase()));

  const onDraftChange = (value: string) => {
    setDraft(value);
    const slashPrefix = value.startsWith("/") && !value.slice(1).includes(" ");
    if (value === "/") {
      setPaletteOpen(true);
      setPaletteIndex(0);
    } else if (paletteOpen && slashPrefix) {
      setPaletteIndex((i) => Math.min(i, Math.max(paletteItems.length - 1, 0)));
    } else {
      setPaletteOpen(false);
    }
  };

  const selectCommand = (cmd: SlashCommand) => {
    setPaletteOpen(false);
    if (cmd.name === "/clear") {
      send("/clear");
      return;
    }
    if (cmd.name === "/persona") {
      const arg = splitCommand(draft, "/persona");
      if (arg) {
        send(`/persona ${arg}`);
      } else {
        setDraft("/persona ");
        inputRef.current?.focus();
      }
      return;
    }
    if (cmd.name === "/attest") {
      const arg = splitCommand(draft, "/attest");
      if (arg) {
        send(`attest ${arg}`);
      } else {
        setDraft("attest <cluster> ");
        inputRef.current?.focus();
      }
      return;
    }
    // A skill (or any other server-supplied) message command: prefill its
    // template for editing rather than sending it unreviewed.
    setDraft(cmd.template ? `${cmd.template} ` : `${cmd.description} `);
    inputRef.current?.focus();
  };

  const onInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!paletteOpen || paletteItems.length === 0) {
      // Submit on Enter explicitly rather than trusting implicit form
      // submission, which some environments (and synthesized key events)
      // never fire.
      if (e.key === "Enter") {
        e.preventDefault();
        send(draft);
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setPaletteIndex((i) => (i + 1) % paletteItems.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setPaletteIndex((i) => (i - 1 + paletteItems.length) % paletteItems.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      selectCommand(paletteItems[paletteIndex]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setPaletteOpen(false);
    }
  };

  return (
    <div className="chatcol">
      <div className="card">
        <div className="hd">
          AI Assistant
          <span className="hd-actions">
            <button type="button" className="new-thread" onClick={props.onNewThread} disabled={props.busy}>
              New thread
            </button>
            <span className={`pill ${props.busy ? "streaming" : "idle"}`}>
              {props.busy ? "streaming" : "idle"}
            </span>
          </span>
        </div>
        <div className="turns" ref={scroller}>
          {props.items.length === 0 && (
            <div className="bubble agent">
              <div className="who">Agent</div>
              {props.composer.emptyStateProse}{" "}
              Try:{" "}
              {props.composer.emptyStateExamples.map((example, i) => (
                <Fragment key={example}>
                  {i > 0 ? " or " : ""}
                  <em>{example}</em>
                </Fragment>
              ))}
              .
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
                    {item.thought && <ThoughtBlock thought={item.thought} streaming={item.thoughtStreaming} />}
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
          className="inrow-wrap"
          onSubmit={(e) => {
            e.preventDefault();
            send(draft);
          }}
        >
          {paletteOpen && (
            <CommandPalette
              items={paletteItems}
              activeIndex={paletteIndex}
              onSelect={selectCommand}
              onHover={setPaletteIndex}
            />
          )}
          <div className="inrow">
            <input
              ref={inputRef}
              value={draft}
              placeholder={props.composer.placeholder}
              onChange={(e) => onDraftChange(e.target.value)}
              onKeyDown={onInputKeyDown}
              disabled={props.busy}
            />
            <button type="submit" disabled={props.busy || !draft.trim()}>
              Send
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
