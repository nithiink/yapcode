"use client";

import { useEffect, useRef, useState } from "react";
import { RealtimeSession } from "@/lib/realtime";
import { GeminiSession } from "@/lib/gemini";
import { ClaudeBackend, RealtimeEvent, RealtimeOptions, VoiceProvider, VoiceSession, VoiceState, VoiceUsage } from "@/lib/voice";
import { INSTRUCTIONS } from "@/lib/instructions";
import LiveTerminal from "./LiveTerminal";

type Turn = { role: "user" | "assistant"; text: string; final: boolean };
type ToolLine = { name: string; ok?: boolean };
type Sess = {
  handle: string;
  session_id: string | null;
  cwd: string;
  model: string;
  status: string;
  cost_usd?: number;
  backend?: string;
};
type Pending = { sessionId: string; kind: string; text: string; options: string[] } | null;
type TxEvent =
  | { kind: "user"; text: string }
  | { kind: "assistant"; text: string }
  | { kind: "tool"; name: string; summary: string; risky: boolean }
  | { kind: "tool_result"; ok: boolean; text: string };

// Per-provider connection params. For OpenAI, cost-saver is applied via session
// config (brevity/cap/pruning) rather than a different model; for Gemini it also
// drops to the cheaper native-audio model.
function connectionParams(provider: VoiceProvider, costSaver: boolean): Partial<RealtimeOptions> {
  if (provider === "gemini") {
    return {
      provider: "gemini",
      model: costSaver
        ? "gemini-2.5-flash-native-audio-preview-12-2025"
        : "gemini-3.1-flash-live-preview",
      voice: "Kore",
    };
  }
  // Use OpenAI direct explicitly (configured today). To route this toggle through
  // Azure instead, change provider to "azure" once the Azure resource is set up.
  return { provider: "openai", model: "gpt-realtime-mini", voice: "marin" };
}

const PROVIDER_LABEL: Record<VoiceProvider, string> = {
  openai: "OpenAI",
  gemini: "Gemini",
};

const BACKEND_LABEL: Record<ClaudeBackend, string> = {
  cli: "CLI",
  sdk: "SDK",
};

const STATE_LABEL: Record<VoiceState, string> = {
  idle: "Offline",
  connecting: "Connecting",
  listening: "Listening",
  hearing: "Hearing you",
  thinking: "Thinking",
  speaking: "Speaking",
};

