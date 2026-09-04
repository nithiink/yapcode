// The workflow timeline's logic: what order the steps go in, what each status
// looks like, and which controls a step may actually offer.
//
// Pure so `node --test` can reach it. Two requirements shape it:
//
//   * ORDER MUST BE STABLE across the poll. The timeline re-renders every
//     couple of seconds; an order that depends on anything but the graph
//     makes rows jump under the cursor. That is the `dockTabs` lesson.
//   * A CONTROL THAT WOULD FAIL IS NOT RENDERED (docs/yuri/design/GUIDE.md
//     §6). Each gate below mirrors the precondition the backend enforces, so
//     the UI cannot offer a button that answers 409.

/** yuri/domain/task.py's ten statuses. */
export const TASK_STATUSES = ["pending", "ready", "dispatched", "running",
                              "waiting_approval", "verifying", "completed",
                              "skipped", "failed", "blocked", "cancelled"] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

export type Task = {
  id: string;
  title: string;
  ordinal: number;
  status: string;
  role?: string | null;
  specialist_id?: string | null;
  attempts?: number;
  max_attempts?: number;
  error?: string | null;
  read_only?: boolean;
  verification?: string[];
  started_at?: string | null;
  ended_at?: string | null;
  result?: { verification?: Verdict[]; assistant_text?: string } | null;
};

export type Verdict = { check: string; verdict: string; detail?: string };

export type Deps = Record<string, string[]>;

/** Dependency order, ties broken by `ordinal` (the authoring order).
 *
 *  Kahn's algorithm with a sorted frontier, so the result depends ONLY on the
 *  graph and the ordinals — never on object identity, insertion order or the
 *  order the API happened to return rows in. That is what makes it stable
 *  across the poll.
 *
 *  A cycle cannot be created through the API (the backend rejects one at
 *  create time), but if one ever reached here the remaining tasks are
 *  appended in ordinal order rather than dropped or looped over: showing the
 *  user a step out of order beats showing them nothing, and beats hanging the
 *  tab. */
export function timelineOrder(tasks: Task[], deps: Deps = {}): Task[] {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const remaining = new Map<string, Set<string>>();
  for (const t of tasks) {
    // Only edges to tasks we actually have: an edge to something outside this
    // workflow must not stall the whole list.
    remaining.set(t.id, new Set((deps[t.id] || []).filter((d) => byId.has(d))));
  }
  const byOrdinal = (a: Task, b: Task) =>
    a.ordinal - b.ordinal || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

  const out: Task[] = [];
  const placed = new Set<string>();
  while (out.length < tasks.length) {
    const free = tasks
      .filter((t) => !placed.has(t.id) && (remaining.get(t.id)?.size ?? 0) === 0)
      .sort(byOrdinal);
    if (free.length === 0) {
      // A cycle, or an unsatisfiable edge. Append the rest in a defined order
      // and stop — never loop.
      const rest = tasks.filter((t) => !placed.has(t.id)).sort(byOrdinal);
      out.push(...rest);
      break;
    }
    for (const t of free) {
      out.push(t);
      placed.add(t.id);
    }
    for (const set of remaining.values()) {
      for (const id of placed) set.delete(id);
    }
  }
  return out;
}

/** One token class per status. `.tk-<status>` in globals.css; a test asserts
 *  every status has one, because a status with no class renders unstyled and
 *  reads as "no state at all". */
export function taskClass(status: string): string {
  return `tk-${TASK_STATUSES.includes(status as TaskStatus) ? status : "pending"}`;
}

/** What a status means, in words. The status name alone is jargon:
 *  "dispatched" and "running" look like the same thing to a reader, and
 *  "verifying" looks like a warning when it is routine. */
export const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: "Waiting on earlier steps",
  ready: "Ready to start",
  dispatched: "Starting",
  running: "Working",
  waiting_approval: "Waiting for you",
  verifying: "Checking the work",
  completed: "Done",
  skipped: "Skipped",
  failed: "Failed",
  blocked: "Needs you",
  cancelled: "Cancelled",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status as TaskStatus] || status;
}

/** A verdict, in words. `unavailable` NEVER renders as a tick: the check could
 *  not run, which is not the same as passing, and the backend does not count
 *  it as one either. */
export function verdictLabel(v: Verdict): { tone: "good" | "bad" | "unknown"; text: string } {
  if (v.verdict === "pass") return { tone: "good", text: `${v.check} passed` };
  if (v.verdict === "fail") {
    return { tone: "bad", text: `${v.check} failed${v.detail ? `: ${v.detail}` : ""}` };
  }
  return {
    tone: "unknown",
    text: `${v.check} could not run${v.detail ? `: ${v.detail}` : ""}`,
  };
}

/** What to say about a declared check that produced no verdict.
 *
 *  "not checked yet" is right for a step still to run and WRONG for a skipped
 *  one, where the check will never run at all — and that is the fact the user
 *  most needs after skipping a test or a review. Same rule as everywhere
 *  else: never let two different situations read the same. */
export function pendingCheckLabel(t: Task, check: string): { tone: "unknown" | "pending"; text: string } {
  if (t.status === "skipped" || t.status === "cancelled") {
    return { tone: "unknown", text: `${check} never ran` };
  }
  return { tone: "pending", text: `${check} not checked yet` };
}

/** The verdicts a task kept, if any. Read off `result` rather than the event
 *  stream so they survive a reload. */
export function verdictsOf(t: Task): Verdict[] {
  const kept = t.result?.verification;
  return Array.isArray(kept) ? kept : [];
}

/** Each mirrors the backend's own precondition, named here so the gate and
 *  the reason live together:
 *
 *  retry  — WorkflowEngine.retry: failed or blocked only.
 *  skip   — skip() has no path from running/verifying/waiting_approval; an
 *           agent is mid-turn and there is nothing to drop.
 *  assign — ASSIGNABLE: pending, ready, failed, blocked. A pin after dispatch
 *           would not change who is already working. */
export function canRetry(t: Task): boolean {
  return t.status === "failed" || t.status === "blocked";
}

export function canSkip(t: Task): boolean {
  return ["pending", "ready", "dispatched", "failed", "blocked"].includes(t.status);
}

export function canAssign(t: Task): boolean {
  return ["pending", "ready", "failed", "blocked"].includes(t.status);
}

export const TERMINAL_TASK = ["completed", "skipped", "cancelled"];

export function isTerminal(t: Task): boolean {
  return TERMINAL_TASK.includes(t.status);
}

/** Progress, for a one-line summary above the list. Counts what is DONE
 *  against the total, and says separately whether anything needs the user —
 *  "3 of 4" hides a blocked step, which is the one thing worth surfacing. */
export function progressOf(tasks: Task[]): { done: number; total: number; needsYou: number } {
  return {
    done: tasks.filter((t) => isTerminal(t)).length,
    total: tasks.length,
    needsYou: tasks.filter((t) => ["failed", "blocked", "waiting_approval"].includes(t.status))
      .length,
  };
}

/** How long a step took, or has been going. Returns "" when the task has not
 *  started — never "0s", which reads as "took no time" rather than "has not
 *  begun". */
export function durationOf(t: Task, now: number = Date.now()): string {
  if (!t.started_at) return "";
  const start = Date.parse(t.started_at);
  if (Number.isNaN(start)) return "";
  const end = t.ended_at ? Date.parse(t.ended_at) : now;
  const secs = Math.max(0, Math.round(((Number.isNaN(end) ? now : end) - start) / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${secs % 60}s`;
  return `${Math.floor(mins / 60)}h ${mins % 60}m`;
}
