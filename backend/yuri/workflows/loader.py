"""Workflow templates: declarative task graphs, parsed and validated (spec §7.3).

Templates ship as JSON, not YAML — the repo pins exact dependency versions and
has no YAML dependency today, and the spec explicitly allows a JSON/DB-backed
schema (§7.3). Field names match the spec's YAML example exactly (`name`,
`description`, `tasks`, and per-task `id`, `role`, `title`, `instruction`,
`depends_on`, `read_only`, `verification`, `requires`, `kind`) so the two read
the same.

`MAX_TASKS_PER_WORKFLOW` lives here, not in the engine: the bound is a
property of a workflow *definition*, and the loader is the only thing that
can reject one at authoring time — before any task ever runs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace

from ..domain.specialist import ROLES
from ..domain.task import TASK_KINDS

# The check names a template's `verification` list may reference. A later
# task (verify.py) implements the checks themselves and asserts its own
# dict's keys equal this exact set, so neither side has to guess the other's.
VERIFY_NAMES: frozenset[str] = frozenset(
    {"tests_pass", "typecheck_pass", "diff_scoped", "review_approved", "human_ok"})

MAX_TASKS_PER_WORKFLOW = 40

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


class TemplateError(ValueError):
    """A template is malformed or unsafe to run. Raised at load/validate time,
    never discovered mid-mission — a cycle found while a mission is running is
    a deadlock the user cannot diagnose."""


@dataclass(frozen=True)
class TemplateTask:
    id: str
    role: str | None
    title: str
    instruction: str
    depends_on: tuple[str, ...] = ()
    read_only: bool = False
    verification: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    kind: str = "agent_task"


@dataclass(frozen=True)
class Template:
    name: str
    description: str
    tasks: tuple[TemplateTask, ...]


def _task_from_dict(d: dict) -> TemplateTask:
    return TemplateTask(
        id=d["id"],
        role=d.get("role"),
        title=d.get("title", d["id"]),
        instruction=d.get("instruction", ""),
        depends_on=tuple(d.get("depends_on", ())),
        read_only=bool(d.get("read_only", False)),
        verification=tuple(d.get("verification", ())),
        requires=tuple(d.get("requires", ())),
        kind=d.get("kind", "agent_task"),
    )


def _template_from_dict(d: dict) -> Template:
    return Template(
        name=d["name"],
        description=d.get("description", ""),
        tasks=tuple(_task_from_dict(t) for t in d.get("tasks", ())),
    )


def parse_templates(dir: str | None = None) -> dict[str, Template]:
    """Parse every `*.json` template in `dir` WITHOUT validating, keyed by
    name.

    Exists so a test can hold a deliberately-broken Template and assert what
    `validate()` rejects and why. Production code should call
    `load_templates`: a template that parses is not a template that can run.
    """
    path = dir if dir is not None else _TEMPLATES_DIR
    out: dict[str, Template] = {}
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(path, name), encoding="utf-8") as f:
            data = json.load(f)
        t = _template_from_dict(data)
        out[t.name] = t
    return out


def load_templates(dir: str | None = None) -> dict[str, Template]:
    """Parse AND validate every `*.json` template in `dir`, keyed by name.

    Validation is unconditional, including for a caller-supplied directory.
    Making it depend on whether an argument was passed would mean a caller
    could not tell from the call site whether the templates they just loaded
    are safe to run — and the first thing a user-authored template directory
    would need is exactly this check. A broken template fails loudly here
    rather than deadlocking a mission later.
    """
    out = parse_templates(dir)
    for t in out.values():
        validate(t)
    return out


def render(template: Template, goal: str) -> tuple[TemplateTask, ...]:
    """Substitute `{goal}` into every task's instruction. Uses `str.replace`,
    never `str.format`: `format` raises `KeyError` on any other brace in an
    instruction, and would interpolate braces the user typed into their own
    goal. `{goal}` is the only interpolation a template gets — a template
    language is a language to maintain."""
    return tuple(replace(t, instruction=t.instruction.replace("{goal}", goal))
                 for t in template.tasks)


def _find_cycle(graph: dict[str, tuple[str, ...]]) -> list[str] | None:
    """Depth-first walk over `depends_on`. Returns the members of one cycle,
    in order, or None. A dependency naming an id absent from `graph` is not a
    cycle — that is reported separately as an unknown dependency."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, ()):
            if dep not in color:
                continue
            if color[dep] == GRAY:
                idx = path.index(dep)
                return path[idx:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        color[node] = BLACK
        path.pop()
        return None

    for node in graph:
        if color[node] == WHITE:
            found = visit(node)
            if found:
                return found
    return None


def validate(template: Template) -> None:
    """Raise TemplateError, naming what is wrong, for: an unknown role, an
    unknown kind, an agent_task with no role, a depends_on naming a task id
    absent from this template,
    duplicate task ids, a dependency cycle (names the members), more than
    MAX_TASKS_PER_WORKFLOW tasks, or a verification entry not in
    VERIFY_NAMES. Must run at load time — see the module docstring."""
    if len(template.tasks) > MAX_TASKS_PER_WORKFLOW:
        raise TemplateError(
            f"{template.name}: {len(template.tasks)} tasks exceeds the "
            f"MAX_TASKS_PER_WORKFLOW bound of {MAX_TASKS_PER_WORKFLOW}")

    ids = [t.id for t in template.tasks]
    seen: set[str] = set()
    dupes: set[str] = set()
    for task_id in ids:
        if task_id in seen:
            dupes.add(task_id)
        seen.add(task_id)
    if dupes:
        raise TemplateError(f"{template.name}: duplicate task id(s): {sorted(dupes)}")

    id_set = set(ids)
    for task in template.tasks:
        if task.role is not None and task.role not in ROLES:
            raise TemplateError(
                f"{template.name}: task {task.id!r} has unknown role {task.role!r}; "
                f"expected one of {list(ROLES)}")
        if task.kind not in TASK_KINDS:
            raise TemplateError(
                f"{template.name}: task {task.id!r} has unknown kind {task.kind!r}; "
                f"expected one of {list(TASK_KINDS)}")
        # A template cannot pin a specialist (ids are per-install), so an
        # agent_task here MUST name a role. Task.__post_init__ rejects this
        # too, but only when the engine builds the row — by which point the
        # user has been told their workflow was fine. Catch it at authoring
        # time, which is what this module exists for.
        if task.kind == "agent_task" and not task.role:
            raise TemplateError(
                f"{template.name}: task {task.id!r} is an agent_task with no role, "
                "so nothing could be dispatched to it")
        for dep in task.depends_on:
            if dep not in id_set:
                raise TemplateError(
                    f"{template.name}: task {task.id!r} depends_on unknown task {dep!r}")
        for check in task.verification:
            if check not in VERIFY_NAMES:
                raise TemplateError(
                    f"{template.name}: task {task.id!r} references unknown verification "
                    f"check {check!r}; expected one of {sorted(VERIFY_NAMES)}")

    graph = {t.id: t.depends_on for t in template.tasks}
    cycle = _find_cycle(graph)
    if cycle:
        raise TemplateError(f"{template.name}: dependency cycle: {' -> '.join(cycle)}")