export default function VoiceAgent() {
  const [connected, setConnected] = useState(false);
  const [provider, setProvider] = useState<VoiceProvider>("openai");
  const [backend, setBackend] = useState<ClaudeBackend>("cli");
  const [costSaver, setCostSaver] = useState(true);
  const [modelLabel, setModelLabel] = useState("");
  const [vstate, setVstate] = useState<VoiceState>("idle");
  const [muted, setMuted] = useState(false);
  const [status, setStatus] = useState("Tap connect and start talking.");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [tools, setTools] = useState<ToolLine[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [pending, setPending] = useState<Pending>(null);
  const [voiceUsage, setVoiceUsage] = useState<VoiceUsage | null>(null);
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TxEvent[]>([]);
  const [fullscreen, setFullscreen] = useState(false);
  const [liveSession, setLiveSession] = useState<string | null>(null);
  const [liveFullscreen, setLiveFullscreen] = useState(false);

  const sessionRef = useRef<VoiceSession | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const orbRef = useRef<HTMLDivElement | null>(null);
  const glowRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const pollTimers = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const txPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const totalCost = sessions.reduce((s, x) => s + (x.cost_usd || 0), 0);

  useEffect(
    () => () => {
      stopAnalyser();
      stopPolling();
      if (txPollRef.current) clearInterval(txPollRef.current);
    },
    [],
  );

  const fetchTranscript = async (handle: string) => {
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "read_transcript", arguments: { session_id: handle } }),
      });
      const d = await r.json();
      setTranscript(d?.result?.events || []);
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
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen, liveFullscreen]);

  const renderTimeline = (events: TxEvent[]) => {
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
  };

  const stopPolling = (sessionId?: string) => {
    if (sessionId) {
      const t = pollTimers.current.get(sessionId);
      if (t) clearInterval(t);
      pollTimers.current.delete(sessionId);
    } else {
      pollTimers.current.forEach((t) => clearInterval(t));
      pollTimers.current.clear();
    }
  };

  // A background Claude turn reached a result — surface any prompt in the UI and
  // tell the voice model to narrate it (works even mid-conversation).
  const handleClaudeResult = (res: any) => {
    if (!res || typeof res !== "object") return;
    const sid = res.session_id;
    if ((res.status === "needs_permission" || res.status === "needs_choice") && res.prompt) {
      setPending({
        sessionId: sid,
        kind: res.prompt.kind,
        text: res.prompt.text,
        options: res.prompt.options || [],
      });
      const opts = (res.prompt.options || []).join(", ");
      const msg =
        res.prompt.kind === "choice"
          ? `[Claude update] Claude is asking: ${res.prompt.text}${opts ? ` Options: ${opts}.` : ""} Read this to the user and get their choice.`
          : `[Claude update] Claude needs permission to ${res.prompt.text}. Ask the user to approve or deny.`;
      sessionRef.current?.injectUpdate(msg);
    } else if (res.status === "completed") {
      setPending(null);
      const txt = (res.assistant_text || "").trim();
      sessionRef.current?.injectUpdate(
        `[Claude update] Claude finished. ${txt ? `It said: ${txt}` : "Done."} Summarize this briefly for the user.`,
      );
    } else if (res.status === "error") {
      setPending(null);
      sessionRef.current?.injectUpdate(
        `[Claude update] Claude hit an error: ${res.error || "unknown"}. Tell the user.`,
      );
    }
    refreshSessions();
  };

  const pollSession = (sessionId: string) => {
    if (!sessionId || pollTimers.current.has(sessionId)) return;
    const timer = setInterval(async () => {
      try {
        const r = await fetch("/api/tools/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "poll_session", arguments: { session_id: sessionId } }),
        });
        const data = await r.json();
        const res = data?.result;
        if (!res || res.status === "working") return; // keep polling
        stopPolling(sessionId);
        if (res.status !== "idle") handleClaudeResult(res);
      } catch {
        /* transient; keep polling */
      }
    }, 1500);
    pollTimers.current.set(sessionId, timer);
  };

  // Restore toggle prefs.
  useEffect(() => {
    const p = localStorage.getItem("vc_provider");
    if (p === "openai" || p === "gemini") setProvider(p);
    const c = localStorage.getItem("vc_cost_saver");
    if (c != null) setCostSaver(c === "1");
    const b = localStorage.getItem("vc_backend");
    if (b === "cli" || b === "sdk") setBackend(b);
  }, []);
  useEffect(() => {
    localStorage.setItem("vc_provider", provider);
  }, [provider]);
  useEffect(() => {
    localStorage.setItem("vc_backend", backend);
  }, [backend]);
  useEffect(() => {
    localStorage.setItem("vc_cost_saver", costSaver ? "1" : "0");
  }, [costSaver]);

  const refreshSessions = async () => {
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "list_sessions", arguments: {} }),
      });
      const data = await r.json();
      setSessions(data?.result?.sessions || []);
    } catch {
      /* ignore */
    }
  };

  // Drive the orb's size from the assistant audio amplitude.
  const startAnalyser = (stream: MediaStream) => {
    try {
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
      audioCtxRef.current = ctx;
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const amp = Math.min(1, Math.sqrt(sum / buf.length) * 3.2);
        orbRef.current?.style.setProperty("--amp", amp.toFixed(3));
        glowRef.current?.style.setProperty("--amp", amp.toFixed(3));
        rafRef.current = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      /* analyser optional */
    }
  };

  const stopAnalyser = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    audioCtxRef.current?.close().catch(() => undefined);
    audioCtxRef.current = null;
    orbRef.current?.style.setProperty("--amp", "0");
    glowRef.current?.style.setProperty("--amp", "0");
  };

  const onEvent = (e: RealtimeEvent) => {
    if (e.type === "status") setStatus(e.status);
    else if (e.type === "state") setVstate(e.state);
    else if (e.type === "usage") setVoiceUsage(e.usage);
    else if (e.type === "error") setStatus(`Error: ${e.message}`);
    else if (e.type === "transcript") {
      setTurns((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        if (last && last.role === e.role && !last.final) {
          next[next.length - 1] = { role: e.role, text: e.text, final: e.final };
        } else {
          next.push({ role: e.role, text: e.text, final: e.final });
        }
        return next.slice(-40);
      });
    } else if (e.type === "tool_call" && e.result !== undefined) {
      setTools((prev) => [...prev, { name: e.name, ok: e.ok }].slice(-20));
      const res: any = e.result;
      // tell_claude/answer_prompt now return "working" — poll for the real result.
      if (
        (e.name === "tell_claude" || e.name === "answer_prompt") &&
        res?.status === "working" &&
        res.session_id
      ) {
        pollSession(res.session_id);
      }
      if (e.name === "interrupt_session") stopPolling(res?.session_id);
      if (["start_session", "tell_claude", "answer_prompt", "interrupt_session"].includes(e.name)) {
        refreshSessions();
      }
    }
  };

  const connect = async () => {
    setVstate("connecting");
    const params = connectionParams(provider, costSaver);
    const opts: RealtimeOptions = {
      ...params,
      instructions: INSTRUCTIONS,
      costSaver,
      backend,
      onEvent,
      onRemoteStream: startAnalyser,
    };
    const s: VoiceSession =
      provider === "gemini" ? new GeminiSession(opts) : new RealtimeSession(opts);
    sessionRef.current = s;
    try {
      await s.start(audioRef.current!);
      setModelLabel(s.activeModel || params.model || PROVIDER_LABEL[provider]);
      setConnected(true);
      setMuted(false);
      refreshSessions();
    } catch (err: any) {
      setStatus(`Failed: ${err?.message || err}`);
      setVstate("idle");
    }
  };

  const disconnect = () => {
    sessionRef.current?.stop();
    sessionRef.current = null;
    stopAnalyser();
    stopPolling();
    if (txPollRef.current) {
      clearInterval(txPollRef.current);
      txPollRef.current = null;
    }
    setConnected(false);
    setVstate("idle");
    setMuted(false);
    setPending(null);
  };

  const toggleMute = () => {
    const next = !muted;
    setMuted(next);
    sessionRef.current?.setMuted(next);
  };

  // Manual fallback for answering a pending prompt by click (voice also works).
  const answerPrompt = async (choice: string) => {
    if (!pending) return;
    const p = pending;
    setPending(null);
    await fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "answer_prompt",
        arguments: { session_id: p.sessionId, choice },
      }),
    });
    pollSession(p.sessionId); // Claude resumes in the background; narrate the result
    refreshSessions();
  };

  return (
    <div className="app">
      <div className="topbar">
        <div className={`brand ${connected ? "live" : ""}`}>
          <span className="dot" /> Voice-Claude
        </div>
        <div className="topmeta">
          {PROVIDER_LABEL[provider]}
          {modelLabel ? ` · ${modelLabel}` : ""} · Claude {BACKEND_LABEL[backend]}
          {backend === "cli" ? " (chrome)" : ""}
          {costSaver ? " · saver on" : ""}
          <br />
          Claude: <b className="cost">${totalCost.toFixed(4)}</b>
          {" · "}Voice: <b className="cost">${(voiceUsage?.costUsd || 0).toFixed(4)}</b>
          {voiceUsage && (
            <> · cache {(voiceUsage.cacheHitRate * 100).toFixed(0)}%</>
          )}
          {" · "}Total: <b className="cost">${(totalCost + (voiceUsage?.costUsd || 0)).toFixed(4)}</b>
        </div>
      </div>

      <div className="stage">
        <div className="orb-wrap">
          <div ref={glowRef} className="orb-glow" />
          <div ref={orbRef} className={`orb ${vstate}`} />
        </div>
        <div className="state-label">{muted ? "Muted" : STATE_LABEL[vstate]}</div>

        <div className="toggles">
          <div className={`seg ${connected ? "locked" : ""}`} role="group" aria-label="Voice provider">
            {(["openai", "gemini"] as VoiceProvider[]).map((p) => (
              <button
                key={p}
                className={`segbtn ${provider === p ? "on" : ""}`}
                disabled={connected}
                onClick={() => setProvider(p)}
              >
                {PROVIDER_LABEL[p]}
              </button>
            ))}
          </div>
          <div className={`seg ${connected ? "locked" : ""}`} role="group" aria-label="Claude backend">
            {(["cli", "sdk"] as ClaudeBackend[]).map((b) => (
              <button
                key={b}
                className={`segbtn ${backend === b ? "on" : ""}`}
                disabled={connected}
                onClick={() => setBackend(b)}
                title={b === "cli" ? "Interactive CLI: Max subscription + Chrome" : "Claude Agent SDK"}
              >
                {BACKEND_LABEL[b]}
              </button>
            ))}
          </div>
          <button
            className={`switch ${costSaver ? "on" : ""}`}
            disabled={connected}
            aria-pressed={costSaver}
            onClick={() => setCostSaver((v) => !v)}
          >
            <span className="knob" />
            Cost saver {costSaver ? "on" : "off"}
          </button>
        </div>
        {connected && <div className="togglehint">Disconnect to change provider or cost saver.</div>}

        <div className="controls">
          {!connected ? (
            <button className="talk" onClick={connect}>
              Connect
            </button>
          ) : (
            <button className="talk stop" onClick={disconnect}>
              Disconnect
            </button>
          )}
          {connected && (
            <button className={`ghost mute ${muted ? "muted" : ""}`} onClick={toggleMute}>
              {muted ? "🔇 Unmute" : "🎙 Mute"}
            </button>
          )}
          <button className="ghost" onClick={refreshSessions}>
            Refresh
          </button>
        </div>
        <div className="status">{status}</div>

        {pending && (
          <div className={`permcard ${pending.kind === "choice" ? "choice" : ""}`}>
            <div className="lead">
              {pending.kind === "choice" ? "Claude is asking" : "Permission needed"}
            </div>
            <div className="ask">
              {pending.kind === "choice" ? (
                pending.text
              ) : (
                <>
                  Claude wants to <code>{pending.text}</code>
                </>
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
      </div>

      <div className="panels">
        <div className="panel">
          <h2>Conversation</h2>
          <div className="scroll">
            {turns.length === 0 && tools.length === 0 && (
              <div className="empty">Assistant replies and Claude actions show here.</div>
            )}
            {turns.map((t, i) => (
              <div key={i} className={`bubble ${t.role}`}>
                <div className="who">{t.role === "user" ? "You" : "Assistant"}</div>
                {t.text}
              </div>
            ))}
            {tools.slice(-6).map((t, i) => (
              <div key={`tool-${i}`} className={`toolrow ${t.ok === false ? "err" : ""}`}>
                → {t.name} {t.ok === false ? "✗" : "✓"}
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <h2>Claude sessions</h2>
          <div className="scroll">
            {sessions.length === 0 && <div className="empty">No active sessions.</div>}
            {sessions.map((s) => {
              const cmd = s.session_id ? `cd ${s.cwd} && claude --resume ${s.session_id}` : null;
              const open = openSession === s.handle;
              return (
                <div key={s.handle} className="sess">
                  <div className="head">
                    <span className="name">{s.cwd.split("/").pop()}</span>
                    {s.backend && <span className="bk">{s.backend.toUpperCase()}</span>}
                    <span className={`badge ${s.status}`}>{s.status}</span>
                    {s.backend === "cli" && (
                      <button
                        className="txtoggle"
                        onClick={() => {
                          if (liveSession === s.handle) {
                            setLiveSession(null);
                            setLiveFullscreen(false);
                          } else {
                            setLiveSession(s.handle);
                          }
                        }}
                      >
                        {liveSession === s.handle ? "Close" : "Live"}
                      </button>
                    )}
                    {liveSession === s.handle && (
                      <button className="txtoggle" title="Fullscreen" onClick={() => setLiveFullscreen(true)}>
                        ⛶
                      </button>
                    )}
                    <button className="txtoggle" onClick={() => toggleTranscript(s.handle)}>
                      {open ? "Hide" : "Transcript"}
                    </button>
                    {open && (
                      <button className="txtoggle" title="Fullscreen" onClick={() => setFullscreen(true)}>
                        ⛶
                      </button>
                    )}
                  </div>
                  {liveSession === s.handle && !liveFullscreen && (
                    <div className="liveterm-box">
                      <LiveTerminal handle={s.handle} />
                    </div>
                  )}
                  <div className="path">
                    {s.model} · ${(s.cost_usd || 0).toFixed(4)} · {s.cwd}
                  </div>
                  {open && <div className="transcript">{renderTimeline(transcript)}</div>}
                  {cmd && (
                    <div className="handoff">
                      <code>{cmd}</code>
                      <button onClick={() => navigator.clipboard.writeText(cmd)}>Copy</button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      {fullscreen && openSession && (
        <div className="tx-overlay" onClick={() => setFullscreen(false)}>
          <div className="tx-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tx-modal-head">
              <span>Claude session transcript</span>
              <button className="txtoggle" onClick={() => setFullscreen(false)}>
                Close ✕
              </button>
            </div>
            <div className="tx-modal-body">{renderTimeline(transcript)}</div>
          </div>
        </div>
      )}

      {liveFullscreen && liveSession && (
        <div className="tx-overlay" onClick={() => setLiveFullscreen(false)}>
          <div className="tx-modal" onClick={(e) => e.stopPropagation()}>
            <div className="tx-modal-head">
              <span>Live Claude CLI</span>
              <button className="txtoggle" onClick={() => setLiveFullscreen(false)}>
                Close ✕
              </button>
            </div>
            <div className="tx-modal-body term">
              <LiveTerminal handle={liveSession} />
            </div>
          </div>
        </div>
      )}

      <audio ref={audioRef} hidden />
    </div>
  );
}
