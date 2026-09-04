"""Daily journal (spec §5.5): one append-only markdown file per day so Yuri can
answer "what happened yesterday?" from her own records."""
from __future__ import annotations

import datetime
import os

from yuri.home import Home
from yuri.services._util import _tail

# Prefixes of journal lines that are state-machine bookkeeping rather than
# something the user would recognise as an event in their day. Matched on the
# text each service actually writes (the journal.append calls in
# yuri/services/*.py). A new line format that should be hidden has to be added
# here, and the failure mode of missing one is a slightly noisier day, not a
# wrong one — which is the right direction for a filter to fail in.
_MACHINERY = ("mission created:", "mission deleted:", "mission '",
              "turn completed in '", "session '", "workflow ", "task '")


def _is_machinery(line: str) -> bool:
    """True for a "- HH:MM  <text>" line whose text is bookkeeping."""
    body = line[2:].lstrip()
    if " " in body:                      # drop the HH:MM stamp
        body = body[body.index(" ") + 1:].lstrip()
    return body.startswith(_MACHINERY)


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

    def read_today_personal(self, cap: int = 2000) -> str:
        """Today's journal with the machinery filtered out.

        The journal is mostly bookkeeping — mission transitions, turn
        completions, sessions lost across a restart. Handing all of it to Yuri
        as "today so far" is why her day reads as a status report: there is
        nothing in it but work, so there is nothing else for her to mention.

        This keeps the lines a person would recognise as something that
        happened (an approval they answered, something she remembered, a note
        she was asked to keep) and drops the state-machine noise. It is a
        FILTER, not a second journal: nothing stops being recorded, and
        read_today still returns everything.
        """
        raw = self.read_today(cap=100_000)
        if not raw:
            return ""
        kept = [ln for ln in raw.splitlines()
                if not ln.startswith("- ") or not _is_machinery(ln)]
        return _tail("\n".join(kept), cap)
