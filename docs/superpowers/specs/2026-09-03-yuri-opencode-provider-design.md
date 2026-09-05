# Yuri — the OpenCode provider (Phase 5) — Design Spec

**Date:** 2026-09-03
**Base:** `main` at `c80f8b8` (phases 1–4 merged)
**Source plan:** `~/Downloads/yuri-plan.md` — phase 5, plus §5 (OpenCode provider), §7 (registry), §18 (agent selection), §41 (error isolation).
**Predecessors:** `docs/superpowers/specs/2026-09-02-yuri-foundation-design.md`, `…-orchestration-narration-design.md`
**Carries:** `docs/yuri/follow-ups.md`

---

## 1. What this increment is

Claude Code has run behind `AgentProvider` since phase 2, but nothing else ever
has — so the abstraction is unproven. This adds **OpenCode** as the second real
provider: a headless HTTP agent Yuri can start a mission on, drive by voice,
approve actions for, and narrate, using the same domain, the same approval
workflow and the same narration policy as Claude.

It is also the prerequisite for the thing the original plan describes as the
target product (§57): *"Have OpenCode implement it and Claude review it."*

**Out of scope:** Hermes · Mission Control UI · multi-agent missions (phase 7)
· `YuriSelfProvider` · semantic memory · cost-aware routing · parallel agents ·
the §33 file moves · renaming `VC_*`.

---

## 2. What was actually observed (not assumed)

Everything here was verified against a live `opencode serve` (v1.18.25) on
2026-09-02 by creating real sessions and sending real prompts. **Several of the
source plan's assumptions about OpenCode are wrong, and so is part of
OpenCode's own OpenAPI document.** Where they disagree, observed behavior wins.

| Fact | Evidence |
|---|---|
| `/api/*` responses are wrapped in **`{"data": …}`** | every call; nothing in the plan mentions it |
| The OpenAPI `Session` schema advertises `directory`; the **real object has `location.directory` and `subpath`** | `GET /api/session/{id}` returned keys `cost, id, location, projectID, subpath, time, title, tokens` |
| `ModelRef` is **`{providerID, id}`** — not `modelID` | passing `modelID` → `InvalidRequestError: Missing key at ["model"]["id"]` |
| `GET /api/session/{id}/event` is a **long-lived SSE stream**, not pollable | a plain GET returned no JSON and held the connection |
| `GET /api/session/{id}/history?after=N` is the **finite, pollable** page | returned a JSON array of durable events |
| Each durable event carries **`durable.seq`** — a monotonic per-session cursor | `{id, type, durable:{aggregateID,seq,version}, data:{…}}` |
| `POST /api/session/{id}/prompt` is **non-blocking and returns `admittedSeq`** | `{admittedSeq:1, id:"msg_…", sessionID, prompt, delivery, timeCreated}` |
| `PermissionV2Reply` is **`once` \| `always` \| `reject`** | component schema |
| Sessions carry **`cost` and `tokens`** (`{input,output,reasoning,cache:{read,write}}`) | session object |
| Server auth is **not in the OpenAPI** (`securitySchemes` is empty); the server warns `OPENCODE_SERVER_PASSWORD is not set; server is unsecured` | startup log |
| Error shape: `{"_tag":"InvalidRequestError","message":…,"kind":"Payload"}` | malformed requests |
| A provider failure surfaces as a **narratable message** | `session.next.step.failed` with `data.error.message = "Provider request failed with HTTP 401: … Model … is not supported"` |
| Models are `provider/model` from `opencode models`; 4 providers authenticated on this machine | `opencode providers list` |

### 2.1 The event vocabulary is only partly known — design for that

Observed on a failing turn: `session.next.prompt.admitted` → `session.next.prompted`
→ `session.next.step.started` → `session.next.step.failed`.

**A successful turn's events (tool calls, step completion) were never captured** —
every model reachable during the probe either returned 401 or produced nothing.

**RESOLVED 2026-09-03** against `opencode serve` 1.18.25 with
`opencode/nemotron-3-ultra-free`, via the shipped provider. A successful turn
emits, in order:

    session.next.prompt.admitted
    session.next.prompted
    session.next.step.started
    session.next.text.started
    session.next.text.ended
    session.next.step.ended

The decision below is **vindicated, not merely unharmed**: a successful turn
ends with `session.next.step.ended`, a type nobody had guessed — the plan and
every draft of the mapping had assumed `session.next.step.completed`. Had
completion been read from an event type, it would never have fired and every
turn would have hung at `working`. Reading it from the message's `finish`
instead is what makes the live turn work on the first try.

Also observed: the assistant message carries `finish: "stop"` with its
top-level `text` field **null** — the text lives in the message's parts — so
`_assistant_text`'s structure-walking is load-bearing, not defensive padding.

