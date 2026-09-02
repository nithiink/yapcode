# Yuri foundation — follow-ups and decisions

Carried out of the phase 1-3 build (merged as `4d47cb8`). Every item here was
found by review, judged non-blocking, and deliberately left. Nothing here is
a known-broken flow — the suite is green and the app runs.

## Recommended next fixes

**Partial unique index on `sessions.native_session_id`.** A full unique index
would break `adopt()`'s legitimate re-adoption of a stopped handle, so it was
declined — but the justification ("only one row is ever live") is unenforced,
and a reviewer reproduced a violation: if `list_native()` fails, `_native()`
returns empty, `adopt()` takes the not-yet-adopted branch and inserts a second
*live* row for the same handle. It cannot misroute (both rows name the same
handle and get different session names), but it strands a mission. The idiomatic
guard is a partial index mirroring `approvals_one_pending` in `0001_init.sql`:
`UNIQUE(native_session_id) WHERE status IN (<live statuses>)`.

**`_touch(row, "running")` can re-admit a lost row without de-duplicating its
name.** Reachable from `send`/`poll`/`answer` between a handle's return and the
next restart. Fails closed today — `resolve()` refuses an ambiguous name — but
the de-dupe belongs in `_touch` alongside the revive path.

**`ClaudeRunner._notify` coverage is one test deep.** The "an observer bug must
never break a turn" guarantee is the safety net under the whole event pipeline.

## Known gaps that matter for phase 4 (narration)

**The tmux runner notifies only from its hook path.** Three completion paths
now notify, but a multi-question `AskUserQuestion` still emits an event only
for the first sub-question (`tmux_runner.py`'s multi-question auto-advance and
`_restore_pending` build fresh prompts without notifying). The poll path still
surfaces every sub-question to the voice model, so the user is asked — but
narration built purely on events would under-report. Do not assume every turn
and every prompt emits.

**Poll-path approvals under-classify risk.** `AdvanceResult.to_dict()` omits
`tool_input`, so an approval recorded via `poll` scores `confirm` where the
observer path scores `dangerous`. Harmless while the observer wins the
request_id dedup; load-bearing the moment `risk` drives policy rather than
just labelling.

**`cost.updated` no longer reaches the Activity feed.** Bridged events are
filtered to non-debug severities to stop double-logging, and cost had no
runner-side line of its own. Still persisted and served by `/yuri/events`, and
per-session cost still renders in the session list.

## Deliberate decisions worth knowing

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

## Not yet verified

A live voice round-trip. Azure and OpenAI realtime are unchanged code covered
only by the existing mint tests; Gemini Live is the path in use and needs a
human at a microphone (plan task 21).

## Smaller items

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
