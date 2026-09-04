import test from "node:test";
import assert from "node:assert/strict";
import {
  STATUS_LABEL, TASK_STATUSES, canAssign, canRetry, canSkip, durationOf, isTerminal,
  progressOf, statusLabel, taskClass, timelineOrder, verdictLabel, verdictsOf,
  type Deps, type Task,
} from "./workflow.ts";

const task = (id: string, ordinal: number, over: Partial<Task> = {}): Task => ({
  id, ordinal, title: `step ${id}`, status: "pending", ...over,
});

const chain = (): { tasks: Task[]; deps: Deps } => ({
  tasks: [task("d", 3), task("a", 0), task("c", 2), task("b", 1)],
  deps: { b: ["a"], c: ["b"], d: ["c"] },
});

test("the timeline is in dependency order, not the order rows arrived", () => {
  const { tasks, deps } = chain();
  assert.deepEqual(timelineOrder(tasks, deps).map((t) => t.id), ["a", "b", "c", "d"]);
});

test("independent steps are ordered by their authoring ordinal", () => {
  const tasks = [task("z", 2), task("x", 0), task("y", 1)];
  assert.deepEqual(timelineOrder(tasks, {}).map((t) => t.id), ["x", "y", "z"]);
});

test("the order is identical every time it is computed", () => {
  // THE requirement. The timeline re-renders on a 2.5s poll; an order that
  // depends on anything but the graph makes rows jump under the cursor.
  const { tasks, deps } = chain();
  const first = timelineOrder(tasks, deps).map((t) => t.id);
  for (let i = 0; i < 5; i++) {
    assert.deepEqual(timelineOrder([...tasks].reverse(), deps).map((t) => t.id), first);
  }
});

test("changing a status does not change the order", () => {
  // Otherwise a step finishing would reshuffle the page the user is reading.
  const { tasks, deps } = chain();
  const before = timelineOrder(tasks, deps).map((t) => t.id);
  const after = timelineOrder(
    tasks.map((t) => ({ ...t, status: t.id === "b" ? "completed" : t.status })), deps);
  assert.deepEqual(after.map((t) => t.id), before);
});

test("a cycle appends the rest instead of hanging the tab", () => {
  // The backend rejects a cycle at create time, so this is unreachable
  // through the API — but an infinite loop here would freeze the browser,
  // and showing a step out of order beats showing nothing.
  const tasks = [task("a", 0), task("b", 1)];
  const out = timelineOrder(tasks, { a: ["b"], b: ["a"] });
  assert.equal(out.length, 2);
  assert.deepEqual(out.map((t) => t.id).sort(), ["a", "b"]);
});

test("a partial cycle still places the tasks that are reachable", () => {
  const tasks = [task("a", 0), task("b", 1), task("c", 2)];
  const out = timelineOrder(tasks, { b: ["c"], c: ["b"] });
  assert.equal(out[0].id, "a");
  assert.equal(out.length, 3);
});

test("an edge to a task that is not in this workflow does not stall the list", () => {
  const tasks = [task("a", 0)];
  assert.deepEqual(timelineOrder(tasks, { a: ["ghost"] }).map((t) => t.id), ["a"]);
});

test("no task is ever dropped or duplicated", () => {
  const { tasks, deps } = chain();
  const out = timelineOrder(tasks, deps);
  assert.equal(out.length, tasks.length);
  assert.equal(new Set(out.map((t) => t.id)).size, tasks.length);
});

test("an empty workflow orders to nothing without complaint", () => {
  assert.deepEqual(timelineOrder([], {}), []);
});

test("every status has a class and a label", () => {
  // A status with no class renders unstyled, which reads as "no state at
  // all"; one with no label shows the raw enum.
  for (const status of TASK_STATUSES) {
    assert.equal(taskClass(status), `tk-${status}`);
    assert.ok(STATUS_LABEL[status], status);
    assert.notEqual(statusLabel(status), status, `${status} is shown as jargon`);
  }
});

