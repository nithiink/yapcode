"""A stdlib fake of OpenCode's /api/* surface, shaped from the live probe
recorded in the design spec section 2 — envelope, ids, event and message
shapes. Lets every OpenCode test run with no real server, no network and no
credentials, which is what makes the provider's contract suite meaningful.

Only the endpoints the provider actually calls are implemented; anything else
404s, so a provider reaching for an unspecified endpoint fails loudly in tests
rather than silently in production.
"""
from __future__ import annotations

import base64
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit
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
        """HTTP Basic, exactly as the real server does it.

        Measured against `opencode serve` with OPENCODE_SERVER_PASSWORD set:
        no header -> 401, `x-opencode-password` -> 401, Basic with an empty
        username -> 401, Basic as opencode:<pw> -> 200. The fake enforced the
        header form until Task 8 measured it, which is precisely the kind of
        agreeable-fake bug that makes a suite lie.
        """
        want = self.state.require_password
        if not want:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(
                header[6:].strip()).decode().partition(":")
        except Exception:
            return False
        return bool(user) and password == want

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

    @staticmethod
    def _int_param(query: dict[str, str], name: str, default: int) -> int | None:
        """An absent or blank param means `default`; garbage means None (-> 400).

        A malformed value is the caller's bug, so it has to be loud -- but loud
        as a 400 the client raises on, not as a traceback from a daemon thread
        that would break the suite's pristine output.
        """
        raw = query.get(name, "").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            return None

    # --- routes ---------------------------------------------------------
    def do_GET(self):                       # noqa: N802
        if not self._pre("GET"):
            return
        split = urlsplit(self.path)
        s, path = self.state, split.path
        # keep_blank_values so `?after=` (what httpx emits for None or "") parses
        # to "" rather than vanishing -- _int_param below turns it into the default.
        query = {k: v[-1] for k, v in
                 parse_qs(split.query, keep_blank_values=True).items()}
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
                after = self._int_param(query, "after", 0)
                if after is None:
                    return self._bad("Expected a number", "after")
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


class _FastBindHTTPServer(HTTPServer):
    """HTTPServer.server_bind() calls socket.getfqdn(host) unconditionally,
    which does a reverse-DNS lookup nothing here needs — the fake only ever
    binds to 127.0.0.1 and is addressed by fake.url, never by server_name.
    In a network-restricted sandbox that lookup can hang for tens of seconds
    (observed here), turning every test process's first fake server into a
    ~35s stall. Skip it."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class FakeOpenCode:
    """Context manager: `with FakeOpenCode() as fake: fake.url, fake.state`."""

    def __init__(self) -> None:
        self.state = FakeOpenCodeState()
        handler = type("_H", (_Handler,), {"state": self.state})
        self._srv = _FastBindHTTPServer(("127.0.0.1", 0), handler)
        # poll_interval is what shutdown() waits on, and __exit__ blocks the
        # event loop for it. The 0.5s default showed up as an asyncio
        # slow-callback warning per fake-using test, plus real wall clock.
        self._thread = threading.Thread(
            target=lambda: self._srv.serve_forever(poll_interval=0.01), daemon=True)

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
