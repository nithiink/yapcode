"""Claude execution backend that drives the **interactive** `claude` CLI in tmux.

Why this exists (vs. SDKClaudeRunner): the interactive CLI stays on the Max 20x
subscription and supports `claude --chrome` (browser control) — neither of which
the Agent SDK offers. It implements the same `ClaudeRunner` interface and the same
background-task + `poll_status` model, so the frontend is unchanged.

How it drives the TUI without fragile screen-scraping:
- INPUT  via `tmux send-keys`.
- OUTPUT (assistant text/tool calls) read structurally from the session `.jsonl`.
- CONTROL (permission gating, turn completion) via per-session Claude Code hooks
  injected with `--settings` (see tmux_hooks/). The PreToolUse hook blocks on a
  decision file we write from `answer()`; the Stop hook signals turn completion.

Validated live (claude v2.1.150): --session-id honored; a one-time per-folder
trust dialog must be accepted (Enter); hooks fire via --settings; PreToolUse
"allow" skips the interactive permission menu; jsonl path is best found via the
hook's transcript_path (cwd encoding maps both '/' and '_' to '-') with a glob
fallback on the unique session id.
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shlex
import shutil
import sys
from uuid import uuid4

import config
from claude_runner import (
    AdvanceResult,
    ClaudeRunner,
    MODE_CYCLE,
    Prompt,
    Status,
    _ALLOW_WORDS,
    _DENY_WORDS,
    decide_permission,
    _parse_questions,
    _summarize_tool,
    normalize_mode,
)
from permissions import classify, mode_covers
from event_log import log_event
import pricing

log = logging.getLogger("yapcode.tmux")


def _slabel(s: "_TmuxSession") -> str:
    """Short session label for the debug stream (name if set, else short handle)."""
    return s.name or s.handle[:8]


# tmux's canonical key names. `tmux send-keys <name>` (without -l) emits the key's
# control sequence ONLY for names it recognizes; ANY unrecognized argument is
# silently typed as literal text. So `send-keys Backspace` types the word
# "Backspace" instead of erasing — the exact failure mode behind the send_keys
# bug. We normalize model-supplied names against the canonical set + an alias
# table, and reject clearly-bad names so they can't slip through as literal text.
_TMUX_KEYS = {
    "Up", "Down", "Left", "Right", "Escape", "Enter", "Space", "Tab", "BTab",
    "BSpace", "Home", "End", "PageUp", "PageDown", "PPage", "NPage", "Insert",
    "IC", "Delete", "DC",
    *(f"F{n}" for n in range(1, 13)),
}
# Case-insensitive aliases → canonical tmux name. Covers the DOM/natural names
# the model tends to emit (verified-literal in tmux 3.6: Backspace, ArrowUp, Esc,
# Return all type verbatim without this map).
_KEY_ALIASES = {
    "esc": "Escape", "escape": "Escape",
    "return": "Enter", "ret": "Enter", "cr": "Enter", "newline": "Enter",
    "backspace": "BSpace", "bs": "BSpace", "back": "BSpace",
    "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "uparrow": "Up", "downarrow": "Down", "leftarrow": "Left", "rightarrow": "Right",
    "spacebar": "Space", "spc": "Space",
    "pgup": "PageUp", "pgdn": "PageDown", "pagedown": "PageDown", "pageup": "PageUp",
    "delete": "Delete", "del": "Delete", "ins": "Insert", "insert": "Insert",
    "home": "Home", "end": "End",
    "tab": "Tab", "backtab": "BTab", "shifttab": "BTab", "shift-tab": "BTab", "btab": "BTab",
}
# A modifier chord (C-c, M-x, S-Up, C-M-Left) or function key — pass through as-is.
_CHORD_RE = re.compile(r"^[CMS](-[CMS])*-\S+$")


def _normalize_key(key: str) -> str:
    """Map a model-supplied key name to a tmux key tmux will actually interpret.

    Raises ValueError for names tmux would silently type as literal text, so the
    caller surfaces an error instead of garbage landing in the terminal. Chords
    (C-c, S-Up, ...) and single characters pass through untouched."""
    k = key.strip()
    if not k:
        raise ValueError("empty key name")
    # Bound the length before the chord regex below: a crafted value like
    # "C-" + "-C"*N otherwise drives its backtracking into superlinear time.
    if len(k) > 32:
        raise ValueError(f"key name too long: {key!r}")
    if len(k) == 1 or _CHORD_RE.match(k):
        return k  # single char or modifier chord — tmux handles directly
    if k in _TMUX_KEYS:
        return k  # already canonical
    low = k.lower()
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    # Tolerate canonical names given in the wrong case (e.g. "escape", "up").
    for canon in _TMUX_KEYS:
        if canon.lower() == low:
            return canon
    raise ValueError(
        f"unknown key name {key!r} — tmux would type it as literal text. "
        f"Use a tmux key name (e.g. Escape, Enter, Up/Down/Left/Right, BSpace, "
        f"Tab, BTab, Space, Delete, PageUp) or a chord (C-c), "
        f"or pass it as {{\"text\": {key!r}}} to type it literally."
    )


def _task_label(t: asyncio.Task) -> str:
    """The message text a queued/running turn carries (set via task.set_name at
    enqueue). Empty for an unlabeled task (asyncio's default "Task-N")."""
    name = t.get_name()
    return "" if name.startswith("Task-") else name

CTRL_ROOT = config.SESSION_STORE_DIR  # set via VC_SESSION_STORE; defaults inside the project
HOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmux_hooks")
ENABLE_CHROME = os.getenv("CLAUDE_CLI_CHROME", "1") != "0"

# A session id becomes a directory name under CTRL_ROOT, so it must be a single
# safe path component; otherwise a caller-supplied id like "../../foo" would
# traverse out of the session store. Real session ids are UUIDs.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def validate_session_id(session_id: str) -> str:
    """Return the trimmed id if it is a safe single path component, else raise ValueError."""
    sid = (session_id or "").strip()
    if not _SESSION_ID_RE.match(sid):
        raise ValueError(f"invalid session id (expected a UUID-like token): {session_id!r}")
    return sid


class _TmuxSession:
    def __init__(self, handle: str, cwd: str, model: str):
        # handle becomes a directory name under CTRL_ROOT and is interpolated into
        # the transcript glob, so it must be a safe single path component. Validating
        # at the source keeps every downstream os.path.join(self.ctrl, ...) contained.
        handle = validate_session_id(handle)
        self.handle = handle               # == --session-id, also handoff id
        self.cwd = cwd                      # realpath
        self.model = model
        self.name: str | None = None        # human-readable name (persisted in meta)
        self.mode = "default"               # permission mode (Shift+Tab cycle)
        self.pane = f"vc_{handle[:8]}"
        self.ctrl = os.path.join(CTRL_ROOT, handle)
        self.transcript_path: str | None = None
        self.jsonl_pos = 0                  # bytes of transcript consumed
        self.status: Status = "running"
        self.turn_prompt: str | None = None  # message of the in-flight turn (narration attribution)
        self.error: str | None = None
        # Subscription billing reports no $ in the jsonl, so cost is rebuilt from
        # token usage × list pricing (see _update_cost). _cost_scan_size caches
        # the last-scanned transcript size so polling doesn't re-read it.
        self.cost_usd = 0.0
        self._cost_scan_size = -1
        self._delta: list[str] = []
        self._transcript: list[str] = []
        self.tools_used: list[str] = []
        self.pending: Prompt | None = None
        self.pending_tool_use_id: str | None = None
        # A second answer for the same prompt must fail fast, not queue: a
        # stale allow run later could approve a LATER prompt the user never heard.
        self.prompt_seq = 0       # bumps each time a prompt parks
        self.answer_claimed = -1  # prompt_seq an in-flight answer has claimed
        # CLI-answerable prompts (AskUserQuestion menus, the ExitPlanMode dialog)
        # render in the live TUI, so the user can answer them with the keyboard —
        # in which case no voice answer() fires to clear `pending`. We watch the
        # pane: once we've seen THIS prompt's menu on screen (_pending_seen is the
        # very Prompt object), its disappearance means it was answered in the CLI.
        # Identity-keying to the Prompt avoids clearing a freshly-raised prompt
        # whose menu hasn't rendered yet.
        self._pending_seen: Prompt | None = None
        self._pending_gone_strikes = 0
        # An AskUserQuestion can carry several questions answered in sequence on
        # one form; track the full list and which one we're on so the menu is
        # driven through every question, not just the first.
        self.questions: list[dict] = []
        self.q_index: int = 0
        self._stop = asyncio.Event()
        self._turn_lock = asyncio.Lock()
        self._evpos = 0                     # bytes of events.jsonl consumed
        self._evbuf = ""
        self._tail: asyncio.Task | None = None
        self._closed = False
        # FIFO of completed turn results that poll_status hasn't read yet. Keeps a
        # finished turn's reply from being lost when a new start_advance fires
        # before poll_status drains it. Bounded so a wedged poll can't blow memory.
        self._pending_results: list[AdvanceResult] = []
        # Tasks queued behind the current bg task — runs serialized through
        # _turn_lock so a rapid second tell_claude doesn't drop the first one's
        # result (asyncio task ref lost = result lost).
        self._extra_tasks: list[asyncio.Task] = []


class TmuxClaudeRunner(ClaudeRunner):
    def __init__(self, default_model: str | None = None):
        self._default_model = default_model or os.getenv("CLAUDE_MODEL", "opus")
        self._sessions: dict[str, _TmuxSession] = {}
        self._bg: dict[str, asyncio.Task[AdvanceResult]] = {}

    # --- tmux helpers -----------------------------------------------------

    async def _tmux(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace")

    async def _capture(self, s: _TmuxSession) -> str:
        _, out = await self._tmux("capture-pane", "-t", s.pane, "-p")
        return out

    async def _capture_history(self, s: _TmuxSession, lines: int = 300) -> str:
        """The visible pane PLUS the last `lines` rows of scrollback. Needed for
        output taller than the pane (e.g. /context) whose top has already
        scrolled off — a plain capture would silently lose it."""
        _, out = await self._tmux("capture-pane", "-t", s.pane, "-p", "-S", f"-{lines}")
        return out

    async def _alive(self, s: _TmuxSession) -> bool:
        rc, _ = await self._tmux("has-session", "-t", s.pane)
        return rc == 0

    # --- lifecycle --------------------------------------------------------

    def _preflight(self, cwd: str) -> str:
        if shutil.which("tmux") is None:
            raise ValueError("tmux is not installed — required to run Claude sessions (macOS: brew install tmux; Debian/Ubuntu: sudo apt install tmux)")
        if shutil.which("claude") is None:
            raise ValueError("the `claude` CLI is not on PATH — install Claude Code (curl -fsSL https://claude.ai/install.sh | bash) and run `claude` once to sign in")
        # Re-assert the directory sandbox at the sink so a session can't be spawned
        # outside ALLOWED_PROJECT_ROOTS even if a caller bypasses resolve_project_path.
        return config.resolve_within_roots(cwd)

    async def _spawn(self, s: _TmuxSession, claude_id_arg: str) -> None:
        """Create the detached tmux pane running `claude` (with our hooks wired via
        --settings) and start tracking it. `claude_id_arg` is either
        `--session-id <new uuid>` (fresh start) or `--resume <existing id>`."""
        # Defense in depth: assert containment before any makedirs/write so the
        # control dir can never escape the session store, even if a future caller
        # reaches _spawn without going through validate_session_id.
        ctrl_real = os.path.realpath(s.ctrl)
        root_real = os.path.realpath(CTRL_ROOT)
        if ctrl_real != root_real and not ctrl_real.startswith(root_real + os.sep):
            raise ValueError("session control dir escapes the session store")
        os.makedirs(os.path.join(s.ctrl, "decisions"), exist_ok=True)
        self._write_settings(s)
        self._write_meta(s)
        self._write_mode(s)

        chrome = "--chrome " if ENABLE_CHROME else ""
        inner = (
            f"VC_CTRL={shlex.quote(s.ctrl)} "
            f"claude {claude_id_arg} --model {shlex.quote(s.model)} "
            f"--permission-mode {shlex.quote(s.mode)} "
            f"{chrome}--settings {shlex.quote(os.path.join(s.ctrl, 'settings.json'))}"
        )
        rc, out = await self._tmux(
            "new-session", "-d", "-s", s.pane, "-c", s.cwd, "-x", "220", "-y", "50", inner
        )
        if rc != 0:
            raise ValueError(f"failed to start tmux session: {out.strip()}")

        # The session can be co-driven by a human `tmux attach` alongside the
        # browser live-terminal. Size the window to the LARGEST attached client
        # (not the default smallest) so a small terminal doesn't shrink the pane
        # for everyone. Best-effort; ignore rc.
        await self._tmux("set-option", "-t", s.pane, "window-size", "largest")
        await self._tmux("set-window-option", "-t", s.pane, "aggressive-resize", "on")

        self._sessions[s.handle] = s
        s._tail = asyncio.create_task(self._tail_events(s))
        await self._await_ready(s)

    async def start(self, cwd: str, model: str | None = None, mode: str = "default") -> str:
        cwd = self._preflight(cwd)
        handle = str(uuid4())
        s = _TmuxSession(handle, cwd, model or self._default_model)
        s.mode = normalize_mode(mode)
        await self._spawn(s, f"--session-id {handle}")
        log.info("tmux session %s started in %s (chrome=%s)", handle, cwd, ENABLE_CHROME)
        return handle

    async def resume(self, session_id: str, cwd: str, model: str | None = None,
                     mode: str = "default", name: str | None = None) -> str:
        """Adopt an EXISTING Claude Code session (e.g. one a user started in their
        own terminal) by reopening it in a hooked tmux pane via `claude --resume`.
        Reuses the real session id as our handle, so it slots into the same
        pane-naming / control-dir / rehydration machinery. Caller must ensure the
        original process has exited (single writer per session)."""
        session_id = validate_session_id(session_id)
        if session_id in self._sessions:
            return session_id  # already adopted/running — no duplicate pane
        cwd = self._preflight(cwd)
        s = _TmuxSession(session_id, cwd, model or self._default_model)
        s.mode = normalize_mode(mode)
        s.name = name
        await self._spawn(s, f"--resume {shlex.quote(session_id)}")
        log.info("tmux session %s resumed in %s (chrome=%s)", session_id, cwd, ENABLE_CHROME)
        return session_id

    def _write_settings(self, s: _TmuxSession) -> None:
        py = shlex.quote(sys.executable)
        def cmd(name: str) -> str:
            return f"{py} {shlex.quote(os.path.join(HOOK_DIR, name))}"
        settings = {
            "hooks": {
                "PreToolUse": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": cmd("hook_pretool.py"), "timeout": 600}]}],
                "Stop": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": cmd("hook_stop.py")}]}],
                "Notification": [{"matcher": "*", "hooks": [
                    {"type": "command", "command": cmd("hook_notify.py")}]}],
            }
        }
        with open(os.path.join(s.ctrl, "settings.json"), "w") as f:
            json.dump(settings, f)

    def _write_meta(self, s: _TmuxSession) -> None:
        with open(os.path.join(s.ctrl, "meta.json"), "w") as f:
            json.dump({"handle": s.handle, "cwd": s.cwd, "model": s.model,
                       "pane": s.pane, "mode": s.mode, "name": s.name}, f)

    def persist_name(self, handle: str, name: str) -> None:
        """Record a session's display name on disk so it survives a restart.
        Called by session_manager when a name is set/changed."""
        s = self._sessions.get(handle)
        if s is None:
            return
        s.name = name
        self._write_meta(s)

    def _write_mode(self, s: _TmuxSession) -> None:
        """Persist the permission mode where the PreToolUse hook can read it, so
        auto/acceptEdits skip the voice permission prompt."""
        tmp = os.path.join(s.ctrl, "mode.tmp")
        path = os.path.join(s.ctrl, "mode")
        with open(tmp, "w") as f:
            f.write(s.mode)
        os.replace(tmp, path)

    async def _await_ready(self, s: _TmuxSession, timeout: float = 10.0) -> None:
        """Accept the one-time per-folder trust dialog and wait for the input box."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        accepted = False
        while loop.time() < deadline:
            if not await self._alive(s):
                return
            pane = await self._capture(s)
            if not accepted and "trust this folder" in pane.lower():
                await self._tmux("send-keys", "-t", s.pane, "Enter")
                accepted = True
                await asyncio.sleep(1.5)
                continue
            if "for shortcuts" in pane or "Try " in pane:
                return
            await asyncio.sleep(0.4)

    async def shutdown(self) -> None:
        """Stop tracking sessions on backend shutdown.

        Default (VC_KILL_SESSIONS_ON_SHUTDOWN unset/false): DETACH — leave the
        tmux panes and their `claude` processes running with control dirs intact,
        so the next startup can rehydrate them. We only cancel our own in-process
        tracking (background turns + the event tail).

        With VC_KILL_SESSIONS_ON_SHUTDOWN=1: KILL — the old behavior, where each
        pane is killed and its control dir removed (nothing survives the restart).

        Either way, close() remains the explicit per-session destroy path."""
        kill = config.KILL_SESSIONS_ON_SHUTDOWN
        for t in self._bg.values():
            if not t.done():
                t.cancel()
        self._bg.clear()
        for s in list(self._sessions.values()):
            for t in s._extra_tasks:
                if not t.done():
                    t.cancel()
            s._extra_tasks.clear()
            s._closed = True
            if s._tail and not s._tail.done():
                s._tail.cancel()
            if kill:
                await self._tmux("kill-session", "-t", s.pane)
                shutil.rmtree(s.ctrl, ignore_errors=True)
        self._sessions.clear()

    # --- rehydration ------------------------------------------------------

    async def _live_panes(self) -> set[str]:
        """Names of tmux sessions currently alive (our panes start with 'vc_')."""
        rc, out = await self._tmux("list-sessions", "-F", "#{session_name}")
        if rc != 0:
            return set()  # no server running -> nothing alive
        return {ln.strip() for ln in out.splitlines() if ln.strip()}

    async def rehydrate(self) -> list[dict]:
        """Re-attach to interactive CLI sessions that outlived a previous backend.

        On a hard kill the tmux panes and their `claude` processes keep running
        and the control dir (events.jsonl / decisions/ / mode / settings.json)
        stays intact, so we never relaunch claude — we just rebuild our in-memory
        tracking and re-arm the event tail against the same files. Control dirs
        whose pane is gone are garbage-collected. Best-effort and idempotent.
        """
        if shutil.which("tmux") is None or not os.path.isdir(CTRL_ROOT):
            return []
        live = await self._live_panes()
        restored: list[dict] = []
        for handle in sorted(os.listdir(CTRL_ROOT)):
            ctrl = os.path.join(CTRL_ROOT, handle)
            if not os.path.isdir(ctrl) or handle in self._sessions:
                continue
            meta = self._read_meta(ctrl)
            pane = (meta or {}).get("pane") or f"vc_{handle[:8]}"
            if pane not in live:
                shutil.rmtree(ctrl, ignore_errors=True)  # dead leftover
                continue
            if not meta:
                continue  # alive but unreadable meta — leave it untouched
            try:
                s = await self._adopt(handle, ctrl, meta)
                self._sessions[handle] = s
                s._tail = asyncio.create_task(self._tail_events(s))
                restored.append({"handle": handle, "name": s.name, "cwd": s.cwd,
                                 "mode": s.mode, "status": s.status})
                log.info("rehydrated tmux session %s (%s) in %s [%s]",
                         handle, s.name, s.cwd, s.status)
            except Exception:
                log.exception("failed to rehydrate %s", handle)
        return restored

    def _read_meta(self, ctrl: str) -> dict | None:
        try:
            with open(os.path.join(ctrl, "meta.json")) as f:
                return json.load(f)
        except Exception:
            return None

    async def _adopt(self, handle: str, ctrl: str, meta: dict) -> _TmuxSession:
        """Build a live _TmuxSession bound to an already-running pane."""
        s = _TmuxSession(handle, meta.get("cwd", ""), meta.get("model", self._default_model))
        s.pane = meta.get("pane") or s.pane
        s.ctrl = ctrl
        s.name = meta.get("name")

        # Authoritative mode from the live TUI footer (meta may be stale); resync
        # the mode file so the PreToolUse hook agrees.
        pane = await self._capture(s)
        s.mode = self._detect_mode(pane)
        self._write_mode(s)

        # Stream only NEW assistant text from here; full history stays available
        # via read_transcript (which reads the jsonl from the top).
        tpath = self._find_transcript(s)
        if tpath and os.path.exists(tpath):
            s.jsonl_pos = os.path.getsize(tpath)

        # Re-arm the event tail past existing events, but first recover an
        # in-flight prompt that was open when the old backend died (using the
        # live pane to land on the right sub-question of a multi-question form).
        evpath = os.path.join(ctrl, "events.jsonl")
        self._restore_pending(s, evpath, pane)
        s._evpos = os.path.getsize(evpath) if os.path.exists(evpath) else 0
        return s

    def _restore_pending(self, s: _TmuxSession, evpath: str, pane: str = "") -> None:
        """If the last event is an unanswered prompt, restore it so the user can
        still approve/deny it — the PreToolUse hook stays parked polling the
        decision file for ~590s, so a recently-orphaned prompt is still live."""
        if not os.path.exists(evpath):
            return
        last = None
        try:
            with open(evpath) as f:
                for line in f:
                    if line.strip():
                        last = line
        except Exception:
            return
        if not last:
            return
        try:
            ev = json.loads(last)
        except Exception:
            return
        kind = ev.get("event")
        if kind == "needs_permission":
            s.pending = Prompt(
                kind="permission",
                text=_summarize_tool(ev.get("tool_name", ""), ev.get("tool_input", {})),
                options=["allow", "deny"],
                tool_name=ev.get("tool_name", ""),
            )
            s.pending_tool_use_id = ev.get("tool_use_id")
            s.prompt_seq += 1
            s.status = "needs_permission"
        elif kind == "needs_choice":
            s.questions = _parse_questions(ev.get("tool_input", {}))
            s.q_index = self._detect_question_index(s, pane)
            s.pending = self._prompt_for_question(s, s.q_index, ev.get("tool_name", ""))
            s.pending_tool_use_id = ev.get("tool_use_id")
            s.prompt_seq += 1
            s.status = "needs_choice"

    def _detect_question_index(self, s: _TmuxSession, pane: str) -> int:
        """For a multi-question form, find which question the live screen is on
        by matching the visible question text. Defaults to 0."""
        if not pane or len(s.questions) <= 1:
            return 0
        for i, q in enumerate(s.questions):
            qt = (q.get("question") or "").strip()
            if qt and qt[:40] in pane:
                return i
        return 0

    # --- driving ----------------------------------------------------------

    # Hard safety cap on how long advance() will wait for a Stop hook before
    # returning whatever's been accumulated. Long enough for slow Claude turns
    # (multi-minute Bash + thinking), short enough that a missed Stop hook can't
    # hang the voice agent forever.
    ADVANCE_HARD_TIMEOUT_S = float(os.getenv("VC_ADVANCE_TIMEOUT_S", "600"))

    async def advance(self, handle: str, message: str) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s.pending is not None:
                return self._err(s, "a prompt is pending; call answer first")
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            s.turn_prompt = message
            await self._send_message(s, message)
            try:
                await asyncio.wait_for(s._stop.wait(), timeout=self.ADVANCE_HARD_TIMEOUT_S)
            except asyncio.TimeoutError:
                # Don't leave the voice agent in limbo: return what we have plus a
                # note so it can surface the wait to the user.
                await self._read_new_text(s)
                s.status = "completed"
                log.warning("advance() for %s timed out after %ss with no Stop hook; "
                            "returning partial result", handle, self.ADVANCE_HARD_TIMEOUT_S)
                res = self._collect(s)
                if not res.assistant_text.strip():
                    res.assistant_text = (
                        "(no completion event fired within the timeout; "
                        "the session may still be working — try peek_screen to verify)"
                    )
                return res
            return self._collect(s)

    async def answer(self, handle: str, choice: str, seq: int) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s.pending is None or s.prompt_seq != seq:
                return self._err(s, "that prompt was already answered — nothing to do")
            kind = s.pending.kind
            pending_tool = s.pending.tool_name
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            if kind == "permission":
                if pending_tool == "ExitPlanMode":
                    # No decision file: the hook already allowed the tool so the
                    # plan renders on screen; the TUI dialog is the actual gate.
                    await self._drive_plan_dialog(s, choice)
                else:
                    decided = self._write_decision(s, choice)
                    if decided is None:
                        # Ambiguous: keep the prompt pending and re-ask. The hook
                        # stays parked (no decision written), so the gate holds.
                        # Release the claim so the retry can answer the SAME prompt
                        # — this is a re-ask, so prompt_seq is unchanged and the
                        # claim wouldn't otherwise clear. The success/deny paths
                        # need no such reset: the next parked prompt bumps
                        # prompt_seq, which alone makes answer_claimed stale.
                        # (The SDK runner has no reset here because its re-ask
                        # loops through can_use_tool, which re-parks and bumps
                        # prompt_seq.)
                        s.status = "needs_permission"
                        s.answer_claimed = -1
                        s._delta.append(
                            "I didn't catch a clear yes or no — say 'allow' to "
                            "approve or 'deny' to reject.")
                        return self._collect(s)
            else:  # choice / AskUserQuestion menu
                more = await self._answer_question(s, choice)
                if more:
                    # A multi-question form just advanced to the next question.
                    # Selecting an option auto-advances the form (no hook event
                    # fires between sub-questions), so surface the next question
                    # right away instead of waiting for the turn to complete.
                    s.q_index += 1
                    s.pending = self._prompt_for_question(s, s.q_index, s.pending.tool_name)
                    s.prompt_seq += 1
                    s.status = "needs_choice"
                    return self._collect(s)
            s.pending = None
            s.pending_tool_use_id = None
            await s._stop.wait()
            return self._collect(s)

    def _prompt_for_question(self, s: _TmuxSession, i: int,
                             tool_name: str = "AskUserQuestion") -> Prompt:
        """Build the pending Prompt for question `i` of the active form. When the
        form has several questions, the spoken text is prefixed with progress
        ('Question 2 of 4: …') so the voice model can narrate where we are."""
        qs = s.questions
        if not qs or i >= len(qs):
            return Prompt(kind="choice", text="Claude has a question",
                          options=[], tool_name=tool_name)
        q = qs[i]
        text = q["question"]
        if len(qs) > 1:
            text = f"Question {i + 1} of {len(qs)}: {text}"
        return Prompt(kind="choice", text=text, options=q["options"],
                      tool_name=tool_name, multi_select=q["multi"])

    async def _send_message(self, s: _TmuxSession, message: str) -> None:
        # Single literal line, a pause, then Enter. Internal newlines are
        # flattened (voice input is one utterance). The pause matters: the TUI
        # has paste detection, so an Enter sent in the same burst as the text is
        # treated as a newline rather than a submit. Sending it separately, after
        # the input settles, makes Enter submit. Verify via capture-pane and
        # retry once if the text is still sitting unsent.
        text = " ".join(message.splitlines())
        log_event("backend", "claude", "send", text, session=_slabel(s),
                  detail={"handle": s.handle, "text": text})
        await self._tmux("send-keys", "-t", s.pane, "-l", "--", text)
        await asyncio.sleep(0.4)
        await self._tmux("send-keys", "-t", s.pane, "Enter")
        await self._ensure_submitted(s, text)

    async def _ensure_submitted(self, s: _TmuxSession, text: str) -> None:
        """If the text is still sitting in the input box (Enter newlined instead
        of submitting), press Enter again. Only inspect the bottom-most `❯` line
        — the live input box — since submitted messages are echoed in history
        with the same prefix."""
        probe = text.strip()[:24]
        if not probe:
            return
        await asyncio.sleep(0.4)
        pane = await self._capture(s)
        input_line = ""
        for line in pane.splitlines():
            ls = line.lstrip()
            if ls.startswith("❯"):
                input_line = ls  # keep the last one = the active input box
        if probe in input_line:
            await self._tmux("send-keys", "-t", s.pane, "Enter")

    def _write_decision(self, s: _TmuxSession, choice: str) -> bool | None:
        """Write the decision file the parked PreToolUse hook waits on. Returns
        True/False (allowed/denied), or None on an ambiguous answer — in which
        case no file is written, so the hook stays parked and the caller re-asks."""
        decision = decide_permission(choice)
        if decision is None:
            log_event("backend", "claude", "decision", f"ambiguous, re-asking: {choice}",
                      session=_slabel(s),
                      detail={"handle": s.handle, "choice": choice,
                              "tool_use_id": s.pending_tool_use_id})
            return None
        allow = decision == "allow"
        path = os.path.join(s.ctrl, "decisions", f"{s.pending_tool_use_id or 'none'}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"decision": decision,
                       "reason": "" if allow else f"user said: {choice}"}, f)
        os.replace(tmp, path)
        log_event("backend", "claude", "decision",
                  decision + (f" ({choice})" if not allow else ""),
                  session=_slabel(s),
                  detail={"handle": s.handle, "decision": decision,
                          "choice": choice, "tool_use_id": s.pending_tool_use_id})
        return allow

    def _classify_plan_choice(self, c: str) -> str:
        """Map the spoken answer to one of the plan dialog's outcomes:
        'auto' (1. auto mode), 'manual' (2. manually approve edits), 'web'
        (3. refine with Ultraplan on the web), or 'decline' (stay in plan mode,
        forward any feedback). Order matters: 'manually approve' contains an
        approve-word, so the more specific intents are checked first."""
        if "manual" in c or "approve edit" in c or "each edit" in c or "review edit" in c:
            return "manual"
        if "ultraplan" in c or "on the web" in c or "refine on" in c:
            return "web"
        if c in _DENY_WORDS or c.startswith(("no", "don't", "do not", "stop", "keep planning")):
            return "decline"
        if "auto" in c or c in _ALLOW_WORDS or any(c.startswith(w) for w in _ALLOW_WORDS):
            return "auto"
        return "decline"  # unrecognized -> treat as feedback to refine the plan

    async def _drive_plan_dialog(self, s: _TmuxSession, choice: str) -> None:
        """Answer the CLI's plan dialog ('ready to execute. Would you like to
        proceed?') by selecting the row matching the spoken choice. Approvals
        sync the session's mode file so the hook policy matches the CLI; a
        decline dismisses the dialog (stays in plan mode) and forwards any
        feedback so Claude can revise the plan."""
        c = (choice or "").strip().lower()
        intent = self._classify_plan_choice(c)
        for _ in range(80):  # ~12s grace for the dialog to render
            pane = await self._capture(s)
            if "Would you like to proceed?" in pane or "ready to execute" in pane:
                break
            await asyncio.sleep(0.15)
        else:
            log.warning("no plan dialog appeared for %s; leaving the TUI as-is", s.handle)
            return
        if intent in ("auto", "manual"):
            await self._select_row(s, 1 if intent == "manual" else 0)
            s.mode = "default" if intent == "manual" else "auto"
            self._write_mode(s)
            self._write_meta(s)
            log_event("backend", "claude", "decision",
                      f"plan approved ({'manually approve edits' if intent == 'manual' else 'auto mode'})",
                      session=_slabel(s), detail={"handle": s.handle, "mode": s.mode})
            return
        if intent == "web":
            await self._select_row(s, 2)
            log_event("backend", "claude", "decision", "plan -> refine with Ultraplan (web)",
                      session=_slabel(s), detail={"handle": s.handle})
            return
        await self._tmux("send-keys", "-t", s.pane, "Escape")
        await asyncio.sleep(0.6)
        bare = c in _DENY_WORDS
        feedback = ("The user declined executing this plan for now. Stay in plan "
                    "mode and wait." if bare else choice)
        log_event("backend", "claude", "decision", f"plan declined ({choice})",
                  session=_slabel(s), detail={"handle": s.handle})
        await self._send_message(s, feedback)

    async def _answer_question(self, s: _TmuxSession, choice: str) -> bool:
        """Drive one question of the AskUserQuestion menu.

        The current TUI requires moving the highlight with the arrow keys and
        pressing Enter to confirm (its footer states "Enter to select · ↑/↓ to
        navigate"). Pressing the option's *digit* alone does NOT register the
        choice — that left the prompt stuck on screen. So we move the highlight
        to the target row and press Enter. Single- vs. multi-select is taken from
        the question payload (authoritative), not scraped from the screen, since
        a question's header also renders with a ☐ glyph.

        The rows are the N options, then a "Type something." entry (free-form
        answer) and "Chat about this". If the spoken choice matches no option we
        select "Type something" and type it.

        Returns True if this form has more questions after the one just answered
        (selecting auto-advances to the next), False if it was the last/only one.
        """
        options = s.pending.options if s.pending else []
        multi = bool(s.pending and getattr(s.pending, "multi_select", False))
        more = bool(s.questions) and s.q_index < len(s.questions) - 1
        await self._wait_for_menu(s)
        c = (choice or "").strip().lower()

        if multi:
            await self._answer_multi(s, options, c)
        else:
            # The spoken prompt numbers the options, so the user may answer by
            # ordinal ("two", transcribed as "2" / "option 2"). Map a bare 1-based
            # index to its row before falling back to text matching.
            m = re.fullmatch(r"(?:option\s*)?(\d+)\.?", c)
            idx = int(m.group(1)) - 1 if m else -1
            if not (0 <= idx < len(options)):
                idx = next(
                    (i for i, o in enumerate(options)
                     if o.strip().lower() == c or (c and c in o.strip().lower())),
                    -1,
                )
            if idx >= 0:
                await self._select_row(s, idx)
            elif choice.strip():
                # No listed option matched — give a free-form answer via "Type
                # something" (the row immediately after the real options).
                await self._select_row(s, len(options))
                await asyncio.sleep(0.4)
                await self._tmux("send-keys", "-t", s.pane, "-l", "--", choice.strip())
                await asyncio.sleep(0.3)
                await self._tmux("send-keys", "-t", s.pane, "Enter")
            else:
                await self._tmux("send-keys", "-t", s.pane, "Enter")

        if not more and len(s.questions) > 1:
            await self._confirm_submit(s)
        return more

    def _menu_cursor(self, pane: str) -> int:
        """0-based index of the currently highlighted numbered row (the line
        carrying the ❯ marker), or 0 if none is found (the menu's initial state)."""
        for line in pane.splitlines():
            m = re.match(r"^(❯\s*)?(\d+)[.)]", line.lstrip())
            if m and m.group(1):
                return int(m.group(2)) - 1
        return 0

    async def _select_row(self, s: _TmuxSession, target: int) -> None:
        """Move the menu highlight to numbered row `target` (0-based) and press
        Enter. Navigates relative to the current cursor so it works no matter
        where the highlight starts."""
        cur = self._menu_cursor(await self._capture(s))
        delta = target - cur
        key = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            await self._tmux("send-keys", "-t", s.pane, key)
            await asyncio.sleep(0.12)
        await asyncio.sleep(0.15)
        await self._tmux("send-keys", "-t", s.pane, "Enter")

    async def _answer_multi(self, s: _TmuxSession, options: list[str], c: str) -> None:
        """Multi-select: toggle each named option with Space (navigating by
        arrows), then activate the Submit row with Enter. NOTE: single-select is
        the common path and is fully verified; this multi-select flow still needs
        a live check against a real multi-select menu."""
        for i, o in enumerate(options):
            if o.strip().lower() in c:
                cur = self._menu_cursor(await self._capture(s))
                key = "Down" if i > cur else "Up"
                for _ in range(abs(i - cur)):
                    await self._tmux("send-keys", "-t", s.pane, key)
                    await asyncio.sleep(0.12)
                await self._tmux("send-keys", "-t", s.pane, "Space")
                await asyncio.sleep(0.2)
        # Move down to the Submit row (just past the options) and confirm.
        await self._select_row(s, len(options))

    async def _confirm_submit(self, s: _TmuxSession) -> None:
        """Confirm the 'Review your answers' screen a multi-question form ends
        on ('❯ 1. Submit answers / 2. Cancel') — the tool only fires after that
        final Enter. Single-question forms submit on selection instead."""
        for _ in range(20):  # the review screen renders well within ~3s
            pane = await self._capture(s)
            if "Submit answers" in pane:
                if self._menu_cursor(pane) != 0:
                    await self._select_row(s, 0)
                else:
                    await self._tmux("send-keys", "-t", s.pane, "Enter")
                return
            await asyncio.sleep(0.15)
        log.warning("submit/review screen never appeared for %s; "
                    "the question form may be left unsubmitted", s.handle)

    async def _wait_for_menu(self, s: _TmuxSession, timeout: float = 4.0) -> None:
        """Wait until the selection menu is actually rendered before sending keys
        (avoids a race where the answer lands in the prompt box instead)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            pane = await self._capture(s)
            if "to navigate" in pane or "Enter to select" in pane:
                return
            await asyncio.sleep(0.15)

    def _is_cli_answerable(self, p: Prompt | None) -> bool:
        """True for prompts the user can also answer with the keyboard in the live
        TUI. AskUserQuestion menus and the ExitPlanMode dialog both render on
        screen (the PreToolUse hook emits 'allow' for them and the runner drives
        the menu). Risky-tool permission prompts instead PARK the hook with no
        on-screen menu, so they can only be resolved by voice/mode/interrupt — the
        pane never shows them and must never be reconciled against it."""
        if p is None:
            return False
        if p.kind == "choice":
            return True
        return p.kind == "permission" and p.tool_name == "ExitPlanMode"

    def _prompt_on_screen(self, p: Prompt, pane: str) -> bool:
        """Whether the CLI-answerable prompt `p` is still rendered in `pane`."""
        if p.kind == "permission":  # ExitPlanMode plan-approval dialog
            return "Would you like to proceed?" in pane or "ready to execute" in pane
        return "to navigate" in pane or "Enter to select" in pane  # question menu

    async def _reconcile_pending(self, s: _TmuxSession) -> None:
        """Drop a CLI-answerable prompt the user answered directly in the terminal.

        The voice answer() path clears `pending` itself; but a keyboard answer in
        the live TUI leaves `pending` set, so list()/poll_status keep reporting a
        prompt the user already dealt with and the UI shows a stale card. We watch
        the pane: once this prompt's menu/dialog has been seen on screen and then
        leaves it, the user answered (or dismissed) it in the CLI — so clear it.

        Skipped while a turn holds _turn_lock: the voice answer() is itself driving
        the menu then, and its transient disappearance is not an external answer.
        Requires two consecutive off-screen polls so a single capture caught
        mid-redraw can't trigger a false clear."""
        p = s.pending
        if not self._is_cli_answerable(p) or s._turn_lock.locked():
            return
        pane = await self._capture(s)
        if self._prompt_on_screen(p, pane):
            s._pending_seen = p
            s._pending_gone_strikes = 0
            return
        if s._pending_seen is not p:
            return  # this prompt's menu hasn't rendered yet — don't clear early
        s._pending_gone_strikes += 1
        if s._pending_gone_strikes < 2:
            return
        if s.pending is not p:  # changed underneath us between awaits
            return
        log_event("claude", "backend", "decision",
                  "prompt answered in the CLI; clearing stale UI prompt",
                  session=_slabel(s),
                  detail={"handle": s.handle, "kind": p.kind, "tool_name": p.tool_name})
        s.pending = None
        s.pending_tool_use_id = None
        s._pending_seen = None
        s._pending_gone_strikes = 0
        s.questions = []
        s.q_index = 0
        if s.status in ("needs_permission", "needs_choice"):
            # The CLI answer let Claude proceed; a later turn_complete will flip
            # this to "completed". Until then it's no longer waiting on the user.
            s.status = "running"

    async def interrupt(self, handle: str) -> None:
        s = self._get(handle)
        bg = self._bg.pop(handle, None)
        if bg and not bg.done():
            bg.cancel()
        for t in s._extra_tasks:
            if not t.done():
                t.cancel()
        s._extra_tasks.clear()
        if s.pending and s.pending.kind == "permission":
            # Teardown writes the deny directly, bypassing the prompt_seq/claim
            # guard: the parked PreToolUse hook must be unblocked even mid-answer.
            # We cancelled the in-flight answer above and clear s.pending below,
            # so any stale answer that still runs fails the guard instead of
            # writing a second decision.
            self._write_decision(s, "deny")
        await self._tmux("send-keys", "-t", s.pane, "Escape")
        s.pending = None
        s.status = "completed"
        s._stop.set()

    async def send_keys(self, handle: str, items: list[dict]) -> dict:
        """Escape hatch: send raw tmux keys/text straight to the session pane.

        Each item is either {"key": "<tmux key name/chord>"} (interpreted by
        tmux, e.g. Escape, Enter, Up, C-c, BTab) or {"text": "<literal>"} (typed
        verbatim). Sent in order with a short pause between, mirroring the cadence
        _send_message/set_mode rely on for the TUI's paste detection. Deliberately
        does NOT take the turn lock — this exists to unstick a session mid-turn —
        and returns a screen snapshot so the caller can see the effect."""
        s = self._get(handle)
        if not await self._alive(s):
            return {"ok": False, "error": "session is not running"}
        # Resolve every {"key": ...} to a name tmux will actually interpret BEFORE
        # sending anything — otherwise an unknown name (e.g. "Backspace") gets typed
        # as literal text, and a mid-sequence failure leaves the pane half-driven.
        try:
            normalized = [
                {**it, "key": _normalize_key(it["key"])} if it.get("key") else it
                for it in items
            ]
        except ValueError as e:
            return {"ok": False, "error": str(e), "screen": await self.peek(handle)}
        items = normalized
        summary = " ".join(
            it["key"] if it.get("key") else f"type:{it.get('text', '')!r}"
            for it in items
        )
        log_event("backend", "claude", "raw_keys", summary, session=_slabel(s),
                  detail={"handle": s.handle, "items": items})
        for it in items:
            if it.get("key"):
                await self._tmux("send-keys", "-t", s.pane, it["key"])
            elif "text" in it:
                await self._tmux("send-keys", "-t", s.pane, "-l", "--", it["text"])
            else:
                continue  # ignore a malformed item rather than abort the sequence
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.2)
        return {"ok": True, "sent": items, "screen": await self.peek(handle)}

    async def close(self, handle: str) -> None:
        s = self._get(handle)
        bg = self._bg.pop(handle, None)
        if bg and not bg.done():
            bg.cancel()
        for t in s._extra_tasks:
            if not t.done():
                t.cancel()
        s._extra_tasks.clear()
        if s.pending and s.pending.kind == "permission":
            # Bypasses the prompt_seq/claim guard on purpose: unblock the parked
            # PreToolUse hook so the dying session doesn't leave it hanging.
            self._write_decision(s, "deny")
        s._closed = True
        if s._tail and not s._tail.done():
            s._tail.cancel()
        await self._tmux("kill-session", "-t", s.pane)
        shutil.rmtree(s.ctrl, ignore_errors=True)
        self._sessions.pop(handle, None)

    def _detect_mode(self, pane: str) -> str:
        """Read the current permission mode off the TUI footer."""
        low = pane.lower()
        if "auto mode on" in low:
            return "auto"
        if "plan mode on" in low:
            return "plan"
        if "accept edits on" in low:
            return "acceptEdits"
        return "default"

    async def set_mode(self, handle: str, mode: str) -> str:
        """Switch the live session's permission mode by cycling Shift+Tab the
        right number of times. The cycle order is fixed (MODE_CYCLE, verified
        live), so from the detected current mode we compute the exact presses to
        reach the target — no blind guessing."""
        s = self._get(handle)
        target = normalize_mode(mode)
        if (s.pending and s.pending.kind == "permission"
                and s.pending.tool_name == "ExitPlanMode"):
            # The plan-approval dialog is on screen; BTab presses would land in
            # the dialog, not the footer. The dialog itself offers the mode
            # change (option 1 is 'auto mode'), so route through answer instead.
            raise ValueError(
                "a plan-approval dialog is open — answer it instead "
                "(e.g. 'auto mode' to approve the plan and auto-apply, or "
                "'manually approve edits')")
        async with s._turn_lock:  # don't toggle mid-turn
            if not await self._alive(s):
                raise ValueError("session is not running")
            current = self._detect_mode(await self._capture(s))
            presses = (MODE_CYCLE.index(target) - MODE_CYCLE.index(current)) % len(MODE_CYCLE)
            for _ in range(presses):
                await self._tmux("send-keys", "-t", s.pane, "BTab")
                await asyncio.sleep(0.25)
            await asyncio.sleep(0.2)
            s.mode = self._detect_mode(await self._capture(s))
            self._write_mode(s)  # keep the hook's view of the mode in sync
        # The mode file only affects FUTURE tool calls — a PreToolUse hook
        # already parked on a permission prompt keeps waiting for its decision
        # file. If the new mode would have auto-approved that tool, approve it
        # now so 'switch to auto mode' doesn't leave the prompt hanging; the
        # resumed turn's output is delivered via poll_status like any answer.
        # Skip if an answer_prompt already claimed it — one decision per prompt.
        if (s.pending and s.pending.kind == "permission"
                and mode_covers(s.mode, s.pending.tool_name)
                and s.answer_claimed != s.prompt_seq):
            log_event("backend", "claude", "decision",
                      f"allow (pending prompt covered by switch to {s.mode})",
                      session=_slabel(s),
                      detail={"handle": s.handle, "tool_name": s.pending.tool_name})
            self.start_answer(handle, "allow")
        return s.mode

    # read_session output lands in a voice model's context (Gemini Live runs a
    # small window), so cap it to the most recent ~5k tokens of conversation.
    READ_CAP_CHARS = 20_000

    async def read(self, handle: str) -> str:
        s = self._get(handle)
        text = "".join(s._transcript)
        if not text.strip():
            # Adopted (handoff/rehydrated) session with no turns since: rebuild
            # the conversation from the on-disk jsonl so "what is this session
            # about?" has an answer.
            from transcript import read_timeline
            text = "\n\n".join(
                f"[{ev['kind']}] {ev['text']}"
                for ev in read_timeline(handle).get("events", [])
                if ev.get("kind") in ("user", "assistant") and ev.get("text", "").strip())
        if len(text) <= self.READ_CAP_CHARS:
            return text
        cut = text[-self.READ_CAP_CHARS:]
        nl = cut.find("\n")  # resync to a line boundary
        if 0 <= nl < 2000:
            cut = cut[nl + 1:]
        return f"(older conversation trimmed)\n{cut}"

    def pane_for(self, handle: str) -> str | None:
        """tmux pane target for a live-terminal attach, or None if unknown."""
        s = self._sessions.get(handle)
        return s.pane if s else None

    async def peek(self, handle: str, lines: int = 40) -> str:
        """A snapshot of what's currently rendered on the session's TUI screen.

        This is the raw visible pane (menus, spinners, the trust dialog, partial
        output) — ground truth the structured jsonl feed doesn't capture. It's a
        screen snapshot, not a transcript: wrapped at the pane width and limited
        to what's on screen, so older output has scrolled off. Use it to see the
        live state (e.g. a menu the model is unsure about), not as the main feed.
        """
        s = self._get(handle)
        if not await self._alive(s):
            return "(session is not running)"
        pane = await self._capture(s)
        rows = [r.rstrip() for r in pane.splitlines()]
        while rows and not rows[0].strip():
            rows.pop(0)
        while rows and not rows[-1].strip():
            rows.pop()
        if lines and len(rows) > lines:
            rows = rows[-lines:]
        return "\n".join(rows)

    def _queue_counts(self, s: _TmuxSession) -> dict:
        """Live view of a session's work pipeline for the UI. Turns are serialized
        through _turn_lock, so among the in-flight tasks (the _bg task plus any
        _extra_tasks queued behind it) at most one executes at a time and the rest
        wait. A finished task stays referenced until the next _harvest_finished
        sweeps it into _pending_results, so finished-but-unharvested tasks count
        toward `pending` too — making the number continuous across the harvest
        boundary and correct even when nothing is polling. Cancelled tasks never
        become results (see _stash_result), so they're excluded. Pure read; never
        harvests; safe on every list() refresh.

          running : a turn is executing right now
          queued  : turns waiting behind it
          pending : finished turns not yet drained/narrated by poll_status
        """
        bg = self._bg.get(s.handle)
        ordered = ([bg] if bg is not None else []) + list(s._extra_tasks)
        live = [t for t in ordered if not t.done()]
        done = sum(1 for t in ordered if t.done() and not t.cancelled())
        queue = [
            {"text": _task_label(t), "state": "running" if i == 0 else "queued"}
            for i, t in enumerate(live)
        ]
        return {
            "running": len(live) > 0,
            "queued": max(0, len(live) - 1),
            "pending": len(s._pending_results) + done,
            "queue": queue,
        }

    def list(self) -> list[dict]:
        out: list[dict] = []
        for s in self._sessions.values():
            self._update_cost(s)
            d = {"handle": s.handle, "session_id": s.handle, "cwd": s.cwd,
                 "model": s.model, "mode": s.mode, "status": s.status,
                 "cost_usd": round(s.cost_usd, 4),
                 **self._queue_counts(s)}
            # A prompt's needs_* result is delivered exactly once via poll_status;
            # expose it here so a later-connecting agent can still see it.
            if s.pending is not None:
                d["prompt"] = {"kind": s.pending.kind, "text": s.pending.text,
                               "options": list(s.pending.options or []),
                               "tool_name": s.pending.tool_name}
            out.append(d)
        return out

    # --- non-blocking driving (mirrors SDKClaudeRunner) -------------------

    def _stash_result(self, s: _TmuxSession, task: asyncio.Task) -> None:
        """Move a completed task's AdvanceResult into the session's pending
        queue so poll_status can deliver it later. Tolerates cancelled tasks."""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            res = AdvanceResult(status="error", assistant_text="",
                                error=str(exc), session_id=s.handle)
        else:
            res = task.result()
        s._pending_results.append(res)
        if len(s._pending_results) > 8:  # hard cap, newest wins
            s._pending_results = s._pending_results[-8:]

    def _harvest_finished(self, handle: str) -> None:
        """Sweep finished tasks (current bg + queued extras) into _pending_results
        so a rapid new start_advance can't drop a previous turn's reply. The
        original bug: _bg[handle] = create_task(...) overwrote the prior ref,
        losing its result — the voice agent then talked about stale output and
        could only see the latest by peek_screen.

        Order matters: _bg holds the OLDEST in-flight task (the one that owns
        _turn_lock), extras are newer. Harvest _bg first so results land in the
        queue in send-order (FIFO)."""
        s = self._sessions.get(handle)
        if s is None:
            return
        task = self._bg.get(handle)
        if task is not None and task.done():
            self._stash_result(s, task)
            self._bg.pop(handle, None)
        still_running: list[asyncio.Task] = []
        for t in s._extra_tasks:
            if t.done():
                self._stash_result(s, t)
            else:
                still_running.append(t)
        s._extra_tasks = still_running

    def start_advance(self, handle: str, message: str) -> None:
        self._get(handle)
        self._harvest_finished(handle)
        # The task name carries the message so list()'s `queue` can show WHAT is
        # running/queued, not just a count.
        task = asyncio.create_task(self.advance(handle, message))
        task.set_name(message)
        if handle in self._bg and not self._bg[handle].done():
            # Previous turn still running. Queue this one — _turn_lock will
            # serialize them and BOTH results land in _pending_results.
            s = self._sessions[handle]
            s._extra_tasks.append(task)
            log.info("start_advance for %s queued behind running turn (%d in queue)",
                     handle, len(s._extra_tasks))
        else:
            self._bg[handle] = task

    def start_answer(self, handle: str, choice: str) -> None:
        s = self._get(handle)
        self._harvest_finished(handle)
        if s.pending is None:
            raise ValueError("no pending prompt to answer — it was already "
                             "resolved (don't answer it again)")
        if s.answer_claimed == s.prompt_seq:
            raise ValueError("that prompt is already being answered — "
                             "don't answer it again")
        s.answer_claimed = s.prompt_seq
        task = asyncio.create_task(self.answer(handle, choice, s.prompt_seq))
        task.set_name(f"answer: {choice}")
        if handle in self._bg and not self._bg[handle].done():
            s._extra_tasks.append(task)
            log.info("start_answer for %s queued behind running turn", handle)
        else:
            self._bg[handle] = task

    def start_builtin_slash(self, handle: str, command: str,
                            settle_secs: float = 1.5, max_wait: float = 60.0) -> None:
        """Run a slash command — hybrid path. Races the Stop hook against a
        screen-settle detector and returns whichever fires first.

        Why hybrid: built-ins like /compact, /context, /clear DON'T fire a Stop
        hook (no assistant turn), so a normal advance() hangs on _stop.wait()
        forever. Skills DO fire a Stop hook. Rather than maintain a list and
        risk missing a built-in, we race both detectors and use the right one
        per-command automatically. `command` should start with '/'."""
        self._get(handle)
        self._harvest_finished(handle)
        task = asyncio.create_task(
            self._run_slash(handle, command, settle_secs, max_wait)
        )
        task.set_name(command)
        if handle in self._bg and not self._bg[handle].done():
            s = self._sessions[handle]
            s._extra_tasks.append(task)
            log.info("start_builtin_slash for %s queued behind running turn", handle)
        else:
            self._bg[handle] = task

    async def _run_slash(self, handle: str, command: str,
                         settle_secs: float, max_wait: float) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s.pending is not None:
                return self._err(s, "a prompt is pending; call answer first")
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            s.turn_prompt = command
            await self._send_message(s, command)

            loop = asyncio.get_event_loop()
            settle_task = asyncio.create_task(self._wait_for_settle(s, settle_secs, max_wait))
            stop_task = asyncio.create_task(s._stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {settle_task, stop_task}, return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (settle_task, stop_task):
                    if not t.done():
                        t.cancel()

            if stop_task in done:
                # Real Claude turn — collect the structured assistant text.
                log.info("slash %s: Stop hook fired (real turn)", command)
                return self._collect(s)

            # Settle detection won → UI-only built-in. The output can be taller
            # than the visible pane (/context routinely is), so re-capture WITH
            # scrollback and slice from the echoed command — returning just the
            # visible pane loses the top of the output (the part with the actual
            # numbers in /context's case).
            log.info("slash %s: screen settled (UI-only built-in)", command)
            full = await self._capture_history(s)
            text = self._slash_output(full, command)
            s.status = "completed"
            return AdvanceResult(status="completed", assistant_text=text,
                                 session_id=s.handle, cost_usd=0.0)

    @staticmethod
    def _slash_output(pane_text: str, command: str) -> str:
        """Extract a slash command's full output from a history-inclusive pane
        capture: everything between the echoed command (`❯ /context`) and the
        live input box, minus transient chrome (feedback survey, footer)."""
        rows = [r.rstrip() for r in pane_text.splitlines()]
        # Start after the LAST echo of the command (the submission this turn).
        for i in range(len(rows) - 1, -1, -1):
            ls = rows[i].lstrip()
            if (ls.startswith("❯") or ls.startswith(">")) and command in ls:
                rows = rows[i + 1:]
                break
        # Cut the live input box and everything under it (footer/shortcuts):
        # the bottom-most `❯` row, plus the box's separator line above it.
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].lstrip().startswith("❯"):
                j = i - 1
                if j >= 0 and rows[j].strip() and set(rows[j].strip()) <= {"─"}:
                    i = j
                rows = rows[:i]
                break
        # Drop the transient feedback survey if it rendered into the tail.
        rows = [
            r for r in rows
            if not r.strip().startswith("● How is Claude doing this session")
            and not re.match(r"^\s*1: Bad\s+2: Fine", r)
        ]
        while rows and not rows[0].strip():
            rows.pop(0)
        while rows and not rows[-1].strip():
            rows.pop()
        return "\n".join(rows[-200:]) or "(slash command ran)"

    async def _wait_for_settle(self, s: _TmuxSession, settle_secs: float,
                                max_wait: float) -> str:
        """Return the visible pane once it has stopped changing for `settle_secs`
        seconds, or once `max_wait` elapses. Captures every 250ms."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max_wait
        last = ""
        stable_since: float | None = None
        while loop.time() < deadline:
            pane = await self._capture(s)
            if pane == last:
                if stable_since is None:
                    stable_since = loop.time()
                elif loop.time() - stable_since >= settle_secs:
                    return pane
            else:
                stable_since = None
                last = pane
            await asyncio.sleep(0.25)
        return last

    def poll_status(self, handle: str) -> dict:
        """Return the oldest unread result for this session, or the live status.
        Order: queued completed result > running task (working) > idle. Always
        sweeps finished tasks into the pending queue first so a turn that
        finished between polls isn't lost."""
        sid = handle if handle in self._sessions else None
        self._harvest_finished(handle)
        s = self._sessions.get(handle)
        if s is not None and s._pending_results:
            return s._pending_results.pop(0).to_dict()
        bg = self._bg.get(handle)
        extras_running = s is not None and any(not t.done() for t in s._extra_tasks)
        if bg is not None and not bg.done():
            return {"status": "working", "session_id": sid}
        if extras_running:
            return {"status": "working", "session_id": sid}
        return {"status": "idle", "session_id": sid}

    # --- event tail + transcript reading ----------------------------------

    async def _tail_events(self, s: _TmuxSession) -> None:
        path = os.path.join(s.ctrl, "events.jsonl")
        loop = asyncio.get_event_loop()
        next_reconcile = 0.0
        while not s._closed:
            try:
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        f.seek(s._evpos)
                        chunk = f.read()
                    if chunk:
                        s._evpos += len(chunk)
                        s._evbuf += chunk.decode(errors="replace")
                        parts = s._evbuf.split("\n")
                        s._evbuf = parts.pop()
                        for line in parts:
                            if line.strip():
                                await self._handle_event(s, line)
                # A keyboard answer in the live TUI fires no event, so poll the
                # pane (throttled — a capture is a subprocess) to retire prompts
                # answered directly in the CLI.
                now = loop.time()
                if s.pending is not None and now >= next_reconcile:
                    next_reconcile = now + 0.4
                    await self._reconcile_pending(s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tail error for %s", s.handle)
            await asyncio.sleep(0.05)

    async def _handle_event(self, s: _TmuxSession, line: str) -> None:
        try:
            ev = json.loads(line)
        except Exception:
            return
        tp = ev.get("transcript_path")
        if tp and not s.transcript_path:
            s.transcript_path = tp
        kind = ev.get("event")
        if kind == "tool":
            name = ev.get("tool_name", "")
            if name:
                s.tools_used.append(name)
                log_event("claude", "backend", "hook", f"tool: {name}", session=_slabel(s),
                          detail=ev)
        elif kind == "needs_permission":
            s.pending = Prompt(
                kind="permission",
                text=_summarize_tool(ev.get("tool_name", ""), ev.get("tool_input", {})),
                options=["allow", "deny"],
                tool_name=ev.get("tool_name", ""),
            )
            s.pending_tool_use_id = ev.get("tool_use_id")
            s.prompt_seq += 1
            s.status = "needs_permission"
            log_event("claude", "backend", "hook",
                      f"needs permission: {s.pending.text}", session=_slabel(s), detail=ev)
            s._stop.set()
        elif kind == "needs_choice":
            s.questions = _parse_questions(ev.get("tool_input", {}))
            s.q_index = 0
            s.pending = self._prompt_for_question(s, 0, ev.get("tool_name", ""))
            s.pending_tool_use_id = ev.get("tool_use_id")
            s.prompt_seq += 1
            s.status = "needs_choice"
            log_event("claude", "backend", "hook",
                      f"needs choice: {s.pending.text}", session=_slabel(s), detail=ev)
            s._stop.set()
        elif kind == "turn_complete":
            await self._read_new_text(s)
            s.status = "completed"
            log_event("claude", "backend", "hook", "turn complete", session=_slabel(s))
            s._stop.set()

    async def _read_new_text(self, s: _TmuxSession, settle: float = 0.7,
                             max_wait: float = 10.0) -> None:
        """Collect ALL assistant text of the just-finished turn.

        Two failure modes this must survive (both seen live):
        - the Stop hook can fire a beat BEFORE Claude's final message line is
          flushed to the jsonl;
        - a tool-using turn streams preamble text ("I'll search…") early, then
          its real answer only after the tool round completes.

        The old code broke out of the read loop on the FIRST non-empty extract,
        so it captured only the preamble; the real answer then bled into the
        NEXT turn's read. The voice therefore narrated the previous prompt's
        result and called the just-finished one "still processing" (and the
        last turn's answer could be dropped entirely).

        Fix: wait for the TUI to stop changing before reading. The spinner
        animates continuously while Claude works, so a stable screen is the true
        "turn done" signal even when the Stop hook fired early. Then read every
        new complete jsonl line. Because this runs *before* `_stop.set()` in
        `_handle_event`, the next queued prompt is held back until this turn is
        fully captured — fixing the cross-turn mis-attribution at the source."""
        await self._wait_for_settle(s, settle, max_wait)
        text = self._extract(s)
        # The closing line may flush a hair after the screen settles; a few short
        # grace reads catch it without busy-waiting.
        for _ in range(4):
            if text.strip():
                break
            await asyncio.sleep(0.15)
            text += self._extract(s)
        if text:
            s._delta.append(text)
            s._transcript.append(text)
            log_event("claude", "backend", "assistant", text, session=_slabel(s),
                      detail={"handle": s.handle, "text": text})

    def _extract(self, s: _TmuxSession) -> str:
        path = self._find_transcript(s)
        if not path or not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(s.jsonl_pos)
            chunk = f.read()
        if not chunk:
            return ""
        # Only consume complete lines; leave a trailing partial for next time.
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            return ""
        s.jsonl_pos += last_nl + 1
        out: list[str] = []
        for raw in chunk[: last_nl + 1].decode(errors="replace").splitlines():
            if not raw.strip():
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if o.get("type") == "assistant":
                for b in o.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        out.append(b.get("text", ""))
                    elif b.get("type") == "tool_use":
                        s.tools_used.append(b.get("name", ""))
        return "".join(out)

    def _update_cost(self, s: _TmuxSession) -> None:
        """Refresh s.cost_usd from the full transcript's token usage.

        Reads the whole jsonl and sums API-equivalent cost across every
        assistant message — accurate regardless of resume (a resumed session's
        prior turns still count). Cheap on repeat calls: if the transcript's
        byte-size is unchanged since the last scan, the cached value is kept, so
        the frequent polling list()/poll path doesn't re-read a static file.
        """
        path = self._find_transcript(s)
        if not path or not os.path.exists(path):
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size == s._cost_scan_size:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        s.cost_usd = pricing.cost_for_transcript_lines(lines)
        s._cost_scan_size = size

    def _find_transcript(self, s: _TmuxSession) -> str | None:
        if s.transcript_path and os.path.exists(s.transcript_path):
            return s.transcript_path
        matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{s.handle}.jsonl"))
        if matches:
            s.transcript_path = matches[0]
            return matches[0]
        return None

    # --- internals --------------------------------------------------------

    def _get(self, handle: str) -> _TmuxSession:
        s = self._sessions.get(handle)
        if s is None:
            raise KeyError(f"unknown session: {handle}")
        return s

    def _collect(self, s: _TmuxSession) -> AdvanceResult:
        text = "".join(s._delta)
        s._delta.clear()
        self._update_cost(s)  # the finished turn added usage to the transcript
        return AdvanceResult(
            status=s.status, assistant_text=text, prompt=s.pending,
            error=s.error, session_id=s.handle, cost_usd=round(s.cost_usd, 4),
            request=s.turn_prompt,
        )

    def _err(self, s: _TmuxSession, msg: str) -> AdvanceResult:
        return AdvanceResult(status="error", assistant_text="", error=msg, session_id=s.handle)
