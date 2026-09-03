"use client";

// The one place that owns everything global-and-continuous: the realtime
// voice connection, the transcript timeline, the injection queue, both SSE
// subscriptions (the unified pipeline log and the Yuri events stream), tool
// dispatch, and the shared lists (sessions/approvals/missions/agents) the nav
// badges need. Any view reaches it through useYuri().
//
// What it deliberately does NOT hold: a mission's steps, a project's detail,
// a session's transcript. Those are per-view detail — the view that shows
// them fetches them itself. Holding them here would make this a god-object
// every view re-renders on.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import { RealtimeSession } from "@/lib/realtime";
import { GeminiSession } from "@/lib/gemini";
import {
  ClaudeBackend,
  RealtimeEvent,
  RealtimeOptions,
  // Aliased: this file also exports a component named `VoiceProvider` (the
  // React context provider), which would otherwise collide with lib/voice's
  // `VoiceProvider` type (the azure|openai|gemini choice).
  VoiceProvider as VoiceProviderKind,
  VoiceSession,
  VoiceState,
  VoiceUsage,
} from "@/lib/voice";
import {
  INSTRUCTIONS,
  yuriContextBlock,
  // Aliased: lib/instructions.ts already exports a type named `YuriContext`
  // (the connect-time snapshot of home/memory/journal/missions/agents) which
  // would collide with this file's `YuriContext` (the React context shape).
  type YuriContext as YuriConnectSnapshot,
} from "@/lib/instructions";
import { authHeaders, withAuthParam } from "@/lib/auth";
import { yget, yput } from "@/lib/api";
import type { Approval, Mission } from "@/lib/yuriTypes";
import { scopedClearPending } from "@/lib/promptState";
import {
  NARRATION_REPLAY_LIMIT,
  createSpokenGate,
  isBlockingNarration,
  isNarrationMode,
  narrationOf,
  replayLimitFor,
  type NarrationMode,
  type SpokenGate,
} from "@/lib/narration";
import { type TimelineItem } from "@/lib/timeline";
import { type Sess } from "@/lib/sessions";
import { MODEL_OPTIONS, connectionParams, PROVIDER_LABEL } from "@/lib/voiceui";
import type { DebugEvent } from "./ActivityFeed";

export type Pending = { sessionId: string; kind: string; text: string; options: string[] } | null;

// --- shared-list shapes -----------------------------------------------------
// Approval and Mission are the real backend dataclass shapes, from
// lib/yuriTypes.ts (re-exported here so existing call sites that imported
// them from this module keep working) — just enough for nav badges and list
// rendering. A view that needs one record's full detail fetches it.
export type { Approval, Mission };

export type Agent = {
  id: string;
  name: string;
  online: boolean;
  version?: string | null;
  detail?: string | null;
  checked_at?: string;
  capabilities?: Record<string, unknown>;
  active_sessions?: number;
};

// A frame off /yuri/events/stream: a serialized YuriEvent (yuri/domain/event.py)
// plus the `narration` line the backend attached for the current mode.
export type YuriEvent = {
  type: string;
  id: string;
  ts: string;
  mission_id?: string | null;
  session_id?: string | null;
  agent_id?: string | null;
  project_id?: string | null;
  severity: string;
  speakable: boolean;
  payload?: Record<string, unknown>;
  narration?: string | null;
};

