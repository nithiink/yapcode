# Yuri — follow-ups and decisions

Everything here was found by review, judged non-blocking, and deliberately
left. Nothing here is a known-broken flow — the suite is green and the app
runs. Items are grouped by the phase that carried them:

* **Phase 4 (narration)** — event-driven narration, the quiet/normal/verbose
  mode, the mission voice commands, and one home for agent selection.
* **Phase 1-3 (foundation)** — the domain, store, providers and services build
  (merged as `4d47cb8`).

## Fixed (2026-09-03, after the phase 5 merge)

All three "recommended next fixes" are done, plus four more from the lists
below. Kept here rather than deleted, because each one records a reachable
failure and the reasoning that closed it.

- **Partial unique index on `sessions.native_session_id`** — migration 0002,
  `sessions_one_live`, mirroring `approvals_one_pending`. The violation was
  reproduced first: when `list_native()` raises, `_native()` comes back empty
  and `adopt()` inserted a SECOND live row with its own mission, stranding one.
  `adopt()` now treats the constraint as "already adopted" and cancels the
  mission it had just created. `migrate()` asserts `LIVE_STATUSES` still
  matches the statuses the index hardcodes, since sqlite cannot import a Python
  constant, and `sqlite.py` no longer keeps its own copy of that list.
- **`_touch` re-admitting a lost row without de-duplicating its name** — the
  de-dupe now runs on every path into the live set, not just rehydrate's revive
  branch. A returning session comes back as "alpha 2".
- **`ClaudeRunner._notify` was untested** — five tests, including that one
  raising event does not poison the stream behind it. Removing the guard fails
  three of them.
- **Poll-path approvals under-classified risk** — `Prompt` carries
  `tool_input` and `AdvanceResult.to_dict()` serialises it, so `rm -rf /` from
  the poll path scores `dangerous` rather than `confirm`. It only labels, but
  the user hears that label: Yuri says "that's a destructive action" off it.
- **A moved read mark was only persisted on the poll path** — `send_message`
  rewinds the cursor and `interrupt` bumps `msg_seen`; one `_merge_marks`
  helper now serves send, interrupt and poll, so an interrupt through the API
  or `interrupt_many` writes it down.
- **`NotImplementedError` is soft at `/tools/execute`** — `set_mode` on
  OpenCode and `send_keys` on the SDK backend now reach the user with the
  message that names what the provider cannot do.
- **Three robustness items in `claude_code.py`** — `_version` orphaned a child
  on timeout (on every health probe); `shutdown` let one raising runner skip
  the rest of teardown and left `on_event` pointing at a cleared provider; two
  loops iterated `self._runners` live, which `runner()` inserts into lazily.

## Carried out of phase 4 (narration)

**The narration invariant is "one owner per FACT", not per event type.** This
was the whole-branch review's central finding, and it is now implemented (see
`yuri/narration/policy.py`'s module docstring, which carries the derivation).
The event-type ownership table is still there and still necessary — it is just
not sufficient, because two different types can carry the same fact
(`_fail_if_alone` derives a mission failure from an agent error with the same
reason string) and because a voice tool's own spoken result is a third carrier
the table never modelled. Anyone adding a mission-level event should decide who
owns the FACT before adding a row to the table.

**`session.lost`'s narration is unreachable in practice.** `rehydrate()` is the
only publisher and it runs at startup, before any voice session can be
connected; by the time one connects the event is history, and the spoken gate's
seed correctly suppresses it. The line is written, tested and honest — it just
has no live path today. It becomes reachable the moment anything detects a lost
session outside startup.

**`mode_reader` reads the module-global `container()`.** `narration_mode()`
resolves the process-wide container rather than the `store` its own
SessionService was constructed with. Harmless while there is exactly one
container per process (and `test_container` sets it), but it is a hidden global
in an otherwise injected service.

**`"X" is paused."` drops the payload's `reason`.** The status-changed payload
carries why, and the `failed` branch reads it; `paused` and `cancelled` do not.
Nothing is wrong today — the only system-driven pause is a closed session,
which is now suppressed as a duplicate anyway — but a ui/api pause says less
than it knows.

**`narrationBusy` has no timeout.** The mode toggle disables itself for the
duration of the PUT, exactly as the per-session mode buttons do. A PUT that
never settles leaves the control disabled until the page is reloaded.

**No component-level test for "the prompt card is still set when narration is
suppressed".** `handleClaudeResult` sets the UI card and injects the line
independently, on purpose — quiet mode must still show a permission prompt on
screen. That independence is asserted at the service level (quiet suppresses
the line) but not through the component.

