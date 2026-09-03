// Pure selectors behind the Missions list/detail views (and the Dashboard's
// blocked-mission controls, which used to keep their own copy of this same
// table inline — see app/page.tsx's history). Kept out of the components so
// they're reachable from `node --test` and so there is exactly one place
// that knows what the backend will and won't allow.
//
// This table is a straight transcription of backend/yuri/domain/mission.py's
// TRANSITIONS (line 29) — the single source of truth. It is not read from the
// backend at runtime (there is no endpoint for it); keeping the two in sync
// is a manual invariant, same as the backend module's own comment admits for
// its callers. If that table changes, this one has to change with it.
import type { Mission } from "./yuriTypes.ts";

const TRANSITIONS: Record<Mission["status"], ReadonlySet<Mission["status"]>> = {
  draft: new Set(["queued", "running", "cancelled"]),
  queued: new Set(["running", "cancelled"]),
  running: new Set(["waiting_for_approval", "paused", "completed", "failed", "cancelled"]),
  waiting_for_approval: new Set(["running", "paused", "failed", "cancelled"]),
  paused: new Set(["running", "cancelled"]),
  completed: new Set(),
  failed: new Set(),
  cancelled: new Set(),
};

// The CSS token class each status renders with (see app/globals.css: --mut,
// --plan, --acc, --warn, --good, --danger). draft/paused/cancelled all share
// "mut" — none of them is actively progressing, and none is an error either.
export const MISSION_CLASS: Record<Mission["status"], string> = {
  draft: "mut",
  queued: "plan",
  running: "acc",
  waiting_for_approval: "warn",
  paused: "mut",
  completed: "good",
  failed: "danger",
  cancelled: "mut",
};

// Pause is legal from anywhere the table allows a move to "paused" —
// currently running and waiting_for_approval. Derived from the table rather
// than hardcoded so it can't silently drift from it.
export function canPause(m: Mission): boolean {
  return TRANSITIONS[m.status].has("paused");
}

// Resume is NOT simply "can this status reach running" — draft, queued and
// waiting_for_approval can all reach running too, but that's a mission
// starting or recovering, not being resumed. "Resume" only ever means
// leaving pause, so this is the one control that can't be derived from the
// table's "running" edge and is instead a direct status check.
export function canResume(m: Mission): boolean {
  return m.status === "paused";
}

// Cancel is legal from every non-terminal status — the table gives every one
// of them a "cancelled" edge, and none of the terminal ones do.
export function canCancel(m: Mission): boolean {
  return TRANSITIONS[m.status].has("cancelled");
}
