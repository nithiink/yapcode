"""Daily journal (spec §5.5): one append-only markdown file per day so Yuri can
answer "what happened yesterday?" from her own records."""
from __future__ import annotations

import datetime
import os

from yuri.home import Home
from yuri.services._util import _tail


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
