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

### `opencode attach --session` does NOT open that session ❌
I recorded the opposite here first, from reading `opencode attach --help`, and
then measured it. **`--session` does not open that session's conversation** in
1.18.25. Tested against a live server with a marker prompt already in the
session:

| invocation | marker visible |
|---|---|
| `attach <url> --session <id>` | no |
| `attach … --session <id> --mini` (has replay) | no |
| `attach … --session <id> --mini --replay-limit 20` | no |
| `attach … --continue` | no |
| `attach … --dir <cwd> --session <id>` | no |
| `opencode --session <id>` (root TUI, same cwd) | no |
| + 14s wait, + Escape, + ctrl-r | no |

Every route lands in a **new** session — the TUI's own rename dialog confirms
it ("New session - <timestamp>"). The TUI renders fine, so this is not a
rendering artefact; the conversation simply is not there.

**Consequences, both acted on:**
- **No "Watch live" for OpenCode.** The plan was a tmux pane running
  `opencode attach`, streamed through the existing terminal websocket. The
  plumbing worked — lazily created, idempotent, killed on `stop()` — but it
  showed *a different conversation*, so it was reverted rather than shipped. A
  button labelled "Watch live" that shows the wrong session is worse than no
  button.
- **`OpenCodeProvider.resume_command` returns None.** It briefly returned
  `opencode attach <url> --session <id>` with a comment claiming it reached
  the same session. That claim was wrong; a command that promises to reopen
  this session and quietly opens another is worse than none.

What *does* work is `opencode attach <url>` as a way to get a terminal on the
server Yuri is using — but it is not session-scoped, so it does not belong on
a session card. Revisit if a later OpenCode makes `--session` effective.

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
