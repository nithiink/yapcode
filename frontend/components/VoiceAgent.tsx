"use client";

import { useEffect, useRef, useState } from "react";
import { RealtimeSession } from "@/lib/realtime";
import { GeminiSession } from "@/lib/gemini";
import { ClaudeBackend, RealtimeEvent, RealtimeOptions, VoiceProvider, VoiceSession, VoiceState, VoiceUsage } from "@/lib/voice";
import { INSTRUCTIONS } from "@/lib/instructions";
import { authHeaders, withAuthParam } from "@/lib/auth";
import LiveTerminal from "./LiveTerminal";

// One ordered list of bubbles + tool rows so the live "Conversation" panel renders
// tool calls inline with the surrounding turns instead of piling them at the end.
type TimelineItem =
  | { kind: "turn"; role: "user" | "assistant"; text: string; final: boolean }
  | { kind: "tool"; name: string; ok?: boolean };
type Sess = {
  handle: string;
  session_id: string | null;
  cwd: string;
  model: string;
  status: string;
  cost_usd?: number;
  backend?: string;
  mode?: string;
  name?: string | null;
  // Live work-pipeline (from the runner's list()): a turn executing now,
  // turns waiting behind it, and finished turns not yet narrated by poll.
  running?: boolean;
  queued?: number;
  pending?: number;
  // The actual in-flight + waiting turns, in order, with their message text.
  queue?: { text: string; state: "running" | "queued" }[];
};

// One event on the unified pipeline bus (backend /debug/stream + browser posts).
type DebugEvent = {
  seq: number;
  ts: string;
  source: string; // voice | backend | claude | user
  dest: string;
  kind: string; // tool_call | tool_result | send | decision | hook | assistant | inject | transcript | error | poll | info
  session?: string | null;
  summary: string;
  detail?: unknown;
};

const MODES: { id: string; label: string; title: string }[] = [
  { id: "default", label: "Normal", title: "Asks before risky actions; approve/deny by voice" },
  { id: "plan", label: "Plan", title: "Only plans — makes no edits or commands" },
  { id: "acceptEdits", label: "Accept Edits", title: "File edits auto-apply; other risky actions still asked" },
  { id: "auto", label: "Auto", title: "Runs everything without asking" },
];
const MODE_LABEL: Record<string, string> = Object.fromEntries(MODES.map((m) => [m.id, m.label]));
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
  if (provider === "azure") {
    // Azure-hosted OpenAI realtime. The model is the Azure *deployment* name set
    // server-side (AZURE_OPENAI_DEPLOYMENT) — point that at your gpt-realtime-mini
    // deployment. Cost-saver is applied via session config, not a model swap.
    return { provider: "azure", voice: "marin" };
  }
  // OpenAI direct — the "native" option, kept switchable alongside Azure.
  return { provider: "openai", model: "gpt-realtime-mini", voice: "marin" };
}

