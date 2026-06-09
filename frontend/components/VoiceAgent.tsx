"use client";

import { useCallback, useEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { RealtimeSession } from "@/lib/realtime";
import { GeminiSession } from "@/lib/gemini";
import { ClaudeBackend, RealtimeEvent, RealtimeOptions, VoiceProvider, VoiceSession, VoiceState, VoiceUsage } from "@/lib/voice";
import { INSTRUCTIONS } from "@/lib/instructions";
import { authHeaders, withAuthParam } from "@/lib/auth";
import { scopedClearPending } from "@/lib/promptState";
import LiveTerminal from "./LiveTerminal";
import { Icon } from "./ui/Icon";

// One ordered list of bubbles + tool rows so the live "Conversation" panel renders
// tool calls inline with the surrounding turns instead of piling them at the end.
type TimelineItem =
  | { kind: "turn"; role: "user" | "assistant"; text: string; final: boolean }
  | { kind: "tool"; id: number; name: string; ok?: boolean; args?: unknown; result?: unknown };

// A plan-approval prompt carries the plan markdown after this marker (set in
// the backend's _summarize_tool); split it off so the card can render it
// formatted instead of as one raw blob.
function splitPlan(text: string): { lead: string; plan: string | null } {
  const i = text.indexOf("The full plan follows");
  if (i < 0) return { lead: text, plan: null };
  const nl = text.indexOf("\n", i);
  return {
    lead: text.slice(0, i).replace(/[—.\s]+$/, ""),
    plan: nl < 0 ? null : text.slice(nl).trim(),
  };
}

// Minimal markdown rendering (headings, lists, bold, inline code, fences) as
// React elements — no innerHTML, so prompt content can't inject markup.
function MarkdownLite({ md }: { md: string }) {
  let key = 0;
  const inline = (s: string): ReactNode[] => {
    const nodes: ReactNode[] = [];
    const re = /(`[^`]+`|\*\*[^*]+\*\*)/g;
    let last = 0;
    for (let m = re.exec(s); m; m = re.exec(s)) {
      if (m.index > last) nodes.push(s.slice(last, m.index));
      const t = m[0];
      nodes.push(t.startsWith("`") ? <code key={key++}>{t.slice(1, -1)}</code> : <b key={key++}>{t.slice(2, -2)}</b>);
      last = m.index + t.length;
    }
    if (last < s.length) nodes.push(s.slice(last));
    return nodes;
  };
  const out: ReactElement[] = [];
  const lines = md.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.startsWith("```")) {
      const buf: string[] = [];
      while (++i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i]);
      out.push(<pre key={key++}>{buf.join("\n")}</pre>);
      continue;
    }
    const h = l.match(/^(#{1,4})\s+(.*)/);
    if (h) {
      out.push(<div key={key++} className={`mdh mdh${h[1].length}`}>{inline(h[2])}</div>);
      continue;
    }
    const li = l.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (li) {
      out.push(<div key={key++} className="mdli"><span className="mdb">{li[1] === "-" || li[1] === "*" ? "•" : li[1]}</span>{inline(li[2])}</div>);
      continue;
    }
    out.push(l.trim() ? <div key={key++} className="mdp">{inline(l)}</div> : <div key={key++} className="mdgap" />);
  }
  return <div className="planmd">{out}</div>;
}

// Pretty-print a tool call's input/output for the expandable detail view.
// Strings pass through; objects are JSON-formatted; nullish renders as a dash.
function fmtPayload(v: unknown): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "string") return v;
  try {
    return JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

// The backend stamps activity-log timestamps as UTC ISO strings ("…Z"). Show
// them in the viewer's own timezone: parse to a Date and format a compact local
// clock (HH:MM:SS.mmm). Falls back to the raw UTC time slice if unparseable.
function fmtLogTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts.slice(11, 23);
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

// Full local date-time (incl. timezone) for the timestamp's hover title, so the
// date — omitted from the compact row — is still available on demand.
function fmtLogTimeTitle(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "long" });
}

type ToolItem = Extract<TimelineItem, { kind: "tool" }>;

// A flat object (all primitive values) renders as an aligned key/value grid;
// anything nested falls back to a JSON code block.
function isFlatObject(v: unknown): v is Record<string, unknown> {
  if (!v || typeof v !== "object" || Array.isArray(v)) return false;
  return Object.values(v as Record<string, unknown>).every(
    (x) => x === null || ["string", "number", "boolean"].includes(typeof x),
  );
}

const clip = (s: string, n = 90) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

