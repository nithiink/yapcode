// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { MODES, MODE_LABEL, sessionStatus, tmuxAttachCommand, type Sess } from "./sessions.ts";

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

test("a CLI-backend session gets a tmux attach command", () => {
  const cmd = tmuxAttachCommand({ ...base, backend: "cli" });
  assert.equal(cmd, `tmux attach -t vc_${base.handle.slice(0, 8)}`);
});

test("an SDK-backend session has no tmux pane to attach to", () => {
  assert.equal(tmuxAttachCommand({ ...base, backend: "sdk" }), null);
});

test("a session with no backend field has no tmux command either", () => {
  assert.equal(tmuxAttachCommand(base), null);
});