const PROVIDER_LABEL: Record<VoiceProvider, string> = {
  azure: "Azure",
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
  const [provider, setProvider] = useState<VoiceProvider>("azure");
  const [backend, setBackend] = useState<ClaudeBackend>("cli");
  const [costSaver, setCostSaver] = useState(true);
  const [modelLabel, setModelLabel] = useState("");
  const [vstate, setVstate] = useState<VoiceState>("idle");
  const [muted, setMuted] = useState(false);
  const [status, setStatus] = useState("Tap connect and start talking.");
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [pending, setPending] = useState<Pending>(null);
  const [voiceUsage, setVoiceUsage] = useState<VoiceUsage | null>(null);
  const [openSession, setOpenSession] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TxEvent[]>([]);
  const [fullscreen, setFullscreen] = useState(false);
  const [liveSession, setLiveSession] = useState<string | null>(null);
  const [liveFullscreen, setLiveFullscreen] = useState(false);
  // Pipeline activity log (voice<->backend<->Claude) from the backend SSE stream.
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>([]);
  const [showDebug, setShowDebug] = useState(false);
  const [logFilter, setLogFilter] = useState("");
  const [logErrorsOnly, setLogErrorsOnly] = useState(false);
  const [logPaused, setLogPaused] = useState(false);
  const [logCopied, setLogCopied] = useState(false);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const logAtBottomRef = useRef(true);
  const esRef = useRef<EventSource | null>(null);

  const sessionRef = useRef<VoiceSession | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const orbRef = useRef<HTMLDivElement | null>(null);
  const glowRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const pollTimers = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const txPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Cost-log connection identity & snapshot timer. connectionId persists for one
  // voice connect()/disconnect() cycle so snapshots can be grouped later.
  const costLogRef = useRef<{
    connectionId: string;
    startedAt: number;
    provider: VoiceProvider;
    backend: ClaudeBackend;
    model: string;
    costSaver: boolean;
    voiceUsage: VoiceUsage | null;
    sessions: Sess[];
  } | null>(null);
  const costLogTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const totalCost = sessions.reduce((s, x) => s + (x.cost_usd || 0), 0);

  const filteredLog = debugEvents.filter((ev) => {
    if (logErrorsOnly && ev.kind !== "error") return false;
    if (logFilter) {
      const hay = `${ev.source} ${ev.dest} ${ev.kind} ${ev.summary} ${ev.session || ""}`.toLowerCase();
      if (!hay.includes(logFilter.toLowerCase())) return false;
    }
    return true;
  });

  // Copy the currently-shown events as readable text (full, untruncated lines)
  // for pasting into a bug report / sharing.
  const copyLog = () => {
    const text = filteredLog
      .map((e) => `${e.ts}  ${e.source}→${e.dest}  ${e.kind}${e.session ? "  " + e.session : ""}  ${e.summary}`)
      .join("\n");
    const done = () => {
      setLogCopied(true);
      window.setTimeout(() => setLogCopied(false), 1200);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done, () => undefined);
    }
  };

  useEffect(
    () => () => {
      stopAnalyser();
      stopPolling();
      if (txPollRef.current) clearInterval(txPollRef.current);
      if (costLogTimerRef.current) clearInterval(costLogTimerRef.current);
    },
    [],
  );

  // Keep the cost-log context in sync with the latest voice usage + Claude
  // session list so the periodic snapshot reads up-to-date values without
  // recreating the timer (which would reset its 30s phase).
  useEffect(() => {
    if (costLogRef.current) costLogRef.current.voiceUsage = voiceUsage;
  }, [voiceUsage]);
  useEffect(() => {
    if (costLogRef.current) costLogRef.current.sessions = sessions;
  }, [sessions]);

  const fetchTranscript = async (handle: string) => {
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
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
    // Name the originating request so the model can't confuse this update with a
    // previous prompt's (the backend threads it through as res.request).
    const reqRaw = (res.request || "").trim();
    const req = reqRaw.length > 90 ? `${reqRaw.slice(0, 90)}…` : reqRaw;
    const forReq = req ? ` for your request “${req}”` : "";
    if ((res.status === "needs_permission" || res.status === "needs_choice") && res.prompt) {
      setPending({
        sessionId: sid,
        kind: res.prompt.kind,
        text: res.prompt.text,
        options: res.prompt.options || [],
      });
      // Number the options and separate them with semicolons. The raw option
      // strings can themselves contain commas and arrows (e.g. '"Train-Us" →
      // "Train"'), so a bare comma-join renders them ambiguously when spoken.
      const opts = (res.prompt.options || [])
        .map((o: string, i: number) => `(${i + 1}) ${o}`)
        .join("; ");
      const msg =
        res.prompt.kind === "choice"
          ? `[Claude update] Claude is asking${forReq}: ${res.prompt.text}${opts ? ` The options are: ${opts}.` : ""} Read the options to the user and get their choice.`
          : `[Claude update] Claude needs permission${forReq} to ${res.prompt.text}. Ask the user to approve or deny.`;
      sessionRef.current?.injectUpdate(msg);
      logDebug("inject", msg, { session: sid }, "backend", "voice");
    } else if (res.status === "completed") {
      setPending(null);
      const txt = (res.assistant_text || "").trim();
      const msg = `[Claude update] Claude finished${forReq}. ${txt ? `It said: ${txt}` : "Done."} This is the latest result — summarize it briefly for the user, and do NOT say this request is still in progress.`;
      sessionRef.current?.injectUpdate(msg);
      logDebug("inject", msg, { session: sid }, "backend", "voice");
    } else if (res.status === "error") {
      setPending(null);
      const msg = `[Claude update] Claude hit an error${forReq}: ${res.error || "unknown"}. Tell the user.`;
      sessionRef.current?.injectUpdate(msg);
      logDebug("inject", msg, { session: sid }, "backend", "voice");
    }
    refreshSessions();
  };

  const pollSession = (sessionId: string) => {
    if (!sessionId || pollTimers.current.has(sessionId)) return;
    // Keep polling until the backend reports idle. On any non-working,
    // non-idle response, surface it and KEEP polling so we drain queued results
    // (poll_status returns one buffered result per call, FIFO).
    const timer = setInterval(async () => {
      try {
        const r = await fetch("/api/tools/execute", {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({ name: "poll_session", arguments: { session_id: sessionId } }),
        });
        const data = await r.json();
        const res = data?.result;
        if (!res || res.status === "working") return;     // keep polling
        if (res.status === "idle") {                       // queue drained, stop
          stopPolling(sessionId);
          return;
        }
        handleClaudeResult(res);                           // drain & keep polling
      } catch {
        /* transient; keep polling */
      }
    }, 1500);
    pollTimers.current.set(sessionId, timer);
  };

  // Restore toggle prefs.
  useEffect(() => {
    const p = localStorage.getItem("vc_provider");
    if (p === "azure" || p === "openai" || p === "gemini") setProvider(p);
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

  // Append one cost-log record to the backend's JSONL. Fire-and-forget — UI
  // never blocks on it and a failure here must not break the session.
  const logCost = (record: Record<string, unknown>) => {
    try {
      fetch("/api/cost/log", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ record }),
        keepalive: true, // lets the request survive a tab-close on the disconnect path
      }).catch(() => undefined);
    } catch {
      /* ignore */
    }
  };

  // Build a snapshot of voice + per-session Claude costs as of right now.
  // Pure read of refs/state; safe to call from intervals and event handlers.
  const buildCostSnapshot = (kind: "snapshot" | "connection_end") => {
    const ctx = costLogRef.current;
    if (!ctx) return null;
    const u = ctx.voiceUsage;
    const sessSnap = ctx.sessions.map((s) => ({
      handle: s.handle,
      name: s.name || null,
      cwd: s.cwd,
      backend: s.backend || null,
      model: s.model,
      mode: s.mode || null,
      status: s.status,
      cost_usd: s.cost_usd || 0,
    }));
    const claudeTotalUsd = sessSnap.reduce((acc, s) => acc + (s.cost_usd || 0), 0);
    const rec: Record<string, unknown> = {
      kind,
      connectionId: ctx.connectionId,
      provider: ctx.provider,
      model: ctx.model,
      backend: ctx.backend,
      costSaver: ctx.costSaver,
      voice: u
        ? {
            costUsd: u.costUsd,
            audioInTokens: u.audioInTokens,
            audioCachedTokens: u.audioCachedTokens,
            audioOutTokens: u.audioOutTokens,
            textInTokens: u.textInTokens,
            textCachedTokens: u.textCachedTokens,
            textOutTokens: u.textOutTokens,
            cacheHitRate: u.cacheHitRate,
          }
        : null,
      claudeSessions: sessSnap,
      claudeTotalUsd,
    };
    if (kind === "connection_end") {
      rec.durationSec = (Date.now() - ctx.startedAt) / 1000;
    }
    return rec;
  };

  const refreshSessions = async () => {
    try {
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: "list_sessions", arguments: {} }),
      });
      const data = await r.json();
      setSessions(data?.result?.sessions || []);
    } catch {
      /* ignore */
    }
  };

  // Keep the session list — and its live running/queued/pending badges — fresh
  // even when no voice session is driving. The other refreshSessions() calls are
  // event-driven (connect, after a result, mode/rename), which can't show the
  // work pipeline ticking down between events. list() is an in-memory read on the
  // backend, so a steady 2s poll is cheap. Initial call loads sessions on mount.
  useEffect(() => {
    refreshSessions();
    const t = setInterval(refreshSessions, 2000);
    return () => clearInterval(t);
  }, []);

  // The backend (and live terminal) is on :8000 at the same host the page loaded
  // from — works on localhost and from the phone alike. Mirrors LiveTerminal.
  const backendBase = () => {
    const host = typeof window !== "undefined" ? window.location.hostname || "localhost" : "localhost";
    const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "https" : "http";
    return `${proto}://${host}:8000`;
  };

  // Push a browser-only event (voice transcripts, [Claude update] injections,
  // voice errors, connect/disconnect) into the backend bus so it lands in the
  // file and streams back to every panel alongside the backend events.
  const logDebug = (
    kind: string,
    summary: string,
    detail?: unknown,
    source = "voice",
    dest = "backend",
  ) => {
    try {
      fetch(`${backendBase()}/debug/log`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ source, dest, kind, summary, detail }),
        keepalive: true,
      }).catch(() => undefined);
    } catch {
      /* ignore */
    }
  };

  // Subscribe to the unified pipeline stream. EventSource auto-reconnects; we
  // drop replayed events by monotonic seq so a reconnect doesn't duplicate.
  useEffect(() => {
    const es = new EventSource(withAuthParam(`${backendBase()}/debug/stream?limit=300`));
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const ev = JSON.parse(e.data) as DebugEvent;
        setDebugEvents((prev) => {
          const maxSeq = prev.length ? prev[prev.length - 1].seq : 0;
          if (ev.seq <= maxSeq) return prev; // replay/reconnect duplicate
          const next = [...prev, ev];
          return next.length > 800 ? next.slice(-800) : next;
        });
      } catch {
        /* ignore malformed frame */
      }
    };
    return () => {
      es.close();
      esRef.current = null;
    };
  }, []);

  // Auto-scroll to the newest line only when the user is already at the bottom,
  // so scrolling up to read history isn't yanked back down by new events.
  useEffect(() => {
    if (logPaused || !showDebug) return;
    if (!logAtBottomRef.current) return;
    const el = logScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [debugEvents, logPaused, showDebug]);

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
    else if (e.type === "error") {
      setStatus(`Error: ${e.message}`);
      logDebug("error", `voice error: ${e.message}`);
    } else if (e.type === "transcript") {
      if (e.final) {
        logDebug(
          "transcript",
          `[${e.role}] ${e.text}`,
          { role: e.role },
          e.role === "user" ? "user" : "voice",
          e.role === "user" ? "voice" : "user",
        );
      }
      setTimeline((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        // Coalesce streaming deltas into the in-progress turn only if it's still
        // the most recent item (nothing — including a tool row — came in between).
        if (last && last.kind === "turn" && last.role === e.role && !last.final) {
          next[next.length - 1] = { kind: "turn", role: e.role, text: e.text, final: e.final };
        } else {
          next.push({ kind: "turn", role: e.role, text: e.text, final: e.final });
        }
        return next.slice(-80);
      });
    } else if (e.type === "tool_call" && e.result !== undefined) {
      setTimeline((prev) => [...prev, { kind: "tool" as const, name: e.name, ok: e.ok }].slice(-80));
      const res: any = e.result;
      // tell_claude/answer_prompt now return "working" — poll for the real result.
      if (
        (e.name === "tell_claude" || e.name === "answer_prompt" || e.name === "run_slash_command") &&
        res?.status === "working" &&
        res.session_id
      ) {
        pollSession(res.session_id);
      }
      if (e.name === "interrupt_session" || e.name === "close_session") stopPolling(res?.session_id);
      if (
        ["start_session", "tell_claude", "answer_prompt", "interrupt_session", "set_mode", "close_session", "rename_session", "run_slash_command"].includes(
          e.name,
        )
      ) {
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
      const model = s.activeModel || params.model || PROVIDER_LABEL[provider];
      setModelLabel(model);
      setConnected(true);
      setMuted(false);
      logDebug("info", `voice connected (${provider} · ${model})`, { provider, model, backend }, "voice", "user");
      refreshSessions();
      // Start the cost-log lifecycle: emit a connection_start record, hold a
      // context object updated by the voiceUsage/sessions effects, then snapshot
      // every 30s until disconnect.
      const connectionId =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      costLogRef.current = {
        connectionId,
        startedAt: Date.now(),
        provider,
        backend,
        model,
        costSaver,
        voiceUsage: null,
        sessions: [],
      };
      logCost({
        kind: "connection_start",
        connectionId,
        provider,
        model,
        backend,
        costSaver,
      });
      if (costLogTimerRef.current) clearInterval(costLogTimerRef.current);
      costLogTimerRef.current = setInterval(() => {
        const snap = buildCostSnapshot("snapshot");
        if (snap) logCost(snap);
      }, 30_000);
    } catch (err: any) {
      setStatus(`Failed: ${err?.message || err}`);
      setVstate("idle");
    }
  };

  const disconnect = () => {
    logDebug("info", "voice disconnected", undefined, "voice", "user");
    // Final cost-log record BEFORE we tear down state, while voiceUsage and the
    // session list still reflect what happened. keepalive on the POST lets it
    // survive even if the user closes the tab right after.
    const finalRec = buildCostSnapshot("connection_end");
    if (finalRec) logCost(finalRec);
    if (costLogTimerRef.current) {
      clearInterval(costLogTimerRef.current);
      costLogTimerRef.current = null;
    }
    costLogRef.current = null;

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

  // Switch a session's permission mode by click (voice "switch to plan mode" also works).
  const [modeBusy, setModeBusy] = useState<string | null>(null);
  const switchMode = async (handle: string, mode: string) => {
    setModeBusy(handle);
    // Optimistic: reflect the target immediately, then reconcile from the backend.
    setSessions((prev) => prev.map((s) => (s.handle === handle ? { ...s, mode } : s)));
    try {
      await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: "set_mode", arguments: { session_id: handle, mode } }),
      });
    } finally {
      await refreshSessions();
      setModeBusy(null);
    }
  };

  // Inline session rename (voice "call this one X" also works).
  const [editing, setEditing] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const startRename = (handle: string, current: string) => {
    setEditing(handle);
    setDraftName(current);
  };
  const commitRename = async (handle: string) => {
    const name = draftName.trim();
    setEditing(null);
    const prev = sessions.find((s) => s.handle === handle)?.name || "";
    if (!name || name === prev) return;
    setSessions((p) => p.map((s) => (s.handle === handle ? { ...s, name } : s)));
    try {
      await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({
          name: "rename_session",
          arguments: { session_id: handle, name },
        }),
      });
    } finally {
      refreshSessions(); // reconcile (e.g. name clash rejected on the server)
    }
  };

  // Manual fallback for answering a pending prompt by click (voice also works).
  const answerPrompt = async (choice: string) => {
    if (!pending) return;
    const p = pending;
    setPending(null);
    await fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
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
      <header className="topbar">
        <div className={`brand ${connected ? "live" : ""}`}>
          <span className="dot" />
          <span className="logo">Voice<span className="sep">·</span>Claude</span>
        </div>
        <div className="topmeta">
          {connected ? STATE_LABEL[vstate] : "Offline"} · {PROVIDER_LABEL[provider]}
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
            {!connected ? (
              <button className="talk" onClick={connect}>
                Connect &amp; talk
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
            <button className="textbtn" onClick={refreshSessions}>
              Refresh
            </button>
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
          <div className="orbcap">{muted ? "Muted" : STATE_LABEL[vstate]}</div>
        </div>
      </section>

      <div className="controls-row">
          <div className={`seg ${connected ? "locked" : ""}`} role="group" aria-label="Voice provider">
            {(["azure", "openai", "gemini"] as VoiceProvider[]).map((p) => (
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
      {connected && (
        <div className="togglehint">Disconnect to change provider, backend, or cost saver.</div>
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

      <div className="panels">
        <div className="panel">
          <h2>
            Conversation <span className="ct">live</span>
          </h2>
          <div className="rule" />
          <div className="scroll conv-scroll">
            {timeline.length === 0 && (
              <div className="empty">Assistant replies and Claude actions show here.</div>
            )}
            {timeline.map((item, i) =>
              item.kind === "turn" ? (
                <div key={i} className={`bubble ${item.role}`}>
                  <div className="who">{item.role === "user" ? "You" : "Assistant"}</div>
                  {item.text}
                </div>
              ) : (
                <div key={i} className={`toolrow ${item.ok === false ? "err" : ""}`}>
                  → {item.name} {item.ok === false ? "✗" : "✓"}
                </div>
              ),
            )}
          </div>
        </div>

        <div className="panel">
          <h2>
            Claude sessions <span className="ct">{sessions.length || "—"} active</span>
          </h2>
          <div className="rule" />
          <div className="scroll">
            {sessions.length === 0 && <div className="empty">No active sessions.</div>}
            {sessions.map((s) => {
              const cmd = s.session_id ? `cd ${s.cwd} && claude --resume ${s.session_id}` : null;
              const tmuxCmd = s.backend === "cli" ? `tmux attach -t vc_${s.handle.slice(0, 8)}` : null;
              const open = openSession === s.handle;
              return (
                <div key={s.handle} className="sess">
                  <div className="head">
                    {editing === s.handle ? (
                      <input
                        className="nameedit"
                        autoFocus
                        value={draftName}
                        onChange={(e) => setDraftName(e.target.value)}
                        onBlur={() => commitRename(s.handle)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") commitRename(s.handle);
                          else if (e.key === "Escape") setEditing(null);
                        }}
                      />
                    ) : (
                      <button
                        className="name namebtn"
                        title="Rename session"
                        onClick={() => startRename(s.handle, s.name || (s.cwd.split("/").pop() ?? ""))}
                      >
                        {s.name || s.cwd.split("/").pop()} <span className="penicon">✎</span>
                      </button>
                    )}
                    {s.backend && <span className="bk">{s.backend.toUpperCase()}</span>}
                    <span className={`modechip ${s.mode || "default"}`}>
                      {MODE_LABEL[s.mode || "default"] || "Normal"}
                    </span>
                    {s.running && (
                      <span className="qchip run" title="A turn is executing right now">
                        <span className="qdot" />
                        working
                      </span>
                    )}
                    {(s.queued ?? 0) > 0 && (
                      <span className="qchip queued" title="Turns waiting behind the current one">
                        {s.queued} queued
                      </span>
                    )}
                    {(s.pending ?? 0) > 0 && (
                      <span className="qchip pending" title="Finished turns not yet narrated by the voice agent">
                        {s.pending} unread
                      </span>
                    )}
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
                  {(s.queue?.length ?? 0) > 0 && (
                    <div className="queuelist">
                      {s.queue!.map((q, i) => (
                        <div key={i} className={`qitem ${q.state}`}>
                          <span className="qmark">{q.state === "running" ? "▶ now" : "⋯ queued"}</span>
                          <span className="qtext">{q.text || "(turn)"}</span>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="modebar" role="group" aria-label="Permission mode">
                    <span className="modelbl">Mode</span>
                    {MODES.map((m) => (
                      <button
                        key={m.id}
                        className={`modepill ${(s.mode || "default") === m.id ? "on" : ""}`}
                        title={m.title}
                        disabled={modeBusy === s.handle}
                        onClick={() => switchMode(s.handle, m.id)}
                      >
                        {m.label}
                      </button>
                    ))}
                  </div>
                  {open && <div className="transcript">{renderTimeline(transcript)}</div>}
                  {tmuxCmd && (
                    <div className="handoff">
                      <span className="hlabel">attach</span>
                      <code>{tmuxCmd}</code>
                      <button onClick={() => navigator.clipboard.writeText(tmuxCmd)}>Copy</button>
                    </div>
                  )}
                  {cmd && (
                    <div className="handoff">
                      <span className="hlabel">resume</span>
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

      {showDebug && (
        <div className="panel debugpanel">
          <div className="loghead">
            <h2>
              Activity <span className="ct">{filteredLog.length} / {debugEvents.length}</span>
            </h2>
            <div className="logctl">
              <input
                className="logsearch"
                placeholder="filter…"
                value={logFilter}
                onChange={(e) => setLogFilter(e.target.value)}
              />
              <button className={`textbtn ${logErrorsOnly ? "on" : ""}`} onClick={() => setLogErrorsOnly((v) => !v)}>
                Errors
              </button>
              <button className={`textbtn ${logPaused ? "on" : ""}`} onClick={() => setLogPaused((v) => !v)}>
                {logPaused ? "Resume" : "Pause"}
              </button>
              <button className={`textbtn ${logCopied ? "on" : ""}`} onClick={copyLog} title="Copy all shown events to the clipboard">
                {logCopied ? "Copied ✓" : "Copy"}
              </button>
              <button className="textbtn" onClick={() => setDebugEvents([])}>
                Clear
              </button>
            </div>
          </div>
          <div className="rule" />
          <div
            className="logscroll"
            ref={logScrollRef}
            onScroll={(e) => {
              const el = e.currentTarget;
              logAtBottomRef.current =
                el.scrollHeight - el.scrollTop - el.clientHeight < 40;
            }}
          >
            {filteredLog.length === 0 && (
              <div className="empty">No matching events yet — talk to the agent and the full pipeline shows here.</div>
            )}
            {filteredLog.map((ev) => (
              <div
                key={ev.seq}
                className={`logrow k-${ev.kind}`}
                title={ev.detail ? JSON.stringify(ev.detail).slice(0, 800) : undefined}
              >
                <span className="lt">{ev.ts.slice(11, 23)}</span>
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
      )}

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
