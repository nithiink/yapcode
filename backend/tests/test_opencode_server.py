"""The server lifecycle, and the one rule that matters most: Yuri never stops
a server she did not start.

Attaching and spawning are tested as the two genuinely different things they
are: an attached server must survive release() (the safety-critical case, and
the entire reason the attach branch exists), a spawned one must not. Spawning
is exercised with a stub binary rather than the real `opencode`, so these
tests need nothing installed, no network and no credentials.

    python -m unittest discover -s backend/tests
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.dirname(__file__))

import config  # noqa: E402
from fake_opencode import FakeOpenCode  # noqa: E402
from yuri.providers.opencode.server import (  # noqa: E402
    OpenCodeServer, OpenCodeUnavailable)


# A stand-in for `opencode serve` that answers the one endpoint acquire()
# probes. Written as a real .py file and exec'd by a two-line sh wrapper: the
# plan generated it by interpolating the source into a shell single-quoted
# string, which breaks on any apostrophe in the body (and whose .replace()
# guard never fired, because the f-string had already substituted the body).
_STUB_SOURCE = '''\
"""Not OpenCode. Just something that answers, so acquire() has a live port."""
import json
import os
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

READY = sys.argv[1] == "ready"
PORT = int(sys.argv[2])

# So a test can see exactly which environment the child was handed.
if os.environ.get("YURI_ENV_DUMP"):
    with open(os.environ["YURI_ENV_DUMP"], "w") as fh:
        for k, v in os.environ.items():
            print(f"{k}={v}", file=fh)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(HTTPServer):
    """HTTPServer.server_bind() calls socket.getfqdn() unconditionally. That
    reverse-DNS lookup takes ~35s in this sandbox -- longer than the readiness
    timeout the spawn test allows, so the plan's stub would have timed out
    every time. fake_opencode.py skips it for exactly the same reason."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


if not READY:
    time.sleep(600)          # alive, but nothing will ever answer

