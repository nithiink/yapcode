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

test('"be quiet" is claimed by narration only — never by mute', () => {
  // The MUTE bullet used to list "be quiet" alongside "stop listening". If the
  // model took that branch it turned the microphone off, which this same
  // prompt says the user cannot undo by voice. Volume phrasings belong to
  // set_narration; only listening phrasings belong to mute.
  const bullets = INSTRUCTIONS.split("\n").filter((l) => /be quiet|stop narrating/i.test(l));
  assert.equal(bullets.length, 1, `claimed by ${bullets.length} bullets`);
  assert.ok(bullets[0].includes("set_narration"));
  assert.ok(!/call the mute tool/i.test(bullets[0]));
  const mute = INSTRUCTIONS.split("\n").filter((l) => /call the mute tool/i.test(l));
  assert.equal(mute.length, 1);
  assert.ok(mute[0].includes('"stop listening"'));
  assert.ok(mute[0].includes("set_narration"), "mute must redirect the talk-less case");
});

test("the AGENTS bullet teaches agent choice without promising a tool that does not exist", () => {
  // There is no list_agents voice tool, which is why the bullet points at the
  // context's AGENTS list; and `agent` is the parameter start_session actually
  // declares, so a rename on either side must break this.
  const bullets = INSTRUCTIONS.split("\n").filter((l) => l.startsWith("- AGENTS:"));
  assert.equal(bullets.length, 1, `claimed by ${bullets.length} bullets`);
  assert.ok(bullets[0].includes('agent="opencode"'));
  assert.ok(/AGENTS list in your context/.test(bullets[0]));
  assert.ok(bullets[0].includes("set_mode"), "set_mode does not apply to an OpenCode session");
  assert.ok(!/list_agents/.test(INSTRUCTIONS), "no such tool exists");
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

test("the context block carries the remembered narration mode", () => {
  // Design section 6: a fresh voice session knows the mode without being told,
  // which is what makes OPERATING's "if it's already quiet don't apologise for
  // being quiet" actionable.
  const base = { home: "h", memory_user: "", journal_today: "", active_missions: [], agents: [] };
  const quiet = yuriContextBlock({ ...base, narration_mode: "quiet" });
  assert.ok(quiet.includes("YOUR NARRATION MODE: quiet"));
  assert.ok(yuriContextBlock({ ...base, narration_mode: "verbose" }).includes("verbose"));
  // Unvalidated input crosses the network: render nothing rather than a lie.
  for (const bad of [undefined, null, "", "loud", 7 as unknown as string]) {
    const out = yuriContextBlock({ ...base, narration_mode: bad });
    assert.ok(!out.includes("NARRATION MODE"), String(bad));
  }
});
