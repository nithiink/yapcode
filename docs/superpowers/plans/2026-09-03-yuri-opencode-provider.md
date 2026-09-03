# Yuri OpenCode Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make OpenCode the second real `AgentProvider`, so Yuri can start a mission on it, drive it by voice, approve its actions and narrate it — through the same domain, approval workflow and narration policy as Claude.

**Architecture:** `yuri/providers/opencode/` in three layers — `client.py` (HTTP: envelope, auth, error translation), `server.py` (attach-or-spawn lifecycle, and the rule that Yuri never stops a server she did not start), `provider.py` (the contract, a per-handle `durable.seq` cursor polled from `/history?after=N`). OpenCode reports `supports_events=False`, so narration takes the poll-owned path. Nothing in the domain, services or frontend changes.

**Tech Stack:** Python 3.14 (`backend/.venv`), `httpx` (already pinned), stdlib `http.server` for the fake OpenCode in tests, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-03-yuri-opencode-provider-design.md` — read it first, especially **§2** (observed API reality; several of the source plan's assumptions and part of OpenCode's own OpenAPI are wrong) and **§2.1** (the event vocabulary is only partly known, so the mapping is defensive by construction).

## Global Constraints

- **Commit per task.** Message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`. Branch `feat/yuri-opencode` off `main`. Never `git stash`, `git checkout`, `git reset`, amend, or rebase.
- **No new dependencies.** `requirements.txt`, `requirements.lock`, `package-lock.json` unchanged. The fake OpenCode server in tests uses stdlib `http.server`.
- **No test may require a real OpenCode server, network, or credentials.** The one live check is Task 8, run by hand.
- Backend: `cd backend && .venv/bin/python -m unittest discover -s tests` → OK; `-W always::ResourceWarning … | grep -c ResourceWarning` → `0`. Green with `~/Yuri` present and absent; leave it **absent**.
- Baseline: **424 backend tests, 32 frontend tests**, both green and pristine.
- `backend/tests/test_tools_dispatch.py` is the voice-tool result-key contract — unchanged.
- **`claude-code` stays the default agent.** Adding a provider must not change which agent an unqualified request gets.
- **Provider failures stay isolated** (plan §41): OpenCode absent, unreachable or broken must not degrade Claude sessions, narration, missions or startup.
- New backend files: `from __future__ import annotations`, a short WHY docstring, type hints. Match `backend/yuri/providers/claude_code.py`'s style.
- **Layering:** `yuri/providers/opencode/` may import `yuri.providers.base` and stdlib/httpx only — never the store, domain, services or API.
- Secrets (`OPENCODE_SERVER_PASSWORD`) are never logged.

---

## File structure

**Created**

| Path | Responsibility |
|---|---|
| `backend/yuri/providers/opencode/__init__.py` | re-export `OpenCodeProvider` |
| `backend/yuri/providers/opencode/client.py` | `OpenCodeClient` — envelope, auth, error translation |
| `backend/yuri/providers/opencode/server.py` | `OpenCodeServer` — attach-or-spawn, `owned` |
| `backend/yuri/providers/opencode/provider.py` | `OpenCodeProvider(AgentProvider)` |
| `backend/tests/fake_opencode.py` | stdlib fake OpenCode HTTP server (shared by tests, no `test_` prefix) |
| `backend/tests/test_opencode_client.py` | |
| `backend/tests/test_opencode_server.py` | |
| `backend/tests/test_opencode_provider.py` | the contract suite + cursor properties |
| `backend/tests/test_opencode_permissions.py` | |
| `backend/tests/test_opencode_rehydrate.py` | |

**Modified**

| Path | Change |
|---|---|
| `backend/tests/provider_contract.py` | capability-aware (Task 1) |
| `backend/config.py` | the `OPENCODE_*` keys |
| `backend/yuri/providers/registry.py` | register `opencode` |
| `backend/yuri/doctor.py` | an OpenCode line |
| `frontend/lib/operating.ts` | teach Yuri that OpenCode exists |

---

## Task 1: Make the contract suite capability-aware

**Files:**
- Modify: `backend/tests/provider_contract.py`
- Test: the existing `backend/tests/test_fake_provider.py` and `test_claude_provider.py` must stay green **unchanged**

**Why this is first.** The contract suite has Claude-shaped assumptions baked in, and they only surface now that a second provider exists — which is exactly what Phase 5 is for. Two of its tests cannot pass for OpenCode:

- `test_lifecycle_create_send_poll_stop` asserts `await p.set_mode(h, "plan") == "plan"`. OpenCode has no permission modes and raises `NotImplementedError`.
- `test_observer_receives_events` requires an observer to fire. OpenCode reports `supports_events=False` and never fires one.

The fix makes the suite **stronger**, not weaker: it now asserts the *negative* case too — a provider that declares it has no modes must actually refuse `set_mode`, and one that declares it emits no events must still accept an observer without raising. A provider can no longer quietly under-deliver on what its own `capabilities()` claims.

**Interfaces — Produces:** the same `AgentProviderContract`, with `_fire_event` no longer abstract (it is only called when `supports_events` is true).

- [ ] **Step 1: Read** `backend/tests/provider_contract.py`, `backend/yuri/providers/base.py` (for `AgentCapabilities`), and `backend/tests/test_fake_provider.py` (the subclass pattern).

- [ ] **Step 2: Split the mode assertion out of the lifecycle test.** In `test_lifecycle_create_send_poll_stop`, delete these two lines:

```python
        mode = await self.p.set_mode(h, "plan")
        self.assertEqual(mode, "plan")
```

and add a new test that branches on the declared capability:

```python
    async def test_set_mode_matches_declared_permission_modes(self):
        """A provider must honour what its own capabilities() claims: modes work
        if it declares any, and are refused if it declares none. Claude has four;
        OpenCode has no equivalent concept."""
        modes = self.p.capabilities().permission_modes
        h = await self.p.create_session(self.ctx, self.opts())
        try:
            if modes:
                target = modes[0]
                self.assertEqual(await self.p.set_mode(h, target), target)
            else:
                with self.assertRaises(NotImplementedError):
                    await self.p.set_mode(h, "plan")
        finally:
            await self.p.stop(h)
```

Note the positive branch now uses `modes[0]` rather than the hardcoded `"plan"`, so it holds for any provider whose modes differ from Claude's.

- [ ] **Step 3: Make the observer test capability-aware.** Replace `test_observer_receives_events` with:

```python
    async def test_observer_matches_declared_event_support(self):
        """supports_events is a promise. A provider that makes it must deliver a
        ProviderEvent to the observer; one that does not must still accept an
        observer without raising, because build_container installs one on every
        provider uniformly."""
        got = []
        self.p.set_observer(lambda h, ev: got.append((h, ev)))
        h = await self.p.create_session(self.ctx, self.opts())
        try:
            if self.p.capabilities().supports_events:
                self._fire_event(h)
                self.assertTrue(got, "declares supports_events but the observer never fired")
                self.assertIsInstance(got[0][1], ProviderEvent)
            else:
                # Nothing to fire; the point is that installing one is harmless.
                self.assertEqual(got, [])
        finally:
            await self.p.stop(h)
```

and make `_fire_event` a no-op default instead of abstract, since a
non-event provider has nothing to fire:

```python
    def _fire_event(self, handle):
        """Trigger a provider event for `handle`. Subclasses that declare
        supports_events must override this; others never have it called."""
        raise NotImplementedError(
            "this provider declares supports_events=True, so the contract test "
            "needs _fire_event to trigger one")
```

- [ ] **Step 4: Run the two existing provider suites — they must pass unchanged.**

Run: `cd backend && .venv/bin/python -m unittest tests.test_fake_provider tests.test_claude_provider -v`
Expected: OK. Both declare `supports_events=True` and non-empty `permission_modes`, so both take the positive branch. Test *count* rises by one per suite (the lifecycle test shed an assertion and gained a sibling).

- [ ] **Step 5: Full suite.** `cd backend && .venv/bin/python -m unittest discover -s tests` → OK, 424 + 2.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/provider_contract.py
git commit -m "$(cat <<'EOF'
test(providers): make the contract suite capability-aware

The suite asserted set_mode returns "plan" and that an observer always
fires — both true of Claude and neither true in general. They only
surface as assumptions now that a second provider exists, which is what
the OpenCode phase is for.

Each is now branched on the provider's own capabilities(), which makes
the contract stronger rather than weaker: a provider declaring no
permission modes must actually refuse set_mode, and one declaring no
event support must still accept an observer, because build_container
installs one on every provider uniformly. A provider can no longer
quietly under-deliver on what it claims.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `OpenCodeClient` — envelope, auth, errors

**Files:**
- Create: `backend/yuri/providers/opencode/__init__.py`, `backend/yuri/providers/opencode/client.py`
- Test: `backend/tests/fake_opencode.py`, `backend/tests/test_opencode_client.py`

**Why first among the OpenCode files.** Nothing above it can be right until the envelope, auth and error translation are. Every `/api/*` response is wrapped in `{"data": …}` (spec §2) — a fact absent from the source plan and the first thing that broke the live probe.

