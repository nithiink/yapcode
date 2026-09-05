// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { MODES, MODE_LABEL, sessionStatus, sessionLabel, type Sess } from "./sessions.ts";

const base: Sess = {
  handle: "h1", session_id: "s1", cwd: "/tmp/proj", model: "opus",
  status: "idle",
};

test("every mode has a label and a title", () => {
  for (const m of MODES) {
    assert.ok(m.id && m.label && m.title, `mode ${JSON.stringify(m)} is incomplete`);
    assert.equal(MODE_LABEL[m.id], m.label);
  }
});

test("an idle session reports no running task", () => {
  const st = sessionStatus(base);
  assert.ok(st.cls);
  assert.ok(st.lead);
});

test("a running turn's text becomes the task line", () => {
  const st = sessionStatus({ ...base, status: "running", running: true,
                             queue: [{ text: "fix the billing bug", state: "running" }] });
  assert.match(st.task, /billing/);
});

test("needs_permission is surfaced as its own lead", () => {
  const st = sessionStatus({ ...base, status: "needs_permission" });
  assert.notEqual(st.lead, sessionStatus(base).lead);
});

test("sessionLabel prefers an explicit name", () => {
  assert.equal(sessionLabel({ ...base, name: "Billing fix" }), "Billing fix");
});

test("sessionLabel falls back to the folder basename when unnamed", () => {
  assert.equal(sessionLabel({ ...base, name: null, cwd: "/Users/ankur/code/yuri-code" }), "yuri-code");
});

test("sessionLabel ignores a trailing slash on the folder", () => {
  assert.equal(sessionLabel({ ...base, name: null, cwd: "/Users/ankur/code/yuri-code/" }), "yuri-code");
});

test("sessionLabel never falls back to the raw handle in full", () => {
  const label = sessionLabel({ handle: "3f9c7a2b-1234-5678-9abc-def012345678", cwd: "", name: null });
  assert.equal(label, "3f9c7a2b");
  assert.ok(label.length < 36);
});

test("sessionLabel adapts a differently-shaped session record via an object literal", () => {
  // MissionDetail's AgentSession has no `cwd`/`handle` — this is the adapter
  // shape every call site not already holding a `Sess` should use.
  const agentSession = { name: null as string | null, working_directory: "/tmp/proj", native_session_id: "abcdef1234567890" };
  const label = sessionLabel({ name: agentSession.name, cwd: agentSession.working_directory, handle: agentSession.native_session_id });
  assert.equal(label, "proj");
});
