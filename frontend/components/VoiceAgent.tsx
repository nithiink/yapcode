"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { authHeaders } from "@/lib/auth";
import LiveTerminal from "./LiveTerminal";
import { Icon } from "./ui/Icon";
import { CopyBtn } from "./ui/CopyBtn";
import { MarkdownLite } from "./conversation/MarkdownLite";
import { Timeline } from "./conversation/Timeline";
import { SessionCard, renderTimeline, type TxEvent } from "./SessionCard";
import { ActivityFeed } from "./ActivityFeed";
import { splitPlan } from "@/lib/timeline";
import { BACKEND_LABEL } from "@/lib/sessions";
import { NARRATION_MODES } from "@/lib/narration";
import { PROVIDER_LABEL, NARRATION_LABEL, orbCaption } from "@/lib/voiceui";
import { useYuri } from "./VoiceProvider";
import type { ClaudeBackend } from "@/lib/voice";

export default function VoiceAgent() {
  const {
    connected,
    muted,
    vstate,
    provider,
    model,
    connect,
    disconnect,
    toggleMute,
    setProvider,
    setModel,
    timeline,
    pending,
    sessions,
    narrationMode,
    setNarrationMode,
    debugEvents,
    backend,
    setBackend,
    modelOptions,
    status,
    modelLabel,
    voiceUsage,
    narrationBusy,
    orbRef,
    glowRef,
    modeBusy,
    switchMode,
    commitRename,
    answerPrompt,
    callTool,
    clearDebugEvents,
  } = useYuri();

  // The OpenAI-family route (native vs Azure) last used, so toggling away to
  // Gemini and back lands on the same route instead of resetting.
  const openaiRouteRef = useRef<Exclude<typeof provider, "gemini">>("azure");
  useEffect(() => {
    if (provider !== "gemini") openaiRouteRef.current = provider;
  }, [provider]);

  // CLI-vs-SDK explainer popover (the ⓘ next to the CLAUDE group label).
  // Opens on hover (transient) AND on press (pinned, for touch/keyboard); the
  // short leave-delay lets the cursor cross the gap into the popover.
  const [claudeInfoPinned, setClaudeInfoPinned] = useState(false);
  const [claudeInfoHover, setClaudeInfoHover] = useState(false);
  const claudeInfoOpen = claudeInfoPinned || claudeInfoHover;
  const claudeGrpRef = useRef<HTMLDivElement | null>(null);
  const infoHoverTimer = useRef<number | null>(null);
  const infoHoverEnter = () => {
    if (infoHoverTimer.current) clearTimeout(infoHoverTimer.current);
    setClaudeInfoHover(true);
  };
  const infoHoverLeave = () => {
    if (infoHoverTimer.current) clearTimeout(infoHoverTimer.current);
    infoHoverTimer.current = window.setTimeout(() => setClaudeInfoHover(false), 140);
  };
  useEffect(() => {
    if (!claudeInfoPinned) return;
    const onDown = (e: MouseEvent) => {
      if (!claudeGrpRef.current?.contains(e.target as Node)) setClaudeInfoPinned(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [claudeInfoPinned]);

  // Set true when the user tries to change the model while connected, so the UI
  // can prompt them to disconnect first.
  const [modelLockHint, setModelLockHint] = useState(false);
  // Clear the "disconnect to change model" prompt once the user disconnects.
  useEffect(() => {
    if (!connected) setModelLockHint(false);
  }, [connected]);

  const [openSession, setOpenSession] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TxEvent[]>([]);
  const [fullscreen, setFullscreen] = useState(false);
  const [liveSession, setLiveSession] = useState<string | null>(null);
  const [liveFullscreen, setLiveFullscreen] = useState(false);
  const [attachOpen, setAttachOpen] = useState(false);
  // Conversation panel always auto-scrolls to the latest message. The scrollbar
  // is theme-matched and auto-hiding: a `.scrolling` class (toggled on scroll,
  // cleared after a short idle) paints the thumb only while the user scrolls.
  // Dynamic top/bottom fade indicators (.more-above / .more-below on the wrap)
  // hint at off-screen content and fade out smoothly at each end.
  const convWrapRef = useRef<HTMLDivElement | null>(null);
  const convScrollRef = useRef<HTMLDivElement | null>(null);
  const convScrollHideRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [showDebug, setShowDebug] = useState(false);
  // The activity panel renders at the bottom of the page — when it's opened,
  // scroll there so the user isn't left staring at an apparently-unchanged page.
  useEffect(() => {
    if (!showDebug) return;
    requestAnimationFrame(() => {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
    });
  }, [showDebug]);
  const [logFilter, setLogFilter] = useState("");
  const [logErrorsOnly, setLogErrorsOnly] = useState(false);
  const [logPaused, setLogPaused] = useState(false);
  const [logCopied, setLogCopied] = useState(false);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const logAtBottomRef = useRef(true);
  // Poll for a session's own transcript while its card is expanded. A session's
  // transcript is per-view detail (this card), not global state — the provider
  // deliberately does not hold it.
  const txPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => () => {
    if (txPollRef.current) clearInterval(txPollRef.current);
  }, []);

  const totalCost = sessions.reduce((s, x) => s + (x.cost_usd || 0), 0);

  // Copy the currently-shown events (built by ActivityFeed, which owns the
  // filtering) as readable text for pasting into a bug report / sharing.
  const copyLog = (text: string) => {
    const done = () => {
      setLogCopied(true);
      window.setTimeout(() => setLogCopied(false), 1200);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done, () => undefined);
    }
  };

  const fetchTranscript = async (handle: string) => {
    try {
      const result: any = await callTool("read_transcript", { session_id: handle });
      setTranscript(result?.events || []);
    } catch {
      /* ignore */
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

  // Auto-scroll to the newest line only when the user is already at the bottom,
  // so scrolling up to read history isn't yanked back down by new events.
  useEffect(() => {
    if (logPaused || !showDebug) return;
    if (!logAtBottomRef.current) return;
    const el = logScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [debugEvents, logPaused, showDebug]);

  // Recompute the top/bottom fade indicators from the current scroll position:
  // show the top fade only when there's content above, the bottom fade only when
  // there's content below (a few px of slack avoids flicker right at each end).
  const updateConvFades = useCallback(() => {
    const el = convScrollRef.current;
    const wrap = convWrapRef.current;
    if (!el || !wrap) return;
    wrap.classList.toggle("more-above", el.scrollTop > 4);
    wrap.classList.toggle("more-below", el.scrollHeight - el.scrollTop - el.clientHeight > 4);
  }, []);

  // Conversation: always auto-scroll to the latest message, then refresh the
  // fades (after auto-scrolling to the bottom, the top fade shows and the bottom
  // fade hides). The themed auto-hide scrollbar lets the user scroll up to review.
  useEffect(() => {
    const el = convScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    updateConvFades();
  }, [timeline, updateConvFades]);

  // Keep the fades correct when the viewport (and so clientHeight) changes.
  useEffect(() => {
    window.addEventListener("resize", updateConvFades);
    return () => window.removeEventListener("resize", updateConvFades);
  }, [updateConvFades]);

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

  return (
    <div className="app">
      <header className="topbar">
        <div className={`brand ${connected ? "live" : ""}`}>
          <span className="speakdot" aria-hidden>
            <i />
            <i />
            <i />
          </span>
          <span className="logo">Yap Code</span>
        </div>
        <div className="topmeta">
          {/* Stable label: don't churn through listening/hearing/thinking/speaking —
              the orb conveys live activity. Just connected / connecting / offline. */}
          {connected ? "Listening" : vstate === "connecting" ? "Connecting" : "Offline"} ·{" "}
          {PROVIDER_LABEL[provider]}
          {modelLabel ? ` · ${modelLabel}` : ""} · {BACKEND_LABEL[backend]}
          {backend === "cli" ? " · chrome" : ""}
          <br />
          Claude <b className="cost">${totalCost.toFixed(4)}</b>
          {" · "}Voice <b className="cost">${(voiceUsage?.costUsd || 0).toFixed(4)}</b>
          {" · "}Total <b className="cost">${(totalCost + (voiceUsage?.costUsd || 0)).toFixed(4)}</b>
        </div>
      </header>

      <section className="hero">
        <div className="lede">
          <div className="kick">Hands-free engineering</div>
          <h1>
            Speak.<br />
            <span className="c">Claude</span>
            <br />
            builds.
          </h1>
          <p className="lede-p">
            A voice on top of Claude Code. Talk; it opens sessions, edits files, runs commands, and
            reports back — while you keep talking.
          </p>
          <div className="actions">
            {connected ? (
              <button className="talk stop" onClick={disconnect}>
                Disconnect
              </button>
            ) : vstate === "connecting" ? (
              <button className="talk connecting" disabled aria-busy="true">
                <span className="spinner" aria-hidden />
                Connecting…
              </button>
            ) : (
              <button className="talk" onClick={connect}>
                Connect &amp; talk
              </button>
            )}
            {connected && (
              <button className={`ghost mute ${muted ? "muted" : ""}`} onClick={toggleMute}>
                {muted ? "🔇 Unmute" : "🎙 Mute"}
              </button>
            )}
            <button
              className={`textbtn ${showDebug ? "on" : ""}`}
              onClick={() => setShowDebug((v) => !v)}
              title="Full voice ↔ backend ↔ Claude pipeline log"
            >
              {showDebug ? "Hide activity" : "Activity log"}
            </button>
          </div>
          <div className="state-row">{status}</div>
        </div>
        <div className="orbwrap">
          <div className="orbinner">
            <div ref={glowRef} className="orb-glow" />
            <div ref={orbRef} className={`orb ${vstate} ${muted ? "muted" : ""}`} />
          </div>
          <div className="orbcap">{orbCaption(connected, muted, vstate)}</div>
        </div>
      </section>

      <div className="controls-row">
          <div className="grp">
            <span className="grplab">Voice</span>
            <div className={`seg ${connected ? "locked" : ""}`} role="group" aria-label="Voice provider">
              <button
                className={`segbtn ${provider === "gemini" ? "on" : ""}`}
                disabled={connected}
                onClick={() => setProvider("gemini")}
                title="Best price and tool-call reliability"
              >
                Gemini
              </button>
              <button
                className={`segbtn ${provider !== "gemini" ? "on" : ""}`}
                disabled={connected}
                onClick={() => setProvider(openaiRouteRef.current)}
              >
                OpenAI
              </button>
            </div>
          </div>
          {provider !== "gemini" && (
            <div className="grp">
              <span className="grplab">Route</span>
              <div className={`seg ${connected ? "locked" : ""}`} role="group" aria-label="OpenAI route">
                <button
                  className={`segbtn ${provider === "openai" ? "on" : ""}`}
                  disabled={connected}
                  onClick={() => setProvider("openai")}
                  title="OpenAI API directly"
                >
                  Native
                </button>
                <button
                  className={`segbtn ${provider === "azure" ? "on" : ""}`}
                  disabled={connected}
                  onClick={() => setProvider("azure")}
                  title="Azure-hosted OpenAI deployment"
                >
                  via Azure
                </button>
              </div>
            </div>
          )}
          <div className="grp" ref={claudeGrpRef}>
            <span className="grplab">
              Claude
              <button
                className="infobtn"
                aria-label="What are CLI and SDK?"
                aria-expanded={claudeInfoOpen}
                onClick={() => {
                  setClaudeInfoPinned((v) => !v);
                  if (claudeInfoPinned) setClaudeInfoHover(false); // unpin closes even mid-hover
                }}
                onMouseEnter={infoHoverEnter}
                onMouseLeave={infoHoverLeave}
              >
                i
              </button>
            </span>
            {claudeInfoOpen && (
              <div className="infopop" role="note" onMouseEnter={infoHoverEnter} onMouseLeave={infoHoverLeave}>
                <div className="prow">
                  <span className="pname">CLI<span className="r">✓ Rec.</span></span>
                  <span className="pdesc">
                    Runs the real Claude Code terminal — you can watch the work live or take
                    over in your own terminal anytime, and it supports Claude in Chrome.
                  </span>
                </div>
                <div className="prow">
                  <span className="pname">SDK</span>
                  <span className="pdesc">
                    Programmatic (Agent SDK) — chat history is kept, so you can find a past
                    session and continue it when needed.
                  </span>
                </div>
              </div>
            )}
            <div className={`seg ${connected ? "locked" : ""}`} role="group" aria-label="Claude backend">
              {(["cli", "sdk"] as ClaudeBackend[]).map((b) => (
                <button
                  key={b}
                  className={`segbtn ${backend === b ? "on" : ""}`}
                  disabled={connected}
                  onClick={() => setBackend(b)}
                >
                  {BACKEND_LABEL[b]}
                </button>
              ))}
            </div>
          </div>
          {/* Narration is a live setting — unlike provider/backend/model it is
              NOT locked while connected, because "be quiet" has to work
              mid-conversation. It lives on the backend, so voice and this
              control always show the same value. */}
          <div className="grp">
            <span className="grplab">Narration</span>
            <div className="seg" role="group" aria-label="Narration mode" aria-busy={narrationBusy}>
              {NARRATION_MODES.map((m) => (
                <button
                  key={m}
                  className={`segbtn ${narrationMode === m ? "on" : ""}`}
                  aria-pressed={narrationMode === m}
                  // Same as switchMode's per-session buttons: one change at a
                  // time, so two fast clicks can't land their PUTs out of order
                  // and leave the control showing the mode that lost.
                  disabled={narrationBusy}
                  onClick={() => setNarrationMode(m)}
                  title={NARRATION_LABEL[m].title}
                >
                  {NARRATION_LABEL[m].label}
                </button>
              ))}
            </div>
          </div>
          {modelOptions.length > 0 && (
            <div
              className={`modelpick ${connected ? "locked" : ""}`}
              title={connected ? "Disconnect to change the model" : "Voice model"}
            >
              <span className="modelpick-lab">Model</span>
              <select
                className="modelsel"
                aria-label="Model"
                value={model}
                // Locked once connected: block the open and prompt to disconnect.
                onMouseDown={(e) => {
                  if (connected) {
                    e.preventDefault();
                    setModelLockHint(true);
                  }
                }}
                // Belt-and-suspenders for keyboard changes while focused + connected.
                onChange={(e) => {
                  if (connected) {
                    setModelLockHint(true);
                    return; // controlled value reverts; selection is rejected
                  }
                  setModel(e.target.value);
                }}
              >
                {modelOptions.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          )}
        </div>
      {connected ? (
        <div className={`togglehint ${modelLockHint ? "warn" : ""}`}>
          {modelLockHint
            ? "You’re connected — disconnect first, then change the model."
            : "Disconnect to change provider, backend, or model."}
        </div>
      ) : (
        <div className="ctrlcap">
          <span className="ck">✓</span> Recommended: <b>Claude CLI</b> with any voice provider
        </div>
      )}

      {pending && (
        <div className={`permcard ${pending.kind === "choice" ? "choice" : ""}`}>
          <div className="lead">
            {pending.kind === "choice" ? "Claude is asking" : "Permission needed"}
          </div>
          <div className="ask">
            {pending.kind === "choice" ? (
              pending.text
            ) : (
              (() => {
                const { lead, plan } = splitPlan(pending.text);
                return (
                  <>
                    Claude wants to <code>{lead}</code>
                    {plan && <MarkdownLite md={plan} />}
                  </>
                );
              })()
            )}
          </div>
          <div className="permbtns">
            {pending.kind === "choice" ? (
              pending.options.map((o) => (
                <button key={o} className="opt" onClick={() => answerPrompt(o)}>
                  {o}
                </button>
              ))
            ) : (
              <>
                <button className="allow" onClick={() => answerPrompt("allow")}>
                  Approve
                </button>
                <button className="deny" onClick={() => answerPrompt("deny")}>
                  Deny
                </button>
              </>
            )}
          </div>
          <div className="permhint">You can also just say it out loud.</div>
        </div>
      )}

      <div className="panels">
        <div className="panel conv-panel">
          <h2>
            Conversation <span className="ct">live</span>
          </h2>
          <div className="rule" />
          <div className="conv-wrap" ref={convWrapRef}>
            <div
              ref={convScrollRef}
              className="scroll conv-scroll"
              onScroll={() => {
                const el = convScrollRef.current;
                if (!el) return;
                el.classList.add("scrolling");
                if (convScrollHideRef.current) clearTimeout(convScrollHideRef.current);
                convScrollHideRef.current = setTimeout(() => el.classList.remove("scrolling"), 1000);
                updateConvFades();
              }}
            >
              {timeline.length === 0 && (
                <div className="empty">Assistant replies and Claude actions show here.</div>
              )}
              <Timeline items={timeline} />
            </div>
          </div>
        </div>

        <div className="panel">
          <h2>
            Agent sessions <span className="ct">{sessions.length || "—"} active</span>
          </h2>
          <div className="rule" />
          <div className="scroll">
            {sessions.length === 0 && <div className="empty">No active sessions.</div>}
            {sessions.map((s) => (
              <SessionCard
                key={s.handle}
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
            ))}
          </div>
        </div>
      </div>

      {showDebug && (
        <ActivityFeed
          events={debugEvents}
          filter={logFilter}
          onFilter={setLogFilter}
          errorsOnly={logErrorsOnly}
          onToggleErrorsOnly={() => setLogErrorsOnly((v) => !v)}
          paused={logPaused}
          onTogglePaused={() => setLogPaused((v) => !v)}
          copied={logCopied}
          onCopy={copyLog}
          onClear={clearDebugEvents}
          scrollRef={logScrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            logAtBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
        />
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

      {liveFullscreen && liveSession && (() => {
        const liveSess = sessions.find((x) => x.handle === liveSession);
        const liveTmuxCmd =
          liveSess?.backend === "cli" ? `tmux attach -t vc_${liveSess.handle.slice(0, 8)}` : null;
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
                    Attach to this session&apos;s tmux in your own terminal for a full native session —
                    keyboard shortcuts, copy-paste, and scrollback.
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