// Done / working / error — drives the status dot and accent. `working` is the
// transient state tell_claude & friends return before the real result polls in.
function toolState(item: ToolItem): "done" | "working" | "error" {
  if (item.ok === false) return "error";
  const status = (item.result as { status?: string } | undefined)?.status;
  if (status === "working") return "working";
  if (status === "error") return "error";
  return "done";
}

// A short, human-readable gloss of what the call actually did, so the row reads
// like an action ("told Claude to…", "mode → auto") instead of bare jargon.
function toolSummary(name: string, args: unknown, result: unknown): string {
  const a = (args ?? {}) as Record<string, any>;
  const r = (result ?? {}) as Record<string, any>;
  switch (name) {
    case "tell_claude":
      return a.message ? clip(String(a.message)) : "";
    case "answer_prompt":
      return a.choice ? `“${clip(String(a.choice), 60)}”` : "";
    case "run_slash_command":
      return String(r.sent || `/${a.command ?? ""}${a.args ? " " + a.args : ""}`).trim();
    case "set_mode":
      return r.mode || a.mode ? `mode → ${r.mode || a.mode}` : "";
    case "rename_session":
      return r.name ? `→ ${r.name}` : a.name || "";
    case "start_session":
      return r.name ? `${r.name}${r.project_path ? " · " + String(r.project_path).split("/").pop() : ""}` : "";
    case "list_sessions":
      return Array.isArray(r.sessions) ? `${r.sessions.length} session${r.sessions.length === 1 ? "" : "s"}` : "";
    case "list_projects":
      return Array.isArray(r.projects) ? `${r.projects.length} projects` : "";
  }
  if (typeof r.message === "string") return clip(r.message);
  if (typeof r.status === "string") return r.status;
  return "";
}

function PayloadView({ value }: { value: unknown }) {
  if (value === undefined || value === null || value === "") return <div className="tc-empty">—</div>;
  if (isFlatObject(value)) {
    const entries = Object.entries(value).filter(([, v]) => v !== undefined && v !== "");
    if (entries.length === 0) return <div className="tc-empty">—</div>;
    return (
      <dl className="tc-kv">
        {entries.map(([k, v]) => (
          <div className="tc-kv-row" key={k}>
            <dt>{k}</dt>
            <dd>{typeof v === "string" ? v : String(v)}</dd>
          </div>
        ))}
      </dl>
    );
  }
  return <pre className="tc-code">{fmtPayload(value)}</pre>;
}

// One tool call as an expandable inline "action card": collapsed shows a status
// dot, the mono tool name, and a human summary; expanded reveals structured
// input/output.
function ToolCall({
  item,
  variant = "card",
  defaultOpen = false,
}: {
  item: ToolItem;
  variant?: "card" | "line";
  defaultOpen?: boolean;
}) {
  const state = toolState(item);
  const summary = toolSummary(item.name, item.args, item.result);
  // Isolated calls open by default so their input/output is visible at a glance
  // (controlled so the user can still collapse them and it survives re-renders).
  const [open, setOpen] = useState(defaultOpen);
  return (
    <details
      className={`tcall ${variant} ${state}`}
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary>
        <span className={`tc-dot ${state}`} aria-hidden />
        <span className="tc-name">{item.name}</span>
        {summary && <span className="tc-summary">{summary}</span>}
        <Icon name="chevron-down" size={13} strokeWidth={1.5} className="tc-chev" />
      </summary>
      <div className="tc-body">
        <section className="tc-sec">
          <div className="tc-label">Input</div>
          <PayloadView value={item.args} />
        </section>
        <section className="tc-sec">
          <div className="tc-label">Output</div>
          <PayloadView value={item.result} />
        </section>
      </div>
    </details>
  );
}