**`SCAN_MAX` is a recall ceiling on mission resolution.** `resolve()` scans the
500 most recently updated missions for an exact-title or id-prefix match. A
full id still resolves by primary key, but a mission older than that by title
does not. Fine for spoken references; wrong if the tool ever becomes a search.

**`resolve()`'s docstring "Order:" line omits the deictic branch.** The code
runs the deictic check FIRST (yielding to a real title), which the paragraph
below the list explains — but the ordered list itself starts at "exact id".

**`AgentRouter`'s final `registry.get` can raise on a misconfigured
container.** The router is hardened against an unknown agent everywhere the
caller supplies the id; the last lookup trusts its own container's default.

**`_frame` (yuri/api/routes.py) has no try/except.** The review judged no
exception currently reachable — `line_for` is pure, every field goes through
`_clip`, and `json.dumps(default=str)` absorbs odd payload values. It is worth
noting anyway because it is the one place a narration bug stops being a wrong
line and becomes a dead transport: a raise inside the generator kills the SSE
stream for that client, and the frontend's EventSource would reconnect into the
same failure. A `try/except` that logs and skips the frame would make a
narration bug degrade instead of disconnect.

## Known gaps carried out of phase 1-3

**The tmux runner notifies only from its hook path.** Three completion paths
now notify, but a multi-question `AskUserQuestion` still emits an event only
for the first sub-question (`tmux_runner.py`'s multi-question auto-advance and
`_restore_pending` build fresh prompts without notifying). The poll path still
surfaces every sub-question to the voice model, so the user is asked — but
narration built purely on events would under-report. Do not assume every turn
and every prompt emits.

**Poll-path approvals under-classify risk.** FIXED — see the Fixed section
above. `Prompt` now carries `tool_input` and `to_dict()` serialises it.

**`cost.updated` no longer reaches the Activity feed.** Bridged events are
filtered to non-debug severities to stop double-logging, and cost had no
runner-side line of its own. Still persisted and served by `/yuri/events`, and
per-session cost still renders in the session list.

## Deliberate decisions worth knowing

- **There is no orchestrator.** Phase 4 ruled one out: missions are created
  implicitly by `SessionService.start`/`adopt` and driven by `_mission_to`
  (derived from session events) plus `MissionService.pause/resume/cancel`. The
  docstrings in `domain/mission.py` and `services/missions.py` that promised
  one were corrected rather than left as a forward reference.
- **Narration wording is server-side** (spec §4): it is testable in
  `yuri/narration/service.py`, identical for any future non-browser surface,
  and the two load-bearing instructions the old frontend injections carried
  travel with the text. The frontend's whole rule is "if a carrier attached a
  line, inject it".
- **A bounded injection queue drops the OLDEST line.** Every queued line is
  already in the model's conversation via its own `conversation.item.create`,
  so a dropped line stays context and only loses its own spoken response;
  falling further behind for the rest of a long turn is worse.
- **Unset `ALLOWED_PROJECT_ROOTS` falls back to `~/Yuri` alone.** No escape (the
  home is realpath-contained) and `yuri doctor` now fails loudly on it.
- **`check_same_thread=False` on store connections.** Required so shutdown can
  close the event writer's connection; safe because `threading.local` means no
  connection is ever shared. It does disable a guardrail against a future
  caller sharing one.
- **Repositories are called inline, not through a threadpool** (the spec said
  otherwise). Local SQLite writes are sub-millisecond; the bus writer is the
  only background persist.
- **`session_manager`'s 16 shims were deleted**, not deprecated; `provider()`
  now raises rather than silently minting a second provider.
- **`risk_for` labels only.** It never auto-approves, so an under-flagged
  ordering like `rm -i -f x` still prompts the user.
- **`ProjectService.list()` returns more per-project keys than the old
  `list_projects`** (`registered`, `id`, `slug`, `kind`, `default_agent`). Additive.

## Verification status

The live Gemini voice round-trip (connect, start a session, close it) was
confirmed working by the user on 2026-09-02 — the phase 1-3 voice leg is
verified. Azure and OpenAI realtime remain unchanged code covered only by the
existing mint tests; neither has been exercised live.

Phase 4 is verified by the suite and by a `/health` + `/yuri/narration` boot,
with the home both present and absent. The narration flows themselves —
mission lines arriving over SSE mid-conversation, the mode toggle, the mission
voice commands — have not been exercised in a live voice round-trip.

