"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { RealtimeSession } from "@/lib/realtime";
import { GeminiSession } from "@/lib/gemini";
import { ClaudeBackend, RealtimeEvent, RealtimeOptions, VoiceProvider, VoiceSession, VoiceState, VoiceUsage } from "@/lib/voice";
import { INSTRUCTIONS, yuriContextBlock, type YuriContext } from "@/lib/instructions";
import { authHeaders, withAuthParam } from "@/lib/auth";
import { scopedClearPending } from "@/lib/promptState";
import {
  NARRATION_MODES,
  NARRATION_REPLAY_LIMIT,
  createSpokenGate,
  isBlockingNarration,
  isNarrationMode,
  narrationOf,
  replayLimitFor,
  type NarrationMode,
  type SpokenGate,
} from "@/lib/narration";
import LiveTerminal from "./LiveTerminal";
import { Icon } from "./ui/Icon";
import { CopyBtn } from "./ui/CopyBtn";
import { MarkdownLite } from "./conversation/MarkdownLite";
import { Timeline } from "./conversation/Timeline";
import { SessionCard, renderTimeline, type TxEvent } from "./SessionCard";
import { ActivityFeed, type DebugEvent } from "./ActivityFeed";
import { splitPlan, type TimelineItem } from "@/lib/timeline";
import { clip } from "@/lib/format";
import { BACKEND_LABEL, type Sess } from "@/lib/sessions";
import { MODEL_OPTIONS, connectionParams, PROVIDER_LABEL, NARRATION_LABEL, orbCaption } from "@/lib/voiceui";

type Pending = { sessionId: string; kind: string; text: string; options: string[] } | null;