// Render the conversation timeline, grouping runs of consecutive tool calls.
// An isolated call renders as a full card; a run of 2+ condenses into light
// lines inside one grouped container, so a burst of actions reads as a single
// tidy block instead of a stack of heavy boxes. Each line stays independently
// expandable (native <details>, keyed by stable id so open state survives
// re-renders and the timeline cap).
function renderConversation(items: TimelineItem[]): ReactElement[] {
  const nodes: ReactElement[] = [];
  let i = 0;
  while (i < items.length) {
    const item = items[i];
    if (item.kind === "turn") {
      nodes.push(
        <div key={`turn-${i}`} className={`bubble ${item.role}`}>
          <div className="who">{item.role === "user" ? "You" : "Assistant"}</div>
          {item.text}
        </div>,
      );
      i++;
      continue;
    }
    // Collect the run of consecutive tool calls starting here.
    const run: ToolItem[] = [];
    while (i < items.length && items[i].kind === "tool") {
      run.push(items[i] as ToolItem);
      i++;
    }
    if (run.length === 1) {
      nodes.push(<ToolCall key={`tool-${run[0].id}`} item={run[0]} variant="card" />);
    } else {
      nodes.push(
        <div key={`tgroup-${run[0].id}`} className="tcall-group">
          {run.map((t) => (
            <ToolCall key={`tool-${t.id}`} item={t} variant="line" />
          ))}
        </div>,
      );
    }
  }
  return nodes;
}
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
  // The live pending prompt when status is needs_permission/needs_choice —
  // lets an agent that connected after the prompt fired still see it in full.
  prompt?: { kind: string; text: string; options?: string[] };
};

// Abbreviate the user's home dir to ~ for a compact path display.
function abbrevHome(path: string): string {
  return path.replace(/^\/(Users|home)\/[^/]+/, "~");
}

// Headline status for a session's status strip: a dot/accent class, a one-word
// lead, and the current-task line — derived from the live work-pipeline so the
// panel answers "what is it doing right now?" at a glance.
function sessionStatus(s: Sess): { cls: string; lead: string; task: string } {
  const running = s.queue?.find((q) => q.state === "running")?.text;
  if (s.status === "needs_permission" || s.status === "needs_choice")
    return { cls: "attn", lead: "Needs you", task: "Waiting for your approval" };
  if (s.status === "error")
    return { cls: "error", lead: "Error", task: "The last turn ran into an error" };
  if (s.running)
    return { cls: "working", lead: "Working", task: running || "Running a task…" };
  return { cls: "ready", lead: "Ready", task: "Waiting for your next instruction" };
}

// Small copy-to-clipboard button with transient ✓ feedback.
function CopyBtn({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className={`copybtn ${done ? "done" : ""}`}
      title={done ? "Copied" : "Copy"}
      aria-label={done ? "Copied" : "Copy"}
      onClick={() => {
        navigator.clipboard?.writeText(text).catch(() => undefined);
        setDone(true);
        setTimeout(() => setDone(false), 1100);
      }}
    >
      {done ? (
        <Icon name="check" size={14} strokeWidth={2.5} />
      ) : (
        <Icon name="copy" size={14} />
      )}
    </button>
  );
}

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

// Realtime models the user can pick per provider, best/most-capable first.
// Azure's list is dynamic: its "models" are the server-side deployment names
// (AZURE_OPENAI_DEPLOYMENTS env), fetched from /api/voice/models at mount —
// the static entry stays empty so no dropdown shows until they load.
const MODEL_OPTIONS: Record<VoiceProvider, { value: string; label: string }[]> = {
  azure: [],
  openai: [
    { value: "gpt-realtime-2", label: "gpt-realtime-2 · most capable" },
    { value: "gpt-realtime-1.5", label: "gpt-realtime-1.5 · best audio" },
    { value: "gpt-realtime-mini", label: "gpt-realtime-mini · economy" },
  ],
  gemini: [
    { value: "gemini-3.1-flash-live-preview", label: "Gemini 3.1 Flash Live · best" },
    { value: "gemini-2.5-flash-native-audio-preview-12-2025", label: "Gemini 2.5 Native Audio" },
  ],
};

// Per-provider connection params. `model` is the user's dropdown choice.
function connectionParams(provider: VoiceProvider, model: string): Partial<RealtimeOptions> {
  if (provider === "gemini") {
    return { provider: "gemini", model, voice: "Kore" };
  }
  if (provider === "azure") {
    // Azure-hosted OpenAI realtime. `model` is an Azure *deployment* name from
    // /api/voice/models; the backend only honors allowlisted names and falls
    // back to its default deployment otherwise (e.g. empty before the fetch).
    return { provider: "azure", model: model || undefined, voice: "marin" };
  }
  // OpenAI direct — the "native" option, kept switchable alongside Azure.
  return { provider: "openai", model, voice: "marin" };
}

// "azure" and "openai" are the same engine family reached over different
// routes (Azure-hosted deployment vs OpenAI direct) — the UI presents them as
// OpenAI with a route sub-choice, not as separate top-level providers.
const PROVIDER_LABEL: Record<VoiceProvider, string> = {
  azure: "OpenAI · Azure",
  openai: "OpenAI",
  gemini: "Gemini",
};

