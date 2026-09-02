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
        try:
            store.migrate()
            v = store.settings.get("schema_version")
        finally:
            store.close()
        ok &= _line(v == SCHEMA_VERSION, "database", f"{home.db_path} (schema v{v})")
    except Exception as exc:
        ok &= _line(False, "database", str(exc))

    # config.allowed_project_roots() always appends Yuri's own home once it
    # exists on disk (independent of ALLOWED_PROJECT_ROOTS), and home.ensure()
    # above has just created it — so `roots` itself is never empty and can't
    # be used as the pass/fail signal here. What actually matters for this
    # check is whether there is any allowed root OTHER than Yuri's home: if
    # not, real project sessions have nowhere to start, even though the
    # effective-roots list looks non-empty. Report the raw configuration
    # alongside the effective roots so both are visible.
    raw_roots = (os.getenv("ALLOWED_PROJECT_ROOTS") or "").strip()
    roots = config.allowed_project_roots()
    home_real = os.path.realpath(home.path)
    project_roots = [r for r in roots if r != home_real]
    if project_roots:
        ok &= _line(True, "allowed roots", ", ".join(roots))
    elif raw_roots:
        ok &= _line(False, "allowed roots",
                     f"ALLOWED_PROJECT_ROOTS={raw_roots!r} resolves to nothing outside "
                     f"Yuri's own home ({home_real}); only her home is reachable — fix "
                     f"ALLOWED_PROJECT_ROOTS in backend/.env")
    else:
        ok &= _line(False, "allowed roots",
                     f"ALLOWED_PROJECT_ROOTS is not set — only Yuri's own home "
                     f"({home_real}) is reachable; set ALLOWED_PROJECT_ROOTS in "
                     f"backend/.env so she can work in your projects (sessions "
                     f"elsewhere will refuse to start)")

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
