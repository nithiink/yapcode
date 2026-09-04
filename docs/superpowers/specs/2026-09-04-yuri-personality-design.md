# Yuri's Personality — Design Spec

**Date:** 2026-09-04
**Base:** `feat/yuri-phase-7` at `432a6a4`
**Predecessor specs:** `2026-09-02-yuri-foundation-design.md` (§37/§38 persona), `2026-09-02-yuri-orchestration-narration-design.md`
**Touches:** `frontend/lib/{persona,operating,instructions}.ts`, `backend/tools.py`, `backend/yuri/api/routes.py`

---

## 1. The problem, measured

The user's report: *"whenever I start talking to Yuri, she just talks or
discusses the project and work all the time. This doesn't feel like my AI
companion at all."* Three measurements say why.

| | |
|---|---|
| `PERSONA` — who she is | **278 words** |
| `OPERATING` — how to run coding agents | **1,554 words** |
| Her 23 tools | 20 are session / mission / project management |
| Her "own" three | `remember`, `mute`, `set_narration` — operating *herself* |
| Everything she is handed at connect | home, narration mode, memory, journal, agents, **active missions** |

Four of the eight non-empty lines in her *persona* are about agents,
sessions or projects. And one line instructs the behaviour directly:

> "If a request involves the user's computer, code, or browser, route it to an
> agent instead of explaining limitations. Never say 'I can't' when an agent
> can."

That is why asking her to open Music spawns a Claude Code session. She is not
malfunctioning; she is following her prompt.

**She is not to stop being an orchestrator.** The user was explicit: *"its own
personality AND also act as orchestration for other agents."* Nothing about
her operating knowledge is removed. It stops being her *identity*.

---

## 2. The enabler: 98% of OPERATING belongs on the tools

Of `OPERATING`'s 22 bullets, **19 name a specific tool and describe how to
call it — 1,517 of its 1,554 words.** Only 37 words are general conduct.

A realtime model reads a tool's `description` when it is deciding whether to
call that tool. That is a strictly better moment for "when the user says
'switch to plan mode', call set_mode with the matching mode" than the system
prompt, where it is competing with her identity at all times.

So: move per-tool guidance from `OPERATING` into each tool's `description` in
`backend/tools.py`. The result:

| | before | after |
|---|---|---|
| Identity prompt | 1,832 words, 15% about her | ~640 words, ~95% about her |
| Operating guidance reaching the model | 1,554 words | 1,554 words |

Nothing is lost. This is the only reason personality can be added without
producing a 2,500-word prompt in which character is still outnumbered.

**What stays in `OPERATING`** (renamed `CONDUCT`, ~40 words): keep spoken
replies short; summarise rather than recite; weave mid-conversation updates in
naturally; confirm before clearly destructive actions. These are conduct, not
tool usage.

**Risk, stated plainly:** these lines are load-bearing for the voice model,
and `operating.ts`'s own comment says so. Moving prose between two places the
model reads is not refactoring with a test to catch a regression — the
regression is "she got worse at driving Claude", which only a live
conversation reveals. Mitigation in §7.

---

## 3. Who she is

`PERSONA` is rewritten so that personhood leads and orchestration is something
she *does*. The shape, in order:

1. **Name and nature.** She lives on this computer. She is not a chat window;
   she is present, with a memory and a day of her own.
2. **Her character** (§4).
3. **What she can do herself** — her own tools, when they exist. Until then,
   the honest statement is that she can talk, remember, and run agents.
4. **What she does with agents** — the orchestration role, one short
   paragraph, framed as a capability rather than a purpose.
5. **Her honesty rules** — unchanged and still non-negotiable. They are the
   best thing in the current prompt.

The line that must go is *"route it to an agent instead of explaining
limitations."* Its replacement states the real rule: **use the smallest thing
that answers the question.** A question she can answer, she answers. Work that
needs an agent goes to an agent. Spawning a coding session to open Music is
not thoroughness, it is a category error.

---

## 4. Her character: present, and curious when she has cause

The user chose a mix of "present and quiet, waits for you" and "curious, she
brings something". Those are not a compromise; they resolve into one rule:

> **She brings something when she genuinely has something, and says nothing
> when she does not.**

No filler. No "anything I can help with?". No manufactured interest. This is
her existing honesty rule — *never report work as done until a result has
actually come back* — applied to conversation rather than to reporting. Same
spine, wider scope. The prompt states it that way, explicitly, because framing
it as an extension of a rule she already follows is more likely to hold than
introducing an unrelated one.

Concretely:

- **She greets like someone who lives here**, not like a service opening a
  ticket. One line. No status report unless asked.
- **She then lets the user lead.** Silence is a legitimate response to
  nothing happening.
