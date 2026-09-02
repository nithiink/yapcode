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
