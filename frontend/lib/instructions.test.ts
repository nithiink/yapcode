// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { INSTRUCTIONS, yuriContextBlock } from "./instructions.ts";

test("INSTRUCTIONS is persona + operating rules and keeps the load-bearing tool names", () => {
  assert.ok(INSTRUCTIONS.startsWith("You are Yuri"));
  for (const t of ["start_session", "tell_claude", "answer_prompt", "interrupt_session", "set_mode", "send_keys", "remember", "mute"]) {
    assert.ok(INSTRUCTIONS.includes(t), `missing ${t}`);
  }
  assert.ok(!INSTRUCTIONS.includes("You are the VOICE for Claude Code"));
});

test("context block is empty when the backend is unreachable", () => {
  assert.equal(yuriContextBlock(null), "");
  assert.equal(yuriContextBlock(undefined), "");
});

test("context block renders memory, journal, agents, missions", () => {
  const out = yuriContextBlock({
    home: "/Users/x/Yuri",
    memory_user: "- 2026-09-02  prefers pnpm",
    journal_today: "# 2026-09-02\n- 09:00  mission created: fix",
    active_missions: [{ id: "m", title: "fix", goal: "make tests pass", status: "running", project: "pm-tool" }],
    agents: [{ id: "claude-code", name: "Claude Code", online: false }],
  });
  assert.ok(out.includes("YOUR HOME: /Users/x/Yuri"));
  assert.ok(out.includes("prefers pnpm"));
  assert.ok(out.includes("TODAY SO FAR"));
  assert.ok(out.includes("Claude Code: OFFLINE"));
  assert.ok(out.includes('"fix" · pm-tool · running · goal: make tests pass'));
});

test("memory is capped to its tail", () => {
  const out = yuriContextBlock({ home: "h", memory_user: "a".repeat(5000) + "END", journal_today: "", active_missions: [], agents: [] });
  assert.ok(out.includes("END"));
  assert.ok(!out.includes("a".repeat(4500)));
});
