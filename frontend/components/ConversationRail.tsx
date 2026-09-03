"use client";

// The conversation half of the old single-screen voice UI: the orb and its
// caption, connect/mute, the provider/route/backend and model pickers, the
// narration-mode toggle, the live transcript, and the pending prompt card.
// Session cards, the activity log and the live terminal are NOT here — those
// move to their own routed views (Sessions, Activity).
//
// This lives in app/layout.tsx, above the routed {children}, alongside
// VoiceProvider — so navigating between views never touches the voice
// connection or either SSE stream. See VoiceProvider.tsx for why.
import { useCallback, useEffect, useRef, useState } from "react";
import { useYuri } from "./VoiceProvider";
import { Timeline } from "./conversation/Timeline";
import { MarkdownLite } from "./conversation/MarkdownLite";
import { splitPlan } from "@/lib/timeline";
import { BACKEND_LABEL } from "@/lib/sessions";
import { NARRATION_MODES } from "@/lib/narration";
import { NARRATION_LABEL, orbCaption } from "@/lib/voiceui";
import type { ClaudeBackend } from "@/lib/voice";

export function ConversationRail() {
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
    narrationMode,
    setNarrationMode,
    backend,
    setBackend,
    modelOptions,
    status,
    narrationBusy,
    orbRef,
    glowRef,
    answerPrompt,
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
  useEffect(() => {
    if (!connected) setModelLockHint(false);
  }, [connected]);

  // Conversation always auto-scrolls to the latest message. The scrollbar is
  // theme-matched and auto-hiding: a `.scrolling` class (toggled on scroll,
  // cleared after a short idle) paints the thumb only while the user scrolls.
  // Dynamic top/bottom fade indicators (.more-above / .more-below on the wrap)
  // hint at off-screen content and fade out smoothly at each end.
  const convWrapRef = useRef<HTMLDivElement | null>(null);
  const convScrollRef = useRef<HTMLDivElement | null>(null);
  const convScrollHideRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  return (
    <aside className="shell-rail">
      <div className="orbwrap">
        <div className="orbinner">
          <div ref={glowRef} className="orb-glow" />
          <div ref={orbRef} className={`orb ${vstate} ${muted ? "muted" : ""}`} />
        </div>
        <div className="orbcap">{orbCaption(connected, muted, vstate)}</div>
      </div>

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
      </div>
      <div className="state-row">{status}</div>

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
                <span className="pname">
                  CLI<span className="r">✓ Rec.</span>
                </span>
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
        {/* Narration is a live setting — unlike provider/backend/model it is NOT
            locked while connected, because "be quiet" has to work mid-conversation. */}
        <div className="grp">
          <span className="grplab">Narration</span>
          <div className="seg" role="group" aria-label="Narration mode" aria-busy={narrationBusy}>
            {NARRATION_MODES.map((m) => (
              <button
                key={m}
                className={`segbtn ${narrationMode === m ? "on" : ""}`}
                aria-pressed={narrationMode === m}
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
              onMouseDown={(e) => {
                if (connected) {
                  e.preventDefault();
                  setModelLockHint(true);
                }
              }}
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
          <div className="lead">{pending.kind === "choice" ? "Claude is asking" : "Permission needed"}</div>
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
    </aside>
  );
}