This is a stated limitation, not a gap to guess past. Consequence for the
design: the event mapping is **defensive by construction** — known types map,
**unknown types are ignored**, and a turn's completion is decided from
`GET /api/session/{id}/message` (`finish`, `content`) rather than from an event
type we have not seen. The implementation discovers the remaining types against
a model that works, and adding one later must be a table entry, never a
restructure.

---

## 3. Decisions taken before design

| Decision | Choice | Why |
|---|---|---|
| Server lifecycle | **Attach if reachable, else spawn** | Works out of the box, but yields to a server the user runs. |
| API generation | **`/api/*`** | Near-exact contract fit: durable non-blocking prompt, a resumable cursor, permissions and questions separated. |
| Event ingestion | **Poll `history?after=<cursor>`** | Fits `poll()` exactly, needs no background task per session, and the cursor gives exactly-once. OpenCode therefore reports `supports_events=False`, so narration uses the poll-owned path — the better-tested half. |
| `set_mode` | **Unsupported, and said so** | OpenCode has no equivalent of Claude's four modes. `NotImplementedError` → the soft error `tools.py` already produces. |
| `delivery` | **Always `queue`** | Matches the queueing the Claude runners already do and that the voice prompt describes ("a rapid second tell_claude doesn't drop the first"). `steer` is a future capability, not a default. |
| Spawned server scope | **One shared server, sessions scoped by `location.directory`** | Observed: one server serves many directories (`projectID` was `global` for a path outside any project). One process, one health check, one lifecycle. |
| Approval mapping | **allow → `once`, deny → `reject`.** Never `always`. | A spoken "yes" must not silently grant standing permission. `always` would be a mode change disguised as an answer. |

---

## 4. Server lifecycle

`backend/yuri/providers/opencode/server.py` — `OpenCodeServer`.

```
attach:  GET <OPENCODE_URL>/api/session   →  200?  →  adopt, owned=False
spawn:   OPENCODE_SPAWN enabled and no server answered
         →  opencode serve --port <p> --hostname 127.0.0.1
         →  poll readiness, owned=True
```

**The governing rule: Yuri never stops a server she did not start.** `owned` is
set once, at acquisition, and `shutdown()` terminates the process only when
`owned` is true. A user's server survives Yuri's restart, her shutdown, and her
crash. This is the whole reason the attach branch exists, and it gets its own
test.

Other rules:

- **Acquisition is lazy and once.** Nothing runs `opencode serve` at Yuri
  startup; the first call needing the server acquires it, guarded by an
  `asyncio.Lock` so two concurrent `start_session`s cannot spawn two servers.
- **Readiness is a probe, not a sleep** — poll `GET /api/session` until it
  answers, with a bounded timeout, then fail with an actionable message naming
  the binary and the port.
- **A dead spawned server is re-acquired** on the next call rather than
  poisoning the provider. A dead *attached* server is reported offline; Yuri
  does not take over someone else's port.
- **Spawn is refusable.** `OPENCODE_SPAWN=0` makes the provider attach-only, so
  a user who wants to own the process always can.
- The spawned process is started with `cwd` set to the first allowed project
  root, inherits no Yuri secrets, and its stdout/stderr go to a log under
  `~/Yuri` (not the terminal, which belongs to the voice UI).

---

## 5. The provider

`backend/yuri/providers/opencode/provider.py` — `OpenCodeProvider(AgentProvider)`,
`id="opencode"`, `name="OpenCode"`. HTTP via `httpx` (already a dependency).
`client.py` holds the envelope-unwrapping, auth and error translation, so
`provider.py` reads as contract methods.