**Interfaces — Produces:**
```python
class OpenCodeError(RuntimeError)          # transport/5xx/unreachable
class OpenCodeRequestError(ValueError)     # OpenCode's InvalidRequestError -> soft error upstream
class OpenCodeClient:
    def __init__(self, base_url: str, password: str | None = None, timeout: float = 30.0)
    async def get(self, path: str, **params) -> Any      # returns the UNWRAPPED data
    async def post(self, path: str, body: dict | None = None) -> Any
    async def close(self) -> None
    @property
    def base_url(self) -> str
```

- [ ] **Step 1: Write the fake OpenCode server** `backend/tests/fake_opencode.py`. It is shared by four test modules, so it has no `test_` prefix and is not collected.

```python
"""A stdlib fake of OpenCode's /api/* surface, shaped from the live probe
recorded in the design spec section 2 — envelope, ids, event and message
shapes. Lets every OpenCode test run with no real server, no network and no
credentials, which is what makes the provider's contract suite meaningful.

Only the endpoints the provider actually calls are implemented; anything else
404s, so a provider reaching for an unspecified endpoint fails loudly in tests
rather than silently in production.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class FakeOpenCodeState:
    """The server's mutable world, driven by tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}      # session id -> durable events
        self.messages: dict[str, list[dict]] = {}    # session id -> projected messages
        self.permissions: dict[str, list[dict]] = {}
        self.questions: dict[str, list[dict]] = {}
        self.replies: list[tuple] = []               # (kind, sid, request_id, body)
        self.interrupts: list[str] = []
        self.require_password: str | None = None
        self.fail_next: tuple[int, Any] | None = None   # (status, body) for one call
        self.calls: list[tuple[str, str]] = []          # (method, path)
        self._n = 0

    # --- test controls ---------------------------------------------------
    def new_session(self, directory: str = "/tmp", **extra) -> str:
        self._n += 1
        sid = f"ses_fake{self._n:04d}"
        self.sessions[sid] = {
            "id": sid, "projectID": "global",
            "location": {"directory": directory},
            "subpath": directory.lstrip("/"),
            "title": f"Fake session {self._n}",
            "cost": 0.0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}},
            "time": {"created": 1, "updated": 1},
            **extra,
        }
        self.events.setdefault(sid, [])
        self.messages.setdefault(sid, [])
        return sid

    def push_event(self, sid: str, type: str, data: dict | None = None) -> int:
        seq = len(self.events.setdefault(sid, [])) + 1
        self.events[sid].append({
            "id": f"evt_{sid}_{seq}", "type": type,
            "durable": {"aggregateID": sid, "seq": seq, "version": 1},
            "data": {"sessionID": sid, **(data or {})},
        })
        return seq

    def push_assistant(self, sid: str, text: str, finish: str = "stop") -> None:
        self.messages.setdefault(sid, []).append({
            "id": f"msg_a{len(self.messages[sid]) + 1}", "type": "assistant",
            "finish": finish, "agent": "build",
            "model": {"id": "fake-model", "providerID": "fake"},
            "content": [{"type": "text", "text": text}],
            "time": {"created": 1, "completed": 2},
        })

    def add_permission(self, sid: str, request_id: str, title: str,
                       tool: str = "bash", metadata: dict | None = None) -> None:
        self.permissions.setdefault(sid, []).append({
            "id": request_id, "sessionID": sid, "title": title,
            "tool": tool, "metadata": metadata or {},
        })

    def add_question(self, sid: str, request_id: str, text: str,
                     options: list[str] | None = None) -> None:
        self.questions.setdefault(sid, []).append({
            "id": request_id, "sessionID": sid, "text": text,
            "options": options or [],
        })


class _Handler(BaseHTTPRequestHandler):
    state: FakeOpenCodeState

    def log_message(self, *a):      # keep the test suite's output pristine
        pass

    # --- plumbing -------------------------------------------------------
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _data(self, payload: Any) -> None:
        self._send(200, {"data": payload})           # the real envelope

    def _bad(self, message: str, field: str = "") -> None:
        self._send(400, {"_tag": "InvalidRequestError", "message": message,
                         "field": field, "kind": "Payload"})

    def _authorized(self) -> bool:
        want = self.state.require_password
        if not want:
            return True
        return self.headers.get("x-opencode-password") == want

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _pre(self, method: str) -> bool:
        """Record the call, apply an injected failure, enforce auth."""
        self.state.calls.append((method, self.path.split("?")[0]))
        if self.state.fail_next is not None:
            status, payload = self.state.fail_next
            self.state.fail_next = None
            self._send(status, payload)
            return False
        if not self._authorized():
            self._send(401, {"_tag": "Unauthorized", "message": "bad password"})
            return False
        return True

    # --- routes ---------------------------------------------------------
    def do_GET(self):                       # noqa: N802
        if not self._pre("GET"):
            return
        s, path = self.state, self.path.split("?")[0]
        query = dict(p.split("=", 1) for p in (self.path.split("?")[1].split("&")
                     if "?" in self.path and self.path.split("?")[1] else []))
        if path == "/api/session":
            return self._data(list(s.sessions.values()))
        parts = path.strip("/").split("/")
        # /api/session/{sid}[/tail]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "session":
            sid, tail = parts[2], parts[3:]
            if sid not in s.sessions:
                return self._bad("Invalid session ID", "sessionID")
            if not tail:
                return self._data(s.sessions[sid])
            if tail == ["history"]:
                after = int(query.get("after", 0))
                return self._data([e for e in s.events.get(sid, [])
                                   if e["durable"]["seq"] > after])
            if tail == ["message"]:
                return self._data(s.messages.get(sid, []))
            if tail == ["permission"]:
                return self._data(s.permissions.get(sid, []))
            if tail == ["question"]:
                return self._data(s.questions.get(sid, []))
        self._send(404, {"_tag": "NotFound", "message": path})

    def do_POST(self):                      # noqa: N802
        if not self._pre("POST"):
            return
        s, path = self.state, self.path.split("?")[0]
        body = self._body()
        if path == "/api/session":
            directory = (body.get("location") or {}).get("directory")
            if not directory:
                return self._bad('Missing key\n  at ["location"]["directory"]')
            model = body.get("model")
            if model is not None and "id" not in model:
                return self._bad('Missing key\n  at ["model"]["id"]')
            sid = s.new_session(directory)
            if model:
                s.sessions[sid]["model"] = model
            return self._data(s.sessions[sid])
        parts = path.strip("/").split("/")
        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "session":
            sid, tail = parts[2], parts[3:]
            if sid not in s.sessions:
                return self._bad("Invalid session ID", "sessionID")
            if tail == ["prompt"]:
                text = (body.get("prompt") or {}).get("text")
                if text is None:
                    return self._bad('Missing key\n  at ["prompt"]["text"]')
                seq = s.push_event(sid, "session.next.prompt.admitted", {"text": text})
                return self._data({"admittedSeq": seq, "id": f"msg_u{seq}",
                                   "sessionID": sid, "prompt": body["prompt"],
                                   "delivery": body.get("delivery", "queue"),
                                   "timeCreated": 1})
            if tail == ["interrupt"]:
                s.interrupts.append(sid)
                return self._data({"ok": True})
            if len(tail) == 3 and tail[0] == "permission" and tail[2] == "reply":
                s.replies.append(("permission", sid, tail[1], body))
                s.permissions[sid] = [p for p in s.permissions.get(sid, [])
                                      if p["id"] != tail[1]]
                return self._data({"ok": True})
            if len(tail) == 3 and tail[0] == "question" and tail[2] == "reply":
                s.replies.append(("question", sid, tail[1], body))
                s.questions[sid] = [q for q in s.questions.get(sid, [])
                                    if q["id"] != tail[1]]
                return self._data({"ok": True})
        self._send(404, {"_tag": "NotFound", "message": path})


class FakeOpenCode:
    """Context manager: `with FakeOpenCode() as fake: fake.url, fake.state`."""

    def __init__(self) -> None:
        self.state = FakeOpenCodeState()
        handler = type("_H", (_Handler,), {"state": self.state})
        self._srv = HTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._srv.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeOpenCode":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()
        self._thread.join(timeout=5)
```

- [ ] **Step 2: Write `backend/tests/test_opencode_client.py`**

