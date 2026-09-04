import { strict as assert } from "node:assert";
import { test } from "node:test";
import { MAX_UNANSWERED_POLLS, nextUnanswered, pollVerdict } from "./polling.ts";

const ok = (status: string) => ({ ok: true, result: { status } });
const soft = (error: string) => ({ ok: false, error });

test("a working session keeps being polled", () => {
  assert.deepEqual(pollVerdict(ok("working"), 0), { action: "wait" });
});

test("idle is the clean exit", () => {
  assert.equal(pollVerdict(ok("idle"), 0).action, "stop");
});

test("any other status is a result to handle", () => {
  for (const s of ["needs_permission", "needs_choice", "completed", "error"]) {
    assert.equal(pollVerdict(ok(s), 0).action, "handle", s);
  }
});

test("a session the backend has forgotten stops the loop at once", () => {
  // This is the reported bug: 329 events for one stopped session, because
  // retrying was treated as resilience rather than a loop with no exit.
  for (const msg of ["unknown session abc", "no such session", "session not found",
                     "that session is closed", "the session is not running"]) {
    const v = pollVerdict(soft(msg), 0);
    assert.equal(v.action, "stop", msg);
    assert.ok((v as { reason: string }).reason.length > 0, "stopping must say why");
  }
});

test("a genuinely transient error is retried, but not forever", () => {
  assert.deepEqual(pollVerdict(soft("backend unreachable"), 0), { action: "wait" });
  const v = pollVerdict(soft("backend unreachable"), MAX_UNANSWERED_POLLS - 1);
  assert.equal(v.action, "stop");
  assert.match((v as { reason: string }).reason, /in a row/);
});

test("an empty response is counted, never mistaken for working", () => {
  // callTool returns data?.result, so a soft error arrives as undefined. The
  // old loop read that as "still working" and never stopped.
  assert.deepEqual(pollVerdict(undefined, 0), { action: "wait" });
  assert.equal(pollVerdict(undefined, MAX_UNANSWERED_POLLS - 1).action, "stop");
  assert.equal(pollVerdict(null, MAX_UNANSWERED_POLLS - 1).action, "stop");
  assert.equal(pollVerdict({}, MAX_UNANSWERED_POLLS - 1).action, "stop");
});

test("the reason a stop gives is always something a human can read", () => {
  for (const env of [soft(""), undefined, {}]) {
    const v = pollVerdict(env, MAX_UNANSWERED_POLLS - 1);
    assert.equal(v.action, "stop");
    assert.ok((v as { reason: string }).reason.trim().length > 3, JSON.stringify(env));
  }
});

test("the counter resets on a usable answer and climbs otherwise", () => {
  assert.equal(nextUnanswered(ok("working"), 5), 0);
  assert.equal(nextUnanswered(soft("boom"), 5), 6);
  assert.equal(nextUnanswered(undefined, 0), 1);
});

test("a working session can be polled indefinitely without tripping the bound", () => {
  // The bound must never cut off real, long-running work — a Bash step can
  // take minutes.
  let n = 0;
  for (let i = 0; i < 1000; i++) {
    assert.deepEqual(pollVerdict(ok("working"), n), { action: "wait" });
    n = nextUnanswered(ok("working"), n);
  }
  assert.equal(n, 0);
});