const BACKEND_LABEL: Record<ClaudeBackend, string> = {
  cli: "CLI",
  sdk: "SDK",
};

// A calm, coarse caption for the orb. The orb's volume animation conveys the
// moment-to-moment activity, so we deliberately collapse listening/hearing/
// speaking into one steady "Listening" label instead of churning the words.
function orbCaption(connected: boolean, muted: boolean, vstate: VoiceState): string {
  if (!connected || vstate === "idle") return "Offline";
  if (muted) return "Muted";
  if (vstate === "connecting") return "Connecting…";
  if (vstate === "thinking") return "Thinking…"; // keep this one — it's a genuine longer pause
  return "Listening";
}

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
        setAttachOpen(false);
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

  const clearPendingFor = (sessionId?: string) =>
    setPending((p) => scopedClearPending(p, sessionId));

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
      clearPendingFor(sid);
      const txt = (res.assistant_text || "").trim();
      const msg = `[Claude update] Claude finished${forReq}. ${txt ? `It said: ${txt}` : "Done."} This is the latest result — summarize it briefly for the user, and do NOT say this request is still in progress.`;
      sessionRef.current?.injectUpdate(msg);
      logDebug("inject", msg, { session: sid }, "backend", "voice");
    } else if (res.status === "error") {
      clearPendingFor(sid);
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
        ? `- Open Claude sessions (reuse these for matching work instead of starting new ones):\n${lines.join("\n")}`
        : "- Open Claude sessions: none — call start_session before any work.",
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
    const params = connectionParams(provider, model);
    const opts: RealtimeOptions = {
      ...params,
      instructions: INSTRUCTIONS + dynamicContext(snapshot),
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
              {renderConversation(timeline)}
            </div>
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
              const st = sessionStatus(s);
              const queuedTurns = (s.queue || []).filter((q) => q.state === "queued");
              return (
                <div key={s.handle} className="sess">
                  <div className="shead">
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
                        {s.name || s.cwd.split("/").pop()}
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

                  {liveSession === s.handle && !liveFullscreen && (
                    <div className="liveterm-box">
                      <div className="liveterm-bar">
                        <button
                          className="ltbtn"
                          title="Hide the live view (the session keeps running)"
                          aria-label="Minimize live view"
                          onClick={() => {
                            setLiveSession(null);
                            setLiveFullscreen(false);
                          }}
                        >
                          <Icon name="close" size={14} />
                        </button>
                        <span className="ltbar-title">Live CLI</span>
                        <button
                          className="ltbtn right"
                          title="Full screen"
                          aria-label="Full screen live view"
                          onClick={() => setLiveFullscreen(true)}
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

                  <div className="moderow">
                    <span className="modelbl">Mode</span>
                    <div className="modeseg" role="group" aria-label="Permission mode">
                      {MODES.map((m) => (
                        <button
                          key={m.id}
                          className={(s.mode || "default") === m.id ? "on" : ""}
                          title={m.title}
                          disabled={modeBusy === s.handle}
                          onClick={() => switchMode(s.handle, m.id)}
                        >
                          {m.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  <div className="actionrow">
                    {s.backend === "cli" && liveSession !== s.handle && (
                      <button
                        className="txtoggle primary"
                        title="Watch the live CLI in your browser"
                        onClick={() => setLiveSession(s.handle)}
                      >
                        <Icon name="play" size={13} /> Watch live
                      </button>
                    )}
                    <button className="txtoggle" onClick={() => toggleTranscript(s.handle)}>
                      {open ? "Hide" : "Transcript"}
                    </button>
                    {open && (
                      <button className="txtoggle" title="Expand" onClick={() => setFullscreen(true)}>
                        <Icon name="fullscreen" size={13} />
                      </button>
                    )}
                  </div>

                  {(tmuxCmd || cmd) && (
                    <details className="handoff">
                      <summary>
                        <span className="chev"><Icon name="chevron-right" size={10} /></span> Continue in your terminal
                      </summary>
                      {tmuxCmd && (
                        <div className="hopt">
                          <div className="htitle">Take the keyboard</div>
                          <div className="hwhy">Jump into this live session in your own terminal.</div>
                          <div className="hcmd">
                            <code>{tmuxCmd}</code>
                            <CopyBtn text={tmuxCmd} />
                          </div>
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
                <span className="lt" title={fmtLogTimeTitle(ev.ts)}>{fmtLogTime(ev.ts)}</span>
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
                <span>Live Claude CLI</span>
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
