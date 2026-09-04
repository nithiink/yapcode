// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { RISK_CLASS, RISK_LABEL, agoOf, approvalTitle, outcomeOf, waitedFor } from "./approvals.ts";
import type { Approval } from "./yuriTypes.ts";

const a = (over: Partial<Approval> = {}): Approval => ({
  id: "a1", session_id: "s1", agent_id: "claude-code", mission_id: null,
  action: "run rm -rf build", tool_name: "Bash", request_id: "r1",
  tool_input: { command: "rm -rf build" }, risk: "confirm", description: "",
  status: "pending", requested_at: "2026-09-03T00:00:00Z",
  resolved_at: null, resolved_by: null, ...over,
});

test("every risk level has a label and a token class", () => {
  for (const risk of ["safe", "confirm", "dangerous"] as const) {
    assert.ok(RISK_LABEL[risk], risk);
    assert.ok(["good", "warn", "danger"].includes(RISK_CLASS[risk]), risk);
  }
});

test("dangerous maps to the danger token, not warn", () => {
  assert.equal(RISK_CLASS.dangerous, "danger");
});

test("approvalTitle prefers the action text", () => {
  assert.match(approvalTitle(a()), /rm -rf build/);
});

test("approvalTitle falls back to the tool name when action is empty", () => {
  // An empty title would render a blank card, which is worse than a bare tool name.
  assert.match(approvalTitle(a({ action: "", description: "" })), /Bash/);
});

test("waitedFor renders seconds, minutes and hours", () => {
  const t0 = Date.parse("2026-09-03T00:00:00Z");
  assert.match(waitedFor(a(), t0 + 5_000), /5s/);
  assert.match(waitedFor(a(), t0 + 125_000), /2m/);
  assert.match(waitedFor(a(), t0 + 7_400_000), /2h/);
});

test("waitedFor never renders a negative wait", () => {
  // Clock skew between the backend and the browser must not print "-3s".
  const t0 = Date.parse("2026-09-03T00:00:00Z");
  assert.doesNotMatch(waitedFor(a(), t0 - 3_000), /-/);
});

test("waitedFor survives an unparseable timestamp", () => {
  assert.equal(typeof waitedFor(a({ requested_at: "nonsense" })), "string");
});

/** outcomeOf returns null while pending; every test below is about a resolved
 *  one, so unwrap with an assertion rather than a `!` that hides a null. */
const decided = (over: Partial<Approval>, now: number) => {
  const out = outcomeOf(a(over), now);
  assert.ok(out, `expected an outcome for status ${over.status}`);
  return out;
};

const RESOLVED = "2026-09-04T10:00:00.000Z";
const NOW = Date.parse("2026-09-04T10:04:00.000Z");

test("a pending approval has no outcome, so its buttons stay", () => {
  assert.equal(outcomeOf(a({ status: "pending" }), NOW), null);
});

test("every resolved status produces an outcome instead of buttons", () => {
  for (const status of ["allowed", "denied", "expired", "superseded"] as const) {
    assert.ok(decided({ status, resolved_at: RESOLVED }, NOW).label.trim().length > 0, status);
  }
});

test("allowed and denied read as decisions, the other two do not", () => {
  // "Expired" alone reads like a verdict someone reached. Nobody decided it.
  assert.match(decided({ status: "expired" }, NOW).label, /unanswered/i);
  assert.match(decided({ status: "superseded" }, NOW).label, /mode change/i);
  assert.equal(decided({ status: "allowed" }, NOW).label, "Allowed");
  assert.equal(decided({ status: "denied" }, NOW).label, "Denied");
});

test("the outcome says who answered and when", () => {
  const out = decided({ status: "allowed", resolved_by: "voice", resolved_at: RESOLVED }, NOW);
  assert.match(out.detail, /by voice/);
  assert.match(out.detail, /4m ago/);
});

test("a mode switch is not credited to a person", () => {
  const out = decided({ status: "superseded", resolved_by: "mode_switch", resolved_at: RESOLVED }, NOW);
  assert.ok(!/by |in the |over the /.test(out.detail), `attributed a mode switch: ${out.detail}`);
});

test("allowed and denied are visually distinct", () => {
  assert.notEqual(decided({ status: "allowed" }, NOW).cls,
                  decided({ status: "denied" }, NOW).cls);
});

test("a missing or broken resolved_at degrades to no time, never a wrong one", () => {
  assert.equal(decided({ status: "allowed", resolved_at: null }, NOW).detail.includes("ago"), false);
  assert.equal(agoOf("not a date"), "");
});

test("agoOf reads naturally across the ranges", () => {
  const t = (iso: string) => agoOf(iso, Date.parse("2026-09-04T12:00:00Z"));
  assert.equal(t("2026-09-04T11:59:40Z"), "just now");
  assert.equal(t("2026-09-04T11:56:00Z"), "4m ago");
  assert.equal(t("2026-09-04T09:00:00Z"), "3h ago");
  assert.equal(t("2026-09-02T12:00:00Z"), "2d ago");
});
