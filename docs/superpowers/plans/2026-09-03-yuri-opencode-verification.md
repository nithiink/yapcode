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
- **The client-side auth header** — untested; no password-protected server run.

## Configuration that works

```
YURI_AGENTS=claude-code,opencode
OPENCODE_MODEL=opencode/nemotron-3-ultra-free
```