srv = Server(("127.0.0.1", PORT), Handler)
print("stub stdout: listening", flush=True)
print("stub stderr: warming up", file=sys.stderr, flush=True)
srv.serve_forever()
'''


def _stub_binary(dirpath: str, port: int, *, ready: bool = True) -> str:
    """Write the stub and an sh wrapper that execs it, and return the wrapper.

    `exec` matters: it replaces the shell, so the pid OpenCodeServer holds is
    the pid of the thing actually listening -- terminate() reaches the server
    rather than a shell that would orphan it. Both files are named
    opencode-stub* so a leaked child is findable with `pgrep -f opencode-stub`.
    """
    stub = os.path.join(dirpath, "opencode-stub.py")
    with open(stub, "w") as f:
        f.write(_STUB_SOURCE)
    path = os.path.join(dirpath, "opencode-stub")
    with open(path, "w") as f:
        f.write("#!/bin/sh\n"
                f'exec "{sys.executable}" "{stub}" '
                f'{"ready" if ready else "never-ready"} {port}\n')
    os.chmod(path, 0o755)
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Attach(unittest.IsolatedAsyncioTestCase):
    async def test_attaches_to_a_reachable_server_and_does_not_own_it(self):
        with FakeOpenCode() as fake:
            # A binary that does not exist: if this test ever spawns, it fails.
            srv = OpenCodeServer(fake.url, spawn=True, binary="definitely-not-a-binary")
            try:
                client = await srv.acquire()
                self.assertFalse(srv.owned, "an attached server must not be owned")
                self.assertEqual(srv.spawn_count, 0)
                self.assertEqual(client.base_url, fake.url.rstrip("/"))
                self.assertIs(srv.client, client)
            finally:
                await srv.release()
            # THE RULE: release must not have stopped a server Yuri did not start.
            probe = OpenCodeServer(fake.url, spawn=False)
            self.assertTrue(await probe.is_reachable(),
                            "release() stopped a server Yuri did not start")

    async def test_client_is_none_until_acquired(self):
        with FakeOpenCode() as fake:
            srv = OpenCodeServer(fake.url, spawn=False)
            self.assertIsNone(srv.client)
            self.assertFalse(srv.owned)
            try:
                await srv.acquire()
                self.assertIsNotNone(srv.client)
            finally:
                await srv.release()
            self.assertIsNone(srv.client)

    async def test_acquire_is_idempotent(self):
        with FakeOpenCode() as fake:
            srv = OpenCodeServer(fake.url, spawn=False)
            try:
                a = await srv.acquire()
                b = await srv.acquire()
                self.assertIs(a, b)
            finally:
                await srv.release()

    async def test_concurrent_acquire_yields_one_client(self):
        with FakeOpenCode() as fake:
            srv = OpenCodeServer(fake.url, spawn=False)
            try:
                got = await asyncio.gather(*(srv.acquire() for _ in range(5)))
                self.assertEqual(len({id(c) for c in got}), 1)
            finally:
                await srv.release()

    async def test_password_is_carried_into_the_client(self):
        with FakeOpenCode() as fake:
            fake.state.require_password = "pw"
            srv = OpenCodeServer(fake.url, spawn=False, password="pw")
            try:
                client = await srv.acquire()
                self.assertIsInstance(await client.get("/api/session"), list)
            finally:
                await srv.release()

    async def test_a_wrong_password_is_not_reachable(self):
        # Proves the password test above is not passing by accident: the same
        # server with the wrong password does not answer.
        with FakeOpenCode() as fake:
            fake.state.require_password = "pw"
            srv = OpenCodeServer(fake.url, spawn=False, password="nope")
            self.assertFalse(await srv.is_reachable())

    async def test_release_before_acquire_is_a_no_op(self):
        # provider.shutdown() calls release() unconditionally, including on a
        # provider that never got as far as acquiring anything.
        srv = OpenCodeServer("http://127.0.0.1:1", spawn=False)
        await srv.release()
        self.assertIsNone(srv.client)
        self.assertFalse(srv.owned)

    async def test_a_dead_attached_server_is_never_replaced_by_a_spawn(self):
        # Design spec section 4: a dead *attached* server is reported offline;
        # Yuri does not take over someone else's port. Spawning here would put
        # her in charge of a port the user had claimed.
        with FakeOpenCode() as fake:
            url = fake.url
            srv = OpenCodeServer(url, spawn=True, binary="definitely-not-a-binary")
            client = await srv.acquire()
        try:
            self.assertFalse(await srv.is_reachable())   # the fake is gone
            self.assertIs(await srv.acquire(), client)   # no spawn attempted
            self.assertEqual(srv.spawn_count, 0)
            self.assertFalse(srv.owned)
        finally:
            await srv.release()


class Spawn(unittest.IsolatedAsyncioTestCase):
    async def test_spawns_when_nothing_answers_and_owns_it(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            # A log directory that does not exist yet: ~/Yuri has no logs/ dir
            # until something makes one, and a spawn must not fail on that.
            log_path = os.path.join(d, "logs", "oc.log")
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d, log_path=log_path,
                                 ready_timeout=20.0)
            try:
                client = await srv.acquire()
                self.assertTrue(srv.owned, "a spawned server must be owned")
                self.assertEqual(srv.spawn_count, 1)
                self.assertIsInstance(await client.get("/api/session"), list)
            finally:
                await srv.release()
            # An owned server IS stopped on release.
            gone = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=False)
            self.assertFalse(await gone.is_reachable())
            # Both streams reached the log, and so neither reached the
            # terminal the voice UI owns.
            with open(log_path) as f:
                logged = f.read()
            self.assertIn("stub stdout: listening", logged)
            self.assertIn("stub stderr: warming up", logged)

    async def test_spawn_disabled_refuses_with_an_actionable_message(self):
        port = _free_port()
        srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=False)
        with self.assertRaises(OpenCodeUnavailable) as cm:
            await srv.acquire()
        msg = str(cm.exception)
        self.assertIn(str(port), msg)
        self.assertRegex(msg, r"OPENCODE_SPAWN|not reachable|start")

    async def test_a_portless_url_refuses_with_opencodes_default_port(self):
        # The plan's hand-rolled parser returned '' here (for
        # 'http://localhost' the tail is '//localhost', which contains '/', so
        # the digit branch won), which would have printed an empty port in this
        # message and run `opencode serve --port ""`.
        cases = {
            "http://localhost": "4096",
            "https://oc.example.com": "4096",
            "http://127.0.0.1:4096/": "4096",
            "http://127.0.0.1:9999": "9999",
            "http://localhost:not-a-port": "4096",
            "http://[::1]:7000": "7000",        # IPv6 literal
            "http://localhost:0": "0",          # `port or DEFAULT` would say 4096
        }
        for url, want in cases.items():
            got = OpenCodeServer(url, spawn=False)._port()
            self.assertEqual(got, want, url)
            self.assertIsInstance(got, str, url)   # it goes on a command line

    async def test_the_base_url_is_public_and_normalised(self):
        # provider.py's health message names the address ("did not answer at
        # ..." is most of what makes it actionable), and health() must not
        # acquire, so there is usually no client to ask for it. It read the
        # private attribute until this property existed.
        self.assertEqual(OpenCodeServer("http://127.0.0.1:4096/", spawn=False).url,
                         "http://127.0.0.1:4096")
        self.assertEqual(OpenCodeServer("http://127.0.0.1:4096", spawn=False).url,
                         "http://127.0.0.1:4096")

    async def test_an_explicit_env_is_used_verbatim_and_is_the_seam_for_secrets(self):
        """Design spec section 4 wants the child to inherit no Yuri secrets, but
        which names are secret is only knowable at the construction site (task
        7). This is that seam: what the caller passes is what the child gets."""
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            dump = os.path.join(d, "env.txt")
            os.environ["YURI_TEST_SECRET"] = "leaked"
            try:
                srv = OpenCodeServer(
                    f"http://127.0.0.1:{port}", spawn=True, binary=binary, cwd=d,
                    log_path=os.path.join(d, "oc.log"),
                    env={"PATH": os.environ["PATH"], "YURI_ENV_DUMP": dump})
                try:
                    await srv.acquire()
                    with open(dump) as f:
                        child = dict(l.rstrip("\n").split("=", 1)
                                     for l in f if "=" in l)
                    self.assertNotIn("YURI_TEST_SECRET", child,
                                     "an explicit env must not be merged with os.environ")
                    self.assertEqual(child.get("YURI_ENV_DUMP"), dump)
                finally:
                    await srv.release()
            finally:
                os.environ.pop("YURI_TEST_SECRET", None)

    async def test_a_password_still_reaches_an_explicit_env(self):
        """The password is set on top of whatever env the caller supplied --
        filtering the environment must not disarm server auth."""
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            dump = os.path.join(d, "env.txt")
            srv = OpenCodeServer(
                f"http://127.0.0.1:{port}", spawn=True, binary=binary, cwd=d,
                log_path=os.path.join(d, "oc.log"), password="pw",
                env={"PATH": os.environ["PATH"], "YURI_ENV_DUMP": dump})
            try:
                await srv.acquire()
                with open(dump) as f:
                    child = dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)
                self.assertEqual(child.get("OPENCODE_SERVER_PASSWORD"), "pw")
            finally:
                await srv.release()

    async def test_a_missing_binary_is_reported_actionably(self):
        port = _free_port()
        srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                             binary="definitely-not-a-binary")
        with self.assertRaises(OpenCodeUnavailable) as cm:
            await srv.acquire()
        self.assertIn("definitely-not-a-binary", str(cm.exception))
        self.assertFalse(srv.owned)

    async def test_a_binary_that_cannot_be_executed_is_reported_actionably(self):
        # An OPENCODE_BIN that exists but is not executable. Without the
        # os.access check this reaches create_subprocess_exec and comes back as
        # a bare PermissionError, which nothing upstream knows how to narrate.
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = os.path.join(d, "opencode-stub")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")     # deliberately not chmod +x
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True, binary=binary)
            with self.assertRaises(OpenCodeUnavailable) as cm:
                await srv.acquire()
            msg = str(cm.exception)
            self.assertIn(binary, msg)
            # The actionable message, not the generic "could not start" one a
            # bare PermissionError would produce.
            self.assertIn("OPENCODE_BIN", msg)
            self.assertFalse(srv.owned)

    async def test_an_unusable_working_directory_is_reported_actionably(self):
        # Every way spawning can fail has to arrive as OpenCodeUnavailable, or
        # an OpenCode misconfiguration crashes a voice tool call with an OSError.
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=os.path.join(d, "no-such-dir"),
                                 ready_timeout=2.0)
            with self.assertRaises(OpenCodeUnavailable):
                await srv.acquire()
            await srv.release()

    async def test_an_unwritable_log_path_is_reported_actionably(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            blocker = os.path.join(d, "not-a-dir")
            with open(blocker, "w") as f:
                f.write("")
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(blocker, "oc.log"),
                                 ready_timeout=2.0)
            with self.assertRaises(OpenCodeUnavailable):
                await srv.acquire()
            await srv.release()

    async def test_a_server_that_never_becomes_ready_times_out_and_is_cleaned_up(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port, ready=False)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(d, "oc.log"),
                                 ready_timeout=2.0)
            # release() drops _proc, so the child has to be caught on the way
            # past -- otherwise "it was cleaned up" is unverifiable and the
            # test would pass just as happily on an implementation that leaks
            # the process.
            spawned: list = []
            ready = srv._await_ready

            async def watch():
                spawned.append(srv._proc)
                return await ready()

            srv._await_ready = watch
            with self.assertRaises(OpenCodeUnavailable) as cm:
                await srv.acquire()
            self.assertRegex(str(cm.exception).lower(), r"ready|timed out")
            # A failed spawn must not leave the child running.
            self.assertFalse(srv.owned)
            self.assertIsNone(srv.client)
            self.assertIsNotNone(spawned[0].returncode,
                                 "a failed spawn left its child running")
            await srv.release()

    async def test_a_binary_that_exits_immediately_fails_fast(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = os.path.join(d, "opencode-stub")
            with open(binary, "w") as f:
                f.write("#!/bin/sh\nexit 3\n")
            os.chmod(binary, 0o755)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d, ready_timeout=30.0)
            loop = asyncio.get_running_loop()
            started = loop.time()
            with self.assertRaises(OpenCodeUnavailable):
                await srv.acquire()
            # It noticed the exit instead of waiting out the whole timeout.
            self.assertLess(loop.time() - started, 10.0)
            self.assertFalse(srv.owned)
            await srv.release()

    async def test_concurrent_acquire_spawns_only_one_process(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(d, "oc.log"))
            try:
                got = await asyncio.gather(*(srv.acquire() for _ in range(4)))
                self.assertEqual(srv.spawn_count, 1)
                self.assertEqual(len({id(c) for c in got}), 1)
            finally:
                await srv.release()

    async def test_release_will_not_stop_a_process_it_does_not_own(self):
        # The defensive branch in release(). Unreachable through the public API
        # by construction -- _proc is only ever set when owned -- so it is
        # reached here by corrupting the state on purpose. It is the last line
        # of defence for the rule the whole file exists to enforce, and it must
        # complain rather than kill.
        child = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "sleep 30",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            srv = OpenCodeServer("http://127.0.0.1:1", spawn=False)
            srv._proc, srv._owned = child, False
            with self.assertLogs("yuri.opencode.server", "ERROR"):
                await srv.release()
            self.assertIsNone(child.returncode, "release() killed a process it did not own")
        finally:
            child.terminate()
            await child.wait()

    async def test_release_survives_a_process_that_exits_as_it_is_signalled(self):
        # The window between release()'s returncode check and the signal: the
        # child can exit inside it, and terminate() then raises. Shutdown must
        # not abort over a process that is already gone.
        class Vanished:
            returncode = None

            def terminate(self):
                raise ProcessLookupError(3, "No such process")

        srv = OpenCodeServer("http://127.0.0.1:1", spawn=False)
        srv._proc, srv._owned = Vanished(), True
        await srv.release()                  # must not raise
        self.assertFalse(srv.owned)

    async def test_a_dead_spawned_server_is_re_acquired(self):
        # Design spec section 4: a dead server we started is re-acquired on the
        # next call rather than poisoning the provider for the rest of the run.
        port = _free_port()
        with tempfile.TemporaryDirectory() as d:
            binary = _stub_binary(d, port)
            srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True,
                                 binary=binary, cwd=d,
                                 log_path=os.path.join(d, "oc.log"))
            try:
                first = await srv.acquire()
                # Simulating a crash means reaching for the child directly:
                # the class deliberately exposes no way to kill anything.
                proc = srv._proc
                proc.kill()
                await proc.wait()
                # Re-acquiring says so out loud rather than silently.
                with self.assertLogs("yuri.opencode.server", "WARNING"):
                    second = await srv.acquire()
                self.assertIsNot(second, first)
                self.assertEqual(srv.spawn_count, 2)
                self.assertTrue(srv.owned)
                self.assertIsInstance(await second.get("/api/session"), list)
            finally:
                await srv.release()


class ChildEnvironment(unittest.IsolatedAsyncioTestCase):
    """Design spec section 4: the spawned child "inherits no Yuri secrets".

    `server.py` cannot decide which names those are — it may not import config,
    and OPENAI_API_KEY/GEMINI_API_KEY are genuinely ambiguous (Yuri's voice
    keys, and plausibly OpenCode's own provider auth). So config.py builds the
    environment and server.py takes it verbatim. These tests read what the
    CHILD PROCESS actually received, so they cover the whole path rather than
    the filter in isolation.
    """

    async def _child_env(self, d: str) -> dict[str, str]:
        """Spawn the stub with config's child environment; return what it got."""
        port = _free_port()
        binary = _stub_binary(d, port)
        dump = os.path.join(d, "env.txt")
        with mock.patch.dict(os.environ, {"YURI_ENV_DUMP": dump}):
            env = config.opencode_child_env()
        srv = OpenCodeServer(f"http://127.0.0.1:{port}", spawn=True, binary=binary,
                             cwd=d, log_path=os.path.join(d, "logs", "oc.log"), env=env)
        try:
            await srv.acquire()
        finally:
            await srv.release()
        with open(dump) as f:
            return dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)

    async def test_yuris_own_auth_token_never_reaches_the_child(self):
        # VC_AUTH_TOKEN is the shared secret gating Yuri's own endpoints.
        # OpenCode has no possible use for it, so it is stripped
        # unconditionally — the escape hatch below does not reach it.
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"VC_AUTH_TOKEN": "yuris-shared-secret"}):
            child = await self._child_env(d)
        self.assertNotIn("VC_AUTH_TOKEN", child)
        self.assertNotIn("yuris-shared-secret", "".join(child.values()))
        # Everything else passes through: the child cannot run without these.
        self.assertIn("PATH", child)
        self.assertEqual(child.get("PATH"), os.environ["PATH"])
        self.assertEqual(child.get("HOME"), os.environ.get("HOME"))

    async def test_the_voice_keys_are_stripped_by_default(self):
        self.assertFalse(config.OPENCODE_INHERIT_KEYS,
                         "OPENCODE_INHERIT_KEYS must default to off — the spec says strip")
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {v: f"voice-{v}" for v in config.VOICE_KEY_VARS}):
            child = await self._child_env(d)
        for var in config.VOICE_KEY_VARS:
            self.assertNotIn(var, child, var)

    async def test_opencode_inherit_keys_hands_the_model_keys_over(self):
        """The escape hatch, for an OpenCode that reads its provider auth from
        the environment rather than from its own `opencode auth login` store.
        It covers the ambiguous model keys and nothing else."""
        env = {v: f"voice-{v}" for v in config.VOICE_KEY_VARS}
        env["VC_AUTH_TOKEN"] = "yuris-shared-secret"
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, env), \
                mock.patch.object(config, "OPENCODE_INHERIT_KEYS", True):
            child = await self._child_env(d)
        for var in config.VOICE_KEY_VARS:
            self.assertEqual(child.get(var), f"voice-{var}", var)
        self.assertNotIn("VC_AUTH_TOKEN", child,
                         "the escape hatch is for model keys — never for Yuri's own token")


class ActionableRefusals(unittest.IsolatedAsyncioTestCase):
    """Every OpenCodeUnavailable message says what to do about it. That text
    only reaches the user if /tools/execute recognises the type: its handler
    maps YuriUnavailable and ValueError to soft errors and everything else to
    "the tool failed unexpectedly", which would discard exactly the wording
    these refusals were written for."""

    def test_it_is_a_provider_unavailable_so_the_message_survives(self):
        from yuri.providers.base import ProviderUnavailable

        self.assertTrue(issubclass(OpenCodeUnavailable, ProviderUnavailable))
        # And still a RuntimeError, so nothing that already caught one changes.
        self.assertTrue(issubclass(OpenCodeUnavailable, RuntimeError))

    async def test_the_refusals_name_what_to_do(self):
        cases = (
            (dict(spawn=False), ("OPENCODE_SPAWN", "opencode serve")),
            (dict(spawn=True, binary="definitely-not-a-binary"),
             ("OPENCODE_BIN", "install OpenCode")),
        )
        for kwargs, expect in cases:
            srv = OpenCodeServer(f"http://127.0.0.1:{_free_port()}", **kwargs)
            with self.assertRaises(OpenCodeUnavailable) as cm:
                await srv.acquire()
            for want in expect:
                self.assertIn(want, str(cm.exception), kwargs)
            await srv.release()


if __name__ == "__main__":
    unittest.main()
