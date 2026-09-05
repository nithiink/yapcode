"""What one specialist is told about another's work (spec §9).

The rule this module exists to enforce is §7.10: **an agent must not
automatically receive every other agent's history.** A transcript dump is the
easy thing to build and the wrong thing to send — it is unbounded, it is
mostly noise, and it hands one agent's mistakes to the next as context.

So a handoff is assembled from ARTIFACTS, and only from the dependencies the
task actually waited on. Two halves that have to meet:

  * `ingest_result` turns a finished turn into artifacts.
  * `build_handoff` reads the artifacts of a task's dependencies back.

If either half drifts, artifacts pile up unread or a task is briefed on
nothing; `test_what_is_ingested_is_what_the_next_task_reads` is the test that
holds them together.

Everything an artifact says is ATTRIBUTED, never asserted: "investigate
reported: …". Another agent's claim is something that was said, which is the
same rule her prompt applies to everything else an agent tells her.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from yuri.domain.artifact import Artifact
from yuri.domain.event import EventType
from yuri.domain.task import Task
from yuri.store.base import Store

# The whole brief. Chosen against the smallest useful context window rather
# than the largest: an overflowing prompt makes the task fail for a reason the
# user cannot see, and truncation the agent is told about is always better
# than a provider error it is not.
HANDOFF_MAX = 6000

# One artifact's body, applied on the way IN. The turn text is already clipped
# to 2000 by the publisher and again by RESULT_TEXT_MAX; this is the third and
# last bound, and the only one that applies to an artifact written by anything
# else (a future tool that reports a finding directly, say).
BODY_MAX = 2000

# How many events back to look when deriving the files a task touched. A task
# whose session produced more tool calls than this loses the earliest ones,
# which is the right way round: the recent edits are the ones the next agent
# needs to know about.
EVENT_SCAN_MAX = 500

# Tool names that mean a file was CHANGED. Read/Grep/Glob are deliberately
# absent: "files this task touched" is about what the next agent may find
# altered, not about what the last one looked at.
WRITE_TOOLS: frozenset[str] = frozenset({
    "Edit", "Write", "MultiEdit", "NotebookEdit", "Update", "str_replace_editor",
    "str_replace_based_edit_tool", "create_file", "edit_file", "write_file",
})

_OMITTED = "(earlier findings omitted — this brief was full)"
_CLIPPED = "… (clipped)"


@dataclass(frozen=True)
class Handoff:
    """One specialist's brief. `summary` is what THIS task is being asked to
    do — the engine sends `render()` in place of the raw instruction, so the
    instruction has to travel inside it."""
    mission_goal: str
    previous: tuple[Artifact, ...] = ()
    summary: str = ""
    files_touched: tuple[str, ...] = ()
    notes: str = ""
    # task_id -> the title of the task that produced it, so an artifact can
    # say who reported it. Not on Artifact itself: the artifact row records
    # which task made it, not what that task was called.
    authors: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        head: list[str] = [f"THE MISSION: {self.mission_goal}"]
        if self.summary:
            head.append("")
            head.append(f"YOUR TASK: {self.summary}")
        if self.notes:
            # Above the findings, not below: on a retry this is the single most
            # important line in the brief.
            head.append("")
            head.append(f"WHY THE LAST ATTEMPT DID NOT STAND: {self.notes}")
        if self.files_touched:
            head.append("")
            head.append("FILES EARLIER TASKS CHANGED: " + ", ".join(self.files_touched))

        tail: list[str] = []
        if self.previous:
            tail = ["", "Previous findings from earlier tasks (what they REPORTED — "
                        "verify anything you rely on):"]

        text = "\n".join(head + tail)
        if not self.previous:
            return text

        # Newest first, because the newest is the most relevant, and dropping
        # from the oldest end is what the budget should cost.
        blocks: list[str] = []
        used = len(text)
        omitted = 0
        for art in reversed(self.previous):
            who = self.authors.get(art.task_id or "", "an earlier task")
            body = " ".join((art.body or "").split())
            block = f"\n- {who} reported ({art.kind}) “{art.title}”: {body}"
            if used + len(block) > HANDOFF_MAX:
                if not blocks:
                    # Nothing fits at all. Clipping the one artifact beats
                    # sending a brief with no context and no explanation.
                    room = max(0, HANDOFF_MAX - used - len(_CLIPPED) - len(block) + len(body))
                    block = (f"\n- {who} reported ({art.kind}) “{art.title}”: "
                             f"{body[:room]}{_CLIPPED}")
                    blocks.append(block)
                    used += len(block)
                    continue
                omitted += 1
                continue
            blocks.append(block)
            used += len(block)

        # Oldest-first in the output, so it reads in the order the work
        # happened even though the budget was spent newest-first.
        out = text + "".join(reversed(blocks))
        if omitted:
            note = f"\n{_OMITTED}"
            if len(out) + len(note) > HANDOFF_MAX:
                out = out[: HANDOFF_MAX - len(note)]
            out += note
        return out


def build_handoff(store: Store, task: Task, deps: set[str]) -> Handoff:
    """Assemble the brief for `task` from the artifacts of `deps`.

    `deps` is passed in rather than read here: the engine knows which
    dependencies are SATISFIED, and briefing a task on a dependency that has
    not finished would carry a half-written finding.
    """
    goal = ""
    w = store.workflows.get(task.workflow_id)
    if w:
        m = store.missions.get(w.mission_id)
        if m:
            # `goal` is optional; the title is what the user actually typed, so
            # it is the right fallback. A brief with no statement of what the
            # work is for is the one thing this must never render.
            goal = m.goal or m.title

    previous: list[Artifact] = []
    authors: dict[str, str] = {}
    files: list[str] = []
    for dep_id in sorted(deps):
        dep = store.tasks.get(dep_id)
        authors[dep_id] = dep.title if dep else "an earlier task"
        for art in store.artifacts.for_task(dep_id):
            if art.kind == "file_list":
                # Carried as a list, not as prose: the next agent wants the
                # paths, and repeating them inside the findings wastes budget.
                for line in (art.body or "").splitlines():
                    name = line.strip()
                    if name and name not in files:
                        files.append(name)
                continue
            previous.append(art)

    # Only on a retry. `error` outlives an attempt, so a first attempt reading
    # it would be briefed on a failure that has not happened yet.
    notes = (task.error or "") if task.attempts > 0 else ""

    return Handoff(mission_goal=goal, previous=tuple(previous),
                   summary=task.instruction or task.title,
                   files_touched=tuple(files), notes=notes, authors=authors)


def ingest_result(store: Store, task: Task, assistant_text: str,
                  files_touched: tuple[str, ...] | list[str]) -> list[Artifact]:
    """Turn a finished turn into the artifacts the next task will read.

    A `summary` is ALWAYS produced. That is the honest default: a handoff must
    never come back empty just because a specialist did not know it was
    supposed to file a finding, and "no artifacts" and "nothing found" would
    otherwise look identical to the next task.
    """
    w = store.workflows.get(task.workflow_id)
    mission_id = w.mission_id if w else ""
    existing = {a.kind: a for a in store.artifacts.for_task(task.id)}
    out: list[Artifact] = []

    body = " ".join((assistant_text or "").split())[:BODY_MAX] or (
        # Not an empty body: the next task would read that as "no findings"
        # rather than "the agent produced no text", which are different things.
        "the agent finished without producing any text at all")
    out.append(_upsert(store, existing, Artifact(
        mission_id=mission_id, task_id=task.id, kind="summary",
        title=f"what {task.title} reported", body=body)))

    files = [str(f) for f in (files_touched or []) if str(f).strip()]
    if files:
        out.append(_upsert(store, existing, Artifact(
            mission_id=mission_id, task_id=task.id, kind="file_list",
            title=f"files {task.title} changed", body="\n".join(files))))
    return out


def _upsert(store: Store, existing: dict[str, Artifact], fresh: Artifact) -> Artifact:
    """Replace this task's artifact of the same kind rather than adding one.

    A retry re-ingests, and two summaries of one task would make the next
    brief say everything twice — spending the budget on a duplicate of what
    the agent has already superseded.
    """
    prior = existing.get(fresh.kind)
    if prior is None:
        store.artifacts.insert(fresh)
        return fresh
    prior.title, prior.body = fresh.title, fresh.body
    # update(), not insert(): `insert` is a plain INSERT and would raise on the
    # primary key. Reusing the existing row's id is the point — the next task's
    # brief must see one summary per task, not one per attempt.
    store.artifacts.update(prior)
    return prior


def files_from_events(store: Store, task: Task) -> tuple[str, ...]:
    """The files this task's agent changed, read off the event log.

    From the log rather than from in-memory bookkeeping, because the log
    survives a restart and a dict of per-task state does not. Scoped by
    `started_at` as well as by session: a specialist's second task REUSES the
    session, so filtering on the session alone would attribute the first
    task's edits to the second.
    """
    if not task.session_id:
        return ()
    events = store.events.list(session_id=task.session_id, since=task.started_at,
                               limit=EVENT_SCAN_MAX)
    files: list[str] = []
    for ev in events:
        if ev.type != EventType.TOOL_STARTED:
            continue
        payload = ev.payload or {}
        if str(payload.get("tool_name") or "") not in WRITE_TOOLS:
            continue
        inp = payload.get("tool_input")
        if not isinstance(inp, dict):
            continue
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
        if not path:
            continue
        name = str(path)
        if name not in files:
            files.append(name)
    return tuple(files)
