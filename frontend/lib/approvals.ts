// Pure selectors behind the Approvals view (and the ApprovalCard it shares
// with the Dashboard) — kept out of the components so they're reachable from
// `node --test`. See approvals.test.ts for the behavior this file has to
// preserve: dangerous mapping to --danger not --warn, an empty action
// falling back to the tool name instead of a blank card, and a negative wait
// from clock skew never printing.
import type { Approval } from "./yuriTypes.ts";

export const RISK_LABEL: Record<Approval["risk"], string> = {
  safe: "Safe",
  confirm: "Confirm",
  dangerous: "Dangerous",
};

// The CSS token each risk level renders with (see app/globals.css: --good,
// --warn, --danger). "dangerous" -> "danger" is the one mapping that must
// never drift to "warn" — the risk label is what Yuri says aloud, and the
// screen has to agree with her.
export const RISK_CLASS: Record<Approval["risk"], string> = {
  safe: "good",
  confirm: "warn",
  dangerous: "danger",
};

// The one-line "what is being asked". action is the human-authored summary
// ("run rm -rf build"); an empty action would render a blank card, which is
// worse than falling back to the bare tool name.
export function approvalTitle(a: Approval): string {
  return a.action || a.description || a.tool_name;
}

// "waiting 2m" — how long this approval has sat unanswered. Clamped at zero
// so clock skew between the backend and the browser never prints a negative
// wait, and survives an unparseable requested_at by returning "".
export function waitedFor(a: Approval, now: number = Date.now()): string {
  const requested = Date.parse(a.requested_at);
  if (Number.isNaN(requested)) return "";
  const secs = Math.max(0, Math.floor((now - requested) / 1000));
  if (secs < 60) return `waiting ${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `waiting ${mins}m`;
  const hours = Math.floor(mins / 60);
  return `waiting ${hours}h`;
}

/** What became of an approval, or `null` while it is still waiting.
 *
 *  The card used to render Allow/Deny for every approval it was handed,
 *  including resolved ones — its own comment admitted as much. Pressing those
 *  bought a 409 from the backend and a "someone answered it first" toast, so
 *  they were controls that could only fail. The rule the mission Delete button
 *  already follows applies here: a control that would fail must not be
 *  rendered. Better still, a resolved approval has everything needed to say
 *  what actually happened, so the space becomes an answer instead of a
 *  disabled button.
 */
export function outcomeOf(a: Approval, now: number = Date.now()): {
  label: string; cls: string; detail: string;
} | null {
  if (a.status === "pending") return null;

  const label = {
    allowed: "Allowed",
    denied: "Denied",
    // Not a decision anyone made — say so, or "Expired" reads like a verdict.
    expired: "Expired unanswered",
    // A mode switch (acceptEdits/auto) approves everything it covers, so the
    // prompt was retired rather than answered.
    superseded: "Covered by a mode change",
  }[a.status];

  const by = a.resolved_by && a.resolved_by !== "mode_switch"
    ? { voice: "by voice", ui: "in the UI", api: "over the API" }[a.resolved_by] ?? ""
    : "";
  const when = a.resolved_at ? agoOf(a.resolved_at, now) : "";
  const cls = a.status === "allowed" ? "good" : a.status === "denied" ? "danger" : "dim";

  return { label, cls, detail: [by, when].filter(Boolean).join(" · ") };
}

/** "just now" / "4m ago" / "2h ago". Empty for an unparseable timestamp — a
 *  wrong time is worse than none. */
export function agoOf(iso: string, now: number = Date.now()): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const secs = Math.max(0, Math.floor((now - at) / 1000));
  if (secs < 45) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${Math.max(1, mins)}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}
