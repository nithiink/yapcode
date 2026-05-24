"use client";

import { useEffect, useRef, useState } from "react";
import { RealtimeSession, RealtimeEvent, VoiceState } from "@/lib/realtime";
import { INSTRUCTIONS } from "@/lib/instructions";

type Turn = { role: "user" | "assistant"; text: string; final: boolean };
type ToolLine = { name: string; ok?: boolean };
type Sess = {
  handle: string;
  session_id: string | null;
  cwd: string;
  model: string;
  status: string;
  cost_usd?: number;
};
type Pending = { sessionId: string; kind: string; text: string; options: string[] } | null;

const MODEL = "gpt-realtime-mini";
const VOICE = "marin";

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
  const [modelLabel, setModelLabel] = useState(MODEL);
  const [vstate, setVstate] = useState<VoiceState>("idle");
  const [status, setStatus] = useState("Tap connect and start talking.");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [tools, setTools] = useState<ToolLine[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [pending, setPending] = useState<Pending>(null);

  const sessionRef = useRef<RealtimeSession | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const orbRef = useRef<HTMLDivElement | null>(null);
  const glowRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);

  const totalCost = sessions.reduce((s, x) => s + (x.cost_usd || 0), 0);

  useEffect(() => () => stopAnalyser(), []);

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
      if (res && typeof res === "object") {
        if ((res.status === "needs_permission" || res.status === "needs_choice") && res.prompt) {
          setPending({
            sessionId: res.session_id,
            kind: res.prompt.kind,
            text: res.prompt.text,
            options: res.prompt.options || [],
          });
        } else if (res.status === "completed" || res.status === "error") {
          setPending(null);
        }
      }
      if (["start_session", "tell_claude", "answer_prompt", "interrupt_session"].includes(e.name)) {
        refreshSessions();
      }
    }
  };

  const connect = async () => {
    setVstate("connecting");
    const s = new RealtimeSession({
      model: MODEL,
      voice: VOICE,
      instructions: INSTRUCTIONS,
      onEvent,
      onRemoteStream: startAnalyser,
    });
    sessionRef.current = s;
    try {
      await s.start(audioRef.current!);
      setModelLabel(s.activeModel || MODEL);
      setConnected(true);
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
    setConnected(false);
    setVstate("idle");
    setPending(null);
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
    refreshSessions();
  };

  return (
    <div className="app">
      <div className="topbar">
        <div className={`brand ${connected ? "live" : ""}`}>
          <span className="dot" /> Voice-Claude
        </div>
        <div className="topmeta">
          {modelLabel} · key server-side
          <br />
          Claude cost: <b className="cost">${totalCost.toFixed(4)}</b>
        </div>
      </div>

      <div className="stage">
        <div className="orb-wrap">
          <div ref={glowRef} className="orb-glow" />
          <div ref={orbRef} className={`orb ${vstate}`} />
        </div>
        <div className="state-label">{STATE_LABEL[vstate]}</div>

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
              return (
                <div key={s.handle} className="sess">
                  <div className="head">
                    <span className="name">{s.cwd.split("/").pop()}</span>
                    <span className={`badge ${s.status}`}>{s.status}</span>
                  </div>
                  <div className="path">
                    {s.model} · ${(s.cost_usd || 0).toFixed(4)} · {s.cwd}
                  </div>
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

      <audio ref={audioRef} hidden />
    </div>
  );
}
