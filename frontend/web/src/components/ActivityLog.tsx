/** The dark activity log (FR-UI-1): phases, tool outcomes, thread id. */
import { useEffect, useRef } from "react";

import type { LogLine } from "../types";

export function ActivityLog(props: { logs: LogLine[]; contextId: string | null }) {
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [props.logs]);

  return (
    <div className="log">
      <div className="hd">
        Activity log
        <span>{props.contextId ? props.contextId.slice(0, 12) : "no thread"}</span>
      </div>
      <div className="lines" ref={scroller}>
        {props.logs.map((line, i) => (
          <div key={i}>
            <span className="ts">{line.at}</span>{" "}
            <span className="tone-dim">{line.tag.padEnd(6)}</span>{" "}
            <span className={`tone-${line.tone}`}>{line.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
