# yapcode — Claude Code plugin

Bring the [yapcode](../../) voice agent into a Claude Code session you're running in
your terminal, so you can **continue your work by voice while still typing in the same
session**. One `claude` process, driven by your keyboard *and* the voice agent at once (via
tmux's multi-client attach) — no second process, no jumbled history.

## What's in here

- **`/voice-handoff`** — a slash command that registers the current session with your local
  yapcode backend so the voice agent can co-drive it.
- **`yapcode`** — a launcher (`bin/yapcode`) you use *instead of* `claude` to start a
  session that's voice-ready from the first moment (so `/voice-handoff` is instant, no restart).

## Prerequisites

- The yapcode backend running on this machine (`backend/ ./run.sh`, default
  `http://localhost:8000`).
- `tmux` installed.

## Install

```bash
# once, from any terminal — installs at user scope, available in every session:
claude plugin marketplace add nithiink/yapcode
claude plugin install yapcode@yapcode

# (same thing from inside a session: /plugin marketplace add … then /plugin install …)

# or, for plugin development, load for one session only:
claude --plugin-dir /path/to/yapcode/integrations/claude-code-plugin
```

Put the launcher on your PATH so you can run `yapcode`:

```bash
ln -s /path/to/yapcode/integrations/claude-code-plugin/bin/yapcode ~/.local/bin/yapcode
```

## Configure (only for a remote / tunneled backend)

On the same machine, no config is needed — it talks to `http://localhost:8000` and localhost
needs no token. To reach a remote backend, set:

```bash
export YAPCODE_URL="https://your-backend"      # e.g. a tunnel URL
export YAPCODE_TOKEN="your VC_AUTH_TOKEN"        # required when the backend has one set
```

## Use it

**Seamless (recommended) — start voice-ready:**

```bash
yapcode            # instead of `claude`; drops you into a voice-ready session
> …work normally, typing…
> /voice-handoff        # voice switches on — keep typing here, open the app to talk
```

**From a session you already started with plain `claude`:**

```bash
> /voice-handoff
# yapcode reopens this session under voice management and prints a
#   tmux attach -t vc_xxxxxxxx
# command. Press Ctrl-D to leave this process, then run that command to keep
# typing in the same session while the voice agent also drives it.
```

> When co-driving, don't type and talk in the exact same instant — keystrokes from both
> clients share one terminal; take turns.
