// Pure helpers for the session dock: which sessions get a tab, in what order,
// and what colour identifies each one.
import type { Sess } from "./sessions.ts";

// Provider identity lives on the tabs and the composer chip — never on the
// orb, which is always Yuri. A provider we have not seen falls back to her
// accent rather than to an invented colour.
const AGENT_HUE: Record<string, string> = {
  "claude-code": "#9cc7a4",
  opencode: "#93a6c9",
};
export const FALLBACK_HUE = "#dd8a6a";

export function agentHue(agentId?: string | null): string {
  return (agentId && AGENT_HUE[agentId]) || FALLBACK_HUE;
}

/** Tab order. Anything needing a decision comes first, then work in flight,
 *  then the rest — so the tab the user has to act on is never the one that
 *  scrolled out of the strip. Ties keep the incoming order, which is the
 *  backend's (newest last), so tabs do not shuffle under the cursor on every
 *  2.5s poll. */
const RANK: Record<string, number> = {
  needs_permission: 0, needs_choice: 0, error: 1, running: 2,
};

export function dockTabs(sessions: Sess[]): Sess[] {
  return sessions
    .map((s, i) => ({ s, i, rank: RANK[s.status] ?? 3 }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map((x) => x.s);
}

/** The tab that should be selected, given what the user last picked. Their
 *  choice is honoured while that session still exists; when it disappears
 *  (closed, or lost) selection falls to the first tab rather than to nothing,
 *  because an empty dock with live sessions behind it looks like a bug. */
export function activeHandle(tabs: Sess[], picked: string | null): string | null {
  if (picked && tabs.some((t) => t.handle === picked)) return picked;
  return tabs[0]?.handle ?? null;
}

/** The dock's live dot: the colour of the most urgent thing across every
 *  session, not just the selected one — the dot is the reason to look at a
 *  tab you are not on. */
export function liveDot(sessions: Sess[]): "attn" | "working" | "idle" {
  if (sessions.some((s) => s.status === "needs_permission" || s.status === "needs_choice"))
    return "attn";
  // `running`, not `status === "running"`: the latter means the session process
  // is alive, which is true of every idle session sitting at a prompt. Keying
  // the dot off it painted green beside the dock's own "Ready".
  if (sessions.some((s) => s.running)) return "working";
  return "idle";
}
