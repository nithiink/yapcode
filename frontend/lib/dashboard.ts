// Pure selectors for the Dashboard's three "what needs you" bands. Kept out
// of the component so they're reachable from `node --test` — see
// dashboard.test.ts for the behavior this file has to preserve: dangerous
// approvals before confirm before safe, oldest ask first within a level, a
// lost session counted as blocked (not running), and terminal mission states
// counted as neither blocked nor running.
import type { Approval, Mission } from "./yuriTypes.ts";
import type { Sess } from "./sessions.ts";

export type BlockedItem =
  | { kind: "mission"; mission: Mission }
  | { kind: "session"; session: Sess };

export type Band = {
  needsYou: Approval[];
  blocked: BlockedItem[];
  running: Sess[];
};

const RISK_ORDER: Record<Approval["risk"], number> = { dangerous: 0, confirm: 1, safe: 2 };

// A mission that cannot make progress without a human: it's waiting on an
// approval, or it already stopped trying. "running" (even mid-step) is not
// blocked; "draft"/"queued"/"paused" aren't stuck on anything either.
const BLOCKED_MISSION_STATUSES = new Set<Mission["status"]>(["waiting_for_approval", "failed"]);

export function bands(approvals: Approval[], missions: Mission[], sessions: Sess[]): Band {
  const needsYou = approvals
    .filter((a) => a.status === "pending")
    .slice()
    .sort((a, b) => {
      const riskDiff = RISK_ORDER[a.risk] - RISK_ORDER[b.risk];
      if (riskDiff !== 0) return riskDiff;
      return a.requested_at < b.requested_at ? -1 : a.requested_at > b.requested_at ? 1 : 0;
    });

  const blocked: BlockedItem[] = [
    ...missions
      .filter((m) => BLOCKED_MISSION_STATUSES.has(m.status))
      .map((mission): BlockedItem => ({ kind: "mission", mission })),
    ...sessions
      .filter((s) => s.status === "lost")
      .map((session): BlockedItem => ({ kind: "session", session })),
  ];

  const running = sessions.filter((s) => s.status === "running");

  return { needsYou, blocked, running };
}

export function navBadges(
  approvals: Approval[],
  missions: Mission[],
): { approvals: number; missions: number } {
  return {
    approvals: approvals.filter((a) => a.status === "pending").length,
    missions: missions.filter((m) => BLOCKED_MISSION_STATUSES.has(m.status)).length,
  };
}
