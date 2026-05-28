"""Enumerate slash commands available inside a Claude Code CLI session.

In Claude Code 2.1.x slash commands == skills + a small set of CLI built-ins.
We surface them so the voice agent can discover what's available and pick the
right one (e.g. /init, /review, /kb-query) instead of guessing or routing
everything through freeform tell_claude.

Sources (deduped by name, first wins):
  builtin         — CLI commands baked into `claude`, not enumerable from disk.
  user-skill      — ~/.claude/skills/<name>/SKILL.md
  plugin-skill    — anything under ~/.claude/plugins/.../skills/<name>/SKILL.md
  user-command    — ~/.claude/commands/<name>.md
  project-command — <cwd>/.claude/commands/<name>.md  (when a session cwd is given)
"""
from __future__ import annotations

import glob
import os
import re


# CLI built-ins baked into `claude` — not enumerable from disk. Many of these
# render a UI overlay or print a notice WITHOUT driving a Claude turn, which
# means they never fire the Stop hook the regular advance() loop waits on.
# tmux_runner routes them through a settle-detect path (universally, with the
# Stop-hook still racing it — whichever fires first wins) so the voice agent
# always gets a [Claude update]. The list is conservative; if you add or
# discover new built-ins put them here so the description shows in
# list_slash_commands too.
BUILTIN_SLASH_COMMANDS: list[dict[str, str]] = [
    {"name": "help",            "description": "Show available commands and shortcuts."},
    {"name": "clear",           "description": "Clear the conversation history in this session."},
    {"name": "model",           "description": "Switch the Claude model used in this session (opus/sonnet/haiku)."},
    {"name": "permissions",     "description": "Manage tool permissions for this session."},
    {"name": "memory",          "description": "Open this session's memory file."},
    {"name": "resume",          "description": "Resume a previous session by id."},
    {"name": "add-dir",         "description": "Add a directory to the session's working set."},
    {"name": "config",          "description": "Open Claude Code configuration."},
    {"name": "compact",         "description": "Compact the conversation context to free room."},
    {"name": "cost",            "description": "Show the running cost for this session."},
    {"name": "context",         "description": "Show context-window usage breakdown for this session."},
    {"name": "agents",          "description": "List and manage Claude subagents."},
    {"name": "hooks",           "description": "List and manage Claude Code hooks."},
    {"name": "mcp",             "description": "List and manage MCP servers."},
    {"name": "skills",          "description": "List and manage skills."},
    {"name": "login",           "description": "Sign in to Claude Code."},
    {"name": "logout",          "description": "Sign out of Claude Code."},
    {"name": "bug",             "description": "File a bug report from this session."},
    {"name": "vim",             "description": "Toggle vim keybindings in the input box."},
    {"name": "terminal-setup",  "description": "Configure terminal integration."},
    {"name": "upgrade",         "description": "Upgrade the Claude Code CLI."},
    {"name": "export",          "description": "Export the session transcript."},
    {"name": "usage",           "description": "Show subscription/quota usage."},
    {"name": "status",          "description": "Show session status info."},
    {"name": "doctor",          "description": "Diagnose Claude Code setup."},
]


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_DESC_LINE = re.compile(r"^\s*description\s*:\s*(.*?)\s*$", re.MULTILINE | re.IGNORECASE)


def _description_from_md(path: str) -> str:
    """Best-effort: pull the `description:` field from a Markdown file's YAML
    frontmatter. Returns "" if no frontmatter or no description key."""
    try:
        with open(path) as f:
            head = f.read(4096)
    except Exception:
        return ""
    m = _FRONTMATTER.match(head)
    if not m:
        return ""
    dm = _DESC_LINE.search(m.group(1))
    return dm.group(1).strip().strip("\"'") if dm else ""


def list_slash_commands(project_cwd: str | None = None) -> list[dict[str, str]]:
    """Every slash command the live CLI is likely to recognize, deduped by name.
    If project_cwd is given, also include that project's local .claude/commands."""
    out: list[dict[str, str]] = [{**c, "source": "builtin"} for c in BUILTIN_SLASH_COMMANDS]
    seen = {c["name"] for c in out}
    home = os.path.expanduser("~")

    skills_root = os.path.join(home, ".claude", "skills")
    if os.path.isdir(skills_root):
        for name in sorted(os.listdir(skills_root)):
            md = os.path.join(skills_root, name, "SKILL.md")
            if os.path.isfile(md) and name not in seen:
                out.append({"name": name, "description": _description_from_md(md),
                            "source": "user-skill"})
                seen.add(name)

    plugins_root = os.path.join(home, ".claude", "plugins")
    if os.path.isdir(plugins_root):
        for md in sorted(glob.glob(
            os.path.join(plugins_root, "**", "skills", "*", "SKILL.md"), recursive=True
        )):
            name = os.path.basename(os.path.dirname(md))
            if name not in seen:
                out.append({"name": name, "description": _description_from_md(md),
                            "source": "plugin-skill"})
                seen.add(name)

    for cdir in (os.path.join(home, ".claude", "commands"),):
        if os.path.isdir(cdir):
            for fn in sorted(os.listdir(cdir)):
                if fn.endswith(".md"):
                    name = fn[:-3]
                    if name not in seen:
                        out.append({"name": name,
                                    "description": _description_from_md(os.path.join(cdir, fn)),
                                    "source": "user-command"})
                        seen.add(name)

    if project_cwd:
        pdir = os.path.join(project_cwd, ".claude", "commands")
        if os.path.isdir(pdir):
            for fn in sorted(os.listdir(pdir)):
                if fn.endswith(".md"):
                    name = fn[:-3]
                    if name not in seen:
                        out.append({"name": name,
                                    "description": _description_from_md(os.path.join(pdir, fn)),
                                    "source": "project-command"})
                        seen.add(name)

    return out
