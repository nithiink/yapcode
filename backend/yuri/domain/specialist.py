"""Specialist — a named agent (spec §5).

An "agent" used to mean a provider binary: `AgentRegistry` maps agent_id to
AgentProvider, and that was the whole model. A Specialist is the thing the
user actually creates and names — a role, a job description, a toolset, and
the provider that runs it.

Yuri's own persona is untouched by this. She is still the only voice and the
only personality the user talks to; a specialist's "persona" is a job
description handed to a provider, not a character. See
docs/superpowers/specs/2026-09-04-yuri-phase-7-design.md §3, which records
why the design README's "agents are providers, not personas" was reversed.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .ids import new_id, utcnow

ROLES: tuple[str, ...] = ("researcher", "developer", "tester", "reviewer",
                          "verifier", "documenter")

# What a task NEEDS done, which is a different axis from AgentCapabilities in
# providers/base.py. Those are mechanical and reported by the provider (can it
# send keys, can it resume); these describe the job a specialist is set up
# for, and live on the specialist because they are a property of its prompt
# and toolset rather than of the binary.
TASK_CAPABILITIES: tuple[str, ...] = ("coding", "code_review", "research",
                                      "testing", "terminal", "browser", "git", "docs")

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A provider-safe id.

    It becomes `--agent <slug>` on a command line and `<slug>.md` inside
    OpenCode's config directory, so it must survive neither shell quoting nor
    path traversal: lowercase, [a-z0-9-] only, and never empty. A name made
    entirely of punctuation still has to produce something usable, hence the
    fallback rather than a raise — the caller named their agent "???" and that
    is not an error worth stopping them for.
    """
    slug = _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")
    return slug or f"agent-{new_id()[:8]}"


@dataclass
class Specialist:
    name: str
    role: str
    provider_id: str
    id: str = field(default_factory=new_id)
    slug: str = ""
    description: str = ""
    system_prompt: str = ""
    model: str | None = None
    tools: tuple[str, ...] = ()
    permission_mode: str = "default"
    capabilities: tuple[str, ...] = ()
    color: str = "#dd8a6a"
    builtin: bool = False
    archived: bool = False
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role: {self.role!r}; expected one of {list(ROLES)}")
        # Tuples, never sets. store/sqlite.py's _to_row serialises JSON columns
        # with json.dumps(v, default=str), and a frozenset is not
        # JSON-serialisable — so default=str would catch it and store the
        # literal string "frozenset({'coding'})", silently, with no error.
        # from_dict hands us lists straight out of json.loads.
        self.tools = tuple(self.tools)
        self.capabilities = tuple(self.capabilities)
        for cap in self.capabilities:
            if cap not in TASK_CAPABILITIES:
                raise ValueError(
                    f"unknown capability: {cap!r}; expected one of {list(TASK_CAPABILITIES)}")
        # Derived once. A rename must NOT move it: a running session was
        # launched with the old slug and would lose its agent mid-task.
        if not self.slug:
            self.slug = slugify(self.name)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Specialist":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# One per role, so a workflow runs on a fresh install with nothing authored.
# Provider assignment follows spec §7.32's defaults; RosterService.seed()
# remaps to a registered provider when the declared one is absent, because a
# roster pointing at a provider the user does not run is a roster of broken
# buttons.
#
# The prompts are deliberately about honesty rather than enthusiasm: every one
# of them tells the specialist what to do when it CANNOT do the job, because a
# confident wrong answer from a task in the middle of a workflow is the
# failure that propagates through the handoff into every task after it.
BUILTINS: tuple[dict, ...] = (
    {"name": "Researcher", "role": "researcher", "provider_id": "opencode",
     "description": "Reads the code and reports what it finds. Changes nothing.",
     "capabilities": ("research", "terminal"), "tools": ("Read", "Grep", "Glob", "Bash"),
     "permission_mode": "plan", "color": "#93a6c9",
     "system_prompt": ("You investigate and report. Read the code, run read-only "
                       "commands, and write up what is actually true with file and "
                       "line references. Do not edit, create or delete any file. If "
                       "the answer is not in the code, say so rather than inferring "
                       "it — someone downstream will act on what you write.")},
    {"name": "Developer", "role": "developer", "provider_id": "opencode",
     "description": "Makes the change.",
     "capabilities": ("coding", "terminal", "git"), "color": "#9cc7a4",
     "system_prompt": ("You implement the change you were handed, and only that "
                       "change. Follow the conventions of the code around you. If "
                       "the handoff's findings turn out to be wrong, stop and say so "
                       "rather than working around them.")},
    {"name": "Tester", "role": "tester", "provider_id": "claude-code",
     "description": "Runs the tests and reports what actually failed.",
     "capabilities": ("testing", "terminal"), "color": "#d8b07a",
     "system_prompt": ("You run the project's tests and report the result exactly. "
                       "Quote real failure output. Never report a pass you did not "
                       "observe, and if you could not run the tests at all, say that "
                       "instead of guessing.")},
    {"name": "Reviewer", "role": "reviewer", "provider_id": "claude-code",
     "description": "Reviews the diff for correctness. Changes nothing.",
     "capabilities": ("code_review", "git"), "tools": ("Read", "Grep", "Glob", "Bash"),
     "permission_mode": "plan", "color": "#dd8a6a",
     "system_prompt": ("You review the diff for defects that would actually bite: "
                       "wrong behaviour, broken invariants, missing error paths. Give "
                       "a concrete failing scenario for each finding. Do not edit "
                       "files. End with exactly one line, and nothing after it: "
                       "'VERDICT: approved' or 'VERDICT: changes-requested'.")},
    {"name": "Verifier", "role": "verifier", "provider_id": "claude-code",
     "description": "Confirms the mission's goal was actually met.",
     "capabilities": ("testing", "terminal", "git"), "color": "#9cc7a4",
     "system_prompt": ("You confirm the mission's stated goal is met by the work that "
                       "was done. Check the goal against the diff and the test "
                       "results. Report met or not-met, with the evidence you used. "
                       "Not-met is a useful answer; do not stretch to say met.")},
    {"name": "Documenter", "role": "documenter", "provider_id": "claude-code",
     "description": "Writes down what changed and why.",
     "capabilities": ("docs",), "color": "#928c81",
     "system_prompt": ("You document what changed and why, for someone who was not "
                       "here. Describe the behaviour, not the diff. Do not invent "
                       "rationale that is not present in the work you were given.")},
)
