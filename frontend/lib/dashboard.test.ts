// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { bands, navBadges } from "./dashboard.ts";
import type { Approval, Mission } from "./yuriTypes.ts";
import type { Sess } from "./sessions.ts";

const approval = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", session_id: "s1", agent_id: "claude-code", mission_id: "m1",
  action: "run rm -rf build", tool_name: "Bash", request_id: "r1",
  tool_input: { command: "rm -rf build" }, risk: "confirm",
  description: "", status: "pending", requested_at: "2026-09-03T00:00:00Z",
  resolved_at: null, resolved_by: null, ...over,
});
const mission = (over: Partial<Mission> = {}): Mission => ({
  id: "m1", title: "Fix billing", project_id: "p1", goal: null, status: "running",
  priority: 0, current_step: null, created_by: "voice", metadata: {},
  created_at: "2026-09-03T00:00:00Z", updated_at: "2026-09-03T00:00:00Z", ...over,
});
const sess = (over: Partial<Sess> = {}): Sess => ({
  handle: "h1", session_id: "s1", cwd: "/tmp", model: "opus", status: "idle", ...over,
} as Sess);

test("only pending approvals need you", () => {
  const b = bands([approval(), approval({ id: "a2", status: "allowed" })], [], []);
  assert.deepEqual(b.needsYou.map((a) => a.id), ["a1"]);
});

test("dangerous approvals come first, then confirm, then safe", () => {
  const b = bands([
    approval({ id: "safe", risk: "safe" }),
    approval({ id: "danger", risk: "dangerous" }),
    approval({ id: "conf", risk: "confirm" }),
  ], [], []);
  assert.deepEqual(b.needsYou.map((a) => a.id), ["danger", "conf", "safe"]);
});

test("within a risk level the oldest ask comes first", () => {
  // The one that has been waiting longest is the one costing you time.
  const b = bands([
    approval({ id: "new", requested_at: "2026-09-03T00:00:10Z" }),
    approval({ id: "old", requested_at: "2026-09-03T00:00:01Z" }),
  ], [], []);
  assert.deepEqual(b.needsYou.map((a) => a.id), ["old", "new"]);
});

test("failed and waiting_for_approval missions are blocked; running is not", () => {
  const b = bands([], [
    mission({ id: "ok", status: "running" }),
    mission({ id: "wait", status: "waiting_for_approval" }),
    mission({ id: "bad", status: "failed" }),
  ], []);
  assert.deepEqual(
    b.blocked.filter((x) => x.kind === "mission").map((x) => (x as { mission: Mission }).mission.id),
    ["wait", "bad"],
  );
});

test("a lost session is blocked", () => {
  const b = bands([], [], [sess({ status: "lost" })]);
  assert.equal(b.blocked.filter((x) => x.kind === "session").length, 1);
});

test("terminal mission states are not blocked", () => {
  for (const status of ["completed", "cancelled"] as const) {
    assert.equal(bands([], [mission({ status })], []).blocked.length, 0, status);
  }
});

test("nothing anywhere yields empty bands, not an error", () => {
  assert.deepEqual(bands([], [], []), { needsYou: [], blocked: [] });
});

test("nav badges count pending approvals and blocked missions only", () => {
  const badges = navBadges(
    [approval(), approval({ id: "a2", status: "denied" })],
    [mission({ status: "failed" }), mission({ id: "m2", status: "running" })],
  );
  assert.deepEqual(badges, { approvals: 1, missions: 1 });
});