export type YuriContext = {
  // voice
  connected: boolean;
  muted: boolean;
  vstate: VoiceState;
  provider: VoiceProviderKind;
  model: string;
  connect: () => void;
  disconnect: () => void;
  toggleMute: () => void;
  setProvider: (p: VoiceProviderKind) => void;
  setModel: (m: string) => void;

  // conversation
  timeline: TimelineItem[];
  pending: Pending; // the live prompt card, or null

  // shared data the nav badges and every view read
  sessions: Sess[];
  approvals: Approval[];
  missions: Mission[];
  agents: Agent[];
  narrationMode: NarrationMode | null;
  setNarrationMode: (m: NarrationMode) => void;

  // streams
  debugEvents: DebugEvent[];
  onYuriEvent: (fn: (ev: YuriEvent) => void) => () => void; // subscribe; returns unsubscribe

  // actions
  callTool: (name: string, args: Record<string, unknown>) => Promise<unknown>;
  refresh: (what: "sessions" | "approvals" | "missions" | "agents") => Promise<void>;

  // --- everything below is beyond the minimal contract above, kept so
  // ConversationRail's UI (carried over from the old single-screen voice UI
  // this shell replaced) still has somewhere to read the connection's
  // finer-grained state from. A future view can ignore all of it and use
  // only the fields above. ---
  backend: ClaudeBackend;
  setBackend: (b: ClaudeBackend) => void;
  azureModels: { value: string; label: string }[];
  modelOptions: { value: string; label: string }[];
  status: string;
  modelLabel: string;
  voiceUsage: VoiceUsage | null;
  narrationBusy: boolean;
  orbRef: RefObject<HTMLDivElement | null>;
  glowRef: RefObject<HTMLDivElement | null>;
  modeBusy: string | null;
  switchMode: (handle: string, mode: string) => Promise<void>;
  commitRename: (handle: string, name: string) => Promise<void>;
  answerPrompt: (choice: string) => Promise<void>;
  clearPendingFor: (sessionId?: string) => void;
  pollSession: (sessionId: string) => void;
  clearDebugEvents: () => void;
};

const YuriCtx = createContext<YuriContext | null>(null);

export function useYuri(): YuriContext {
  const ctx = useContext(YuriCtx);
  if (!ctx) throw new Error("useYuri() must be called within a <VoiceProvider>");
  return ctx;
}