```python
"""The HTTP seam. Every /api/* response is wrapped in {"data": ...} (design
spec section 2) — the first thing that broke the live probe — and OpenCode's
InvalidRequestError must arrive upstream as a ValueError, which is what
tools.py turns into a soft error the voice model can recover from.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.opencode.client import (  # noqa: E402
    OpenCodeClient, OpenCodeError, OpenCodeRequestError)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_unwraps_the_data_envelope(self):
        with FakeOpenCode() as fake:
            fake.state.new_session("/tmp/x")
            c = OpenCodeClient(fake.url)
            try:
                out = await c.get("/api/session")
                # Unwrapped: a list of sessions, not {"data": [...]}.
                self.assertIsInstance(out, list)
                self.assertEqual(out[0]["location"]["directory"], "/tmp/x")
            finally:
                await c.close()

    async def test_post_unwraps_and_sends_json(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url)
            try:
                out = await c.post("/api/session",
                                   {"location": {"directory": "/tmp/y"}})
                self.assertTrue(out["id"].startswith("ses_"))
            finally:
                await c.close()

    async def test_invalid_request_becomes_a_valueerror(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url)
            try:
                with self.assertRaises(OpenCodeRequestError) as cm:
                    await c.post("/api/session", {})      # missing location
                self.assertIsInstance(cm.exception, ValueError)
                self.assertIn("location", str(cm.exception))
            finally:
                await c.close()

    async def test_server_error_becomes_an_opencode_error(self):
        with FakeOpenCode() as fake:
            fake.state.fail_next = (500, {"message": "boom"})
            c = OpenCodeClient(fake.url)
            try:
                with self.assertRaises(OpenCodeError):
                    await c.get("/api/session")
            finally:
                await c.close()

    async def test_unreachable_server_becomes_an_opencode_error(self):
        # Port 1 is reserved and nothing listens there.
        c = OpenCodeClient("http://127.0.0.1:1", timeout=1.0)
        try:
            with self.assertRaises(OpenCodeError):
                await c.get("/api/session")
        finally:
            await c.close()

    async def test_password_is_sent_when_configured(self):
        with FakeOpenCode() as fake:
            fake.state.require_password = "s3cret"
            ok = OpenCodeClient(fake.url, password="s3cret")
            bad = OpenCodeClient(fake.url)
            try:
                self.assertIsInstance(await ok.get("/api/session"), list)
                with self.assertRaises(OpenCodeError):
                    await bad.get("/api/session")
            finally:
                await ok.close()
                await bad.close()

    async def test_the_password_never_reaches_a_log_or_a_repr(self):
        c = OpenCodeClient("http://127.0.0.1:1", password="s3cret")
        try:
            self.assertNotIn("s3cret", repr(c))
            self.assertNotIn("s3cret", str(c))
        finally:
            await c.close()

    async def test_query_params_are_passed(self):
        with FakeOpenCode() as fake:
            sid = fake.state.new_session()
            fake.state.push_event(sid, "a")
            fake.state.push_event(sid, "b")
            c = OpenCodeClient(fake.url)
            try:
                after0 = await c.get(f"/api/session/{sid}/history", after=0)
                after1 = await c.get(f"/api/session/{sid}/history", after=1)
                self.assertEqual(len(after0), 2)
                self.assertEqual(len(after1), 1)
            finally:
                await c.close()

    async def test_base_url_trailing_slash_is_normalised(self):
        with FakeOpenCode() as fake:
            c = OpenCodeClient(fake.url + "/")
            try:
                self.assertIsInstance(await c.get("/api/session"), list)
                self.assertFalse(c.base_url.endswith("/"))
            finally:
                await c.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run** → FAIL (`ModuleNotFoundError: yuri.providers.opencode`).

- [ ] **Step 4: Write `backend/yuri/providers/opencode/__init__.py`**

```python
"""OpenCode as an AgentProvider — the second real agent behind the contract,
and therefore the thing that proves the abstraction. Three layers: client
(HTTP), server (attach-or-spawn lifecycle), provider (the contract)."""
from __future__ import annotations

from .provider import OpenCodeProvider

__all__ = ["OpenCodeProvider"]
```

(Write this file **last** in this task, after `client.py`, or the import will
fail — `provider.py` does not exist yet. Simplest: create `__init__.py` empty
now and add the re-export in Task 4.)

- [ ] **Step 5: Write `backend/yuri/providers/opencode/client.py`**

```python
"""The HTTP seam to an OpenCode server.

Three jobs, each learned the hard way from the live probe recorded in the
design spec section 2:

  * unwrap the {"data": ...} envelope every /api/* response carries — nothing
    in the source plan mentions it, and it silently broke the first probes;
  * translate OpenCode's errors into the two shapes the rest of Yuri already
    knows — a ValueError for "you asked wrongly" (which tools.py turns into a
    soft error the voice model recovers from) and a RuntimeError for "the
    server is broken or gone" (which the provider reports as unhealthy);
  * carry the server password, if one is configured, without ever logging it.

Nothing above this file should know what an envelope is.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("yuri.opencode.client")

# OpenCode's own error envelope uses _tag for the discriminator.
_REQUEST_ERROR_TAGS = frozenset({"InvalidRequestError", "NotFound"})


class OpenCodeError(RuntimeError):
    """The server is unreachable, broken, or refused us. Provider-level."""


class OpenCodeRequestError(ValueError):
    """We asked wrongly (OpenCode's InvalidRequestError). A ValueError on
    purpose: tools.py already maps ValueError to a soft error the voice model
    can recover from, so a bad request reaches the user as words rather than a
    stack trace."""


class OpenCodeClient:
    def __init__(self, base_url: str, password: str | None = None,
                 timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        # Held privately and never rendered: __repr__ is the default, which
        # shows the class and address, so the secret cannot leak through a log
        # line that interpolates the client.
        self._password = password or None
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def base_url(self) -> str:
        return self._base

    def _headers(self) -> dict[str, str]:
        # The OpenAPI declares no security scheme (spec section 9), so the
        # mechanism is empirical. This header is what the fake enforces and
        # what Task 8 verifies against a real password-protected server; if
        # that check shows otherwise, this one method changes.
        return {"x-opencode-password": self._password} if self._password else {}

    async def get(self, path: str, **params: Any) -> Any:
        return await self._call("GET", path, params=params or None)

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self._call("POST", path, json=body if body is not None else {})

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kw: Any) -> Any:
        url = f"{self._base}{path}"
        try:
            resp = await self._client.request(method, url, headers=self._headers(), **kw)
        except httpx.HTTPError as exc:
            # Unreachable, DNS, timeout: the server's problem, not the caller's.
            raise OpenCodeError(f"OpenCode at {self._base} is unreachable: {exc}") from exc

        if resp.status_code >= 400:
            raise self._error_for(resp)
        if not resp.content:
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            raise OpenCodeError(f"OpenCode returned non-JSON from {path}") from exc
        # The envelope. Some endpoints (and the error shape) are unwrapped, so
        # only unwrap when the key is actually there.
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    def _error_for(self, resp: httpx.Response) -> Exception:
        tag, message = "", resp.text[:300]
        try:
            body = resp.json()
            if isinstance(body, dict):
                tag = str(body.get("_tag") or "")
                message = str(body.get("message") or message)
        except ValueError:
            pass
        if resp.status_code == 400 or tag in _REQUEST_ERROR_TAGS:
            return OpenCodeRequestError(message)
        return OpenCodeError(f"OpenCode returned {resp.status_code}: {message}")
```

- [ ] **Step 6: Run** `cd backend && .venv/bin/python -m unittest tests.test_opencode_client -v` until green.

Note the fake's auth check reads the `x-opencode-password` header, matching
`_headers()`. If Task 8's live check shows OpenCode wants a different
mechanism, both change together — that is the point of keeping it in one method.

- [ ] **Step 7: Full suite** → OK, pristine. **Watch for a leaked thread or socket** from the fake server: every test must use it as a context manager so `__exit__` shuts it down, or the ResourceWarning count will rise.

- [ ] **Step 8: Commit**

```bash
git add backend/yuri/providers/opencode backend/tests/fake_opencode.py backend/tests/test_opencode_client.py
git commit -m "$(cat <<'EOF'
feat(opencode): add the HTTP client and a stdlib fake server

The client's three jobs all come from the live probe recorded in the
design spec: unwrap the {"data": ...} envelope every /api/* response
carries, translate OpenCode's errors into the two shapes Yuri already
knows (ValueError for a bad request, so tools.py turns it into a soft
error the voice model recovers from; RuntimeError for a server that is
broken or gone), and carry the server password without logging it.

The fake implements only the endpoints the provider actually calls, so
reaching for an unspecified one fails loudly in tests rather than
silently in production — and it means no OpenCode test needs a real
server, network or credentials.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `OpenCodeServer` — attach or spawn, and never kill what you didn't start

**Files:**
- Create: `backend/yuri/providers/opencode/server.py`
- Test: `backend/tests/test_opencode_server.py`

**The rule this task exists to enforce:** *Yuri never stops a server she did not start.* `owned` is decided once, at acquisition, and `shutdown()` terminates the process only when it is true. A server the user runs must survive Yuri's shutdown, restart and crash. That is the entire reason the attach branch exists, and it gets its own test.

**Interfaces — Produces:**
```python
class OpenCodeUnavailable(RuntimeError)     # could not attach and could not/should not spawn
class OpenCodeServer:
    def __init__(self, url: str, *, spawn: bool = True, binary: str = "opencode",
                 password: str | None = None, cwd: str | None = None,
                 log_path: str | None = None, ready_timeout: float = 20.0)
    async def acquire(self) -> OpenCodeClient   # attach if reachable, else spawn; idempotent
    async def release(self) -> None             # stop ONLY if owned; always drop the client
    @property
    def owned(self) -> bool
    @property
    def client(self) -> OpenCodeClient | None   # None until acquired
    async def is_reachable(self) -> bool
```

- [ ] **Step 1: Write `backend/tests/test_opencode_server.py`**

```python
"""The server lifecycle, and the one rule that matters most: Yuri never stops
a server she did not start. Spawning is exercised with a stub binary rather
than the real opencode, so these tests need nothing installed.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.opencode.server import (  # noqa: E402
    OpenCodeServer, OpenCodeUnavailable)


def _stub_binary(dirpath: str, port: int, *, ready: bool = True) -> str:
    """A fake `opencode` that serves the one endpoint acquire() probes.
    Spawning the real binary in a unit test would be slow and machine-dependent."""
    path = os.path.join(dirpath, "opencode-stub")
    body = textwrap.dedent(f"""
        import json, sys, time
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200)
                b = json.dumps({{"data": []}}).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers(); self.wfile.write(b)
        if not {ready!r}:
            time.sleep(600)          # never becomes ready
        HTTPServer(("127.0.0.1", {port}), H).serve_forever()
    """)
    with open(path, "w") as f:
        f.write(f"#!/usr/bin/env sh\nexec {sys.executable} -c '{body}'\n"
                .replace("'{body}'", f"\"$(cat <<'PYEOF'\n{body}\nPYEOF\n)\""))
    os.chmod(path, 0o755)
    return path


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Attach(unittest.IsolatedAsyncioTestCase):
    async def test_attaches_to_a_reachable_server_and_does_not_own_it(self):
        with FakeOpenCode() as fake:
            srv = OpenCodeServer(fake.url, spawn=True, binary="definitely-not-a-binary")
            try:
                client = await srv.acquire()
                self.assertFalse(srv.owned, "an attached server must not be owned")
                self.assertEqual(client.base_url, fake.url.rstrip("/"))
            finally:
                await srv.release()
            # THE RULE: release must not have stopped it.
            self.assertTrue(await OpenCodeServer(fake.url, spawn=False).is_reachable(),
                            "release() stopped a server Yuri did not start")

    async def test_acquire_is_idempotent(self):
        with FakeOpenCode() as fake:
            srv = OpenCodeServer(fake.url, spawn=False)
            try:
                a = await srv.acquire()
                b = await srv.acquire()
                self.assertIs(a, b)
            finally:
                await srv.release()

    async def test_concurrent_acquire_yields_one_client(self):
        with FakeOpenCode() as fake:
            srv = OpenCodeServer(fake.url, spawn=False)
            try:
                got = await asyncio.gather(*(srv.acquire() for _ in range(5)))
                self.assertEqual(len({id(c) for c in got}), 1)
            finally:
                await srv.release()

    async def test_password_is_carried_into_the_client(self):
        with FakeOpenCode() as fake:
            fake.state.require_password = "pw"
            srv = OpenCodeServer(fake.url, spawn=False, password="pw")
            try:
                client = await srv.acquire()
                self.assertIsInstance(await client.get("/api/session"), list)
            finally:
                await srv.release()


class Spawn(unittest.IsolatedAsyncioTestCase):
    async def test_spawns_when_nothing_answers_and_owns_it(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(d, "oc.log"),
                                 ready_timeout=20.0)
            try:
                client = await srv.acquire()
                self.assertTrue(srv.owned, "a spawned server must be owned")
                self.assertIsInstance(await client.get("/api/session"), list)
            finally:
                await srv.release()
            # An owned server IS stopped on release.
            self.assertFalse(await OpenCodeServer(f"http://127.0.0.1:{port}",
                                                  spawn=False).is_reachable())

    async def test_spawn_disabled_refuses_with_an_actionable_message(self):
        port = _free_port()
        srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=False)
        with self.assertRaises(OpenCodeUnavailable) as cm:
            await srv.acquire()
        msg = str(cm.exception)
        self.assertIn(str(port), msg)
        self.assertRegex(msg, r"OPENCODE_SPAWN|not reachable|start")

    async def test_a_missing_binary_is_reported_actionably(self):
        port = _free_port()
        srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                             binary="definitely-not-a-binary")
        with self.assertRaises(OpenCodeUnavailable) as cm:
            await srv.acquire()
        self.assertIn("definitely-not-a-binary", str(cm.exception))

    async def test_a_server_that_never_becomes_ready_times_out_and_is_cleaned_up(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port, ready=False)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(d, "oc.log"),
                                 ready_timeout=2.0)
            with self.assertRaises(OpenCodeUnavailable) as cm:
                await srv.acquire()
            self.assertRegex(str(cm.exception).lower(), r"ready|timed out")
            # A failed spawn must not leave the child running.
            self.assertFalse(srv.owned)
            await srv.release()

    async def test_concurrent_acquire_spawns_only_one_process(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(d, "oc.log"))
            try:
                await asyncio.gather(*(srv.acquire() for _ in range(4)))
                self.assertEqual(srv.spawn_count, 1)
            finally:
                await srv.release()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL (no `server` module).

