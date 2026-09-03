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
