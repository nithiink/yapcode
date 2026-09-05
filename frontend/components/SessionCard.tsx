"use client";

import { Icon } from "./ui/Icon";
import { CopyBtn } from "./ui/CopyBtn";
import LiveTerminal from "./LiveTerminal";
import { sessionStatus, sessionLabel, MODES, type Sess } from "@/lib/sessions";
import { abbrevHome } from "@/lib/format";

// One event in a Claude session's own transcript (distinct from the voice
// conversation's TimelineItem — this is the read_transcript/poll shape).
export type TxEvent =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string }
  | { kind: "tool"; name: string; summary: string; risky: boolean }
  | { kind: "tool_result"; ok: boolean; text: string };

// Also used by the sessions view's fullscreen transcript overlay, which is
// not part of this card.
export function renderTimeline(events: TxEvent[]) {
  if (events.length === 0) return <div className="empty">No transcript yet.</div>;
  return events.map((e, i) => {
    if (e.kind === "tool")
      return (
        <div key={i} className="tx tool">
          <span className="tag">{e.risky ? "🔒" : "🔧"} {e.name}</span> {e.summary}
        </div>
      );
    if (e.kind === "tool_result")
      return (
        <div key={i} className={`tx result ${e.ok ? "" : "err"}`}>
          ↳ {e.ok ? "✓" : "✗"} {e.text}
        </div>
      );
    return (
      <div key={i} className={`tx ${e.kind}`}>
        <span className="who">{e.kind === "user" ? "You→Claude" : "Claude"}</span>
        {e.text}
      </div>
    );
  });
}