- [ ] **Step 3: Write `backend/yuri/providers/opencode/server.py`**

```python
"""Getting hold of an OpenCode server, and letting go of it correctly.

Two ways to have one: attach to a server already answering at the configured
URL, or spawn `opencode serve` and manage it. The governing rule is that Yuri
never stops a server she did not start — `owned` is decided once, at
acquisition, and release() terminates the process only when it is true. A
server the user runs survives Yuri's shutdown, her restart and her crash;
that is the whole reason the attach branch exists.

Acquisition is lazy: nothing spawns at Yuri startup, and an asyncio.Lock means
two concurrent start_session calls cannot race into two servers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

from .client import OpenCodeClient, OpenCodeError

log = logging.getLogger("yuri.opencode.server")

READY_POLL_INTERVAL = 0.25


class OpenCodeUnavailable(RuntimeError):
    """No server could be attached to, and none could (or may) be spawned.
    The message says what to do about it."""


class OpenCodeServer:
    def __init__(self, url: str, *, spawn: bool = True, binary: str = "opencode",
                 password: str | None = None, cwd: str | None = None,
                 log_path: str | None = None, ready_timeout: float = 20.0) -> None:
        self._url = url.rstrip("/")
        self._spawn_allowed = spawn
        self._binary = binary
        self._password = password
        self._cwd = cwd
        self._log_path = log_path
        self._ready_timeout = ready_timeout
        self._client: OpenCodeClient | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._owned = False
        self._lock = asyncio.Lock()
        self.spawn_count = 0          # for tests: proves the lock works

    # --- state -----------------------------------------------------------

    @property
    def owned(self) -> bool:
        """True only for a server this object spawned. The kill switch."""
        return self._owned

    @property
    def client(self) -> OpenCodeClient | None:
        return self._client

    async def is_reachable(self) -> bool:
        probe = OpenCodeClient(self._url, password=self._password, timeout=3.0)
        try:
            await probe.get("/api/session")
            return True
        except Exception:
            return False
        finally:
            await probe.close()

    # --- acquire / release ------------------------------------------------

    async def acquire(self) -> OpenCodeClient:
        """Attach if something answers, else spawn. Idempotent and race-safe."""
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is not None:      # another waiter won
                return self._client
            if await self.is_reachable():
                log.info("attached to an existing OpenCode server at %s", self._url)
                self._client = OpenCodeClient(self._url, password=self._password)
                self._owned = False           # NOT ours: never stop it
                return self._client
            if not self._spawn_allowed:
                raise OpenCodeUnavailable(
                    f"OpenCode is not reachable at {self._url} and spawning is "
                    "disabled (OPENCODE_SPAWN=0). Start it with "
                    f"`opencode serve --port {self._port()}`, or set OPENCODE_SPAWN=1.")
            await self._spawn()
            self._client = OpenCodeClient(self._url, password=self._password)
            self._owned = True                # ours: release() stops it
            return self._client

    async def release(self) -> None:
        """Drop the client, and stop the process only if we started it."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if not self._owned:
            # Defensive: _proc should only ever be set when owned. Saying so
            # loudly beats silently killing a user's server.
            log.error("refusing to stop an OpenCode server Yuri did not start")
            return
        self._owned = False
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (TimeoutError, asyncio.TimeoutError):
                proc.kill()
                await proc.wait()
        log.info("stopped the OpenCode server Yuri started")

    # --- spawning ---------------------------------------------------------

    def _port(self) -> str:
        tail = self._url.rsplit(":", 1)[-1]
        return tail.split("/")[0] if tail.isdigit() or "/" in tail else "4096"

    async def _spawn(self) -> None:
        binary = self._binary if os.path.sep in self._binary else shutil.which(self._binary)
        if not binary or not os.path.exists(binary):
            raise OpenCodeUnavailable(
                f"the OpenCode binary {self._binary!r} is not on PATH — install "
                "OpenCode, or set OPENCODE_BIN to its full path, or set "
                "OPENCODE_SPAWN=0 and run `opencode serve` yourself.")
        env = dict(os.environ)
        if self._password:
            env["OPENCODE_SERVER_PASSWORD"] = self._password
        # Its output belongs in a log, not in the terminal the voice UI owns.
        sink = open(self._log_path, "ab") if self._log_path else asyncio.subprocess.DEVNULL
        try:
            self._proc = await asyncio.create_subprocess_exec(
                binary, "serve", "--port", self._port(), "--hostname", "127.0.0.1",
                cwd=self._cwd or None, env=env,
                stdout=sink if self._log_path else sink,
                stderr=asyncio.subprocess.STDOUT if self._log_path else sink,
            )
        finally:
            if self._log_path and hasattr(sink, "close"):
                sink.close()
        self.spawn_count += 1
        self._owned = True        # set before the readiness wait so a timeout cleans up
        if not await self._await_ready():
            await self.release()
            raise OpenCodeUnavailable(
                f"spawned OpenCode at {self._url} but it never became ready within "
                f"{self._ready_timeout:.0f}s"
                + (f" — see {self._log_path}" if self._log_path else ""))

    async def _await_ready(self) -> bool:
        """Probe until it answers. A probe, never a sleep — the startup time
        depends on the machine and on plugin loading."""
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.returncode is not None:
                return False            # it died; no point waiting out the clock
            if await self.is_reachable():
                return True
            await asyncio.sleep(READY_POLL_INTERVAL)
        return False
```

