// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { INSTRUCTIONS, yuriContextBlock } from "./instructions.ts";
import { CONDUCT } from "./operating.ts";

test("INSTRUCTIONS is who she is, then how she conducts herself", () => {
  assert.ok(INSTRUCTIONS.startsWith("You are Yuri"));
  assert.ok(INSTRUCTIONS.includes("HOW YOU WORK:"), "conduct is missing");
  assert.ok(!INSTRUCTIONS.includes("You are the VOICE for Claude Code"));
});

test("the identity prompt is mostly about her, not about driving agents", () => {
  // The whole point of the split. It used to be 278 words of who she is
  // against 1,554 of how to run coding agents, and she talked about work
  // because work was nearly all her prompt was about. If this ratio drifts
  // back, so will she.
  const words = INSTRUCTIONS.split(/\s+/).length;
  assert.ok(words < 900, `identity prompt is ${words} words; it is growing back`);
  const workish = (INSTRUCTIONS.match(/session|agent|Claude|mission|tool/gi) || []).length;
  assert.ok(workish / words < 0.06,
    `${((workish / words) * 100).toFixed(0)}% of her identity prompt is work vocabulary`);
});

test("CONDUCT names no tools at all", () => {
  // Per-tool guidance lives on each tool's own description (backend/tools.py),
  // where the model reads it when it is deciding to call that tool. A tool
  // name appearing here means the split has eroded and the prompt is
  // absorbing tool manuals again. The backend has the matching test that
  // every moved rule actually arrived.
  for (const t of ["start_session", "tell_claude", "answer_prompt", "interrupt_session",
                   "set_mode", "send_keys", "mute", "set_narration", "list_sessions",
                   "run_slash_command", "get_handoff", "peek_screen", "rename_session",
                   "list_missions", "cancel_mission", "pause_mission"]) {
    assert.ok(!CONDUCT.includes(t), `${t} is back in CONDUCT`);
  }
});

test("she is told what she can actually do, and not to fake the rest", () => {
  // The line that caused the reported bug was "route it to an agent instead
  // of explaining limitations", which is why asking her to open Music started
  // a Claude session.
  assert.ok(!/route it to an agent/i.test(INSTRUCTIONS));
  assert.ok(/smallest thing that answers/i.test(INSTRUCTIONS),
    "the replacement routing rule is missing");
});

test("her curiosity is bounded by an enumerated list, not an adjective", () => {
  // A model told to be curious invents material. The prompt names what counts
  // as genuinely having something; without the list, "be interested" produces
  // filler, which is the failure mode most likely to feel fake.
  assert.ok(/Anything else is filler/i.test(INSTRUCTIONS));
  for (const n of ["1.", "2.", "3.", "4."]) {
    assert.ok(INSTRUCTIONS.includes(n), `the list lost item ${n}`);
  }
});

test("context block is empty when the backend is unreachable", () => {
  assert.equal(yuriContextBlock(null), "");
  assert.equal(yuriContextBlock(undefined), "");
});

test("context block leads with the moment and the person, not the work", () => {
  const out = yuriContextBlock({
    home: "/Users/x/Yuri",
    now: "Thursday 04 September, 11:52",
    last_spoke_at: "2026-09-04T09:10:00Z",
    memory_user: "- 2026-09-02  prefers pnpm",
    journal_today: "- 09:00  he mentioned his sister is visiting",
    active_missions: [{ id: "m", title: "fix", goal: "make tests pass", status: "running", project: "pm-tool" }],
    agents: [{ id: "claude-code", name: "Claude Code", online: false }],
  });
  assert.ok(out.includes("RIGHT NOW: Thursday 04 September, 11:52"));
  assert.ok(out.includes("prefers pnpm"));
  assert.ok(out.includes("YOUR DAY SO FAR"));
  assert.ok(out.includes("sister is visiting"));
  assert.ok(out.includes("Claude Code: OFFLINE"));
  assert.ok(out.includes('"fix" · pm-tool · running · goal: make tests pass'));

  // Order is the point: she should meet the moment and the person before the
  // work. The old block opened with her home and gave missions equal billing.
  assert.ok(out.indexOf("RIGHT NOW") < out.indexOf("WHAT YOU REMEMBER"));
  assert.ok(out.indexOf("WHAT YOU REMEMBER") < out.indexOf("WORK RUNNING"));
});

test("with nothing running, the block says nothing about work at all", () => {
  // An empty work section is not a conversation starter, and "ACTIVE
  // MISSIONS: none" invited her to report on it.
  const out = yuriContextBlock({
    home: "h", now: "Thursday 04 September, 11:52", last_spoke_at: null,
    memory_user: "", journal_today: "", active_missions: [], agents: [],
  });
  assert.ok(!/WORK RUNNING/.test(out));
  assert.ok(!/MISSION/i.test(out));
  assert.ok(/nothing worth mentioning yet/.test(out), "a quiet day should say so plainly");
  assert.ok(/first time/i.test(out), "never having spoken is a usable fact, not a blank");
});

test("a missing time or last-spoke degrades without inventing one", () => {
  const out = yuriContextBlock({
    home: "h", memory_user: "", journal_today: "", active_missions: [], agents: [],
  });
  assert.ok(!/RIGHT NOW/.test(out), "rendered a time it does not have");
  assert.ok(/first time/i.test(out));
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
