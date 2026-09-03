"use client";

import type { RefObject, UIEvent } from "react";
import { fmtLogTime, fmtLogTimeTitle } from "@/lib/format";
import { filterEvents } from "@/lib/activity";

// One event on the unified pipeline bus (backend /debug/stream + browser posts).
export type DebugEvent = {
  seq: number;
  ts: string;
  source: string; // voice | backend | claude | user
  dest: string;
  kind: string; // tool_call | tool_result | send | decision | hook | assistant | inject | transcript | error | poll | info
  session?: string | null;
  summary: string;
  detail?: unknown;
};

// Pipeline activity log (voice<->backend<->Claude). The filter text, the
// errors-only/paused/copied toggles, and the scroll position all live in the
// parent (app/activity/page.tsx) — this stays presentational, deriving the
// filtered rows from the raw `events` it's given.
export function ActivityFeed({
  events,
  streamConnected,
  filter,
  onFilter,
  errorsOnly,
  onToggleErrorsOnly,
  paused,
  onTogglePaused,
  copied,
  onCopy,
  onClear,
  scrollRef,
  onScroll,
}: {
  events: DebugEvent[];
  // Whether the /debug/stream this feed is fed by is currently connected —
  // an empty list on a dead stream is a different situation from an empty
  // list because nothing has happened yet, and only one of those means
  // talking to the agent will make anything show up here.
  streamConnected: boolean;
  filter: string;
  onFilter: (v: string) => void;
  errorsOnly: boolean;
  onToggleErrorsOnly: () => void;
  paused: boolean;
  onTogglePaused: () => void;
  copied: boolean;
  onCopy: (text: string) => void;
  onClear: () => void;
  scrollRef: RefObject<HTMLDivElement | null>;
  onScroll: (e: UIEvent<HTMLDivElement>) => void;
}) {
  const filteredLog = filterEvents(events, filter, errorsOnly);

  // Copy the currently-shown events as readable text (full, untruncated lines)
  // for pasting into a bug report / sharing.
  const handleCopy = () => {
    const text = filteredLog
      .map((e) => `${e.ts}  ${e.source}→${e.dest}  ${e.kind}${e.session ? "  " + e.session : ""}  ${e.summary}`)
      .join("\n");
    onCopy(text);
  };

  return (
    <div className="panel debugpanel">
      {/* No heading here: the panel this renders in supplies one (.viewtitle in
          app/activity/page.tsx), and two "Activity" headings stacked was
          exactly what the panel shell made visible. The shown/total count the
          old heading carried moved up there in the page, which filters with the same lib/activity.ts helper. */}
      <div className="loghead">
        <div className="logctl">
          <input
            className="logsearch"
            placeholder="filter…"
            value={filter}
            onChange={(e) => onFilter(e.target.value)}
          />
          <button className={`textbtn ${errorsOnly ? "on" : ""}`} onClick={onToggleErrorsOnly}>
            Errors
          </button>
          <button className={`textbtn ${paused ? "on" : ""}`} onClick={onTogglePaused}>
            {paused ? "Resume" : "Pause"}
          </button>
          <button className={`textbtn ${copied ? "on" : ""}`} onClick={handleCopy} title="Copy all shown events to the clipboard">
            {copied ? "Copied ✓" : "Copy"}
          </button>
          <button className="textbtn" onClick={onClear}>
            Clear
          </button>
        </div>
      </div>
      <div className="rule" />
      <div className="logscroll" ref={scrollRef} onScroll={onScroll}>
        {filteredLog.length === 0 && (
          <div className="empty">
            {events.length === 0 && !streamConnected
              ? "The activity stream is unreachable right now — this stays empty until the backend comes back."
              : "No matching events yet — talk to the agent and the full pipeline shows here."}
          </div>
        )}
        {filteredLog.map((ev) => (
          <div
            key={ev.seq}
            className={`logrow k-${ev.kind}`}
            title={ev.detail ? JSON.stringify(ev.detail).slice(0, 800) : undefined}
          >
            <span className="lt" title={fmtLogTimeTitle(ev.ts)}>{fmtLogTime(ev.ts)}</span>
            <span className="lhop">
              <span className={`htag ${ev.source}`}>{ev.source}</span>
              <span className="harr">→</span>
              <span className={`htag ${ev.dest}`}>{ev.dest}</span>
            </span>
            <span className="lk">{ev.kind}</span>
            {ev.session && <span className="lsess">{ev.session}</span>}
            <span className="lsum">{ev.summary}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
