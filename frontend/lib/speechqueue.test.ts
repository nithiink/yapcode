import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  forceDrain, hasPending, newSpeechQueue, noteInterrupted, noteTurnComplete,
  noteTurnStart, reset, submit,
} from "./speechqueue.ts";

test("an update with nothing in flight goes straight out", () => {
  const q = newSpeechQueue();
  assert.deepEqual(submit(q, "the tests passed"), { send: "the tests passed", dropped: 0 });
});

test("an update landing mid-sentence is held, not sent", () => {
  // THE bug. On Gemini, sending is how you ask for a response, and a new turn
  // preempts the current one — so this used to cut her off mid-word.
  const q = newSpeechQueue();
  noteTurnStart(q);
  assert.deepEqual(submit(q, "the build finished"), { send: null, dropped: 0 });
  assert.ok(hasPending(q));
});

test("it is released when she finishes the sentence", () => {
  const q = newSpeechQueue();
  noteTurnStart(q);
  submit(q, "the build finished");
  assert.equal(noteTurnComplete(q), "the build finished");
  assert.ok(!hasPending(q));
});

test("held updates come out one at a time, in order", () => {
  // One at a time so each gets its own spoken response instead of three
  // results being merged into one unreadable paragraph.
  const q = newSpeechQueue();
  noteTurnStart(q);
  for (const t of ["first", "second", "third"]) submit(q, t);
  assert.equal(noteTurnComplete(q), "first");
  assert.equal(noteTurnComplete(q), "second");
  assert.equal(noteTurnComplete(q), "third");
  assert.equal(noteTurnComplete(q), null);
});

test("releasing one marks her as responding again", () => {
  // Otherwise the second release would fire while the first is still being
  // spoken, which is the bug again one level down.
  const q = newSpeechQueue();
  noteTurnStart(q);
  submit(q, "a");
  submit(q, "b");
  noteTurnComplete(q);
  assert.equal(submit(q, "c").send, null, "sent while the released item was still speaking");
});

test("nothing held means nothing released, and she is free", () => {
  const q = newSpeechQueue();
  noteTurnStart(q);
  assert.equal(noteTurnComplete(q), null);
  assert.equal(submit(q, "now").send, "now");
});

test("an interruption holds the backlog rather than pushing it at the user", () => {
  // They cut in because they wanted the floor. Answering with a queued build
  // notification is the same rudeness aimed the other way.
  const q = newSpeechQueue();
  noteTurnStart(q);
  submit(q, "the build finished");
  noteInterrupted(q);
  assert.ok(hasPending(q), "an interruption dropped the backlog");
  // It survives to the next turn's end.
  noteTurnStart(q);
  assert.equal(noteTurnComplete(q), "the build finished");
});

test("a permission request is never evicted by the bound", () => {
  // A dropped ask is unrecoverable — poll_status hands each buffered result
  // back exactly once, so it is never re-offered.
  const q = newSpeechQueue();
  noteTurnStart(q);
  submit(q, "PERMISSION: rm -rf build", true, 3);
  for (let i = 0; i < 20; i++) submit(q, `texture ${i}`, false, 3);
  const released: string[] = [];
  for (let i = 0; i < 25; i++) {
    const out = noteTurnComplete(q);
    if (out) released.push(out);
    else break;
    noteTurnStart(q);
  }
  assert.ok(released.includes("PERMISSION: rm -rf build"), "the ask was evicted");
});

test("texture lines are dropped rather than growing without bound", () => {
  const q = newSpeechQueue();
  noteTurnStart(q);
  let dropped = 0;
  for (let i = 0; i < 30; i++) dropped += submit(q, `line ${i}`, false, 4).dropped;
  assert.ok(dropped > 0, "the queue grew unbounded");
  assert.ok(q.pending.length <= 4, `queue holds ${q.pending.length}`);
});

test("a stalled turn eventually releases rather than swallowing everything", () => {
  // A queue that can deadlock is worse than one that gives up: a session that
  // never sends turnComplete would otherwise lose every background result.
  const q = newSpeechQueue();
  noteTurnStart(q);
  submit(q, "the build finished");
  assert.equal(forceDrain(q), "the build finished");
});

test("teardown drops everything so nothing fires into a dead socket", () => {
  const q = newSpeechQueue();
  noteTurnStart(q);
  submit(q, "a");
  reset(q);
  assert.ok(!hasPending(q));
  assert.equal(submit(q, "b").send, "b", "still considered responding after teardown");
});
