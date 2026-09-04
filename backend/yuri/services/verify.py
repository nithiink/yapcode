"""Verification — what makes a task's `completed` mean something (spec §10).

Before this module existed the engine entered `verifying`, published the
declared check names, and transitioned straight to `completed`. Every
`tests_pass` in every shipped template was decoration: a `bug-fix` workflow
marked its test task complete with the tests red and handed that "success" to
the reviewer and then to the user. This module, and the call the engine makes
to it, are the end of that.

THE ONE RULE, and the reason for most of the shape below:

    `unavailable` FAILS the task. It never passes it.

A project with no test command configured cannot claim `tests_pass`. Reporting
a pass for a check that never ran is strictly worse than reporting nothing —
it is the machine telling the user something it does not know. So there is no
"assume it's fine" default anywhere in here: every check either produces a
verdict from evidence it actually gathered, or says `unavailable` and takes
the task down with it.

WHY THE COMMANDS COME FROM CONFIG AND ARE NEVER GUESSED
Inferring `pytest` from a `tests/` directory would make the check pass or fail
on a command the user never chose, in their working tree. `verify.tests` /
`verify.typecheck` are declared or the check is `unavailable`.

WHY THERE IS NO SHELL
Project config is user data. This repo has shipped shell-injection bugs from
interpolating values into shell strings before (5149db7), and a verification
command is the worst possible place for the next one: it runs unattended, in
the user's repo, with their credentials. Every command goes through
`shlex.split` + `create_subprocess_exec`, so no shell ever interprets a
character of it. A test greps this file for every spelling that would spawn
one and fails if it finds any — which is why none of those spellings is
written out anywhere in here, comments included.

WHY A TIMEOUT AND A KILLED PROCESS GROUP
A check that never returns parks the workflow forever with no explanation —
indistinguishable, from the outside, from an agent still working. A timeout is
a `fail` carrying "timed out", and the child is killed by process GROUP: the
configured command is routinely a wrapper (`sh -c …`, `npm run …`) whose
children outlive a kill aimed at the wrapper alone.

WHY `detail` IS BUILT SO CAREFULLY
It is what Yuri reads out. "The tests failed" on its own sends the user to go
look; "The tests failed — 2 failed in test_billing.py" does not. So the detail
always carries the tail of the real output — and is capped, because a 10MB
pytest log must not reach the voice model, the event log, or the `error`
column of a task row.
"""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import posixpath
import re
import shlex
import signal
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, Sequence

from yuri.domain.mission import Mission
from yuri.domain.project import Project
from yuri.domain.task import Task
from yuri.store.base import Store
# The frozen set of names a template's `verification` list may reference.
# Imported, never re-declared: CHECKS' keys must EQUAL it (a test asserts it),
# so a template can never name a check nothing implements and this module can
# never grow a check no template is allowed to ask for.
from yuri.workflows.loader import VERIFY_NAMES

log = logging.getLogger("yuri.services.verify")

PASS = "pass"
FAIL = "fail"
UNAVAILABLE = "unavailable"
VERDICTS: tuple[str, ...] = (PASS, FAIL, UNAVAILABLE)

# How much of a check's evidence survives into the result. `detail` is read
# aloud, stored in the event payload and clipped again into `tasks.error`, so
# this is the first of several bounds and the only one that sees the raw bytes.
DETAIL_MAX = 2000
# Wall clock for one configured command. Ten minutes is longer than any test
# suite this orchestrator should be waiting on synchronously, and short enough
# that a hung one is reported inside a single user's patience.
DEFAULT_TIMEOUT_S = 600.0
# How long to wait for a killed process to actually die before giving up on
# reaping it. It has already had SIGKILL; this only bounds our own wait.
_REAP_TIMEOUT_S = 5.0
# How many out-of-scope paths a `diff_scoped` detail names before it stops.
OUT_OF_SCOPE_NAMED = 8

