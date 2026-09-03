"""Getting hold of an OpenCode server, and letting go of it correctly.

Two ways to have one: attach to a server already answering at the configured
URL, or spawn `opencode serve` and manage it. The governing rule is that Yuri
never stops a server she did not start — `owned` is decided once, at
acquisition, and release() terminates the process only when it is true. A
server the user runs survives Yuri's shutdown, her restart and her crash; that
is the whole reason the attach branch exists.

Acquisition is lazy: nothing spawns at Yuri startup, and an asyncio.Lock means
two concurrent start_session calls cannot race into two servers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from urllib.parse import urlsplit

from ..base import ProviderUnavailable
from .client import DEFAULT_USERNAME, OpenCodeClient

log = logging.getLogger("yuri.opencode.server")

READY_POLL_INTERVAL = 0.25
PROBE_TIMEOUT = 3.0
TERMINATE_GRACE = 5.0
DEFAULT_PORT = 4096          # OpenCode's own default for `opencode serve`


class OpenCodeUnavailable(ProviderUnavailable):
    """No server could be attached to, and none could (or may) be spawned.
    The message says what to do about it.

    A ProviderUnavailable so /tools/execute shows that message rather than
    replacing it with "the tool failed unexpectedly" -- still a RuntimeError,
    so nothing that already catches one changes behaviour."""


class OpenCodeServer:
    def __init__(self, url: str, *, spawn: bool = True, binary: str = "opencode",
                 password: str | None = None, username: str = DEFAULT_USERNAME,
                 cwd: str | None = None,
                 log_path: str | None = None, ready_timeout: float = 20.0,
                 env: dict[str, str] | None = None) -> None:
        self._url = url.rstrip("/")
        self._spawn_allowed = spawn
        self._binary = binary
        self._password = password
        self._username = username or DEFAULT_USERNAME
        self._cwd = cwd
        self._log_path = log_path
        self._ready_timeout = ready_timeout
        # Design spec section 4 wants the child to inherit no Yuri secrets, but
        # which names are secret is not knowable here: OPENAI_API_KEY and
        # GEMINI_API_KEY are both Yuri's voice-model vars AND plausibly
        # OpenCode's own provider auth, and VC_AUTH_TOKEN lives in config,
        # which this layer may not import. So the decision belongs to the
        # caller that knows: pass `env` and it is used verbatim. None keeps the
        # inherit-everything default. Task 7 builds the filtered one.
        self._env = env
        self._client: OpenCodeClient | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._owned = False
        self._lock = asyncio.Lock()
        self.spawn_count = 0          # for tests: proves the lock works

    # --- state -----------------------------------------------------------

    @property
    def password(self) -> str | None:
        """The server password, for a child that must authenticate the same way.

        Public because the live-view pane runs `opencode attach`, which needs
        it in ITS environment. Still never rendered: callers put it in an env
        dict, never in argv (a tmux command line is world-readable via ps) and
        never in a log line.
        """
        return self._password

    @property
    def username(self) -> str:
        return self._username

    @property
    def can_spawn(self) -> tuple[bool, str]:
        """(could this object start a server, why not) — without starting one.

        `health()` needs the difference between "nothing is answering, but a
        session would start one fine" and "nothing is answering and nothing
        can". Reporting the first as offline made Yuri tell the user her new
        agent was unavailable in the default configuration, on every fresh
        boot, before the first OpenCode session had ever run.
        """
        if not self._spawn_allowed:
            return False, ("OPENCODE_SPAWN=0, so Yuri will not start one — run "
                           "`opencode serve` yourself, or set OPENCODE_SPAWN=1")
        # Exactly _spawn's own resolution, including the os.access check: an
        # explicit OPENCODE_BIN path that does not exist or is not executable
        # would otherwise be reported spawnable and then fail at spawn time.
        found = (self._binary if os.path.sep in self._binary
                 else shutil.which(self._binary))
        if not found or not os.access(found, os.X_OK):
            return False, (f"{self._binary!r} was not found or is not executable "
                           "— install OpenCode, or set OPENCODE_BIN to its full path")
        return True, found

    @property
    def url(self) -> str:
        """The base URL, normalised (no trailing slash).

        Public because diagnostics need it when there is no client to ask:
        `health()` deliberately never acquires, so naming the address is most
        of what makes its message actionable ("did not answer at
        http://127.0.0.1:4096" tells the user what to fix).
        """
        return self._url

    @property
    def owned(self) -> bool:
        """True only for a server this object spawned. The kill switch."""
        return self._owned

    @property
    def client(self) -> OpenCodeClient | None:
        return self._client

    async def is_reachable(self) -> bool:
        probe = OpenCodeClient(self._url, password=self._password,
                              username=self._username,
                               timeout=PROBE_TIMEOUT)
        try:
            await probe.get("/api/session")
            return True
        except Exception:
            return False
        finally:
            # Every throwaway client on every path, or the suite grows
            # ResourceWarnings.
            await probe.close()

    # --- acquire / release ------------------------------------------------

    async def acquire(self) -> OpenCodeClient:
        """Attach if something answers, else spawn. Idempotent and race-safe."""
        if self._client is not None and not self._spawned_and_dead():
            return self._client
        async with self._lock:
            if self._client is not None and not self._spawned_and_dead():
                return self._client          # another waiter won
            if self._spawned_and_dead():
                # A server we started has exited. Design spec section 4: it is
                # re-acquired on the next call rather than poisoning the
                # provider for the rest of the run. Only ever ours — an
                # attached server that dies is reported offline and left
                # alone, because taking over the user's port is not ours to do.
                log.warning("the OpenCode server Yuri started exited (rc=%s); "
                            "re-acquiring", self._proc.returncode)
                await self._forget()
            if await self.is_reachable():
                log.info("attached to an existing OpenCode server at %s", self._url)
                self._client = OpenCodeClient(self._url, password=self._password,
                                          username=self._username)
                self._owned = False           # NOT ours: never stop it
                return self._client
            if not self._spawn_allowed:
                raise OpenCodeUnavailable(
                    f"OpenCode is not reachable at {self._url} and spawning is "
                    "disabled (OPENCODE_SPAWN=0). Start it with "
                    f"`opencode serve --port {self._port()}`, or set OPENCODE_SPAWN=1.")
            await self._spawn()
            self._client = OpenCodeClient(self._url, password=self._password,
                                          username=self._username)
            self._owned = True                # ours: release() stops it
            return self._client

    async def release(self) -> None:
        """Drop the client, and stop the process only if we started it.

        Deliberately not lock-guarded: _spawn() calls it on a failed spawn
        while already holding the lock. The residual race is a concurrent
        caller-initiated release() during a spawn, which can only ever
        double-handle a process this object itself started -- it cannot reach
        a server Yuri attached to, because _proc is only ever set by _spawn().
        """
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
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE)
            except (TimeoutError, asyncio.TimeoutError):
                # It ignored SIGTERM; Yuri's own shutdown must not hang on it.
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
            except ProcessLookupError:
                # It exited in the gap between the check and the signal. A
                # raise here would abort shutdown over a process that is
                # already gone.
                pass
        log.info("stopped the OpenCode server Yuri started")

    def _spawned_and_dead(self) -> bool:
        """A server we started that is no longer running."""
        return (self._owned and self._proc is not None
                and self._proc.returncode is not None)

    async def _forget(self) -> None:
        """Drop a dead spawned server's state without killing anything: the
        process is already gone, so there is nothing to terminate."""
        client, self._client = self._client, None
        if client is not None:
            await client.close()
        self._proc = None
        self._owned = False

    # --- spawning ---------------------------------------------------------

    def _port(self) -> str:
        """The port to spawn on, as the string that goes on the command line.

        urlsplit knows the URL grammar; the hand-rolled version this replaces
        returned '' for a portless URL ('http://localhost' splits to a tail of
        '//localhost', which contains '/', so the wrong branch won), which
        would have run `opencode serve --port ""` and printed an empty port in
        the refusal above.
        """
        try:
            port = urlsplit(self._url).port
        except ValueError:          # a non-numeric port in the URL
            port = None
        # `port or DEFAULT_PORT` would rewrite an explicit :0 to 4096.
        return str(DEFAULT_PORT if port is None else port)

    async def _spawn(self) -> None:
        binary = self._binary if os.path.sep in self._binary else shutil.which(self._binary)
        # os.access rather than os.path.exists: an explicit OPENCODE_BIN path
        # that is present but not executable would otherwise reach
        # create_subprocess_exec and come back as a raw PermissionError, which
        # is not a refusal anything upstream knows how to narrate.
        if not binary or not os.access(binary, os.X_OK):
            raise OpenCodeUnavailable(
                f"the OpenCode binary {self._binary!r} was not found or is not "
                "executable — install OpenCode, or set OPENCODE_BIN to its full "
                "path, or set OPENCODE_SPAWN=0 and run `opencode serve` yourself.")
        env = dict(os.environ) if self._env is None else dict(self._env)
        if self._password:
            # The name in the server's own startup log (design spec section 2).
            env["OPENCODE_SERVER_PASSWORD"] = self._password
            # So the child expects the same username we send.
            env["OPENCODE_SERVER_USERNAME"] = self._username
        # Its output belongs in a log, not in the terminal the voice UI owns:
        # with a log path both streams append there, without one both are
        # discarded. Either way nothing reaches our stdout.
        sink = None
        try:
            if self._log_path:
                os.makedirs(os.path.dirname(self._log_path) or ".", exist_ok=True)
                sink = open(self._log_path, "ab")
                stdout, stderr = sink, asyncio.subprocess.STDOUT
            else:
                stdout = stderr = asyncio.subprocess.DEVNULL
            self._proc = await asyncio.create_subprocess_exec(
                binary, "serve", "--port", self._port(), "--hostname", "127.0.0.1",
                cwd=self._cwd or None, env=env, stdout=stdout, stderr=stderr,
            )
        except OSError as exc:
            # An unwritable log path or an unusable cwd. Every way spawning can
            # fail leaves here as the one type callers handle, so an OpenCode
            # misconfiguration cannot surface as a bare OSError from a tool call
            # (plan section 41: provider failures stay isolated).
            raise OpenCodeUnavailable(
                f"could not start {binary!r}: {exc}") from exc
        finally:
            if sink is not None:
                sink.close()        # the child inherited the fd; this copy is ours
        self.spawn_count += 1
        # Before the readiness wait, deliberately: if it never becomes ready,
        # release() below is what stops the child, and release() only stops
        # what is owned. Setting this after the wait would leak the process.
        self._owned = True
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