// One agent session as it appears in the "Agent sessions" panel: status,
// queued turns, an optional live-terminal box, the mode switcher, the
// transcript toggle, and the "continue in your terminal" handoff.
//
// All the UI state this card touches (which row is being renamed, whether the
// transcript is open, whether the live view is fullscreen, which session is
// live/mode-busy) is owned by the parent and arrives as props — this stays
// purely presentational.
export function SessionCard({
  s,
  open,
  live,
  modeBusy,
  onToggleTranscript,
  onWatch,
  onSwitchMode,
  transcript,
  editing,
  draftName,
  onDraftNameChange,
  onCommitRename,
  onCancelRename,
  onStartRename,
  liveFullscreen,
  onExpandLive,
  onMinimizeLive,
  onExpandTranscript,
  attachCommand,
  attachLoading,
  onOpenHandoff,
}: {
  s: Sess;
  open: boolean; // transcript expanded
  live: boolean; // this session is the one being watched
  modeBusy: boolean;
  onToggleTranscript: () => void;
  onWatch: () => void;
  onSwitchMode: (mode: string) => void;
  transcript: TxEvent[];
  editing: boolean; // this session's name is being edited
  draftName: string;
  onDraftNameChange: (v: string) => void;
  onCommitRename: () => void;
  onCancelRename: () => void;
  onStartRename: () => void;
  liveFullscreen: boolean;
  onExpandLive: () => void;
  onMinimizeLive: () => void;
  onExpandTranscript: () => void;
  // The tmux co-drive command, fetched from the backend's own get_handoff
  // (never fabricated client-side — see lib/sessions.ts's removed
  // tmuxAttachCommand). undefined: not fetched yet (the handoff panel hasn't
  // been opened); null: fetched, and this backend genuinely has no pane.
  attachCommand?: string | null;
  attachLoading?: boolean;
  // Fetch attachCommand lazily — called when the handoff <details> is opened,
  // not for every card on every render.
  onOpenHandoff?: () => void;
}) {
  // From the provider — only it knows how to reopen its own session.
  const cmd = s.resume_command || null;
  const st = sessionStatus(s);
  const queuedTurns = (s.queue || []).filter((q) => q.state === "queued");

  return (
    <div className="sess">
      <div className="shead">
        {editing ? (
          <input
            className="nameedit"
            autoFocus
            value={draftName}
            onChange={(e) => onDraftNameChange(e.target.value)}
            onBlur={() => onCommitRename()}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCommitRename();
              else if (e.key === "Escape") onCancelRename();
            }}
          />
        ) : (
          <button className="name namebtn" title="Rename session" onClick={() => onStartRename()}>
            {sessionLabel(s)}
            <span className="penicon" aria-hidden>
              <Icon name="edit" size={15} strokeWidth={1.7} />
            </span>
          </button>
        )}
      </div>

      {/* Status strip — what the session is doing right now. */}
      <div className={`statusline ${st.cls}`}>
        <span className={`sdot ${st.cls}`} />
        <span className="lead">{st.lead}</span>
        <span className="task">{st.task}</span>
        {(s.queued ?? 0) > 0 && (
          <span className="qmore" title="Turns waiting behind the current one">
            +{s.queued} queued
          </span>
        )}
        {(s.pending ?? 0) > 0 && (
          <span className="qmore" title="Finished turns not yet narrated by the voice agent">
            {s.pending} unread
          </span>
        )}
      </div>

      {queuedTurns.length > 0 && (
        <div className="queuelist">
          {queuedTurns.map((q, i) => (
            <div key={i} className="qitem queued">
              <span className="qmark">⋯ queued</span>
              <span className="qtext">{q.text || "(turn)"}</span>
            </div>
          ))}
        </div>
      )}

      {live && !liveFullscreen && (
        <div className="liveterm-box">
          <div className="liveterm-bar">
            <button
              className="ltbtn"
              title="Hide the live view (the session keeps running)"
              aria-label="Minimize live view"
              onClick={() => onMinimizeLive()}
            >
              <Icon name="close" size={14} />
            </button>
            <span className="ltbar-title">Live CLI</span>
            <button
              className="ltbtn right"
              title="Full screen"
              aria-label="Full screen live view"
              onClick={() => onExpandLive()}
            >
              <Icon name="fullscreen" size={14} />
            </button>
          </div>
          <div className="liveterm-inner">
            <LiveTerminal handle={s.handle} />
          </div>
        </div>
      )}

      <div className="path">
        {s.backend?.toUpperCase()} · {s.model}
        {s.cost_usd && s.cost_usd > 0 ? ` · $${s.cost_usd.toFixed(4)}` : ""} · {abbrevHome(s.cwd)}
      </div>

      {open && <div className="transcript">{renderTimeline(transcript)}</div>}

      {s.supports_modes !== false && (
        <div className="moderow">
          <span className="modelbl">Mode</span>
          <div className="modeseg" role="group" aria-label="Permission mode">
            {MODES.map((m) => (
              <button
                key={m.id}
                className={(s.mode || "default") === m.id ? "on" : ""}
                title={m.title}
                disabled={modeBusy}
                onClick={() => onSwitchMode(m.id)}
              >
                {m.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="actionrow">
        {s.can_watch && !live && (
          <button className="txtoggle primary" title="Watch this session live in your browser" onClick={() => onWatch()}>
            <Icon name="play" size={13} /> Watch live
          </button>
        )}
        <button className="txtoggle" onClick={() => onToggleTranscript()}>
          {open ? "Hide" : "Transcript"}
        </button>
        {open && (
          <button className="txtoggle" title="Expand" onClick={() => onExpandTranscript()}>
            <Icon name="fullscreen" size={13} />
          </button>
        )}
      </div>

      {(s.can_watch || cmd) && (
        <details
          className="handoff"
          onToggle={(e) => {
            if ((e.target as HTMLDetailsElement).open) onOpenHandoff?.();
          }}
        >
          <summary>
            <span className="chev"><Icon name="chevron-right" size={10} /></span> Continue in your terminal
          </summary>
          {s.can_watch && (
            <div className="hopt">
              <div className="htitle">Take the keyboard</div>
              <div className="hwhy">Jump into this live session in your own terminal.</div>
              {attachLoading ? (
                <div className="hcmd">Asking the backend for its pane…</div>
              ) : attachCommand ? (
                <div className="hcmd">
                  <code>{attachCommand}</code>
                  <CopyBtn text={attachCommand} />
                </div>
              ) : null /* fetched and no live pane, or not opened yet — never a guessed command */}
            </div>
          )}
          {cmd && (
            <div className="hopt">
              <div className="htitle">Reopen anywhere</div>
              <div className="hwhy">Start a fresh terminal from this session&apos;s history.</div>
              <div className="hcmd">
                <code>{cmd}</code>
                <CopyBtn text={cmd} />
              </div>
            </div>
          )}
        </details>
      )}
    </div>
  );
}
