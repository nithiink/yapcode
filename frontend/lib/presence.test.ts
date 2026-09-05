import { strict as assert } from "node:assert";
import { test } from "node:test";
import { presenceLine } from "./presence.ts";
import type { Approval, Mission } from "./yuriTypes.ts";
import type { Sess } from "./sessions.ts";

const appr = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", session_id: "s1", risk: "confirm", status: "pending",
  description: "run something", requested_at: "2026-09-04T10:00:00Z",
  ...over,
} as Approval);

const sess = (over: Partial<Sess> = {}): Sess => ({
  handle: "h1", session_id: "s1", cwd: "/Users/x/projects/billing", model: "opus",
  status: "running", ...over,
});

const mission = (over: Partial<Mission> = {}): Mission => ({
  id: "m1", title: "Billing fix", status: "running", project_id: "p1",
  ...over,
} as Mission);

test("a waiting decision wins the sentence, even mid-speech", () => {
  const line = presenceLine([appr()], [], [sess()], true);
  assert.equal(line, "one decision is waiting on you");
  assert.equal(presenceLine([appr(), appr({ id: "a2" })], [], [], false),
    "2 decisions are waiting on you");
});

test("a decided approval does not count as waiting", () => {
  const line = presenceLine([appr({ status: "allowed" })], [], [sess({ status: "stopped" })], false);
  assert.equal(line, "watching 1 session · nothing needs you");
});

test("speech outranks work in flight", () => {
  assert.equal(presenceLine([], [], [sess({ running: true })], true), "telling you what just happened");
});

test("work in flight names the session and pluralises the count", () => {
  assert.match(presenceLine([], [], [sess({ running: true })], false), /billing.*is working/);
  const two = presenceLine([], [],
    [sess({ running: true }), sess({ handle: "h2", session_id: "s2", running: true })], false);
  assert.equal(two, "watching 2 sessions · 2 sessions working");
});

test("a live-but-idle session is not described as working", () => {
  // status "running" means the process is up, which is true of every session
  // sitting at a prompt. Reporting that as work in flight contradicted the
  // dock's own "Ready" one panel over.
  assert.equal(presenceLine([], [], [sess({ status: "running", running: false })], false),
    "watching 1 session · nothing needs you");
});

test("blocked work is named before work in flight", () => {
  const line = presenceLine([], [mission({ status: "failed" })], [sess({ running: true })], false);
  assert.match(line, /Billing fix is stuck/);
  assert.ok(!line.includes("working"), `work in flight leaked into a blocked line: ${line}`);
});

test("more than one stuck thing says so rather than dropping them silently", () => {
  const line = presenceLine([], [mission({ status: "failed" }),
    mission({ id: "m2", title: "Schema", status: "waiting_for_approval" })], [], false);
  assert.match(line, /\(\+1 more\)/);
});

test("quiet with sessions is different from quiet with none", () => {
  assert.equal(presenceLine([], [], [sess({ status: "stopped" })], false),
    "watching 1 session · nothing needs you");
  assert.match(presenceLine([], [], [], false), /say the word/);
});

test("the line is never empty", () => {
  for (const s of [[], [sess()], [sess({ status: "stopped" })]]) {
    for (const sp of [true, false]) {
      assert.ok(presenceLine([], [], s, sp).length > 0);
    }
  }
});
