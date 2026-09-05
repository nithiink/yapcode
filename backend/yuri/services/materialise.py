"""SpecialistMaterialiser — how a persona reaches the agent (spec §5.2).

Each provider owns its own named-agent mechanism, and the two measured here
disagree on timing. Claude Code takes an inline `--agents <json>` argument at
launch — nothing touches disk. OpenCode reads agent definitions from a
markdown file in its config directory and exposes no write API for them
(`GET /api/agent` is read-only), so the file must exist on disk BEFORE
`POST /session {"agent": slug}` is sent. `ensure()` is the seam that hides
that asymmetry from callers.

Call it on specialist create/update AND again before every dispatch: a config
file is derived state that can be deleted or hand-edited behind Yuri's back,
and a stale or missing OpenCode definition would otherwise fail the launch
with a provider error the user cannot act on. That is also why
OpenCodeMaterialiser always rewrites rather than skipping when the file
already exists — "it's there" is not the same guarantee as "it's current".

A provider that answers `capabilities().supports_personas == False` has no
such mechanism at all, so its specialists' prompts are prepended to the first
message of the task instead: degraded, but honest.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Protocol

from yuri.domain.specialist import Specialist
from yuri.providers.base import AgentProvider


class SpecialistMaterialiser(Protocol):
    async def ensure(self, spec: Specialist) -> dict:
        """Make `spec` usable by this provider. Returns launch kwargs
        (e.g. {"agents_json": ...} or {"agent": slug}). Idempotent."""
        ...


class ClaudeMaterialiser:
    """Builds the `--agents <json>` payload. Nothing is written to disk: the
    whole definition travels inline with the launch command, and
    tmux_runner.py is responsible for shlex.quote-ing the result into its one
    shell string (see 5149db7 for why that step is not optional)."""

    async def ensure(self, spec: Specialist) -> dict:
        entry: dict = {"description": spec.description, "prompt": spec.system_prompt}
        # An empty tools tuple means "the provider's default toolset"; writing
        # `"tools": []` would instead mean "no tools at all" and silently
        # cripple the agent. A None model gets the same treatment: omit the
        # key rather than send a null the CLI has to special-case.
        if spec.tools:
            entry["tools"] = list(spec.tools)
        if spec.model:
            entry["model"] = spec.model
        agents_json = json.dumps({spec.slug: entry})
        return {"agents_json": agents_json, "agent": spec.slug}


# Belt-and-braces on top of specialist.slugify(): a slug is a plain writable
# column, so a row restored from an old backup or edited directly in the
# store could carry one that never went through slugify at all. This check
# is the only thing left standing between that value and an arbitrary file
# write, so it must run before the slug ever touches a path.
_UNSAFE_SLUG_MARKERS = ("/", "\\", "..")


def _scalar(value: str) -> str:
    # These are single-line YAML values by construction (description, model,
    # color); a stray newline from a pasted description would otherwise
    # start a new (attacker- or accident-controlled) YAML key on its own line.
    return value.replace("\n", " ").replace("\r", " ").strip()


def _neutralise_prompt(prompt: str) -> str:
    """Indent any line that could be mistaken for a frontmatter delimiter.

    The prompt is written directly after the closing `---` fence. A prompt
    that itself begins a line with `---` would otherwise close the
    frontmatter block early and turn everything after it into YAML — so any
    such line gets a leading space, which a `---\\n...\\n---\\n` frontmatter
    parser no longer recognises as a delimiter (it must be flush at column
    0), while the prompt's meaning is untouched.
    """
    return "\n".join((" " + ln if ln.startswith("---") else ln)
                     for ln in prompt.splitlines())


class OpenCodeMaterialiser:
    """Writes `<config_dir>/<slug>.md` so OpenCode can resolve `--agent
    <slug>` / `{"agent": slug}` against a definition it already has on disk.
    """

    def __init__(self, config_dir: str):
        self.config_dir = config_dir

    async def ensure(self, spec: Specialist) -> dict:
        slug = spec.slug
        if any(marker in slug for marker in _UNSAFE_SLUG_MARKERS):
            raise ValueError(f"unsafe specialist slug: {slug!r}")

        os.makedirs(self.config_dir, exist_ok=True)
        path = os.path.join(self.config_dir, f"{slug}.md")
        body = self._render(spec)

        # Atomic: a reader (or a concurrent ensure()) must never observe a
        # half-written file. Temp file in the same directory so os.replace
        # is a same-filesystem rename, not a copy.
        fd, tmp_path = tempfile.mkstemp(dir=self.config_dir, prefix=f".{slug}-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(body)
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        return {"agent": slug}

    @staticmethod
    def _render(spec: Specialist) -> str:
        lines = ["---", f"description: {_scalar(spec.description)}", "mode: subagent"]
        if spec.model:
            lines.append(f"model: {_scalar(spec.model)}")
        if spec.tools:
            lines.append("tools: [" + ", ".join(spec.tools) + "]")
        lines.append(f"color: {_scalar(spec.color)}")
        lines.append("---")
        header = "\n".join(lines)
        return f"{header}\n{_neutralise_prompt(spec.system_prompt)}\n"


class PrependMaterialiser:
    """The degraded path for a provider with no native persona mechanism.
    Nothing to write, nothing to name — the prompt is handed back so the
    caller can prepend it to the task's first message instead."""

    async def ensure(self, spec: Specialist) -> dict:
        return {"prepend": spec.system_prompt}


def materialiser_for(provider: AgentProvider, home: str) -> SpecialistMaterialiser:
    """Pick the right materialiser for `provider`.

    Capability first: a provider that cannot carry a persona natively gets
    the prepend path regardless of its id. `home` is the directory OpenCode's
    own config lives under (`~/.config/opencode/agent/<slug>.md`), passed in
    rather than read from the environment so this stays testable with a
    tempdir instead of the real user's config.
    """
    if not provider.capabilities().supports_personas:
        return PrependMaterialiser()
    if provider.id == "claude-code":
        return ClaudeMaterialiser()
    if provider.id == "opencode":
        return OpenCodeMaterialiser(os.path.join(home, ".config", "opencode", "agent"))
    raise ValueError(f"no materialiser for provider {provider.id!r}")