- [ ] **Step 4: Run** `tests.test_opencode_server -v` until green.

If the stub-binary shell quoting fights you, write the stub as a `.py` file and
make the "binary" a two-line `sh` wrapper that execs `sys.executable stub.py` —
the point is only that `acquire()` spawns *something* that answers, not that it
resembles OpenCode.

- [ ] **Step 5: Full suite** → OK and pristine. A spawned child that outlives a
test will show up as a warning or a hung run; every test releases in `finally`.

- [ ] **Step 6: Commit**

```bash
git add backend/yuri/providers/opencode/server.py backend/tests/test_opencode_server.py
git commit -m "$(cat <<'EOF'
feat(opencode): attach to a server, or spawn one we then own

Two ways to have an OpenCode server: attach to one already answering, or
spawn `opencode serve` and manage it. The rule the whole file exists to
enforce is that Yuri never stops a server she did not start — `owned` is
decided once at acquisition and release() terminates the process only
when it is true, so a server the user runs survives her shutdown,
restart and crash.

Acquisition is lazy and lock-guarded: nothing spawns at Yuri startup,
and concurrent start_session calls cannot race into two servers.
Readiness is a probe rather than a sleep, a failed spawn cleans up after
itself, and every refusal names what to do about it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: `OpenCodeProvider` — the contract, and the cursor

**Files:**
- Create: `backend/yuri/providers/opencode/provider.py`; finish `opencode/__init__.py`
- Test: `backend/tests/test_opencode_provider.py`

**This is the task that proves the abstraction.** `OpenCodeProviderContract` subclasses the same `AgentProviderContract` that `FakeAgentProvider` and `ClaudeCodeProvider` pass — against a fake OpenCode HTTP server. If it passes, "Claude Code is one provider among several" is a test result rather than a claim.

**Interfaces — Produces:**
```python
class OpenCodeProvider(AgentProvider):
    id = "opencode"; name = "OpenCode"
    def __init__(self, server: OpenCodeServer, *, default_model: str | None = None)
    # plus a `_Handle` per session: {cursor: int, in_flight: bool, cwd: str, model: str|None}
```

- [ ] **Step 1: Write `backend/tests/test_opencode_provider.py`**

```python
"""OpenCode against the shared provider contract — the test that makes "the
AgentProvider abstraction works" a result rather than a claim, since the same
assertions already pass for the fake and for Claude Code.

Plus the two properties the cursor exists to give: a completed turn is
reported exactly once, and an event type we have never seen is ignored rather
than fatal (design spec section 2.1).

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from provider_contract import AgentProviderContract  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402


class _Base(unittest.IsolatedAsyncioTestCase):
    """Boots a fake OpenCode and a provider attached to it."""

    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.server = OpenCodeServer(self.fake.url, spawn=False)
        self.p = OpenCodeProvider(self.server)

    async def asyncTearDown(self):
        await self.p.shutdown()
        self.fake.__exit__(None, None, None)


class OpenCodeProviderContract(AgentProviderContract):
    """The shared contract, against a fake OpenCode server."""

    def make_provider(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.addCleanup(lambda: self.fake.__exit__(None, None, None))
        return OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))

    # supports_events is False, so the contract never calls _fire_event.


del AgentProviderContract   # discovery collects the base class otherwise


class Cursor(_Base):
    async def _session(self):
        from yuri.providers.base import ProjectContext, SessionOptions
        return await self.p.create_session(ProjectContext("p", "/tmp"), SessionOptions())

    async def test_a_completed_turn_is_reported_exactly_once(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.prompted")
        self.fake.state.push_assistant(h, "I did it.")
        self.fake.state.push_event(h, "session.next.step.completed")
        first = self.p.poll(h)
        self.assertEqual(first["status"], "completed")
        self.assertIn("I did it.", first["assistant_text"])
        # The cursor advanced: polling again must not re-report the turn.
        self.assertIn(self.p.poll(h)["status"], {"idle", "working"})

    async def test_an_unknown_event_type_is_ignored_not_fatal(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.some.type.nobody.mapped")
        res = self.p.poll(h)             # must not raise
        self.assertIn(res["status"], {"working", "idle"})

    async def test_a_failed_step_becomes_an_error_with_the_message(self):
        h = await self._session()
        self.p.send_message(h, "do it")
        self.fake.state.push_event(h, "session.next.step.failed",
                                   {"error": {"message": "HTTP 401: Model not supported"}})
        res = self.p.poll(h)
        self.assertEqual(res["status"], "error")
        self.assertIn("401", res["error"])

    async def test_working_while_a_turn_is_in_flight_then_idle(self):
        h = await self._session()
        self.assertEqual(self.p.poll(h)["status"], "idle")
        self.p.send_message(h, "do it")
        self.assertEqual(self.p.poll(h)["status"], "working")

    async def test_cost_comes_from_the_session(self):
        h = await self._session()
        self.fake.state.sessions[h]["cost"] = 0.25
        listed = {s["handle"]: s for s in self.p.list_native()}
        self.assertEqual(listed[h]["cost_usd"], 0.25)

    async def test_the_unsupported_surface_raises_notimplemented(self):
        h = await self._session()
        with self.assertRaises(NotImplementedError):
            await self.p.set_mode(h, "plan")
        with self.assertRaises(NotImplementedError):
            await self.p.send_keys(h, [{"key": "Escape"}])
        with self.assertRaises(NotImplementedError):
            self.p.run_slash(h, "/init")
        self.assertIsNone(await self.p.peek(h))
        self.assertIsNone(self.p.native_pane(h))
        self.assertIsNone(self.p.backend_of(h))

    async def test_stop_forgets_the_handle_without_deleting_the_session(self):
        h = await self._session()
        await self.p.stop(h)
        self.assertNotIn(h, {s["handle"] for s in self.p.list_native()})
        # The session still exists server-side: it is durable and not ours to delete.
        self.assertIn(h, self.fake.state.sessions)

    async def test_the_model_is_passed_when_configured(self):
        from yuri.providers.base import ProjectContext, SessionOptions
        p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False),
                             default_model="google/gemini-x")
        try:
            h = await p.create_session(ProjectContext("p", "/tmp"), SessionOptions())
            self.assertEqual(self.fake.state.sessions[h]["model"],
                             {"providerID": "google", "id": "gemini-x"})
        finally:
            await p.shutdown()

    async def test_an_unreachable_server_makes_health_offline_not_a_crash(self):
        p = OpenCodeProvider(OpenCodeServer("http://127.0.0.1:1", spawn=False))
        try:
            h = await p.health()
            self.assertFalse(h.online)
            self.assertTrue(h.detail)
        finally:
            await p.shutdown()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL (no `provider` module).

- [ ] **Step 3: Write `backend/yuri/providers/opencode/provider.py`.** Implement every `AgentProvider` method per the spec's §5 mapping table. The parts that carry the design:

```python
    def poll(self, handle: str) -> dict[str, Any]:
        """Advance the cursor and map what arrived.

        Synchronous by contract (the voice model polls; a turn can take
        minutes), but the work is HTTP — so it runs the coroutine on the
        provider's own loop the same way the rest of this class does.

        Two properties this must keep, both tested: the cursor advances only
        past events actually returned, so a completed turn is never reported
        twice; and an unrecognised event type advances the cursor without
        being fatal, because the successful-turn vocabulary is only partly
        known (design spec section 2.1).
        """
```

Guidance rather than a full transcription, since the shape depends on how you
bridge sync `poll`/`send_message` to async HTTP:

- **The sync/async bridge is the one real design choice here.** `send_message`,
  `answer`, `poll`, `list_native`, `run_slash` and `backend_of` are sync by
  contract; everything OpenCode offers is async HTTP. Pick one mechanism and
  use it everywhere: either (a) the provider owns a background thread with its
  own event loop and these methods submit coroutines to it and block briefly,
  or (b) it keeps a small `asyncio.run_coroutine_threadsafe` bridge onto the
  running loop. **Do not** call `asyncio.run()` inside a running loop — it
  raises. Whichever you choose, `send_message` must stay effectively
  non-blocking (`POST …/prompt` returns immediately with `admittedSeq`), and
  `poll` must not block longer than a normal HTTP round trip. Write down which
  you chose and why in the module docstring.
- `create_session`: `POST /api/session` with `{"location": {"directory": project.root_path}}`
  plus `{"model": {"providerID": p, "id": m}}` when a model is configured
  (parse `"provider/model"`). Register a `_Handle` with `cursor=0`,
  `cwd=project.root_path`.
- `send_message`: `POST /api/session/{h}/prompt` with
  `{"prompt": {"text": message}, "delivery": "queue"}`; set the handle's
  cursor to `admittedSeq - 1` if it is lower (so the admitted event itself is
  read back) and mark `in_flight`.
- `poll`: check pending permissions and questions **first** (§7 / Task 5 wires
  these; in this task return `working`/`idle`/`completed`/`error` only), then
  `GET /history?after=cursor`. Advance the cursor to the highest returned
  `durable.seq`. Decide the result in this order: a failed step → `error`; an
  assistant message with `finish` set → `completed` with its text (capped) and
  clear `in_flight`; else `working` if `in_flight` else `idle`. Always include
  `"session_id": handle`.
- `list_native`: `GET /api/session`, keep only registered handles, and shape
  each row like the other providers do — `handle`, `session_id`, `cwd` (from
  `location.directory`), `model`, `mode` (`""`), `status`, `cost_usd` (from
  `cost`), `queued` (0).
- `stop`: forget the handle. **Do not** call any delete endpoint — OpenCode
  sessions are durable and the user may resume one.
- `health`: `GET /api/session` through the server; `online=False` with the
  reason in `detail` when it raises, and note attached-vs-spawned. Cache it the
  way `ClaudeCodeProvider` does (30s) so a UI poll cannot hammer it.
- `set_mode` / `send_keys` / `run_slash` / `resume`: `NotImplementedError` with
  a message naming OpenCode, so the soft error the user hears is informative.
- `peek` / `native_pane` / `backend_of`: `None`.
- `set_observer`: store it; never call it.
- `shutdown`: forget all handles and `await server.release()` — which stops the
  process only if owned.
- `poll` on an unregistered handle: `KeyError` (the contract requires it).

- [ ] **Step 4: Finish `opencode/__init__.py`** with the `OpenCodeProvider` re-export from Task 2 Step 4.

- [ ] **Step 5: Run** `tests.test_opencode_provider tests.test_fake_provider tests.test_claude_provider -v` — all three provider suites green. That is the abstraction proven.

- [ ] **Step 6: Full suite** → OK, pristine.

- [ ] **Step 7: Commit**

```bash
git add backend/yuri/providers/opencode backend/tests/test_opencode_provider.py
git commit -m "$(cat <<'EOF'
feat(opencode): implement the provider contract with a durable cursor

The same AgentProviderContract that FakeAgentProvider and
ClaudeCodeProvider pass now passes for OpenCode against a fake server,
so "Claude Code is one provider among several" is a test result rather
than a claim.

Turn state comes from a per-handle durable.seq cursor polled from
/history?after=N, which gives two properties the tmux backend needs a
FIFO to approximate: a completed turn is reported exactly once, and an
event type nobody has mapped advances the cursor without being fatal —
necessary because a successful turn's vocabulary is only partly known
(design spec 2.1). Completion is therefore read from the message's
finish field rather than from an event type we have never seen.

stop() forgets the handle and deliberately does not delete the session:
OpenCode sessions are durable and the user may resume one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Permissions and questions

**Files:**
- Modify: `backend/yuri/providers/opencode/provider.py` (`poll` surfaces them, `answer` replies)
- Test: `backend/tests/test_opencode_permissions.py`

**The safety rule this task encodes:** `allow → "once"`, `deny → "reject"`, and **`always` is never sent.** `decide_permission` answers one question; `always` would turn a single spoken "yes" into a standing grant the user never agreed to. Granting standing permission is a mode change, and OpenCode exposes no mode.

OpenCode asks; **Yuri owns the workflow** (plan §20). Requests are surfaced in the `Prompt` shape the domain already understands, so `ApprovalService.record_request`, `risk_for` labelling, the one-pending-approval invariant and the whole voice approval flow work unchanged.

- [ ] **Step 1: Write `backend/tests/test_opencode_permissions.py`**

```python
"""OpenCode asks; Yuri owns the workflow. The rule with teeth: a spoken "yes"
maps to OpenCode's "once", never "always" — granting standing permission is a
mode change, not an answer to a question.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402