# `VERDICT: approved` — an EXPLICIT line, anchored per-line, with the verdict
# word captured. Linear-time (no nested quantifier): this regex runs over agent
# output, which is untrusted input of unbounded length, and the repo has fixed
# a ReDoS before (01fbfa7).
_VERDICT_RE = re.compile(r"^[^\S\n]*VERDICT[^\S\n]*:[^\S\n]*(\S[^\n]*)$",
                         re.IGNORECASE | re.MULTILINE)
# What counts as approval. Anything else the reviewer wrote is NOT approval —
# the default direction here is "no", because a verdict word we do not
# recognize is a reviewer saying something we did not understand.
_APPROVES: frozenset[str] = frozenset({
    "approved", "approve", "approves", "lgtm", "pass", "passed", "ok", "yes"})


@dataclass(frozen=True)
class VerificationResult:
    """One check's outcome. Frozen because it is evidence: nothing downstream
    of the check that produced it may edit the verdict it reports."""
    check: str
    verdict: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(
                f"unknown verdict {self.verdict!r}; expected one of {list(VERDICTS)}")

    @property
    def ok(self) -> bool:
        return self.verdict == PASS

    def to_dict(self) -> dict:
        return {"check": self.check, "verdict": self.verdict, "detail": self.detail}


@dataclass(frozen=True)
class CheckContext:
    """Everything a check is allowed to look at.

    Every field is optional and every check states what it does without one,
    because the callers differ: the engine has all of it, reconciliation
    re-runs checks from a rehydrated row, and a test supplies the two fields
    the check under test reads. A missing field is `unavailable` (with a
    detail naming what was missing), never a pass.
    """
    store: Store | None = None
    task: Task | None = None
    mission: Mission | None = None
    project: Project | None = None
    cwd: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S


# --- helpers ---------------------------------------------------------------


def _clip(text: str, cap: int = DETAIL_MAX) -> str:
    """Keep the TAIL, not the head. A failing test run puts the summary the
    user needs ("2 failed in test_billing.py") at the END of a log whose first
    megabyte is collection noise."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return "…" + text[-(cap - 1):]


def verify_config(ctx: CheckContext) -> dict:
    """The project's declared verification commands, or `{}`.

    Read from `project.metadata["verify"]` (migration 0004; set through
    ProjectService.set_verify), falling back to `mission.metadata["verify"]`.
    Two sources on purpose: "how do you run this repo's tests" is a property
    of the repo, so the project is the home — but a mission may override it
    for one piece of work without reconfiguring the project.

    `getattr` rather than attribute access: the fallback path is also how this
    behaved before the project column existed, and a store restored from an
    older backup can still hand back a Project without it.
    """
    for holder in (ctx.project, ctx.mission):
        meta = getattr(holder, "metadata", None)
        if isinstance(meta, dict):
            cfg = meta.get("verify")
            if isinstance(cfg, dict):
                return cfg
    return {}


def _cwd(ctx: CheckContext) -> str | None:
    return ctx.cwd or getattr(ctx.project, "root_path", None)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's whole process group.

    The configured command is usually a wrapper — `sh -c 'pytest …'`, `npm run
    test` — and killing only the wrapper leaves the suite running in the
    user's tree while we report a timeout. `start_new_session=True` at spawn
    is what makes the group ours to kill.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass


async def _run(argv: Sequence[str], cwd: str, timeout_s: float) -> tuple[int | None, str]:
    """Run `argv` in `cwd`. Returns (exit code, combined output), with a code
    of None meaning it timed out.

    stderr is merged into stdout because the two interleave in the order the
    tool wrote them, and the failure summary is often split across both.
    stdin is /dev/null: a check that stops to ask a question has hung, and
    should hit the timeout rather than block on a terminal nobody is at.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except (asyncio.TimeoutError, TimeoutError):
        _kill_group(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=_REAP_TIMEOUT_S)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("verification command %r did not die after SIGKILL", argv[:1])
        return None, ""
    return proc.returncode, (out or b"").decode("utf-8", errors="replace")