export function VoiceProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false);
  const [provider, setProvider] = useState<VoiceProviderKind>("gemini");
  const [backend, setBackend] = useState<ClaudeBackend>("cli");
  // The user's chosen model per provider; falls back to that provider's default.
  const [modelByProvider, setModelByProvider] = useState<Partial<Record<VoiceProviderKind, string>>>({});
  const [modelLabel, setModelLabel] = useState("");
  // How much Yuri narrates. Deliberately NOT persisted to localStorage: the
  // backend remembers it (settings row) and voice can change it mid-sentence,
  // so a second copy here could disagree with what she actually speaks. null
  // means "not read yet / backend unreachable" — the control stays clickable.
  const [narrationMode, setNarrationModeState] = useState<NarrationMode | null>(null);
  const [narrationBusy, setNarrationBusy] = useState(false);
  const [vstate, setVstate] = useState<VoiceState>("idle");
  const [muted, setMuted] = useState(false);
  const [status, setStatus] = useState("Tap connect and start talking.");
  const [timeline, setTimeline] = useState<TimelineItem[]>([]);
  // Monotonic id for tool rows so each expandable <details> keeps its open
  // state across re-renders even as old timeline items roll off the cap.
  const toolIdRef = useRef(0);
  const [sessions, setSessions] = useState<Sess[]>([]);
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [missions, setMissions] = useState<Mission[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [pending, setPending] = useState<Pending>(null);
  const [voiceUsage, setVoiceUsage] = useState<VoiceUsage | null>(null);
  // Pipeline activity log (voice<->backend<->Claude) from the backend SSE stream.
  const [debugEvents, setDebugEvents] = useState<DebugEvent[]>([]);

  const esRef = useRef<EventSource | null>(null);
  // The narration stream is a SECOND, separate subscription from the Activity
  // panel's /debug/stream above: different endpoint, different payload, and it
  // only lives while a voice session is connected.
  const narrationEsRef = useRef<EventSource | null>(null);
  // Held across reconnects on purpose — see createSpokenGate's comment. Built
  // lazily at first use, same reasoning as before the move: this provider
  // re-renders on every debug event and volume tick, and
  // useRef(createSpokenGate()) would allocate a gate on each one only to
  // throw it away.
  const narrationGateRef = useRef<SpokenGate | null>(null);
  // Views subscribed via onYuriEvent(). A Set, not an array, so repeated
  // subscribe/unsubscribe (e.g. a view remounting) never accumulates dupes.
  const listeners = useRef(new Set<(ev: YuriEvent) => void>());

  const sessionRef = useRef<VoiceSession | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // The orb's DOM nodes live in whichever view renders the hero section
  // (ConversationRail today); the refs themselves live here because the
  // analyser loop that drives them is part of the connection, not the view.
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
  // Cost-log connection identity & snapshot timer. connectionId persists for one
  // voice connect()/disconnect() cycle so snapshots can be grouped later.
  const costLogRef = useRef<{
    connectionId: string;
    startedAt: number;
    provider: VoiceProviderKind;
    backend: ClaudeBackend;
    model: string;
    voiceUsage: VoiceUsage | null;
    sessions: Sess[];
  } | null>(null);
  const costLogTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [modeBusy, setModeBusy] = useState<string | null>(null);

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
  const setModel = useCallback(
    (m: string) => setModelByProvider((prev) => ({ ...prev, [provider]: m })),
    [provider],
  );

  useEffect(
    () => () => {
      stopAnalyser();
      stopPolling();
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

  // Shared tool dispatch: every /api/tools/execute call in this file (and any
  // view) goes through here so the fetch shape lives in exactly one place.
  const callTool = useCallback(async (name: string, args: Record<string, unknown>): Promise<unknown> => {
    const r = await fetch("/api/tools/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ name, arguments: args }),
    });
    const data = await r.json();
    return data?.result;
  }, []);

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
        const res: any = await callTool("poll_session", { session_id: sessionId });
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
    const result: any = await callTool("list_sessions", {});
    return result?.sessions || [];
  };

  const refreshSessions = async () => {
    try {
      setSessions(await fetchSessions());
    } catch {
      /* ignore */
    }
  };

  // The other three shared lists. No poll for these — each is fetched once on
  // mount, and again on demand via refresh(). Unlike sessions (which carries
  // live turn state the event stream doesn't fully describe), these have
  // nothing driving them yet in this task; a later view can call refresh()
  // after an onYuriEvent it cares about, or on its own mount.
  const refreshApprovals = async () => {
    try {
      const r = await fetch("/api/yuri/approvals", { headers: authHeaders() });
      if (!r.ok) return;
      const d = await r.json();
      setApprovals(Array.isArray(d?.approvals) ? d.approvals : []);
    } catch {
      /* ignore */
    }
  };
  const refreshMissions = async () => {
    try {
      const r = await fetch("/api/yuri/missions", { headers: authHeaders() });
      if (!r.ok) return;
      const d = await r.json();
      setMissions(Array.isArray(d?.missions) ? d.missions : []);
    } catch {
      /* ignore */
    }
  };
  const refreshAgents = async () => {
    try {
      const r = await fetch("/api/yuri/agents", { headers: authHeaders() });
      if (!r.ok) return;
      const d = await r.json();
      setAgents(Array.isArray(d?.agents) ? d.agents : []);
    } catch {
      /* ignore */
    }
  };

  const refresh = useCallback(async (what: "sessions" | "approvals" | "missions" | "agents") => {
    if (what === "sessions") return refreshSessions();
    if (what === "approvals") return refreshApprovals();
    if (what === "missions") return refreshMissions();
    return refreshAgents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    refreshApprovals();
    refreshMissions();
    refreshAgents();
  }, []);

  // Read the server's narration mode. The backend is the single source of
  // truth, so a failure (it answers 503 while the home dir is unavailable)
  // leaves the last known value on screen rather than blanking the control —
  // and the buttons still work, because a PUT sets the mode outright.
  const refreshNarrationMode = useCallback(async () => {
    try {
      const d = await yget<{ mode?: string }>("narration");
      if (isNarrationMode(d?.mode)) setNarrationModeState(d.mode);
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

  const setNarrationMode = async (mode: NarrationMode) => {
    const prev = narrationMode;
    setNarrationModeState(mode); // optimistic, like switchMode; reconciled below
    setNarrationBusy(true);
    try {
      const d = await yput<{ mode?: string }>("narration", { mode });
      if (isNarrationMode(d?.mode)) setNarrationModeState(d.mode);
    } catch (err: any) {
      setNarrationModeState(prev); // never show a mode the backend didn't accept
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

  // Subscribe; returns an unsubscribe. A Set so re-subscribing (e.g. a view
  // remounting) can never register the same listener twice.
  const onYuriEvent = useCallback((fn: (ev: YuriEvent) => void) => {
    listeners.current.add(fn);
    return () => {
      listeners.current.delete(fn);
    };
  }, []);

  // The Yuri events stream: mission-level narration AND every view's
  // onYuriEvent fan-out ride the same connection. The poll loop owns the
  // session-turn events, this stream owns mission state and lost contact (the
  // backend's yuri/narration/policy.py decides which, and sends
  // narration:null on the carrier that doesn't own an event — so subscribing
  // to both cannot double-speak).
  //
  // The subscription itself is NOT gated on the voice session being
  // connected — every routed view depends on onYuriEvent to know when its
  // list is stale (nav badges, Approvals refreshing on approval.*, and so
  // on), and that has to work with the mic off. Only SPEAKING a line is
  // gated: injectUpdate would push into a closed voice session, which is
  // pointless (there is nothing to inject into) rather than merely wasteful.
  // connectedRef (kept current by the effect below) is read at delivery
  // time, not captured at subscribe time, so a connect/disconnect mid-stream
  // takes effect on the very next frame without resubscribing.
  //
  // Mount-only ([] deps): the subscription no longer depends on `connected`,
  // so re-running it on every connect/disconnect would just tear down and
  // reopen an EventSource that has no reason to move, and would re-run the
  // seed fetch for no benefit. gate.lineFor still runs on every frame
  // regardless of connected — see the comment at that call — which is what
  // keeps a later connect from re-speaking whatever piled up while
  // disconnected.
  useEffect(() => {
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
          // Capture newness BEFORE lineFor runs: lineFor remembers the id as a
          // side effect, so calling hasSeen after would always report "seen".
          // This is the real replay signal — narration:null (unspeakable in
          // this mode, or an event type with no line at all, e.g.
          // mission.status_changed) is NOT the same thing as "already
          // delivered", so `line !== null` cannot stand in for it.
          const isNew = !gate.hasSeen(frame);
          // Called unconditionally — even while disconnected — because the
          // gate must keep consuming ids the whole time. If this were
          // skipped while disconnected, connecting later would replay-speak
          // everything that happened in between; consuming here (and simply
          // not acting on the result) is what keeps a later connect silent
          // about the backlog, the same "replay is silent" property the seed
          // logic gives the very first subscription.
          const line = gate.lineFor(frame);
          if (line && connectedRef.current) {
            // Always false today (the blocking types are poll-owned, so they
            // carry no line here) — passed anyway so both carriers apply the
            // one rule if ownership ever moves.
            sessionRef.current?.injectUpdate(line, { blocking: isBlockingNarration(frame) });
            logDebug("inject", line, undefined, "backend", "voice");
          }
          // Fan the event out to every view listener, but ONLY if the gate
          // above had never seen its id before — a reconnect's replay must
          // not repeat an event to subscribers, even one with no narration
          // line. Each call is wrapped: mirrors ClaudeRunner._notify on the
          // backend — a bug in one view's handler must not kill the stream
          // for the others.
          if (isNew) {
            listeners.current.forEach((fn) => {
              try {
                fn(frame as YuriEvent);
              } catch {
                /* a view bug must not break the stream */
              }
            });
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
  }, []);

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
    let yuriCtx: YuriConnectSnapshot | null = null;
    try {
      const r = await fetch("/api/yuri/context", { headers: authHeaders() });
      if (r.ok) yuriCtx = (await r.json()) as YuriConnectSnapshot;
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
  const switchMode = async (handle: string, mode: string) => {
    setModeBusy(handle);
    // Optimistic: reflect the target immediately, then reconcile from the backend.
    setSessions((prev) => prev.map((s) => (s.handle === handle ? { ...s, mode } : s)));
    try {
      const res: any = await callTool("set_mode", { session_id: handle, mode });
      // Mirror the voice set_mode path: if the switch auto-approved a pending
      // permission, clear the stale card and resume polling for the resumed turn.
      if (res?.prompt_resolved) {
        clearPendingFor(handle);
        pollSession(handle);
      }
    } finally {
      await refreshSessions();
      setModeBusy(null);
    }
  };

  // Inline session rename (voice "call this one X" also works). `name` is the
  // caller's draft text — the input value itself is view-local UI state.
  const commitRename = async (handle: string, name: string) => {
    const trimmed = name.trim();
    const prev = sessions.find((s) => s.handle === handle)?.name || "";
    if (!trimmed || trimmed === prev) return;
    setSessions((p) => p.map((s) => (s.handle === handle ? { ...s, name: trimmed } : s)));
    try {
      await callTool("rename_session", { session_id: handle, name: trimmed });
    } finally {
      refreshSessions(); // reconcile (e.g. name clash rejected on the server)
    }
  };

  // Manual fallback for answering a pending prompt by click (voice also works).
  const answerPrompt = async (choice: string) => {
    if (!pending) return;
    const p = pending;
    clearPendingFor(p.sessionId);
    await callTool("answer_prompt", { session_id: p.sessionId, choice });
    pollSession(p.sessionId); // Claude resumes in the background; narrate the result
    refreshSessions();
  };

  // Memoised so a consumer that reads only a slice of this (e.g. a future
  // nav badge reading just approvals.length) doesn't re-render on every
  // /debug/stream frame, transcript delta or 2s session poll -- all of which
  // otherwise re-render this provider and, without this, would hand out a
  // brand-new `value` object every single time regardless.
  //
  // The dependency list is deliberately complete rather than trimmed for
  // convenience: it includes every non-setState-setter, non-ref identifier
  // the object below actually reads, INCLUDING several action functions
  // (connect, disconnect, toggleMute, setNarrationMode, switchMode,
  // commitRename, answerPrompt, clearPendingFor, pollSession) that are plain
  // closures redefined on every render rather than useCallback-wrapped. That
  // means this memo does not yet stop those renders from producing a new
  // `value` -- being honest about the dependency list surfaces that
  // limitation instead of hiding it by omitting them. Stabilising those
  // functions (and the cost-log/audio-analyser machinery some of them close
  // over) via useCallback, or extracting them into their own hooks, would be
  // the natural follow-up but is a larger, separate change.
  //
  // Omitted on purpose, both provably stable for the component's lifetime:
  // setProvider/setBackend (raw useState setters -- a React guarantee) and
  // orbRef/glowRef (ref objects never change identity).
  const value: YuriContext = useMemo(
    () => ({
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
      approvals,
      missions,
      agents,
      narrationMode,
      setNarrationMode,

      debugEvents,
      onYuriEvent,

      callTool,
      refresh,

      backend,
      setBackend,
      azureModels,
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
      clearPendingFor,
      pollSession,
      clearDebugEvents: () => setDebugEvents([]),
    }),
    [
      connected,
      muted,
      vstate,
      provider,
      model,
      connect,
      disconnect,
      toggleMute,
      setModel,
      timeline,
      pending,
      sessions,
      approvals,
      missions,
      agents,
      narrationMode,
      setNarrationMode,
      debugEvents,
      onYuriEvent,
      callTool,
      refresh,
      backend,
      azureModels,
      modelOptions,
      status,
      modelLabel,
      voiceUsage,
      narrationBusy,
      modeBusy,
      switchMode,
      commitRename,
      answerPrompt,
      clearPendingFor,
      pollSession,
    ],
  );

  return (
    <YuriCtx.Provider value={value}>
      {children}
      <audio ref={audioRef} hidden />
    </YuriCtx.Provider>
  );
}