class Permissions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()
        self.p = OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))
        self.h = await self.p.create_session(ProjectContext("p", "/tmp"), SessionOptions())

    async def asyncTearDown(self):
        await self.p.shutdown()
        self.fake.__exit__(None, None, None)

    async def test_a_pending_permission_becomes_needs_permission(self):
        self.fake.state.add_permission(self.h, "req1", "run rm -rf build", tool="bash")
        res = self.p.poll(self.h)
        self.assertEqual(res["status"], "needs_permission")
        pr = res["prompt"]
        self.assertEqual(pr["kind"], "permission")
        self.assertIn("rm -rf build", pr["text"])
        self.assertEqual(pr["options"], ["allow", "deny"])
        # request_id must be OpenCode's, so the domain's dedup keys off it
        # rather than falling back to a synthesized id.
        self.assertEqual(pr["request_id"], "req1")

    async def test_a_pending_question_becomes_needs_choice(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web", "mobile"])
        res = self.p.poll(self.h)
        self.assertEqual(res["status"], "needs_choice")
        pr = res["prompt"]
        self.assertEqual(pr["kind"], "choice")
        self.assertIn("Which target?", pr["text"])
        self.assertEqual(pr["options"], ["web", "mobile"])
        self.assertEqual(pr["request_id"], "q1")

    async def test_allow_sends_once_and_never_always(self):
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.p.poll(self.h)
        self.p.answer(self.h, "allow")
        kind, sid, rid, body = self.fake.state.replies[-1]
        self.assertEqual((kind, sid, rid), ("permission", self.h, "req1"))
        self.assertEqual(body["reply"], "once")
        # THE RULE: one spoken yes must not grant a standing permission.
        self.assertNotEqual(body["reply"], "always")

    async def test_deny_sends_reject(self):
        self.fake.state.add_permission(self.h, "req1", "run rm -rf /")
        self.p.poll(self.h)
        self.p.answer(self.h, "deny")
        self.assertEqual(self.fake.state.replies[-1][3]["reply"], "reject")

    async def test_always_is_never_sent_for_any_phrasing(self):
        # Every phrasing decide_permission accepts as an allow must still be
        # "once". A provider that upgraded an enthusiastic yes to "always"
        # would be granting standing permission on the user's behalf.
        for phrasing in ("allow", "yes", "y", "sure", "ok", "approve",
                         "yes always", "always allow that"):
            self.fake.state.add_permission(self.h, f"r_{phrasing}", "run ls")
            self.p.poll(self.h)
            self.p.answer(self.h, phrasing)
            self.assertNotEqual(self.fake.state.replies[-1][3]["reply"], "always",
                                f"{phrasing!r} was upgraded to a standing grant")

    async def test_an_ambiguous_answer_is_refused_not_guessed(self):
        self.fake.state.add_permission(self.h, "req1", "run rm -rf /")
        self.p.poll(self.h)
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "hmm maybe")
        self.assertEqual(self.fake.state.replies, [],
                         "an ambiguous answer must not reach OpenCode at all")

    async def test_answering_a_question_uses_the_question_endpoint(self):
        self.fake.state.add_question(self.h, "q1", "Which target?", ["web", "mobile"])
        self.p.poll(self.h)
        self.p.answer(self.h, "web")
        kind, _, rid, body = self.fake.state.replies[-1]
        self.assertEqual((kind, rid), ("question", "q1"))
        self.assertIn("web", str(body))

    async def test_a_permission_takes_precedence_over_history(self):
        # A blocked turn must report the ask, not "working" — the user is the
        # only thing that can unblock it.
        self.fake.state.push_event(self.h, "session.next.prompted")
        self.fake.state.add_permission(self.h, "req1", "run ls")
        self.assertEqual(self.p.poll(self.h)["status"], "needs_permission")

    async def test_answering_with_nothing_pending_is_a_soft_error(self):
        with self.assertRaises(ValueError):
            self.p.answer(self.h, "allow")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement in `provider.py`.**

- In `poll`, **before** reading history: `GET /api/session/{h}/permission` and
  `…/question`. A pending permission → `{"status": "needs_permission", "prompt": {...}}`;
  a pending question → `needs_choice`. Remember the pending request's id and
  kind on the handle so `answer` knows where to reply.