export default function VoiceAgent() {
  const [connected, setConnected] = useState(false);
  const [provider, setProvider] = useState<VoiceProvider>("gemini");
  // The OpenAI-family route (native vs Azure) last used, so toggling away to
  // Gemini and back lands on the same route instead of resetting.
  const openaiRouteRef = useRef<Exclude<VoiceProvider, "gemini">>("azure");
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
  const [backend, setBackend] = useState<ClaudeBackend>("cli");
  // The user's chosen model per provider; falls back to that provider's default.
  const [modelByProvider, setModelByProvider] = useState<Partial<Record<VoiceProvider, string>>>({});
  // Set true when the user tries to change the model while connected, so the UI
  // can prompt them to disconnect first.
  const [modelLockHint, setModelLockHint] = useState(false);
  const [modelLabel, setModelLabel] = useState("");
  // How much Yuri narrates. Deliberately NOT persisted to localStorage: the
  // backend remembers it (settings row) and voice can change it mid-sentence,
  // so a second copy here could disagree with what she actually speaks. null
  // means "not read yet / backend unreachable" — the control stays clickable.
  const [narrationMode, setNarrationMode] = useState<NarrationMode | null>(null);
  const [narrationBusy, setNarrationBusy] = useState(false);
  const [vstate, setVstate] = useState<VoiceState>("idle");
  const [muted, setMuted] = useState(false);
  const [status, setStatus] = useState("Tap connect and start talking.");
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  // Monotonic id for tool rows so each expandable <details> keeps its open
  // state across re-renders even as old timeline items roll off the cap.
  const toolIdRef = useRef(0);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [pending, setPending] = useState<Pending>(null);
  const [voiceUsage, setVoiceUsage] = useState<VoiceUsage | null>(null);
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
  // Pipeline activity log (voice<->backend<->Claude) from the backend SSE stream.
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>([]);
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
  const esRef = useRef<EventSource | null>(null);
  // The narration stream is a SECOND, separate subscription from the Activity
  // panel's /debug/stream above: different endpoint, different payload, and it
  // only lives while a voice session is connected.
  const narrationEsRef = useRef<EventSource | null>(null);
  // Held across reconnects on purpose — see createSpokenGate's comment. Built
  // lazily at first use: this component re-renders on every debug event and
  // volume tick, and useRef(createSpokenGate()) would allocate a gate on each
  // one only to throw it away.
  const narrationGateRef = useRef<SpokenGate | null>(null);

  const sessionRef = useRef<VoiceSession | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const orbRef = useRef<HTMLDivElement | null>(null);
  const glowRef = useRef<HTMLDivElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  // Per-stream analysers feeding ONE smoothed --amp: keyed by source so the
  // mic and the assistant audio can both drive the orb without clobbering
  // each other. smoothed holds the envelope state across rAF frames.
  const analysersRef = useRef<Map<"mic" | "remote", { analyser: AnalyserNode; buf: Uint8Array }>>(
    new Map(),
  );
  const smoothedRef = useRef(0);
  // Mirror `connected` into a ref so the rAF orb loop (a stable closure) can gate
  // volume scaling without re-subscribing — the orb stays still until fully
  // connected, even though the mic analyser attaches during the handshake.
  const connectedRef = useRef(false);
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
    voiceUsage: VoiceUsage | null;
    sessions: Sess[];
  } | null>(null);
  const costLogTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const totalCost = sessions.reduce((s, x) => s + (x.cost_usd || 0), 0);

  // Azure deployment names are server infra (env-configured), so the dropdown
  // options come from the backend rather than being hardcoded here.
  const [azureModels, setAzureModels] = useState<{ value: string; label: string }[]>([]);
  useEffect(() => {
    fetch("/api/voice/models", { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const names: string[] = d?.azure || [];
        setAzureModels(names.map((n) => ({ value: n, label: n })));
      })
      .catch(() => undefined); // no dropdown for Azure, same as before
  }, []);

  // The model to connect with for the current provider. Validate the stored
  // choice against the current option list so a model id that's since been
  // removed from the API (or a stale localStorage value) falls back to the
  // provider's default instead of leaving the select on a dead value.
  const modelOptions = provider === "azure" ? azureModels : MODEL_OPTIONS[provider];
  const storedModel = modelByProvider[provider];
  const model = modelOptions.some((o) => o.value === storedModel)
    ? (storedModel as string)
    : (modelOptions[0]?.value ?? "");

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
        setAttachOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [fullscreen, liveFullscreen]);

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

  const clearPendingFor = (sessionId?: string) =>
    setPending((p) => scopedClearPending(p, sessionId));

  // A background Claude turn reached a result — surface any prompt in the UI and
  // tell the voice model to narrate it (works even mid-conversation).
  const handleClaudeResult = (res: any) => {
    if (!res || typeof res !== "object") return;
    const sid = res.session_id;
    // The prompt card is UI state and stays client-side; only the WORDING moved
    // to the backend, which threads the originating request, the option
    // numbering and the risk lead-in into res.narration.
    if ((res.status === "needs_permission" || res.status === "needs_choice") && res.prompt) {
      setPending({
        sessionId: sid,
        kind: res.prompt.kind,
        text: res.prompt.text,
        options: res.prompt.options || [],
      });
    } else if (res.status === "completed" || res.status === "error") {
      clearPendingFor(sid);
    }
    // Wording comes from the backend so it is consistent, testable, and the
    // same for any future non-browser surface. See lib/narration.ts. The poll
    // owns the four session-turn events (backend yuri/narration/policy.py), so
    // the same result never also arrives narrated on the event stream.
    const line = narrationOf(res);
    if (line) {
      // A permission request or a question BLOCKS the agent on an answer, so
      // the transport's queue bound must never evict it — the frontend half of
      // the backend's ALWAYS_SPEAK set. poll_status hands back each buffered
      // result exactly once, so a dropped ask is never re-offered.
      sessionRef.current?.injectUpdate(line, { blocking: isBlockingNarration(res) });
      logDebug("inject", line, { session: sid }, "backend", "voice");
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
    const b = localStorage.getItem("vc_backend");
    if (b === "cli" || b === "sdk") setBackend(b);
    const m = localStorage.getItem("vc_models");
    if (m) {
      try {
        const parsed = JSON.parse(m);
        if (parsed && typeof parsed === "object") setModelByProvider(parsed);
      } catch {
        /* ignore malformed pref */
      }
    }
  }, []);
  useEffect(() => {
    localStorage.setItem("vc_provider", provider);
  }, [provider]);
  useEffect(() => {
    localStorage.setItem("vc_backend", backend);
  }, [backend]);
  useEffect(() => {
    localStorage.setItem("vc_models", JSON.stringify(modelByProvider));
  }, [modelByProvider]);
  // Clear the "disconnect to change model" prompt once the user disconnects.
  useEffect(() => {
    if (!connected) setModelLockHint(false);
  }, [connected]);

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

  const fetchSessions = async (): Promise<Sess[]> => {
    const r = await fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ name: "list_sessions", arguments: {} }),
    });
    const data = await r.json();
    return data?.result?.sessions || [];
  };

  const refreshSessions = async () => {
    try {
      setSessions(await fetchSessions());
    } catch {
      /* ignore */
    }
  };

  // Read the server's narration mode. The backend is the single source of
  // truth, so a failure (it answers 503 while the home dir is unavailable)
  // leaves the last known value on screen rather than blanking the control —
  // and the buttons still work, because a PUT sets the mode outright.
  const refreshNarrationMode = useCallback(async () => {
    try {
      const r = await fetch("/api/yuri/narration", { headers: authHeaders() });
      if (!r.ok) return;
      const d = await r.json();
      if (isNarrationMode(d?.mode)) setNarrationMode(d.mode);
    } catch {
      /* keep the last known mode */
    }
  }, []);

  // On mount, and again on every connect/disconnect. The retry matters: if the
  // backend was down at page load (it answers 503 while its home dir is
  // unavailable) a mount-only read would leave the control showing no
  // selection forever, even after the backend came back.
  useEffect(() => {
    refreshNarrationMode();
  }, [refreshNarrationMode, connected]);

  const changeNarrationMode = async (mode: NarrationMode) => {
    const prev = narrationMode;
    setNarrationMode(mode); // optimistic, like switchMode; reconciled below
    setNarrationBusy(true);
    try {
      const r = await fetch("/api/yuri/narration", {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ mode }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      if (isNarrationMode(d?.mode)) setNarrationMode(d.mode);
    } catch (err: any) {
      setNarrationMode(prev); // never show a mode the backend didn't accept
      logDebug("error", `narration mode change failed: ${err?.message || err}`, { mode }, "user", "backend");
    } finally {
      setNarrationBusy(false);
    }
  };

  // Point-in-time context appended to the static instructions at connect():
  // Claude sessions outlive voice connections, so a fresh model otherwise wakes
  // up blind to what's running and re-asks (or worse, re-creates). Built ONCE
  // per connection and never rewritten mid-session — instructions sit at the
  // front of the prompt-cache prefix, and updating them would re-bill the whole
  // prefix uncached on the next response. list_sessions stays the live truth.
  const dynamicContext = (sess: Sess[]): string => {
    const lines = sess.map((s) => {
      const state = s.running ? "working" : s.status || "idle";
      const extras = [
        s.mode && s.mode !== "default" ? `${s.mode} mode` : "",
        s.queued ? `${s.queued} queued` : "",
      ].filter(Boolean);
      const folder = s.cwd.replace(/^\/Users\/[^/]+/, "~");
      let line = `- "${s.name || s.handle.slice(0, 8)}" · ${folder} · ${[state, ...extras].join(", ")}`;
      // A prompt that fired before this conversation connected was never narrated.
      if (s.prompt) {
        const opts = (s.prompt.options || []).map((o, i) => `(${i + 1}) ${o}`).join("; ");
        line +=
          `\n  WAITING ON ${s.prompt.kind === "choice" ? "A QUESTION" : "PERMISSION"} ` +
          `(answer via answer_prompt): ${s.prompt.text}${opts ? ` Options: ${opts}.` : ""}`;
      }
      return line;
    });
    return [
      "",
      "",
      "CURRENT STATE (snapshot from the moment this conversation connected — it goes stale as you work; list_sessions is the live source of truth):",
      `- Connected: ${new Date().toLocaleString()}`,
      lines.length
        ? `- Open agent sessions (reuse these for matching work instead of starting new ones):\n${lines.join("\n")}`
        : "- Open agent sessions: none — call start_session before any work.",
    ].join("\n");
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

  // The backend (and live terminal) is on BACKEND_PORT at the same host the page
  // loaded from — works on localhost and from the phone alike. Mirrors LiveTerminal.
  const backendBase = () => {
    const host = typeof window !== "undefined" ? window.location.hostname || "localhost" : "localhost";
    const proto = typeof window !== "undefined" && window.location.protocol === "https:" ? "https" : "http";
    return `${proto}://${host}:${process.env.BACKEND_PORT || "8000"}`;
  };

  // Push a browser-only event (voice transcripts, narration injections,
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

  // Mission-level narration: the poll loop owns the session-turn events, the
  // stream owns mission state and lost contact (the backend's
  // yuri/narration/policy.py decides which, and sends narration:null on the
  // carrier that doesn't own an event — so subscribing to both cannot
  // double-speak). Only frames carrying a line are spoken.
  //
  // Gated on the voice session being connected: narrating into a closed
  // session is pointless, and it avoids holding a stream open on a page nobody
  // is talking to.
  useEffect(() => {
    if (!connected) return;
    let cancelled = false;
    const gate = (narrationGateRef.current ??= createSpokenGate());
    (async () => {
      // The stream ALWAYS replays its newest events — `limit` is clamped to a
      // minimum of 1 server-side, so there is no way to ask for no replay.
      // Seeding the gate with those same ids is what makes the replay silent;
      // without it, connecting would re-speak whatever happened last, possibly
      // hours ago. When the seed SUCCEEDS both limits are
      // NARRATION_REPLAY_LIMIT and must stay equal (see its comment).
      //
      // When it fails we narrow the stream to one event instead of trusting an
      // unseeded 50: each injected line costs a full model response, so an
      // unseeded wide replay would read out minutes of history at connect.
      // replayLimitFor() holds that reasoning.
      let seeded = false;
      try {
        const r = await fetch(`/api/yuri/events?limit=${NARRATION_REPLAY_LIMIT}`, { headers: authHeaders() });
        if (r.ok) {
          const d = await r.json();
          gate.seed((Array.isArray(d?.events) ? d.events : []).map((e: any) => e?.id));
          seeded = true;
        }
      } catch {
        /* nothing to seed — fall back to a 1-event replay below */
      }
      if (cancelled) return; // disconnected while we were seeding
      const es = new EventSource(
        withAuthParam(`${backendBase()}/yuri/events/stream?limit=${replayLimitFor(seeded)}`),
      );
      narrationEsRef.current = es;
      es.onmessage = (m) => {
        try {
          const frame = JSON.parse(m.data);
          const line = gate.lineFor(frame);
          if (line) {
            // Always false today (the blocking types are poll-owned, so they
            // carry no line here) — passed anyway so both carriers apply the
            // one rule if ownership ever moves.
            sessionRef.current?.injectUpdate(line, { blocking: isBlockingNarration(frame) });
            logDebug("inject", line, undefined, "backend", "voice");
          }
        } catch {
          /* malformed frame; ignore */
        }
      };
    })();
    return () => {
      cancelled = true;
      narrationEsRef.current?.close();
      narrationEsRef.current = null;
    };
  }, [connected]);

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

  // Keep the orb-loop's connection gate current.
  useEffect(() => {
    connectedRef.current = connected;
  }, [connected]);

  // Drive the orb's size from live audio volume. Both the user's mic and the
  // assistant's speech feed analysers on ONE shared AudioContext; each frame we
  // take the LOUDER of the two as the instantaneous target, then envelope-smooth
  // it (fast attack, slow release) so the orb rises lively and falls gently
  // instead of twitching frame-to-frame.
  const orbLoop = () => {
    let target = 0;
    // Only react to audio once fully connected; while connecting the analyser is
    // already live (mic acquired) but the orb should stay at rest (target 0).
    if (connectedRef.current) {
      for (const { analyser, buf } of analysersRef.current.values()) {
        analyser.getByteTimeDomainData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) {
          const v = (buf[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.min(1, Math.sqrt(sum / buf.length) * 3.2);
        if (rms > target) target = rms; // loudest source wins
      }
    }
    // Envelope: snap up quickly, ease down slowly.
    const k = target > smoothedRef.current ? 0.35 : 0.08;
    smoothedRef.current += (target - smoothedRef.current) * k;
    const amp = smoothedRef.current.toFixed(3);
    orbRef.current?.style.setProperty("--amp", amp);
    glowRef.current?.style.setProperty("--amp", amp);
    rafRef.current = requestAnimationFrame(orbLoop);
  };

  // Attach a stream (mic or remote) to the shared analyser graph. The context is
  // created lazily on first attach and reused, so a second stream never clobbers
  // the first. The rAF loop starts once and reads whatever analysers are present.
  const attachStream = (stream: MediaStream, kind: "mic" | "remote") => {
    try {
      let ctx = audioCtxRef.current;
      if (!ctx || ctx.state === "closed") {
        ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
        audioCtxRef.current = ctx;
      }
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      src.connect(analyser);
      const buf = new Uint8Array(analyser.frequencyBinCount);
      analysersRef.current.set(kind, { analyser, buf });
      if (rafRef.current == null) rafRef.current = requestAnimationFrame(orbLoop);
    } catch {
      /* analyser optional */
    }
  };

  const stopAnalyser = () => {
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    for (const { analyser } of analysersRef.current.values()) {
      try {
        analyser.disconnect();
      } catch {
        /* already gone */
      }
    }
    analysersRef.current.clear();
    smoothedRef.current = 0;
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
        // Fast path: streaming deltas coalesce into the in-progress turn while
        // it's still the most recent item.
        if (last && last.kind === "turn" && last.role === e.role && !last.final) {
          next[next.length - 1] = { kind: "turn", role: e.role, text: e.text, final: e.final };
          return next.slice(-80);
        }
        // Otherwise this event belongs to an earlier row (or is genuinely new).
        // Walk back to the most recent turn for this role — over tool rows AND
        // the other role's turns, because Gemini's input transcription keeps
        // streaming after the assistant starts replying, so same-utterance
        // updates legitimately arrive from beyond the role boundary.
        // The discriminator between "same utterance, still streaming" and "new
        // utterance" is cumulative text: a continuation always EXTENDS the
        // draft (startsWith); a new utterance never does. Role-boundary or
        // text-equality rules alone each broke one side of this (overwritten /
        // missing "You" rows vs duplicated rows).
        let crossedOtherRole = false;
        for (let k = next.length - 1; k >= 0; k--) {
          const it = next[k];
          if (it.kind !== "turn") continue;
          if (it.role !== e.role) {
            crossedOtherRole = true;
            continue;
          }
          if (!it.final && e.text.startsWith(it.text)) {
            // Same accumulator generation — update/finalize the draft in place,
            // wherever it sits in the timeline.
            next[k] = { kind: "turn", role: e.role, text: e.text, final: e.final };
            return next.slice(-80);
          }
          // A final re-sent verbatim IMMEDIATELY (nothing from the other role
          // in between) is a provider echo — drop it. The same text after the
          // other role replied is a genuine repeat — keep it.
          if (it.final && e.final && it.text === e.text && !crossedOtherRole) return next;
          break;
        }
        next.push({ kind: "turn", role: e.role, text: e.text, final: e.final });
        return next.slice(-80);
      });
    } else if (e.type === "tool_call" && e.result !== undefined) {
      const id = ++toolIdRef.current;
      setTimeline((prev) =>
        [
          ...prev,
          { kind: "tool" as const, id, name: e.name, ok: e.ok, args: e.arguments, result: e.result },
        ].slice(-80),
      );
      const res: any = e.result;
      // tell_claude/answer_prompt now return "working" — poll for the real result.
      if (
        (e.name === "tell_claude" || e.name === "answer_prompt" || e.name === "run_slash_command") &&
        res?.status === "working" &&
        res.session_id
      ) {
        pollSession(res.session_id);
      }
      // Dismiss the prompt card on a voice answer, same as clicking its buttons;
      // a follow-up prompt re-raises a fresh card via the poll.
      if (e.name === "answer_prompt") {
        clearPendingFor(res?.session_id);
      }
      // A mode switch can auto-approve the pending permission (prompt_resolved).
      // The backend resolves it asynchronously, so clear the now-stale card and
      // resume polling to drain the resumed turn — else the card hangs.
      if (e.name === "set_mode" && res?.prompt_resolved && res.session_id) {
        clearPendingFor(res.session_id);
        pollSession(res.session_id);
      }
      // Interrupt and close both dismiss any pending permission server-side
      // (deny + escape / deny + kill the session), so drop that session's now-
      // stale card along with its poll loop.
      if (e.name === "interrupt_session" || e.name === "close_session") {
        stopPolling(res?.session_id);
        clearPendingFor(res?.session_id);
      }
      // The model muted itself by voice ("mute", "be quiet"). Flip the real mic
      // state to match, exactly as the on-screen Mute button does. Unmute stays
      // manual — once muted the model can't hear a spoken "unmute".
      if (e.name === "mute" && e.ok) {
        setMuted(true);
        sessionRef.current?.setMuted(true);
      }
      // The user changed how much she narrates by voice ("be quiet", "tell me
      // everything"). Re-read the server value so the on-screen toggle agrees
      // with what she'll actually speak.
      if (e.name === "set_narration") refreshNarrationMode();
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
    // Fetch the session list fresh rather than trusting the 2s poll — on a cold
    // page load the polled state may still be empty, and baking a wrong "no
    // sessions" snapshot invites the model to start duplicates.
    let snapshot = sessions;
    try {
      snapshot = await fetchSessions();
      setSessions(snapshot);
    } catch {
      /* offline backend surfaces in start(); use the last poll for the snapshot */
    }
    // Yuri's own context (memory, journal, missions, agent health). Best-effort:
    // an unreachable backend must not block connecting — the snapshot above
    // already covers live sessions.
    let yuriCtx: YuriContext | null = null;
    try {
      const r = await fetch("/api/yuri/context", { headers: authHeaders() });
      if (r.ok) yuriCtx = (await r.json()) as YuriContext;
    } catch {
      logDebug("error", "yuri context unavailable at connect", undefined, "voice", "backend");
    }
    const params = connectionParams(provider, model);
    const opts: RealtimeOptions = {
      ...params,
      instructions: INSTRUCTIONS + dynamicContext(snapshot) + yuriContextBlock(yuriCtx),
      backend,
      onEvent,
      onDebug: (msg) => logDebug("info", `transport: ${msg}`, undefined, "voice", "backend"),
      onRemoteStream: (s) => attachStream(s, "remote"),
      onLocalStream: (s) => attachStream(s, "mic"),
    };
    const s: VoiceSession =
      provider === "gemini" ? new GeminiSession(opts) : new RealtimeSession(opts);
    sessionRef.current = s;
    try {
      await s.start(audioRef.current!);
      const activeModel = s.activeModel || params.model || PROVIDER_LABEL[provider];
      setModelLabel(activeModel);
      setConnected(true);
      setMuted(false);
      logDebug("info", `voice connected (${provider} · ${activeModel})`, { provider, model: activeModel, backend }, "voice", "user");
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
        model: activeModel,
        voiceUsage: null,
        sessions: [],
      };
      logCost({
        kind: "connection_start",
        connectionId,
        provider,
        model: activeModel,
        backend,
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
      const r = await fetch("/api/tools/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: "set_mode", arguments: { session_id: handle, mode } }),
      });
      // Mirror the voice set_mode path: if the switch auto-approved a pending
      // permission, clear the stale card and resume polling for the resumed turn.
      const res = (await r.json())?.result;
      if (res?.prompt_resolved) {
        clearPendingFor(handle);
        pollSession(handle);
      }
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
    clearPendingFor(p.sessionId);
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
                  onClick={() => changeNarrationMode(m)}
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
                  setModelByProvider((prev) => ({ ...prev, [provider]: e.target.value }));
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
                onCommitRename={() => commitRename(s.handle)}
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
          onClear={() => setDebugEvents([])}
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

      <audio ref={audioRef} hidden />
    </div>
  );
}
