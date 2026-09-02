# Yuri Foundation — Design Spec (Phases 1–3)

**Date:** 2026-09-02
**Branch:** `feat/yuri-foundation` (off `main`)
**Source plan:** `~/Downloads/yuri-plan.md` (the "YURI — Voice-First Multi-Agent Mission Control" contract). Section references below (§n) point at that document.
**Scope of this spec:** plan Phases 0–3 — assessment, test safety net, `AgentProvider` extraction, Yuri domain + SQLite persistence, EventBus, Yuri persona/memory. Phases 4+ (orchestrator, OpenCode, Mission Control UI) are explicitly out of scope and listed in §10.

---

## 1. Product decisions made during brainstorming

| Decision | Choice | Why |
|---|---|---|
| First increment | Assessment + Phases 1–3 | Largest coherent chunk that leaves every existing flow working; delivers persistence you can see (missions survive refresh). |
| Yuri's workspace | `~/Yuri` — her home, one directory (env `YURI_HOME`) | Holds her state store *and* her scratch space. Registered as a project of `kind="home"` with `auto_approve_edits=true`. Everything outside stays sandboxed exactly as today. |
| Persona | Control plane **with** identity + persistent memory now; her own agent brain later (as a provider) | Keeps §3 "voice/mission layer has no agent-specific assumptions" intact while making her feel continuous. |
| Rename | Additive only | Add `bin/yuri`; new code under `backend/yuri/`; keep `VC_*` env vars, `.yapcode/` store, `yapcode` CLI, module names. §2, §29. |
| Persistence | stdlib `sqlite3`, no new deps | §55.11. Repos behind ABCs so Postgres is possible later (§27). |
| Domain ↔ existing code | Extract in place | `ClaudeCodeProvider` *wraps* the two existing runners; `tools.py` handlers write through services. No parallel stack, no directory moves (§33). |
| Missions | Implicit attach | `start_session` creates a mission; first `tell_claude` sets its goal. Explicit mission commands arrive in Phase 4 on the same records. |
| Voice provider used for live verification | Gemini Live | Azure/OpenAI paths covered by existing tests + unchanged code; not exercised live (no keys). |

