"""Structured memory, file-backed (spec §5.6): dated lines appended to
memory/user.md or memory/projects/<slug>.md. Deliberately no schema — the user
can read and edit what Yuri knows in a text editor. Path segments come only
from a validated slug, never from spoken text."""
from __future__ import annotations

import datetime
import os
import re

from yuri.home import Home
from yuri.services._util import _tail

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,64}$")


class BadSlug(ValueError):
    pass


class Memory:
    def __init__(self, home: Home):
        self.home = home

    def _project_path(self, slug: str) -> str:
        if not _SLUG_RE.match(slug or ""):
            raise BadSlug(f"invalid project slug {slug!r} (lowercase letters, digits, dashes)")
        path = os.path.join(self.home.projects_memory_dir, f"{slug}.md")
        # Containment check compares realpath'd copies only (mirrors
        # tmux_runner.py's ctrl-dir check) — path itself is returned unresolved
        # so callers see a stable, non-symlink-rewritten path.
        real = os.path.realpath(path)
        root = os.path.realpath(self.home.memory_dir)
        if real != root and not real.startswith(root + os.sep):
            raise BadSlug("memory path escaped memory/")  # belt and braces
        return path

    def remember(self, fact: str, project_slug: str | None = None) -> str:
        fact = " ".join(str(fact or "").split())
        if not fact:
            raise ValueError("nothing to remember")
        # `is None` (not truthiness): an explicit "" must still go through
        # _project_path so it hits the regex and raises BadSlug, rather than
        # silently falling back to user memory like an omitted slug would.
        path = self.home.user_memory_path if project_slug is None else self._project_path(project_slug)
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