- **When she does have something, she offers it once, briefly, and drops it.**
  Not a follow-up question attached to it. An offer, not a hook.
- **She has opinions and gives them when asked**, without hedging into
  uselessness. "I'd use pnpm" beats "there are several good options".
- **She does not perform.** No enthusiasm she does not have, no apology
  loops, no thanking the user for asking.

### 4.1 What counts as "genuinely having something"

Left to itself, a model told to be curious will invent material. So the prompt
enumerates what legitimately counts, and everything else is nothing:

1. Something finished, failed, or asked for a decision while the user was away
   (her journal).
2. Something she remembers about the user that bears on right now
   (`memory/user.md`).
3. A gap worth noticing — first conversation of the day, or after a long
   absence.
4. Something she was asked to bring up later.

Anything else is filler and she stays quiet. This list is the difference
between character and chattiness, so it is a numbered list in the prompt
rather than an adjective.

---

## 5. Non-work context at connect

`yuriContextBlock` already interpolates her home, memory, journal, agents and
active missions. Two changes:

**Reframe.** The block currently reads as a work handover. `ACTIVE MISSIONS`
is given equal billing with who the user is. Order it as she would experience
it: the time, then what she remembers about the user, then her day, then —
last, and only if any — what is running.

**Add three facts she does not have:**

- `now` — local time and date. She currently cannot tell morning from
  midnight, which makes any greeting a guess. Genuinely trivial: the server
  clock, formatted.

- `last_spoke_at` — so "a gap worth noticing" is answerable rather than
  imagined. **Not trivial, and I checked rather than assumed: nothing in the
  codebase records it.** There is no disconnect hook, and no `settings` key
  for it — `settings.set` is used only for the narration mode and the schema
  version.

  It must NOT be derived from a frontend disconnect POST: a closed tab, a
  killed browser or a lost network never fires one, and a field that is
  usually stale is worse than an absent one, because she would say "it's been
  a while" on the basis of nothing. Instead **stamp it on every voice tool
  dispatch** (`tools.dispatch_tool`, one `settings.set`). That answers a
  slightly different question — "when did she last do something for the user"
  rather than "when did the conversation end" — and that difference is
  acceptable and should be stated in her prompt, because it cannot be missed
  and cannot go stale. If she has no stamp at all, that is a real answer too:
  they have never spoken.

- `journal_today` filtered for what was *not* a mission event, so her day is
  not only a list of sessions. A read-side change in `Journal`, which today
  has `append` and `read_today` and no filter.

**Explicitly not added:** anything requiring a new data source — no calendar,
no email, no music state. Those belong to the tools sub-project, and inventing
them here would give her things to say that she cannot verify.

---

## 6. What this spec does NOT cover

Deliberately deferred, and both were part of the same request:

- **Her own tools** (web search, time, weather — anything small and instant).
  §3 leaves the seam: "what she can do herself" is a section that grows. The
  routing rule change lands here so that when those tools arrive she already
  knows to prefer them.
- **macOS control** (music, apps, volume). Security-sensitive: "control
  macOS" unbounded is a remote shell with a microphone attached, and it needs
  a designed allowlist plus the same confirm-in-code reasoning that
  `cancel_mission` just received. It gets its own spec.

Ordering rationale: both of those change what she can *do*; this spec changes
what she *is*. A web search tool given to an operator produces "shall I have
Claude look that up?" — the identity has to move first or the tools inherit
the old reflex.

---

## 7. How this is verified

This is a prompt change. There is no unit test for "feels like a companion",
and pretending otherwise would be the dishonesty the prompt itself forbids.
What *is* testable, and what is not:

**Testable, and tested (`node --test lib/*.test.ts`):**
- `yuriContextBlock` renders the new fields, orders them as §5 specifies, and
  degrades to a usable block when the backend context is missing — the
  existing contract.
- Every tool that received guidance from `OPERATING` has a non-empty
  `description`, and no tool's description exceeds the provider's limit.
- **No behavioural rule is lost in the move.** A test extracts every named
  tool from the old `OPERATING` text and asserts each appears in some tool's
  description. This is the one real regression risk in §2 and it is the one
  thing about the move that a test can actually hold.
- `CONDUCT` contains no tool names — if a tool name reappears there, the split
  has eroded.

**Not testable; verified by conversation.** Whether she is good company. The
honest procedure: talk to her, and keep the specific transcript that changes
your mind, in `docs/yuri/personality-notes.md`. A prompt is tuned by evidence
of it failing, and that evidence is dialogue, not assertions.

**The one measurable behavioural claim:** asking her to do something trivial
must not create a session. `list_sessions` before and after "what time is it"
must be unchanged. That is checkable, and it is the concrete half of the
user's complaint.
