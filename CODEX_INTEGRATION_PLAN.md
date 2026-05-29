# Plan: Integrating Codex into voice-claude

## 1. What Codex actually is, and the integration paths it exposes

OpenAI **Codex CLI** ([github.com/openai/codex](https://github.com/openai/codex)) is the closest analogue to the `claude` CLI this project already drives. It's a Rust-based local agent with multiple addressable surfaces, three of which matter for us:

1. **Interactive TUI** (`codex` + optional initial prompt). Same shape as `claude` — a fullscreen terminal app with a permission menu, a `/permissions` slash command, slash commands, MCP, hooks (`PreToolUse`, `PostToolUse`, `Stop`, `PermissionRequest`, etc.) configured via `~/.codex/config.toml` (or per‑project `.codex/`). PreToolUse hooks are command-shaped, can `matcher = "^Bash$"`, take a `timeout`, and **can block** until the script returns a decision — exactly the mechanism `backend/tmux_hooks/hook_pretool.py` already uses with claude.
2. **`codex exec --json`** — non-interactive JSONL stream of `turn/started`, item deltas (`item/agentMessage/delta`, `item/commandExecution/outputDelta`), `turn/completed`. Resume via `codex exec resume <id>` or `--last`. **Footgun:** in exec mode "approval requests cause immediate failure unless policies are set to auto-approve" ([Headless exec mode docs](https://deepwiki.com/openai/codex/4.2-headless-execution-mode-(codex-exec))). Exec mode is fine for fire-and-forget; it does not surface a live permission prompt the way voice-claude's spoken approval flow needs.
3. **`codex app-server`** — long-lived bidirectional **JSON-RPC 2.0 over stdio** ([codex-rs/app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)). Methods include `thread/start`, `thread/resume`, `turn/start`, `turn/interrupt`, streaming notifications (`item/agentMessage/delta`, `item/commandExecution/outputDelta`, `turn/completed`), and — most importantly — **`tool/requestUserInput` is a server→client request the host answers**, plus `permissions_approval_request` / `mcp_approval_request` event types. This is the Codex analogue of Claude Agent SDK's `can_use_tool` callback. The Python wrapper is `codex_app_server` ([Python SDK overview](https://codex.danielvaughan.com/2026/03/30/codex-python-sdk-embedding-agents/)); a typed JSONL-streaming alternative is `acodex` ([maksimzayats/acodex](https://github.com/maksimzayats/acodex/)). The official TypeScript SDK is `@openai/codex-sdk` ([SDK docs](https://developers.openai.com/codex/sdk)).

There is **no deterministic `--session-id` flag** for Codex like `claude --session-id` ([CLI reference](https://developers.openai.com/codex/cli/reference)) — sessions get IDs assigned by Codex under `~/.codex/sessions/`. This is the one place where voice-claude's current handle-equals-session-id invariant breaks; we'll need to map a voice-claude handle to a discovered Codex session id.

## 2. Where Codex maps onto the existing architecture

voice-claude already factors execution behind a single seam: **`ClaudeRunner`** in `backend/claude_runner.py:90`, with two implementations (`SDKClaudeRunner`, `TmuxClaudeRunner`). The voice tools in `backend/tools.py:182` don't know which one they're driving — they just call `runner_for(handle)`. That seam is exactly the right place to plug Codex in.

The mapping is one-to-one:

| voice-claude concept | Claude | Codex equivalent |
|---|---|---|
| Interactive TUI backend | `TmuxClaudeRunner` (`claude --session-id … --settings hooks.json` in tmux) | New `TmuxCodexRunner` — `codex` in tmux + `~/.codex/config.toml` hooks pointing at our scripts |
| Programmatic backend | `SDKClaudeRunner` (Claude Agent SDK) | New `AppServerCodexRunner` (Python `codex_app_server` over JSON-RPC) |
| `can_use_tool` callback | `claude_runner.py:158` `_cb` parks an `asyncio.Future` | Handle inbound `tool/requestUserInput` / `permissions_approval_request` JSON-RPC requests and park a Future the same way |
| PreToolUse hook polls `decisions/<id>.json` | `backend/tmux_hooks/hook_pretool.py:78` | Same script, repurposed — Codex hooks are command-shaped with the same TOML/JSON config and stdin/stdout decision protocol ([Advanced config](https://developers.openai.com/codex/config-advanced)) |
| 4 permission modes (`default`/`plan`/`acceptEdits`/`auto`) | Native mode cycle, Shift+Tab | Map onto Codex's `approval_policy` × `sandbox_mode` matrix (see §4) |
| Live tmux pane in browser (`/sessions/{handle}/terminal`) | tmux capture/attach | Works unchanged — provider-agnostic |
| Transcript reader | `backend/transcript.py` reads `~/.claude/projects/*/<id>.jsonl` | Codex's per-session log under `~/.codex/sessions/` — analogous file format but field names differ; needs a parallel reader |
| `AGENTS.md` / `CLAUDE.md` | Claude reads `CLAUDE.md` | Codex reads `AGENTS.md` |

## 3. Recommended phasing

**MVP = the TUI path first.** It's the highest-fidelity match to what's already shipped, the hook-blocks-on-decision-file pattern is proven, and it inherits Codex's full feature surface (slash commands, MCP, plugin marketplace). The app-server path comes next because the SDK is still labeled experimental and lacks a clean per-tool callback at the Python SDK level (though the underlying JSON-RPC has it).

Order of work (independently shippable slices):

- **Phase 1 — TUI Codex backend.** New `TmuxCodexRunner` + Codex hook scripts. Voice can start, drive, approve, interrupt, and close Codex sessions in tmux. Single agent toggle in the UI. Rehydration on backend restart.
- **Phase 2 — App-server backend.** New `AppServerCodexRunner` using `codex_app_server` (Python). Programmatic path with structured events and proper turn cost reporting. Mirrors how SDK sits alongside CLI today.
- **Phase 3 — Polish.** Codex transcript reader, cost rates, AGENTS.md awareness, voice prompt tuning to teach the model about agent choice.

## 4. Detailed plan — Phase 1 (TUI Codex backend)

### 4.1 Backend module layout

Touch list (all `backend/`):

- **New: `codex_runner.py`** — the `CodexRunner` ABC. Likely identical to `ClaudeRunner` (`claude_runner.py:90`), so the cleanest move is to **rename `ClaudeRunner` → `AgentRunner`** in a small refactor and let both Claude and Codex implement it. The `AdvanceResult`/`Prompt` dataclasses and `MODE_CYCLE` are already agent-agnostic. The status string union (`running`/`needs_permission`/`needs_choice`/`completed`/`error`) carries over without change.
- **New: `tmux_codex_runner.py`** — `TmuxCodexRunner(AgentRunner)`. Structurally a fork of `tmux_runner.py`, with these differences:
  - Launches `codex` instead of `claude`. No `--session-id`; capture the ID from the TUI/hooks instead (see §4.2).
  - Settings file is `~/.codex/config.toml` (TOML) rather than `--settings <json>`. Codex's CLI takes `--config key=value` for inline overrides ([CLI reference](https://developers.openai.com/codex/cli/reference)), and per-project hooks live in `<repo>/.codex/config.toml` or `<repo>/.codex/hooks.json`. We can either:
    - (a) write a per-session config file and pass `--profile <name>` + `--config hooks.PreToolUse=…`, OR
    - (b) write `<session-ctrl-dir>/.codex/config.toml` and start codex with `--cd <session-ctrl-dir>` so it picks up the per-session config. Option (a) is cleaner.
  - Mode cycle uses **`/permissions`** rather than Shift+Tab (per [features docs](https://developers.openai.com/codex/cli/features)). Replace `_detect_mode`/`set_mode` to drive the `/permissions` slash command and parse the resulting footer text.
- **New: `codex_hooks/`** — `hook_pretool.py`, `hook_stop.py`, `hook_notify.py` parallel to `tmux_hooks/`. Substantially the same logic but with Codex's hook input/output JSON shape (per [Codex advanced config](https://developers.openai.com/codex/config-advanced)). Key differences likely to be field names (`tool_name` vs `command_name`, `tool_use_id` vs `request_id`) and decision schema (`{"decision": "allow"|"deny"}` may be different — needs to be confirmed against the live Codex hook schema; the existing Claude script's emit() format is `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": …}}` which is close to but not identical to Codex's expected shape).
- **Edit: `permissions.py`** — currently classifies Claude tool names (`Read`, `Bash`, `AskUserQuestion`, etc.). Codex doesn't expose tools by these names; its hooks fire on **command** patterns (shell-first) plus MCP elicitations and a `request_permissions` tool. Either:
  - Generalize `classify()` to take an `agent` argument and switch sets per agent, OR
  - Add a parallel `permissions_codex.py` with the Codex-flavored safe/risky sets. Voice prompts ("run the command: …", "use the … tool") still work — Codex emits the command string.
- **Edit: `session_manager.py`** — add an `"agent"` dimension alongside backend. The registry today is `_runners: dict[backend_str, ClaudeRunner]` (`session_manager.py:17`). Re-key to `(agent, backend)` or `f"{agent}:{backend}"`. Update `register_owner`, `runner_for`, `list_all_sessions` to track and expose the agent. `default_name_for` and `resolve_session` need no logic changes. The `handoff_command` helper at `session_manager.py:253` currently writes `claude --resume …` — extend it to switch on agent and emit `codex resume <id>` for Codex sessions.
- **Edit: `tools.py`** — extend the `start_session` tool definition with an `agent: "claude"|"codex"` enum (`tools.py:43-73`), default `"claude"` for backwards compat. The other tool definitions (tell_claude, answer_prompt, peek_screen, etc.) are agent-agnostic — but consider renaming them to `tell_agent`/`tell_assistant` for clarity, or keep the names and update descriptions to "the running agent (Claude or Codex)". Renaming is a frontend-visible change because the realtime function-call names get sent to the OpenAI model — safe to do but worth doing in one shot. Updating just descriptions is the minimum.
- **Edit: `config.py`** — add `CODEX_BIN = os.getenv("CODEX_BIN", "codex")` and an optional `CODEX_CONFIG_HOME` override so tests can sandbox `~/.codex/`.
- **Edit: `main.py`** — no real changes; lifespan rehydration (`main.py:65`) iterates runners generically. Just ensure `rehydrate_cli_sessions()` knows about both Claude and Codex CLI runners (refactor it to loop over all CLI-shaped runners and aggregate restored lists).

### 4.2 The session-id problem

`TmuxClaudeRunner` hard-codes `handle == session-id == tmux pane name` (`tmux_runner.py:113`). For Codex, the session id is **assigned by Codex** during startup and surfaced through the `transcript_path` hook payload or via `codex resume` picker output.

Concrete approach: keep the voice-claude `handle` (uuid) as the **tmux pane name and our own control-dir name** so all our existing on-disk plumbing keeps working unchanged. Discover the Codex session id from the first hook event that fires (PreToolUse / SessionStart / Stop — all of them carry the session ID and transcript path per [config-reference](https://developers.openai.com/codex/config-reference)) and store it as `s.session_id`. `handoff_command` uses it. The transcript reader uses it via `~/.codex/sessions/<id>` glob, mirroring `tmux_runner.py:_find_transcript` at line 775.

This means rehydration needs one extra step: on restart, re-read `meta.json` (we already do this — `tmux_runner.py:_read_meta` at line 278), but now also load the stored `session_id` so handoff still works without waiting for the next tool event.

### 4.3 Mode mapping

voice-claude's four modes need to project onto Codex's `approval_policy` × `sandbox_mode` matrix:

| voice-claude mode | Codex `approval_policy` | Codex `sandbox_mode` | Notes |
|---|---|---|---|
| `default` | `on-request` | `workspace-write` | PreToolUse hook parks on the decision file, voice approves/denies. Equivalent to `--full-auto` ([approvals docs](https://developers.openai.com/codex/agent-approvals-security)). |
| `plan` | `untrusted` | `read-only` | Or, if Codex grows a native "plan only" mode like Claude has, prefer that. Today's docs say read-only + no writes is the closest. |
| `acceptEdits` | `granular` (auto-approve sandbox/exec, prompt on risky) | `workspace-write` | Granular lets edits go through while still surfacing other risky categories. The PreToolUse hook can short-circuit edit tools the same way `hook_pretool.py:69` does for Claude. |
| `auto` | `never` | `workspace-write` (or `danger-full-access` if user opts in) | No prompts. Map this in the runner without exposing `--yolo` as a default. |

Persist the mode in `<session>/mode` exactly as today; the Codex PreToolUse hook reads it via `_common.read_mode()` and short-circuits accordingly. Switching modes mid-session uses `/permissions` rather than Shift+Tab.

### 4.4 Hooks: what to port and what changes

`backend/tmux_hooks/_common.py` (events.jsonl + decisions/ file protocol) is **fully reusable** because it's just file IO; nothing in it talks to Claude. Copy it into `codex_hooks/_common.py` (or share via a top-level module).

`hook_pretool.py` (`backend/tmux_hooks/hook_pretool.py:36`) needs three changes:
1. Read Codex's payload fields. From [hooks config docs](https://developers.openai.com/codex/config-advanced) the shape is along the lines of `{tool_name, command, cwd, session_id, transcript_path, …}` for PreToolUse — confirm against a live `codex` run before locking field names. The `request_permissions` PermissionRequest event has its own shape and is the one that fires on the bespoke `request_permissions` tool call.
2. Map "risky" classification for Codex (shell command matcher patterns rather than the named-tool set).
3. The hook **output** format: Codex's [advanced config](https://developers.openai.com/codex/config-advanced) example uses a slightly different `decision` key. Treat the Claude format (`hookSpecificOutput.permissionDecision`) as Claude-specific; emit Codex's expected format in the new script.

`hook_stop.py` and `hook_notify.py` port nearly verbatim — both just append events to the control dir.

Wire the hooks via per-session `~/.codex/profiles/<handle>/config.toml`:
```toml
[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "/abs/path/to/python /abs/path/to/codex_hooks/hook_pretool.py"
timeout = 590
```
and start with `codex --profile <handle> --sandbox workspace-write --ask-for-approval on-request`.

### 4.5 The event tail

`tmux_runner._tail_events` (`tmux_runner.py:675`) is agent-agnostic — it tails `events.jsonl`, which we control. The same task can power both runners. The fields that get written by `_handle_event` (`tmux_runner.py:697`) — `needs_permission`, `needs_choice`, `turn_complete` — are emitted from our own hook scripts, so the format is whatever we make it. Keep the same shape so the runner glue code is shared.

The new wrinkle: Codex's `AskUserQuestion` analogue is `request_permissions` → `tool/requestUserInput` (in app-server) or the `PermissionRequest` hook (in TUI). It accepts 1–3 questions, very close to Claude's 1–4. The existing question-driving code in `_answer_question` (`tmux_runner.py:458`) navigates a TUI menu via arrow keys and Enter — Codex's permission TUI menu is similar but visually different. Expect to tune `_menu_cursor` and `_wait_for_menu` to match Codex's menu rendering (look for "Enter to select" footer text).

### 4.6 Frontend changes

`frontend/components/VoiceAgent.tsx:545-557` has a two-button toggle group for `cli`/`sdk` backend. Add a sibling toggle group for agent:

- New state: `agent: "claude" | "codex"` (localStorage key `vc_agent`)
- New `agent` field passed into the realtime session and through to `start_session` tool calls (mirror the existing `backend` plumbing at `frontend/components/VoiceAgent.tsx:369`)
- Header label at line 480: `… · ${BACKEND_LABEL[backend]} · ${AGENT_LABEL[agent]}`
- Disable when connected (same UX as the existing toggles)

`frontend/lib/voice.ts:47` adds `AgentKind = "claude" | "codex"` and extends `RealtimeOptions` with `agent?: AgentKind`.

`frontend/lib/instructions.ts` needs a new section explaining when to use which agent, e.g. "Use Codex when the user asks for it explicitly, or when the task is shell-heavy / one-shot. Otherwise default to Claude." The current INSTRUCTIONS hard-code "Claude" throughout — generalize references to "the agent" where the message applies to both, keep "Claude" where it specifically means Claude.

`frontend/components/LiveTerminal.tsx` is provider-agnostic (it just attaches to a tmux pane via WS) — no changes.

### 4.7 Environment / install prerequisites

- User installs Codex: `brew install codex` or the npm/curl installer ([install instructions](https://github.com/openai/codex)). The runner's `start()` should `shutil.which("codex")` and raise a clean error if missing, parallel to the current `claude` check at `tmux_runner.py:116`.
- Auth: either `codex login` (OAuth, recommended) or set `CODEX_API_KEY` in the backend `.env`. The backend doesn't need to know about either — Codex picks them up on its own.
- Document required env in the existing `.env.example`.

### 4.8 Permissioning the new code paths

`permissions.py`'s `SAFE_MCP_PREFIXES = ("mcp__claude-in-chrome__",)` is global. Codex MCP servers configured under `~/.codex/config.toml` use the same MCP naming convention, so the same prefix works if the user also configures claude-in-chrome under Codex. Note in CHANGELOG/README that MCP servers are configured separately for each agent.

## 5. Detailed plan — Phase 2 (App-server Codex backend)

Files:

- **New: `backend/app_server_codex_runner.py`** — `AppServerCodexRunner(AgentRunner)`. Wraps `codex_app_server` Python SDK ([PyPI codex-sdk-py](https://pypi.org/project/codex-sdk-py/), [acodex](https://github.com/maksimzayats/acodex/) as alternative).
- Subprocess model: keep one long-running `codex app-server` per backend (analogous to how `SDKClaudeRunner` keeps one `ClaudeSDKClient` per session). The Python SDK handles this — `with Codex() as codex` ([Python SDK](https://codex.danielvaughan.com/2026/03/30/codex-python-sdk-embedding-agents/)).
- Mapping into `_Session`:
  - `start()` → `codex.thread_start(model=…, config={"approval_policy": …, "sandbox_mode": …})`; capture `thread_id` as `s.session_id`.
  - `advance(message)` → `thread.turn(input=message)` returns a handle; stream notifications inside `_consume()`:
    - `item/agentMessage/delta` → append to `_delta` / `_transcript`
    - `item/commandExecution/started` → record tool usage
    - `turn/completed` → set `status="completed"`, set the `s._stop` event
  - `tool/requestUserInput` (server→client request) → park on a Future like `_can_use_tool` in `claude_runner.py:350`; resolve it from `answer()`. Surface as `Prompt(kind="choice", …)`.
  - `permissions_approval_request` → same, `kind="permission"`.
- Mode swap mid-session: app-server has `thread/settings/update` which queues a partial update for the next turn — perfect for `set_mode()`.
- Interrupt: `turn/interrupt` JSON-RPC.

The Python SDK marks itself "experimental" (per [SDK page](https://developers.openai.com/codex/sdk)). If the SDK doesn't yet expose `tool/requestUserInput` as a Python callback, fall back to driving JSON-RPC directly via `subprocess`+`asyncio.subprocess` against `codex app-server --listen stdio://` — the protocol is well-specified ([app-server README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)) and bidirectional events are core to the protocol.

## 6. Detailed plan — Phase 3 (Polish)

- **Transcript reader.** `backend/transcript.py` currently globs `~/.claude/projects/*/{handle}.jsonl`. Add a `codex_transcript.py` that globs `~/.codex/sessions/**/<session_id>*` and translates Codex's item types (`agentMessage`, `commandExecution`, `fileChange`, `mcpToolCall`) into the same `{kind:'user'|'assistant'|'tool'|'tool_result'}` shape `read_timeline` already emits. Then dispatch by agent in `read_transcript` (`tools.py:237`).
- **Cost model.** Codex returns usage in `turn/completed` notifications. Add a small Codex pricing table to surface cost the same way SDK does today (`claude_runner.py:330`). The TUI backend can still report `0.0` like Claude TUI does.
- **AGENTS.md vs CLAUDE.md.** No code changes — just call out in README that Codex reads `AGENTS.md`. Optionally the voice INSTRUCTIONS can hint at this when the user asks "what does it read?".
- **Permission UI labels.** The frontend permission card at `VoiceAgent.tsx:572` says "Claude wants to …" — change to "{Agent} wants to …" based on the resolved session.
- **MCP cross-config.** If the user wants the same MCP servers in both agents, document the parallel `codex mcp add …` workflow. No code work needed.

## 7. Risks and verification points

These are the unknowns I'd want to settle live before locking the plan in code:

- **Exact Codex hook stdin/stdout schema.** The high-level docs ([advanced config](https://developers.openai.com/codex/config-advanced)) list event names but the only payload schema specifics I could confirm were "PreToolUse can block". Run `codex` once with a logging hook (`command = "python -c 'import sys; open(\"/tmp/h\", \"a\").write(sys.stdin.read())'"`) and inspect — that's 10 minutes of verification before porting `hook_pretool.py`.
- **Whether `codex` TUI's `/permissions` menu can be driven by send-keys.** Almost certainly yes (it's a TUI), but the current `_detect_mode`/`set_mode` parsing (`tmux_runner.py:582`) needs new regex.
- **`codex_app_server` SDK maturity.** The Python wrapper is experimental; the underlying JSON-RPC is stable. If the SDK is too thin, prefer raw JSON-RPC.
- **Approval menu visual differences.** voice-claude's `_select_row` (`tmux_runner.py:515`) relies on a specific cursor glyph and numbered rows. Codex's permission TUI may differ — verify before relying on this code path.
- **No deterministic `--session-id`.** Means `handoff_command` and rehydration both rely on storing the discovered session id in `meta.json`. Already covered in §4.2 but worth flagging as the one architectural concession.
- **Existing memory note.** `voice-claude-backend-hot-reload.md` reminds me that backend `.py` edits hot-reload via uvicorn `--reload` and that detach-on-shutdown preserves CLI sessions. The Codex CLI runner should inherit the same rehydration shape (`tmux_runner.py:242`) so a backend restart doesn't kill in-flight Codex sessions either.

## 8. Desktop-app visibility (answering a question that came up)

If voice-claude starts a Codex session in tmux on this laptop, **the chat history is visible in the Codex desktop app on the same machine**, because both surfaces read from the per-user store at `~/.codex/sessions/`. The session appears in the desktop picker and is resumable from there (or from `codex resume <id>` in another terminal).

Caveats:

- Same OS user required (sessions are user-scoped under `$HOME`).
- The rollout file is appended live, but it's an empirical question how aggressively the desktop app live-tails an in-progress session vs. only showing it after a list refresh. Worth a 30-second check during Phase 1.
- Do **not** pass `--ephemeral` — that disables session persistence and hides the run from the desktop app.
- Per-session `--profile <handle>` (our plan for hook injection) layers config only; it does not redirect the session store.

This is the same pattern Claude already exhibits: a tmux-launched `claude` session lands at `~/.claude/projects/.../<id>.jsonl` and is resumable from the standalone `claude` CLI elsewhere. Codex inherits the shape via its own store.

## 9. Suggested first commit boundary

If this gets greenlit, smallest viable first PR:

1. Rename `ClaudeRunner` → `AgentRunner` (mechanical refactor, no behavior change) and update imports — `claude_runner.py:90`, `tmux_runner.py:35`, `session_manager.py:14`.
2. Add `agent` to `_owner` registry and to session list output, defaulted to `"claude"`.
3. Add the frontend Agent toggle stubbed to claude-only (the codex option present but disabled, with a tooltip "coming next").

That sets up the seam without functional risk; the actual Codex runner lands in PR #2.

---

## Sources

- [Codex SDK overview](https://developers.openai.com/codex/sdk)
- [Codex non-interactive (exec) mode](https://developers.openai.com/codex/noninteractive)
- [Codex CLI command/flag reference](https://developers.openai.com/codex/cli/reference)
- [Codex CLI features](https://developers.openai.com/codex/cli/features)
- [Codex agent approvals & security](https://developers.openai.com/codex/agent-approvals-security)
- [Codex configuration reference (hooks, mcp, approval_policy)](https://developers.openai.com/codex/config-reference)
- [Codex advanced configuration (hook syntax example)](https://developers.openai.com/codex/config-advanced)
- [Headless exec mode internals](https://deepwiki.com/openai/codex/4.2-headless-execution-mode-(codex-exec))
- [codex-rs app-server JSON-RPC README](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Codex Python SDK (codex_app_server) walkthrough](https://codex.danielvaughan.com/2026/03/30/codex-python-sdk-embedding-agents/)
- [acodex (alt typed Python SDK)](https://github.com/maksimzayats/acodex/)
- [openai/codex repo](https://github.com/openai/codex)