async def _command_check(check: str, key: str, what: str,
                         ctx: CheckContext) -> VerificationResult:
    """Shared body of `tests_pass` and `typecheck_pass`: resolve the configured
    command, refuse to invent one, run it without a shell, judge by exit code."""
    command = verify_config(ctx).get(key)
    if not isinstance(command, str) or not command.strip():
        # NOT a pass, and not silence: name the exact key the user has to set.
        # Guessing the command from what is lying around in the tree is how a
        # check comes to pass or fail on something the user never chose.
        return VerificationResult(check, UNAVAILABLE, (
            f"no {what} command is configured for this project, so I can't check it — "
            f"set metadata.verify.{key} to the command that runs {what} here"))
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        return VerificationResult(check, UNAVAILABLE, (
            f"the configured {what} command could not be parsed ({exc}): {command!r}; "
            f"fix metadata.verify.{key}"))
    if not argv:
        return VerificationResult(check, UNAVAILABLE, (
            f"the configured {what} command is empty; set metadata.verify.{key}"))

    cwd = _cwd(ctx)
    if not cwd or not os.path.isdir(cwd):
        return VerificationResult(check, UNAVAILABLE, (
            f"I can't run the {what} command: the mission's working directory "
            f"({cwd or 'unset'}) doesn't exist"))

    try:
        code, output = await _run(argv, cwd, ctx.timeout_s)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
        # The command itself could not be started (typo, not installed, not
        # executable). That is "the check did not run", not "the code is bad".
        return VerificationResult(check, UNAVAILABLE, (
            f"the {what} command couldn't be started ({exc}): {command!r}"))

    tail = _clip(output)
    if code is None:
        # A fail, not `unavailable`: we could not learn the answer AND we spent
        # the user's wall clock finding that out, which is a defect in the
        # command or the tree, and either way the task must not complete.
        return VerificationResult(check, FAIL, _clip(
            f"the {what} command timed out after {int(ctx.timeout_s)}s: {command}"))
    if code == 0:
        return VerificationResult(check, PASS, _clip(f"{command} passed (exit 0)"))
    return VerificationResult(check, FAIL, _clip(
        f"{what} failed (exit {code}) — {tail}" if tail
        else f"{what} failed (exit {code}) with no output: {command}"))


# --- the five checks -------------------------------------------------------


async def check_tests_pass(ctx: CheckContext) -> VerificationResult:
    return await _command_check("tests_pass", "tests", "tests", ctx)


async def check_typecheck_pass(ctx: CheckContext) -> VerificationResult:
    return await _command_check("typecheck_pass", "typecheck", "the typecheck", ctx)


def _expected_paths(task: Task | None) -> tuple[str, ...]:
    """The paths a task said it would touch.

    They come from the task's own finish report (`result.expected_paths`),
    which is the only place that knows: a template declares WHAT to do, not
    which files it will land in. No declaration means no scope, and a scope
    check with no scope cannot pass.
    """
    result = getattr(task, "result", None) or {}
    raw = result.get("expected_paths") or result.get("paths") or ()
    if isinstance(raw, str):
        raw = (raw,)
    return tuple(str(p).strip() for p in raw if str(p).strip())


def _normalise(path: str) -> str:
    p = str(path).strip().replace(os.sep, "/")
    while p.startswith("./"):
        p = p[2:]
    return posixpath.normpath(p).lstrip("/") if p else p


def _in_scope(changed: str, expected: Iterable[str]) -> bool:
    for want in expected:
        w = _normalise(want)
        if not w:
            continue
        if changed == w:
            return True
        # A declared DIRECTORY covers everything under it — "backend/yuri" is
        # how a task says "my change lives in this package".
        if changed.startswith(w.rstrip("/") + "/"):
            return True
        if fnmatch.fnmatch(changed, w):
            return True
    return False


async def _git(args: Sequence[str], cwd: str, timeout_s: float) -> tuple[int | None, str]:
    return await _run(["git", *args], cwd, timeout_s)


