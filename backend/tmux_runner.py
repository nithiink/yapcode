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
import shlex
import shutil
import sys
from uuid import uuid4

from claude_runner import (
    AdvanceResult,
    ClaudeRunner,
    MODE_CYCLE,
    Prompt,
    Status,
    _ALLOW_WORDS,
    _parse_question,
    _summarize_tool,
    normalize_mode,
)
from permissions import classify

log = logging.getLogger("voice-claude.tmux")

CTRL_ROOT = os.path.expanduser("~/.voice-claude/tmux")
HOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmux_hooks")
ENABLE_CHROME = os.getenv("CLAUDE_CLI_CHROME", "1") != "0"


class _TmuxSession:
    def __init__(self, handle: str, cwd: str, model: str):
        self.handle = handle               # == --session-id, also handoff id
        self.cwd = cwd                      # realpath
        self.model = model
        self.mode = "default"               # permission mode (Shift+Tab cycle)
        self.pane = f"vc_{handle[:8]}"
        self.ctrl = os.path.join(CTRL_ROOT, handle)
        self.transcript_path: str | None = None
        self.jsonl_pos = 0                  # bytes of transcript consumed
        self.status: Status = "running"
        self.error: str | None = None
        self.cost_usd = 0.0                 # interactive = subscription, no $ in jsonl
        self._delta: list[str] = []
        self._transcript: list[str] = []
        self.tools_used: list[str] = []
        self.pending: Prompt | None = None
        self.pending_tool_use_id: str | None = None
        self._stop = asyncio.Event()
        self._turn_lock = asyncio.Lock()
        self._evpos = 0                     # bytes of events.jsonl consumed
        self._evbuf = ""
        self._tail: asyncio.Task | None = None
        self._closed = False


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

    async def _alive(self, s: _TmuxSession) -> bool:
        rc, _ = await self._tmux("has-session", "-t", s.pane)
        return rc == 0

    # --- lifecycle --------------------------------------------------------

    async def start(self, cwd: str, model: str | None = None, mode: str = "default") -> str:
        if shutil.which("tmux") is None:
            raise ValueError("tmux is not installed (brew install tmux) — required for the CLI backend")
        if shutil.which("claude") is None:
            raise ValueError("the `claude` CLI is not on PATH")
        cwd = os.path.realpath(os.path.expanduser(cwd))
        if not os.path.isdir(cwd):
            raise ValueError(f"not a directory: {cwd}")

        handle = str(uuid4())
        s = _TmuxSession(handle, cwd, model or self._default_model)
        s.mode = normalize_mode(mode)
        os.makedirs(os.path.join(s.ctrl, "decisions"), exist_ok=True)
        self._write_settings(s)
        self._write_meta(s)

        chrome = "--chrome " if ENABLE_CHROME else ""
        inner = (
            f"VC_CTRL={shlex.quote(s.ctrl)} "
            f"claude --session-id {handle} --model {shlex.quote(s.model)} "
            f"--permission-mode {shlex.quote(s.mode)} "
            f"{chrome}--settings {shlex.quote(os.path.join(s.ctrl, 'settings.json'))}"
        )
        rc, out = await self._tmux(
            "new-session", "-d", "-s", s.pane, "-c", cwd, "-x", "220", "-y", "50", inner
        )
        if rc != 0:
            raise ValueError(f"failed to start tmux session: {out.strip()}")

        self._sessions[handle] = s
        s._tail = asyncio.create_task(self._tail_events(s))
        await self._await_ready(s)
        log.info("tmux session %s started in %s (chrome=%s)", handle, cwd, ENABLE_CHROME)
        return handle

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
            json.dump({"handle": s.handle, "cwd": s.cwd, "model": s.model, "pane": s.pane}, f)

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
        for t in self._bg.values():
            if not t.done():
                t.cancel()
        self._bg.clear()
        for s in list(self._sessions.values()):
            s._closed = True
            if s._tail and not s._tail.done():
                s._tail.cancel()
            await self._tmux("kill-session", "-t", s.pane)
            shutil.rmtree(s.ctrl, ignore_errors=True)
        self._sessions.clear()

    # --- driving ----------------------------------------------------------

    async def advance(self, handle: str, message: str) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s.pending is not None:
                return self._err(s, "a prompt is pending; call answer first")
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            await self._send_message(s, message)
            await s._stop.wait()
            return self._collect(s)

    async def answer(self, handle: str, choice: str) -> AdvanceResult:
        s = self._get(handle)
        async with s._turn_lock:
            if s.pending is None:
                return self._err(s, "no pending prompt to answer")
            kind = s.pending.kind
            s._delta.clear()
            s._stop.clear()
            s.status = "running"
            if kind == "permission":
                self._write_decision(s, choice)
            else:  # choice / AskUserQuestion menu
                await self._answer_question(s, choice)
            s.pending = None
            s.pending_tool_use_id = None
            await s._stop.wait()
            return self._collect(s)

    async def _send_message(self, s: _TmuxSession, message: str) -> None:
        # Single literal line, a pause, then Enter. Internal newlines are
        # flattened (voice input is one utterance). The pause matters: the TUI
        # has paste detection, so an Enter sent in the same burst as the text is
        # treated as a newline rather than a submit. Sending it separately, after
        # the input settles, makes Enter submit. Verify via capture-pane and
        # retry once if the text is still sitting unsent.
        text = " ".join(message.splitlines())
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

    def _write_decision(self, s: _TmuxSession, choice: str) -> None:
        c = (choice or "").strip().lower()
        allow = c in _ALLOW_WORDS or any(c.startswith(w) for w in _ALLOW_WORDS)
        path = os.path.join(s.ctrl, "decisions", f"{s.pending_tool_use_id or 'none'}.json")
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"decision": "allow" if allow else "deny",
                       "reason": "" if allow else f"user said: {choice}"}, f)
        os.replace(tmp, path)

    async def _answer_question(self, s: _TmuxSession, choice: str) -> None:
        """Drive the AskUserQuestion selection menu.

        The menu numbers each option (1..N) and a number key selects it
        immediately — far more robust than counting arrow keys. After the listed
        options it shows a "Type something." entry (N+1) for a free-form answer
        and "Chat about this" (N+2). We match the spoken choice to a listed
        option and press its digit; if nothing matches, we use "Type something"
        and type the answer.
        """
        options = s.pending.options if s.pending else []
        await self._wait_for_menu(s)
        pane = await self._capture(s)
        c = (choice or "").strip().lower()

        # Multi-select: options render as "[ ]" checkboxes and need an explicit
        # Submit. Toggle each option named in the spoken answer (number key
        # toggles), then move right to the Submit tab and confirm.
        if "[ ]" in pane or "[✔]" in pane:
            for i, o in enumerate(options):
                if i < 9 and o.strip().lower() in c:
                    await self._tmux("send-keys", "-t", s.pane, str(i + 1))
                    await asyncio.sleep(0.25)
            for _ in range(5):  # navigate right to the review/Submit screen
                p = await self._capture(s)
                if "Submit answers" in p or "Ready to submit" in p:
                    break
                await self._tmux("send-keys", "-t", s.pane, "Right")
                await asyncio.sleep(0.4)
            await self._tmux("send-keys", "-t", s.pane, "1")  # "Submit answers"
            return

        idx = next(
            (i for i, o in enumerate(options)
             if o.strip().lower() == c or (c and c in o.strip().lower())),
            -1,
        )
        if 0 <= idx < 9:
            await self._tmux("send-keys", "-t", s.pane, str(idx + 1))
            return
        if idx >= 9:  # rare long list: arrow-navigate then Enter
            for _ in range(idx):
                await self._tmux("send-keys", "-t", s.pane, "Down")
                await asyncio.sleep(0.05)
            await self._tmux("send-keys", "-t", s.pane, "Enter")
            return
        # No listed option matched — give a free-form answer via "Type something".
        type_idx = len(options) + 1
        if 1 <= type_idx <= 9 and choice.strip():
            await self._tmux("send-keys", "-t", s.pane, str(type_idx))
            await asyncio.sleep(0.4)
            await self._tmux("send-keys", "-t", s.pane, "-l", "--", choice.strip())
            await asyncio.sleep(0.4)
            await self._tmux("send-keys", "-t", s.pane, "Enter")
        else:
            await self._tmux("send-keys", "-t", s.pane, "Enter")

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

    async def interrupt(self, handle: str) -> None:
        s = self._get(handle)
        bg = self._bg.pop(handle, None)
        if bg and not bg.done():
            bg.cancel()
        if s.pending and s.pending.kind == "permission":
            self._write_decision(s, "deny")
        await self._tmux("send-keys", "-t", s.pane, "Escape")
        s.pending = None
        s.status = "completed"
        s._stop.set()

    async def close(self, handle: str) -> None:
        s = self._get(handle)
        bg = self._bg.pop(handle, None)
        if bg and not bg.done():
            bg.cancel()
        if s.pending and s.pending.kind == "permission":
            self._write_decision(s, "deny")  # unblock any parked PreToolUse hook
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
        return s.mode

    async def read(self, handle: str) -> str:
        return "".join(self._get(handle)._transcript)

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

    def list(self) -> list[dict]:
        return [
            {"handle": s.handle, "session_id": s.handle, "cwd": s.cwd,
             "model": s.model, "mode": s.mode, "status": s.status, "cost_usd": 0.0}
            for s in self._sessions.values()
        ]

    # --- non-blocking driving (mirrors SDKClaudeRunner) -------------------

    def start_advance(self, handle: str, message: str) -> None:
        self._get(handle)
        self._bg[handle] = asyncio.create_task(self.advance(handle, message))

    def start_answer(self, handle: str, choice: str) -> None:
        self._get(handle)
        self._bg[handle] = asyncio.create_task(self.answer(handle, choice))

    def poll_status(self, handle: str) -> dict:
        task = self._bg.get(handle)
        sid = handle if handle in self._sessions else None
        if task is None:
            return {"status": "idle", "session_id": sid}
        if not task.done():
            return {"status": "working", "session_id": sid}
        self._bg.pop(handle, None)
        exc = task.exception()
        if exc is not None:
            return {"status": "error", "error": str(exc), "session_id": sid}
        return task.result().to_dict()

    # --- event tail + transcript reading ----------------------------------

    async def _tail_events(self, s: _TmuxSession) -> None:
        path = os.path.join(s.ctrl, "events.jsonl")
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
        elif kind == "needs_permission":
            s.pending = Prompt(
                kind="permission",
                text=_summarize_tool(ev.get("tool_name", ""), ev.get("tool_input", {})),
                options=["allow", "deny"],
                tool_name=ev.get("tool_name", ""),
            )
            s.pending_tool_use_id = ev.get("tool_use_id")
            s.status = "needs_permission"
            s._stop.set()
        elif kind == "needs_choice":
            text, options = _parse_question(ev.get("tool_input", {}))
            s.pending = Prompt(kind="choice", text=text, options=options,
                               tool_name=ev.get("tool_name", ""))
            s.pending_tool_use_id = ev.get("tool_use_id")
            s.status = "needs_choice"
            s._stop.set()
        elif kind == "turn_complete":
            await self._read_new_text(s)
            s.status = "completed"
            s._stop.set()

    async def _read_new_text(self, s: _TmuxSession) -> None:
        """Collect assistant text produced since the last read, tolerating the
        Stop hook firing just before the final line is flushed."""
        text = ""
        for _ in range(10):  # ~1.5s
            text += self._extract(s)
            if text.strip():
                break
            await asyncio.sleep(0.15)
        if text:
            s._delta.append(text)
            s._transcript.append(text)

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
        return AdvanceResult(
            status=s.status, assistant_text=text, prompt=s.pending,
            error=s.error, session_id=s.handle, cost_usd=0.0,
        )

    def _err(self, s: _TmuxSession, msg: str) -> AdvanceResult:
        return AdvanceResult(status="error", assistant_text="", error=msg, session_id=s.handle)
