# yapcode

A voice front-end for [Claude Code](https://claude.com/claude-code). A realtime
voice agent (OpenAI / Azure OpenAI over WebRTC, or Google Gemini Live over
WebSocket) acts as Claude's "mouth and ears": you speak, and it drives real
Claude Code sessions on your machine — starting sessions in your projects,
sending instructions, approving permission prompts, running slash commands, and
narrating results back to you. A live terminal view streams the interactive
Claude TUI to the browser (and your phone).

- **Backend** (`backend/`): FastAPI. Mints short-lived voice-provider tokens
  (provider keys stay server-side) and turns the voice agent's tool calls into
  real actions against Claude Code via tmux. 
- **Frontend** (`frontend/`): Next.js 16 / React 19. The browser UI, the voice
  transports, and the live xterm terminal.

> ⚠️ **This backend executes commands on your computer.** Read
> [Security & access control](#security--access-control) before exposing it
> beyond localhost.

## Prerequisites

- Python 3.12+ and Node 20+.
- A Claude Code login (the CLI uses your existing subscription — no Anthropic API
  key needed).
- A realtime voice provider key: Azure OpenAI, OpenAI, or Google Gemini.
- macOS or Linux (it needs `tmux`). On **Windows**, run it under WSL2 — see
  [Windows (WSL2)](#windows-wsl2).

## Install with Homebrew (recommended)

The quickest path on macOS (and Linuxbrew). The formula pulls in the system
dependencies — `tmux`, Python 3.12, and Node — for you, builds the app, and puts
a `yapcode` command on your `PATH`:

```bash
brew tap nithiink/yapcode
brew install yapcode
```

Then launch it:

```bash
yapcode up
```

The first `up` runs a short **setup wizard**: it asks for your voice provider and
API key and the folder(s) the agent may edit, auto-generates a `VC_AUTH_TOKEN`
(for later network/phone use), and writes everything to
`~/.config/yapcode/.env` with `600` permissions. It then starts the backend
and frontend and opens the app in your browser. Press Ctrl-C to stop both. That
config file is the single source of truth — later runs never re-prompt.

You still need **Claude Code installed and logged in** (see
[Prerequisites](#prerequisites)) — it's the engine yapcode drives. Homebrew
can't install or log you into that for you.

Other commands:

```bash
yapcode config    # open ~/.config/yapcode/.env to change settings
yapcode session   # start + attach a voice-ready Claude session in this dir
```

Update later with `brew upgrade yapcode`. Your settings
(`~/.config/yapcode`) and runtime state (`~/.local/state/yapcode`)
live outside the install and survive upgrades and uninstall.

For LAN/phone access, set up your config first with `yapcode up`, then see
[LAN / phone (network mode)](#lan--phone-network-mode) — that path uses the
`VC_AUTH_TOKEN` the wizard already generated.

## Setup (from source)

Prefer to run from a clone — for development, or to hack on the code? Install the
dependencies manually (you'll also need `tmux` on your `PATH`):

> **Check your Python first:** `python3 --version` must print **3.12+**. With an
> older default (Ubuntu 20.04 ships 3.8) pip fails with the misleading error
> `No matching distribution found for claude-agent-sdk==…`. If yours is older:
> on macOS `brew install python@3.12` and substitute `python3.12` for `python3`
> below; on Ubuntu upgrade to **24.04**, whose default `python3` is 3.12.

```bash
# 1. Backend deps
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Configure
cp .env.example .env
#    then edit .env — see "Configuration" below. At minimum set a voice provider
#    key and ALLOWED_PROJECT_ROOTS.

# 3. Frontend deps
cd ../frontend
npm ci
```

## Running

### Localhost (simplest)

```bash
# terminal 1 — backend on http://localhost:8000
cd backend && ./run.sh

# terminal 2 — frontend on http://localhost:3000
cd frontend && npm run dev
```

Open <http://localhost:3000>. On localhost the backend trusts loopback, so no
auth token is needed.

### LAN / phone (network mode)

Binds the backend to `0.0.0.0` over TLS so your phone can reach it.

```bash
# requires VC_AUTH_TOKEN set in backend/.env (run-network.sh fails closed without it)
cd backend && ./run-network.sh        # https://0.0.0.0:8000
cd frontend && npm run dev:network     # https://0.0.0.0:3000
```

- Needs dev TLS certs at `frontend/.certs/dev-key.pem` and `dev-cert.pem`.
- One-time per device: open `https://<host>:8000` and accept the self-signed
  cert, or the `wss` terminal is silently blocked.
- On each device, open the app once as
  `https://<host>:3000/#vc_token=<your VC_AUTH_TOKEN>` — the browser stores the
  token and strips it from the URL.

## Windows (WSL2)

There is **no native Windows support**: yapcode drives Claude Code through
**tmux**, which only runs on Unix-like systems. The supported path is **WSL2**
(Windows Subsystem for Linux 2) — a real Linux kernel running inside Windows, so
`tmux`, Python, Node, and Claude Code all run natively. WSL2 forwards
`localhost` to your Windows browser, and the mic works there because `localhost`
is a secure context (no TLS certs needed) — so the app behaves just like it does
on macOS.

Run **every command below inside the Ubuntu terminal**, not PowerShell (except
step 1).

**1. Install WSL2** — in **PowerShell (Run as administrator)**:

```powershell
wsl --install
```

Reboot, launch **Ubuntu** from the Start menu, and set a username/password.

**2. Install the toolchain** (in the Ubuntu terminal):

```bash
sudo apt update && sudo apt install -y tmux git python3 python3-venv curl
# Node 20+ via nvm:
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
exec $SHELL
nvm install 20
```

Then check `python3 --version` — it must be **3.12+**. **Ubuntu 24.04** (what a
fresh `wsl --install` gives you) ships 3.12; if you're on an older WSL distro
(20.04 ships 3.8, 22.04 ships 3.10), install a current one from PowerShell and
redo these steps inside it:

```powershell
wsl --install -d Ubuntu-24.04
```

**3. Install Claude Code *inside WSL* and log in.** It must live on the Linux
side, not Windows. Install per <https://claude.com/claude-code>, then run
`claude` once and sign in (uses your existing subscription — no API key needed).

**4. Get the project and run it** — same as on macOS, from the Ubuntu terminal.
Either use the launcher from a clone:

```bash
git clone <repo-url> ~/yapcode     # clone into your Linux home (see below)
cd ~/yapcode
./bin/yapcode up                   # first run = setup wizard, then starts both servers
```

…or follow [Setup (from source)](#setup-from-source) + [Running](#running)
manually. ([Homebrew](#install-with-homebrew-recommended) also works under WSL2
via Linuxbrew once the repo is public.)

Then open **`http://localhost:3000` in your Windows browser** (Chrome/Edge).

**WSL2-specific gotchas:**

- **Clone into your Linux home** (e.g. `~/yapcode`), **not** `/mnt/c/...` —
  the Windows-mounted drive is slow and breaks file watching.
- Set `ALLOWED_PROJECT_ROOTS` to a **Linux path** (e.g. `/home/<you>/projects`),
  and keep the projects you want Claude to edit on the Linux side too.
- Open the app at **`http://localhost:3000`**, not a LAN IP — WSL2 forwards
  `localhost`, and the mic only works in that secure context.

## Use it alongside your terminal (live shared session)

You can drive one Claude session by **voice and keyboard at the same time** — talk to the voice
agent while you keep typing in your terminal, all on the same live `claude` process. This works
because yapcode runs each session in a tmux session (`vc_<id>`), and tmux lets multiple
clients (the browser live-terminal, the voice agent, and your own `tmux attach`) drive one pane
at once — single process, single transcript, no conflicts.

Install the Claude Code plugin in [`integrations/claude-code-plugin/`](integrations/claude-code-plugin/),
then:

- **Start voice-ready:** run `yapcode` (the plugin's launcher) instead of `claude`. Work
  normally; type `/voice-handoff` to switch voice on — no restart, keep typing, open the app to
  talk.
- **From a plain `claude` session:** type `/voice-handoff`; yapcode reopens the session
  under management and prints a `tmux attach -t vc_…` command — press Ctrl-D, run it, and you're
  co-driving with full features.

When co-driving, take turns — don't type and talk in the exact same instant (both share one
terminal). See the plugin README for install/config.

## Configuration

All backend config lives in `backend/.env` (copy from `.env.example`). Key
settings:

| Variable | Required | Purpose |
|----------|----------|---------|
| `ALLOWED_PROJECT_ROOTS` | **Yes** | Comma-separated dirs the agent may start sessions in. **The directory sandbox is mandatory** — if this is unset, `start_session` refuses to start (fail closed). Sessions cannot escape these roots. |
| `VC_AUTH_TOKEN` | For network mode | Shared secret required on every sensitive request when set. **Required to expose the backend beyond localhost** (see below). |
| `VOICE_PROVIDER` | Yes | `azure` \| `openai` \| `gemini` (can also be toggled in the UI). |
| `AZURE_OPENAI_*` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | Per provider | Provider credentials — stay server-side; the browser only ever gets a short-lived ephemeral token. |
| `CLAUDE_MODEL` | No | `opus` (default) or `sonnet`. |
| `VC_ALLOWED_ORIGINS` / `VC_ALLOWED_ORIGIN_REGEX` | No | Extra browser origins allowed cross-origin (localhost + private-LAN are allowed by default). |

See `backend/.env.example` for the full annotated list.

## Security & access control

The backend turns voice/tool calls into real command execution, so access is
gated by two independent layers:

1. **Authentication — who can act.** Every sensitive endpoint and the live
   terminal WebSocket require a shared secret `VC_AUTH_TOKEN`:
   - **If `VC_AUTH_TOKEN` is set**, it is required from *every* caller (including
     loopback, so the same-origin frontend proxy can't be used to relay an
     unauthenticated remote request).
   - **If it is unset**, only loopback (localhost) clients are allowed and all
     remote requests are refused — so plain `run.sh` works with zero config,
     while `run-network.sh` fails closed unless you set a token first.

   The browser supplies the token once via
   `https://<host>:3000/#vc_token=<token>` (persisted to `localStorage`); the
   Next `/api/*` routes forward it; the WebSocket/SSE pass it as a query param.

   The frontend dev server (`npm run dev`) binds **loopback only**
   (`-H 127.0.0.1`), so in the zero-config localhost mode the `/api/*` proxy is
   not reachable from the LAN and cannot relay a remote caller into the
   loopback-trusted backend. LAN/phone access uses `npm run dev:network`, which
   binds `0.0.0.0` and pairs with `run-network.sh`'s mandatory token. The
   `/api/*` routes also reject **cross-site** requests (`Sec-Fetch-Site`, with an
   `Origin`/`Host` fallback), so a malicious page open in your browser can't
   drive the backend via the proxy (drive-by CSRF) — the proxy is the boundary a
   browser reaches, and it enforces same-origin there.

   Generate a token with:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **Directory sandbox — where they can act.** `ALLOWED_PROJECT_ROOTS` confines
   every session's working directory. It is **mandatory**: with no roots
   configured, `start_session` refuses rather than allowing a session anywhere on
   the filesystem. Paths are realpath-resolved and containment-checked, so `..`,
   symlinks, and absolute paths cannot escape the roots.

Additional hardening: CORS is restricted to the trusted frontend origins (no
wildcard) and the Origin allowlist is enforced in-app (not just via CORS response
headers); the WebSocket handshake validates Origin + token before accepting; the
auth token is redacted from access logs; and the interactive API docs
(`/docs`, `/openapi.json`) are disabled.