async def check_diff_scoped(ctx: CheckContext) -> VerificationResult:
    expected = _expected_paths(ctx.task)
    if not expected:
        return VerificationResult("diff_scoped", UNAVAILABLE, (
            "the task never declared which files it expected to touch "
            "(result.expected_paths), so there is no scope to check the diff against"))
    cwd = _cwd(ctx)
    if not cwd or not os.path.isdir(cwd):
        return VerificationResult("diff_scoped", UNAVAILABLE, (
            f"I can't read the diff: the mission's working directory "
            f"({cwd or 'unset'}) doesn't exist"))

    changed: set[str] = set()
    # Unstaged, staged, AND untracked. Plain `git diff` cannot see a file the
    # agent CREATED, and "it wrote a new file somewhere it shouldn't have" is
    # exactly the escape this check exists to catch.
    for args in (["diff", "--name-only"],
                 ["diff", "--name-only", "--cached"],
                 ["ls-files", "--others", "--exclude-standard"]):
        try:
            code, out = await _git(args, cwd, ctx.timeout_s)
        except OSError as exc:
            return VerificationResult("diff_scoped", UNAVAILABLE, (
                f"I couldn't run git in {cwd} ({exc}), so the diff can't be scoped"))
        if code is None:
            return VerificationResult("diff_scoped", FAIL,
                                      f"`git {' '.join(args)}` timed out in {cwd}")
        if code != 0:
            return VerificationResult("diff_scoped", UNAVAILABLE, _clip(
                f"`git {' '.join(args)}` failed (exit {code}) in {cwd} — "
                f"{_clip(out, 300)}; the diff can't be scoped"))
        changed.update(_normalise(line) for line in out.splitlines() if line.strip())

    stray = sorted(p for p in changed if p and not _in_scope(p, expected))
    if not stray:
        return VerificationResult("diff_scoped", PASS, (
            f"all {len(changed)} changed file(s) are within the declared scope"
            if changed else "nothing outside the declared scope was changed"))
    named = ", ".join(stray[:OUT_OF_SCOPE_NAMED])
    more = f" and {len(stray) - OUT_OF_SCOPE_NAMED} more" if len(stray) > OUT_OF_SCOPE_NAMED else ""
    return VerificationResult("diff_scoped", FAIL, _clip(
        f"{len(stray)} file(s) outside the declared scope were changed: {named}{more}"))


async def check_review_approved(ctx: CheckContext) -> VerificationResult:
    if ctx.store is None or ctx.task is None:
        return VerificationResult("review_approved", UNAVAILABLE,
                                  "there is no task to read a review for")
    artifacts = ctx.store.artifacts.for_task(ctx.task.id)
    if not artifacts:
        return VerificationResult("review_approved", UNAVAILABLE, (
            "the reviewer left no artifact, so nothing has approved this"))
    # Reviews first, then anything else the task produced: the verdict belongs
    # in a `review` artifact, but a reviewer who filed it under `summary` and
    # wrote the line has still stated a verdict, and refusing to read it would
    # block a task over filing.
    ordered = ([a for a in artifacts if a.kind == "review"]
               + [a for a in artifacts if a.kind != "review"])
    for a in ordered:
        stated_lines = _VERDICT_RE.findall(a.body or "")
        if not stated_lines:
            continue
        # The LAST verdict line wins: a reviewer who changed their mind while
        # writing did so further down the page.
        stated = " ".join(stated_lines[-1].split())
        word = re.split(r"[\s,.;:!]+", stated.lower())[0].strip("*_`'\"")
        if word in _APPROVES:
            return VerificationResult("review_approved", PASS,
                                      _clip(f"the reviewer said: VERDICT: {stated}"))
        return VerificationResult("review_approved", FAIL,
                                  _clip(f"the reviewer did not approve it — VERDICT: {stated}"))
    # An agent that forgot the line has not approved anything. Reading "seems
    # ok to me" as approval is the machine deciding a review passed on prose
    # it guessed the sentiment of.
    return VerificationResult("review_approved", UNAVAILABLE, (
        "the review has no explicit 'VERDICT:' line, so I can't tell whether it was "
        "approved — ask the reviewer to state a verdict"))


