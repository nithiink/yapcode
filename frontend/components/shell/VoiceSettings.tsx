"use client";

// The voice connection's settings: provider, OpenAI route, Claude backend, and
// the voice model. Carried over verbatim from the rail this shell replaced —
// the shape of the choices did not change, only where they live. They sit
// behind a disclosure in the dock because they are set once and then left
// alone, unlike narration mode, which is a live control and lives in the top
// bar. Everything here is locked while connected (a mid-connection provider
// swap would need a reconnect); narration deliberately is not.
import { useEffect, useRef, useState } from "react";
import { useYuri } from "@/components/VoiceProvider";
import { BACKEND_LABEL } from "@/lib/sessions";
import type { ClaudeBackend } from "@/lib/voice";

export function VoiceSettings() {
  const {
    connected, provider, setProvider, backend, setBackend,
    model, setModel, modelOptions,
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

  return (
    <details className="dock-settings">
      <summary>Voice setup</summary>
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
      {modelLockHint && (
        <div className="modellock">Disconnect first to change the voice model.</div>
      )}
    </details>
  );
}
