// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { MISSION_CLASS, canCancel, canDelete, canPause, canResume } from "./missions.ts";
import type { Mission } from "./yuriTypes.ts";

const m = (status: Mission["status"]): Mission => ({
  id: "m1", title: "t", project_id: "p1", goal: null, status, priority: 0,
  current_step: null, created_by: "voice", metadata: {},
  created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z",
});

test("every status has a token class", () => {
  for (const s of ["draft", "queued", "running", "waiting_for_approval", "paused",
                   "completed", "failed", "cancelled"] as const) {
    assert.ok(MISSION_CLASS[s], s);
  }
});

// These mirror backend/yuri/domain/mission.py's TRANSITIONS table. Offering a
// control the backend refuses with a 409 is worse than not offering it.
test("pause is offered only from running and waiting_for_approval", () => {
  assert.equal(canPause(m("running")), true);
  assert.equal(canPause(m("waiting_for_approval")), true);
  assert.equal(canPause(m("paused")), false);
  assert.equal(canPause(m("draft")), false);
});

test("resume is offered only from paused", () => {
  assert.equal(canResume(m("paused")), true);
  assert.equal(canResume(m("running")), false);
});

test("cancel is offered from every non-terminal status", () => {
  for (const s of ["draft", "queued", "running", "waiting_for_approval", "paused"] as const) {
    assert.equal(canCancel(m(s)), true, s);
  }
});

test("no control is offered on a terminal mission", () => {
  for (const s of ["completed", "failed", "cancelled"] as const) {
    assert.equal(canPause(m(s)), false, s);
    assert.equal(canResume(m(s)), false, s);
    assert.equal(canCancel(m(s)), false, s);
  }
});

test("delete is offered only once a mission has stopped moving", () => {
  for (const status of ["completed", "failed", "cancelled"] as Mission["status"][]) {
    assert.ok(canDelete(m(status)), `${status} should be deletable`);
  }
  for (const status of ["draft", "queued", "running", "waiting_for_approval", "paused"] as Mission["status"][]) {
    assert.ok(!canDelete(m(status)),
      `${status} would 409 on the backend — the button must not be there`);
  }
});

test("delete and cancel are never both offered", () => {
  // They are complements: cancel means it can still move, delete means it
  // cannot. Both showing at once would mean one of them is a broken click.
  for (const status of ["draft", "queued", "running", "waiting_for_approval",
                        "paused", "completed", "failed", "cancelled"] as Mission["status"][]) {
    const mission = m(status);
    assert.notEqual(canDelete(mission), canCancel(mission), `both/neither for ${status}`);
  }
});