- Build the prompt as the domain expects: `kind`, `text` (OpenCode's `title` for
  a permission, `text` for a question — clip it), `tool_name` (its `tool`),
  `tool_input` (its `metadata` if a dict, else `{}`), `options`
  (`["allow","deny"]` for a permission, OpenCode's own for a question),
  `request_id` (OpenCode's id), `multi_select` (`False`).
- `answer(handle, choice)`:
  - Nothing pending → `ValueError` (soft error; the model re-asks).
  - Pending **permission** → `decide_permission(choice)`; `None` → `ValueError`
    ("I couldn't tell if that means allow or deny"); `"allow"` →
    `POST …/permission/{rid}/reply {"reply": "once"}`; `"deny"` → `{"reply": "reject"}`.
    **Never `"always"`** — put that in a comment naming why.
  - Pending **question** → `POST …/question/{rid}/reply` with the chosen option
    (match `choice` case-insensitively against the options; fall back to
    sending the raw text, mirroring how the Claude path lets the user answer in
    their own words).
  - Clear the remembered pending id afterwards, and mark `in_flight` so the
    next `poll` reports `working`.

- [ ] **Step 4: Run** `tests.test_opencode_permissions tests.test_opencode_provider -v` → green, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add backend/yuri/providers/opencode/provider.py backend/tests/test_opencode_permissions.py
git commit -m "$(cat <<'EOF'
feat(opencode): surface permissions and questions through Yuri's flow

OpenCode asks; Yuri owns the workflow. Pending requests are surfaced in
the Prompt shape the domain already understands, so record_request, the
risk labelling, the one-pending-approval invariant and the voice
approval flow all work unchanged.

The rule with teeth: allow maps to OpenCode's "once" and deny to
"reject", and "always" is never sent for any phrasing — decide_permission
answers one question, and upgrading an enthusiastic yes to a standing
grant would be a mode change made on the user's behalf. An ambiguous
answer never reaches OpenCode at all.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Rehydration and the cursor's durability

**Files:**
- Modify: `backend/yuri/providers/opencode/provider.py` (`rehydrate`, cursor export/import)
- Test: `backend/tests/test_opencode_rehydrate.py`

OpenCode sessions are durable server-side, so unlike an SDK session they can be re-adopted after a Yuri restart. The cursor must come back with them, or she re-narrates history.

**Two rules:**
1. **A session the server has but Yuri has no row for is left alone.** It may be the user's own OpenCode work; adopting it would put Yuri in charge of something she was never asked to run.
2. **The cursor is restored from `runtime_metadata`**, which `SessionService` already persists per row — no schema change.

- [ ] **Step 1: Write `backend/tests/test_opencode_rehydrate.py`**

```python
"""OpenCode sessions outlive Yuri, so they can be re-adopted — but only the
ones she was actually running, and only with their cursor intact, or she
re-narrates history the user already heard.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.base import ProjectContext, SessionOptions  # noqa: E402
from yuri.providers.opencode.provider import OpenCodeProvider  # noqa: E402
from yuri.providers.opencode.server import OpenCodeServer  # noqa: E402


class Rehydrate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeOpenCode()
        self.fake.__enter__()

    async def asyncTearDown(self):
        self.fake.__exit__(None, None, None)

    def _provider(self):
        return OpenCodeProvider(OpenCodeServer(self.fake.url, spawn=False))

    async def test_a_known_session_is_readopted_with_its_cursor(self):
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(5):
            self.fake.state.push_event(sid, "session.next.prompted")
        p = self._provider()
        try:
            restored = await p.rehydrate(known={sid: {"opencode_cursor": 5,
                                                      "cwd": "/tmp/proj"}})
            self.assertEqual([r["handle"] for r in restored], [sid])
            # The cursor came back: the five old events must not be re-read.
            self.assertIn(p.poll(sid)["status"], {"idle", "working"})
            self.assertEqual(p.cursor_for(sid), 5)
        finally:
            await p.shutdown()

    async def test_a_restored_session_without_a_cursor_starts_from_now_not_zero(self):
        # Re-reading from 0 would re-narrate everything the user already heard.
        sid = self.fake.state.new_session("/tmp/proj")
        for _ in range(3):
            self.fake.state.push_event(sid, "session.next.prompted")
        p = self._provider()
        try:
            await p.rehydrate(known={sid: {"cwd": "/tmp/proj"}})
            self.assertEqual(p.cursor_for(sid), 3)
        finally:
            await p.shutdown()

    async def test_a_session_yuri_never_ran_is_left_alone(self):
        mine = self.fake.state.new_session("/tmp/proj")
        theirs = self.fake.state.new_session("/tmp/their-own-work")
        p = self._provider()
        try:
            restored = await p.rehydrate(known={mine: {"cwd": "/tmp/proj"}})
            handles = [r["handle"] for r in restored]
            self.assertIn(mine, handles)
            self.assertNotIn(theirs, handles, "adopted a session Yuri never ran")
            self.assertNotIn(theirs, {s["handle"] for s in p.list_native()})
        finally:
            await p.shutdown()

    async def test_a_vanished_session_is_simply_not_restored(self):
        p = self._provider()
        try:
            restored = await p.rehydrate(known={"ses_gone": {"cwd": "/tmp/proj"}})
            self.assertEqual(restored, [])
            # SessionService marks the row lost; the provider just does not claim it.
            self.assertNotIn("ses_gone", {s["handle"] for s in p.list_native()})
        finally:
            await p.shutdown()

    async def test_an_unreachable_server_rehydrates_to_nothing_without_raising(self):
        p = OpenCodeProvider(OpenCodeServer("http://127.0.0.1:1", spawn=False))
        try:
            self.assertEqual(await p.rehydrate(known={"ses_x": {}}), [])
        finally:
            await p.shutdown()

    async def test_the_cursor_is_exported_for_persistence(self):
        sid = self.fake.state.new_session("/tmp/proj")
        p = self._provider()
        try:
            from yuri.providers.base import ProjectContext, SessionOptions
            h = await p.create_session(ProjectContext("p", "/tmp/proj"), SessionOptions())
            p.send_message(h, "go")
            p.poll(h)
            meta = p.runtime_metadata_for(h)
            self.assertIn("opencode_cursor", meta)
            self.assertGreaterEqual(meta["opencode_cursor"], 1)
        finally:
            await p.shutdown()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.**

- `rehydrate(known: dict[str, dict] | None = None) -> list[dict]`. Widen the
  signature with a default so the base class and `ClaudeCodeProvider` stay
  compatible.
- **`SessionService.rehydrate` must be changed to pass `known`** — verified: it
  currently calls `await p.rehydrate()` with no arguments (`sessions.py:709`).
  Change that call to build, per provider, a map of the rows it already has:

```python
                known = {r.native_session_id: dict(r.runtime_metadata or {},
                                                   cwd=r.working_directory)
                         for r in self.store.sessions.list()
                         if r.agent_id == p.id}
                restored.extend(await p.rehydrate(known=known))
```

  Keep it inside the existing `try/except` so a provider that raises still only
  logs. `ClaudeCodeProvider.rehydrate` must keep working — give it a
  `**_ignored` or a matching optional parameter rather than changing its
  behavior, and confirm `tests/test_claude_provider.py` and
  `tests/test_session_service.py` stay green **unchanged**.
- With no `known`, rehydrate nothing (there is nothing to re-adopt into) and
  return `[]`.
- Otherwise `GET /api/session`, and for each id present in both the server's
  list and `known`: register a handle with `cursor` from
  `known[id]["opencode_cursor"]` if present, else **the session's current
  highest `durable.seq`** (never 0 — that would re-narrate history), and `cwd`
  from `known[id]["cwd"]` or the session's `location.directory`.
- An unreachable server logs and returns `[]` rather than raising, so a dead
  OpenCode cannot break Yuri's startup (plan §41).
- Add `cursor_for(handle) -> int` and `runtime_metadata_for(handle) -> dict`
  (returning `{"opencode_cursor": …}`) so the cursor can be persisted and
  asserted. Have `list_native` include `runtime_metadata` in each row if that
  is how `SessionService` picks it up — check, and match.

- [ ] **Step 4: Run** the OpenCode suites plus `tests.test_session_service` (the
  rehydrate call site) → green, then the full suite.

- [ ] **Step 5: Commit**

```bash
git add backend/yuri/providers/opencode/provider.py backend/tests/test_opencode_rehydrate.py
git commit -m "$(cat <<'EOF'
feat(opencode): re-adopt durable sessions with their cursor intact

OpenCode sessions outlive Yuri, so unlike an SDK session they can be
re-adopted after a restart — but only the ones she was actually running.
A session the server has and Yuri has no row for is left alone: it may
be the user's own OpenCode work, and adopting it would put her in charge
of something she was never asked to run.

The cursor comes back from runtime_metadata, which SessionService
already persists per row, so no schema change. A restored session with
no stored cursor starts from the session's current sequence rather than
zero, because re-reading from zero would re-narrate everything the user
already heard. An unreachable server rehydrates to nothing rather than
raising, so a dead OpenCode cannot break startup.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Registry, config, doctor, and teaching Yuri that OpenCode exists

**Files:**
- Modify: `backend/config.py`, `backend/yuri/providers/registry.py`, `backend/yuri/doctor.py`, `frontend/lib/operating.ts`
- Test: `backend/tests/test_registry.py` (extend), `backend/tests/test_doctor.py` (extend)

- [ ] **Step 1: Config.** Add beside the existing `YURI_*` keys in `backend/config.py`:

```python
# --- OpenCode provider ------------------------------------------------------
# Yuri attaches to a server already answering at OPENCODE_URL; only when
# nothing answers does she spawn one, and she only ever stops a server she
# started. OPENCODE_SPAWN=0 makes her attach-only, for a user who wants to own
# the process. OPENCODE_SERVER_PASSWORD is never logged.
OPENCODE_URL: str = (os.getenv("OPENCODE_URL") or "http://127.0.0.1:4096").strip()
OPENCODE_SPAWN: bool = _env_bool("OPENCODE_SPAWN", True)
OPENCODE_BIN: str = (os.getenv("OPENCODE_BIN") or "opencode").strip()
OPENCODE_SERVER_PASSWORD: str = (os.getenv("OPENCODE_SERVER_PASSWORD") or "").strip()
OPENCODE_MODEL: str = (os.getenv("OPENCODE_MODEL") or "").strip()
```

Add the same keys to `backend/.env.example` with a comment block explaining
attach-vs-spawn and that OpenCode is optional. Do **not** add
`OPENCODE_SERVER_PASSWORD`'s value anywhere, and confirm `config.summary()`
does not print it (it prints names and sources only — verify).

- [ ] **Step 2: Registry.** In `build_registry`, add an `opencode` branch that
constructs `OpenCodeProvider(OpenCodeServer(config.OPENCODE_URL, spawn=…, binary=…, password=…, cwd=<first allowed root>, log_path=<~/Yuri/opencode.log>), default_model=config.OPENCODE_MODEL or None)`, and add `"opencode"` to `KNOWN`. Import lazily inside the branch, matching how the Claude branch does it, so a registry without OpenCode never imports httpx-dependent code paths it does not need.

- [ ] **Step 3: Extend `backend/tests/test_registry.py`**

```python
    async def test_opencode_registers_when_asked(self):
        reg = build_registry("claude-code,opencode", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["claude-code", "opencode"])

    async def test_claude_code_is_still_the_first_and_default(self):
        # Adding a provider must not change which agent an unqualified request
        # gets. AgentRouter's fallback is the container default, and the
        # container's default is claude-code.
        reg = build_registry("claude-code,opencode", claude_factory=lambda b: None)
        self.assertEqual(reg.ids()[0], "claude-code")

    async def test_opencode_alone_is_allowed(self):
        reg = build_registry("opencode", claude_factory=lambda b: None)
        self.assertEqual(reg.ids(), ["opencode"])

    async def test_registering_opencode_does_not_touch_the_network(self):
        # Construction must be lazy: no server is acquired until a session is
        # started, so a registry build cannot hang on a dead OpenCode.
        reg = build_registry("opencode", claude_factory=lambda b: None)
        p = reg.get("opencode")
        self.assertIsNone(p.server.client)
```

- [ ] **Step 4: `yuri doctor`.** Add an OpenCode line, in the shape the existing
checks use: report the binary (found/not on PATH), the URL, and whether it is
reachable now — labelling `attached`, `spawnable`, or `unavailable`. **OpenCode
is optional**, so an unavailable OpenCode must **not** make `doctor` exit
non-zero unless `YURI_AGENTS` actually includes `opencode`. Add a test for both
directions (in `YURI_AGENTS` → counts; not in it → informational only).

- [ ] **Step 5: Teach the voice model.** Add to `frontend/lib/operating.ts`:

```
- AGENTS: more than one coding agent may be available. The AGENTS list in your context (from /yuri/context at connect) tells you which are online — there is no tool to re-check them, so if the user asks mid-conversation, answer from that list and say it was accurate at connect. Claude Code is the default; OpenCode is a second agent with its own strengths. "Use OpenCode", "have OpenCode do it", "switch to OpenCode" means passing agent="opencode" to start_session. Do NOT silently switch agents: if the user asks for one that was offline, say so and offer the one that is up. OpenCode has no permission modes, so set_mode fails on an OpenCode session — say that plainly rather than retrying, and note it still asks for permission the normal way.
```

**Verified: there is no `list_agents` voice tool**, which is why the bullet
points at the context's AGENTS list instead. Do not add one in this task — a
new voice tool is its own change with its own result-key contract. If you think
one is warranted, say so in your report as a follow-up.

- [ ] **Step 6: Run** the full backend suite, `npm test`, `npx tsc --noEmit`.

- [ ] **Step 7: Commit**

```bash
git add backend/config.py backend/.env.example backend/yuri/providers/registry.py backend/yuri/doctor.py frontend/lib/operating.ts backend/tests/test_registry.py backend/tests/test_doctor.py
git commit -m "$(cat <<'EOF'
feat(opencode): register the provider, and teach Yuri it exists

Config, registry, doctor and the voice prompt. OpenCode is optional:
registering it acquires no server (so a registry build cannot hang on a
dead one), and an unavailable OpenCode only fails `yuri doctor` when
YURI_AGENTS actually asks for it.

claude-code remains first and default, pinned by a test — adding a
provider must not change which agent an unqualified request gets. The
prompt tells Yuri not to switch agents silently, and that set_mode does
not apply to an OpenCode session.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Live check against the real OpenCode

Not automatable, and the only place the remaining unknowns close. Record results
in `docs/superpowers/plans/2026-09-03-yuri-opencode-verification.md`.

**This is where the event vocabulary gets discovered.** Spec §2.1 is explicit
that a successful turn's types were never observed. Capture them here and add
them to the mapping if they change anything.

Setup — pick a model that actually works on this machine (`opencode models`;
four providers are authenticated, and the probe's free models 401'd):

```bash
export YURI_AGENTS=claude-code,opencode
export OPENCODE_MODEL=<provider/model that works>
cd backend && ./run.sh
```
```bash
cd frontend && npm run dev
```

- [ ] **Attach beats spawn.** Start `opencode serve --port 4096` yourself first,
  then start Yuri. `yuri doctor` should say attached. **Stop Yuri and confirm
  your server is still running** — this is the rule the whole lifecycle exists
  for, and the automated test only proves it against a fake.
- [ ] **Spawn works.** With no server running, start Yuri and say "start a
  session in <project> using OpenCode". She should spawn one, and stopping Yuri
  should stop it. Check `~/Yuri/opencode.log` has output.
- [ ] **A real turn.** "Have OpenCode read note.txt and tell me what it says."
  Watch for: the mission being created, narration of the turn's completion, and
  the assistant text being reported honestly (quoted, not claimed as verified).
- [ ] **Capture the event vocabulary.** While a turn runs,
  `curl -s localhost:4096/api/session/<sid>/history?after=0 | python3 -m json.tool | grep '"type"'`
  and record every type seen. **Compare against the mapping** — anything
  unmapped that carries meaning is a follow-up (or a one-line table entry).
- [ ] **A real permission.** Ask for something that needs approval ("have
  OpenCode delete note.txt"). Confirm she asks, that "deny" reaches OpenCode as
  `reject`, and that "allow" reaches it as `once` — check with
  `curl -s localhost:4096/api/session/<sid>/permission`.

  **Two shapes task 5 could not prove against a fake, both to settle here:**
  1. *Does replying actually drop the request from the pending list?* Reply,
     then `curl` that endpoint again. Task 5's staleness pre-check assumes it
     does. If the request drops EARLIER than expected the failure is benign —
     a false stale-refusal, heard as one extra re-ask. The other direction is
     the one to watch: if OpenCode never removes an answered request, the next
     poll re-surfaces the same `request_id` and the user hears the same
     question forever instead of the turn moving to `working`. Nothing unsafe
     is granted either way, but it would look like a hang.
  2. *The question reply body shape.* Task 5 sends `{"reply": …}` to
     `…/question/{id}/reply` purely by symmetry with the permission reply —
     never observed. Answer a real question and check the server accepts it;
     the guess is isolated to one line in `_question_reply` if it is wrong.
- [ ] **Restart with a live session.** Stop and restart Yuri mid-session;
  confirm the session is re-adopted and that she does **not** re-narrate
  anything already heard.
- [ ] **Auth.** Start a server with `OPENCODE_SERVER_PASSWORD=x`, set the same
  in Yuri's env, and confirm she connects — and that she reports offline with a
  wrong password. **This is the one config key the spec admits is unproven
  (§9)**; if the header mechanism in `client._headers()` is wrong, fix it there
  and note it.
- [ ] **Nothing regressed.** A Claude session still works end to end alongside
  an OpenCode one: connect, start, tell, permission, answer, close. Both appear
  in `list_sessions` with the right `agent_id`.
- [ ] **Isolation.** Kill the OpenCode server mid-session. Claude sessions,
  narration, missions and the UI must all keep working; OpenCode should report
  offline (plan §41).
- [ ] **Final:** full backend suite OK and pristine in both `~/Yuri` states;
  `npm test`; `npx tsc --noEmit`. Report anything unmapped, anything spoken
  wrongly, and whether the auth mechanism held.
