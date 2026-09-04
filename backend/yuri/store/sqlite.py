"""stdlib sqlite3 implementation of the repositories. One connection per
thread (threading.local) so FastAPI's threadpool workers never share a
connection; WAL so readers don't block the writer."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from dataclasses import fields
from typing import Any, Iterable

from yuri.domain.approval import Approval
from yuri.domain.event import YuriEvent
from yuri.domain.mission import Mission, MissionStep
from yuri.domain.artifact import Artifact
from yuri.domain.project import Project
from yuri.domain.session import LIVE_STATUSES, AgentSession
from yuri.domain.specialist import Specialist
from yuri.domain.task import Task
from yuri.domain.workflow import LIVE_WORKFLOW, Workflow
from .base import (ApprovalRepo, ArtifactRepo, EventRepo, LiveSessionExists, MissionRepo,
                   PendingApprovalExists, ProjectRepo, SessionRepo, SettingsRepo,
                   SpecialistRepo, Store, TaskRepo, WorkflowRepo)

log = logging.getLogger("yuri.store.sqlite")

SCHEMA_VERSION = 4
_MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")

# Columns serialised as JSON in both directions by _to_row/_from_row. Register
# EVERY list/tuple column: an unregistered one is passed to sqlite3 raw, and
# while a tuple raises there, _to_row's `json.dumps(v, default=str)` fallback
# means a registered-but-wrong type is stored as a Python repr with no error.
_JSON_COLS = {"metadata", "result", "runtime_metadata", "tool_input", "payload",
              "tools", "capabilities", "requires", "verification"}
# Columns stored as 0/1 and read back as real bools. Miss one and
# `if row.read_only:` works while `row.read_only is True` does not.
_BOOL_COLS = {"auto_approve_edits", "speakable", "builtin", "archived", "read_only"}


class _Conn:
    """Per-thread connection factory.

    WHY `check_same_thread=False` when the whole point is one connection per
    thread: `threading.local` already guarantees a connection is only ever
    *used* by the thread that opened it — the flag exists purely so
    `close_all()` can close them. sqlite's same-thread assertion covers
    `close()` too, so with the default (True) closing a worker's connection
    from the thread that tears the store down raises `ProgrammingError` and the
    connection stays open: no WAL checkpoint on a clean shutdown, and (until
    the GC finalizes it) an `unclosed database` ResourceWarning. "Let each
    thread close its own" is not implementable here — the event bus persists
    via `asyncio.to_thread`, so its connection lives on a thread in asyncio's
    default executor that we get no shutdown hook on.
    """

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def get(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=10.0, isolation_level=None,
                                  check_same_thread=False)
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
                    # Never swallow this silently: a connection that would not
                    # close is a connection whose WAL was not checkpointed.
                    log.warning("sqlite: closing a connection to %s failed", self.path,
                                exc_info=True)
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

    def delete(self, id):
        """Remove a mission and everything owned by it.

        Owned means: the row's mission_id is NOT NULL, so it cannot outlive
        the mission even in principle, and it means nothing on its own — a
        task graph with no mission is not history, it is a dangling plan.
        Contrast `sessions`, whose mission_id is nullable and which
        MissionService detaches instead, because a session row records an
        agent run that really happened.

        Children first, deepest first, in ONE transaction. Migration 0003
        added workflows/tasks/artifacts pointing at missions(id) and this
        method was not updated, so from Phase 7 onwards deleting any mission
        that had a plan raised IntegrityError and the API answered 500. A
        partial delete would be worse than the 500: the next attempt would
        fail somewhere else, with the mission half gone.
        """
        # This mission's tasks, named once and reused: every clause below has
        # to clear EVERY reference to them, not just the expected one, or the
        # `DELETE FROM tasks` fails on a foreign key and the whole thing
        # rolls back into another 500.
        mine = """SELECT t.id FROM tasks t JOIN workflows w ON t.workflow_id = w.id
                  WHERE w.mission_id = ?"""
        con = self._c.get()
        with con:
            # Both columns of task_deps reference tasks(id), so both sides of
            # an edge count as a reference.
            con.execute(f"DELETE FROM task_deps WHERE task_id IN ({mine}) "
                        f"OR depends_on IN ({mine})", (id, id))
            # Artifacts before tasks, since artifacts.task_id references
            # tasks(id) — by mission (the NOT NULL column) and by task, which
            # are the same set unless something has gone wrong upstream.
            con.execute(f"DELETE FROM artifacts WHERE mission_id = ? OR task_id IN ({mine})",
                        (id, id))
            con.execute(
                """DELETE FROM tasks WHERE workflow_id IN
                     (SELECT id FROM workflows WHERE mission_id = ?)""", (id,))
            con.execute("DELETE FROM workflows WHERE mission_id = ?", (id,))
            # Drained by 0003, kept because an older database may still hold rows.
            con.execute("DELETE FROM mission_steps WHERE mission_id = ?", (id,))
            con.execute("DELETE FROM missions WHERE id = ?", (id,))


class SqliteSessions(_Base, SessionRepo):
    table, cls = "sessions", AgentSession

    def detach_mission(self, mission_id):
        self._c.get().execute(
            "UPDATE sessions SET mission_id = NULL WHERE mission_id = ?", (mission_id,))

    def insert(self, row):
        try:
            super().insert(row)
        except sqlite3.IntegrityError as exc:
            # sessions_one_live (migration 0002). sqlite names the index in
            # some builds and the column in others, so match either -- and
            # re-raise anything else untouched rather than mislabelling a
            # different constraint as this one.
            if "sessions_one_live" in str(exc) or "sessions.native_session_id" in str(exc):
                raise LiveSessionExists(
                    f"a live session row already exists for {row.native_session_id}") from exc
            raise

    def get_by_native(self, native_id):
        return self._one("SELECT * FROM sessions WHERE native_session_id = ? "
                         "ORDER BY started_at DESC LIMIT 1", (native_id,))

    def list(self, mission_id=None, live_only=False):
        where, args = [], []
        if mission_id:
            where.append("mission_id = ?")
            args.append(mission_id)
        if live_only:
            # The domain's list, not a copy: 0002's partial unique index is
            # written against these same statuses, and a drift between the
            # three would make "one live row per handle" quietly stop meaning
            # what live_rows() returns.
            live = tuple(sorted(LIVE_STATUSES))
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


class SqliteSpecialists(_Base, SpecialistRepo):
    table, cls = "specialists", Specialist

    # get_by_* deliberately exclude archived rows: their whole purpose is
    # "can this name/slug be used", and an archived row frees both.
    def get_by_slug(self, slug):
        return self._one("SELECT * FROM specialists WHERE slug = ? AND archived = 0", (slug,))

    def get_by_name(self, name):
        return self._one("SELECT * FROM specialists WHERE name = ? AND archived = 0", (name,))

    def list(self, include_archived=False):
        if include_archived:
            return self._many("SELECT * FROM specialists ORDER BY role, name")
        return self._many(
            "SELECT * FROM specialists WHERE archived = 0 ORDER BY role, name")


class SqliteWorkflows(_Base, WorkflowRepo):
    table, cls = "workflows", Workflow

    def for_mission(self, mission_id, live_only=False):
        if live_only:
            marks = ",".join("?" * len(LIVE_WORKFLOW))
            return self._many(
                f"SELECT * FROM workflows WHERE mission_id = ? AND status IN ({marks}) "
                "ORDER BY version DESC", (mission_id, *LIVE_WORKFLOW))
        return self._many(
            "SELECT * FROM workflows WHERE mission_id = ? ORDER BY version DESC",
            (mission_id,))

    def live(self):
        marks = ",".join("?" * len(LIVE_WORKFLOW))
        return self._many(
            f"SELECT * FROM workflows WHERE status IN ({marks}) ORDER BY created_at",
            LIVE_WORKFLOW)


class SqliteTasks(_Base, TaskRepo):
    table, cls = "tasks", Task

    def for_workflow(self, workflow_id):
        return self._many(
            "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY ordinal", (workflow_id,))

    def add_dep(self, task_id, depends_on):
        if task_id == depends_on:
            raise ValueError(f"task {task_id[:8]} cannot depend on itself")
        # OR IGNORE: adding the same edge twice is a no-op, not an error. The
        # engine re-derives edges on retry and must not have to check first.
        self._c.get().execute(
            "INSERT OR IGNORE INTO task_deps (task_id, depends_on) VALUES (?, ?)",
            (task_id, depends_on))

    def deps_for(self, workflow_id):
        rows = self._c.get().execute(
            "SELECT d.task_id, d.depends_on FROM task_deps d "
            "JOIN tasks t ON t.id = d.task_id WHERE t.workflow_id = ?", (workflow_id,))
        out: dict[str, set[str]] = {}
        for task_id, depends_on in rows:
            out.setdefault(task_id, set()).add(depends_on)
        return out

    def holders_of(self, specialist_id, live_only=True):
        if live_only:
            return self._many(
                "SELECT * FROM tasks WHERE specialist_id = ? "
                "AND status NOT IN ('completed','skipped','cancelled')", (specialist_id,))
        return self._many("SELECT * FROM tasks WHERE specialist_id = ?", (specialist_id,))


class SqliteArtifacts(_Base, ArtifactRepo):
    table, cls = "artifacts", Artifact

    def for_mission(self, mission_id):
        return self._many(
            "SELECT * FROM artifacts WHERE mission_id = ? ORDER BY created_at", (mission_id,))

    def for_task(self, task_id):
        return self._many(
            "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at", (task_id,))


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
        # newest `limit` rows, returned oldest-first. The inner SELECT * does
        # not project sqlite's implicit rowid, so the outer ORDER BY can't
        # see it unless the inner query names it explicitly; the extra
        # "rowid" column that reaches _from_row is harmless because
        # from_dict() ignores keys it doesn't recognize.
        sql = f"SELECT * FROM (SELECT *, rowid FROM events{clause} ORDER BY ts DESC, rowid DESC LIMIT ?) " \
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
        self.specialists = SqliteSpecialists(self._conn)
        self.workflows = SqliteWorkflows(self._conn)
        self.tasks = SqliteTasks(self._conn)
        self.artifacts = SqliteArtifacts(self._conn)

    def migrate(self) -> None:
        # 0002's partial unique index hardcodes the live statuses -- sqlite
        # cannot import a Python constant. Fail loudly here if someone adds a
        # status to LIVE_STATUSES without touching the index, rather than
        # letting "one live row per handle" silently stop covering it.
        expected = {"starting", "running", "needs_permission", "needs_choice", "idle"}
        if set(LIVE_STATUSES) != expected:
            raise RuntimeError(
                "LIVE_STATUSES changed but migrations/0002_one_live_session_per_handle.sql "
                f"still indexes {sorted(expected)}; add a migration that rebuilds "
                "sessions_one_live for the new set")
        # Same guard for 0003's workflows_one_live: the statuses are hardcoded
        # in SQL because sqlite cannot import a Python constant, so adding one
        # to LIVE_WORKFLOW must not silently leave it outside the index.
        expected_wf = {"draft", "running", "paused", "waiting_for_human"}
        if set(LIVE_WORKFLOW) != expected_wf:
            raise RuntimeError(
                "LIVE_WORKFLOW changed but migrations/0003_workflows_and_roster.sql "
                f"still indexes {sorted(expected_wf)}; add a migration that rebuilds "
                "workflows_one_live for the new set")
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
