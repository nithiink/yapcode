# Task 8 — live check against real OpenCode

`opencode serve` 1.18.25 · macOS · 2026-09-03 · four providers authenticated
(OpenRouter api, Google oauth, omniroute api, AIHubMix api).

## Settled

### A real turn completes end to end ✅
Through the shipped provider, not curl: spawn → `create_session` → `send_message`
→ poll → `completed` with the assistant's text (`'PONG'`), then `shutdown`
stopped the server it started.

### The successful-turn event vocabulary (spec §2.1's open question) ✅
```
session.next.prompt.admitted
session.next.prompted
session.next.step.started
session.next.text.started
session.next.text.ended
session.next.step.ended
```
A turn ends with **`session.next.step.ended`** — *not* `…step.completed`, which
is what the plan and every draft of the mapping assumed. Reading completion
from an event type would have hung every turn at `working`. Reading it from the
message's `finish` is why the first live turn worked.

The assistant message carries `finish: "stop"` with top-level `text` **null** —
the text is in its parts — so `_assistant_text`'s structure-walking is
load-bearing.

### Failure mapping ✅
A model with no credentials produces
`session.next.prompt.admitted → prompted → step.started → step.failed`, and the
provider reports `error` carrying the server's own message
(`Provider request failed with HTTP 401: … missing_api_key`).

### Attach / spawn / ownership ✅
`health()` reports the not-yet-running case as **online** because it is
spawnable (the fix the whole-branch review forced); `create_session` spawns;
`owned` is `True`; `shutdown()` stops it; a stray server is never left behind.

### The model is transmitted correctly ✅
`OPENCODE_MODEL=provider/model` is parsed and stored by the server as
`{"id": …, "providerID": …, "variant": "default"}`. With no model set, the
session's `model` is `null` and OpenCode picks its own default — which on this
machine is an `opencode/*` free model that **401s**, so `OPENCODE_MODEL` is
effectively required.

### Environment filtering ✅
`VC_AUTH_TOKEN` never reaches the child. `OPENCODE_INHERIT_KEYS=1` restores the
voice keys and still does not pass `VC_AUTH_TOKEN`. Note it does **not** fix a
model 401 — OpenCode authenticates from its own `opencode auth login` store,
not from these variables, so the default (strip) costs nothing here.

### The auth mechanism — RESOLVED, and the guess was wrong ✅
`opencode serve` with `OPENCODE_SERVER_PASSWORD` set:

| sent | result |
|---|---|
| no header | 401 |
| `x-opencode-password: <pw>` — **what the client sent** | 401 |
| Basic with an empty username | 401 |
| `Basic opencode:<pw>` | **200** |

It is HTTP Basic, and the username is load-bearing. Every password-protected
setup would have failed with a hard error. The guess was wrong in production
and *right in the tests*, because the fake had been written to agree with it —
an agreeable fake is worse than no fake. Fixed in `_headers()`, the one
function Task 2 isolated it into, and the fake now enforces the real thing.
`opencode attach --username` documents the same `opencode` default.

### Opening one session in a terminal: `opencode -s <id> --mini` ✅
**The user found this after I concluded it was impossible, and they were
right.** My matrix had a hole: I tested `attach --session --mini` and the root
TUI *without* `--mini`, but never the root TUI *with* it — which is the one
combination that works.

| invocation | opens the named session |
|---|---|
| `attach <url> --session <id>` | no — lands in a new session |
| `attach … --session <id> --mini` | no |
| `attach … --continue` / `--dir <cwd> --session <id>` | no |
| `opencode -s <id>` (root TUI, no --mini) | no |
| **`opencode -s <id> --mini`** | **yes** |

`--mini` is the interface that replays history. Sessions are **global** in
OpenCode's store, not directory-scoped (`opencode session list` returns the
same list from any cwd), so the cwd only decides which project the TUI opens
on. This is now what `resume_command` returns.

### Seeing a live OpenCode session: the transcript panel ✅
Chasing "can I watch it?" produced the right answer, which was never the
terminal. `read_transcript` called Claude Code's on-disk JSONL reader directly
for every session, so an OpenCode session's transcript panel was always empty
— the same "every session is Claude Code" assumption as the resume command.
`transcript()` is now a provider method: Claude Code reads its JSONL, OpenCode
reads `/api/session/{id}/message`, which is the same source `poll` reads. The
panel polls every 2.5s, so the turn appears as it lands.

Deliberately the API and not OpenCode's SQLite store, because a live session's
messages are not reliably written there while it runs (below).

### A REAL BUG this uncovered: the message API is newest-first ✅ fixed
`/api/session/{id}/message` returns **newest first** — measured. `poll`'s
`msg_seen` high-water mark slices `messages[msg_seen:]`, which assumes
oldest-first, so it took the OLDEST entries as "fresh". Observed live before
the fix:

    TURN1: completed  text='AAA1'
    TURN2: completed  text=''        <- previous turn's leftovers, empty text

**The fake had been appending oldest-first and hid it** — the same
agreeable-fake failure as the auth header. The fake now returns newest-first
like the server, and reverting the ordering fix fails **six** tests, including
every exactly-once guarantee. Those guarantees were previously passing while
production was broken. After the fix, against the real server:

    TURN1: completed  text='AAA1'
    TURN2: completed  text='BBB2'

Also observed: a third message type, `system` (carrying "Skills"), which is
ignored rather than rendered.

### Still no "Watch live" in the browser ❌
Not because the view is wrong — because it is a **separate process reading the
shared store**, and a live session's messages are not reliably persisted while
Yuri drives it. Measured, with the server still running and a turn just
completed:

| session | rows in `message` | TUI shows it |
|---|---|---|
| live, provider-driven, turn completed | **0** | no |
| an older session with committed messages | 2 | yes (fully) |

Three separate live runs persisted 0 messages, in `/tmp` and in a fresh
project directory, before and after a graceful server stop. So a pane would be
empty exactly when someone wanted to watch. The reverted implementation is in
the history if this changes: lazily created, idempotent, killed on `stop()`,
password passed by environment rather than argv (a tmux command line is
world-readable via `ps`).

## Not settled

- **A model that authenticates but never starts.** `google/gemini-2.5-flash`
  completes fine under `opencode run`, but through `serve` + `POST /prompt` the
  turn stops after `session.next.prompted` — no `step.started`, no assistant
  message, no error. Same code path that works with
  `opencode/nemotron-3-ultra-free`, so it is model/provider-specific and
  **outside Yuri**: she reports `working` forever, which is honest but
  indistinguishable from a slow turn. Worth a provider-side turn timeout later.
- **A real permission prompt** — not yet triggered live, so the once/reject
  wire format and whether OpenCode drops an answered request from its pending
  list are still unproven (see `docs/yuri/follow-ups.md`).
- **The question reply body shape** — still a guess by symmetry.
- **A password-protected end-to-end turn.** Auth itself is now measured and
  fixed, but no full turn has run against a secured server.

## Configuration that works

```
YURI_AGENTS=claude-code,opencode
OPENCODE_MODEL=opencode/nemotron-3-ultra-free
```
