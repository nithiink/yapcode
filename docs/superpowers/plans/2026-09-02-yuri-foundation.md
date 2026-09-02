# Yuri Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the existing Yapcode Claude integration behind an `AgentProvider`, add the Yuri domain (Project / Mission / Session / Approval / Event) with SQLite persistence in `~/Yuri`, an EventBus, and Yuri's persona + memory — with every existing voice flow still working.

**Architecture:** New code lives in `backend/yuri/` (providers → domain → store → events → services → api → app). `ClaudeCodeProvider` *wraps* the two existing runners (`TmuxClaudeRunner`, `SDKClaudeRunner`); `tools.py` handlers call `SessionService`, which writes mission/session/approval rows and publishes events as a side effect of the flows that already exist. Nothing moves; `tmux_runner.py` changes by ~15 lines (an optional observer).

**Tech Stack:** Python 3.14 (backend venv at `backend/.venv`), FastAPI, stdlib `sqlite3`, `unittest`; Next 16 / React 19 / TS, `node --test` (Node 24 runs `.ts` natively).

**Spec:** `docs/superpowers/specs/2026-09-02-yuri-foundation-design.md` — read it first; §n references below point at it.

## Global Constraints

- **NO COMMITS.** The user has said "do not commit anything until I tell you". Every task ends with running the full suite instead of `git commit`. Work stays on branch `feat/yuri-foundation`.
- No new Python dependencies (`backend/requirements.txt` unchanged). No new npm dependencies (spec said vitest; the repo already runs `.test.ts` via `node --test`, so use that).
- Additive only: keep `VC_*` env vars, `.yapcode/tmux` store, `bin/yapcode`, module names, `/tools/execute`, `TOOL_DEFINITIONS` names/args (one new tool `remember` is allowed).
- Tests run without tmux, Claude, network, or voice keys: `cd backend && .venv/bin/python -m unittest discover -s tests` and `cd frontend && npm test`.
- Backend tests import with `sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))` then `import tools` etc. (existing convention).
- Every new backend file starts with `from __future__ import annotations`. Type hints, short module docstring explaining *why* (match existing style).
- Timestamps: ISO-8601 UTC with `Z`, ms precision (`event_log._utcnow_iso` style). IDs: `uuid4()` strings.
- Yuri is she/her. Never log secrets. Fail closed on anything security-relevant.
- Baseline before Task 1: `40 tests OK` (backend), `5 pass` (frontend). Every task must leave both green plus its own new tests.

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `backend/yuri/__init__.py` | package marker |
| `backend/yuri/providers/base.py` | `AgentProvider` ABC + `AgentCapabilities`, `AgentHealth`, `ProjectContext`, `SessionOptions`, `ProviderEvent` |
| `backend/yuri/providers/fake.py` | `FakeAgentProvider` test double |
| `backend/yuri/providers/claude_code.py` | `ClaudeCodeProvider` adapter over the two runners |
| `backend/yuri/providers/registry.py` | `AgentRegistry` |
| `backend/yuri/home.py` | `~/Yuri` layout (`ensure`, paths) |
| `backend/yuri/domain/{__init__,ids,project,mission,session,approval,event,risk}.py` | pure dataclasses / enums / transitions / risk classifier |
| `backend/yuri/store/{__init__,base,sqlite}.py`, `backend/yuri/store/migrations/0001_init.sql` | repositories |
| `backend/yuri/events/bus.py` | `EventBus` |
| `backend/yuri/services/{journal,memory,projects,approvals,missions,sessions}.py` | application services |
| `backend/yuri/api/{schemas,routes}.py` | FastAPI router under `/yuri` |
| `backend/yuri/app.py` | `Container`, `startup()`, `shutdown()` |
| `backend/yuri/doctor.py` | `yuri doctor` checks |
| `bin/yuri` | launcher (`doctor`, else exec `bin/yapcode`) |
| `frontend/lib/persona.ts`, `frontend/lib/operating.ts` | split voice prompt |
| `frontend/lib/instructions.test.ts` | prompt assembly tests |
| `frontend/app/api/yuri/[...path]/route.ts` | same-origin proxy to `/yuri/*` |
| `backend/tests/test_*.py` (listed per task) | |

**Modified**

| Path | Change |
|---|---|
| `backend/claude_runner.py` | `ClaudeRunner.on_event` attribute; SDK runner calls it |
| `backend/tmux_runner.py` | `_notify()` helper called from `_handle_event`, `_update_cost` |
| `backend/config.py` | `YURI_HOME`, `YURI_AGENTS`; `allowed_project_roots()` appends the home |
| `backend/session_manager.py` | runner registry functions become shims over the provider; names/project helpers stay |
| `backend/tools.py` | handlers call `SessionService`; `remember` tool |
| `backend/main.py` | lifespan builds the container; `include_router`; handoff + terminal WS use the service |
| `frontend/lib/instructions.ts` | `INSTRUCTIONS = PERSONA + OPERATING`; `yuriContextBlock()` |
| `frontend/components/VoiceAgent.tsx` | fetch `/api/yuri/context` at connect and append |
| `frontend/package.json` | `"test": "node --test lib/*.test.ts"` |
| `backend/tests/test_set_mode_prompt_sync.py` | re-based on the container + fake provider |

---

## Phase 1 — Safety net (Tasks 1–5). Written against CURRENT code; must pass before any refactor.

### Task 1: Permission + event_log tests

**Files:**
- Test: `backend/tests/test_permissions.py`
- Test: `backend/tests/test_event_log.py`

**Interfaces:** Consumes `permissions.classify/mode_covers/is_plan_file_write`, `claude_runner.decide_permission`, `event_log.log_event/recent/subscribe/unsubscribe/_buffer`.

- [ ] **Step 1: Write `backend/tests/test_permissions.py`**

```python
"""Pins the permission policy the voice approval flow depends on (Phase 1
safety net — see docs/superpowers/plans/2026-09-02-yuri-foundation.md).

    python -m unittest discover -s backend/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import permissions  # noqa: E402
from claude_runner import decide_permission  # noqa: E402


class Classify(unittest.TestCase):
    def test_read_only_tools_are_safe(self):
        for t in ["Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "ToolSearch"]:
            self.assertEqual(permissions.classify(t), "safe", t)

    def test_question_tool(self):
        self.assertEqual(permissions.classify("AskUserQuestion"), "question")

    def test_everything_else_is_risky(self):
        for t in ["Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "mcp__foo__bar", ""]:
            self.assertEqual(permissions.classify(t), "risky", t)

    def test_chrome_mcp_prefix_is_safe(self):
        self.assertEqual(permissions.classify("mcp__claude-in-chrome__navigate"), "safe")


class ModeCovers(unittest.TestCase):
    def test_auto_covers_all(self):
        self.assertTrue(permissions.mode_covers("auto", "Bash"))

    def test_accept_edits_covers_only_edit_tools(self):
        self.assertTrue(permissions.mode_covers("acceptEdits", "Edit"))
        self.assertFalse(permissions.mode_covers("acceptEdits", "Bash"))

    def test_default_and_plan_cover_nothing(self):
        self.assertFalse(permissions.mode_covers("default", "Edit"))
        self.assertFalse(permissions.mode_covers("plan", "Edit"))


class PlanFileWrite(unittest.TestCase):
    def test_write_inside_plans_dir(self):
        fp = os.path.join(permissions._PLANS_DIR, "x.md")
        self.assertTrue(permissions.is_plan_file_write("Write", {"file_path": fp}))

    def test_write_elsewhere(self):
        self.assertFalse(permissions.is_plan_file_write("Write", {"file_path": "/tmp/x.md"}))

    def test_non_edit_tool(self):
        fp = os.path.join(permissions._PLANS_DIR, "x.md")
        self.assertFalse(permissions.is_plan_file_write("Bash", {"file_path": fp}))

    def test_traversal_out_of_plans_dir(self):
        fp = os.path.join(permissions._PLANS_DIR, "..", "settings.json")
        self.assertFalse(permissions.is_plan_file_write("Write", {"file_path": fp}))


class DecidePermission(unittest.TestCase):
    def test_allow_words(self):
        for c in ["yes", "y", "allow", "Yes, go ahead", "approve", "ok", "sure"]:
            self.assertEqual(decide_permission(c), "allow", c)

    def test_deny_words_win(self):
        for c in ["no", "deny", "yes but don't", "stop", "nope"]:
            self.assertEqual(decide_permission(c), "deny", c)

    def test_ambiguous_is_none(self):
        for c in ["", "maybe", "your call"]:
            self.assertIsNone(decide_permission(c), c)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run it**

Run: `cd backend && .venv/bin/python -m unittest tests.test_permissions -v`
Expected: all PASS (this pins current behavior; if `"y"` turns out to be an allow word, change that assertion to match reality — the point is to pin, not to change).

- [ ] **Step 3: Write `backend/tests/test_event_log.py`**

```python
"""Pins the debug event bus (ring buffer + fan-out + drop-on-full) that the
Activity panel and the future Yuri EventBus bridge rely on.

    python -m unittest discover -s backend/tests
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import event_log  # noqa: E402


class EventLog(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        event_log._buffer.clear()
        event_log._subscribers.clear()

    async def test_record_shape_and_truncation(self):
        rec = event_log.log_event("voice", "backend", "tool_call", "x" * 1000,
                                  session="s1", detail={"a": 1})
        self.assertEqual(set(rec), {"seq", "ts", "source", "dest", "kind", "session",
                                    "summary", "detail"})
        self.assertEqual(len(rec["summary"]), 600)
        self.assertTrue(rec["ts"].endswith("Z"))

    async def test_recent_returns_oldest_first_and_honors_limit(self):
        for i in range(5):
            event_log.log_event("a", "b", "info", str(i))
        self.assertEqual([r["summary"] for r in event_log.recent(2)], ["3", "4"])
        self.assertEqual(len(event_log.recent(0)), 5)

    async def test_subscriber_receives_live_events(self):
        q = event_log.subscribe()
        try:
            event_log.log_event("a", "b", "info", "hello")
            rec = await asyncio.wait_for(q.get(), 1.0)
            self.assertEqual(rec["summary"], "hello")
        finally:
            event_log.unsubscribe(q)

    async def test_slow_subscriber_drops_instead_of_blocking(self):
        q = event_log.subscribe()
        try:
            for i in range(q.maxsize + 50):
                event_log.log_event("a", "b", "info", str(i))
            self.assertEqual(q.qsize(), q.maxsize)
        finally:
            event_log.unsubscribe(q)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run both + full suite**

Run: `cd backend && .venv/bin/python -m unittest discover -s tests`
Expected: `OK`, count = 40 + new tests.

---

### Task 2: session_manager tests (resolution + sandbox)

**Files:**
- Test: `backend/tests/test_session_manager.py`

**Interfaces:** Consumes `session_manager.resolve_session/set_session_name/default_name_for/resolve_project_path/list_projects/_runners/_names/_owner`.

- [ ] **Step 1: Write the test**

```python
"""Pins session_manager: name/handle resolution and the mandatory project
sandbox (`resolve_project_path`). Runner stubbed — no tmux.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import session_manager as sm  # noqa: E402


class _StubRunner:
    def __init__(self, handles):
        self.handles = list(handles)
        self.persisted = {}

    def list(self):
        return [{"handle": h, "session_id": h, "cwd": "/tmp", "model": "opus",
                 "mode": "default", "status": "idle", "cost_usd": 0.0} for h in self.handles]

    def persist_name(self, handle, name):
        self.persisted[handle] = name


class ResolveSession(unittest.TestCase):
    def setUp(self):
        self.runner = _StubRunner(["aaaaaaaa-1111", "bbbbbbbb-2222"])
        self._p = mock.patch.dict(sm._runners, {"cli": self.runner}, clear=True)
        self._p.start()
        sm._owner.clear()
        sm._names.clear()
        sm._owner["aaaaaaaa-1111"] = "cli"
        sm._owner["bbbbbbbb-2222"] = "cli"

    def tearDown(self):
        self._p.stop()
        sm._owner.clear()
        sm._names.clear()

    def test_exact_handle(self):
        self.assertEqual(sm.resolve_session("aaaaaaaa-1111"), "aaaaaaaa-1111")

    def test_unique_prefix(self):
        self.assertEqual(sm.resolve_session("bbbb"), "bbbbbbbb-2222")

    def test_name_case_insensitive(self):
        sm.set_session_name("aaaaaaaa-1111", "Billing Fix")
        self.assertEqual(sm.resolve_session("billing fix"), "aaaaaaaa-1111")
        self.assertEqual(self.runner.persisted["aaaaaaaa-1111"], "Billing Fix")

    def test_unknown_lists_names(self):
        sm.set_session_name("aaaaaaaa-1111", "alpha")
        with self.assertRaises(KeyError) as cm:
            sm.resolve_session("zzz")
        self.assertIn("alpha", str(cm.exception))

    def test_duplicate_name_rejected(self):
        sm.set_session_name("aaaaaaaa-1111", "same")
        with self.assertRaises(ValueError):
            sm.set_session_name("bbbbbbbb-2222", "SAME")

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            sm.set_session_name("aaaaaaaa-1111", "   ")

    def test_default_name_dedupes(self):
        sm._names["aaaaaaaa-1111"] = "proj"
        self.assertEqual(sm.default_name_for("/x/proj"), "proj 2")

    def test_list_all_sessions_adds_backend_and_name(self):
        sm.set_session_name("aaaaaaaa-1111", "n1")
        out = {s["handle"]: s for s in sm.list_all_sessions()}
        self.assertEqual(out["aaaaaaaa-1111"]["backend"], "cli")
        self.assertEqual(out["aaaaaaaa-1111"]["name"], "n1")
        self.assertIsNone(out["bbbbbbbb-2222"]["name"])


class ResolveProjectPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "Alpha"))
        os.mkdir(os.path.join(self.root, "beta"))
        os.mkdir(self.root + "-evil")
        self._env = mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        import shutil
        shutil.rmtree(self.root + "-evil", ignore_errors=True)
        self.tmp.cleanup()

    def test_empty_defaults_to_first_root(self):
        self.assertEqual(sm.resolve_project_path(""), self.root)
        self.assertEqual(sm.resolve_project_path("anywhere"), self.root)

    def test_absolute_contained(self):
        self.assertEqual(sm.resolve_project_path(os.path.join(self.root, "Alpha")),
                         os.path.join(self.root, "Alpha"))

    def test_fuzzy_name_case_insensitive(self):
        self.assertEqual(sm.resolve_project_path("alpha"), os.path.join(self.root, "Alpha"))
        self.assertEqual(sm.resolve_project_path("BETA"), os.path.join(self.root, "beta"))

    def test_traversal_rejected(self):
        with self.assertRaises(ValueError):
            sm.resolve_project_path(os.path.join(self.root, "..", ".."))

    def test_sibling_root_rejected(self):
        with self.assertRaises(ValueError):
            sm.resolve_project_path(self.root + "-evil")

    def test_outside_rejected(self):
        with self.assertRaises(ValueError):
            sm.resolve_project_path("/etc")

    def test_symlinked_root_resolves(self):
        link = self.root + "-link"
        os.symlink(self.root, link)
        try:
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": link}):
                self.assertEqual(sm.resolve_project_path("alpha"),
                                 os.path.join(self.root, "Alpha"))
        finally:
            os.unlink(link)

    def test_fails_closed_without_roots(self):
        with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": ""}):
            with self.assertRaises(ValueError):
                sm.resolve_project_path("alpha")

    def test_list_projects_skips_hidden(self):
        os.mkdir(os.path.join(self.root, ".hidden"))
        names = [p["name"] for p in sm.list_projects()["projects"]]
        self.assertEqual(names, ["Alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run**

Run: `cd backend && .venv/bin/python -m unittest tests.test_session_manager -v`
Expected: PASS. If any assertion disagrees with actual behavior, fix the assertion (pin reality), not the code.

- [ ] **Step 3: Full suite**

Run: `cd backend && .venv/bin/python -m unittest discover -s tests` → `OK`.

---

### Task 3: tools dispatch snapshot tests

**Files:**
- Test: `backend/tests/test_tools_dispatch.py`

**Interfaces:** Consumes `tools.dispatch_tool`, `tools.TOOL_DEFINITIONS`, `tools._last_start`, `session_manager` registry dicts. **Produces** the result-key contract that Task 17 must preserve.

- [ ] **Step 1: Write the test**

```python
"""Snapshot of every voice tool's result contract, taken BEFORE the provider /
service refactor. If a later task changes a key here, that is a user-visible
regression for the voice model — fix the code, not this file.

Runner is a stub injected into session_manager's registry; no tmux/Claude.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import session_manager as sm  # noqa: E402
import tools  # noqa: E402


class _StubRunner:
    """Mimics ClaudeRunner's surface with recorded calls and canned answers."""

    def __init__(self):
        self.sessions = {}
        self.calls = []
        self.next_poll = {"status": "idle"}
        self.persisted = {}

    async def start(self, cwd, model=None, mode="default"):
        h = f"h{len(self.sessions) + 1}-" + "0" * 8
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": cwd, "model": model or "opus",
                            "mode": mode, "status": "idle", "cost_usd": 0.0, "queued": 0}
        self.calls.append(("start", cwd, model, mode))
        return h

    def list(self):
        return list(self.sessions.values())

    def start_advance(self, h, msg):
        self.calls.append(("advance", h, msg))
        self.sessions[h]["status"] = "working"

    def start_answer(self, h, choice):
        self.calls.append(("answer", h, choice))

    def start_builtin_slash(self, h, text):
        self.calls.append(("slash", h, text))

    def poll_status(self, h):
        return {**self.next_poll, "session_id": h}

    async def interrupt(self, h):
        self.calls.append(("interrupt", h))

    async def close(self, h):
        self.calls.append(("close", h))
        self.sessions.pop(h)

    async def set_mode(self, h, mode):
        self.sessions[h]["mode"] = mode
        return mode

    async def read(self, h):
        return "assistant text"

    async def peek(self, h, lines=40):
        return "screen"

    async def send_keys(self, h, items):
        return {"session_id": h, "screen": "after keys", "sent": items}

    def pane_for(self, h):
        return f"vc_{h[:8]}"

    def persist_name(self, h, name):
        self.persisted[h] = name

    async def shutdown(self):
        pass


class ToolsDispatch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = _StubRunner()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "proj"))
        self.patches = [
            mock.patch.dict(sm._runners, {"cli": self.runner}, clear=True),
            mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
        ]
        for p in self.patches:
            p.start()
        sm._owner.clear()
        sm._names.clear()
        tools._last_start = None

    def tearDown(self):
        for p in self.patches:
            p.stop()
        sm._owner.clear()
        sm._names.clear()
        tools._last_start = None
        self.tmp.cleanup()

    async def _start(self, **kw):
        return await tools.dispatch_tool("start_session", {"project_path": "proj", **kw})

    def test_every_definition_has_name_and_object_params(self):
        for d in tools.TOOL_DEFINITIONS:
            self.assertEqual(d["type"], "function")
            self.assertEqual(d["parameters"]["type"], "object")
        names = [d["name"] for d in tools.TOOL_DEFINITIONS]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("poll_session", names)  # app-level only

    async def test_list_projects_keys(self):
        out = await tools.dispatch_tool("list_projects", {})
        self.assertEqual(set(out), {"roots", "projects"})

    async def test_start_session_keys_and_default_name(self):
        out = await self._start()
        self.assertEqual(set(out), {"session_id", "name", "project_path", "backend", "mode",
                                    "message"})
        self.assertEqual(out["name"], "proj")
        self.assertEqual(out["backend"], "cli")
        self.assertEqual(out["project_path"], os.path.join(self.root, "proj"))

    async def test_start_session_duplicate_guard(self):
        first = await self._start()
        second = await self._start()
        self.assertTrue(second["duplicate_guard"])
        self.assertEqual(second["existing_session"]["session_id"], first["session_id"])
        third = await self._start(another=True)
        self.assertNotIn("duplicate_guard", third)
        self.assertEqual(third["name"], "proj 2")

    async def test_list_sessions_keys(self):
        await self._start()
        out = await tools.dispatch_tool("list_sessions", {})
        s = out["sessions"][0]
        for k in ["handle", "session_id", "cwd", "model", "mode", "status", "cost_usd",
                  "backend", "name"]:
            self.assertIn(k, s)

    async def test_tell_claude_returns_working(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("tell_claude", {"session_id": sid, "message": "hi"})
        self.assertEqual(out, {"status": "working", "session_id": sid})
        self.assertIn(("advance", sid, "hi"), self.runner.calls)

    async def test_session_id_falls_back_to_sole_session(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("tell_claude", {"message": "hi"})
        self.assertEqual(out["session_id"], sid)

    async def test_no_sessions_is_soft_error(self):
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("tell_claude", {"message": "hi"})

    async def test_ambiguous_session_is_soft_error_listing_names(self):
        await self._start()
        await self._start(another=True, name="second")
        with self.assertRaises(ValueError) as cm:
            await tools.dispatch_tool("tell_claude", {"message": "hi"})
        self.assertIn("second", str(cm.exception))

    async def test_unknown_session_is_soft_error(self):
        await self._start()
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("tell_claude", {"session_id": "nope", "message": "x"})

    async def test_answer_prompt(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("answer_prompt", {"session_id": sid, "choice": "allow"})
        self.assertEqual(out, {"status": "working", "session_id": sid})
        self.assertIn(("answer", sid, "allow"), self.runner.calls)

    async def test_poll_session_forwards(self):
        sid = (await self._start())["session_id"]
        self.runner.next_poll = {"status": "completed", "assistant_text": "done"}
        out = await tools.dispatch_tool("poll_session", {"session_id": sid})
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["session_id"], sid)

    async def test_interrupt_and_close(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("interrupt_session", {"session_id": sid})
        self.assertEqual(out, {"status": "interrupted", "session_id": sid})
        out = await tools.dispatch_tool("close_session", {"session_id": sid})
        self.assertEqual(out, {"status": "closed", "session_id": sid})
        self.assertEqual(await tools.dispatch_tool("list_sessions", {}), {"sessions": []})

    async def test_rename(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("rename_session", {"session_id": sid, "name": "Neo"})
        self.assertEqual(set(out), {"session_id", "name", "message"})
        self.assertEqual(out["name"], "Neo")
        out = await tools.dispatch_tool("tell_claude", {"session_id": "neo", "message": "x"})
        self.assertEqual(out["session_id"], sid)

    async def test_set_mode_without_prompt(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("set_mode", {"session_id": sid, "mode": "plan"})
        self.assertEqual(out, {"session_id": sid, "mode": "plan"})

    async def test_read_and_peek(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("read_session", {"session_id": sid})
        self.assertEqual(out, {"session_id": sid, "text": "assistant text"})
        out = await tools.dispatch_tool("peek_screen", {"session_id": sid})
        self.assertEqual(out["screen"], "screen")
        self.assertEqual(out["session_id"], sid)

    async def test_get_handoff_keys(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("get_handoff", {"session_id": sid})
        self.assertEqual(set(out), {"session_id", "name", "cwd", "attach_command",
                                    "resume_command", "command"})
        self.assertTrue(out["attach_command"].startswith("tmux attach -t vc_"))
        self.assertIn("claude --resume", out["resume_command"])

    async def test_send_keys(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("send_keys", {"session_id": sid,
                                                      "items": [{"key": "Escape"}]})
        self.assertEqual(out["screen"], "after keys")
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("send_keys", {"session_id": sid, "items": []})

    async def test_run_slash_command(self):
        sid = (await self._start())["session_id"]
        out = await tools.dispatch_tool("run_slash_command",
                                        {"session_id": sid, "command": "/init", "args": "x"})
        self.assertEqual(out, {"status": "working", "session_id": sid, "sent": "/init x"})
        self.assertIn(("slash", sid, "/init x"), self.runner.calls)

    async def test_list_slash_commands_keys(self):
        out = await tools.dispatch_tool("list_slash_commands", {})
        self.assertIn("commands", out)

    async def test_unknown_tool(self):
        with self.assertRaises(KeyError):
            await tools.dispatch_tool("nope", {})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run**

Run: `cd backend && .venv/bin/python -m unittest tests.test_tools_dispatch -v`
Expected: PASS. Investigate any failure by reading `tools.py` — the stub may need one more method; add it to the stub. Do not change `tools.py`.

- [ ] **Step 3: Full suite** → `OK`.

---

### Task 4: tmux rehydration test (no real tmux)

**Files:**
- Test: `backend/tests/test_tmux_rehydrate.py`

**Interfaces:** Consumes `tmux_runner.TmuxClaudeRunner.rehydrate/_tmux/CTRL_ROOT/_find_transcript`, `shutil.which`.

- [ ] **Step 1: Write the test**

```python
"""Pins TmuxClaudeRunner.rehydrate() — the most fragile untested path in the
repo. tmux is faked at `_tmux`, the control store is a temp dir.

    python -m unittest discover -s backend/tests
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import tmux_runner  # noqa: E402

H1 = "11111111-aaaa-bbbb-cccc-000000000001"
H2 = "22222222-aaaa-bbbb-cccc-000000000002"
H3 = "33333333-aaaa-bbbb-cccc-000000000003"


def _mk_ctrl(root, handle, meta, events=None, pane_alive_name=None):
    ctrl = os.path.join(root, handle)
    os.makedirs(ctrl)
    if meta is not None:
        with open(os.path.join(ctrl, "meta.json"), "w") as f:
            json.dump({"handle": handle, "cwd": "/tmp", "model": "opus",
                       "pane": pane_alive_name or f"vc_{handle[:8]}", "mode": "default",
                       "name": None, **meta}, f)
    if events:
        with open(os.path.join(ctrl, "events.jsonl"), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
    return ctrl


class FakeTmux:
    def __init__(self, live, pane_text=""):
        self.live = set(live)
        self.pane_text = pane_text
        self.calls = []

    async def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "list-sessions":
            return 0, "\n".join(sorted(self.live))
        if args[0] == "capture-pane":
            return 0, self.pane_text
        if args[0] == "has-session":
            return (0 if args[2] in self.live else 1), ""
        return 0, ""


class Rehydrate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.patches = [
            mock.patch.object(tmux_runner, "CTRL_ROOT", self.root),
            mock.patch.object(tmux_runner.shutil, "which", lambda n: "/usr/bin/tmux"),
            mock.patch.object(tmux_runner.TmuxClaudeRunner, "_find_transcript",
                              lambda self, s: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_live_pane_restored_dead_pane_gc(self):
        _mk_ctrl(self.root, H1, {"name": "alive"})
        _mk_ctrl(self.root, H2, {"name": "dead"})
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"}, pane_text="accept edits on")
        restored = await runner.rehydrate()
        self.assertEqual([r["handle"] for r in restored], [H1])
        self.assertEqual(restored[0]["name"], "alive")
        self.assertEqual(restored[0]["mode"], "acceptEdits")  # from the live footer
        self.assertFalse(os.path.isdir(os.path.join(self.root, H2)))  # gc'd
        self.assertIn(H1, runner._sessions)
        with open(os.path.join(self.root, H1, "mode")) as f:
            self.assertEqual(f.read().strip(), "acceptEdits")  # resynced for the hook
        await runner.shutdown()

    async def test_unreadable_meta_left_alone(self):
        _mk_ctrl(self.root, H3, None)
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H3[:8]}"})
        self.assertEqual(await runner.rehydrate(), [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, H3)))

    async def test_pending_permission_restored(self):
        _mk_ctrl(self.root, H1, {}, events=[
            {"event": "tool", "tool_name": "Read"},
            {"event": "needs_permission", "tool_name": "Bash",
             "tool_input": {"command": "rm x"}, "tool_use_id": "tu1"},
        ])
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"})
        restored = await runner.rehydrate()
        self.assertEqual(restored[0]["status"], "needs_permission")
        s = runner._sessions[H1]
        self.assertEqual(s.pending.kind, "permission")
        self.assertEqual(s.pending.tool_name, "Bash")
        self.assertEqual(s.pending_tool_use_id, "tu1")
        listed = runner.list()[0]
        self.assertEqual(listed["prompt"]["tool_name"], "Bash")
        await runner.shutdown()

    async def test_idempotent(self):
        _mk_ctrl(self.root, H1, {})
        runner = tmux_runner.TmuxClaudeRunner()
        runner._tmux = FakeTmux(live={f"vc_{H1[:8]}"})
        await runner.rehydrate()
        self.assertEqual(await runner.rehydrate(), [])
        await runner.shutdown()

    async def test_no_tmux_binary_returns_empty(self):
        with mock.patch.object(tmux_runner.shutil, "which", lambda n: None):
            runner = tmux_runner.TmuxClaudeRunner()
            self.assertEqual(await runner.rehydrate(), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run**

Run: `cd backend && .venv/bin/python -m unittest tests.test_tmux_rehydrate -v`
Expected: PASS. Known gotcha: `_adopt` starts `_tail_events` as a task — `shutdown()` cancels it; always call `await runner.shutdown()` in tests that restore a session (`VC_KILL_SESSIONS_ON_SHUTDOWN` is unset, so shutdown only detaches).

**If this cannot be made to pass without modifying `tmux_runner.py`: STOP and report to the user** (spec §9 known risk). Do not skip the test.

- [ ] **Step 3: Full suite** → `OK`.

---

### Task 5: auth matrix test + frontend `npm test`

**Files:**
- Test: `backend/tests/test_main_auth.py`
- Modify: `frontend/package.json` (scripts)

- [ ] **Step 1: Write `backend/tests/test_main_auth.py`**

```python
"""Pins the access decision shared by HTTP and WebSocket paths.

    python -m unittest discover -s backend/tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import main  # noqa: E402


class AccessOk(unittest.TestCase):
    def test_no_token_configured_loopback_only(self):
        with mock.patch.object(config, "AUTH_TOKEN", ""):
            self.assertTrue(main._access_ok("127.0.0.1", None)[0])
            self.assertTrue(main._access_ok("::1", None)[0])
            ok, reason = main._access_ok("192.168.1.5", None)
            self.assertFalse(ok)
            self.assertIn("VC_AUTH_TOKEN", reason)

    def test_token_configured_required_everywhere(self):
        with mock.patch.object(config, "AUTH_TOKEN", "secret"):
            self.assertFalse(main._access_ok("127.0.0.1", None)[0])
            self.assertFalse(main._access_ok("127.0.0.1", "wrong")[0])
            self.assertTrue(main._access_ok("10.0.0.2", "secret")[0])


class TokenFrom(unittest.TestCase):
    def test_bearer_header(self):
        self.assertEqual(main._token_from({"authorization": "Bearer abc"}, {}), "abc")

    def test_x_vc_token_header(self):
        self.assertEqual(main._token_from({"x-vc-token": " abc "}, {}), "abc")

    def test_query_param(self):
        self.assertEqual(main._token_from({}, {"token": "q"}), "q")

    def test_missing(self):
        self.assertIsNone(main._token_from({}, {}))


class OriginAllowed(unittest.TestCase):
    def test_localhost_dev(self):
        self.assertTrue(config.origin_allowed("http://localhost:3000"))

    def test_private_lan_any_port(self):
        self.assertTrue(config.origin_allowed("https://192.168.1.20:3000"))

    def test_public_rejected(self):
        self.assertFalse(config.origin_allowed("https://evil.example.com"))

    def test_empty_rejected(self):
        self.assertFalse(config.origin_allowed(None))
        self.assertFalse(config.origin_allowed(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** `cd backend && .venv/bin/python -m unittest tests.test_main_auth -v` → PASS.

- [ ] **Step 3: Add the frontend test script** — in `frontend/package.json` `"scripts"`, add:

```json
    "test": "node --test lib/*.test.ts",
```

(after the `"start"` line; keep the trailing-comma structure valid).

- [ ] **Step 4: Run** `cd frontend && npm test` → `pass 5`.

- [ ] **Step 5: Full backend suite** → `OK`. **Phase 1 complete.**

---

## Phase 2 — AgentProvider (Tasks 6–8)

### Task 6: Provider contract types + FakeAgentProvider + contract test base

**Files:**
- Create: `backend/yuri/__init__.py`, `backend/yuri/providers/__init__.py`, `backend/yuri/providers/base.py`, `backend/yuri/providers/fake.py`
- Test: `backend/tests/provider_contract.py` (base class, not collected), `backend/tests/test_fake_provider.py`

**Interfaces — Produces (used by every later task):**
```python
# yuri.providers.base
AgentCapabilities(interactive_terminal, slash_commands, send_keys, permission_modes, supports_interrupt, supports_rehydrate, supports_resume, supports_events, cost_tracking)
AgentHealth(online: bool, version: str|None, detail: str, checked_at: str)
ProjectContext(project_id: str, root_path: str)
SessionOptions(backend="cli", mode="default", model=None, name=None)
ProviderEvent(kind: str, payload: dict)   # kinds: tool_started | needs_permission | needs_choice | turn_completed | cost_updated | error
Observer = Callable[[str, ProviderEvent], None]
class AgentProvider(ABC)  # methods exactly as in Step 2
utcnow_iso() -> str
# yuri.providers.fake
class FakeAgentProvider(AgentProvider): calls: list[tuple]; script(handle, result: dict); emit(handle, ev: ProviderEvent); sessions: dict[str, dict]
```

- [ ] **Step 1: Package markers**

`backend/yuri/__init__.py`:
```python
"""Yuri — the control plane above coding agents. New code only; the existing
Yapcode modules (tools, session_manager, *_runner) stay where they are and call
into this package. Layers, top-down: api → services → domain/store/events →
providers. Providers never import the store or the domain."""
```
`backend/yuri/providers/__init__.py`: empty file.

- [ ] **Step 2: Write `backend/yuri/providers/base.py`**

```python
"""AgentProvider — the contract every coding-agent backend implements.

Shaped to what the existing runners actually do: `send_message`/`answer` are
NON-BLOCKING (they kick off a turn and return) and `poll` returns the runner's
result dict — the voice model depends on "returns working instantly, poll later"
(see frontend/lib/operating.ts). Awaiting a turn to completion here would stall
the voice for minutes.
"""
from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


def utcnow_iso() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


@dataclass(frozen=True)
class AgentCapabilities:
    interactive_terminal: bool = False
    slash_commands: bool = False
    send_keys: bool = False
    permission_modes: tuple[str, ...] = ("default",)
    supports_interrupt: bool = True
    supports_rehydrate: bool = False
    supports_resume: bool = False
    supports_events: bool = False
    cost_tracking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"interactive_terminal": self.interactive_terminal,
                "slash_commands": self.slash_commands, "send_keys": self.send_keys,
                "permission_modes": list(self.permission_modes),
                "supports_interrupt": self.supports_interrupt,
                "supports_rehydrate": self.supports_rehydrate,
                "supports_resume": self.supports_resume,
                "supports_events": self.supports_events,
                "cost_tracking": self.cost_tracking}


@dataclass(frozen=True)
class AgentHealth:
    online: bool
    version: str | None
    detail: str
    checked_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return {"online": self.online, "version": self.version, "detail": self.detail,
                "checked_at": self.checked_at}


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    root_path: str


@dataclass(frozen=True)
class SessionOptions:
    backend: str = "cli"
    mode: str = "default"
    model: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ProviderEvent:
    """Provider-neutral runtime signal. kind ∈ tool_started | needs_permission |
    needs_choice | turn_completed | cost_updated | error. The provider never sees
    Yuri ids, missions, or the store — SessionService turns these into YuriEvents."""
    kind: str
    payload: dict[str, Any]


Observer = Callable[[str, ProviderEvent], None]


class AgentProvider(ABC):
    id: str = ""
    name: str = ""

    @abstractmethod
    def capabilities(self) -> AgentCapabilities: ...

    @abstractmethod
    async def health(self) -> AgentHealth: ...

    @abstractmethod
    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        """Start a new native session in project.root_path; returns the native handle."""

    @abstractmethod
    def send_message(self, handle: str, message: str) -> None: ...

    @abstractmethod
    def answer(self, handle: str, choice: str) -> None: ...

    @abstractmethod
    def poll(self, handle: str) -> dict[str, Any]:
        """Oldest unread turn result, or {"status": "working"|"idle", "session_id": handle}."""

    @abstractmethod
    async def interrupt(self, handle: str) -> None: ...

    @abstractmethod
    async def stop(self, handle: str) -> None: ...

    @abstractmethod
    async def set_mode(self, handle: str, mode: str) -> str: ...

    @abstractmethod
    async def read(self, handle: str) -> str: ...

    @abstractmethod
    async def peek(self, handle: str, lines: int = 40) -> str | None:
        """Live screen snapshot, or None when the backend has no TUI."""

    @abstractmethod
    def list_native(self) -> list[dict[str, Any]]:
        """Runner-shaped session dicts (handle, cwd, model, mode, status, cost_usd, prompt?,
        queued counts) tagged with "backend"."""

    @abstractmethod
    def set_observer(self, cb: Observer | None) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    # Optional surface — default "unsupported". Callers check capabilities() or catch.
    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        raise NotImplementedError(f"{self.id} does not support send_keys")

    def run_slash(self, handle: str, text: str) -> None:
        raise NotImplementedError(f"{self.id} does not support slash commands")

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        raise NotImplementedError(f"{self.id} does not support resume")

    def native_pane(self, handle: str) -> str | None:
        return None

    def backend_of(self, handle: str) -> str | None:
        return None

    async def rehydrate(self) -> list[dict[str, Any]]:
        return []
```

- [ ] **Step 3: Write `backend/yuri/providers/fake.py`**

```python
"""Deterministic in-memory AgentProvider for tests (spec §45). Records every
call, lets tests script poll results and fire observer events."""
from __future__ import annotations

from typing import Any

from .base import (AgentCapabilities, AgentHealth, AgentProvider, Observer, ProjectContext,
                   ProviderEvent, SessionOptions)


class FakeAgentProvider(AgentProvider):
    id = "fake"
    name = "Fake Agent"

    def __init__(self, *, online: bool = True, supports_terminal: bool = True):
        self.online = online
        self.supports_terminal = supports_terminal
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self._scripted: dict[str, list[dict[str, Any]]] = {}
        self._observer: Observer | None = None
        self._n = 0

    # --- test controls ----------------------------------------------------
    def script(self, handle: str, result: dict[str, Any]) -> None:
        self._scripted.setdefault(handle, []).append(result)

    def emit(self, handle: str, ev: ProviderEvent) -> None:
        if self._observer:
            self._observer(handle, ev)

    # --- contract ---------------------------------------------------------
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(interactive_terminal=self.supports_terminal,
                                 slash_commands=self.supports_terminal,
                                 send_keys=self.supports_terminal,
                                 permission_modes=("default", "acceptEdits", "plan", "auto"),
                                 supports_interrupt=True, supports_rehydrate=True,
                                 supports_resume=True, supports_events=True,
                                 cost_tracking=True)

    async def health(self) -> AgentHealth:
        return AgentHealth(online=self.online, version="fake-1", detail="ok" if self.online else "down")

    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        self._n += 1
        h = f"fake-{self._n}"
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": project.root_path,
                            "model": opts.model or "fake-model", "mode": opts.mode,
                            "status": "idle", "cost_usd": 0.0, "queued": 0,
                            "backend": opts.backend}
        self.calls.append(("create_session", h, project.root_path, opts))
        return h

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        self.sessions[native_session_id] = {"handle": native_session_id,
                                            "session_id": native_session_id,
                                            "cwd": project.root_path, "model": "fake-model",
                                            "mode": opts.mode, "status": "idle",
                                            "cost_usd": 0.0, "queued": 0, "backend": opts.backend}
        self.calls.append(("resume", native_session_id))
        return native_session_id

    def send_message(self, handle: str, message: str) -> None:
        self._get(handle)["status"] = "working"
        self.calls.append(("send_message", handle, message))

    def answer(self, handle: str, choice: str) -> None:
        self._get(handle)["status"] = "working"
        self.calls.append(("answer", handle, choice))

    def poll(self, handle: str) -> dict[str, Any]:
        self._get(handle)
        q = self._scripted.get(handle) or []
        if q:
            res = q.pop(0)
            self.sessions[handle]["status"] = res.get("status", "idle")
            return {**res, "session_id": handle}
        return {"status": self.sessions[handle]["status"] if self.sessions[handle]["status"] == "working" else "idle",
                "session_id": handle}

    async def interrupt(self, handle: str) -> None:
        self._get(handle)["status"] = "idle"
        self.calls.append(("interrupt", handle))

    async def stop(self, handle: str) -> None:
        self._get(handle)
        self.sessions.pop(handle)
        self.calls.append(("stop", handle))

    async def set_mode(self, handle: str, mode: str) -> str:
        self._get(handle)["mode"] = mode
        self.calls.append(("set_mode", handle, mode))
        return mode

    async def read(self, handle: str) -> str:
        self._get(handle)
        return "fake assistant text"

    async def peek(self, handle: str, lines: int = 40) -> str | None:
        self._get(handle)
        return "fake screen" if self.supports_terminal else None

    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        if not self.supports_terminal:
            raise NotImplementedError("no terminal")
        self._get(handle)
        self.calls.append(("send_keys", handle, items))
        return {"session_id": handle, "screen": "fake screen", "sent": items}

    def run_slash(self, handle: str, text: str) -> None:
        if not self.supports_terminal:
            raise NotImplementedError("no terminal")
        self._get(handle)["status"] = "working"
        self.calls.append(("run_slash", handle, text))

    def list_native(self) -> list[dict[str, Any]]:
        return [dict(s) for s in self.sessions.values()]

    def native_pane(self, handle: str) -> str | None:
        return f"fake_{handle}" if self.supports_terminal and handle in self.sessions else None

    def backend_of(self, handle: str) -> str | None:
        s = self.sessions.get(handle)
        return s["backend"] if s else None

    def set_observer(self, cb: Observer | None) -> None:
        self._observer = cb

    async def rehydrate(self) -> list[dict[str, Any]]:
        return []

    async def shutdown(self) -> None:
        self.sessions.clear()

    def _get(self, handle: str) -> dict[str, Any]:
        s = self.sessions.get(handle)
        if s is None:
            raise KeyError(f"unknown session: {handle}")
        return s
```

- [ ] **Step 4: Write the contract base `backend/tests/provider_contract.py`** (no `test_` prefix, so discovery doesn't collect it directly)

```python
"""Contract every AgentProvider must satisfy (spec §45). Subclass in a
test_*.py, set `make_provider()` and `project_root`, and the lifecycle
assertions run against your implementation."""
import unittest

from yuri.providers.base import ProjectContext, ProviderEvent, SessionOptions


class AgentProviderContract(unittest.IsolatedAsyncioTestCase):
    project_root = "/tmp"

    def make_provider(self):
        raise NotImplementedError

    def opts(self):
        return SessionOptions()

    async def asyncSetUp(self):
        self.p = self.make_provider()
        self.ctx = ProjectContext(project_id="p1", root_path=self.project_root)

    async def asyncTearDown(self):
        await self.p.shutdown()

    async def test_identity_and_capabilities(self):
        self.assertTrue(self.p.id)
        self.assertTrue(self.p.name)
        caps = self.p.capabilities()
        self.assertIsInstance(caps.to_dict()["permission_modes"], list)

    async def test_health_returns_health(self):
        h = await self.p.health()
        self.assertIsInstance(h.online, bool)
        self.assertTrue(h.checked_at.endswith("Z"))

    async def test_lifecycle_create_send_poll_stop(self):
        h = await self.p.create_session(self.ctx, self.opts())
        self.assertTrue(h)
        listed = {s["handle"]: s for s in self.p.list_native()}
        self.assertIn(h, listed)
        self.assertEqual(listed[h]["cwd"], self.project_root)
        self.p.send_message(h, "hello")
        res = self.p.poll(h)
        self.assertIn(res["status"], {"working", "idle", "completed"})
        self.assertEqual(res["session_id"], h)
        await self.p.interrupt(h)
        mode = await self.p.set_mode(h, "plan")
        self.assertEqual(mode, "plan")
        self.assertIsInstance(await self.p.read(h), str)
        await self.p.stop(h)
        self.assertNotIn(h, {s["handle"] for s in self.p.list_native()})

    async def test_unknown_handle_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.p.poll("does-not-exist")

    async def test_observer_receives_events(self):
        got = []
        self.p.set_observer(lambda h, ev: got.append((h, ev)))
        h = await self.p.create_session(self.ctx, self.opts())
        self._fire_event(h)
        self.assertTrue(got, "observer never called")
        self.assertIsInstance(got[0][1], ProviderEvent)
        await self.p.stop(h)

    def _fire_event(self, handle):
        """Subclasses trigger a provider event for `handle` here."""
        raise NotImplementedError
```

- [ ] **Step 5: Write `backend/tests/test_fake_provider.py`**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from provider_contract import AgentProviderContract  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class FakeProviderContract(AgentProviderContract):
    def make_provider(self):
        return FakeAgentProvider()

    def _fire_event(self, handle):
        self.p.emit(handle, ProviderEvent("turn_completed", {"assistant_text": "done"}))

    async def test_scripted_poll_is_consumed_in_order(self):
        h = await self.p.create_session(self.ctx, self.opts())
        self.p.script(h, {"status": "needs_permission"})
        self.p.script(h, {"status": "completed", "assistant_text": "ok"})
        self.assertEqual(self.p.poll(h)["status"], "needs_permission")
        self.assertEqual(self.p.poll(h)["status"], "completed")
        self.assertEqual(self.p.poll(h)["status"], "idle")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: Run** `cd backend && .venv/bin/python -m unittest tests.test_fake_provider -v` → PASS (6 tests).

- [ ] **Step 7: Full suite** → `OK`.

---

### Task 7: ClaudeCodeProvider adapter + runner observer + registry

**Files:**
- Modify: `backend/claude_runner.py` (`ClaudeRunner` class attr; `_consume`, `_can_use_tool`)
- Modify: `backend/tmux_runner.py` (`_notify`; `_handle_event`; `_update_cost`)
- Create: `backend/yuri/providers/claude_code.py`, `backend/yuri/providers/registry.py`
- Test: `backend/tests/test_claude_provider.py`, `backend/tests/test_registry.py`

**Interfaces — Produces:**
```python
ClaudeCodeProvider(runner_factory: Callable[[str], ClaudeRunner] | None = None)
  .runner_for(handle) -> ClaudeRunner          # used by session_manager shims
  .backend_of(handle) -> "cli"|"sdk"|None
  .register(handle, backend)                   # used after rehydrate / resume
AgentRegistry(): register(p), get(id), all(), ids(), async health_all() -> dict[str, AgentHealth]
build_registry(agents_csv: str, claude_factory=None) -> AgentRegistry
ClaudeRunner.on_event: Callable[[str, str, dict], None] | None   # (handle, native_kind, raw)
```

- [ ] **Step 1: Failing tests — `backend/tests/test_claude_provider.py`**

```python
"""ClaudeCodeProvider forwards to the existing runners and maps their hook
events into ProviderEvents. Runners are stubs — no tmux/SDK."""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from provider_contract import AgentProviderContract  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.claude_code import ClaudeCodeProvider  # noqa: E402


class _StubRunner:
    on_event = None

    def __init__(self, backend):
        self.backend = backend
        self.sessions = {}
        self.calls = []
        self._n = 0

    async def start(self, cwd, model=None, mode="default"):
        self._n += 1
        h = f"{self.backend}-{self._n}-00000000"
        self.sessions[h] = {"handle": h, "session_id": h, "cwd": cwd, "model": model or "opus",
                            "mode": mode, "status": "idle", "cost_usd": 0.0}
        return h

    async def resume(self, session_id, cwd, model=None, mode="default", name=None):
        self.sessions[session_id] = {"handle": session_id, "session_id": session_id, "cwd": cwd,
                                     "model": "opus", "mode": mode, "status": "idle",
                                     "cost_usd": 0.0}
        return session_id

    def list(self):
        return list(self.sessions.values())

    def start_advance(self, h, m):
        self.calls.append(("advance", h, m))

    def start_answer(self, h, c):
        self.calls.append(("answer", h, c))

    def start_builtin_slash(self, h, t):
        self.calls.append(("slash", h, t))

    def poll_status(self, h):
        if h not in self.sessions:
            raise KeyError(h)
        return {"status": "idle", "session_id": h}

    async def interrupt(self, h):
        self.calls.append(("interrupt", h))

    async def close(self, h):
        self.sessions.pop(h)

    async def set_mode(self, h, mode):
        self.sessions[h]["mode"] = mode
        return mode

    async def read(self, h):
        return "text"

    async def peek(self, h, lines=40):
        return "screen"

    async def send_keys(self, h, items):
        return {"session_id": h, "screen": "x", "sent": items}

    def pane_for(self, h):
        return f"vc_{h[:8]}"

    def persist_name(self, h, name):
        self.calls.append(("persist_name", h, name))

    async def rehydrate(self):
        return []

    async def shutdown(self):
        self.sessions.clear()


def _factory_holder():
    made = {}

    def factory(backend):
        made.setdefault(backend, _StubRunner(backend))
        return made[backend]
    return factory, made


class ClaudeProviderContract(AgentProviderContract):
    def make_provider(self):
        self.factory, self.made = _factory_holder()
        return ClaudeCodeProvider(runner_factory=self.factory)

    def _fire_event(self, handle):
        self.made["cli"].on_event(handle, "turn_complete", {"assistant_text": "done"})


class ClaudeProviderRouting(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.factory, self.made = _factory_holder()
        self.p = ClaudeCodeProvider(runner_factory=self.factory)
        self.ctx = ProjectContext("p1", "/tmp")

    async def test_backend_option_selects_runner(self):
        cli = await self.p.create_session(self.ctx, SessionOptions(backend="cli"))
        sdk = await self.p.create_session(self.ctx, SessionOptions(backend="sdk"))
        self.assertEqual(self.p.backend_of(cli), "cli")
        self.assertEqual(self.p.backend_of(sdk), "sdk")
        self.p.send_message(sdk, "hi")
        self.assertIn(("advance", sdk, "hi"), self.made["sdk"].calls)
        tagged = {s["handle"]: s["backend"] for s in self.p.list_native()}
        self.assertEqual(tagged, {cli: "cli", sdk: "sdk"})

    async def test_sdk_handle_has_no_terminal_features(self):
        sdk = await self.p.create_session(self.ctx, SessionOptions(backend="sdk"))
        self.assertIsNone(self.p.native_pane(sdk))
        with self.assertRaises(NotImplementedError):
            self.p.run_slash(sdk, "/init")
        with self.assertRaises(NotImplementedError):
            await self.p.send_keys(sdk, [{"key": "Escape"}])

    async def test_cli_handle_terminal_features(self):
        cli = await self.p.create_session(self.ctx, SessionOptions(backend="cli"))
        self.assertEqual(self.p.native_pane(cli), f"vc_{cli[:8]}")
        self.p.run_slash(cli, "/init")
        self.assertIn(("slash", cli, "/init"), self.made["cli"].calls)
        self.assertEqual(await self.p.peek(cli), "screen")

    async def test_resume_registers_cli_owner(self):
        h = await self.p.resume("abcdefab-1111-2222-3333-444444444444", self.ctx,
                                SessionOptions(name="n"))
        self.assertEqual(self.p.backend_of(h), "cli")

    async def test_event_mapping(self):
        got = []
        self.p.set_observer(lambda h, ev: got.append((h, ev.kind, ev.payload)))
        cli = await self.p.create_session(self.ctx, SessionOptions())
        r = self.made["cli"]
        r.on_event(cli, "tool", {"tool_name": "Read", "tool_input": {"file_path": "x"}})
        r.on_event(cli, "needs_permission", {"request_id": "r1", "tool_name": "Bash",
                                             "tool_input": {"command": "rm x"},
                                             "text": "run rm x"})
        r.on_event(cli, "needs_choice", {"request_id": "r2", "text": "pick", "options": ["a"],
                                         "multi_select": False})
        r.on_event(cli, "turn_complete", {"assistant_text": "x" * 3000, "tools_used": ["Read"]})
        r.on_event(cli, "cost", {"cost_usd": 0.5, "model": "opus"})
        r.on_event(cli, "error", {"message": "boom"})
        r.on_event(cli, "unknown_kind", {})
        kinds = [k for _, k, _ in got]
        self.assertEqual(kinds, ["tool_started", "needs_permission", "needs_choice",
                                 "turn_completed", "cost_updated", "error"])
        self.assertEqual(got[1][2]["request_id"], "r1")
        self.assertEqual(got[1][2]["options"], ["allow", "deny"])
        self.assertEqual(len(got[3][2]["assistant_text"]), 2000)
        self.assertIsNone(got[4][2]["input_tokens"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Failing test — `backend/tests/test_registry.py`**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry, build_registry  # noqa: E402


class Registry(unittest.IsolatedAsyncioTestCase):
    async def test_register_get_all_health(self):
        reg = AgentRegistry()
        ok = FakeAgentProvider()
        down = FakeAgentProvider(online=False)
        down.id = "fake-down"
        reg.register(ok)
        reg.register(down)
        self.assertIs(reg.get("fake"), ok)
        self.assertEqual(reg.ids(), ["fake", "fake-down"])
        with self.assertRaises(KeyError):
            reg.get("nope")
        health = await reg.health_all()
        self.assertTrue(health["fake"].online)
        self.assertFalse(health["fake-down"].online)

    async def test_build_registry_skips_unknown_ids(self):
        reg = build_registry("claude-code, bogus", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["claude-code"])

    async def test_build_registry_default(self):
        reg = build_registry("", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["claude-code"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run both → FAIL** with `ModuleNotFoundError: yuri.providers.claude_code`.

- [ ] **Step 4: Add the observer attribute to `backend/claude_runner.py`**

In `class ClaudeRunner(ABC):` add as the first line of the body (before the abstract methods):

```python
    # Optional observer for runtime signals (Yuri's ClaudeCodeProvider installs
    # one): called as on_event(handle, native_kind, raw_dict) from the runner's
    # own sync/async paths. Never awaited; must not raise. None = no observer.
    on_event: "Callable[[str, str, dict[str, Any]], None] | None" = None

    def _notify(self, handle: str, kind: str, raw: dict[str, Any]) -> None:
        cb = self.on_event
        if cb is None:
            return
        try:
            cb(handle, kind, raw)
        except Exception:  # an observer bug must never break a turn
            logging.getLogger("yapcode.runner").exception("on_event observer failed")
```

Ensure `import logging` and `from typing import Any, Callable, Literal, Optional` are present at the top of `claude_runner.py` (add `Callable` and `logging` if missing).

In `SDKClaudeRunner._consume`:
- inside the `ToolUseBlock` branch, after `s.tools_used.append(b.name)`: `self._notify(s.handle, "tool", {"tool_name": b.name, "tool_input": b.input})`
- in the `ResultMessage` branch, after the `if msg.total_cost_usd:` block: `self._notify(s.handle, "cost", {"cost_usd": s.cost_usd, "model": s.model})`
- in the `is_error` branch after setting `s.error`: `self._notify(s.handle, "error", {"message": s.error})`
- in the `else` (completed) branch after computing `txt`: `self._notify(s.handle, "turn_complete", {"assistant_text": txt, "tools_used": list(s.tools_used)})`
- in the outer `except Exception as e:` after `s.error = str(e)`: `self._notify(s.handle, "error", {"message": s.error})`

In `SDKClaudeRunner._can_use_tool`, right after `s._stop.set()` inside the `while True` loop:
```python
                p = s.pending
                self._notify(s.handle,
                             "needs_choice" if kind == "question" else "needs_permission",
                             {"request_id": p.request_id, "tool_name": tool_name,
                              "tool_input": tool_input, "text": p.text,
                              "options": list(p.options), "multi_select": p.multi_select})
```

- [ ] **Step 5: Add notifications to `backend/tmux_runner.py`**

In `_handle_event`:
- `kind == "tool"` branch, after the `log_event(...)`: `self._notify(s.handle, "tool", {"tool_name": name, "tool_input": ev.get("tool_input", {})})`
- `needs_permission` branch, after `s._stop.set()`: 
  ```python
            self._notify(s.handle, "needs_permission",
                         {"request_id": s.pending.request_id, "tool_name": s.pending.tool_name,
                          "tool_input": ev.get("tool_input", {}), "text": s.pending.text,
                          "options": list(s.pending.options)})
  ```
- `needs_choice` branch, after `s._stop.set()`:
  ```python
            self._notify(s.handle, "needs_choice",
                         {"request_id": s.pending.request_id, "tool_name": s.pending.tool_name,
                          "text": s.pending.text, "options": list(s.pending.options),
                          "multi_select": s.pending.multi_select})
  ```
- `turn_complete` branch, after `s._stop.set()`:
  ```python
            self._notify(s.handle, "turn_complete",
                         {"assistant_text": "".join(s._delta), "tools_used": list(s.tools_used)})
  ```

In `_update_cost`, after `s._cost_scan_size = size`:
```python
        self._notify(s.handle, "cost", {"cost_usd": s.cost_usd, "model": s.model})
```

- [ ] **Step 6: Write `backend/yuri/providers/claude_code.py`**

```python
"""Claude Code as an AgentProvider. Wraps the two existing runners — the
interactive CLI in tmux ("cli") and the Agent SDK ("sdk") — behind one
provider id. This adapter is the ONLY place that knows both runners exist;
above it, Yuri sees "claude-code" sessions with a per-handle backend tag.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Callable

from claude_runner import ClaudeRunner
from .base import (AgentCapabilities, AgentHealth, AgentProvider, Observer, ProjectContext,
                   ProviderEvent, SessionOptions)

log = logging.getLogger("yuri.providers.claude")

HEALTH_TTL_S = 30.0
BACKENDS = ("cli", "sdk")


def default_runner_factory(backend: str) -> ClaudeRunner:
    # Imported lazily: tmux_runner/claude_runner pull in the SDK and tmux
    # constants, which tests avoid by injecting a factory.
    if backend == "sdk":
        from claude_runner import SDKClaudeRunner
        return SDKClaudeRunner()
    from tmux_runner import TmuxClaudeRunner
    return TmuxClaudeRunner()


async def _version(cmd: list[str], timeout: float = 5.0) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode != 0:
            return None
        return out.decode(errors="replace").strip().splitlines()[0][:80] if out else ""
    except Exception:
        return None


class ClaudeCodeProvider(AgentProvider):
    id = "claude-code"
    name = "Claude Code"

    def __init__(self, runner_factory: Callable[[str], ClaudeRunner] | None = None):
        self._factory = runner_factory or default_runner_factory
        self._runners: dict[str, ClaudeRunner] = {}
        self._owner: dict[str, str] = {}      # native handle -> backend
        self._observer: Observer | None = None
        self._health: tuple[float, AgentHealth] | None = None

    # --- runner plumbing (also used by session_manager shims) --------------

    def runner(self, backend: str = "cli") -> ClaudeRunner:
        backend = (backend or "cli").lower()
        if backend not in BACKENDS:
            backend = "cli"
        r = self._runners.get(backend)
        if r is None:
            r = self._factory(backend)
            r.on_event = functools.partial(self._on_runner_event, backend)
            self._runners[backend] = r
        return r

    def register(self, handle: str, backend: str) -> None:
        self._owner[handle] = (backend or "cli").lower()

    def backend_of(self, handle: str) -> str | None:
        b = self._owner.get(handle)
        if b is not None:
            return b
        for backend, r in self._runners.items():
            if any(s["handle"] == handle for s in r.list()):
                self._owner[handle] = backend
                return backend
        return None

    def runner_for(self, handle: str) -> ClaudeRunner:
        b = self.backend_of(handle)
        if b is None:
            raise KeyError(f"unknown session: {handle}")
        return self.runner(b)

    def _cli_only(self, handle: str, what: str) -> ClaudeRunner:
        if self.backend_of(handle) != "cli":
            raise NotImplementedError(f"{what} controls the interactive CLI; this session uses the SDK backend.")
        return self.runner("cli")

    # --- contract -----------------------------------------------------------

    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(interactive_terminal=True, slash_commands=True, send_keys=True,
                                 permission_modes=("default", "acceptEdits", "plan", "auto"),
                                 supports_interrupt=True, supports_rehydrate=True,
                                 supports_resume=True, supports_events=True, cost_tracking=True)

    async def health(self) -> AgentHealth:
        now = time.monotonic()
        if self._health and now - self._health[0] < HEALTH_TTL_S:
            return self._health[1]
        claude_v, tmux_v = await asyncio.gather(_version(["claude", "--version"]),
                                                _version(["tmux", "-V"]))
        online = claude_v is not None
        parts = [f"claude: {claude_v or 'missing'}", f"tmux: {tmux_v or 'missing (cli backend unavailable)'}"]
        h = AgentHealth(online=online, version=claude_v or None, detail=" · ".join(parts))
        self._health = (now, h)
        return h

    async def create_session(self, project: ProjectContext, opts: SessionOptions) -> str:
        backend = opts.backend if opts.backend in BACKENDS else "cli"
        handle = await self.runner(backend).start(project.root_path, opts.model, opts.mode)
        self.register(handle, backend)
        return handle

    async def resume(self, native_session_id: str, project: ProjectContext,
                     opts: SessionOptions) -> str:
        handle = await self.runner("cli").resume(native_session_id, project.root_path,
                                                 opts.model, opts.mode, opts.name)
        self.register(handle, "cli")
        return handle

    def send_message(self, handle: str, message: str) -> None:
        self.runner_for(handle).start_advance(handle, message)

    def answer(self, handle: str, choice: str) -> None:
        self.runner_for(handle).start_answer(handle, choice)

    def poll(self, handle: str) -> dict[str, Any]:
        return self.runner_for(handle).poll_status(handle)

    async def interrupt(self, handle: str) -> None:
        await self.runner_for(handle).interrupt(handle)

    async def stop(self, handle: str) -> None:
        await self.runner_for(handle).close(handle)
        self._owner.pop(handle, None)

    async def set_mode(self, handle: str, mode: str) -> str:
        return await self.runner_for(handle).set_mode(handle, mode)

    async def read(self, handle: str) -> str:
        return await self.runner_for(handle).read(handle)

    async def peek(self, handle: str, lines: int = 40) -> str | None:
        r = self.runner_for(handle)
        peek = getattr(r, "peek", None)
        return await peek(handle, lines) if peek else None

    async def send_keys(self, handle: str, items: list[dict]) -> dict[str, Any]:
        return await self._cli_only(handle, "send_keys").send_keys(handle, items)

    def run_slash(self, handle: str, text: str) -> None:
        r = self._cli_only(handle, "slash commands")
        start = getattr(r, "start_builtin_slash", None)
        if start:
            start(handle, text)
        else:
            r.start_advance(handle, text)

    def list_native(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for backend, r in self._runners.items():
            for s in r.list():
                out.append({**s, "backend": backend})
        return out

    def native_pane(self, handle: str) -> str | None:
        if self.backend_of(handle) != "cli":
            return None
        pane_for = getattr(self.runner("cli"), "pane_for", None)
        return pane_for(handle) if pane_for else None

    def persist_name(self, handle: str, name: str) -> None:
        persist = getattr(self.runner_for(handle), "persist_name", None)
        if persist:
            try:
                persist(handle, name)
            except Exception:
                log.debug("persist_name failed for %s", handle, exc_info=True)

    def set_observer(self, cb: Observer | None) -> None:
        self._observer = cb

    async def rehydrate(self) -> list[dict[str, Any]]:
        r = self.runner("cli")
        rehydrate = getattr(r, "rehydrate", None)
        if rehydrate is None:
            return []
        restored = await rehydrate()
        for s in restored:
            self.register(s["handle"], "cli")
        return restored

    async def shutdown(self) -> None:
        for r in self._runners.values():
            await r.shutdown()
        self._runners.clear()
        self._owner.clear()

    # --- runner events -> ProviderEvent ---------------------------------------

    def _on_runner_event(self, backend: str, handle: str, kind: str, raw: dict[str, Any]) -> None:
        cb = self._observer
        if cb is None:
            return
        ev = self._map(kind, raw)
        if ev is None:
            return
        try:
            cb(handle, ev)
        except Exception:
            log.exception("provider observer failed")

    @staticmethod
    def _map(kind: str, raw: dict[str, Any]) -> ProviderEvent | None:
        if kind == "tool":
            return ProviderEvent("tool_started", {"tool_name": raw.get("tool_name", ""),
                                                  "tool_input": raw.get("tool_input") or {}})
        if kind == "needs_permission":
            return ProviderEvent("needs_permission", {
                "request_id": raw.get("request_id"), "tool_name": raw.get("tool_name", ""),
                "tool_input": raw.get("tool_input") or {}, "text": raw.get("text", ""),
                "options": ["allow", "deny"]})
        if kind == "needs_choice":
            return ProviderEvent("needs_choice", {
                "request_id": raw.get("request_id"), "tool_name": raw.get("tool_name", ""),
                "text": raw.get("text", ""), "options": list(raw.get("options") or []),
                "multi_select": bool(raw.get("multi_select"))})
        if kind == "turn_complete":
            return ProviderEvent("turn_completed", {
                "assistant_text": (raw.get("assistant_text") or "")[:2000],
                "tools_used": list(raw.get("tools_used") or [])})
        if kind == "cost":
            return ProviderEvent("cost_updated", {
                "model": raw.get("model"), "input_tokens": raw.get("input_tokens"),
                "output_tokens": raw.get("output_tokens"), "cost_usd": raw.get("cost_usd")})
        if kind == "error":
            return ProviderEvent("error", {"message": str(raw.get("message") or "unknown error")})
        return None
```

- [ ] **Step 7: Write `backend/yuri/providers/registry.py`**

```python
"""Which agent providers exist and whether they're actually reachable (spec §7).
Configured by YURI_AGENTS (comma list; default "claude-code"). A provider being
configured does not make it "online" — health() decides that."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .base import AgentHealth, AgentProvider

log = logging.getLogger("yuri.registry")

KNOWN = ("claude-code",)


class AgentRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, AgentProvider] = {}

    def register(self, provider: AgentProvider) -> None:
        self._providers[provider.id] = provider

    def get(self, agent_id: str) -> AgentProvider:
        p = self._providers.get(agent_id)
        if p is None:
            raise KeyError(f"unknown agent: {agent_id!r}. Known: {self.ids()}")
        return p

    def all(self) -> list[AgentProvider]:
        return list(self._providers.values())

    def ids(self) -> list[str]:
        return list(self._providers)

    async def health_all(self) -> dict[str, AgentHealth]:
        ps = self.all()
        results = await asyncio.gather(*(p.health() for p in ps), return_exceptions=True)
        out: dict[str, AgentHealth] = {}
        for p, r in zip(ps, results):
            out[p.id] = r if isinstance(r, AgentHealth) else AgentHealth(
                online=False, version=None, detail=f"health check failed: {r}")
        return out

    async def shutdown(self) -> None:
        for p in self.all():
            try:
                await p.shutdown()
            except Exception:
                log.exception("provider %s shutdown failed", p.id)


def build_registry(agents_csv: str, claude_factory: Callable | None = None) -> AgentRegistry:
    from .claude_code import ClaudeCodeProvider
    reg = AgentRegistry()
    wanted = [a.strip() for a in (agents_csv or "").split(",") if a.strip()] or ["claude-code"]
    for a in wanted:
        if a == "claude-code":
            reg.register(ClaudeCodeProvider(runner_factory=claude_factory))
        else:
            log.warning("YURI_AGENTS: unknown agent %r skipped (known: %s)", a, KNOWN)
    return reg
```

- [ ] **Step 8: Run** `cd backend && .venv/bin/python -m unittest tests.test_claude_provider tests.test_registry -v` → PASS.

- [ ] **Step 9: Full suite** → `OK` (the Phase 1 tests prove the runner edits changed no behavior).

---

### Task 8: Route `tools.py` / `session_manager.py` through the provider

**Files:**
- Modify: `backend/session_manager.py` (runner registry section)
- Modify: `backend/config.py` (add `YURI_AGENTS`)
- Test: existing `test_tools_dispatch.py`, `test_session_manager.py` (keep passing; their stubs are injected via the new `set_provider_for_tests` hook described below)

**Interfaces — Produces:** `session_manager.provider() -> ClaudeCodeProvider`; `session_manager.set_provider(p)`; the old names `get_runner/runner_for/register_owner/backend_of/cli_pane_for/_raw_sessions/rehydrate_cli_sessions/shutdown_all` keep their signatures.

- [ ] **Step 1: Add to `backend/config.py`** (after `KILL_SESSIONS_ON_SHUTDOWN`):

```python
# Which agent providers Yuri registers (comma-separated ids). Default: Claude
# Code only. A configured agent is still shown "offline" until its health check
# passes (spec §7).
YURI_AGENTS: str = (os.getenv("YURI_AGENTS") or "claude-code").strip()
```

- [ ] **Step 2: Rewrite the runner-registry section of `backend/session_manager.py`**

Replace everything from `from claude_runner import ClaudeRunner, SDKClaudeRunner` down to and including `def runner_for(handle)` with:

```python
from claude_runner import ClaudeRunner
from yuri.providers.claude_code import ClaudeCodeProvider

# One ClaudeCodeProvider owns both runners (spec §3.2). The module-level
# functions below are compatibility shims so tools.py / main.py keep working
# unchanged while Phase 3 moves callers onto SessionService.
_provider: ClaudeCodeProvider | None = None
_names: dict[str, str] = {}  # handle -> human-readable display name


def provider() -> ClaudeCodeProvider:
    global _provider
    if _provider is None:
        _provider = ClaudeCodeProvider()
    return _provider


def set_provider(p: ClaudeCodeProvider | None) -> None:
    """Install the provider (app startup) or a test double. None resets."""
    global _provider
    _provider = p


def get_runner(backend: str = "cli") -> ClaudeRunner:
    return provider().runner(backend)


def register_owner(handle: str, backend: str) -> None:
    provider().register(handle, backend)


def backend_of(handle: str) -> str | None:
    return provider().backend_of(handle)


def runner_for(handle: str) -> ClaudeRunner:
    return provider().runner_for(handle)
```

Then:
- `_raw_sessions()` body → `return provider().list_native()`
- `cli_pane_for(handle)` body → `return provider().native_pane(handle)`
- in `set_session_name`, replace the `persist = getattr(runner_for(handle), "persist_name", None) ...` block with `provider().persist_name(handle, name)`
- `close_session`: replace `r = runner_for(handle); await r.close(handle); _owner.pop(handle, None)` with `await provider().stop(handle)`
- `rehydrate_cli_sessions`: replace the `runner = get_runner("cli") ... restored = await rehydrate()` lines with `restored = await provider().rehydrate()` and drop the `register_owner(handle, "cli")` line (the provider registers)
- `shutdown_all`: body → `await provider().shutdown(); _names.clear()`
- delete the `_runners` and `_owner` module dicts.

- [ ] **Step 3: Update the two Phase 1 tests' fixtures** to inject the stub through the provider instead of `sm._runners`:

In `test_session_manager.py` `ResolveSession.setUp`, replace the `mock.patch.dict(sm._runners …)` + `_owner` lines with:
```python
        from yuri.providers.claude_code import ClaudeCodeProvider
        self.prov = ClaudeCodeProvider(runner_factory=lambda b: self.runner)
        self.prov.runner("cli")   # instantiate: list_native() only sees created runners
        self.prov.register("aaaaaaaa-1111", "cli")
        self.prov.register("bbbbbbbb-2222", "cli")
        sm.set_provider(self.prov)
        sm._names.clear()
```
and `tearDown` → `sm.set_provider(None); sm._names.clear()`.

In `test_tools_dispatch.py` `setUp`, replace the `mock.patch.dict(sm._runners …)` patch with `mock.patch.dict(os.environ, …)` only, and add before the loop:
```python
        from yuri.providers.claude_code import ClaudeCodeProvider
        sm.set_provider(ClaudeCodeProvider(runner_factory=lambda b: self.runner))
```
`tearDown` adds `sm.set_provider(None)`. Remove the `sm._owner.clear()` lines in both files.

- [ ] **Step 4: Run full suite** → `OK`. Every tool result key from Task 3 is unchanged; that is the Phase 2 acceptance.

- [ ] **Step 5: Manual Checkpoint A (live)** — `cd backend && ./run.sh` in one terminal, `cd frontend && npm run dev` in another. Open http://localhost:3000, connect with Gemini, say "start a session in yuri-code", then "tell it to say hello", wait for the spoken summary, then "close it". Confirm the Activity panel shows tool_call/tool_result events and the terminal panel attaches. **Stop and report to the user before Phase 3.**

---

## Phase 3 — Domain, store, events, services, API, persona (Tasks 9–20)

### Task 9: Yuri home + config

**Files:**
- Create: `backend/yuri/home.py`
- Modify: `backend/config.py` (`YURI_HOME`; `allowed_project_roots()` appends it), `backend/session_manager.py` (`_allowed_roots` delegates to config)
- Test: `backend/tests/test_home.py`

**Interfaces — Produces:**
```python
config.YURI_HOME: str                       # abs path, default ~/Yuri
config.allowed_project_roots() -> list[str] # now includes YURI_HOME when it is a directory
yuri.home.Home(path): .path, .db_path, .memory_dir, .projects_memory_dir, .journal_dir, .workspace_dir, .user_memory_path
  .ensure() -> Home   (idempotent; 0700; writes memory/user.md header if absent)
yuri.home.default_home() -> Home            # from config.YURI_HOME
```

- [ ] **Step 1: Failing test `backend/tests/test_home.py`**

```python
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.home import Home  # noqa: E402


class HomeLayout(unittest.TestCase):
    def test_ensure_creates_layout_idempotently(self):
        with tempfile.TemporaryDirectory() as d:
            h = Home(os.path.join(d, "Yuri")).ensure()
            for p in [h.memory_dir, h.projects_memory_dir, h.journal_dir, h.workspace_dir]:
                self.assertTrue(os.path.isdir(p), p)
            self.assertTrue(os.path.isfile(h.user_memory_path))
            with open(h.user_memory_path) as f:
                first = f.read()
            self.assertIn("# What Yuri knows about you", first)
            mode = stat.S_IMODE(os.stat(h.path).st_mode)
            self.assertEqual(mode, 0o700)
            # second call must not clobber the memory file
            with open(h.user_memory_path, "a") as f:
                f.write("- keep me\n")
            Home(h.path).ensure()
            with open(h.user_memory_path) as f:
                self.assertIn("keep me", f.read())
            self.assertEqual(h.db_path, os.path.join(h.path, "yuri.db"))

    def test_home_joins_allowed_roots_only_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "Yuri")
            with mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
                 mock.patch.object(config, "YURI_HOME", home):
                self.assertNotIn(os.path.realpath(home), config.allowed_project_roots())
                Home(home).ensure()
                self.assertIn(os.path.realpath(home), config.allowed_project_roots())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError: yuri.home`).

- [ ] **Step 3: `backend/config.py`** — after `YURI_AGENTS` add:

```python
# Yuri's home: her state store (yuri.db), memory/, journal/ and workspace/.
# She may read/write freely here; it is appended to the project sandbox roots
# at runtime (see allowed_project_roots) once it exists.
YURI_HOME: str = os.path.abspath(os.path.expanduser(os.getenv("YURI_HOME") or "~/Yuri"))
```

and change `allowed_project_roots()` to:

```python
def allowed_project_roots() -> list[str]:
    """Realpath'd ALLOWED_PROJECT_ROOTS entries — the directory sandbox a session's
    cwd must live under — plus Yuri's home once it exists. Realpath (not just
    abspath) so symlinked roots compare correctly against a realpath'd candidate."""
    raw = os.getenv("ALLOWED_PROJECT_ROOTS", "")
    roots = [os.path.realpath(os.path.expanduser(p)) for p in raw.split(",") if p.strip()]
    home = os.path.realpath(YURI_HOME)
    if os.path.isdir(home) and home not in roots:
        roots.append(home)
    return roots
```

- [ ] **Step 4: `backend/session_manager.py`** — `_allowed_roots()` body → `return config.allowed_project_roots()` (add `import config` at the top). This removes the duplicated env parsing so both sandboxes see the same roots.

- [ ] **Step 5: Write `backend/yuri/home.py`**

```python
"""Yuri's home directory (~/Yuri by default; YURI_HOME to override).

    yuri.db          SQLite state store
    memory/user.md   what she knows about you (plain markdown, edit freely)
    memory/projects/ per-project notes
    journal/         one append-only file per day
    workspace/       her scratch space
"""
from __future__ import annotations

import os

USER_MEMORY_HEADER = (
    "# What Yuri knows about you\n\n"
    "Plain markdown. Yuri appends dated lines here when you tell her to remember\n"
    "something; edit or delete anything you like.\n\n"
)


class Home:
    def __init__(self, path: str):
        self.path = os.path.abspath(os.path.expanduser(path))

    @property
    def db_path(self) -> str:
        return os.path.join(self.path, "yuri.db")

    @property
    def memory_dir(self) -> str:
        return os.path.join(self.path, "memory")

    @property
    def projects_memory_dir(self) -> str:
        return os.path.join(self.memory_dir, "projects")

    @property
    def user_memory_path(self) -> str:
        return os.path.join(self.memory_dir, "user.md")

    @property
    def journal_dir(self) -> str:
        return os.path.join(self.path, "journal")

    @property
    def workspace_dir(self) -> str:
        return os.path.join(self.path, "workspace")

    def ensure(self) -> "Home":
        os.makedirs(self.path, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.path, 0o700)
        except OSError:
            pass
        for d in (self.memory_dir, self.projects_memory_dir, self.journal_dir, self.workspace_dir):
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(self.user_memory_path):
            with open(self.user_memory_path, "w", encoding="utf-8") as f:
                f.write(USER_MEMORY_HEADER)
        return self


def default_home() -> Home:
    import config
    return Home(config.YURI_HOME)
```

- [ ] **Step 6: Run** `tests.test_home` → PASS; full suite → `OK`.

---

### Task 10: Domain entities + risk classifier

**Files:**
- Create: `backend/yuri/domain/__init__.py`, `ids.py`, `project.py`, `mission.py`, `session.py`, `approval.py`, `event.py`, `risk.py`
- Test: `backend/tests/test_domain.py`, `backend/tests/test_risk.py`

**Interfaces — Produces (exact names used by store/services):**
```python
yuri.domain.ids: new_id() -> str; utcnow() -> str
yuri.domain.project: Project(id, slug, name, root_path, kind="user", default_agent=None, auto_approve_edits=False, repo_url=None, created_at, updated_at); .to_dict(); Project.from_dict(d); slugify(name) -> str
yuri.domain.mission: MissionStatus (str enum: DRAFT..CANCELLED), TERMINAL: frozenset, TRANSITIONS: dict, InvalidTransition(ValueError)
  Mission(id, title, goal=None, project_id, status="running", priority=0, current_step=None, created_by="voice", metadata={}, created_at, updated_at); .transition(to: str) -> bool (False when same-state); .to_dict(); from_dict
  MissionStep(id, mission_id, ordinal, title, agent_id=None, status="pending", session_id=None, result={}); to_dict/from_dict
yuri.domain.session: AgentSession(id, mission_id, project_id, agent_id, native_session_id, backend, status="starting", name=None, mode="default", model=None, working_directory, started_at, last_activity_at, runtime_metadata={}); LIVE_STATUSES: frozenset; to_dict/from_dict
yuri.domain.approval: Approval(id, mission_id, session_id, agent_id, action, tool_name, tool_input={}, risk="confirm", description="", status="pending", request_id, requested_at, resolved_at=None, resolved_by=None); to_dict/from_dict
yuri.domain.event: EventType (str constants), DEFAULTS: dict[type -> (severity, speakable)], YuriEvent(id, ts, type, mission_id=None, session_id=None, agent_id=None, project_id=None, severity="info", speakable=False, payload={}); YuriEvent.make(type, **fields) applies DEFAULTS; to_dict/from_dict
yuri.domain.risk: risk_for(tool_name, tool_input) -> "safe"|"confirm"|"dangerous"; DANGEROUS_PATTERNS
```

- [ ] **Step 1: Failing tests**

`backend/tests/test_domain.py`:
```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.event import DEFAULTS, EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import InvalidTransition, Mission, MissionStatus  # noqa: E402
from yuri.domain.project import Project, slugify  # noqa: E402
from yuri.domain.session import AgentSession, LIVE_STATUSES  # noqa: E402


class MissionTransitions(unittest.TestCase):
    def _m(self, status="running"):
        return Mission(title="t", project_id="p", status=status, created_by="voice")

    def test_valid_paths(self):
        m = self._m("draft")
        self.assertTrue(m.transition("running"))
        self.assertTrue(m.transition("waiting_for_approval"))
        self.assertTrue(m.transition("running"))
        self.assertTrue(m.transition("paused"))
        self.assertTrue(m.transition("running"))
        self.assertTrue(m.transition("completed"))
        self.assertEqual(m.status, "completed")

    def test_same_state_is_noop(self):
        m = self._m("running")
        before = m.updated_at
        self.assertFalse(m.transition("running"))
        self.assertEqual(m.updated_at, before)

    def test_terminal_cannot_move(self):
        for t in ["completed", "failed", "cancelled"]:
            m = self._m(t)
            with self.assertRaises(InvalidTransition):
                m.transition("running")

    def test_invalid_edge(self):
        with self.assertRaises(InvalidTransition):
            self._m("paused").transition("completed")
        with self.assertRaises(InvalidTransition):
            self._m("running").transition("nonsense")

    def test_round_trip(self):
        m = self._m()
        m.metadata = {"k": 1}
        self.assertEqual(Mission.from_dict(m.to_dict()), m)
        self.assertEqual(MissionStatus.RUNNING, "running")


class ProjectSlug(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(slugify("PM Tool"), "pm-tool")
        self.assertEqual(slugify("  yuri_code!! "), "yuri-code")
        self.assertEqual(slugify(""), "project")

    def test_round_trip(self):
        p = Project(slug="x", name="X", root_path="/tmp/x", kind="home", auto_approve_edits=True)
        self.assertEqual(Project.from_dict(p.to_dict()), p)


class SessionDefaults(unittest.TestCase):
    def test_live_statuses(self):
        self.assertEqual(LIVE_STATUSES,
                         frozenset({"starting", "running", "needs_permission", "needs_choice", "idle"}))
        s = AgentSession(project_id="p", agent_id="claude-code", native_session_id="h",
                         backend="cli", working_directory="/tmp")
        self.assertEqual(s.status, "starting")
        self.assertEqual(AgentSession.from_dict(s.to_dict()), s)


class Events(unittest.TestCase):
    def test_make_applies_defaults(self):
        e = YuriEvent.make(EventType.APPROVAL_REQUESTED, session_id="s", payload={"x": 1})
        self.assertEqual((e.severity, e.speakable), ("notice", True))
        self.assertTrue(e.ts.endswith("Z"))
        self.assertEqual(YuriEvent.from_dict(e.to_dict()), e)

    def test_every_type_has_defaults(self):
        types = [v for k, v in vars(EventType).items() if k.isupper()]
        self.assertTrue(types)
        for t in types:
            self.assertIn(t, DEFAULTS, t)

    def test_explicit_override(self):
        e = YuriEvent.make(EventType.TOOL_STARTED, severity="warning", speakable=True)
        self.assertEqual((e.severity, e.speakable), ("warning", True))


if __name__ == "__main__":
    unittest.main()
```

`backend/tests/test_risk.py`:
```python
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.risk import risk_for  # noqa: E402


class Risk(unittest.TestCase):
    def test_safe(self):
        self.assertEqual(risk_for("Read", {"file_path": "x"}), "safe")
        self.assertEqual(risk_for("mcp__claude-in-chrome__navigate", {}), "safe")

    def test_edits_confirm(self):
        for t in ["Edit", "Write", "MultiEdit", "NotebookEdit"]:
            self.assertEqual(risk_for(t, {}), "confirm", t)

    def test_plain_bash_confirm(self):
        self.assertEqual(risk_for("Bash", {"command": "ls -la"}), "confirm")
        self.assertEqual(risk_for("Bash", {"command": "git status"}), "confirm")

    def test_destructive_bash_dangerous(self):
        for cmd in ["rm -rf build", "git push --force origin main", "git reset --hard HEAD~1",
                    "psql -c 'DROP TABLE users'", "mkfs.ext4 /dev/sda1", "echo hi > /dev/sda",
                    "chmod -R 777 /", "sudo rm -r /var"]:
            self.assertEqual(risk_for("Bash", {"command": cmd}), "dangerous", cmd)

    def test_unknown_risky_tool_confirm(self):
        self.assertEqual(risk_for("mcp__something__else", {}), "confirm")

    def test_question_is_safe(self):
        self.assertEqual(risk_for("AskUserQuestion", {}), "safe")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Write the domain package**

`backend/yuri/domain/__init__.py`: empty.

`backend/yuri/domain/ids.py`:
```python
from __future__ import annotations

import datetime
from uuid import uuid4


def new_id() -> str:
    return str(uuid4())


def utcnow() -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
```

`backend/yuri/domain/project.py`:
```python
"""A registered working directory (spec §9). Roots are validated by the service
layer against the sandbox; this is data only."""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return s[:64] or "project"


@dataclass
class Project:
    slug: str
    name: str
    root_path: str
    id: str = field(default_factory=new_id)
    kind: str = "user"                 # "user" | "home"
    default_agent: str | None = None
    auto_approve_edits: bool = False
    repo_url: str | None = None
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    @staticmethod
    def for_path(root_path: str, name: str | None = None, **kw) -> "Project":
        name = name or os.path.basename(os.path.normpath(root_path)) or "project"
        return Project(slug=slugify(name), name=name, root_path=root_path, **kw)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
```

`backend/yuri/domain/mission.py`:
```python
"""Mission — the unit of work (spec §8). A deterministic state machine; the
orchestrator (Phase 4) drives it, services enforce it."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum

from .ids import new_id, utcnow


class MissionStatus(str, Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL = frozenset({"completed", "failed", "cancelled"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"queued", "running", "cancelled"}),
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"waiting_for_approval", "paused", "completed", "failed", "cancelled"}),
    "waiting_for_approval": frozenset({"running", "paused", "failed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset(), "failed": frozenset(), "cancelled": frozenset(),
}


class InvalidTransition(ValueError):
    pass


@dataclass
class Mission:
    title: str
    project_id: str
    id: str = field(default_factory=new_id)
    goal: str | None = None
    status: str = "running"
    priority: int = 0
    current_step: str | None = None
    created_by: str = "voice"          # voice | ui | api | handoff | system
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def transition(self, to: str) -> bool:
        """Move to `to`. Returns False (no-op) for same-state; raises
        InvalidTransition for anything the table forbids."""
        if to == self.status:
            return False
        allowed = TRANSITIONS.get(self.status)
        if allowed is None or to not in allowed:
            raise InvalidTransition(f"mission {self.id[:8]}: {self.status} → {to} is not allowed")
        self.status = to
        self.updated_at = utcnow()
        return True

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Mission":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class MissionStep:
    mission_id: str
    ordinal: int
    title: str
    id: str = field(default_factory=new_id)
    agent_id: str | None = None
    status: str = "pending"            # pending | running | done | failed | skipped
    session_id: str | None = None
    result: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MissionStep":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
```

`backend/yuri/domain/session.py`:
```python
"""AgentSession — Yuri's record of one agent runtime (spec §10). `id` is
Yuri's; `native_session_id` is the provider's handle and is never the primary key."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

LIVE_STATUSES = frozenset({"starting", "running", "needs_permission", "needs_choice", "idle"})


@dataclass
class AgentSession:
    project_id: str
    agent_id: str
    native_session_id: str
    backend: str
    working_directory: str
    id: str = field(default_factory=new_id)
    mission_id: str | None = None
    status: str = "starting"           # LIVE_STATUSES | stopped | lost
    name: str | None = None
    mode: str = "default"
    model: str | None = None
    started_at: str = field(default_factory=utcnow)
    last_activity_at: str = field(default_factory=utcnow)
    runtime_metadata: dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    def touch(self) -> None:
        self.last_activity_at = utcnow()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentSession":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
```

`backend/yuri/domain/approval.py`:
```python
"""Approval — a first-class record of an agent asking permission (spec §20)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow


@dataclass
class Approval:
    session_id: str
    agent_id: str
    action: str
    tool_name: str
    request_id: str
    id: str = field(default_factory=new_id)
    mission_id: str | None = None
    tool_input: dict = field(default_factory=dict)
    risk: str = "confirm"              # safe | confirm | dangerous
    description: str = ""
    status: str = "pending"            # pending | allowed | denied | expired | superseded
    requested_at: str = field(default_factory=utcnow)
    resolved_at: str | None = None
    resolved_by: str | None = None     # voice | ui | api | mode_switch

    def resolve(self, decision: str, by: str) -> None:
        if decision not in ("allowed", "denied", "expired", "superseded"):
            raise ValueError(f"bad decision {decision!r}")
        self.status = decision
        self.resolved_at = utcnow()
        self.resolved_by = by

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Approval":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
```

`backend/yuri/domain/event.py`:
```python
"""YuriEvent — the normalized event every subsystem emits (spec §11). `severity`
and `speakable` are hints the narration layer (Phase 4) filters on."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow


class EventType:
    MISSION_CREATED = "mission.created"
    MISSION_STATUS_CHANGED = "mission.status_changed"
    SESSION_CREATED = "session.created"
    SESSION_MESSAGE_SENT = "session.message_sent"
    SESSION_TURN_COMPLETED = "session.turn_completed"
    SESSION_QUESTION = "session.question"
    SESSION_INTERRUPTED = "session.interrupted"
    SESSION_STOPPED = "session.stopped"
    SESSION_LOST = "session.lost"
    TOOL_STARTED = "tool.started"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    COST_UPDATED = "cost.updated"
    AGENT_ERROR = "agent.error"
    PROJECT_REGISTERED = "project.registered"
    MEMORY_REMEMBERED = "memory.remembered"


# type -> (severity, speakable)   (spec §6.1)
DEFAULTS: dict[str, tuple[str, bool]] = {
    EventType.TOOL_STARTED: ("debug", False),
    EventType.SESSION_MESSAGE_SENT: ("debug", False),
    EventType.COST_UPDATED: ("debug", False),
    EventType.SESSION_CREATED: ("info", False),
    EventType.MISSION_CREATED: ("info", True),
    EventType.MISSION_STATUS_CHANGED: ("info", True),
    EventType.SESSION_TURN_COMPLETED: ("info", True),
    EventType.SESSION_QUESTION: ("notice", True),
    EventType.APPROVAL_REQUESTED: ("notice", True),
    EventType.APPROVAL_RESOLVED: ("info", False),
    EventType.SESSION_INTERRUPTED: ("info", False),
    EventType.SESSION_STOPPED: ("info", False),
    EventType.SESSION_LOST: ("warning", True),
    EventType.AGENT_ERROR: ("error", True),
    EventType.PROJECT_REGISTERED: ("info", False),
    EventType.MEMORY_REMEMBERED: ("info", False),
}


@dataclass
class YuriEvent:
    type: str
    id: str = field(default_factory=new_id)
    ts: str = field(default_factory=utcnow)
    mission_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    project_id: str | None = None
    severity: str = "info"
    speakable: bool = False
    payload: dict = field(default_factory=dict)

    @classmethod
    def make(cls, type: str, **fields) -> "YuriEvent":
        sev, speak = DEFAULTS.get(type, ("info", False))
        fields.setdefault("severity", sev)
        fields.setdefault("speakable", speak)
        return cls(type=type, **fields)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "YuriEvent":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})
```

`backend/yuri/domain/risk.py`:
```python
"""Risk classification for approvals (spec §4.3). Reuses permissions.classify
for the safe set; adds a small destructive-pattern list for Bash. Not a policy
engine — a tuple of regexes with tests."""
from __future__ import annotations

import re

from permissions import EDIT_TOOLS, classify

DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p, re.I) for p in (
    r"\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-r|-rf|-fr|--recursive)\b",
    r"\bsudo\s+rm\b",
    r"\bgit\s+push\b.*(--force|-f)\b",
    r"\bgit\s+reset\s+--hard\b",
    r"\bdrop\s+(table|database|schema)\b",
    r"\bmkfs(\.\w+)?\b",
    r">\s*/dev/(sd|nvme|disk|hd)",
    r"\bchmod\s+-R\s+777\b",
    r"\bdd\s+if=",
))


def risk_for(tool_name: str, tool_input: dict | None) -> str:
    kind = classify(tool_name)
    if kind in ("safe", "question"):
        return "safe"
    if tool_name in EDIT_TOOLS:
        return "confirm"
    if tool_name == "Bash":
        cmd = str((tool_input or {}).get("command") or "")
        if any(p.search(cmd) for p in DANGEROUS_PATTERNS):
            return "dangerous"
    return "confirm"
```

- [ ] **Step 4: Run** `tests.test_domain tests.test_risk` → PASS; full suite → `OK`.

---

### Task 11: SQLite store + migrations

**Files:**
- Create: `backend/yuri/store/__init__.py`, `backend/yuri/store/base.py`, `backend/yuri/store/sqlite.py`, `backend/yuri/store/migrations/0001_init.sql`
- Test: `backend/tests/test_store.py`

**Interfaces — Produces:**
```python
yuri.store.base:
  PendingApprovalExists(ValueError)
  class ProjectRepo(ABC): insert(p), get(id)->Project|None, get_by_slug(slug), get_by_root(root_path), list()->list[Project], update(p)
  class MissionRepo(ABC): insert(m), get(id), list(status=None, limit=200), update(m), insert_step(step), steps_for(mission_id)->list[MissionStep], update_step(step)
  class SessionRepo(ABC): insert(s), get(id), get_by_native(native_id), list(mission_id=None, live_only=False), update(s)
  class ApprovalRepo(ABC): insert(a) [raises PendingApprovalExists], get(id), get_by_request(request_id), pending_for_session(session_id)->Approval|None, list(status=None, session_id=None, limit=200), update(a)
  class EventRepo(ABC): insert(e), list(mission_id=None, session_id=None, since=None, limit=200)->list[YuriEvent] (oldest first)
  class SettingsRepo(ABC): get(key, default=None), set(key, value)
  class Store(ABC): projects, missions, sessions, approvals, events, settings; migrate(); close()
yuri.store.sqlite: SqliteStore(path: str) -> Store ; SCHEMA_VERSION = 1
```

- [ ] **Step 1: Failing test `backend/tests/test_store.py`**

```python
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.approval import Approval  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.domain.mission import Mission, MissionStep  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.store.base import PendingApprovalExists  # noqa: E402
from yuri.store.sqlite import SCHEMA_VERSION, SqliteStore  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "yuri.db")
        self.store = SqliteStore(self.path)
        self.store.migrate()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_migrate_idempotent_and_versioned(self):
        self.store.migrate()
        self.assertEqual(self.store.settings.get("schema_version"), SCHEMA_VERSION)
        con = sqlite3.connect(self.path)
        try:
            self.assertEqual(con.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertTrue({"projects", "missions", "mission_steps", "sessions", "approvals",
                         "events", "settings"} <= names)

    def test_project_round_trip(self):
        p = Project(slug="x", name="X", root_path="/tmp/x", auto_approve_edits=True)
        self.store.projects.insert(p)
        self.assertEqual(self.store.projects.get(p.id), p)
        self.assertEqual(self.store.projects.get_by_slug("x"), p)
        self.assertEqual(self.store.projects.get_by_root("/tmp/x"), p)
        p.name = "Y"
        self.store.projects.update(p)
        self.assertEqual(self.store.projects.get(p.id).name, "Y")
        self.assertEqual([q.id for q in self.store.projects.list()], [p.id])

    def test_mission_and_steps(self):
        p = Project(slug="x", name="X", root_path="/tmp/x")
        self.store.projects.insert(p)
        m = Mission(title="fix", project_id=p.id, metadata={"a": 1})
        self.store.missions.insert(m)
        st = MissionStep(mission_id=m.id, ordinal=1, title="work")
        self.store.missions.insert_step(st)
        self.assertEqual(self.store.missions.get(m.id), m)
        self.assertEqual(self.store.missions.steps_for(m.id), [st])
        m.transition("paused")
        self.store.missions.update(m)
        self.assertEqual([x.id for x in self.store.missions.list(status="paused")], [m.id])
        self.assertEqual(self.store.missions.list(status="running"), [])
        st.status = "done"
        self.store.missions.update_step(st)
        self.assertEqual(self.store.missions.steps_for(m.id)[0].status, "done")

    def test_session_lookups(self):
        p = Project(slug="x", name="X", root_path="/tmp/x")
        self.store.projects.insert(p)
        s = AgentSession(project_id=p.id, agent_id="claude-code", native_session_id="h1",
                         backend="cli", working_directory="/tmp/x", status="running")
        self.store.sessions.insert(s)
        self.assertEqual(self.store.sessions.get_by_native("h1"), s)
        s2 = AgentSession(project_id=p.id, agent_id="claude-code", native_session_id="h2",
                          backend="cli", working_directory="/tmp/x", status="stopped")
        self.store.sessions.insert(s2)
        self.assertEqual([x.id for x in self.store.sessions.list(live_only=True)], [s.id])
        s.status = "lost"
        self.store.sessions.update(s)
        self.assertEqual(self.store.sessions.list(live_only=True), [])

    def test_one_pending_approval_per_session(self):
        a1 = Approval(session_id="s1", agent_id="a", action="run", tool_name="Bash", request_id="r1")
        self.store.approvals.insert(a1)
        a2 = Approval(session_id="s1", agent_id="a", action="run", tool_name="Bash", request_id="r2")
        with self.assertRaises(PendingApprovalExists):
            self.store.approvals.insert(a2)
        self.assertEqual(self.store.approvals.pending_for_session("s1"), a1)
        a1.resolve("denied", "voice")
        self.store.approvals.update(a1)
        self.store.approvals.insert(a2)  # allowed now
        self.assertEqual(self.store.approvals.get_by_request("r2"), a2)
        self.assertEqual([x.id for x in self.store.approvals.list(status="denied")], [a1.id])

    def test_events_filter_and_order(self):
        for i in range(3):
            self.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id="m1",
                                                    payload={"i": i}))
        self.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id="m2"))
        got = self.store.events.list(mission_id="m1")
        self.assertEqual([e.payload["i"] for e in got], [0, 1, 2])
        self.assertEqual(len(self.store.events.list(limit=2)), 2)
        since = got[1].ts
        later = self.store.events.list(mission_id="m1", since=since)
        self.assertTrue(all(e.ts >= since for e in later))

    def test_settings_json(self):
        self.store.settings.set("k", {"x": [1, 2]})
        self.assertEqual(self.store.settings.get("k"), {"x": [1, 2]})
        self.assertEqual(self.store.settings.get("missing", 7), 7)

    def test_threads_get_their_own_connection(self):
        errors = []

        def work(n):
            try:
                self.store.settings.set(f"t{n}", n)
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(self.store.settings.get("t3"), 3)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError`).

- [ ] **Step 3: Write `backend/yuri/store/__init__.py`** (empty) and **`backend/yuri/store/base.py`**

```python
"""Repository interfaces (spec §4.4). Sync methods — async callers wrap them in
run_in_threadpool / asyncio.to_thread. Kept as ABCs so Postgres can replace
SQLite later without touching services."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from yuri.domain.approval import Approval
from yuri.domain.event import YuriEvent
from yuri.domain.mission import Mission, MissionStep
from yuri.domain.project import Project
from yuri.domain.session import AgentSession


class PendingApprovalExists(ValueError):
    """A session already has a pending approval (one decision per prompt)."""


class ProjectRepo(ABC):
    @abstractmethod
    def insert(self, p: Project) -> None: ...
    @abstractmethod
    def get(self, id: str) -> Project | None: ...
    @abstractmethod
    def get_by_slug(self, slug: str) -> Project | None: ...
    @abstractmethod
    def get_by_root(self, root_path: str) -> Project | None: ...
    @abstractmethod
    def list(self) -> list[Project]: ...
    @abstractmethod
    def update(self, p: Project) -> None: ...


class MissionRepo(ABC):
    @abstractmethod
    def insert(self, m: Mission) -> None: ...
    @abstractmethod
    def get(self, id: str) -> Mission | None: ...
    @abstractmethod
    def list(self, status: str | None = None, limit: int = 200) -> list[Mission]: ...
    @abstractmethod
    def update(self, m: Mission) -> None: ...
    @abstractmethod
    def insert_step(self, step: MissionStep) -> None: ...
    @abstractmethod
    def steps_for(self, mission_id: str) -> list[MissionStep]: ...
    @abstractmethod
    def update_step(self, step: MissionStep) -> None: ...


class SessionRepo(ABC):
    @abstractmethod
    def insert(self, s: AgentSession) -> None: ...
    @abstractmethod
    def get(self, id: str) -> AgentSession | None: ...
    @abstractmethod
    def get_by_native(self, native_id: str) -> AgentSession | None: ...
    @abstractmethod
    def list(self, mission_id: str | None = None, live_only: bool = False) -> list[AgentSession]: ...
    @abstractmethod
    def update(self, s: AgentSession) -> None: ...


class ApprovalRepo(ABC):
    @abstractmethod
    def insert(self, a: Approval) -> None: ...
    @abstractmethod
    def get(self, id: str) -> Approval | None: ...
    @abstractmethod
    def get_by_request(self, request_id: str) -> Approval | None: ...
    @abstractmethod
    def pending_for_session(self, session_id: str) -> Approval | None: ...
    @abstractmethod
    def list(self, status: str | None = None, session_id: str | None = None,
             limit: int = 200) -> list[Approval]: ...
    @abstractmethod
    def update(self, a: Approval) -> None: ...


class EventRepo(ABC):
    @abstractmethod
    def insert(self, e: YuriEvent) -> None: ...
    @abstractmethod
    def list(self, mission_id: str | None = None, session_id: str | None = None,
             since: str | None = None, limit: int = 200) -> list[YuriEvent]: ...


class SettingsRepo(ABC):
    @abstractmethod
    def get(self, key: str, default: Any = None) -> Any: ...
    @abstractmethod
    def set(self, key: str, value: Any) -> None: ...


class Store(ABC):
    projects: ProjectRepo
    missions: MissionRepo
    sessions: SessionRepo
    approvals: ApprovalRepo
    events: EventRepo
    settings: SettingsRepo

    @abstractmethod
    def migrate(self) -> None: ...
    @abstractmethod
    def close(self) -> None: ...
```

- [ ] **Step 4: Write `backend/yuri/store/migrations/0001_init.sql`**

```sql
-- Yuri state store v1 (spec §4). JSON columns are TEXT holding json.dumps().
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'user',
  default_agent TEXT,
  auto_approve_edits INTEGER NOT NULL DEFAULT 0,
  repo_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  goal TEXT,
  project_id TEXT NOT NULL REFERENCES projects(id),
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  current_step TEXT,
  created_by TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS missions_status ON missions(status);

CREATE TABLE IF NOT EXISTS mission_steps (
  id TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL REFERENCES missions(id),
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  agent_id TEXT,
  status TEXT NOT NULL,
  session_id TEXT,
  result TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS mission_steps_mission ON mission_steps(mission_id, ordinal);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  mission_id TEXT REFERENCES missions(id),
  project_id TEXT NOT NULL REFERENCES projects(id),
  agent_id TEXT NOT NULL,
  native_session_id TEXT NOT NULL,
  backend TEXT NOT NULL,
  status TEXT NOT NULL,
  name TEXT,
  mode TEXT NOT NULL DEFAULT 'default',
  model TEXT,
  working_directory TEXT NOT NULL,
  started_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  runtime_metadata TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS sessions_mission ON sessions(mission_id);
CREATE INDEX IF NOT EXISTS sessions_native ON sessions(native_session_id);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  mission_id TEXT,
  session_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  action TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  tool_input TEXT NOT NULL DEFAULT '{}',
  risk TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  request_id TEXT NOT NULL UNIQUE,
  requested_at TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT
);
CREATE INDEX IF NOT EXISTS approvals_session_status ON approvals(session_id, status);
-- one decision per prompt (encodes the fix in commit 14bc293)
CREATE UNIQUE INDEX IF NOT EXISTS approvals_one_pending ON approvals(session_id) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  mission_id TEXT,
  session_id TEXT,
  agent_id TEXT,
  project_id TEXT,
  severity TEXT NOT NULL,
  speakable INTEGER NOT NULL DEFAULT 0,
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_mission_ts ON events(mission_id, ts);
CREATE INDEX IF NOT EXISTS events_session_ts ON events(session_id, ts);
```

- [ ] **Step 5: Write `backend/yuri/store/sqlite.py`**

```python
"""stdlib sqlite3 implementation of the repositories. One connection per
thread (threading.local) so FastAPI's threadpool workers never share a
connection; WAL so readers don't block the writer."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import fields
from typing import Any, Iterable

from yuri.domain.approval import Approval
from yuri.domain.event import YuriEvent
from yuri.domain.mission import Mission, MissionStep
from yuri.domain.project import Project
from yuri.domain.session import AgentSession
from .base import (ApprovalRepo, EventRepo, MissionRepo, PendingApprovalExists, ProjectRepo,
                   SessionRepo, SettingsRepo, Store)

SCHEMA_VERSION = 1
_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

_JSON_COLS = {"metadata", "result", "runtime_metadata", "tool_input", "payload"}
_BOOL_COLS = {"auto_approve_edits", "speakable"}


class _Conn:
    """Per-thread connection factory."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA foreign_keys=ON")
            self._local.con = con
            with self._lock:
                self._all.append(con)
        return con

    def close_all(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:
                    pass
            self._all.clear()
        self._local = threading.local()


def _to_row(obj: Any) -> dict[str, Any]:
    d = {}
    for f in fields(obj):
        v = getattr(obj, f.name)
        if f.name in _JSON_COLS:
            v = json.dumps(v, default=str)
        elif f.name in _BOOL_COLS:
            v = 1 if v else 0
        d[f.name] = v
    return d


def _from_row(cls, row: sqlite3.Row | None):
    if row is None:
        return None
    d = dict(row)
    for k in list(d):
        if k in _JSON_COLS and isinstance(d[k], str):
            d[k] = json.loads(d[k])
        elif k in _BOOL_COLS:
            d[k] = bool(d[k])
    return cls.from_dict(d)


def _insert_sql(table: str, row: dict[str, Any]) -> tuple[str, list[Any]]:
    cols = list(row)
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [row[c] for c in cols])


def _update_sql(table: str, row: dict[str, Any]) -> tuple[str, list[Any]]:
    cols = [c for c in row if c != "id"]
    return (f"UPDATE {table} SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
            [row[c] for c in cols] + [row["id"]])


class _Base:
    table = ""
    cls: Any = None

    def __init__(self, conn: _Conn):
        self._c = conn

    def _one(self, sql: str, args: Iterable[Any] = ()):
        return _from_row(self.cls, self._c.get().execute(sql, tuple(args)).fetchone())

    def _many(self, sql: str, args: Iterable[Any] = ()):
        return [_from_row(self.cls, r) for r in self._c.get().execute(sql, tuple(args)).fetchall()]

    def insert(self, obj) -> None:
        sql, args = _insert_sql(self.table, _to_row(obj))
        self._c.get().execute(sql, args)

    def update(self, obj) -> None:
        sql, args = _update_sql(self.table, _to_row(obj))
        self._c.get().execute(sql, args)

    def get(self, id: str):
        return self._one(f"SELECT * FROM {self.table} WHERE id = ?", (id,))


class SqliteProjects(_Base, ProjectRepo):
    table, cls = "projects", Project

    def get_by_slug(self, slug):
        return self._one("SELECT * FROM projects WHERE slug = ?", (slug,))

    def get_by_root(self, root_path):
        return self._one("SELECT * FROM projects WHERE root_path = ?", (root_path,))

    def list(self):
        return self._many("SELECT * FROM projects ORDER BY name COLLATE NOCASE")


class SqliteMissions(_Base, MissionRepo):
    table, cls = "missions", Mission

    def list(self, status=None, limit=200):
        if status:
            return self._many("SELECT * FROM missions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                              (status, limit))
        return self._many("SELECT * FROM missions ORDER BY updated_at DESC LIMIT ?", (limit,))

    def insert_step(self, step):
        sql, args = _insert_sql("mission_steps", _to_row(step))
        self._c.get().execute(sql, args)

    def steps_for(self, mission_id):
        rows = self._c.get().execute(
            "SELECT * FROM mission_steps WHERE mission_id = ? ORDER BY ordinal", (mission_id,)).fetchall()
        return [_from_row(MissionStep, r) for r in rows]

    def update_step(self, step):
        sql, args = _update_sql("mission_steps", _to_row(step))
        self._c.get().execute(sql, args)


class SqliteSessions(_Base, SessionRepo):
    table, cls = "sessions", AgentSession

    def get_by_native(self, native_id):
        return self._one("SELECT * FROM sessions WHERE native_session_id = ? "
                         "ORDER BY started_at DESC LIMIT 1", (native_id,))

    def list(self, mission_id=None, live_only=False):
        where, args = [], []
        if mission_id:
            where.append("mission_id = ?")
            args.append(mission_id)
        if live_only:
            live = ("starting", "running", "needs_permission", "needs_choice", "idle")
            where.append(f"status IN ({', '.join('?' * len(live))})")
            args.extend(live)
        sql = "SELECT * FROM sessions" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY started_at"
        return self._many(sql, args)


class SqliteApprovals(_Base, ApprovalRepo):
    table, cls = "approvals", Approval

    def insert(self, a):
        try:
            super().insert(a)
        except sqlite3.IntegrityError as exc:
            if "approvals_one_pending" in str(exc) or "approvals.session_id" in str(exc):
                raise PendingApprovalExists(
                    f"session {a.session_id} already has a pending approval") from exc
            raise

    def get_by_request(self, request_id):
        return self._one("SELECT * FROM approvals WHERE request_id = ?", (request_id,))

    def pending_for_session(self, session_id):
        return self._one("SELECT * FROM approvals WHERE session_id = ? AND status = 'pending'",
                         (session_id,))

    def list(self, status=None, session_id=None, limit=200):
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        sql = "SELECT * FROM approvals" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY requested_at DESC LIMIT ?"
        return self._many(sql, args + [limit])


class SqliteEvents(_Base, EventRepo):
    table, cls = "events", YuriEvent

    def list(self, mission_id=None, session_id=None, since=None, limit=200):
        where, args = [], []
        if mission_id:
            where.append("mission_id = ?")
            args.append(mission_id)
        if session_id:
            where.append("session_id = ?")
            args.append(session_id)
        if since:
            where.append("ts >= ?")
            args.append(since)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        # newest `limit` rows, returned oldest-first
        sql = f"SELECT * FROM (SELECT * FROM events{clause} ORDER BY ts DESC, rowid DESC LIMIT ?) " \
              "ORDER BY ts ASC, rowid ASC"
        return self._many(sql, args + [limit])


class SqliteSettings(SettingsRepo):
    def __init__(self, conn: _Conn):
        self._c = conn

    def get(self, key, default=None):
        row = self._c.get().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self._c.get().execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                              (key, json.dumps(value)))


class SqliteStore(Store):
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self._conn = _Conn(path)
        self.projects = SqliteProjects(self._conn)
        self.missions = SqliteMissions(self._conn)
        self.sessions = SqliteSessions(self._conn)
        self.approvals = SqliteApprovals(self._conn)
        self.events = SqliteEvents(self._conn)
        self.settings = SqliteSettings(self._conn)

    def migrate(self) -> None:
        con = self._conn.get()
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        current = self.settings.get("schema_version", 0)
        for fname in sorted(os.listdir(_MIGRATIONS_DIR)):
            if not fname.endswith(".sql"):
                continue
            version = int(fname.split("_", 1)[0])
            if version <= current:
                continue
            with open(os.path.join(_MIGRATIONS_DIR, fname), encoding="utf-8") as f:
                sql = f.read()
            con.execute("BEGIN")
            try:
                for stmt in _statements(sql):
                    con.execute(stmt)
                self.settings.set("schema_version", version)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            current = version

    def close(self) -> None:
        self._conn.close_all()


def _statements(sql: str) -> list[str]:
    """Split a migration file on ';' at line ends (no procedural SQL here)."""
    out, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            out.append("\n".join(buf).rstrip(";"))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out
```

`executescript` is deliberately not used in `migrate()`: it auto-commits and would break the per-file transaction.

- [ ] **Step 6: Run** `tests.test_store` → PASS; full suite → `OK`.

---

### Task 12: EventBus

**Files:**
- Create: `backend/yuri/events/__init__.py`, `backend/yuri/events/bus.py`
- Test: `backend/tests/test_event_bus.py`

**Interfaces — Produces:**
```python
EventBus(repo: EventRepo | None = None, bridge: Callable[[YuriEvent], None] | None = None)
  .publish(e: YuriEvent) -> YuriEvent      # sync, never raises
  .subscribe() -> asyncio.Queue ; .unsubscribe(q)
  .start_writer() ; async .stop_writer()   # drains the persist queue via asyncio.to_thread(repo.insert)
  async .drain()                           # test helper: wait until the persist queue is empty
bridge_to_event_log(e: YuriEvent) -> None  # summary line into event_log.log_event(source="yuri", dest="ui", kind=e.type, …)
summarize(e: YuriEvent) -> str
```

- [ ] **Step 1: Failing test `backend/tests/test_event_bus.py`**

```python
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import event_log  # noqa: E402
from yuri.domain.event import EventType, YuriEvent  # noqa: E402
from yuri.events.bus import EventBus, bridge_to_event_log, summarize  # noqa: E402


class _MemRepo:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def insert(self, e):
        if self.fail:
            raise RuntimeError("disk on fire")
        self.rows.append(e)

    def list(self, **kw):
        return list(self.rows)


class Bus(unittest.IsolatedAsyncioTestCase):
    async def test_fanout_persist_and_bridge(self):
        repo = _MemRepo()
        bridged = []
        bus = EventBus(repo=repo, bridge=bridged.append)
        bus.start_writer()
        q = bus.subscribe()
        try:
            e = bus.publish(YuriEvent.make(EventType.MISSION_CREATED, mission_id="m",
                                           payload={"title": "Fix it"}))
            got = await asyncio.wait_for(q.get(), 1.0)
            self.assertEqual(got.id, e.id)
            await bus.drain()
            self.assertEqual([r.id for r in repo.rows], [e.id])
            self.assertEqual(bridged, [e])
        finally:
            bus.unsubscribe(q)
            await bus.stop_writer()

    async def test_slow_subscriber_drops(self):
        bus = EventBus()
        q = bus.subscribe()
        for _ in range(q.maxsize + 10):
            bus.publish(YuriEvent.make(EventType.TOOL_STARTED))
        self.assertEqual(q.qsize(), q.maxsize)
        bus.unsubscribe(q)

    async def test_publish_never_raises(self):
        bus = EventBus(repo=_MemRepo(fail=True), bridge=lambda e: 1 / 0)
        bus.start_writer()
        try:
            bus.publish(YuriEvent.make(EventType.TOOL_STARTED))
            await bus.drain()  # writer swallows the repo error
        finally:
            await bus.stop_writer()

    async def test_bridge_to_event_log(self):
        event_log._buffer.clear()
        e = YuriEvent.make(EventType.APPROVAL_REQUESTED, session_id="s1",
                           payload={"description": "run rm -rf build", "session_name": "billing"})
        bridge_to_event_log(e)
        rec = event_log.recent(1)[0]
        self.assertEqual((rec["source"], rec["dest"], rec["kind"]), ("yuri", "ui", e.type))
        self.assertEqual(rec["session"], "billing")
        self.assertIn("rm -rf build", rec["summary"])
        self.assertEqual(rec["detail"]["id"], e.id)

    def test_summaries(self):
        self.assertEqual(summarize(YuriEvent.make(EventType.MISSION_CREATED,
                                                  payload={"title": "T"})), "mission created: T")
        self.assertEqual(summarize(YuriEvent.make(EventType.SESSION_TURN_COMPLETED,
                                                  payload={"assistant_text": "x" * 500}))[:16],
                         "turn completed: ")
        self.assertEqual(summarize(YuriEvent.make(EventType.TOOL_STARTED,
                                                  payload={"tool_name": "Read"})), "tool: Read")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/events/__init__.py`** (empty) **and `backend/yuri/events/bus.py`**

```python
"""Yuri's domain EventBus (spec §6.1): one producer, three sinks — the event
repo (persisted via a background writer), live subscribers (SSE), and a bridge
into the existing debug bus so the Activity panel shows Yuri events today.
publish() is sync, non-blocking and never raises — same discipline as
event_log.log_event, so the tmux runner's sync paths can call into it safely."""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import event_log
from yuri.domain.event import EventType, YuriEvent
from yuri.store.base import EventRepo

log = logging.getLogger("yuri.events")


def summarize(e: YuriEvent) -> str:
    p = e.payload or {}
    t = e.type
    if t == EventType.MISSION_CREATED:
        return f"mission created: {p.get('title', '')}"
    if t == EventType.MISSION_STATUS_CHANGED:
        return f"mission {p.get('from', '?')} → {p.get('to', '?')}"
    if t == EventType.SESSION_CREATED:
        return f"session created: {p.get('name') or p.get('native_session_id', '')}"
    if t == EventType.SESSION_MESSAGE_SENT:
        return f"→ agent: {str(p.get('message', ''))[:120]}"
    if t == EventType.SESSION_TURN_COMPLETED:
        return f"turn completed: {str(p.get('assistant_text', ''))[:160]}"
    if t == EventType.SESSION_QUESTION:
        return f"question: {p.get('text', '')}"
    if t == EventType.TOOL_STARTED:
        return f"tool: {p.get('tool_name', '')}"
    if t == EventType.APPROVAL_REQUESTED:
        return f"needs approval [{p.get('risk', '?')}]: {p.get('description', '')}"
    if t == EventType.APPROVAL_RESOLVED:
        return f"approval {p.get('status', '?')} by {p.get('by', '?')}: {p.get('description', '')}"
    if t == EventType.COST_UPDATED:
        c = p.get("cost_usd")
        return f"cost ${c:.4f}" if isinstance(c, (int, float)) else "cost updated"
    if t == EventType.AGENT_ERROR:
        return f"agent error: {p.get('message', '')}"
    if t == EventType.SESSION_LOST:
        return "session lost (agent process did not survive the restart)"
    if t == EventType.MEMORY_REMEMBERED:
        return f"remembered: {str(p.get('fact', ''))[:120]}"
    return t


def bridge_to_event_log(e: YuriEvent) -> None:
    p = e.payload or {}
    session = p.get("session_name") or p.get("native_session_id") or e.session_id
    event_log.log_event("yuri", "ui", e.type, summarize(e), session=session, detail=e.to_dict())


class EventBus:
    def __init__(self, repo: EventRepo | None = None,
                 bridge: Callable[[YuriEvent], None] | None = None):
        self._repo = repo
        self._bridge = bridge
        self._subs: set[asyncio.Queue] = set()
        self._persist_q: "asyncio.Queue[YuriEvent]" = asyncio.Queue(maxsize=20000)
        self._writer: Optional[asyncio.Task] = None

    def publish(self, e: YuriEvent) -> YuriEvent:
        for q in list(self._subs):
            try:
                q.put_nowait(e)
            except asyncio.QueueFull:
                pass
        if self._repo is not None:
            try:
                self._persist_q.put_nowait(e)
            except asyncio.QueueFull:
                log.warning("event persist queue full; dropping %s", e.type)
        if self._bridge is not None:
            try:
                self._bridge(e)
            except Exception:
                log.exception("event bridge failed")
        return e

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def start_writer(self) -> None:
        if self._repo is not None and (self._writer is None or self._writer.done()):
            self._writer = asyncio.create_task(self._write_loop())

    async def stop_writer(self) -> None:
        if self._writer is not None:
            self._writer.cancel()
            try:
                await self._writer
            except (asyncio.CancelledError, Exception):
                pass
            self._writer = None

    async def drain(self) -> None:
        await self._persist_q.join()

    async def _write_loop(self) -> None:
        assert self._repo is not None
        while True:
            e = await self._persist_q.get()
            try:
                await asyncio.to_thread(self._repo.insert, e)
            except Exception:
                log.exception("event persist failed")
            finally:
                self._persist_q.task_done()
```

- [ ] **Step 4: Run** `tests.test_event_bus` → PASS; full suite → `OK`.

---

### Task 13: Journal + Memory services

**Files:**
- Create: `backend/yuri/services/__init__.py`, `backend/yuri/services/journal.py`, `backend/yuri/services/memory.py`
- Test: `backend/tests/test_journal_memory.py`

**Interfaces — Produces:**
```python
Journal(home: Home): .append(line: str) -> str (path) ; .read_today(cap=4000) -> str ; .today_path() -> str
Memory(home: Home): .remember(fact: str, project_slug: str | None = None) -> str (path) ; .read_user(cap=4000) -> str ; .read_project(slug, cap=4000) -> str
BadSlug(ValueError)
```

- [ ] **Step 1: Failing test**

```python
import datetime
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.memory import BadSlug, Memory  # noqa: E402


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.j = Journal(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_creates_dated_file_with_header(self):
        path = self.j.append("mission created: Fix it")
        today = datetime.date.today().isoformat()
        self.assertEqual(os.path.basename(path), f"{today}.md")
        with open(path) as f:
            text = f.read()
        self.assertTrue(text.startswith(f"# {today}\n"))
        self.assertRegex(text, r"\n- \d\d:\d\d  mission created: Fix it\n")
        self.j.append("second")
        self.assertIn("second", self.j.read_today())

    def test_read_today_caps_and_handles_missing(self):
        self.assertEqual(self.j.read_today(), "")
        self.j.append("x" * 5000)
        self.assertLessEqual(len(self.j.read_today(cap=100)), 100)

    def test_newlines_in_line_are_flattened(self):
        self.j.append("a\nb")
        self.assertIn("- ", self.j.read_today())
        self.assertNotIn("a\nb", self.j.read_today())


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.m = Memory(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_user(self):
        path = self.m.remember("prefers pnpm over npm")
        self.assertEqual(path, self.home.user_memory_path)
        text = self.m.read_user()
        self.assertRegex(text, r"- \d{4}-\d\d-\d\d  prefers pnpm over npm")

    def test_remember_project(self):
        path = self.m.remember("tests live in backend/tests", project_slug="yuri-code")
        self.assertEqual(path, os.path.join(self.home.projects_memory_dir, "yuri-code.md"))
        self.assertIn("tests live in", self.m.read_project("yuri-code"))

    def test_bad_slug_rejected(self):
        for bad in ["../etc", "a/b", "UPPER", "", "x" * 65, "sp ace"]:
            with self.assertRaises(BadSlug):
                self.m.remember("x", project_slug=bad)

    def test_empty_fact_rejected(self):
        with self.assertRaises(ValueError):
            self.m.remember("   ")

    def test_read_user_cap_keeps_tail(self):
        for i in range(200):
            self.m.remember(f"fact {i}")
        out = self.m.read_user(cap=300)
        self.assertLessEqual(len(out), 300)
        self.assertIn("fact 199", out)  # most recent survives the cap


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/services/__init__.py`** (empty), **`journal.py`**, **`memory.py`**

`backend/yuri/services/journal.py`:
```python
"""Daily journal (spec §5.5): one append-only markdown file per day so Yuri can
answer "what happened yesterday?" from her own records."""
from __future__ import annotations

import datetime
import os

from yuri.home import Home


def _tail(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[-cap:]


class Journal:
    def __init__(self, home: Home):
        self.home = home

    def today_path(self) -> str:
        return os.path.join(self.home.journal_dir, f"{datetime.date.today().isoformat()}.md")

    def append(self, line: str) -> str:
        path = self.today_path()
        line = " ".join(str(line).split())
        now = datetime.datetime.now().strftime("%H:%M")
        os.makedirs(self.home.journal_dir, exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new:
                f.write(f"# {datetime.date.today().isoformat()}\n\n")
            f.write(f"- {now}  {line}\n")
        return path

    def read_today(self, cap: int = 4000) -> str:
        path = self.today_path()
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return _tail(f.read(), cap)
```

`backend/yuri/services/memory.py`:
```python
"""Structured memory, file-backed (spec §5.6): dated lines appended to
memory/user.md or memory/projects/<slug>.md. Deliberately no schema — the user
can read and edit what Yuri knows in a text editor. Path segments come only
from a validated slug, never from spoken text."""
from __future__ import annotations

import datetime
import os
import re

from yuri.home import Home

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")


class BadSlug(ValueError):
    pass


def _tail(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[-cap:]


class Memory:
    def __init__(self, home: Home):
        self.home = home

    def _project_path(self, slug: str) -> str:
        if not _SLUG_RE.match(slug or ""):
            raise BadSlug(f"invalid project slug {slug!r} (lowercase letters, digits, dashes)")
        path = os.path.realpath(os.path.join(self.home.projects_memory_dir, f"{slug}.md"))
        root = os.path.realpath(self.home.memory_dir)
        if not path.startswith(root + os.sep):
            raise BadSlug("memory path escaped memory/")  # belt and braces
        return path

    def remember(self, fact: str, project_slug: str | None = None) -> str:
        fact = " ".join(str(fact or "").split())
        if not fact:
            raise ValueError("nothing to remember")
        path = self._project_path(project_slug) if project_slug else self.home.user_memory_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        new = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if new and project_slug:
                f.write(f"# Project notes: {project_slug}\n\n")
            f.write(f"- {datetime.date.today().isoformat()}  {fact}\n")
        return path

    def read_user(self, cap: int = 4000) -> str:
        return self._read(self.home.user_memory_path, cap)

    def read_project(self, slug: str, cap: int = 4000) -> str:
        return self._read(self._project_path(slug), cap)

    @staticmethod
    def _read(path: str, cap: int) -> str:
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return _tail(f.read(), cap)
```

- [ ] **Step 4: Run** `tests.test_journal_memory` → PASS; full suite → `OK`.

---

### Task 14: ProjectService + ApprovalService

**Files:**
- Create: `backend/yuri/services/projects.py`, `backend/yuri/services/approvals.py`
- Test: `backend/tests/test_project_service.py`, `backend/tests/test_approval_service.py`

**Interfaces — Produces:**
```python
ProjectService(store: Store, home: Home, bus: EventBus)
  .ensure_home() -> Project                 # kind="home", slug "yuri", auto_approve_edits=True
  .home() -> Project
  .resolve_or_create(ref: str) -> Project   # session_manager.resolve_project_path then upsert; ValueError on bad ref
  .register(path: str, name: str|None=None, default_agent: str|None=None) -> Project
  .get(id) -> Project (KeyError)
  .list() -> dict {roots, projects: [ {name, path, registered: bool, id?, slug?, kind?, default_agent?} ]}
ApprovalService(store: Store, bus: EventBus, journal: Journal)
  .record_request(session: AgentSession, prompt: dict) -> Approval   # idempotent on prompt["request_id"]
  .resolve(approval_id, decision: "allowed"|"denied", by) -> Approval  (KeyError / ValueError if not pending)
  .resolve_by_session(session: AgentSession, choice: str, by) -> Approval | None   # decide_permission; None when no pending; ValueError when ambiguous
  .pending() -> list[Approval] ; .get(id) ; .list(status=None)
```

- [ ] **Step 1: Failing tests**

`backend/tests/test_project_service.py`:
```python
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.projects import ProjectService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class ProjectServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "alpha"))
        self.home = Home(os.path.join(self.root, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.events = self.bus.subscribe()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", self.home.path)]
        [p.start() for p in self.patches]
        self.svc = ProjectService(self.store, self.home, self.bus)

    def tearDown(self):
        [p.stop() for p in self.patches]
        self.store.close()
        self.tmp.cleanup()

    def test_ensure_home_is_idempotent(self):
        h1 = self.svc.ensure_home()
        h2 = self.svc.ensure_home()
        self.assertEqual(h1.id, h2.id)
        self.assertEqual((h1.kind, h1.slug, h1.auto_approve_edits), ("home", "yuri", True))
        self.assertEqual(h1.root_path, os.path.realpath(self.home.path))
        self.assertEqual(self.svc.home().id, h1.id)

    def test_resolve_or_create_upserts_by_root(self):
        p1 = self.svc.resolve_or_create("alpha")
        p2 = self.svc.resolve_or_create(os.path.join(self.root, "alpha"))
        self.assertEqual(p1.id, p2.id)
        self.assertEqual(p1.slug, "alpha")
        ev = self.events.get_nowait()
        self.assertEqual(ev.type, "project.registered")
        self.assertEqual(ev.project_id, p1.id)

    def test_resolve_bad_ref_raises(self):
        with self.assertRaises(ValueError):
            self.svc.resolve_or_create("/etc")

    def test_register_and_slug_dedupe(self):
        os.mkdir(os.path.join(self.root, "Alpha2"))
        a = self.svc.register(os.path.join(self.root, "alpha"), name="Alpha")
        b = self.svc.register(os.path.join(self.root, "Alpha2"), name="Alpha", default_agent="claude-code")
        self.assertEqual(a.slug, "alpha")
        self.assertEqual(b.slug, "alpha-2")
        self.assertEqual(b.default_agent, "claude-code")

    def test_list_merges_registered_and_discovered(self):
        self.svc.ensure_home()
        self.svc.resolve_or_create("alpha")
        os.mkdir(os.path.join(self.root, "beta"))
        out = self.svc.list()
        by_name = {p["name"]: p for p in out["projects"]}
        self.assertTrue(by_name["alpha"]["registered"])
        self.assertFalse(by_name["beta"]["registered"])
        self.assertEqual(by_name["Yuri"]["kind"], "home")
        self.assertIn(os.path.realpath(self.home.path), out["roots"])


if __name__ == "__main__":
    unittest.main()
```

`backend/tests/test_approval_service.py`:
```python
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.approvals import ApprovalService  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402

PROMPT = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
          "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"],
          "request_id": "req-1"}


class ApprovalServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        self.svc = ApprovalService(self.store, self.bus, Journal(self.home))
        self.sess = AgentSession(project_id="p", agent_id="claude-code", native_session_id="h1",
                                 backend="cli", working_directory="/tmp", mission_id="m1",
                                 name="billing")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _events(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    def test_record_request_is_idempotent_and_classifies_risk(self):
        a1 = self.svc.record_request(self.sess, PROMPT)
        a2 = self.svc.record_request(self.sess, PROMPT)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a1.risk, "dangerous")
        self.assertEqual(a1.mission_id, "m1")
        self.assertEqual(a1.session_id, self.sess.id)
        evs = self._events()
        self.assertEqual([e.type for e in evs], ["approval.requested"])
        self.assertEqual(evs[0].payload["session_name"], "billing")

    def test_new_request_supersedes_stale_pending(self):
        a1 = self.svc.record_request(self.sess, PROMPT)
        a2 = self.svc.record_request(self.sess, {**PROMPT, "request_id": "req-2"})
        self.assertNotEqual(a1.id, a2.id)
        self.assertEqual(self.store.approvals.get(a1.id).status, "superseded")
        self.assertEqual(self.svc.pending(), [a2])

    def test_resolve_by_session_allow_deny_ambiguous(self):
        self.svc.record_request(self.sess, PROMPT)
        with self.assertRaises(ValueError):
            self.svc.resolve_by_session(self.sess, "hmm maybe", by="voice")
        a = self.svc.resolve_by_session(self.sess, "yes go ahead", by="voice")
        self.assertEqual((a.status, a.resolved_by), ("allowed", "voice"))
        self.assertIsNone(self.svc.resolve_by_session(self.sess, "yes", by="voice"))
        ev = [e for e in self._events() if e.type == "approval.resolved"][0]
        self.assertEqual(ev.payload["status"], "allowed")

    def test_resolve_by_id_twice_fails(self):
        a = self.svc.record_request(self.sess, PROMPT)
        self.svc.resolve(a.id, "denied", by="ui")
        with self.assertRaises(ValueError):
            self.svc.resolve(a.id, "allowed", by="ui")
        with self.assertRaises(KeyError):
            self.svc.resolve("nope", "allowed", by="ui")

    def test_journal_line_written_on_resolve(self):
        a = self.svc.record_request(self.sess, PROMPT)
        self.svc.resolve(a.id, "denied", by="ui")
        self.assertIn("denied", Journal(self.home).read_today())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/services/projects.py`**

```python
"""Projects (spec §5.2): registered rows ∪ discovered folders under the allowed
roots. Every root that reaches the store went through session_manager.
resolve_project_path — the sandbox is not re-implemented here."""
from __future__ import annotations

import os

import session_manager
from yuri.domain.event import EventType, YuriEvent
from yuri.domain.project import Project, slugify
from yuri.events.bus import EventBus
from yuri.home import Home
from yuri.store.base import Store

HOME_SLUG = "yuri"


class ProjectService:
    def __init__(self, store: Store, home: Home, bus: EventBus):
        self.store = store
        self.home_dir = home
        self.bus = bus

    def _unique_slug(self, base: str) -> str:
        slug, i = base, 2
        while self.store.projects.get_by_slug(slug) is not None:
            slug = f"{base}-{i}"
            i += 1
        return slug

    def ensure_home(self) -> Project:
        root = os.path.realpath(self.home_dir.path)
        existing = self.store.projects.get_by_root(root)
        if existing:
            return existing
        p = Project(slug=self._unique_slug(HOME_SLUG), name=os.path.basename(root) or "Yuri",
                    root_path=root, kind="home", auto_approve_edits=True)
        self.store.projects.insert(p)
        self.bus.publish(YuriEvent.make(EventType.PROJECT_REGISTERED, project_id=p.id,
                                        payload={"name": p.name, "root_path": root, "kind": "home"}))
        return p

    def home(self) -> Project:
        return self.ensure_home()

    def get(self, project_id: str) -> Project:
        p = self.store.projects.get(project_id)
        if p is None:
            raise KeyError(f"unknown project: {project_id}")
        return p

    def resolve_or_create(self, ref: str) -> Project:
        root = session_manager.resolve_project_path(ref)   # raises ValueError (sandbox)
        return self._upsert(root, None, None)

    def register(self, path: str, name: str | None = None, default_agent: str | None = None) -> Project:
        root = session_manager.resolve_project_path(path)
        return self._upsert(root, name, default_agent)

    def _upsert(self, root: str, name: str | None, default_agent: str | None) -> Project:
        existing = self.store.projects.get_by_root(root)
        if existing:
            changed = False
            if name and existing.name != name:
                existing.name, changed = name, True
            if default_agent and existing.default_agent != default_agent:
                existing.default_agent, changed = default_agent, True
            if changed:
                self.store.projects.update(existing)
            return existing
        name = name or os.path.basename(root) or "project"
        kind = "home" if root == os.path.realpath(self.home_dir.path) else "user"
        p = Project(slug=self._unique_slug(slugify(name)), name=name, root_path=root, kind=kind,
                    default_agent=default_agent, auto_approve_edits=(kind == "home"))
        self.store.projects.insert(p)
        self.bus.publish(YuriEvent.make(EventType.PROJECT_REGISTERED, project_id=p.id,
                                        payload={"name": p.name, "root_path": root, "kind": kind}))
        return p

    def list(self) -> dict:
        discovered = session_manager.list_projects()
        registered = {p.root_path: p for p in self.store.projects.list()}
        out: list[dict] = []
        seen: set[str] = set()
        for p in registered.values():
            seen.add(p.root_path)
            out.append({"name": p.name, "path": p.root_path, "registered": True, "id": p.id,
                        "slug": p.slug, "kind": p.kind, "default_agent": p.default_agent})
        for d in discovered["projects"]:
            real = os.path.realpath(d["path"])
            if real in seen:
                continue
            out.append({"name": d["name"], "path": d["path"], "registered": False})
        out.sort(key=lambda x: x["name"].lower())
        return {"roots": discovered["roots"], "projects": out}
```

- [ ] **Step 4: Write `backend/yuri/services/approvals.py`**

```python
"""Approvals (spec §5.4). The provider decides what it wants to do; Yuri owns
the record and the decision. Fails closed: an ambiguous spoken answer is an
error the caller must re-ask, never an allow."""
from __future__ import annotations

from claude_runner import decide_permission
from yuri.domain.approval import Approval
from yuri.domain.event import EventType, YuriEvent
from yuri.domain.risk import risk_for
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.services.journal import Journal
from yuri.store.base import PendingApprovalExists, Store


class ApprovalService:
    def __init__(self, store: Store, bus: EventBus, journal: Journal):
        self.store = store
        self.bus = bus
        self.journal = journal

    def get(self, approval_id: str) -> Approval:
        a = self.store.approvals.get(approval_id)
        if a is None:
            raise KeyError(f"unknown approval: {approval_id}")
        return a

    def pending(self) -> list[Approval]:
        return self.store.approvals.list(status="pending")

    def list(self, status: str | None = None) -> list[Approval]:
        return self.store.approvals.list(status=status)

    def record_request(self, session: AgentSession, prompt: dict) -> Approval:
        request_id = str(prompt.get("request_id") or "")
        if request_id:
            existing = self.store.approvals.get_by_request(request_id)
            if existing is not None:
                return existing
        tool_name = str(prompt.get("tool_name") or "")
        tool_input = prompt.get("tool_input") or {}
        a = Approval(session_id=session.id, mission_id=session.mission_id, agent_id=session.agent_id,
                     action=tool_name or "action", tool_name=tool_name, tool_input=tool_input,
                     risk=risk_for(tool_name, tool_input), description=str(prompt.get("text") or ""),
                     request_id=request_id or f"{session.id}:{tool_name}:{session.last_activity_at}")
        try:
            self.store.approvals.insert(a)
        except PendingApprovalExists:
            stale = self.store.approvals.pending_for_session(session.id)
            if stale is not None:
                stale.resolve("superseded", "system")
                self.store.approvals.update(stale)
            self.store.approvals.insert(a)
        self.bus.publish(YuriEvent.make(
            EventType.APPROVAL_REQUESTED, mission_id=a.mission_id, session_id=a.session_id,
            agent_id=a.agent_id, payload={"approval_id": a.id, "risk": a.risk,
                                          "tool_name": a.tool_name, "description": a.description,
                                          "session_name": session.name,
                                          "native_session_id": session.native_session_id}))
        return a

    def resolve(self, approval_id: str, decision: str, by: str) -> Approval:
        a = self.get(approval_id)
        if a.status != "pending":
            raise ValueError(f"approval {approval_id[:8]} is already {a.status}")
        if decision not in ("allowed", "denied"):
            raise ValueError(f"decision must be allowed|denied, got {decision!r}")
        a.resolve(decision, by)
        self.store.approvals.update(a)
        self.bus.publish(YuriEvent.make(
            EventType.APPROVAL_RESOLVED, mission_id=a.mission_id, session_id=a.session_id,
            agent_id=a.agent_id, payload={"approval_id": a.id, "status": a.status, "by": by,
                                          "description": a.description, "risk": a.risk}))
        self.journal.append(f"approval {a.status} ({a.risk}) by {by}: {a.description}")
        return a

    def resolve_by_session(self, session: AgentSession, choice: str, by: str) -> Approval | None:
        a = self.store.approvals.pending_for_session(session.id)
        if a is None:
            return None
        decision = decide_permission(choice)
        if decision is None:
            raise ValueError(f"I couldn't tell if {choice!r} means allow or deny — please say allow or deny.")
        return self.resolve(a.id, "allowed" if decision == "allow" else "denied", by)
```

- [ ] **Step 5: Run** both tests → PASS; full suite → `OK`.

---

### Task 15: MissionService

**Files:**
- Create: `backend/yuri/services/missions.py`
- Test: `backend/tests/test_mission_service.py`

**Interfaces — Produces:**
```python
MissionService(store, bus, journal)
  .stop_sessions: Callable[[list[AgentSession]], Awaitable[None]] | None   # set by the container to SessionService.stop_many
  .create(project: Project, title: str, created_by: str, goal: str|None=None, agent_id: str|None=None) -> Mission   # + one step "work"; emits mission.created; journal
  .get(mission_id) -> Mission (KeyError)
  .detail(mission_id) -> dict  {mission, steps, sessions, approvals, events}
  .list(status=None) -> list[Mission]
  .set_status(mission: Mission, to: str, by: str, reason: str|None=None) -> bool   # transition + event + journal; False on same-state
  .set_goal_if_empty(mission, goal) -> None
  async .pause(id, by) / .resume(id, by) / .cancel(id, by) -> Mission    # cancel awaits stop_sessions on live sessions
```

- [ ] **Step 1: Failing test**

```python
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from yuri.domain.mission import InvalidTransition  # noqa: E402
from yuri.domain.project import Project  # noqa: E402
from yuri.domain.session import AgentSession  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402


class MissionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Home(os.path.join(self.tmp.name, "Yuri")).ensure()
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        self.svc = MissionService(self.store, self.bus, Journal(self.home))
        self.project = Project(slug="p", name="P", root_path="/tmp/p")
        self.store.projects.insert(self.project)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _types(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait().type)
        return out

    async def test_create_has_one_step_and_event(self):
        m = self.svc.create(self.project, "Fix billing", created_by="voice", agent_id="claude-code")
        steps = self.store.missions.steps_for(m.id)
        self.assertEqual([(s.ordinal, s.title, s.agent_id, s.status) for s in steps],
                         [(1, "work", "claude-code", "running")])
        self.assertEqual(m.status, "running")
        self.assertEqual(self._types(), ["mission.created"])
        self.assertIn("Fix billing", Journal(self.home).read_today())

    async def test_goal_set_once(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        self.svc.set_goal_if_empty(m, "x" * 600)
        self.svc.set_goal_if_empty(m, "second")
        self.assertEqual(len(self.svc.get(m.id).goal), 500)

    async def test_pause_resume_cancel_and_events(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        self._types()
        m = await self.svc.pause(m.id, by="ui")
        self.assertEqual(m.status, "paused")
        m = await self.svc.resume(m.id, by="ui")
        self.assertEqual(m.status, "running")
        stopped = []

        async def stop_many(sessions):
            stopped.extend(s.id for s in sessions)
        self.svc.stop_sessions = stop_many
        s = AgentSession(project_id=self.project.id, agent_id="a", native_session_id="h",
                         backend="cli", working_directory="/tmp/p", mission_id=m.id, status="running")
        self.store.sessions.insert(s)
        m = await self.svc.cancel(m.id, by="ui")
        self.assertEqual(m.status, "cancelled")
        self.assertEqual(stopped, [s.id])
        self.assertEqual(self._types(), ["mission.status_changed"] * 3)

    async def test_invalid_transition_raises(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        await self.svc.cancel(m.id, by="ui")
        with self.assertRaises(InvalidTransition):
            await self.svc.resume(m.id, by="ui")

    async def test_detail_shape(self):
        m = self.svc.create(self.project, "t", created_by="voice")
        d = self.svc.detail(m.id)
        self.assertEqual(set(d), {"mission", "steps", "sessions", "approvals", "events"})
        self.assertEqual(d["mission"]["id"], m.id)
        with self.assertRaises(KeyError):
            self.svc.detail("nope")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/services/missions.py`**

```python
"""Missions (spec §5.3). In this phase missions are created implicitly by
SessionService.start; explicit start/routing arrives with the orchestrator."""
from __future__ import annotations

from typing import Awaitable, Callable

from yuri.domain.event import EventType, YuriEvent
from yuri.domain.mission import Mission, MissionStep
from yuri.domain.project import Project
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.services.journal import Journal
from yuri.store.base import Store

GOAL_MAX = 500


class MissionService:
    def __init__(self, store: Store, bus: EventBus, journal: Journal):
        self.store = store
        self.bus = bus
        self.journal = journal
        # Injected by the container (SessionService.stop_many) to avoid a cycle.
        self.stop_sessions: Callable[[list[AgentSession]], Awaitable[None]] | None = None

    def get(self, mission_id: str) -> Mission:
        m = self.store.missions.get(mission_id)
        if m is None:
            raise KeyError(f"unknown mission: {mission_id}")
        return m

    def list(self, status: str | None = None) -> list[Mission]:
        return self.store.missions.list(status=status)

    def create(self, project: Project, title: str, created_by: str, goal: str | None = None,
               agent_id: str | None = None) -> Mission:
        m = Mission(title=title, project_id=project.id, goal=(goal or None) and goal[:GOAL_MAX],
                    status="running", created_by=created_by)
        self.store.missions.insert(m)
        step = MissionStep(mission_id=m.id, ordinal=1, title="work", agent_id=agent_id, status="running")
        self.store.missions.insert_step(step)
        m.current_step = step.id
        self.store.missions.update(m)
        self.bus.publish(YuriEvent.make(EventType.MISSION_CREATED, mission_id=m.id,
                                        project_id=project.id, agent_id=agent_id,
                                        payload={"title": title, "goal": m.goal,
                                                 "project": project.name, "created_by": created_by}))
        self.journal.append(f"mission created: {title} ({project.name})")
        return m

    def set_goal_if_empty(self, mission: Mission, goal: str) -> None:
        if mission.goal or not goal:
            return
        mission.goal = " ".join(goal.split())[:GOAL_MAX]
        self.store.missions.update(mission)

    def set_status(self, mission: Mission, to: str, by: str, reason: str | None = None) -> bool:
        frm = mission.status
        if not mission.transition(to):      # raises InvalidTransition on bad edges
            return False
        self.store.missions.update(mission)
        self.bus.publish(YuriEvent.make(EventType.MISSION_STATUS_CHANGED, mission_id=mission.id,
                                        project_id=mission.project_id,
                                        payload={"from": frm, "to": to, "by": by, "reason": reason,
                                                 "title": mission.title}))
        self.journal.append(f"mission '{mission.title}': {frm} → {to}" + (f" ({reason})" if reason else ""))
        return True

    def detail(self, mission_id: str) -> dict:
        m = self.get(mission_id)
        return {"mission": m.to_dict(),
                "steps": [s.to_dict() for s in self.store.missions.steps_for(m.id)],
                "sessions": [s.to_dict() for s in self.store.sessions.list(mission_id=m.id)],
                "approvals": [a.to_dict() for a in self.store.approvals.list(limit=50)
                              if a.mission_id == m.id],
                "events": [e.to_dict() for e in self.store.events.list(mission_id=m.id, limit=50)]}

    async def pause(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        self.set_status(m, "paused", by)
        return m

    async def resume(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        self.set_status(m, "running", by)
        return m

    async def cancel(self, mission_id: str, by: str) -> Mission:
        m = self.get(mission_id)
        live = [s for s in self.store.sessions.list(mission_id=m.id, live_only=True)]
        if live and self.stop_sessions is not None:
            await self.stop_sessions(live)
        self.set_status(m, "cancelled", by)
        return m
```

- [ ] **Step 4: Run** `tests.test_mission_service` → PASS; full suite → `OK`.

---

### Task 16: SessionService

**Files:**
- Create: `backend/yuri/services/sessions.py`
- Test: `backend/tests/test_session_service.py`

**Interfaces — Produces:**
```python
SessionService(store, bus, journal, registry, projects: ProjectService, approvals: ApprovalService, missions: MissionService, default_agent="claude-code")
  # lookup (all accept: Yuri session id | native handle | unique 8+ char prefix | name, case-insensitive)
  .resolve(ref) -> str                     # native handle; KeyError listing names when unknown
  .row_for(handle) -> AgentSession | None
  .list() -> list[dict]                    # today's list_all_sessions shape + agent_id, mission_id, yuri_session_id
  .native_pane(ref) -> str | None
  .default_name_for(cwd) -> str
  # lifecycle
  async .start(project_ref, backend="cli", mode="default", model=None, name=None, created_by="voice", agent_id=None) -> dict
        # {"session_id", "name", "project_path", "backend", "mode", "message", "mission_id", "yuri_session_id"}
  async .adopt(native_id, cwd, name=None) -> dict   # {"session_id","name","cwd","attach","already": bool, "mission_id"}
  .send(ref, message) -> {"status":"working","session_id"}
  .answer(ref, choice) -> {"status":"working","session_id"}
  .poll(ref) -> dict                       # provider result, observed
  async .interrupt(ref) -> {"status":"interrupted","session_id"}
  async .stop(ref) -> {"status":"closed","session_id"}
  async .stop_many(rows: list[AgentSession]) -> None
  async .set_mode(ref, mode) -> dict       # {"session_id","mode", "prompt_resolved"?, "message"?}  (today's texts)
  .rename(ref, name) -> {"session_id","name","message"}
  async .peek(ref, lines=40) -> dict ; async .read(ref) -> {"session_id","text"} ; async .send_keys(ref, items) -> dict
  .run_slash(ref, text) -> {"status":"working","session_id","sent"}
  .handoff_info(ref) -> dict               # today's get_handoff keys
  .on_provider_event(agent_id, handle, ev: ProviderEvent) -> None
  async .rehydrate() -> list[dict]
```
Note on threading: store calls are made inline from async code (local SQLite writes are sub-millisecond; the EventBus writer is the only background persist). Spec §4.4 said `run_in_threadpool`; this plan defers that until profiling shows a need — a one-line wrap at the call sites.

- [ ] **Step 1: Failing test `backend/tests/test_session_service.py`**

```python
import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri.events.bus import EventBus  # noqa: E402
from yuri.home import Home  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402
from yuri.providers.registry import AgentRegistry  # noqa: E402
from yuri.services.approvals import ApprovalService  # noqa: E402
from yuri.services.journal import Journal  # noqa: E402
from yuri.services.missions import MissionService  # noqa: E402
from yuri.services.projects import ProjectService  # noqa: E402
from yuri.services.sessions import SessionService  # noqa: E402
from yuri.store.sqlite import SqliteStore  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"], "request_id": "r1"}


class SessionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "proj"))
        self.home = Home(os.path.join(self.root, "Yuri")).ensure()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", self.home.path)]
        [p.start() for p in self.patches]
        self.store = SqliteStore(self.home.db_path)
        self.store.migrate()
        self.bus = EventBus()
        self.q = self.bus.subscribe()
        self.journal = Journal(self.home)
        self.fake = FakeAgentProvider()
        self.registry = AgentRegistry()
        self.registry.register(self.fake)
        self.projects = ProjectService(self.store, self.home, self.bus)
        self.approvals = ApprovalService(self.store, self.bus, self.journal)
        self.missions = MissionService(self.store, self.bus, self.journal)
        self.svc = SessionService(self.store, self.bus, self.journal, self.registry, self.projects,
                                  self.approvals, self.missions, default_agent="fake")
        self.fake.set_observer(lambda h, ev: self.svc.on_provider_event("fake", h, ev))
        self.missions.stop_sessions = self.svc.stop_many

    def tearDown(self):
        [p.stop() for p in self.patches]
        self.store.close()
        self.tmp.cleanup()

    def _types(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait().type)
        return out

    async def test_start_creates_project_mission_session(self):
        out = await self.svc.start("proj", created_by="voice")
        self.assertEqual(set(out), {"session_id", "name", "project_path", "backend", "mode",
                                    "message", "mission_id", "yuri_session_id"})
        self.assertEqual(out["name"], "proj")
        self.assertEqual(out["project_path"], os.path.join(self.root, "proj"))
        row = self.store.sessions.get(out["yuri_session_id"])
        self.assertEqual((row.native_session_id, row.status, row.agent_id), (out["session_id"], "idle", "fake"))
        m = self.store.missions.get(out["mission_id"])
        self.assertEqual((m.title, m.goal, m.status), ("proj", None, "running"))
        step = self.store.missions.steps_for(m.id)[0]
        self.assertEqual(step.session_id, row.id)
        self.assertEqual(self._types(), ["project.registered", "mission.created", "session.created"])

    async def test_names_dedupe_and_clash_falls_back(self):
        a = await self.svc.start("proj")
        b = await self.svc.start("proj", name="PROJ")   # clash (case-insensitive) → default
        self.assertEqual((a["name"], b["name"]), ("proj", "proj 2"))
        c = await self.svc.start("proj", name="billing")
        self.assertEqual(c["name"], "billing")
        self.assertEqual(self.svc.resolve("BILLING"), c["session_id"])
        self.assertEqual(self.svc.resolve(c["yuri_session_id"]), c["session_id"])

    async def test_resolve_unknown_lists_names(self):
        await self.svc.start("proj", name="alpha")
        with self.assertRaises(KeyError) as cm:
            self.svc.resolve("zzz")
        self.assertIn("alpha", str(cm.exception))

    async def test_send_sets_goal_once_and_poll_observes_permission(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self._types()
        self.assertEqual(self.svc.send(sid, "fix the payment bug"), {"status": "working", "session_id": sid})
        self.svc.send(sid, "second message")
        self.assertEqual(self.store.missions.get(out["mission_id"]).goal, "fix the payment bug")
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        res = self.svc.poll(sid)
        self.assertEqual(res["status"], "needs_permission")
        pend = self.approvals.pending()
        self.assertEqual(len(pend), 1)
        self.assertEqual(pend[0].risk, "dangerous")
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "waiting_for_approval")
        self.assertEqual(self.svc.row_for(sid).status, "needs_permission")
        # second poll of the same prompt must not create a second approval
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.assertEqual(len(self.approvals.pending()), 1)

    async def test_answer_resolves_approval_and_forwards(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.assertEqual(self.svc.answer(sid, "deny"), {"status": "working", "session_id": sid})
        self.assertEqual(self.store.approvals.list(status="denied")[0].resolved_by, "voice")
        self.assertIn(("answer", sid, "deny"), self.fake.calls)
        with self.assertRaises(ValueError):
            self.fake.script(sid, {"status": "needs_permission", "prompt": {**PERM, "request_id": "r2"}})
            self.svc.poll(sid)
            self.svc.answer(sid, "hmm")
        self.svc.answer(sid, "allow")   # clear r2 so no approval is pending
        # a choice prompt (no pending approval) just forwards
        self.svc.answer(sid, "option two")
        self.assertIn(("answer", sid, "option two"), self.fake.calls)

    async def test_completed_poll_returns_mission_to_running(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.fake.script(sid, {"status": "completed", "assistant_text": "done"})
        self.svc.poll(sid)
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "running")
        self.assertEqual(self.svc.row_for(sid).status, "idle")

    async def test_error_fails_mission_when_sole_session(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "error", "error": "boom"})
        self.svc.poll(sid)
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "failed")

    async def test_stop_pauses_mission_not_completes(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.assertEqual(await self.svc.stop(sid), {"status": "closed", "session_id": sid})
        self.assertEqual(self.svc.row_for(sid).status, "stopped")
        self.assertEqual(self.store.missions.get(out["mission_id"]).status, "paused")
        self.assertEqual(self.svc.list(), [])

    async def test_set_mode_resolves_covered_prompt(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.script(sid, {"status": "needs_permission", "prompt": PERM})
        self.svc.poll(sid)
        self.fake.sessions[sid]["prompt"] = PERM   # what list_native shows while parked
        res = await self.svc.set_mode(sid, "acceptEdits")
        self.assertIs(res["prompt_resolved"], False)
        self.assertIn("still", res["message"])
        res = await self.svc.set_mode(sid, "auto")
        self.assertIs(res["prompt_resolved"], True)
        self.assertIn("approved under the new mode", res["message"])
        self.assertEqual(self.store.approvals.list(status="allowed")[0].resolved_by, "mode_switch")
        self.fake.sessions[sid].pop("prompt")
        self.assertEqual(await self.svc.set_mode(sid, "plan"), {"session_id": sid, "mode": "plan"})

    async def test_provider_events_are_mapped(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self._types()
        self.fake.emit(sid, ProviderEvent("tool_started", {"tool_name": "Read", "tool_input": {}}))
        self.fake.emit(sid, ProviderEvent("needs_permission", {**PERM, "request_id": "hook-1"}))
        self.fake.emit(sid, ProviderEvent("turn_completed", {"assistant_text": "ok", "tools_used": ["Read"]}))
        self.fake.emit(sid, ProviderEvent("cost_updated", {"model": "m", "cost_usd": 0.01,
                                                           "input_tokens": None, "output_tokens": None}))
        self.fake.emit(sid, ProviderEvent("error", {"message": "x"}))
        self.fake.emit("unknown-handle", ProviderEvent("error", {"message": "ignored"}))
        types = [t for t in self._types() if t != "mission.status_changed"]
        self.assertEqual(types, ["tool.started", "approval.requested", "session.turn_completed",
                                 "cost.updated", "agent.error"])
        self.assertEqual(self.svc.row_for(sid).runtime_metadata.get("cost_usd"), 0.01)
        self.assertIn("turn completed", self.journal.read_today())

    async def test_rehydrate_marks_lost_and_adopts_unknown(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        self.fake.sessions.pop(sid)                      # simulate: process died
        self.fake.sessions["ghost"] = {"handle": "ghost", "session_id": "ghost",
                                       "cwd": os.path.join(self.root, "proj"), "model": "m",
                                       "mode": "default", "status": "idle", "cost_usd": 0.0,
                                       "queued": 0, "backend": "cli"}
        self._types()
        await self.svc.rehydrate()
        self.assertEqual(self.svc.row_for(sid).status, "lost")
        ghost = self.svc.row_for("ghost")
        self.assertIsNotNone(ghost)
        self.assertIsNone(ghost.mission_id)
        self.assertIn("session.lost", self._types())

    async def test_list_shape(self):
        out = await self.svc.start("proj", name="n")
        s = self.svc.list()[0]
        for k in ["handle", "session_id", "cwd", "model", "mode", "status", "cost_usd", "backend",
                  "name", "agent_id", "mission_id", "yuri_session_id"]:
            self.assertIn(k, s)
        self.assertEqual(s["name"], "n")
        self.assertEqual(self.svc.native_pane(out["session_id"]), f"fake_{out['session_id']}")

    async def test_handoff_info_and_rename(self):
        out = await self.svc.start("proj")
        sid = out["session_id"]
        info = self.svc.handoff_info(sid)
        self.assertEqual(set(info), {"session_id", "name", "cwd", "attach_command", "resume_command", "command"})
        r = self.svc.rename(sid, "Neo")
        self.assertEqual(r["name"], "Neo")
        self.assertEqual(self.svc.row_for(sid).name, "Neo")

    async def test_adopt(self):
        out = await self.svc.adopt("abcdefab-1111-2222-3333-444444444444", os.path.join(self.root, "proj"), name="handed")
        self.assertFalse(out["already"])
        self.assertEqual(out["name"], "handed")
        again = await self.svc.adopt("abcdefab-1111-2222-3333-444444444444", os.path.join(self.root, "proj"))
        self.assertTrue(again["already"])
        self.assertEqual(self.store.missions.get(out["mission_id"]).created_by, "handoff")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/services/sessions.py`**

```python
"""SessionService (spec §5.1) — the seam where the existing voice tools meet
the Yuri domain. Every handler in tools.py that touches a session goes through
here, so mission/session/approval rows and events happen as a side effect of
flows that already exist. Provider calls are forwarded unchanged in shape."""
from __future__ import annotations

import logging
import os
import shlex

from claude_runner import normalize_mode
from permissions import mode_covers
from yuri.domain.event import EventType, YuriEvent
from yuri.domain.mission import InvalidTransition
from yuri.domain.session import AgentSession
from yuri.events.bus import EventBus
from yuri.providers.base import AgentProvider, ProjectContext, ProviderEvent, SessionOptions
from yuri.providers.registry import AgentRegistry
from yuri.services.approvals import ApprovalService
from yuri.services.journal import Journal
from yuri.services.missions import MissionService
from yuri.services.projects import ProjectService
from yuri.store.base import Store

log = logging.getLogger("yuri.sessions")


class SessionService:
    def __init__(self, store: Store, bus: EventBus, journal: Journal, registry: AgentRegistry,
                 projects: ProjectService, approvals: ApprovalService, missions: MissionService,
                 default_agent: str = "claude-code"):
        self.store = store
        self.bus = bus
        self.journal = journal
        self.registry = registry
        self.projects = projects
        self.approvals = approvals
        self.missions = missions
        self.default_agent = default_agent

    # --- lookup ---------------------------------------------------------------

    def _native(self) -> dict[str, tuple[AgentProvider, dict]]:
        out: dict[str, tuple[AgentProvider, dict]] = {}
        for p in self.registry.all():
            for s in p.list_native():
                out[s["handle"]] = (p, s)
        return out

    def _provider_for(self, handle: str) -> AgentProvider:
        row = self.row_for(handle)
        if row is not None:
            try:
                return self.registry.get(row.agent_id)
            except KeyError:
                pass
        for p in self.registry.all():
            if any(s["handle"] == handle for s in p.list_native()):
                return p
        raise KeyError(f"unknown session: {handle}")

    def row_for(self, handle: str) -> AgentSession | None:
        return self.store.sessions.get_by_native(handle)

    def _live_rows(self) -> list[AgentSession]:
        return self.store.sessions.list(live_only=True)

    def resolve(self, ref: str) -> str:
        ref = (ref or "").strip()
        native = self._native()
        if ref in native:
            return ref
        row = self.store.sessions.get(ref) if ref else None
        if row is not None and row.native_session_id in native:
            return row.native_session_id
        low = ref.lower()
        for r in self._live_rows():
            if r.name and r.name.lower() == low and r.native_session_id in native:
                return r.native_session_id
        hits = [h for h in native if ref and h.startswith(ref)]
        if len(hits) == 1:
            return hits[0]
        names = sorted(r.name for r in self._live_rows() if r.name and r.native_session_id in native)
        raise KeyError(f"no session matches '{ref}'. Active session names: {names or '(none named yet)'}.")

    def list(self) -> list[dict]:
        rows = {r.native_session_id: r for r in self._live_rows()}
        out: list[dict] = []
        for handle, (p, s) in self._native().items():
            r = rows.get(handle)
            out.append({**s, "agent_id": p.id, "name": r.name if r else None,
                        "mission_id": r.mission_id if r else None,
                        "yuri_session_id": r.id if r else None})
        return out

    def native_pane(self, ref: str) -> str | None:
        try:
            handle = self.resolve(ref)
        except KeyError:
            return None
        return self._provider_for(handle).native_pane(handle)

    # --- names ------------------------------------------------------------------

    def _taken(self, exclude: str | None = None) -> set[str]:
        return {r.name.lower() for r in self._live_rows() if r.name and r.id != exclude}

    def default_name_for(self, cwd: str) -> str:
        base = os.path.basename(os.path.normpath(cwd)) or "session"
        taken = self._taken()
        if base.lower() not in taken:
            return base
        i = 2
        while f"{base} {i}".lower() in taken:
            i += 1
        return f"{base} {i}"

    def _pick_name(self, requested: str | None, cwd: str) -> str:
        name = " ".join((requested or "").split())
        if name and name.lower() not in self._taken():
            return name
        return self.default_name_for(cwd)

    # --- lifecycle ----------------------------------------------------------------

    async def start(self, project_ref: str, backend: str = "cli", mode: str = "default",
                    model: str | None = None, name: str | None = None, created_by: str = "voice",
                    agent_id: str | None = None) -> dict:
        project = self.projects.resolve_or_create(project_ref)
        agent = self.registry.get(agent_id or project.default_agent or self.default_agent)
        sess_name = self._pick_name(name, project.root_path)
        mission = self.missions.create(project, sess_name, created_by=created_by, agent_id=agent.id)
        mode = normalize_mode(mode)
        try:
            handle = await agent.create_session(ProjectContext(project.id, project.root_path),
                                                SessionOptions(backend=backend, mode=mode, model=model,
                                                               name=sess_name))
        except Exception as exc:
            self.missions.set_status(mission, "failed", by="system", reason=f"{agent.name} unavailable: {exc}")
            self.bus.publish(YuriEvent.make(EventType.AGENT_ERROR, mission_id=mission.id, agent_id=agent.id,
                                            project_id=project.id, payload={"message": str(exc)}))
            raise
        backend_tag = agent.backend_of(handle) or backend
        row = AgentSession(project_id=project.id, agent_id=agent.id, native_session_id=handle,
                           backend=backend_tag, working_directory=project.root_path,
                           mission_id=mission.id, status="idle", name=sess_name, mode=mode, model=model)
        self.store.sessions.insert(row)
        step = self.store.missions.steps_for(mission.id)[0]
        step.session_id = row.id
        self.store.missions.update_step(step)
        self._persist_name(agent, handle, sess_name)
        self.bus.publish(YuriEvent.make(EventType.SESSION_CREATED, mission_id=mission.id, session_id=row.id,
                                        agent_id=agent.id, project_id=project.id,
                                        payload={"name": sess_name, "native_session_id": handle,
                                                 "backend": backend_tag, "mode": mode,
                                                 "cwd": project.root_path}))
        return {"session_id": handle, "name": sess_name, "project_path": project.root_path,
                "backend": backend_tag, "mode": mode,
                "message": f"Started {agent.name} session '{sess_name}' in {project.root_path}.",
                "mission_id": mission.id, "yuri_session_id": row.id}

    async def adopt(self, native_id: str, cwd: str, name: str | None = None) -> dict:
        agent = self.registry.get(self.default_agent)
        pane = agent.native_pane(native_id)
        if pane:
            row = self.row_for(native_id)
            return {"session_id": native_id, "name": row.name if row else None, "cwd": cwd,
                    "attach": f"tmux attach -t {pane}", "already": True,
                    "mission_id": row.mission_id if row else None}
        project = self.projects.resolve_or_create(cwd)
        sess_name = self._pick_name(name, project.root_path)
        mission = self.missions.create(project, sess_name, created_by="handoff", agent_id=agent.id)
        handle = await agent.resume(native_id, ProjectContext(project.id, project.root_path),
                                    SessionOptions(backend="cli", name=sess_name))
        row = AgentSession(project_id=project.id, agent_id=agent.id, native_session_id=handle, backend="cli",
                           working_directory=project.root_path, mission_id=mission.id, status="idle",
                           name=sess_name)
        self.store.sessions.insert(row)
        self._persist_name(agent, handle, sess_name)
        self.bus.publish(YuriEvent.make(EventType.SESSION_CREATED, mission_id=mission.id, session_id=row.id,
                                        agent_id=agent.id, project_id=project.id,
                                        payload={"name": sess_name, "native_session_id": handle,
                                                 "backend": "cli", "adopted": True}))
        pane = agent.native_pane(handle) or f"vc_{handle[:8]}"
        return {"session_id": handle, "name": sess_name, "cwd": project.root_path,
                "attach": f"tmux attach -t {pane}", "already": False, "mission_id": mission.id}

    def _persist_name(self, agent: AgentProvider, handle: str, name: str) -> None:
        persist = getattr(agent, "persist_name", None)
        if persist:
            try:
                persist(handle, name)
            except Exception:
                log.debug("persist_name failed", exc_info=True)

    def _touch(self, row: AgentSession | None, status: str | None = None) -> None:
        if row is None:
            return
        if status:
            row.status = status
        row.touch()
        self.store.sessions.update(row)

    def send(self, ref: str, message: str) -> dict:
        handle = self.resolve(ref)
        row = self.row_for(handle)
        if row is not None and row.mission_id:
            self.missions.set_goal_if_empty(self.missions.get(row.mission_id), message)
        self._provider_for(handle).send_message(handle, message)
        self._touch(row, "running")
        self.bus.publish(self._ev(EventType.SESSION_MESSAGE_SENT, row, handle, {"message": message[:500]}))
        return {"status": "working", "session_id": handle}

    def answer(self, ref: str, choice: str) -> dict:
        handle = self.resolve(ref)
        row = self.row_for(handle)
        if row is not None:
            self.approvals.resolve_by_session(row, choice, by="voice")   # None when it's a choice prompt
        self._provider_for(handle).answer(handle, choice)
        self._touch(row, "running")
        return {"status": "working", "session_id": handle}

    def poll(self, ref: str) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        res = p.poll(handle)
        row = self.row_for(handle)
        if row is None:
            return res
        status = res.get("status")
        emits = not p.capabilities().supports_events   # otherwise the observer already did
        if status == "needs_permission" and res.get("prompt"):
            self.approvals.record_request(row, res["prompt"])
            self._touch(row, "needs_permission")
            self._mission_to(row, "waiting_for_approval", "agent asked for permission")
        elif status == "needs_choice":
            self._touch(row, "needs_choice")
            if emits:
                self.bus.publish(self._ev(EventType.SESSION_QUESTION, row, handle,
                                          {"text": (res.get("prompt") or {}).get("text", "")}))
        elif status == "completed":
            self._touch(row, "idle")
            self._mission_to(row, "running", None)
            if emits:
                self._turn_completed(row, handle, res.get("assistant_text", ""), [])
        elif status == "error":
            self._touch(row, "idle")
            if emits:
                self.bus.publish(self._ev(EventType.AGENT_ERROR, row, handle, {"message": res.get("error", "")}))
            self._fail_if_alone(row, res.get("error") or "agent error")
        elif status == "working":
            self._touch(row, "running")
        return res

    async def interrupt(self, ref: str) -> dict:
        handle = self.resolve(ref)
        await self._provider_for(handle).interrupt(handle)
        row = self.row_for(handle)
        self._touch(row, "idle")
        self.bus.publish(self._ev(EventType.SESSION_INTERRUPTED, row, handle, {}))
        return {"status": "interrupted", "session_id": handle}

    async def stop(self, ref: str) -> dict:
        handle = self.resolve(ref)
        await self._provider_for(handle).stop(handle)
        row = self.row_for(handle)
        self._touch(row, "stopped")
        self.bus.publish(self._ev(EventType.SESSION_STOPPED, row, handle, {}))
        if row is not None and row.mission_id:
            others = [s for s in self.store.sessions.list(mission_id=row.mission_id, live_only=True)]
            if not others:
                self._mission_to(row, "paused", "session closed")
        return {"status": "closed", "session_id": handle}

    async def stop_many(self, rows: list[AgentSession]) -> None:
        for r in rows:
            try:
                await self.stop(r.native_session_id)
            except KeyError:
                self._touch(r, "stopped")

    async def set_mode(self, ref: str, mode: str) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        native = self._native().get(handle, (None, {}))[1]
        prompt = native.get("prompt")           # snapshot BEFORE the switch (runner resolves async)
        new_mode = await p.set_mode(handle, mode)
        row = self.row_for(handle)
        if row is not None:
            row.mode = new_mode
            self._touch(row)
        out: dict = {"session_id": handle, "mode": new_mode}
        if prompt and prompt.get("kind") == "permission":
            if mode_covers(new_mode, prompt.get("tool_name", "")):
                if row is not None:
                    a = self.store.approvals.pending_for_session(row.id)
                    if a is not None:
                        self.approvals.resolve(a.id, "allowed", by="mode_switch")
                out["prompt_resolved"] = True
                out["message"] = (f"Mode is now '{new_mode}'. The pending permission ({prompt['text']}) "
                                  "was approved under the new mode — the session is continuing.")
            else:
                out["prompt_resolved"] = False
                out["message"] = (f"Mode is now '{new_mode}', but the pending permission ({prompt['text']}) "
                                  "is NOT covered by it and still needs an allow/deny from the user.")
        return out

    def rename(self, ref: str, name: str) -> dict:
        handle = self.resolve(ref)
        row = self.row_for(handle)
        clean = " ".join((name or "").split())
        if not clean:
            raise ValueError("name cannot be empty")
        if clean.lower() in self._taken(exclude=row.id if row else None):
            raise ValueError(f"the name '{clean}' is already used by another session; pick a different one")
        if row is not None:
            row.name = clean
            self._touch(row)
            if row.mission_id:
                m = self.missions.get(row.mission_id)
                m.title = clean
                self.store.missions.update(m)
        self._persist_name(self._provider_for(handle), handle, clean)
        return {"session_id": handle, "name": clean, "message": f"Renamed the session to '{clean}'."}

    async def peek(self, ref: str, lines: int = 40) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        screen = await p.peek(handle, lines)
        out: dict = {"session_id": handle, "screen": screen if screen is not None else (await p.read(handle)) or "(no output yet)"}
        if screen is None:
            out["note"] = "This backend has no live screen; showing accumulated text."
        native = self._native().get(handle, (None, {}))[1]
        if native.get("prompt"):
            out["pending_prompt"] = native["prompt"]
            out["note_prompt"] = "This session is waiting on the prompt above — answer it with answer_prompt."
        return out

    async def read(self, ref: str) -> dict:
        handle = self.resolve(ref)
        return {"session_id": handle, "text": await self._provider_for(handle).read(handle)}

    async def send_keys(self, ref: str, items: list[dict]) -> dict:
        handle = self.resolve(ref)
        return await self._provider_for(handle).send_keys(handle, items)

    def run_slash(self, ref: str, text: str) -> dict:
        handle = self.resolve(ref)
        self._provider_for(handle).run_slash(handle, text)
        self._touch(self.row_for(handle), "running")
        return {"status": "working", "session_id": handle, "sent": text}

    def handoff_info(self, ref: str) -> dict:
        handle = self.resolve(ref)
        p = self._provider_for(handle)
        row = self.row_for(handle)
        native = self._native().get(handle, (None, {}))[1]
        cwd = row.working_directory if row else native.get("cwd", "")
        resume = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(handle)}"
        pane = p.native_pane(handle)
        return {"session_id": handle, "name": row.name if row else None, "cwd": cwd,
                "attach_command": f"tmux attach -t {pane}" if pane else None,
                "resume_command": resume, "command": resume}

    # --- provider events → domain -------------------------------------------------

    def on_provider_event(self, agent_id: str, handle: str, ev: ProviderEvent) -> None:
        try:
            row = self.row_for(handle)
            if row is None:
                return
            k, p = ev.kind, ev.payload
            if k == "tool_started":
                self.bus.publish(self._ev(EventType.TOOL_STARTED, row, handle,
                                          {"tool_name": p.get("tool_name"), "tool_input": p.get("tool_input")}))
            elif k == "needs_permission":
                self.approvals.record_request(row, {**p, "kind": "permission"})
                self._touch(row, "needs_permission")
                self._mission_to(row, "waiting_for_approval", "agent asked for permission")
            elif k == "needs_choice":
                self._touch(row, "needs_choice")
                self.bus.publish(self._ev(EventType.SESSION_QUESTION, row, handle,
                                          {"text": p.get("text", ""), "options": p.get("options", [])}))
            elif k == "turn_completed":
                self._touch(row, "idle")
                self._mission_to(row, "running", None)
                self._turn_completed(row, handle, p.get("assistant_text", ""), p.get("tools_used", []))
            elif k == "cost_updated":
                row.runtime_metadata = {**row.runtime_metadata, "cost_usd": p.get("cost_usd"), "model": p.get("model")}
                self._touch(row)
                self.bus.publish(self._ev(EventType.COST_UPDATED, row, handle, dict(p)))
            elif k == "error":
                self.bus.publish(self._ev(EventType.AGENT_ERROR, row, handle, {"message": p.get("message", "")}))
                self._fail_if_alone(row, p.get("message") or "agent error")
        except Exception:
            log.exception("on_provider_event failed (%s %s)", agent_id, ev.kind)

    def _turn_completed(self, row: AgentSession, handle: str, text: str, tools_used: list) -> None:
        self.bus.publish(self._ev(EventType.SESSION_TURN_COMPLETED, row, handle,
                                  {"assistant_text": (text or "")[:2000], "tools_used": list(tools_used or [])}))
        self.journal.append(f"turn completed in '{row.name or handle[:8]}': {' '.join((text or '').split())[:160]}")

    def _mission_to(self, row: AgentSession, to: str, reason: str | None) -> None:
        if not row.mission_id:
            return
        try:
            self.missions.set_status(self.missions.get(row.mission_id), to, by="system", reason=reason)
        except InvalidTransition:
            pass   # e.g. paused mission receiving a late completion — leave it

    def _fail_if_alone(self, row: AgentSession, reason: str) -> None:
        if not row.mission_id:
            return
        others = [s for s in self.store.sessions.list(mission_id=row.mission_id, live_only=True) if s.id != row.id]
        if not others:
            self._mission_to(row, "failed", reason)

    def _ev(self, type: str, row: AgentSession | None, handle: str, payload: dict) -> YuriEvent:
        payload = {**payload, "native_session_id": handle, "session_name": row.name if row else None}
        return YuriEvent.make(type, mission_id=row.mission_id if row else None,
                              session_id=row.id if row else None, agent_id=row.agent_id if row else None,
                              project_id=row.project_id if row else None, payload=payload)

    # --- restart ------------------------------------------------------------------------

    async def rehydrate(self) -> list[dict]:
        restored: list[dict] = []
        for p in self.registry.all():
            try:
                restored.extend(await p.rehydrate())
            except Exception:
                log.exception("rehydrate failed for %s", p.id)
        native = self._native()
        for r in self._live_rows():
            if r.native_session_id not in native:
                r.status = "lost"
                r.touch()
                self.store.sessions.update(r)
                self.bus.publish(self._ev(EventType.SESSION_LOST, r, r.native_session_id, {}))
                self.journal.append(f"session '{r.name or r.native_session_id[:8]}' lost across restart")
        for handle, (p, s) in native.items():
            if self.row_for(handle) is not None:
                continue
            try:
                project = self.projects.resolve_or_create(s.get("cwd", ""))
            except ValueError:
                log.warning("rehydrated session %s has cwd outside allowed roots; not recorded", handle[:8])
                continue
            row = AgentSession(project_id=project.id, agent_id=p.id, native_session_id=handle,
                               backend=s.get("backend", "cli"), working_directory=project.root_path,
                               status="idle", name=s.get("name"), mode=s.get("mode", "default"),
                               model=s.get("model"))
            self.store.sessions.insert(row)
        return restored
```

- [ ] **Step 4: Run** `tests.test_session_service` → PASS. If `test_names_dedupe_and_clash_falls_back` fails on the `FakeAgentProvider.list_native()` not carrying `name`, that is expected — names come from rows, which is the point.

- [ ] **Step 5: Full suite** → `OK`.

---

### Task 17: Container + main.py lifespan + tools.py write-through

**Files:**
- Create: `backend/yuri/app.py`
- Modify: `backend/tools.py` (all session handlers), `backend/main.py` (lifespan, `/session/handoff`, terminal WS), `backend/session_manager.py` (`provider()` prefers the container's)
- Modify tests: `backend/tests/test_tools_dispatch.py`, `backend/tests/test_set_mode_prompt_sync.py`, `backend/tests/test_session_manager.py` (fixtures)
- Test: `backend/tests/test_app_container.py`

**Interfaces — Produces:**
```python
yuri.app:
  @dataclass Container: home, store, bus, registry, journal, memory, projects, approvals, missions, sessions
  build_container(home: Home, registry: AgentRegistry, *, bridge=bridge_to_event_log, default_agent="claude-code") -> Container   # migrates, wires observers + stop_sessions, ensure_home, session_manager.set_provider(claude) when present
  set_container(c | None) ; container() -> Container (RuntimeError if unset) ; container_or_none()
  async startup() -> Container    # default_home().ensure(), build_registry(config.YURI_AGENTS), build_container, bus.start_writer()
  async shutdown()
  test_container(tmp_home_path, provider, default_agent=None) -> Container   # helper for tests: FakeAgentProvider or ClaudeCodeProvider(stub factory); bridge=None
```

- [ ] **Step 1: Failing test `backend/tests/test_app_container.py`**

```python
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import session_manager as sm  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.providers.base import ProviderEvent  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class ContainerTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_container_wires_everything(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            fake = FakeAgentProvider()
            c = yapp.test_container(os.path.join(d, "Yuri"), fake)
            try:
                self.assertIs(yapp.container(), c)
                self.assertEqual(c.projects.home().kind, "home")
                self.assertIs(c.missions.stop_sessions.__self__, c.sessions)
                self.assertTrue(os.path.exists(c.home.db_path))
                # observer wired: a provider event lands on the bus
                q = c.bus.subscribe()
                out = await c.sessions.start("Yuri")
                fake.emit(out["session_id"], ProviderEvent("turn_completed", {"assistant_text": "x", "tools_used": []}))
                types = []
                while not q.empty():
                    types.append(q.get_nowait().type)
                self.assertIn("session.turn_completed", types)
            finally:
                yapp.set_container(None)
                c.store.close()
        with self.assertRaises(RuntimeError):
            yapp.container()

    async def test_startup_and_shutdown(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d, "YURI_AGENTS": "claude-code"}), \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")):
            c = await yapp.startup()
            try:
                self.assertEqual(c.registry.ids(), ["claude-code"])
                self.assertIs(sm.provider(), c.registry.get("claude-code"))
                self.assertTrue(os.path.isdir(c.home.workspace_dir))
            finally:
                await yapp.shutdown()
            self.assertIsNone(yapp.container_or_none())
            self.assertIsNone(sm._provider)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/app.py`**

```python
"""Composition root (spec §5.9). Builds the object graph once at startup;
tools.py and the routes fetch services from container(). Tests build their own
with a temp home and a fake provider via test_container()."""
from __future__ import annotations

import functools
import logging
from dataclasses import dataclass

import config
import session_manager
from yuri.events.bus import EventBus, bridge_to_event_log
from yuri.home import Home, default_home
from yuri.providers.base import AgentProvider
from yuri.providers.registry import AgentRegistry, build_registry
from yuri.services.approvals import ApprovalService
from yuri.services.journal import Journal
from yuri.services.memory import Memory
from yuri.services.missions import MissionService
from yuri.services.projects import ProjectService
from yuri.services.sessions import SessionService
from yuri.store.base import Store
from yuri.store.sqlite import SqliteStore

log = logging.getLogger("yuri.app")


@dataclass
class Container:
    home: Home
    store: Store
    bus: EventBus
    registry: AgentRegistry
    journal: Journal
    memory: Memory
    projects: ProjectService
    approvals: ApprovalService
    missions: MissionService
    sessions: SessionService


_container: Container | None = None


def container() -> Container:
    if _container is None:
        raise RuntimeError("Yuri container not initialised (app startup has not run)")
    return _container


def container_or_none() -> Container | None:
    return _container


def set_container(c: Container | None) -> None:
    global _container
    _container = c


def build_container(home: Home, registry: AgentRegistry, *, bridge=bridge_to_event_log,
                    default_agent: str = "claude-code") -> Container:
    home.ensure()
    store = SqliteStore(home.db_path)
    store.migrate()
    bus = EventBus(repo=store.events, bridge=bridge)
    journal = Journal(home)
    memory = Memory(home)
    projects = ProjectService(store, home, bus)
    approvals = ApprovalService(store, bus, journal)
    missions = MissionService(store, bus, journal)
    sessions = SessionService(store, bus, journal, registry, projects, approvals, missions,
                              default_agent=default_agent)
    missions.stop_sessions = sessions.stop_many
    for p in registry.all():
        p.set_observer(functools.partial(sessions.on_provider_event, p.id))
    projects.ensure_home()
    try:
        session_manager.set_provider(registry.get("claude-code"))   # shims share the instance
    except KeyError:
        pass
    c = Container(home, store, bus, registry, journal, memory, projects, approvals, missions, sessions)
    set_container(c)
    return c


async def startup() -> Container:
    home = default_home().ensure()
    registry = build_registry(config.YURI_AGENTS)
    c = build_container(home, registry)
    c.bus.start_writer()
    log.info("yuri: home=%s db=%s agents=%s", home.path, home.db_path, registry.ids())
    return c


async def shutdown() -> None:
    c = _container
    if c is None:
        return
    try:
        await c.bus.stop_writer()
        await c.registry.shutdown()
    finally:
        c.store.close()
        set_container(None)
        session_manager.set_provider(None)


def test_container(home_path: str, provider: AgentProvider, default_agent: str | None = None) -> Container:
    reg = AgentRegistry()
    reg.register(provider)
    return build_container(Home(home_path), reg, bridge=None, default_agent=default_agent or provider.id)
```

- [ ] **Step 4: Rewrite the session handlers in `backend/tools.py`**

Replace the `from session_manager import (...)` block with:

```python
from yuri.app import container
```
(`mode_covers` is no longer used in `tools.py` — the logic lives in `SessionService.set_mode`; remove its import.)

and replace the body of `_require_session` and `dispatch_tool` handlers as follows (keep `TOOL_DEFINITIONS`, the duplicate guard and `mute` untouched):

```python
def _svc():
    return container().sessions


def _require_session(args: dict[str, Any], action: str) -> str:
    """(docstring unchanged)"""
    ref = (args.get("session_id") or "").strip()
    svc = _svc()
    if not ref:
        sessions = svc.list()
        if len(sessions) == 1:
            return sessions[0]["handle"]
        if not sessions:
            raise ValueError(f"there are no active sessions to {action}. Start one first.")
        names = ", ".join(s.get("name") or s["handle"][:8] for s in sessions)
        raise ValueError(
            f"which session should I {action}? {len(sessions)} are open: {names}. "
            "Ask the user which one, then pass its session_id.")
    try:
        return svc.resolve(ref)
    except KeyError as exc:
        raise ValueError(str(exc)) from exc
```

Handlers inside `dispatch_tool`:

```python
    if name == "list_projects":
        return container().projects.list()

    if name == "list_sessions":
        return {"sessions": _svc().list()}

    if name == "start_session":
        # (duplicate-guard block unchanged up to `backend = ...`)
        backend = args.get("backend") or "cli"
        mode = args.get("mode") or "default"
        _last_start = {"ts": now, "handle": "", "name": "(starting…)"}
        try:
            out = await _svc().start(args.get("project_path", ""), backend=backend, mode=mode,
                                     model=args.get("model"), name=args.get("name"), created_by="voice")
        except BaseException:
            _last_start = recent
            raise
        _last_start = {"ts": time.monotonic(), "handle": out["session_id"], "name": out["name"]}
        return out

    if name == "rename_session":
        return _svc().rename(_require_session(args, "rename"), args["name"])

    if name == "set_mode":
        return await _svc().set_mode(_require_session(args, "change the mode for"), args["mode"])

    if name == "list_slash_commands":
        cwd: str | None = None
        sid_arg = args.get("session_id")
        if sid_arg:
            try:
                sid = _svc().resolve(sid_arg)
                sess = next((s for s in _svc().list() if s["handle"] == sid), None)
                if sess:
                    cwd = sess["cwd"]
            except KeyError:
                pass
        return {"commands": list_slash_commands(cwd)}

    if name == "run_slash_command":
        sid = _require_session(args, "run that command in")
        cmd = str(args.get("command", "")).strip().lstrip("/")
        if not cmd:
            raise ValueError("command is required (e.g. 'init' or '/init')")
        extra = (args.get("args") or "").strip()
        text = f"/{cmd}" + (f" {extra}" if extra else "")
        try:
            return _svc().run_slash(sid, text)
        except NotImplementedError:
            return {"ok": False, "error": "slash commands run in the interactive CLI; this session uses the SDK backend."}

    if name == "tell_claude":
        return _svc().send(_require_session(args, "send that to"), args["message"])

    if name == "answer_prompt":
        return _svc().answer(_require_session(args, "answer for"), args["choice"])

    if name == "poll_session":
        return _svc().poll(_svc().resolve(args["session_id"]))

    if name == "read_transcript":
        from transcript import read_timeline
        return read_timeline(_svc().resolve(args["session_id"]))

    if name == "interrupt_session":
        return await _svc().interrupt(_require_session(args, "interrupt"))

    if name == "close_session":
        return await _svc().stop(_require_session(args, "close"))

    if name == "peek_screen":
        return await _svc().peek(_require_session(args, "peek at"))

    if name == "read_session":
        return await _svc().read(_require_session(args, "read"))

    if name == "get_handoff":
        return _svc().handoff_info(_require_session(args, "hand off"))

    if name == "send_keys":
        sid = _require_session(args, "send keys to")
        items = args.get("items") or []
        if not isinstance(items, list) or not items:
            raise ValueError("items is required (a non-empty list of {key} or {text} objects)")
        try:
            return await _svc().send_keys(sid, items)
        except NotImplementedError:
            return {"ok": False, "error": "send_keys controls the interactive CLI; this session uses the SDK backend."}
```

`poll_session` in `tools.py` used `resolve_session(args["session_id"])` raising `KeyError` → the endpoint maps to 404; `svc.resolve` also raises `KeyError`, so that is preserved.

- [ ] **Step 5: `backend/main.py` changes**

Imports: replace the `from session_manager import (...)` block with `from yuri import app as yuri_app` and `from yuri.api.routes import build_router` (the router module is Task 18 — for this task, import only `yuri_app`; add the router import in Task 18).

Lifespan body:
```python
    log.info("config: %s", config.summary())
    if not config.voice_keys_found():
        log.warning(...)  # unchanged
    c = await yuri_app.startup()
    event_log.start_writer()
    try:
        restored = await c.sessions.rehydrate()
        if restored:
            log.info("rehydrated %d CLI session(s): %s", len(restored),
                     [s.get("name") or s["handle"][:8] for s in restored])
    except Exception:
        log.exception("CLI session rehydration failed (continuing without it)")
    yield
    await event_log.stop_writer()
    await yuri_app.shutdown()
```

`/session/handoff` body after the `validate_session_id` check:
```python
    try:
        out = await yuri_app.container().sessions.adopt(sid, req.cwd, req.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    attach = out["attach"]
    if out["already"]:
        return {"session_id": out["session_id"], "name": out["name"], "attach": attach,
                "message": f"Voice is live on this session. Keep typing here, or attach "
                           f"another terminal with: {attach}"}
    return {"session_id": out["session_id"], "name": out["name"], "cwd": out["cwd"], "attach": attach,
            "message": f"Reopened '{out['name']}' under yapcode. Exit your old session "
                       f"(Ctrl-D), then run: {attach}"}
```
Keep `from tmux_runner import validate_session_id`.

Terminal WS: `pane = cli_pane_for(handle)` → `pane = yuri_app.container().sessions.native_pane(handle)`.

- [ ] **Step 6: `backend/session_manager.py`** — `provider()` now prefers the container's instance:

```python
def provider() -> ClaudeCodeProvider:
    global _provider
    if _provider is None:
        _provider = ClaudeCodeProvider()
    return _provider
```
stays, but `build_container` already calls `set_provider(...)`, and `shutdown()` resets it. Mark `set_session_name`, `default_name_for`, `resolve_session`, `list_all_sessions`, `close_session`, `peek_session`, `set_session_mode`, `rehydrate_cli_sessions`, `shutdown_all` with a one-line docstring note: `"""DEPRECATED shim — SessionService owns this now; kept for callers not yet migrated."""`. Do not delete them.

- [ ] **Step 7: Re-base the three test fixtures on the container**

`test_tools_dispatch.py` `setUp` becomes:
```python
        self.runner = _StubRunner()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.mkdir(os.path.join(self.root, "proj"))
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.root}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.root, "Yuri"))]
        for p in self.patches:
            p.start()
        from yuri.providers.claude_code import ClaudeCodeProvider
        self.c = yapp.test_container(os.path.join(self.root, "Yuri"),
                                     ClaudeCodeProvider(runner_factory=lambda b: self.runner),
                                     default_agent="claude-code")
        tools._last_start = None
```
with `import config` and `from yuri import app as yapp` added; `tearDown` → `yapp.set_container(None); self.c.store.close(); sm.set_provider(None); ...`. Update two assertions that legitimately changed: `test_start_session_keys_and_default_name` expects the key set `{"session_id","name","project_path","backend","mode","message","mission_id","yuri_session_id"}`, and `test_list_sessions_keys` adds `"agent_id", "mission_id", "yuri_session_id"` to the required keys. Everything else stays byte-identical — that is the regression check.

`test_set_mode_prompt_sync.py` — replace `_patch_session` and the class with a container-backed version:
```python
import config
from yuri import app as yapp
from yuri.providers.fake import FakeAgentProvider

PERM = lambda tool: {"kind": "permission", "text": f"run {tool}", "tool_name": tool,
                     "tool_input": {}, "options": ["allow", "deny"], "request_id": "r1"}


class SetModePromptSync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)
        self.sid = (await self.c.sessions.start("Yuri", name="demo"))["session_id"]

    async def asyncTearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    async def _set_mode(self, prompt, mode):
        if prompt:
            self.fake.script(self.sid, {"status": "needs_permission" if prompt["kind"] == "permission" else "needs_choice", "prompt": prompt})
            self.c.sessions.poll(self.sid)
            self.fake.sessions[self.sid]["prompt"] = prompt
        return await tools.dispatch_tool("set_mode", {"session_id": "demo", "mode": mode})
```
The five test methods keep their bodies and assertions unchanged (`import tempfile`, `os`, `mock` as needed).

`test_session_manager.py` `ResolveSession` — this class tests the deprecated shims; keep it exactly as re-based in Task 8 (provider injected via `sm.set_provider`). No change.

- [ ] **Step 8: Run full suite** → `OK`. Then boot the backend (`cd backend && ./run.sh`) and confirm the log shows `yuri: home=… db=… agents=['claude-code']` and no tracebacks; `curl -s localhost:8000/health` → `{"status":"ok"}`. Stop it.

---

### Task 18: REST API + `/yuri/context` + Next proxy

**Files:**
- Create: `backend/yuri/api/__init__.py`, `backend/yuri/api/schemas.py`, `backend/yuri/api/routes.py`
- Modify: `backend/main.py` (`include_router`)
- Create: `frontend/app/api/yuri/[...path]/route.ts`
- Test: `backend/tests/test_yuri_api.py`

**Interfaces — Produces:** `build_router(require_auth: Callable) -> APIRouter` mounted at `/yuri`; endpoints per spec §5.8; `GET /yuri/context` → `{home, memory_user, journal_today, active_missions:[{id,title,goal,status,project}], agents:[{id,name,online,version,detail}]}`.

- [ ] **Step 1: Failing test `backend/tests/test_yuri_api.py`**

```python
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.api.routes import build_router  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402

PERM = {"kind": "permission", "text": "run rm -rf build", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf build"}, "options": ["allow", "deny"], "request_id": "r1"}


class YuriApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.fake = FakeAgentProvider()
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), self.fake)
        self.denied = False

        async def guard():
            if self.denied:
                raise HTTPException(status_code=401, detail="nope")
        app = FastAPI()
        app.include_router(build_router(guard))
        self.client = TestClient(app)

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def _start(self):
        import asyncio
        return asyncio.run(self.c.sessions.start("proj", name="s1"))

    def test_auth_dependency_applies_to_every_route(self):
        self.denied = True
        for path in ["/yuri/context", "/yuri/projects", "/yuri/agents", "/yuri/missions", "/yuri/sessions",
                     "/yuri/approvals", "/yuri/events"]:
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_projects(self):
        r = self.client.get("/yuri/projects")
        self.assertEqual(r.status_code, 200)
        names = [p["name"] for p in r.json()["projects"]]
        self.assertIn("proj", names)
        r = self.client.post("/yuri/projects", json={"path": "proj", "default_agent": "fake"})
        self.assertEqual(r.status_code, 201)
        pid = r.json()["id"]
        self.assertEqual(self.client.get(f"/yuri/projects/{pid}").json()["default_agent"], "fake")
        self.assertEqual(self.client.post("/yuri/projects", json={"path": "/etc"}).status_code, 400)
        self.assertEqual(self.client.get("/yuri/projects/nope").status_code, 404)

    def test_agents_and_health(self):
        r = self.client.get("/yuri/agents")
        self.assertEqual(r.status_code, 200)
        a = r.json()["agents"][0]
        self.assertEqual((a["id"], a["online"]), ("fake", True))
        self.assertIn("capabilities", a)
        self.assertEqual(self.client.get("/yuri/agents/fake/health").json()["online"], True)
        self.assertEqual(self.client.get("/yuri/agents/nope/health").status_code, 404)

    def test_missions_flow(self):
        out = self._start()
        r = self.client.get("/yuri/missions")
        self.assertEqual([m["id"] for m in r.json()["missions"]], [out["mission_id"]])
        self.assertEqual(self.client.get("/yuri/missions?status=paused").json()["missions"], [])
        d = self.client.get(f"/yuri/missions/{out['mission_id']}").json()
        self.assertEqual(set(d), {"mission", "steps", "sessions", "approvals", "events"})
        mid = out["mission_id"]
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/pause").json()["status"], "paused")
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/resume").json()["status"], "running")
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/cancel").json()["status"], "cancelled")
        self.assertEqual(self.client.post(f"/yuri/missions/{mid}/resume").status_code, 409)
        self.assertEqual(self.client.get("/yuri/sessions").json()["sessions"], [])  # cancel stopped it

    def test_sessions_and_interrupt(self):
        out = self._start()
        r = self.client.get("/yuri/sessions").json()["sessions"]
        self.assertEqual(r[0]["yuri_session_id"], out["yuri_session_id"])
        row = self.client.get(f"/yuri/sessions/{out['yuri_session_id']}").json()
        self.assertEqual(row["native_session_id"], out["session_id"])
        r = self.client.post(f"/yuri/sessions/{out['yuri_session_id']}/interrupt")
        self.assertEqual(r.json()["status"], "interrupted")
        self.assertEqual(self.client.get("/yuri/sessions/nope").status_code, 404)

    def test_approvals(self):
        out = self._start()
        self.fake.script(out["session_id"], {"status": "needs_permission", "prompt": PERM})
        self.c.sessions.poll(out["session_id"])
        pend = self.client.get("/yuri/approvals?status=pending").json()["approvals"]
        self.assertEqual(len(pend), 1)
        aid = pend[0]["id"]
        r = self.client.post(f"/yuri/approvals/{aid}/deny")
        self.assertEqual(r.json()["status"], "denied")
        self.assertIn(("answer", out["session_id"], "deny"), self.fake.calls)
        self.assertEqual(self.client.post(f"/yuri/approvals/{aid}/approve").status_code, 409)

    def test_events_and_context(self):
        out = self._start()
        # events persist via the bus writer, which needs a running loop; the list endpoint
        # reads the repo, so insert one directly to prove the read path.
        from yuri.domain.event import EventType, YuriEvent
        self.c.store.events.insert(YuriEvent.make(EventType.TOOL_STARTED, mission_id=out["mission_id"]))
        evs = self.client.get(f"/yuri/events?mission_id={out['mission_id']}").json()["events"]
        self.assertEqual(evs[-1]["type"], "tool.started")
        self.c.memory.remember("likes tea")
        ctx = self.client.get("/yuri/context").json()
        self.assertEqual(set(ctx), {"home", "memory_user", "journal_today", "active_missions", "agents"})
        self.assertIn("likes tea", ctx["memory_user"])
        self.assertEqual(ctx["active_missions"][0]["title"], "s1")
        self.assertEqual(ctx["agents"][0]["id"], "fake")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/api/__init__.py`** (empty), **`schemas.py`**, **`routes.py`**

`backend/yuri/api/schemas.py`:
```python
"""Request bodies for the /yuri routes. Responses are the domain dataclasses'
to_dict() output — one shape everywhere (UI, CLI, tests)."""
from __future__ import annotations

from pydantic import BaseModel


class ProjectCreate(BaseModel):
    path: str
    name: str | None = None
    default_agent: str | None = None
```

`backend/yuri/api/routes.py`:
```python
"""HTTP surface of the Yuri domain (spec §5.8). Routes validate, call a
service, and shape the response — no orchestration here. Built by
build_router(require_auth) so main.py's auth dependency applies to every route
without a circular import."""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from yuri.app import container
from yuri.domain.mission import InvalidTransition
from .schemas import ProjectCreate

ACTIVE = ("running", "waiting_for_approval", "paused", "queued")


def _by(request: Request) -> str:
    return "ui" if request.headers.get("origin") else "api"


def build_router(require_auth: Callable) -> APIRouter:
    r = APIRouter(prefix="/yuri", dependencies=[Depends(require_auth)])

    # --- context ------------------------------------------------------------
    @r.get("/context")
    async def context():
        c = container()
        health = await c.registry.health_all()
        missions = [m for m in c.missions.list() if m.status in ACTIVE][:20]
        projects = {p.id: p.name for p in c.store.projects.list()}
        return {"home": c.home.path,
                "memory_user": c.memory.read_user(),
                "journal_today": c.journal.read_today(),
                "active_missions": [{"id": m.id, "title": m.title, "goal": m.goal, "status": m.status,
                                     "project": projects.get(m.project_id)} for m in missions],
                "agents": [{"id": p.id, "name": p.name, **health[p.id].to_dict()} for p in c.registry.all()]}

    # --- projects -----------------------------------------------------------
    @r.get("/projects")
    async def list_projects():
        return container().projects.list()

    @r.post("/projects", status_code=201)
    async def create_project(body: ProjectCreate):
        try:
            return container().projects.register(body.path, body.name, body.default_agent).to_dict()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @r.get("/projects/{project_id}")
    async def get_project(project_id: str):
        try:
            return container().projects.get(project_id).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- agents -------------------------------------------------------------
    @r.get("/agents")
    async def list_agents():
        c = container()
        health = await c.registry.health_all()
        live = c.store.sessions.list(live_only=True)
        return {"agents": [{"id": p.id, "name": p.name, **health[p.id].to_dict(),
                            "capabilities": p.capabilities().to_dict(),
                            "active_sessions": sum(1 for s in live if s.agent_id == p.id)}
                           for p in c.registry.all()]}

    @r.get("/agents/{agent_id}/health")
    async def agent_health(agent_id: str):
        try:
            p = container().registry.get(agent_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return (await p.health()).to_dict()

    # --- missions -----------------------------------------------------------
    @r.get("/missions")
    async def list_missions(status: str | None = None):
        return {"missions": [m.to_dict() for m in container().missions.list(status=status)]}

    @r.get("/missions/{mission_id}")
    async def get_mission(mission_id: str):
        try:
            return container().missions.detail(mission_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    async def _transition(mission_id: str, action: str, request: Request):
        c = container()
        try:
            fn = getattr(c.missions, action)
            return (await fn(mission_id, by=_by(request))).to_dict()
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except InvalidTransition as exc:
            raise HTTPException(409, str(exc)) from exc

    @r.post("/missions/{mission_id}/pause")
    async def pause(mission_id: str, request: Request):
        return await _transition(mission_id, "pause", request)

    @r.post("/missions/{mission_id}/resume")
    async def resume(mission_id: str, request: Request):
        return await _transition(mission_id, "resume", request)

    @r.post("/missions/{mission_id}/cancel")
    async def cancel(mission_id: str, request: Request):
        return await _transition(mission_id, "cancel", request)

    # --- sessions -----------------------------------------------------------
    @r.get("/sessions")
    async def list_sessions():
        return {"sessions": container().sessions.list()}

    @r.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        row = container().store.sessions.get(session_id)
        if row is None:
            raise HTTPException(404, f"unknown session: {session_id}")
        return row.to_dict()

    @r.post("/sessions/{session_id}/interrupt")
    async def interrupt(session_id: str):
        try:
            return await container().sessions.interrupt(session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- approvals ----------------------------------------------------------
    @r.get("/approvals")
    async def list_approvals(status: str | None = None):
        return {"approvals": [a.to_dict() for a in container().approvals.list(status=status)]}

    async def _decide(approval_id: str, decision: str, request: Request):
        c = container()
        try:
            a = c.approvals.resolve(approval_id, decision, by=_by(request))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        row = c.store.sessions.get(a.session_id)
        if row is not None and row.is_live:
            try:
                c.registry.get(row.agent_id).answer(row.native_session_id,
                                                    "allow" if decision == "allowed" else "deny")
            except Exception as exc:   # the decision is recorded even if the agent is gone
                return {**a.to_dict(), "forwarded": False, "error": str(exc)}
        return {**a.to_dict(), "forwarded": True}

    @r.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, request: Request):
        return await _decide(approval_id, "allowed", request)

    @r.post("/approvals/{approval_id}/deny")
    async def deny(approval_id: str, request: Request):
        return await _decide(approval_id, "denied", request)

    # --- events -------------------------------------------------------------
    @r.get("/events")
    async def list_events(mission_id: str | None = None, session_id: str | None = None,
                          since: str | None = None, limit: int = 200):
        evs = container().store.events.list(mission_id=mission_id, session_id=session_id, since=since,
                                            limit=max(1, min(limit, 1000)))
        return {"events": [e.to_dict() for e in evs]}

    @r.get("/events/stream")
    async def stream_events(mission_id: str | None = None, limit: int = 200):
        c = container()

        async def gen():
            q = c.bus.subscribe()
            try:
                for e in c.store.events.list(mission_id=mission_id, limit=limit):
                    yield f"data: {json.dumps(e.to_dict(), default=str)}\n\n"
                while True:
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=15.0)
                        if mission_id and e.mission_id != mission_id:
                            continue
                        yield f"data: {json.dumps(e.to_dict(), default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                c.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                          "X-Accel-Buffering": "no"})

    return r
```

- [ ] **Step 4: Mount in `backend/main.py`** — after the CORS middleware block:

```python
from yuri.api.routes import build_router  # noqa: E402  (after `app` exists is fine; keep with other imports)
app.include_router(build_router(require_auth))
```
(`require_auth` is defined above the routes in `main.py`; place the `include_router` line right after `require_auth`'s definition.)

- [ ] **Step 5: Next proxy `frontend/app/api/yuri/[...path]/route.ts`**

```ts
import { NextRequest, NextResponse } from "next/server";
import { forwardAuth, blockCrossSite } from "@/lib/proxyAuth";

const BACKEND = process.env.BACKEND_URL || "http://localhost:8000";

// Same-origin proxy for the Yuri control API (/yuri/*). Mirrors app/api/tools:
// no secret of its own, forwards the browser's token, rejects cross-site calls.
// The SSE stream (/yuri/events/stream) is browser-direct like /debug/stream.
async function proxy(req: NextRequest, path: string[]) {
  const blocked = blockCrossSite(req);
  if (blocked) return blocked;
  const qs = req.nextUrl.search || "";
  const init: RequestInit = { method: req.method, headers: forwardAuth(req, {}), cache: "no-store" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = await req.text();
  }
  const resp = await fetch(`${BACKEND}/yuri/${path.map(encodeURIComponent).join("/")}${qs}`, init);
  const text = await resp.text();
  return new NextResponse(text, { status: resp.status, headers: { "Content-Type": "application/json" } });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}

export async function POST(req: NextRequest, ctx: Ctx) {
  return proxy(req, (await ctx.params).path);
}
```

- [ ] **Step 6: Run** `tests.test_yuri_api` → PASS; full suite → `OK`; `cd frontend && npx tsc --noEmit` → no errors; `curl -s localhost:8000/yuri/context` with the backend running → JSON with the five keys.

---

### Task 19: `remember` tool, persona/operating split, frontend context injection

**Files:**
- Modify: `backend/tools.py` (definition + handler)
- Create: `frontend/lib/persona.ts`, `frontend/lib/operating.ts`, `frontend/lib/instructions.test.ts`
- Modify: `frontend/lib/instructions.ts`, `frontend/components/VoiceAgent.tsx` (connect path)
- Test: `backend/tests/test_remember_tool.py`

- [ ] **Step 1: Failing test `backend/tests/test_remember_tool.py`**

```python
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import tools  # noqa: E402
from yuri import app as yapp  # noqa: E402
from yuri.providers.fake import FakeAgentProvider  # noqa: E402


class RememberTool(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.mkdir(os.path.join(self.tmp.name, "proj"))
        self.patches = [mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": self.tmp.name}),
                        mock.patch.object(config, "YURI_HOME", os.path.join(self.tmp.name, "Yuri"))]
        [p.start() for p in self.patches]
        self.c = yapp.test_container(os.path.join(self.tmp.name, "Yuri"), FakeAgentProvider())
        self.q = self.c.bus.subscribe()

    def tearDown(self):
        yapp.set_container(None)
        self.c.store.close()
        [p.stop() for p in self.patches]
        self.tmp.cleanup()

    def test_definition(self):
        d = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "remember")
        self.assertEqual(d["parameters"]["required"], ["fact"])
        self.assertIn("project", d["parameters"]["properties"])

    async def test_remember_user_fact(self):
        out = await tools.dispatch_tool("remember", {"fact": "prefers dark mode"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["path"], self.c.home.user_memory_path)
        self.assertIn("prefers dark mode", self.c.memory.read_user())
        self.assertEqual(self.q.get_nowait().type, "memory.remembered")
        self.assertIn("remembered", self.c.journal.read_today())

    async def test_remember_project_fact_resolves_folder(self):
        out = await tools.dispatch_tool("remember", {"fact": "uses uv", "project": "proj"})
        self.assertTrue(out["path"].endswith(os.path.join("memory", "projects", "proj.md")))
        self.assertIn("uses uv", self.c.memory.read_project("proj"))

    async def test_bad_project_is_soft_error(self):
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("remember", {"fact": "x", "project": "/etc"})
        with self.assertRaises(ValueError):
            await tools.dispatch_tool("remember", {"fact": "   "})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Add to `TOOL_DEFINITIONS` in `backend/tools.py`** (after `mute`):

```python
    {
        "type": "function",
        "name": "remember",
        "description": "Store a durable fact in Yuri's memory (~/Yuri/memory). Use it when the user states a preference, corrects you, or says 'remember this'. Pass project to file it under that project's notes instead of the user's.",
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "One sentence, in the user's terms."},
                "project": {"type": "string", "description": "Optional project folder name the fact is about."},
            },
            "required": ["fact"],
        },
    },
```

and the handler in `dispatch_tool` (before `raise KeyError`):

```python
    if name == "remember":
        c = container()
        slug = None
        project = (args.get("project") or "").strip()
        if project:
            slug = c.projects.resolve_or_create(project).slug     # ValueError → soft error
        path = c.memory.remember(args.get("fact", ""), project_slug=slug)
        from yuri.domain.event import EventType, YuriEvent
        c.bus.publish(YuriEvent.make(EventType.MEMORY_REMEMBERED, payload={"fact": args.get("fact", ""),
                                                                            "project": slug}))
        c.journal.append(f"remembered{' for ' + slug if slug else ''}: {args.get('fact', '')}")
        return {"ok": True, "path": path,
                "message": "Remembered." if not slug else f"Noted under {slug}."}
```

- [ ] **Step 4: Run** `tests.test_remember_tool` → PASS.

- [ ] **Step 5: Frontend prompt split**

`frontend/lib/persona.ts`:
```ts
// Who Yuri is. Operating rules (how to drive the agents) live in operating.ts;
// this file is identity and voice only. Plan §37/§38.
export const PERSONA = `You are Yuri — a personal AI companion who lives on this computer and runs the user's coding agents for them. You are calm, concise, proactive and technically competent. You speak in the first person as Yuri (she/her). You are the operator, not the coder: agents such as Claude Code do the actual work in the user's projects, and you direct them, watch them, and report back.

YOUR HOME: ~/Yuri. It holds your memory (memory/user.md and memory/projects/), your daily journal (journal/), and a workspace/ folder that is yours to use. You may start an agent session in ~/Yuri like any other project. Anything you learn that should outlast this conversation goes into memory via the remember tool — preferences, corrections, facts about the user's projects. Don't ask permission to remember ordinary preferences; do it and say so briefly.

WHAT YOU CAN REACH THROUGH AGENTS: files, shell, git, tests, the web, a real Chrome browser, whole multi-step engineering tasks. If a request involves the user's computer, code, or browser, route it to an agent instead of explaining limitations. Never say "I can't" when an agent can.

HONESTY RULES (non-negotiable):
- Distinguish three things: what an agent SAID, what it actually DID, and what you VERIFIED. Report them as such ("Claude says the tests pass" vs "the test command exited 0").
- Never report work as done until a result has actually come back. "It's on it" is fine; "it's fixed" is not, until it is.
- Report failures plainly and offer the next step. Ask for approval when an action is risky; say what the action is in plain words.
- Keep spoken replies short. Summarize; don't recite code.`;
```

`frontend/lib/operating.ts`: move the *entire* body of today's `INSTRUCTIONS` template literal (from "HOW TO OPERATE:" to the end) into `export const OPERATING = \`HOW TO OPERATE:\n...\`;` unchanged, **except**: delete the first two paragraphs ("You are the VOICE for Claude Code…" and "WHAT CLAUDE CAN DO…") and the "YOUR MINDSET" block, since PERSONA replaces them; in the retained text replace the phrase "Started Claude session" → "Started an agent session" if it occurs, and add one bullet at the end of the list:
```
- MEMORY: when the user states a preference ("I prefer pnpm"), corrects you, or says "remember this", call remember with a one-sentence fact (add project when it's about a specific project). Acknowledge briefly ("Noted.").
```
Everything else (tool names like tell_claude/answer_prompt, the duplicate-start rule, names, slash commands, modes, co-driving, send_keys, mute) stays verbatim — those lines are load-bearing for the voice model.

`frontend/lib/instructions.ts`:
```ts
// .ts extensions: `node --test` resolves relative imports literally, and
// tsconfig has allowImportingTsExtensions, so Next accepts them too.
import { PERSONA } from "./persona.ts";
import { OPERATING } from "./operating.ts";

export const INSTRUCTIONS = PERSONA + "\n\n" + OPERATING;

export type YuriContext = {
  home: string;
  memory_user: string;
  journal_today: string;
  active_missions: { id: string; title: string; goal: string | null; status: string; project: string | null }[];
  agents: { id: string; name: string; online: boolean; version?: string | null; detail?: string }[];
};

const cap = (s: string, n: number) => (s.length > n ? s.slice(s.length - n) : s);

// Block appended to the connect-time snapshot. Pure so it can be unit-tested;
// returns "" when the backend context is unavailable so connect still works.
export function yuriContextBlock(ctx: YuriContext | null | undefined): string {
  if (!ctx) return "";
  const lines: string[] = ["", "", `YOUR HOME: ${ctx.home}`];
  const mem = (ctx.memory_user || "").trim();
  lines.push("WHAT YOU REMEMBER ABOUT THE USER:", mem ? cap(mem, 4000) : "(nothing yet — use remember when you learn something)");
  const journal = (ctx.journal_today || "").trim();
  if (journal) lines.push("", "TODAY SO FAR (your journal):", cap(journal, 4000));
  const agents = (ctx.agents || []).map((a) => `- ${a.name}: ${a.online ? "online" : "OFFLINE"}${a.version ? ` (${a.version})` : ""}`);
  if (agents.length) lines.push("", "AGENTS:", ...agents);
  const missions = (ctx.active_missions || []).map(
    (m) => `- "${m.title}"${m.project ? ` · ${m.project}` : ""} · ${m.status}${m.goal ? ` · goal: ${m.goal}` : ""}`,
  );
  lines.push("", missions.length ? `ACTIVE MISSIONS:\n${missions.join("\n")}` : "ACTIVE MISSIONS: none");
  return lines.join("\n");
}
```

`frontend/lib/instructions.test.ts`:
```ts
// Run: npm test (node --test)
import { test } from "node:test";
import assert from "node:assert/strict";
import { INSTRUCTIONS, yuriContextBlock } from "./instructions.ts";

test("INSTRUCTIONS is persona + operating rules and keeps the load-bearing tool names", () => {
  assert.ok(INSTRUCTIONS.startsWith("You are Yuri"));
  for (const t of ["start_session", "tell_claude", "answer_prompt", "interrupt_session", "set_mode", "send_keys", "remember", "mute"]) {
    assert.ok(INSTRUCTIONS.includes(t), `missing ${t}`);
  }
  assert.ok(!INSTRUCTIONS.includes("You are the VOICE for Claude Code"));
});

test("context block is empty when the backend is unreachable", () => {
  assert.equal(yuriContextBlock(null), "");
  assert.equal(yuriContextBlock(undefined), "");
});

test("context block renders memory, journal, agents, missions", () => {
  const out = yuriContextBlock({
    home: "/Users/x/Yuri",
    memory_user: "- 2026-09-02  prefers pnpm",
    journal_today: "# 2026-09-02\n- 09:00  mission created: fix",
    active_missions: [{ id: "m", title: "fix", goal: "make tests pass", status: "running", project: "pm-tool" }],
    agents: [{ id: "claude-code", name: "Claude Code", online: false }],
  });
  assert.ok(out.includes("YOUR HOME: /Users/x/Yuri"));
  assert.ok(out.includes("prefers pnpm"));
  assert.ok(out.includes("TODAY SO FAR"));
  assert.ok(out.includes("Claude Code: OFFLINE"));
  assert.ok(out.includes('"fix" · pm-tool · running · goal: make tests pass'));
});

test("memory is capped to its tail", () => {
  const out = yuriContextBlock({ home: "h", memory_user: "a".repeat(5000) + "END", journal_today: "", active_missions: [], agents: [] });
  assert.ok(out.includes("END"));
  assert.ok(!out.includes("a".repeat(4500)));
});
```

- [ ] **Step 6: Wire the connect path in `frontend/components/VoiceAgent.tsx`**

Change the import to `import { INSTRUCTIONS, yuriContextBlock, type YuriContext } from "@/lib/instructions";`. Just before `const params = connectionParams(provider, model);` in the connect function add:

```ts
    // Yuri's own context (memory, journal, missions, agent health). Best-effort:
    // an unreachable backend must not block connecting — the snapshot above
    // already covers live sessions.
    let yuriCtx: YuriContext | null = null;
    try {
      const r = await fetch("/api/yuri/context", { headers: authHeaders() });
      if (r.ok) yuriCtx = (await r.json()) as YuriContext;
    } catch {
      logDebug("error", "yuri context unavailable at connect", undefined, "voice", "backend");
    }
```
and change `instructions: INSTRUCTIONS + dynamicContext(snapshot),` → `instructions: INSTRUCTIONS + dynamicContext(snapshot) + yuriContextBlock(yuriCtx),`. Check `logDebug`'s signature at its definition (`VoiceAgent.tsx:908`) and match its argument order.

- [ ] **Step 7: Run** `cd frontend && npm test` → `pass 9`; `npx tsc --noEmit` → clean; backend full suite → `OK`.

- [ ] **Step 8: Manual Checkpoint B (live)** — see Task 21.

---

### Task 20: `bin/yuri` launcher + `yuri doctor`

**Files:**
- Create: `bin/yuri`, `backend/yuri/doctor.py`
- Test: `backend/tests/test_doctor.py`

- [ ] **Step 1: Failing test**

```python
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from yuri import doctor  # noqa: E402


class Doctor(unittest.TestCase):
    def test_reports_each_check_and_exit_code(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")), \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d, "GEMINI_API_KEY": "x"}), \
             mock.patch.object(doctor.shutil, "which", lambda n: None):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = doctor.main([])
            out = buf.getvalue()
            for label in ["home", "database", "allowed roots", "claude", "tmux", "voice keys"]:
                self.assertIn(label, out, label)
            self.assertEqual(code, 1)  # claude/tmux missing → non-zero
            self.assertTrue(os.path.isfile(os.path.join(d, "Yuri", "yuri.db")))

    def test_ok_when_tools_present(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(config, "YURI_HOME", os.path.join(d, "Yuri")), \
             mock.patch.dict(os.environ, {"ALLOWED_PROJECT_ROOTS": d, "GEMINI_API_KEY": "x"}), \
             mock.patch.object(doctor.shutil, "which", lambda n: "/usr/bin/" + n):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(doctor.main([]), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Write `backend/yuri/doctor.py`**

```python
"""`yuri doctor` — local environment checks. Prints one line per check;
exit 0 when everything required is present."""
from __future__ import annotations

import os
import shutil
import sys

import config
from yuri.home import Home
from yuri.store.sqlite import SCHEMA_VERSION, SqliteStore


def _line(ok: bool, label: str, detail: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {label:<14} {detail}")
    return ok


def main(argv: list[str]) -> int:
    print("yuri doctor")
    ok = True
    home = Home(config.YURI_HOME)
    try:
        home.ensure()
        ok &= _line(True, "home", home.path)
    except Exception as exc:
        ok &= _line(False, "home", f"{home.path}: {exc}")
    try:
        store = SqliteStore(home.db_path)
        store.migrate()
        v = store.settings.get("schema_version")
        store.close()
        ok &= _line(v == SCHEMA_VERSION, "database", f"{home.db_path} (schema v{v})")
    except Exception as exc:
        ok &= _line(False, "database", str(exc))
    roots = config.allowed_project_roots()
    ok &= _line(bool(roots), "allowed roots", ", ".join(roots) or "ALLOWED_PROJECT_ROOTS is not set (sessions will refuse to start)")
    claude = shutil.which("claude")
    ok &= _line(claude is not None, "claude", claude or "not on PATH — install Claude Code")
    tmux = shutil.which("tmux")
    ok &= _line(tmux is not None, "tmux", tmux or "not on PATH — brew install tmux")
    keys = config.voice_keys_found()
    ok &= _line(bool(keys), "voice keys", ", ".join(f"{k} ({src})" for k, src in keys) or "none found")
    _line(True, "agents", config.YURI_AGENTS)
    print("ok" if ok else "problems found")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 4: Write `bin/yuri`** (`chmod +x`)

```bash
#!/usr/bin/env bash
# yuri — primary launcher. `yuri doctor` runs environment checks; every other
# subcommand is delegated to the compatibility launcher `yapcode` (up / session /
# config) so nothing existing changes (plan §29).
set -euo pipefail
APP_ROOT="${YAPCODE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

case "${1:-up}" in
  doctor)
    cd "$APP_ROOT/backend"
    PY="./.venv/bin/python"; [ -x "$PY" ] || PY="python3"
    exec "$PY" -m yuri.doctor
    ;;
  -h|--help|help)
    printf '%s\n' "usage: yuri {up|doctor|session [dir]|config}" >&2
    ;;
  *)
    exec "$APP_ROOT/bin/yapcode" "$@"
    ;;
esac
```

- [ ] **Step 5: Run** `tests.test_doctor` → PASS; `./bin/yuri doctor` → prints checks, exit 0 on this machine; `./bin/yuri help` → usage; full suite → `OK`.

---

### Task 21: Verification checkpoints (manual, spec §9)

Do these with the backend (`cd backend && ./run.sh`) and frontend (`cd frontend && npm run dev`) running. Record results (pass/fail + one line) in `docs/superpowers/plans/2026-09-02-yuri-foundation-verification.md` for the user.

- [ ] **Startup**: log shows `config: …` banner then `yuri: home=/Users/…/Yuri db=… agents=['claude-code']`; `~/Yuri/{memory/user.md,journal,workspace}` exist.
- [ ] **Health**: `curl -s localhost:8000/yuri/agents | python3 -m json.tool` → claude-code `online: true` with a version string.
- [ ] **Checkpoint B — live Gemini run**: connect → "start a session in yuri-code" → "tell it to list the files in the backend folder" → wait → permission or completion narrated → if a permission prompt appears say "deny" → "close it". Then:
  - `curl -s localhost:8000/yuri/missions` → one mission, `goal` = the list-files instruction, status `paused`.
  - `curl -s "localhost:8000/yuri/approvals"` → the denied approval (if one was requested).
  - `cat ~/Yuri/journal/$(date +%F).md` → mission created / turn completed / approval lines.
  - Browser refresh → `GET /yuri/missions` unchanged (persistence survives refresh, spec §51).
- [ ] **Persona**: at connect, ask "what do you remember about me?" → she answers from `memory/user.md`; say "remember that I prefer pnpm" → `remember` tool fires, line appended.
- [ ] **Rehydrate**: start a CLI session, restart the backend → session listed again, its row still live; `tmux kill-session -t vc_<8 chars>`, restart → row `lost`, `session.lost` in `/yuri/events`.
- [ ] **Handoff**: from a plain terminal run `claude` in an allowed project, use `/voice-handoff` → adopted; `/yuri/missions` shows `created_by: handoff`.
- [ ] **Existing surfaces**: `send_keys` (say "press escape"), `set_mode` with a pending prompt ("allow that and switch to auto"), `run_slash_command /init` (or `/help`), live terminal WebSocket attaches, `/debug/stream` shows `yuri` source events, `cost_usd` shows in the session list.
- [ ] **Not exercised live** (report as such): Azure and OpenAI realtime paths — covered by unchanged code + `test_mint_output_modalities.py`.
- [ ] **Final**: `cd backend && .venv/bin/python -m unittest discover -s tests` → `OK`; `cd frontend && npm test` → all pass; `npx tsc --noEmit` clean. `git status` shows only intended files; **nothing committed** — report to the user for review.
