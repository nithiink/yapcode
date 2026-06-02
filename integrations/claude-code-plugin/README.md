# voice-claude — Claude Code plugin

Bring the [voice-claude](../../) voice agent into a Claude Code session you're running in
your terminal, so you can **continue your work by voice while still typing in the same
session**. One `claude` process, driven by your keyboard *and* the voice agent at once (via
tmux's multi-client attach) — no second process, no jumbled history.

## What's in here

- **`/voice-handoff`** — a slash command that registers the current session with your local
  voice-claude backend so the voice agent can co-drive it.
- **`voice-claude`** — a launcher (`bin/voice-claude`) you use *instead of* `claude` to start a
  session that's voice-ready from the first moment (so `/voice-handoff` is instant, no restart).

## Prerequisites

- The voice-claude backend running on this machine (`backend/ ./run.sh`, default
  `http://localhost:8000`).
- `tmux` installed.

## Install

```bash
# from a marketplace (once published):
/plugin marketplace add nithiink/voice-claude
/plugin install voice-claude

# or for local testing, point Claude Code at this folder:
claude --plugin-dir /path/to/voice-claude/integrations/claude-code-plugin
```

Put the launcher on your PATH so you can run `voice-claude`:

```bash
ln -s /path/to/voice-claude/integrations/claude-code-plugin/bin/voice-claude ~/.local/bin/voice-claude
```

## Configure (only for a remote / tunneled backend)

On the same machine, no config is needed — it talks to `http://localhost:8000` and localhost
needs no token. To reach a remote backend, set:

```bash
export VOICE_CLAUDE_URL="https://your-backend"      # e.g. a tunnel URL
export VOICE_CLAUDE_TOKEN="your VC_AUTH_TOKEN"        # required when the backend has one set
```

## Use it

**Seamless (recommended) — start voice-ready:**

```bash
voice-claude            # instead of `claude`; drops you into a voice-ready session
> …work normally, typing…
> /voice-handoff        # voice switches on — keep typing here, open the app to talk
```

**From a session you already started with plain `claude`:**

```bash
> /voice-handoff
# voice-claude reopens this session under voice management and prints a
#   tmux attach -t vc_xxxxxxxx
# command. Press Ctrl-D to leave this process, then run that command to keep
# typing in the same session while the voice agent also drives it.
```

> When co-driving, don't type and talk in the exact same instant — keystrokes from both
> clients share one terminal; take turns.