## Smaller items (phase 1-3 tasks)

- Task 1 coverage ceiling — only brief-prescribed cases (no extra EDIT_TOOLS/classify members)
- Task 2 two assertions (test_fuzzy_name_case_insensitive, test_symlinked_root_resolves) pin macOS case-insensitive-FS casing; would fail on case-sensitive Linux CI. Fix = one comment or os.path.samefile. Reviewer verified the behavior is real, not a masked regression.
- Task 3 test_list_slash_commands_keys does real FS I/O under ~/.claude (assertion itself env-independent; can emit an asyncio slow-callback diagnostic).
- Task 4 unused `import asyncio` in tests/test_tmux_rehydrate.py.
- Task 5 _access_ok test omits the "localhost" loopback host (plan scope, not implementer).
- Task 6 tests/test_tmux_rehydrate.py emits INFO log lines ("rehydrated tmux session ...") into suite output — from the code under test, not the test; silence via logging config if wanted.
- Task 6 test_fake_provider.py lacks a module docstring (all sibling tests have one); yuri/__init__.py + providers/__init__.py lack `from __future__ import annotations` (inert — docstring-only/empty).
- Task 7 (1) _version() doesn't proc.kill() on timeout — orphan risk; (2) no in-flight guard on the health cache — N concurrent /agents polls each spawn 2 subprocs; (3) _cli_only raises NotImplementedError for an UNKNOWN handle instead of KeyError; (4) shutdown()/stop() teardown not failure-guarded (one raising runner skips the rest); (5) backend_of/list_native iterate self._runners live — wrap in list() to avoid mutation-during-iteration; (6) _StubRunner is duck-typed, so ClaudeRunner._notify and its 9 real call sites are covered by no test (the "observer bug must never break a turn" guarantee is untested); (8) shutdown() doesn't reset r.on_event (partial keeps pointing at a cleared provider).
- Task 8 session_manager.py module docstring still describes the deleted _runners design (inline comment above _provider is accurate).
- Task 9 the two fail-closed sandbox tests still pass once ~/Yuri exists only because their probe strings (/tmp, "alpha") don't collide with the home's name/contents — coincidental, not semantic. Harden by patching config.YURI_HOME to a temp path in both fixtures.
- Task 11 dead `"approvals_one_pending" in str(exc)` branch (sqlite 3.53.4 never names the index in the error); tests/test_store.py lacks `from __future__ import annotations` + WHY docstring (brief-inherited).
- Task 12 summarize()'s SESSION_QUESTION (`text`) and MEMORY_REMEMBERED (`fact`) payload keys are unconfirmed until Tasks 16/19 set them — verify those two render non-blank once producers exist.
- Task 14 json.dumps(sort_keys=True) in the synth-id hash raises TypeError on a dict with MIXED-TYPE keys ({1:"x","a":1}); unreachable today (tool_input always comes from the SDK's JSON-decoded tool block, so keys are strings) but a new failure mode — wrap the hash construction if the fallback is ever fed non-provider prompts.
- Task 15 redundant list comprehension around store.sessions.list() at missions.py:99; create() truncates the goal without whitespace-collapsing while set_goal_if_empty() does (brief-inherited inconsistency).
- Task 16 poll-path approvals under-classify risk because AdvanceResult.to_dict() omits tool_input, so risk_for("Bash", {}) -> "confirm" for an rm -rf the observer path scores "dangerous" (harmless today: the observer normally records first and wins the request_id dedup — but matters if Task 18 ever surfaces risk in a policy); duplicate row per native id on re-adopt of a `stopped` (not `lost`) handle; resolve() says "no session matches" when the real cause is an unreachable provider; poll()/on_provider_event() duplicate the bookkeeping the emits gate distinguishes; 2 tests assert on log levels.
- Task 17 startup()'s INFO banner adds a line to suite output; the validation-before-dispatch reorder in tools.py send_keys/run_slash_command is untested (an SDK session with an empty command now gets the "required" ValueError instead of the SDK soft error); start_session's `message` now names the provider ("Started Claude Code session") and `mode` comes back normalized (value-level, keys unchanged).
- Task 18 _decide() maps ValueError->409 rather than the general ->400 (correct here — the only reachable ValueError is "already resolved", a genuine conflict — but worth a comment so nobody "fixes" it); /yuri/context's active_missions[].project name lookup is untested for a mission whose project row is gone.
- Task 20 none outstanding.

## Phase 5 (OpenCode provider)

- **FIXED — `NotImplementedError` is not soft at `/tools/execute`.** `main.py`'s exception
  chain maps `YuriUnavailable` and `ValueError` to soft errors and `KeyError` to
  404, but everything else — including `NotImplementedError` — becomes a generic
  "the tool failed unexpectedly". So `set_mode` on an OpenCode session (which has
  no permission modes) surfaces without its explanation, and the voice prompt's
  AGENTS bullet is the only thing carrying it.
  **Predates OpenCode**: `base.py`, `claude_code.py` and `fake.py` already raise
  `NotImplementedError` for unsupported surfaces (send_keys, slash commands, and
  resume on the SDK backend), so the same generic error already happens there.
  Fixing it means adding one branch in `main.py`, out of scope for a provider task.
  Symptom is mild — the model has been pre-briefed, so it degrades to correct
  behaviour with worse wording.

- **`list_native()`'s empty-handles early return breaks the `answered` contract.**
  `_native_map` treats "the provider's `list_native()` did not raise" as *the
  provider answered*, and uses that to distinguish "its sessions are gone" from
  "it could not be enumerated" — the distinction spec §38 exists to protect.
  `OpenCodeProvider.list_native` returns `[]` without touching the network when
  it holds no handles, which is exactly the state after `rehydrate` skipped an
  unreachable server. So Yuri claims OpenCode answered with nothing, and:
  - a restart with the server down marks every OpenCode row `lost` and narrates
    it, while those sessions are alive and durable server-side. `lost` is only
    cleared by another `rehydrate`, which only runs at startup, so they stay
    detached for the rest of the run even after the server comes up.
  - `stop_many` with the server down records `status: stopped` — the unverified
    claim the sibling branch was written to avoid. It should be `lost`.

  Reachable whenever `OPENCODE_SPAWN=0` (supported) and the user's server is not
  up, or the binary is missing.

  **Not fixed here because the obvious fixes are both wrong.** Making
  `list_native` do the GET when it holds no handles puts an HTTP round trip on
  `resolve`/`list`/`poll`, and it would *acquire* — reintroducing the startup
  spawn just closed. The real fix is one explicit "could I reach a server
  without starting one" notion on the provider, used by both `rehydrate` and
  `list_native`, with `list_native` raising when the answer is no so the
  `answered` set stays honest. That is a deliberate design change to a
  cross-service contract, not an end-of-branch patch.

- **Smaller, from the same review:** the mark-persistence item is FIXED (one
  `_merge_marks` helper now serves send, interrupt and poll). Still open: A session row's `backend` reads `"cli"` for OpenCode (the UI is
  right because `list_native` says `opencode`, but the row, `start_session`'s
  result and the `revived` payload are wrong). `send_keys`/`run_slash_command`
  tell an OpenCode user "this session uses the SDK backend", which is false
  (predates OpenCode). `peek_screen` never reports a pending prompt for
  OpenCode. `read_transcript` returns `{found: false}` for an OpenCode handle,
  so OpenCode has no "show me the whole conversation" surface.
  `config.summary()` was never taught the OpenCode keys.