async def check_human_ok(ctx: CheckContext) -> VerificationResult:
    if ctx.store is None or ctx.task is None:
        return VerificationResult("human_ok", UNAVAILABLE,
                                  "there is no task to look for your approval on")
    if not ctx.task.session_id:
        return VerificationResult("human_ok", UNAVAILABLE, (
            "this task has no session, so there is no approval of yours to check"))
    approvals = ctx.store.approvals.list(session_id=ctx.task.session_id, limit=50)
    if not approvals:
        return VerificationResult("human_ok", UNAVAILABLE, (
            "you were never asked to approve this, so I can't record that you did"))
    # Newest first (the repo orders by requested_at DESC). The latest decision
    # is the one that stands.
    latest = approvals[0]
    if latest.status == "allowed":
        return VerificationResult("human_ok", PASS,
                                  _clip(f"you approved it ({latest.description or latest.action})"))
    if latest.status == "denied":
        return VerificationResult("human_ok", FAIL,
                                  _clip(f"you turned it down ({latest.description or latest.action})"))
    # pending / expired / superseded: no answer. An unanswered question is not
    # a yes, and this is the check whose whole content is that the user said so.
    return VerificationResult("human_ok", UNAVAILABLE, _clip(
        f"your approval is still {latest.status} — I don't have an answer from you yet"))


# Keys MUST equal loader.VERIFY_NAMES exactly; test_verification asserts it, so
# neither side can drift into naming a check the other does not have.
CHECKS: dict[str, Callable[[CheckContext], Awaitable[VerificationResult]]] = {
    "tests_pass": check_tests_pass,
    "typecheck_pass": check_typecheck_pass,
    "diff_scoped": check_diff_scoped,
    "review_approved": check_review_approved,
    "human_ok": check_human_ok,
}


# --- the API the engine uses -----------------------------------------------


async def run_checks(names: Iterable[str],
                     ctx: CheckContext | None = None) -> list[VerificationResult]:
    """Run the declared checks, in order, and report every one of them.

    Sequential, not gathered: two of these shell out into the SAME working
    tree, and a typecheck racing a test run in one directory is how a
    verification step starts producing verdicts that depend on timing.

    Every check runs even after one fails — the user is owed the whole picture
    in one sentence, not one failure per retry.
    """
    ctx = ctx or CheckContext()
    out: list[VerificationResult] = []
    for name in names or ():
        fn = CHECKS.get(name)
        if fn is None:
            # Unknown is `unavailable`, never ignored: silently dropping a name
            # would turn a typo in a template into a task that verifies nothing
            # and reports success. (loader.validate() rejects these at
            # authoring time; this is the second wall, for a graph built by
            # hand or by voice.)
            out.append(VerificationResult(name, UNAVAILABLE, (
                f"'{name}' isn't a check I know how to run; expected one of "
                f"{', '.join(sorted(CHECKS))}")))
            continue
        try:
            out.append(await fn(ctx))
        except Exception as exc:                       # noqa: BLE001
            # A bug in a check must not crash the engine mid-workflow — but it
            # must not pass the task either. `unavailable`: no verdict was
            # produced, and the detail says why.
            log.exception("verification check %s raised", name)
            out.append(VerificationResult(name, UNAVAILABLE,
                                          _clip(f"the '{name}' check itself failed to run: {exc}")))
    return out


def passed(results: Sequence[VerificationResult]) -> bool:
    """True only if every check PASSED. No checks declared is a pass (there was
    nothing to prove); one `unavailable` is not, and that asymmetry is the
    entire point of the module."""
    return all(r.verdict == PASS for r in results)


def failures(results: Sequence[VerificationResult]) -> list[VerificationResult]:
    """Every result that did not pass — `fail` and `unavailable` alike, because
    downstream they mean the same thing: this task is not done."""
    return [r for r in results if r.verdict != PASS]


def reason(results: Sequence[VerificationResult]) -> str:
    """One line naming what failed and why — what lands in `tasks.error` and in
    the failure event. Always names the check, because "verification failed"
    with no subject sends the user looking through four of them."""
    bad = failures(results)
    if not bad:
        return ""
    return "; ".join(f"{r.check}: {r.detail}" if r.detail else r.check for r in bad)
