"use client";

// The Sessions view: every open agent session as a full-width card, with room
// to breathe (SessionCard used to render at half a panel's width, packed in
// beside the Conversation panel; here it gets the shell's whole main column).
//
// Two actions have no dedicated /yuri/* route and go through callTool, which
// is what the UI already does for read_transcript (see lib/api.ts's own
// comment on why REST is the default and callTool the exception): close and
// send. Interrupt DOES have a real route, so it uses ypost directly.
import { useCallback, useEffect, useRef, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { SessionCard, renderTimeline, type TxEvent } from "@/components/SessionCard";
import { ViewError } from "@/components/ViewError";
import LiveTerminal from "@/components/LiveTerminal";
import { Icon } from "@/components/ui/Icon";
import { CopyBtn } from "@/components/ui/CopyBtn";
import { tmuxAttachCommand, type Sess } from "@/lib/sessions";
import { yget, ypost, ApiError } from "@/lib/api";

export default function Page() {
  // sessions is kept fresh by the provider's own 2.5s poll, which runs
  // regardless of which view is mounted (see VoiceProvider.tsx) — a *later*
  // poll failure just leaves the last-known list on screen. But that poll
  // (like refreshApprovals/refreshMissions) swallows its own fetch failures,
  // so an empty `sessions` on first mount can't be told apart from "the
  // backend is unreachable." This view can't accept that: it probes the same
  // endpoint itself once, purely to observe success/failure, before trusting
  // an empty list — the same fetch-then-adopt shape app/page.tsx uses.
  const { sessions, callTool, refresh, modeBusy, switchMode, commitRename, pollSession } = useYuri();

  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null); // handle with an action in flight

  const load = useCallback(async () => {
    try {
      await yget<{ sessions: Sess[] }>("sessions");
      setLoadError(null);
      await refresh("sessions");
    } catch (e) {
      setLoadError(e);
    }
  }, [refresh]);

  useEffect(() => {
    void load();
  }, [load]);

  // Per-card message drafts, so switching cards (or the poll refreshing
  // `sessions`) never clobbers what's half-typed in another card.
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  // Transcript: only one card can be expanded at a time (mirrors the original
  // single-screen behavior) — a session's transcript is per-view detail, not
  // global state the provider holds.
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TxEvent[]>([]);
  const [fullscreen, setFullscreen] = useState(false);
  const txPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(
    () => () => {
      if (txPollRef.current) clearInterval(txPollRef.current);
    },
    [],
  );

  // Live-watch state: which session's terminal is embedded in its card, and
  // whether that embed is blown up to a fullscreen modal.
  const [liveSession, setLiveSession] = useState<string | null>(null);
  const [liveFullscreen, setLiveFullscreen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);

  // Inline session rename (voice "call this one X" also works).
  const [editing, setEditing] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const startRename = (handle: string, current: string) => {
    setEditing(handle);
    setDraftName(current);
  };
  const commitRenameLocal = async (handle: string) => {
    const name = draftName;
    setEditing(null);
    await commitRename(handle, name);
  };

  const fetchTranscript = async (handle: string) => {
    try {
      const result: any = await callTool("read_transcript", { session_id: handle });
      setTranscript(result?.events || []);
    } catch {
      /* ignore — the poll below will just try again */
    }
  };

  const toggleTranscript = (handle: string) => {
    if (txPollRef.current) {
      clearInterval(txPollRef.current);
      txPollRef.current = null;
    }
    if (openSession === handle) {
      setOpenSession(null);
      setTranscript([]);
      setFullscreen(false);
      return;
    }
    setOpenSession(handle);
    setTranscript([]);
    fetchTranscript(handle);
    txPollRef.current = setInterval(() => fetchTranscript(handle), 2500);
  };

  // Esc closes whichever fullscreen overlay is open.
  useEffect(() => {
    if (!fullscreen && !liveFullscreen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setFullscreen(false);
        setLiveFullscreen(false);
        setAttachOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen, liveFullscreen]);

  const send = async (handle: string) => {
    const message = (drafts[handle] || "").trim();
    if (!message) return;
    setBusy(handle);
    setError(null);
    try {
      await callTool("tell_claude", { session_id: handle, message });
      setDrafts((d) => ({ ...d, [handle]: "" }));
      // tell_claude returns immediately with status "working" — poll for the
      // real result the same way a voice-triggered send does.
      pollSession(handle);
    } catch (e) {
      setError(`Could not send that message: ${(e as Error).message}`);
    } finally {
      setBusy(null);
      await refresh("sessions");
    }
  };

  const interrupt = async (handle: string) => {
    setBusy(handle);
    setError(null);
    try {
      await ypost(`/sessions/${handle}/interrupt`);
    } catch (e) {
      setError(
        e instanceof ApiError ? `Could not interrupt that session: ${e.message}` : "Could not interrupt that session.",
      );
    } finally {
      setBusy(null);
      await refresh("sessions");
    }
  };

  const close = async (handle: string) => {
    setBusy(handle);
    setError(null);
    try {
      await callTool("close_session", { session_id: handle });
      if (liveSession === handle) {
        setLiveSession(null);
        setLiveFullscreen(false);
      }
      if (openSession === handle) {
        setOpenSession(null);
        setTranscript([]);
        setFullscreen(false);
      }
    } catch (e) {
      setError(`Could not close that session: ${(e as Error).message}`);
    } finally {
      setBusy(null);
      await refresh("sessions");
    }
  };

  return (
    <div className="sessions-view">
      <h2 className="viewtitle">Sessions</h2>

      {loadError ? <ViewError error={loadError} onRetry={() => void load()} /> : null}
      {!loadError && error ? <div className="apr-error">{error}</div> : null}

      {loadError ? null : sessions.length === 0 ? (
        <div className="empty">No active sessions.</div>
      ) : (
        <div className="sessions-list">
          {sessions.map((s) => {
            const running = !!s.running;
            const cardBusy = busy === s.handle;
            return (
              <div className="sesswrap" key={s.handle}>
                <SessionCard
                  s={s}
                  open={openSession === s.handle}
                  live={liveSession === s.handle}
                  modeBusy={modeBusy === s.handle}
                  onToggleTranscript={() => toggleTranscript(s.handle)}
                  onWatch={() => setLiveSession(s.handle)}
                  onSwitchMode={(m) => switchMode(s.handle, m)}
                  transcript={transcript}
                  editing={editing === s.handle}
                  draftName={draftName}
                  onDraftNameChange={setDraftName}
                  onCommitRename={() => commitRenameLocal(s.handle)}
                  onCancelRename={() => setEditing(null)}
                  onStartRename={() => startRename(s.handle, s.name || (s.cwd.split("/").pop() ?? ""))}
                  liveFullscreen={liveFullscreen}
                  onExpandLive={() => setLiveFullscreen(true)}
                  onMinimizeLive={() => {
                    setLiveSession(null);
                    setLiveFullscreen(false);
                  }}
                  onExpandTranscript={() => setFullscreen(true)}
                />
                <form
                  className="sess-msgbar"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void send(s.handle);
                  }}
                >
                  <input
                    className="sess-msgbox"
                    placeholder={running ? "A turn is running — wait for it to finish…" : "Type an instruction…"}
                    value={drafts[s.handle] || ""}
                    disabled={running || cardBusy}
                    onChange={(e) => setDrafts((d) => ({ ...d, [s.handle]: e.target.value }))}
                  />
                  <button
                    type="submit"
                    className="txtoggle primary"
                    disabled={running || cardBusy || !(drafts[s.handle] || "").trim()}
                  >
                    Send
                  </button>
                  <button
                    type="button"
                    className="txtoggle"
                    title="Interrupt the running turn"
                    disabled={!running || cardBusy}
                    onClick={() => void interrupt(s.handle)}
                  >
                    Interrupt
                  </button>
                  <button
                    type="button"
                    className="txtoggle danger"
                    title="Close this session"
                    disabled={cardBusy}
                    onClick={() => void close(s.handle)}
                  >
                    Close
                  </button>
                </form>
              </div>
            );
          })}
        </div>
      )}

      {fullscreen && openSession && (
        <div className="tx-overlay" onClick={() => setFullscreen(false)}>
          <div className="tx-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tx-modal-head">
              <span>Claude session transcript</span>
              <button className="txtoggle" onClick={() => setFullscreen(false)}>
                Close <Icon name="close" size={13} />
              </button>
            </div>
            <div className="tx-modal-body">{renderTimeline(transcript)}</div>
          </div>
        </div>
      )}

      {liveFullscreen &&
        liveSession &&
        (() => {
          const liveSess = sessions.find((x) => x.handle === liveSession);
          const liveTmuxCmd = liveSess ? tmuxAttachCommand(liveSess) : null;
          const liveName =
            liveSess?.name || liveSess?.cwd.split("/").pop() || liveSess?.handle.slice(0, 8) || "Live Claude CLI";
          return (
            <div
              className="tx-overlay"
              onClick={() => {
                setLiveFullscreen(false);
                setAttachOpen(false);
              }}
            >
              <div className="tx-modal" onClick={(e) => e.stopPropagation()}>
                <div className="tx-modal-head">
                  <span>{liveName}</span>
                  <div className="tx-head-actions">
                    {liveTmuxCmd && (
                      <button
                        className="attachbtn"
                        title="Attach to this session in your own terminal"
                        aria-expanded={attachOpen}
                        onClick={() => setAttachOpen((v) => !v)}
                      >
                        <Icon name="keyboard" size={16} strokeWidth={1.75} />
                        Attach in your terminal
                      </button>
                    )}
                    <button
                      className="txtoggle"
                      onClick={() => {
                        setLiveFullscreen(false);
                        setAttachOpen(false);
                      }}
                    >
                      Close <Icon name="close" size={13} />
                    </button>
                  </div>
                </div>
                {attachOpen && liveTmuxCmd && (
                  <div className="attach-pop">
                    <div className="ap-title">Open in your terminal</div>
                    <div className="ap-why">
                      Attach to this session&apos;s tmux in your own terminal for a full native session — keyboard
                      shortcuts, copy-paste, and scrollback.
                    </div>
                    <div className="cmdfield">
                      <code>{liveTmuxCmd}</code>
                      <CopyBtn text={liveTmuxCmd} />
                    </div>
                  </div>
                )}
                <div className="tx-modal-body term">
                  <LiveTerminal handle={liveSession} />
                </div>
              </div>
            </div>
          );
        })()}
    </div>
  );
}
