// Session-list types and the pure helpers that derive a session's headline
// status and the mode/backend label maps — kept out of the component so they
// are reachable from `node --test`.
import type { ClaudeBackend } from "./voice.ts";

export type Sess = {
  handle: string;
  session_id: string | null;
  cwd: string;
  model: string;
  status: string;
  cost_usd?: number;
  backend?: string;
  mode?: string;
  agent_id?: string;
  agent_name?: string;
  // What this session's provider actually supports. The panel used to assume
  // every session was Claude Code: it rendered a permission-mode switcher for
  // OpenCode (which has no modes, so every click failed) and built its own
  // `claude --resume` line for a session Claude had never heard of.
  supports_modes?: boolean;
  resume_command?: string | null;
  // Whether a live terminal view can be offered for THIS session. Not the
  // same as backend === "cli": OpenCode's session lives in a server, and its
  // view is an `opencode attach` pane created on demand.
  can_watch?: boolean;
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

// Headline status for a session's status strip: a dot/accent class, a one-word
// lead, and the current-task line — derived from the live work-pipeline so the
// panel answers "what is it doing right now?" at a glance.
export function sessionStatus(s: Sess): { cls: string; lead: string; task: string } {
  const running = s.queue?.find((q) => q.state === "running")?.text;
  if (s.status === "needs_permission" || s.status === "needs_choice")
    return { cls: "attn", lead: "Needs you", task: "Waiting for your approval" };
  if (s.status === "error")
    return { cls: "error", lead: "Error", task: "The last turn ran into an error" };
  if (s.running)
    return { cls: "working", lead: "Working", task: running || "Running a task…" };
  return { cls: "ready", lead: "Ready", task: "Waiting for your next instruction" };
}

export const MODES: { id: string; label: string; title: string }[] = [
  { id: "default", label: "Normal", title: "Asks before risky actions; approve/deny by voice" },
  { id: "plan", label: "Plan", title: "Only plans — makes no edits or commands" },
  { id: "acceptEdits", label: "Accept Edits", title: "File edits auto-apply; other risky actions still asked" },
  { id: "auto", label: "Auto", title: "Runs everything without asking" },
];
export const MODE_LABEL: Record<string, string> = Object.fromEntries(MODES.map((m) => [m.id, m.label]));

export const BACKEND_LABEL: Record<ClaudeBackend, string> = {
  cli: "CLI",
  sdk: "SDK",
};

// The best available label for a session, so the same one never shows five
// different ways across Dashboard/Sessions/Terminal/Mission detail: an
// explicit name if the user (or Yuri) set one, else the session's own folder
// (never the parent path, just its basename), else a short handle prefix —
// never the raw handle/session id in full. Structurally typed rather than
// `Sess`-only so callers holding a differently-shaped session record (e.g.
// MissionDetail's AgentSession, keyed by `id`/`native_session_id`/
// `working_directory`) can adapt with an object literal instead of this file
// growing a second, near-identical helper.
export function sessionLabel(s: { name?: string | null; cwd: string; handle: string }): string {
  if (s.name) return s.name;
  const base = s.cwd.split("/").filter(Boolean).pop();
  if (base) return base;
  return s.handle.slice(0, 8);
}
