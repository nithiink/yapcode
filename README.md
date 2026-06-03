# voice-claude

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

## Install with Homebrew (recommended)

The quickest path on macOS (and Linuxbrew). The formula pulls in the system
dependencies — `tmux`, Python 3.12, and Node — for you, builds the app, and puts
a `voice-claude` command on your `PATH`:

```bash
brew tap nithiink/voice-claude
brew install voice-claude
```

Then launch it:

```bash
voice-claude up
```

The first `up` runs a short **setup wizard**: it asks for your voice provider and
API key and the folder(s) the agent may edit, auto-generates a `VC_AUTH_TOKEN`
(for later network/phone use), and writes everything to
`~/.config/voice-claude/.env` with `600` permissions. It then starts the backend
and frontend and opens the app in your browser. Press Ctrl-C to stop both. That
config file is the single source of truth — later runs never re-prompt.

You still need **Claude Code installed and logged in** (see
[Prerequisites](#prerequisites)) — it's the engine voice-claude drives. Homebrew
can't install or log you into that for you.

Other commands:

```bash
voice-claude config    # open ~/.config/voice-claude/.env to change settings
voice-claude session   # start + attach a voice-ready Claude session in this dir
```

Update later with `brew upgrade voice-claude`. Your settings
(`~/.config/voice-claude`) and runtime state (`~/.local/state/voice-claude`)
live outside the install and survive upgrades and uninstall.

For LAN/phone access, set up your config first with `voice-claude up`, then see
[LAN / phone (network mode)](#lan--phone-network-mode) — that path uses the
`VC_AUTH_TOKEN` the wizard already generated.

## Setup (from source)

Prefer to run from a clone — for development, or to hack on the code? Install the
dependencies manually (you'll also need `tmux` on your `PATH`):

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

## Use it alongside your terminal (live shared session)

You can drive one Claude session by **voice and keyboard at the same time** — talk to the voice
agent while you keep typing in your terminal, all on the same live `claude` process. This works
because voice-claude runs each session in a tmux session (`vc_<id>`), and tmux lets multiple
clients (the browser live-terminal, the voice agent, and your own `tmux attach`) drive one pane
at once — single process, single transcript, no conflicts.

Install the Claude Code plugin in [`integrations/claude-code-plugin/`](integrations/claude-code-plugin/),
then:

- **Start voice-ready:** run `voice-claude` (the plugin's launcher) instead of `claude`. Work
  normally; type `/voice-handoff` to switch voice on — no restart, keep typing, open the app to
  talk.
- **From a plain `claude` session:** type `/voice-handoff`; voice-claude reopens the session
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
