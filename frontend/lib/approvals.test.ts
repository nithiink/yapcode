// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { RISK_CLASS, RISK_LABEL, approvalTitle, waitedFor } from "./approvals.ts";
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