Pronouns for Yuri: she/her (user's usage).

---

## 2. Assessment — current repo mapped to the plan (§54, §56)

### 2.1 What exists

| File | Lines | Role today | Plan mapping |
|---|---|---|---|
| `backend/claude_runner.py` | 658 | `ClaudeRunner` ABC (`start/advance/answer/start_advance/start_answer/poll_status/interrupt/close/set_mode/read/list/shutdown` + optional `send_keys/resume`), `Prompt`, `AdvanceResult`, `SDKClaudeRunner` | Already ~70 % of `AgentProvider`. Becomes the *inner* runner behind `ClaudeCodeProvider`. |
| `backend/tmux_runner.py` | 1499 | `TmuxClaudeRunner`: spawns `claude` in tmux, drives TUI (permission menus, plan dialog, AskUserQuestion), tails hook `events.jsonl`, rehydrates from `.yapcode/tmux/<handle>/meta.json`, cost from transcript | Claude infrastructure. **Untouched** except a 1-line optional observer hook. Zero tests today. |
| `backend/tmux_hooks/*` | ~200 | PreToolUse/Notify/Stop hooks → `events.jsonl` + decision files | Claude infrastructure. Untouched. |
| `backend/session_manager.py` | 299 | process-wide `_runners/_owner/_names`, `resolve_session`, `resolve_project_path` (mandatory sandbox), `list_projects`, rehydrate, `peek` | `_owner` → provider handle map; names → `AgentSession.name`; project resolution stays and is *called by* `ProjectService`; rehydrate wrapped by `SessionService.rehydrate()`. |
| `backend/tools.py` | 499 | 17 voice tool definitions + `dispatch_tool` | Command/tool layer. Definitions unchanged; handlers call services. |
| `backend/permissions.py` | 72 | `SAFE_TOOLS`, `EDIT_TOOLS`, `classify`, `mode_covers`, `is_plan_file_write` | Reused by `ApprovalService.risk_for`. |
| `backend/event_log.py` | 152 | debug ring buffer + SSE fan-out + JSONL | Stays as the **debug** bus; domain events bridge into it. |
| `backend/cost_log.py`, `pricing.py` | 206 | UI-driven cost JSONL; per-model rates | Kept; `cost.updated` events carry the same numbers. |
| `backend/main.py` | 669 | FastAPI app: auth (`_access_ok`, origin allowlist), token mint for 3 voice providers, `/tools/execute`, `/session/handoff`, terminal WebSocket, `/debug/*` | API composition. Gains `include_router(yuri_router)` + container startup in lifespan. |
| `backend/config.py` | 255 | `.env` loading with provenance, `AUTH_TOKEN`, origins | Gains `YURI_HOME`, `YURI_AGENTS`. |
| `frontend/components/VoiceAgent.tsx` | 1944 | the whole UI: voice connect (3 providers), sessions list, prompt cards, terminal, activity panel, cost | Unchanged except appending `/yuri/context` to instructions. |
| `frontend/lib/instructions.ts` | 34 | voice system prompt "You are the VOICE for Claude Code" | Split into `persona.ts` + `operating.ts`. |
| `backend/tests/*` | 7 files | unittest; `sys.path.insert(0, backend)` | Extended (see §8). |

### 2.2 Gaps vs the plan

- **No persistence** of sessions/missions — process dicts + tmux `meta.json`. (§8, §27)
- **No project registry** — `ALLOWED_PROJECT_ROOTS` + fuzzy match. (§9)
- **No mission concept.** (§8)
- **No normalized domain events** — `event_log` records `source/dest/kind`, not `mission_id/session_id/severity`. (§11)
- **No agent registry / health.** (§7)
- **Voice prompt is Claude-specific** and self-describes as capability-less. (§3, §37)
- **No frontend test runner** (one `.test.ts`, no vitest). No CI.

### 2.3 Conflicts between plan and code, and resolutions

| Conflict | Resolution |
|---|---|
| Plan's `AgentProvider.send_message` is `async … -> ActionResult`; the voice model depends on non-blocking "returns `working` instantly, poll later" (`instructions.ts`). | Contract keeps `send_message`/`answer` **sync + non-blocking** and `poll()`; making them await-to-completion would regress voice responsiveness. |
| Plan §33 target layout vs. §33 "no massive move in phase one". | New code goes in `backend/yuri/`; nothing existing moves. |
| Plan §9 "strict sandbox" vs. user's "Yuri has full access to her workspace". | `~/Yuri` is appended to the allowed roots at runtime and marked `auto_approve_edits`; shell commands there still follow approval policy. Sandbox code unchanged. |
| Plan says "Persist missions" but `start_session` doesn't know the goal. | Goal = first `tell_claude` message (capped 500 chars); Yuri may retitle later. |
| `event_log` vs. new EventBus = risk of two event systems (§49). | Single producer, bridge sink into `event_log`. Debug bus remains debug-only. |

---

## 3. Provider contract & registry (`backend/yuri/providers/`)

### 3.1 Types (`base.py`)

```python
@dataclass(frozen=True)
class AgentCapabilities:
    interactive_terminal: bool
    slash_commands: bool
    send_keys: bool
    permission_modes: tuple[str, ...]
    supports_interrupt: bool
    supports_rehydrate: bool
    supports_resume: bool        # adopt a foreign native session (/voice-handoff)
    supports_events: bool
    cost_tracking: bool

@dataclass(frozen=True)
class AgentHealth:
    online: bool
    version: str | None
    detail: str
    checked_at: str              # ISO-8601 UTC

@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    root_path: str

@dataclass(frozen=True)
class SessionOptions:
    backend: str = "cli"         # provider-specific ("cli" | "sdk" for Claude)
    mode: str = "default"
    model: str | None = None
    name: str | None = None

@dataclass(frozen=True)
class ProviderEvent:
    """Provider-neutral runtime signal. The provider maps its native hooks/API
    into one of these; it never sees Yuri ids, missions, or the store."""
    kind: str        # tool_started | needs_permission | needs_choice | turn_completed | cost_updated | error
    payload: dict    # kind-specific; see §6.2

class AgentProvider(ABC):
    id: str
    name: str
    def capabilities(self) -> AgentCapabilities: ...
    async def health(self) -> AgentHealth: ...
    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str   # native handle
    def send_message(self, handle: str, message: str) -> None          # non-blocking
    def answer(self, handle: str, choice: str) -> None                 # non-blocking
    def poll(self, handle: str) -> dict                                # existing poll_status shape
    async def interrupt(self, handle: str) -> None
    async def stop(self, handle: str) -> None
    async def set_mode(self, handle: str, mode: str) -> str
    async def read(self, handle: str) -> str
    async def peek(self, handle: str, lines: int = 40) -> str | None   # None when no TUI
    async def send_keys(self, handle: str, items: list[dict]) -> dict  # NotImplementedError if unsupported
    def run_slash(self, handle: str, text: str) -> None                # NotImplementedError if unsupported
    async def resume(self, native_session_id: str, project: ProjectContext, opts: SessionOptions) -> str
    def list_native(self) -> list[dict]                                # runner.list() shape, unchanged
    def native_pane(self, handle: str) -> str | None                   # tmux target for the terminal WS
    def set_observer(self, cb: Callable[[str, ProviderEvent], None] | None) -> None  # normalized, see §6.2
    async def rehydrate(self) -> list[dict]
    async def shutdown(self) -> None
```

### 3.2 `ClaudeCodeProvider` (`claude_code.py`)

- `id="claude-code"`, `name="Claude Code"`.
- Owns both existing runners, created lazily: `SessionOptions.backend` selects `TmuxClaudeRunner` ("cli", default) or `SDKClaudeRunner` ("sdk"). Keeps `handle → runner` map (replaces `session_manager._owner`).
- Capabilities: terminal/slash/send_keys/rehydrate/resume true only for the cli backend — reported as the union, with per-handle checks inside methods that raise `NotImplementedError` for sdk handles (today's `backend_of(sid) != "cli"` guards in `tools.py` move here).
- `health()`: `claude --version` and `tmux -V` via `asyncio.create_subprocess_exec`, 5 s timeout, cached 30 s.
- Observer: installs `runner.on_event = self._on_runner_event` on both runners (see §6.2).

### 3.3 `FakeAgentProvider` (`fake.py`)

In-memory. `create_session` returns `fake-<n>`; `send_message` records and marks `working`; `script(handle, result_dict)` queues a `poll` result; `emit(handle, kind, raw)` fires the observer. Records every call in `self.calls`. Used by contract tests, service tests, API tests.

### 3.4 `AgentRegistry` (`registry.py`)

`register(provider)`, `get(id) -> AgentProvider` (raises `KeyError`), `all()`, `async health_all() -> dict[id, AgentHealth]`. Built from `YURI_AGENTS` (comma list, default `claude-code`). Unknown ids are logged and skipped.

### 3.5 Changes to existing files

- `claude_runner.py`: class attribute `on_event: Callable[[str, str, dict], None] | None = None` on `ClaudeRunner` (args: handle, native kind, raw dict); `SDKClaudeRunner._can_use_tool` and `_consume` call it when set.
- `tmux_runner.py`: `_handle_event` and `_update_cost` call `self.on_event(handle, kind, ev)` when set. Nothing else.
- `session_manager.py`: `get_runner/runner_for/register_owner/backend_of` become thin shims over the registry's `ClaudeCodeProvider` (kept so nothing importing them breaks, deprecated in docstring). `_names` remains until §5 moves names to `AgentSession`.
- `tools.py`: uses the registry; behavior identical (guarded by `test_tools_dispatch.py`).

---

## 4. Domain + persistence (`backend/yuri/domain/`, `backend/yuri/store/`)

### 4.1 Yuri's home (`backend/yuri/home.py`)

```
$YURI_HOME (default ~/Yuri)     mode 0700
├── yuri.db                     SQLite, WAL, foreign_keys=ON
├── memory/user.md              created with a header if absent
├── memory/projects/            per-project notes, <slug>.md
├── journal/                    YYYY-MM-DD.md, append-only
└── workspace/                  scratch
```

`ensure()` creates the layout, idempotent. `home_project()` returns the `Project` row of `kind="home"` (created on first run). At startup `YURI_HOME` is appended to the effective allowed roots (`session_manager._allowed_roots()` appends `config.YURI_HOME`) so `resolve_project_path` accepts it without modifying its containment logic.

### 4.2 Entities (`domain/*.py`, dataclasses; no I/O)

All ids UUID4 strings; all timestamps ISO-8601 UTC with `Z` (matches `event_log`).

- **Project**: `id, slug, name, root_path, kind ∈ {user, home}, default_agent, auto_approve_edits: bool, repo_url?, created_at, updated_at`. `slug` = lowercase basename, de-duped with `-2`, `-3`.
- **Mission**: `id, title, goal?, project_id, status, priority: int = 0, current_step?, created_by ∈ {voice, ui, api, handoff, system}, metadata: dict, created_at, updated_at`.
  `MissionStatus ∈ {draft, queued, running, waiting_for_approval, paused, completed, failed, cancelled}` (§8).
  Allowed transitions: `draft→queued|running|cancelled`, `queued→running|cancelled`, `running→waiting_for_approval|paused|completed|failed|cancelled`, `waiting_for_approval→running|paused|failed|cancelled`, `paused→running|cancelled`. Terminal: `completed, failed, cancelled`. Same-state transitions are no-ops. `Mission.transition(to)` raises `InvalidTransition` otherwise.
- **MissionStep**: `id, mission_id, ordinal, title, agent_id?, status ∈ {pending, running, done, failed, skipped}, session_id?, result: dict`. Phase 3 writes exactly one step per mission, title `"work"`.
- **AgentSession**: `id, mission_id?, project_id, agent_id, native_session_id, backend, status ∈ {starting, running, needs_permission, needs_choice, idle, stopped, lost}, name, mode, model?, working_directory, started_at, last_activity_at, runtime_metadata: dict`.
- **Approval**: `id, mission_id?, session_id, agent_id, action, tool_name, tool_input: dict, risk ∈ {safe, confirm, dangerous}, description, status ∈ {pending, allowed, denied, expired, superseded}, request_id (native, unique), requested_at, resolved_at?, resolved_by? ∈ {voice, ui, api, mode_switch}`.
- **YuriEvent**: `id, ts, type, mission_id?, session_id?, agent_id?, project_id?, severity ∈ {debug, info, notice, warning, error}, speakable: bool, payload: dict`.
  Types (string enum `EventType`): `mission.created, mission.status_changed, session.created, session.message_sent, session.turn_completed, session.question, session.interrupted, session.stopped, session.lost, tool.started, approval.requested, approval.resolved, cost.updated, agent.error, project.registered, memory.remembered`.

### 4.3 Risk classification (`domain/risk.py`)

`risk_for(tool_name, tool_input) -> Risk`: `permissions.classify()=="safe"` → `safe`; `EDIT_TOOLS` → `confirm`; `Bash` whose command matches a small destructive pattern list (`rm -rf`, `git push --force`, `git reset --hard`, `drop table`, `mkfs`, `> /dev/`, `chmod -R 777`) → `dangerous`; every other risky tool → `confirm`. Patterns are a tuple constant with tests; not a policy engine.

### 4.4 Store (`store/`)

- `base.py`: ABCs `ProjectRepo, MissionRepo (incl. steps), SessionRepo, ApprovalRepo, EventRepo, SettingsRepo`, plus `Store` holding all six + `migrate()`.
- `sqlite.py`: implementations. One `sqlite3.Connection` per thread (`threading.local`), `row_factory=sqlite3.Row`, `PRAGMA journal_mode=WAL; foreign_keys=ON`. Sync API; async callers use `run_in_threadpool`.
- `migrations/0001_init.sql`: tables `projects, missions, mission_steps, sessions, approvals, events, settings`; indexes on `missions(status)`, `sessions(mission_id)`, `sessions(native_session_id)`, `approvals(session_id, status)`, `approvals(request_id) UNIQUE`, `events(mission_id, ts)`, `events(ts)`. Version in `settings.schema_version`. Applied in filename order at startup; each file runs in one transaction.
- Invariant enforced in `ApprovalRepo.insert`: at most one `pending` approval per `session_id` (partial unique index) — encodes the fix in commit `14bc293`.

### 4.5 Reconcile with existing runtime state

`SessionService.rehydrate()` (§5.1) wraps the tmux rehydration: restored handles with a DB row → row `status` refreshed from the runner; rows `running/needs_*` whose handle did not return → `lost` + `session.lost` event; returned handles with no row → new `AgentSession` with `mission_id=NULL`, `created_by="system"`. Nothing is deleted. `persist_name` to `meta.json` continues to be called.

---

## 5. Services and API (`backend/yuri/services/`, `backend/yuri/api/`)

### 5.1 `SessionService`

Only layer that talks to both repos and providers for sessions.

- `start(project_ref, backend, mode, model, name, created_by) -> dict`: `ProjectService.resolve_or_create(project_ref)` → create `Mission(status=running, title=session name, goal=None)` + step → `provider.create_session()` → `AgentSession` → events `mission.created`, `session.created` → journal line. Returns today's `start_session` dict + `mission_id`, `yuri_session_id`.
- `adopt(native_id, cwd, name)` — the `/session/handoff` path; same rows, `created_by="handoff"`, `provider.resume()`.
- `send(ref, message)`: sets mission `goal` if `None` (first 500 chars) → `provider.send_message` → `session.message_sent`, `last_activity_at`.
- `poll(ref) -> dict`: forwards `provider.poll()`; observes: `needs_permission` → `ApprovalService.record_request()` (dedup on `request_id`), session `needs_permission`, mission `waiting_for_approval`; `needs_choice` → `session.question`; `completed` → `session.turn_completed`, session `idle`, mission back to `running`; `error` → `agent.error`, mission `failed` only if it has no other live session.
- `answer(ref, choice)`: delegates to `ApprovalService.resolve_by_session` when a pending approval exists, else forwards `provider.answer` (a `choice` prompt).
- `interrupt(ref)`, `stop(ref)`: forward; `stop` → session `stopped`, and if the mission has no other live session → mission `paused` (never `completed`; §38).
- `set_mode(ref, mode)`: forwards; keeps today's `mode_covers` logic — a pending approval covered by the new mode is resolved `allowed` with `resolved_by="mode_switch"`.
- `rename, peek, read, send_keys, run_slash, handoff_info, list, resolve(ref)`: forward + keep the row current. `resolve` accepts Yuri id, native handle, 8-char prefix, or name (case-insensitive), raising `ValueError` with the names list — the soft-error contract `tools.py` relies on.
- `rehydrate()` per §4.5.

### 5.2 `ProjectService`

`list()` = registered rows ∪ discovered subfolders of allowed roots (`registered: false`), superset of today's `list_projects`. `resolve_or_create(ref)` uses `resolve_project_path` then upserts a row. `register(path, name?, default_agent?)`. `home()`.

### 5.3 `MissionService`

`list(status?)`, `get(id)` (with steps, sessions, approvals, last 50 events), `pause/resume/cancel(id, by)` with the §4.2 transition table; `cancel` also `provider.stop()`s live sessions. No `start()` in this phase.

### 5.4 `ApprovalService`

`pending()`, `record_request(session, prompt) -> Approval` (idempotent on `request_id`), `resolve(id, decision, by)`, `resolve_by_session(ref, choice, by)` using `decide_permission` (ambiguous → `ValueError`, fail closed), `risk_for` (§4.3). Emits `approval.requested` / `approval.resolved`; journal line on resolve.

### 5.5 Journal (`services/journal.py`)

`append(line)` → `journal/YYYY-MM-DD.md` as `- HH:MM  <line>`; file created with `# 2026-09-02` header. Called on `mission.created`, `mission.status_changed`, `session.turn_completed`, `approval.resolved`, `memory.remembered`.

### 5.6 Memory (`services/memory.py`)

`remember(fact, scope="user" | "project:<slug>")` appends `- YYYY-MM-DD  <fact>` to `memory/user.md` or `memory/projects/<slug>.md`. Path is built from a validated slug (`^[a-z0-9-]{1,64}$`), never from user text; refuses anything resolving outside `memory/`. `read_user(cap=4000)`, `read_today(cap=4000)`.

### 5.7 New voice tool

`remember` — `{fact: string, project?: string}`; result `{ok, path, message}`. Added to `TOOL_DEFINITIONS`; the `operating.ts` prompt tells Yuri when to use it (user states a preference, corrects her, or says "remember this").

### 5.8 REST (`api/routes.py`, `api/schemas.py`) — all under `Depends(require_auth)`

```
GET  /yuri/context                          {home, memory_user, journal_today, active_missions, agents}
GET  /yuri/projects        POST /yuri/projects        GET /yuri/projects/{id}
GET  /yuri/agents          GET  /yuri/agents/{id}/health
GET  /yuri/missions?status=                 GET /yuri/missions/{id}
POST /yuri/missions/{id}/pause|resume|cancel
GET  /yuri/sessions        GET  /yuri/sessions/{id}   POST /yuri/sessions/{id}/interrupt
GET  /yuri/approvals?status=pending
POST /yuri/approvals/{id}/approve|deny
GET  /yuri/events?mission_id=&since=&limit=   GET /yuri/events/stream (SSE)
```

Routes only validate and call services. Errors: `ValueError` → 400, `KeyError` → 404, `InvalidTransition` → 409. `/tools/execute`, `/session/*`, `/debug/*`, terminal WS unchanged. Frontend `next.config.mjs` proxy gains one `/yuri/:path*` rewrite.

### 5.9 Composition (`backend/yuri/app.py`)

`build_container(home_path, registry) -> Container` holds store, bus, services. `main.py` lifespan: config banner → `yuri.app.startup()` (home.ensure → store.migrate → registry → container → bus writer) → `event_log.start_writer()` → `SessionService.rehydrate()`. Shutdown: bus writer stop → provider shutdown → store close. `tools.py` and routes read services from `yuri.app.container()`; tests build their own container with a temp home and `FakeAgentProvider`.

---

## 6. EventBus and provider events (`backend/yuri/events/`)

### 6.1 Bus

`bus.publish(event: YuriEvent) -> YuriEvent` — sync, non-blocking, never raises. Sinks: (1) `EventRepo` via bounded `asyncio.Queue(20000)` drained by a background task (shared writer helper `events/writer.py`, also usable by `event_log` later); (2) live subscribers, `asyncio.Queue(2000)` each, drop-on-full; (3) bridge → `event_log.log_event(source="yuri", dest="ui", kind=event.type, summary=<type-specific one-liner>, session=<name or native id>, detail=payload)`. `subscribe()/unsubscribe()` mirror `event_log`.

Default severity/speakable per type: `tool.started`=debug/F · `session.message_sent`=debug/F · `cost.updated`=debug/F · `session.created`=info/F · `mission.created`=info/T · `mission.status_changed`=info/T · `session.turn_completed`=info/T · `session.question`=notice/T · `approval.requested`=notice/T · `approval.resolved`=info/F · `session.interrupted`=info/F · `session.stopped`=info/F · `session.lost`=warning/T · `agent.error`=error/T · `project.registered`=info/F · `memory.remembered`=info/F.

### 6.2 Provider → domain mapping

Two hops, one boundary each:

1. **Runner → `ProviderEvent`** (inside `ClaudeCodeProvider._on_runner_event`, Claude-specific):

| Runner/hook event | `ProviderEvent.kind` | payload |
|---|---|---|
| `tool` | `tool_started` | `tool_name, tool_input` |
| `needs_permission` | `needs_permission` | `request_id, tool_name, tool_input, text, options` |
| `needs_choice` | `needs_choice` | `request_id, text, options, multi_select` |
| `turn_complete` | `turn_completed` | `assistant_text[:2000], tools_used` |
| cost delta (`_update_cost`) | `cost_updated` | `model, input_tokens?, output_tokens?, cost_usd?` — nullable (§40) |
| runner exception | `error` | `message` |

Hooks see only a tool's start; no `tool_completed` is fabricated.

2. **`ProviderEvent` → `YuriEvent` + row updates** (inside `SessionService.on_provider_event(agent_id, handle, ev)`, provider-neutral): `tool_started→tool.started`, `needs_permission→approval.requested` (via `ApprovalService.record_request`, same dedup as the poll path), `needs_choice→session.question`, `turn_completed→session.turn_completed`, `cost_updated→cost.updated`, `error→agent.error`. Resolves `handle → AgentSession` to stamp `mission_id/session_id/project_id`.

Wired at container build: `provider.set_observer(lambda h, ev: session_service.on_provider_event(provider.id, h, ev))`. Provider code never imports the store or the domain.

### 6.3 SSE

`GET /yuri/events/stream?limit=200&mission_id=`: replay from `EventRepo`, then live; `: ping` every 15 s; same headers as `/debug/stream`.

---

## 7. Yuri's voice (frontend)

- `frontend/lib/persona.ts` — who she is: name Yuri, lives in `~/Yuri`, calm/concise/proactive (§37), the said/did/verified rule (§38), never claims completion without a verified result, asks for approval on `confirm`/`dangerous`, delegates all real work to agents, refers to Claude Code as *an agent she runs*, uses `remember` for durable facts.
- `frontend/lib/operating.ts` — every current operational rule from `instructions.ts` verbatim (tool usage, duplicate-start rule, names, slash commands, modes, co-driving, send_keys, mute), with "Claude" → "the agent" only where it's the voice's self-description, not where it names the `tell_claude`/`answer_prompt` tools.
- `frontend/lib/instructions.ts` — `export const INSTRUCTIONS = PERSONA + "\n\n" + OPERATING;` so `VoiceAgent.tsx` is unchanged there.
- `VoiceAgent.tsx` connect path: fetch `/yuri/context`; append `WHAT YOU REMEMBER:\n<memory_user>\n\nTODAY SO FAR:\n<journal_today>\n\nACTIVE MISSIONS:\n…` to the existing snapshot block. Failure to fetch → connect proceeds without it (logged to `/debug/log`).
- Assembly is a pure function `buildInstructions(snapshot, context)` in `frontend/lib/instructions.ts`, unit-tested with vitest.

---

## 8. Tests

Runner: `python -m unittest discover -s backend/tests` (existing convention: `sys.path.insert(0, backend)`), plus `vitest` (pinned devDependency) via `npm test`.

**Phase 1 (written first, against current code):** `test_tools_dispatch.py` (every tool's result keys, soft errors, sole-session fallback, duplicate guard), `test_session_manager.py` (`resolve_session`, names, `resolve_project_path` containment incl. `..`, symlinked root, `<root>-evil`), `test_permissions.py`, `test_event_log.py`, `test_tmux_rehydrate.py` (temp store, `_tmux` patched), `test_main_auth.py`.

**Phase 2:** `test_provider_contract.py` (parametrized base over Fake + ClaudeCodeProvider-with-fake-runners), `test_registry.py`.

**Phase 3:** `test_home.py`, `test_store.py` (migrations idempotent, round-trips, transitions, one-pending-approval), `test_risk.py`, `test_event_bus.py`, `test_claude_provider_events.py`, `test_session_service.py`, `test_mission_service.py`, `test_approval_service.py`, `test_journal_memory.py`, `test_yuri_api.py` (TestClient through auth), `frontend/lib/instructions.test.ts`.

All tests run without tmux, Claude, network, or voice keys.

---

## 9. Order of work, checkpoints, verification

1. This spec (done). Phase 1 tests; vitest wiring.
2. Providers + registry; `tools.py` on the registry. **Checkpoint A — live Gemini session.**
3. Home + domain + store. 4. EventBus + observer + mapping. 5. Services + write-through + handoff + rehydrate/reconcile. 6. API + context + `remember` + journal.
7. `persona.ts`/`operating.ts`; frontend context injection. **Checkpoint B — live Gemini session.**
8. `bin/yuri` (execs `bin/yapcode`; adds `yuri doctor`: home, migrations, `claude`/`tmux` presence, voice keys).

TDD throughout; no commits until told.

**Verification (§50):** all tests green · `run.sh` + `npm run dev` boot with banner `yuri: home=… db=… agents=[claude-code online]` · live Gemini: start session → tell → permission → deny → close; then `GET /yuri/missions` shows goal set and `paused`, one `denied` approval, journal lines, mission survives refresh · restart with CLI session open → rehydrated and row `running`; kill pane, restart → `lost` · `/voice-handoff` adopts · `send_keys`, `set_mode` with pending prompt, `/init` slash, terminal WS, `/debug/stream`, `cost_usd` each exercised once · Azure/OpenAI: tests only, reported as not exercised live.

**Known risk:** `tmux_runner.py` has no tests; `test_tmux_rehydrate.py` is the only guard on its most fragile path. If it cannot be isolated from real tmux without refactoring, stop and report rather than skip.

---

## 10. Out of scope (this increment)

Orchestrator, AgentRouter, explicit voice mission commands (Phase 4) · OpenCode and Hermes providers (5, 8) · Mission Control UI routes (6) · multi-step / multi-agent missions (7) · narration quiet/normal/verbose modes (§16) · `YuriSelfProvider` (her own agent brain) · any moves toward the §33 directory layout · renaming `VC_*` env vars, `.yapcode/` store, or the `yapcode` CLI · vector/semantic memory · remote access.