| Contract | OpenCode | Notes |
|---|---|---|
| `create_session` | `POST /api/session` `{location:{directory}, model?:{providerID,id}}` | returns `ses_…`; the handle is that id |
| `send_message` | `POST /api/session/{id}/prompt` `{prompt:{text}, delivery:"queue"}` | non-blocking; store the returned `admittedSeq` |
| `poll` | `GET /api/session/{id}/history?after=<cursor>` then, on a terminal event, `GET /api/session/{id}/message` | §6 |
| `answer` | `POST …/permission/{requestID}/reply` `{reply}` or `…/question/{requestID}/reply` | §7 |
| `interrupt` | `POST /api/session/{id}/interrupt` | |
| `stop` | forget the handle; **do not delete the session** | OpenCode sessions are durable and the user may resume one; deleting is destructive and not ours to do |
| `set_mode` | — | `NotImplementedError` |
| `read` | `GET /api/session/{id}/message` → concatenated assistant text | |
| `peek` | — | returns `None` (no TUI), exactly as the SDK backend does |
| `send_keys` / `run_slash` | — | `NotImplementedError` |
| `resume` | — | `NotImplementedError`; adoption is Claude-specific |
| `list_native` | `GET /api/session` filtered to handles we own | plus `cost_usd` from the session's `cost` |
| `native_pane` | — | `None` |
| `rehydrate` | `GET /api/session` | see §8 |
| `health` | `GET /api/session` against the acquired base URL | reports attached-vs-spawned in `detail` |
| `shutdown` | stops the server **only if `owned`** | the safety-critical row: an attached server must survive Yuri's shutdown (§4). Always forgets local handle state, whether owned or not. |
| `set_observer` | stored, never invoked | the ABC requires it; because we poll (`supports_events=False`) no observer event is ever emitted. Storing it rather than raising keeps `build_container`'s uniform wiring working. |
| `backend_of` | returns `None` | OpenCode has no per-handle backend split (Claude's `cli`/`sdk`); `list_native` tags rows with the provider id instead. |
| `capabilities` | §5.1 | |

### 5.1 Capabilities

```python
AgentCapabilities(
    interactive_terminal=False, slash_commands=False, send_keys=False,
    permission_modes=(),          # OpenCode has no equivalent
    supports_interrupt=True,
    supports_rehydrate=True,      # durable sessions survive a Yuri restart
    supports_resume=False,
    supports_events=False,        # we poll the cursor; narration uses the poll path
    cost_tracking=True,
)
```

`permission_modes=()` is what makes `set_mode` a clean soft error instead of a
lie, and `supports_events=False` routes narration down the poll-owned path the
previous phase tested hardest.

---

## 6. Polling: the cursor and the result

Per handle the provider keeps `cursor: int` (from `admittedSeq`, then the
highest `durable.seq` seen) and the in-flight `messageID`.

`poll()` fetches `history?after=cursor`, advances the cursor past everything
returned, and maps to the existing result contract:

| Observed / expected event | Result |
|---|---|
| nothing new, turn in flight | `{"status":"working"}` |
| nothing new, no turn | `{"status":"idle"}` |
| a pending permission (§7) | `{"status":"needs_permission", "prompt":{…}}` |
| a pending question (§7) | `{"status":"needs_choice", "prompt":{…}}` |
| `session.next.step.failed` | `{"status":"error", "error": data.error.message}` |
| turn finished (from `message`: `finish` set, `type=="assistant"`) | `{"status":"completed", "assistant_text": …}` |
| any unrecognised type | ignored; cursor still advances |

Two properties this must have, each tested:

1. **Exactly-once.** Advancing the cursor only past events actually returned
   means a repeated `poll()` never re-reports a completed turn — the bug the
   tmux backend needs its `_pending_results` FIFO to avoid.
2. **A crash does not lose a turn.** The cursor lives in the provider's memory,
   so a Yuri restart re-reads from the session's stored cursor (§8) rather than
   from zero — re-narrating history would be the failure mode.

`assistant_text` is capped the way the Claude path caps it, so narration's own
clipping is never the only guard.

---

## 7. Permissions and questions

OpenCode asks; **Yuri owns the workflow** (plan §20). Pending requests are read
from `GET /api/session/{id}/permission` and `…/question` and surfaced as the
`Prompt` shape the domain already understands, so `ApprovalService.record_request`,
the `risk_for` labelling, the one-pending-approval invariant and the voice
approval flow all work unchanged.

| Yuri | OpenCode |
|---|---|
| `allow` | `reply: "once"` |
| `deny` | `reply: "reject"` |
| — | `always` is **never sent** |

That last row is a safety rule, not an omission: `decide_permission` answers a
single question, and `always` would turn one spoken "yes" into a standing grant
the user never agreed to. Granting standing permission is a mode change, and
OpenCode has no mode we expose.

`request_id` comes from OpenCode's request id, so the content-addressed
synthesis added in the foundation stays a fallback rather than the norm.
Questions map to `needs_choice` with OpenCode's options.

---

## 8. Restart, rehydration and the store

OpenCode sessions are durable server-side, so unlike an SDK session they can be
re-adopted. `rehydrate()` lists sessions and re-adopts those whose
`AgentSession` rows exist, restoring each cursor from
`runtime_metadata["opencode_cursor"]` — which `SessionService` already persists
per row, so no schema change is needed.

A row whose OpenCode session is gone becomes `lost`, exactly as the Claude path
does. A session that exists on the server but has no row is left alone: it may
be the user's own work in their own OpenCode, and adopting it would put Yuri in
charge of something she was never asked to run.

---

## 9. Configuration

```env
YURI_AGENTS=claude-code,opencode      # the registry already reads this
OPENCODE_URL=http://127.0.0.1:4096    # attach target, and the spawn port
OPENCODE_SPAWN=1                      # 0 = attach-only, never spawn
OPENCODE_BIN=opencode                 # resolved on PATH if unset
OPENCODE_SERVER_PASSWORD=             # sent to the server when set
OPENCODE_MODEL=                       # "provider/model"; unset = OpenCode's default
```

Config is read through `backend/config.py` beside the existing `YURI_*` keys, so
provenance reporting and `yuri doctor` pick it up for free. `yuri doctor` gains
an OpenCode line: binary present, URL reachable, attached-or-spawnable.

**Auth is deliberately unresolved here.** The OpenAPI declares no security
scheme, so the mechanism (a header, HTTP Basic, a query param) must be
determined empirically against a password-protected server before
`OPENCODE_SERVER_PASSWORD` is claimed to work. Until then the provider sends it
by the mechanism the implementation verifies, and the spec is honest that this
is the one config key not yet proven. It is never logged.

---

## 10. What must keep working

- Every existing voice flow. `backend/tests/test_tools_dispatch.py` remains the
  result-key contract; no existing tool's keys change.
- **`claude-code` stays the default agent.** Adding a provider must not change
  which agent an unqualified request gets. `AgentRouter`'s order already does
  this; a test pins it.
- **Provider failures stay isolated** (plan §41). OpenCode being absent,
  unreachable or broken must not degrade Claude sessions, narration, missions or
  startup — `AgentRegistry.health_all` already tolerates a failing provider, and
  `_native_map`'s per-provider guard already survives one that raises.
- 424 backend + 32 frontend tests, pristine, in both `~/Yuri` states.

---

## 11. Testing

The contract suite exists and is the point: `OpenCodeProviderContractTest`
subclasses `AgentProviderContract` (foundation phase) against a **fake OpenCode
HTTP server** — a `http.server` handler in-process, no new dependency —
returning the envelopes and event shapes recorded in §2. That is what makes
"the abstraction is proven" a test result rather than a claim.

- `test_opencode_server.py` — attach when reachable; spawn when not; **never
  terminates an attached server**; concurrent acquisition spawns once;
  `OPENCODE_SPAWN=0` refuses to spawn; readiness timeout is actionable.
- `test_opencode_client.py` — envelope unwrapping; `InvalidRequestError` →
  `ValueError`; a 5xx → a provider error; password sent when configured, absent
  when not; secrets never logged.
- `test_opencode_provider.py` — the contract suite, plus the cursor's
  exactly-once property, unknown-event tolerance, `NotImplementedError` for the
  unsupported surface, and `cost_usd` from the session.
- `test_opencode_permissions.py` — permission → `needs_permission`; question →
  `needs_choice`; allow → `once`; deny → `reject`; **`always` is never sent**.
- `test_opencode_rehydrate.py` — cursor restored from `runtime_metadata`; a
  vanished session becomes `lost`; an unowned server session is left alone.
- `test_registry.py` — `YURI_AGENTS=claude-code,opencode` registers both;
  `claude-code` remains the default.

All without a real OpenCode server, network, or credentials. A separate,
explicitly-marked live check exercises the real binary (§12).

---

## 12. Order of work

1. `client.py` + its tests — envelope, auth, errors. Nothing else can be right
   until this is.
2. `server.py` + its tests — attach/spawn/never-kill-what-you-didn't-start.
3. `provider.py` + the contract suite against the fake server.
4. Permissions and questions.
5. Rehydration and the cursor's durability.
6. Registry, config, `yuri doctor`, and the `operating.ts` line teaching Yuri
   that OpenCode exists and how to choose it.
7. **Live check against the real binary** — `opencode serve`, a real session, a
   real prompt on a model the user has working, a real permission if one
   arises. This is where the unknown event types get discovered and the mapping
   table earns its entries.

---

## 13. Risks

**The unknown event vocabulary is the main one.** A successful turn's types were
never observed, so completion is inferred from `message.finish` and unknown
events are ignored. If OpenCode signals something important only by an event
type we do not map, Yuri will look idle while work happens. The live check in
step 7 is what closes it; the defensive mapping is what keeps the failure
benign (silence) rather than wrong (a false completion).

**Auth is unproven** (§9). If `OPENCODE_SERVER_PASSWORD` needs a mechanism we
guess wrong, a secured server reports offline. Bounded: the failure is loud and
local to the provider.

**Spawning is a new kind of thing for Yuri to own.** She already supervises tmux
panes, but those are the user's sessions; a spawned server is infrastructure.
The `owned` flag and its test are the guard, and the worst case is a stray
process on a known port rather than lost work.