test("an unknown status still gets a class rather than none", () => {
  assert.equal(taskClass("something-new"), "tk-pending");
  assert.equal(statusLabel("something-new"), "something-new");
});

test("unavailable renders as could not run, never as a tick", () => {
  // The backend does not count it as a pass either. A project with no test
  // command cannot claim tests_pass.
  const out = verdictLabel({ check: "tests_pass", verdict: "unavailable",
                             detail: "no test command configured" });
  assert.equal(out.tone, "unknown");
  assert.match(out.text, /could not run/);
  assert.ok(!/passed/.test(out.text));
});

test("pass and fail read as themselves, and fail carries the detail", () => {
  assert.equal(verdictLabel({ check: "tests_pass", verdict: "pass" }).tone, "good");
  const bad = verdictLabel({ check: "tests_pass", verdict: "fail", detail: "2 failed" });
  assert.equal(bad.tone, "bad");
  assert.match(bad.text, /2 failed/);
});

test("verdicts are read off the task, so they survive a reload", () => {
  const t = task("a", 0, { result: { verification: [{ check: "tests_pass", verdict: "pass" }] } });
  assert.equal(verdictsOf(t).length, 1);
  assert.deepEqual(verdictsOf(task("b", 1)), []);
  assert.deepEqual(verdictsOf(task("c", 2, { result: { verification: "nope" as never } })), []);
});

test("only a step that could actually act offers the control", () => {
  // Each gate mirrors the backend's precondition; a button that answers 409
  // is the bug this prevents.
  const status = (s: string) => task("a", 0, { status: s });
  assert.ok(canRetry(status("failed")) && canRetry(status("blocked")));
  for (const s of ["pending", "ready", "dispatched", "running", "verifying", "completed"]) {
    assert.ok(!canRetry(status(s)), s);
  }
  // skip: no path out of running/verifying/waiting_approval — an agent is
  // mid-turn and there is nothing to drop.
  for (const s of ["running", "verifying", "waiting_approval", "completed", "skipped"]) {
    assert.ok(!canSkip(status(s)), s);
  }
  assert.ok(canSkip(status("dispatched")), "dispatched is un-dispatchable, then skippable");
  // assign: ASSIGNABLE only. A pin after dispatch would not change who is
  // already working.
  assert.ok(canAssign(status("pending")) && canAssign(status("failed")));
  assert.ok(!canAssign(status("dispatched")));
});

test("terminal statuses are the three that satisfy a dependency", () => {
  assert.ok(isTerminal(task("a", 0, { status: "completed" })));
  assert.ok(isTerminal(task("a", 0, { status: "skipped" })));
  assert.ok(!isTerminal(task("a", 0, { status: "blocked" })));
});

test("progress counts what needs the user separately", () => {
  // "3 of 4" hides a blocked step, which is the one thing worth surfacing.
  const out = progressOf([
    task("a", 0, { status: "completed" }),
    task("b", 1, { status: "blocked" }),
    task("c", 2, { status: "running" }),
  ]);
  assert.deepEqual(out, { done: 1, total: 3, needsYou: 1 });
});

test("a step that has not started shows no duration at all", () => {
  // "0s" reads as "took no time" rather than "has not begun".
  assert.equal(durationOf(task("a", 0)), "");
  assert.equal(durationOf(task("a", 0, { started_at: "not a date" })), "");
});

test("a finished step shows how long it took, a running one how long so far", () => {
  const start = "2026-09-04T10:00:00Z";
  assert.equal(durationOf(task("a", 0, { started_at: start, ended_at: "2026-09-04T10:00:42Z" })),
               "42s");
  assert.equal(durationOf(task("a", 0, { started_at: start, ended_at: "2026-09-04T10:02:05Z" })),
               "2m 5s");
  assert.equal(durationOf(task("a", 0, { started_at: start, ended_at: "2026-09-04T11:30:00Z" })),
               "1h 30m");
  assert.equal(durationOf(task("a", 0, { started_at: start }), Date.parse("2026-09-04T10:00:10Z")),
               "10s");
});