- **No "Watch live" for OpenCode, and the reason is message persistence.**
  The terminal handoff works (`opencode -s <id> --mini`), but a live view would
  be a *separate* process reading OpenCode's shared store, and a session Yuri is
  actively driving persists **0 rows** in that store's `message` table —
  measured three times, in two directories, with the server both running and
  gracefully stopped, after turns that Yuri itself read the reply from over
  HTTP. So a pane would be blank precisely when someone wanted to watch.

  Two consequences worth chasing:
  1. **It weakens the durability story.** Phase 5 was built on "OpenCode
     sessions outlive Yuri and can be re-adopted". Rehydration restores the
     cursors correctly, but if the conversation itself is not in the store, what
     comes back may be thinner than assumed. Worth establishing what actually
     triggers a commit — one early session DID persist 2 messages and got an
     auto-generated title ("Ping pong test"), while later identical-looking runs
     persisted none, so it may be tied to the auto-title/summarise step, which
     fails on a flaky free model.
  2. **Then "Watch live" becomes possible**, by running the handoff command in a
     tmux pane and streaming it through the existing terminal websocket. The
     session stays in the server, so the pane is only a view and nothing
     fragments. The reverted attempt is in git history: lazily created,
     idempotent, killed on `stop()`, password passed by environment rather than
     argv (a tmux command line is world-readable via `ps`).
