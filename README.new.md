# yapcode

**Talk to your laptop. Watch Claude Code write the code.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: macOS | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-blue.svg)](#prerequisites)
[![Works with Claude Code](https://img.shields.io/badge/works%20with-Claude%20Code-d97757.svg)](https://claude.com/claude-code)

**yapcode is a voice front-end for [Claude Code](https://claude.com/claude-code).** A realtime
voice agent becomes Claude's **mouth and ears**: you speak, and it drives **real** Claude Code
sessions on your machine via `tmux` — starting sessions in your projects, sending instructions,
approving permission prompts, running slash commands, and narrating the results back. A live
terminal streams the actual Claude TUI to your browser and your phone, so you can watch (and
take over by keyboard) at any time.

<!-- DEMO: 30-60s GIF/video goes here — show: speak an instruction → Claude Code works in the live terminal → result narrated back; include phone view -->

### What it does

- 🎙️ **Hands-free Claude Code** — speak an instruction; the voice agent relays it to a real `claude` session and reads the answer back.
- 🖥️ **Live terminal, anywhere** — the interactive Claude TUI streams to your browser and phone via xterm.js; watch it work or grab the keyboard.
- 🔀 **Three voice providers** — OpenAI, Azure OpenAI, or Google Gemini Live. Gemini has a free tier. Provider keys stay server-side; the browser only gets a short-lived token.
- ✅ **Approve by voice** — Claude pauses for permission; say "yes" / "allow" / "switch to auto mode" without touching the keyboard.
- 🤝 **Co-drive** — voice and keyboard share one `tmux` session, so you can type and talk on the same Claude session simultaneously.

> [!WARNING]
> **The backend executes real commands on your computer** — it drives `claude`, which runs
> shell commands, edits files, and approves tool calls on your behalf. Read
> **[Security model (read this first)](#security-model-read-this-first)** before you expose it
> beyond `localhost`.

---

## 60-second quickstart

> [!IMPORTANT]
> **You need three things before yapcode can do anything:**
>
> 1. **[Claude Code](https://claude.com/claude-code), installed and logged in** — yapcode
>    drives your existing Claude Code subscription (no Anthropic API key needed):
>    ```sh
>    curl -fsSL https://claude.ai/install.sh | bash   # or see https://claude.com/claude-code
>    claude                                           # run once to sign in
>    ```
> 2. **`tmux`, Python 3.12+, and Node 20+** — `yapcode up` checks for them and tells you
>    exactly what to install if anything's missing.
> 3. **A voice-provider API key** — Google **Gemini** (has a free tier), **OpenAI**, or
>    **Azure OpenAI**. The first-run wizard asks for it.
>
> Full details: [Prerequisites](#prerequisites).

### Works today — install from source

```sh
git clone https://github.com/nithiink/yapcode.git
cd yapcode
./bin/yapcode up          # first run: setup wizard → auto-installs deps → opens http://localhost:3000
```

On first run a short wizard asks for an (optional) **Gemini** key, an (optional) **OpenAI** key,
and the **folder(s)** the agent may edit — then writes `~/.config/yapcode/.env` (mode `600`).
After that, `yapcode up` just starts both servers and opens the app. See the
[first-run wizard](#first-run-setup-wizard) for details.

### Coming soon — Homebrew

> **Homebrew distribution is not live yet** (the formula needs the repo public + a release tag).
> Until then, use the from-source path above — it's the same wizard and just as fast.

```sh
brew tap nithiink/yapcode
brew install yapcode      # pulls in tmux, python@3.12, node; builds the app
yapcode up                # first run: setup wizard → starts servers → opens browser
```

---

## Getting the most out of yapcode

A quick tour of the ways people actually use it:

1. **🎙️ Start everything by voice** — open the app and just talk: *"start a session in my blog repo"*. The agent launches a real Claude session, relays your instructions, and narrates results back — while the live terminal streams in your browser.

2. **🔁 Hand off in either direction** — in a terminal `claude` session, type `/voice-handoff` to continue **that exact session** by voice; started by voice in the app? Run the printed `tmux attach` to take the keyboard. Details: [co-driving](#co-driving-voice--keyboard-on-one-session).

3. **🤝 Co-drive — type *and* talk** — voice and keyboard share one tmux session. Type a precise edit, lean back, steer by voice — same session, one transcript.

4. **📱 Code from mobile** — run [network mode](#lan--phone-network-mode-tls--token-required), open the app on your phone (same Wi-Fi), and you have the full experience anywhere in the house: talk to Claude **and watch the live terminal**, while the work happens on your machine.

5. **✅ Approve permissions with voice, switch modes** — Claude pauses for permission? Just say *"yes"*, *"allow"*, or *"switch to auto mode"*.

6. **🖥️ Control the real TUI via voice** — *"run /init"* · *"interrupt that"* · *"what's on the screen?"* — the agent runs slash commands, stops runaway tasks, and reads you the actual terminal.

7. **🗂️ Juggle projects** — list, start, rename, switch, and close sessions across all your allowed folders — entirely by voice.

8. **☕ Kick off and walk away** — start a long refactor, go make coffee, then ask *"how's it going?"* — the agent peeks at the session and tells you. Approve the next prompt from wherever you are.

---

## Contents

- [Getting the most out of yapcode](#getting-the-most-out-of-yapcode)
- [How it works](#how-it-works)
- [Security model (read this first)](#security-model-read-this-first)
- [What's in the box](#whats-in-the-box)
- [Prerequisites](#prerequisites)
- [Install with Homebrew (recommended)](#install-with-homebrew-recommended)
- [Install from source](#install-from-source)
- [Running](#running)
- [Windows (WSL2)](#windows-wsl2)
- [Configuration reference](#configuration-reference)
- [Security & access control (details)](#security--access-control-details)
- [Co-driving: voice + keyboard on one session](#co-driving-voice--keyboard-on-one-session)
- [Troubleshooting](#troubleshooting)
- [Architecture (for contributors)](#architecture-for-contributors)
- [Contributing](#contributing)
- [License](#license)

---

## How it works

```
  you ──speak──▶  Voice agent          (OpenAI / Azure, or Gemini Live)
                    │  tool calls
                    ▼
                 Backend  (FastAPI, :8000)
                    │  tmux send-keys / capture-pane
                    ▼
                 Claude Code CLI  (real `claude` session in a tmux pane)
                    │
                    ▼
  browser/phone ◀── live xterm.js terminal  (Next.js frontend, :3000)
```

- **Backend (`backend/`)** — FastAPI. Mints short-lived voice-provider tokens (your provider
  keys stay server-side; the browser only ever gets an ephemeral token) and turns the voice
  agent's tool calls into real actions against Claude Code via tmux.
- **Frontend (`frontend/`)** — Next.js 16 / React 19. The browser UI, the realtime audio
  transports, and the live xterm.js terminal that streams the Claude TUI.
- Each session runs in a tmux session named `vc_<id>`, so the voice agent, the browser
  terminal, and your own keyboard can all drive the same Claude session at once.

The code-level path through this pipeline is in
[Architecture (for contributors)](#architecture-for-contributors).

---

## Security model (read this first)

yapcode runs a backend that **executes commands on your machine**. It has two independent
layers of defense — both **fail-closed**.

| Layer | Control | What it does |
| --- | --- | --- |
| **1. Authentication** | `VC_AUTH_TOKEN` | A shared secret that gates every sensitive endpoint and the live-terminal WebSocket. **If set**, it is required from *every* caller — including loopback (`loopback` = connections from your own machine: `127.0.0.1`, `::1`, `localhost`). **If unset**, only loopback clients are allowed and all remote callers are refused. Network mode (`run-network.sh`, binding `0.0.0.0`) **refuses to start without a token**. |
| **2. Directory sandbox** | `ALLOWED_PROJECT_ROOTS` | A **mandatory** allowlist of directories Claude sessions may run in. If unset, `start_session` refuses — sessions cannot be started anywhere on the filesystem. Paths are `realpath`-resolved and containment-checked, so `..`, symlinks, and absolute-path escapes are blocked. |

In plain language:

- **On `localhost`, it just works** with zero config — the backend trusts loopback (your own
  machine: `127.0.0.1`, `::1`, `localhost`), so no token is needed.
- **The moment you go off your laptop** (LAN, phone, tunnel), you *must* set a token, and
  the network launcher will not start without one.
- **Claude can never wander outside the folders you list** in `ALLOWED_PROJECT_ROOTS`.

Full details — the `_access_ok` check, Origin allowlist, CSRF defenses, log redaction — are in
[Security & access control (details)](#security--access-control-details) below.

---

## What's in the box

```
yapcode/
├── backend/                   # FastAPI: mints voice tokens, drives Claude via tmux
├── frontend/                  # Next.js 16 / React 19: UI, voice transports, xterm terminal
├── integrations/              # Claude Code plugin (/voice-handoff co-driving)
│   └── claude-code-plugin/
├── packaging/                 # Homebrew formula + release script
│   ├── yapcode.rb
│   └── release.sh
├── bin/                       # the `yapcode` CLI launcher (bash)
├── LICENSE                    # MIT
└── README.md
```

---

## Prerequisites

| Requirement | Notes |
| --- | --- |
| **OS** | macOS or Linux. Windows must use **[WSL2](#windows-wsl2)** (yapcode drives Claude Code through tmux, which is Unix-only). |
| **tmux** | On `PATH`. |
| **Python 3.12+** | An older default `python3` fails with a *misleading* `No matching distribution found for claude-agent-sdk` error. |
| **Node 20+** | For the frontend. |
| **Git** | To clone (for the from-source path). |
| **Claude Code** | Installed and **logged in** separately: [claude.com/claude-code](https://claude.com/claude-code), or `curl -fsSL https://claude.ai/install.sh | bash` then run `claude` once to sign in. Uses your existing subscription — no Anthropic API key. Homebrew cannot install or log you into it. |
| **A voice provider key** | OpenAI, Azure OpenAI, or Google Gemini. **Gemini has a free API tier** ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)). |

```sh
# macOS — installs everything except Claude Code itself
brew install tmux node python@3.12 git
```

On **Debian / Ubuntu**, apt covers tmux/Python/git, but its Node is usually too old — install
Node 20+ via [nvm](https://github.com/nvm-sh/nvm) (as shown in the [WSL2 section](#windows-wsl2)),
not from apt. Use Ubuntu 24.04+ for Python 3.12.

```sh
# Debian / Ubuntu — Python/tmux/git only (install Node 20+ separately via nvm)
sudo apt install tmux git python3 python3-venv
```

---

## Install with Homebrew (recommended)

> Not live yet — see [Coming soon — Homebrew](#coming-soon--homebrew) in the quickstart. The
> steps below describe the published experience.

Works on macOS and Linuxbrew. The two install commands are the
[same as the quickstart](#coming-soon--homebrew); the details unique to Homebrew:

- The formula `depends_on` node, python@3.12, and tmux, copies the source into the **Cellar**,
  builds the backend virtualenv and a **production** frontend build (so the launcher runs
  `next start`, with no compile-on-launch), and puts `yapcode` on your `PATH`.
- Your config (`~/.config/yapcode/.env`) and runtime state (`~/.local/state/yapcode/`) live
  **outside** the install and **survive `brew upgrade` and uninstall**.

```bash
brew upgrade yapcode    # later, to update
```

### First-run setup wizard

The first time you run any subcommand (`up` / `session` / `config`) with no config present, a
wizard runs and writes `~/.config/yapcode/.env` (created `0600`, `umask 077`). It prompts for:

1. **Gemini API key** — optional (free tier at <https://aistudio.google.com/apikey>), Enter to skip.
2. **OpenAI key** (`sk-...`) — optional, Enter to skip.
3. **Folder(s) the agent may edit** — comma-separated; a leading `~` is expanded. At least one
   is required; each must exist and may **not** be empty, `/`, or your `$HOME` (rejected as too
   broad).

The wizard derives the default `VOICE_PROVIDER` (Gemini key → `gemini`; else OpenAI key →
`openai`; else `openai` placeholder with a warning to add a key later) and **auto-generates a
`VC_AUTH_TOKEN`** for later network/phone use. **Azure OpenAI is config-file-only** (not
prompted) — add it later with `yapcode config`.

Only the *absence* of the config file triggers the wizard; later runs never re-prompt. To change
anything, edit the file or run `yapcode config`.

### CLI subcommands

| Command | What it does |
| --- | --- |
| `yapcode up` *(default)* | Runs the wizard if needed, bootstraps deps, starts backend (`:8000`) + frontend (`:3000`), opens the app. Ctrl-C stops both. |
| `yapcode session [dir]` | Starts and attaches a voice-ready Claude session in `dir`. |
| `yapcode config` | Opens `~/.config/yapcode/.env` in `$EDITOR` (falls back to `open`). |

`yapcode -h` / `--help` prints `usage: yapcode {up|session [dir]|config}`. An unknown
subcommand prints usage to stderr and exits `2`.

---

## Install from source

This is the path that works today. The two install commands are the
[same as the quickstart](#works-today--install-from-source); the details unique to a clone:

- On first `up` the launcher **bootstraps dependencies**: if `backend/.venv` is missing it picks
  a Python 3.12+ interpreter and `pip install`s `backend/requirements.txt`; if
  `frontend/node_modules` is missing it runs `npm ci`.
- It then preflights ports 8000/3000, starts both servers, polls for readiness (~60s), prints
  `yapcode is running — http://localhost:3000`, and opens it.
- `./bin/yapcode config` and `./bin/yapcode session` work from a clone too.

### Manual / dev setup

Use this if you want full control of each piece (and for contributing — see
[Contributing](#contributing)).

```bash
# Backend
cd backend
python3 -m venv .venv                       # MUST be Python 3.12+ (see Troubleshooting)
.venv/bin/pip install -r requirements.txt
cp .env.example .env                         # then edit: at minimum a voice provider key
                                             #   and ALLOWED_PROJECT_ROOTS

# Frontend
cd ../frontend
npm ci
```

`ALLOWED_PROJECT_ROOTS` and a voice provider key are the minimum required config. `tmux` must be
on your `PATH`. On macOS, if your default `python3` is older than 3.12:

```bash
brew install python@3.12
# then create the venv with python3.12 -m venv .venv
```

> **Config precedence.** With a `backend/.env` present (it is, in a clone), `backend/.env`
> **takes precedence** over `~/.config/yapcode/.env` for any key set in both — `backend/config.py`
> calls `load_dotenv(override=True)`, so `backend/.env` overrides values the launcher exported
> from the config-dir file. The config-dir `~/.config/yapcode/.env` is authoritative only when
> there is **no** `backend/.env` (e.g. a Homebrew install, where the Cellar ships none). For a
> single source of truth in a clone, edit `backend/.env` (or delete it and rely on the config-dir
> file).

---

## Running

> Using the launcher? `yapcode up` already handles localhost running — this section is the
> manual equivalent, useful for development. **Network mode applies to everyone.**

### Localhost (zero-config, trusted loopback)

```bash
# Terminal 1 — backend on http://localhost:8000
cd backend && ./run.sh

# Terminal 2 — frontend on http://localhost:3000
cd frontend && npm run dev
```

On localhost the backend trusts loopback (your own machine: `127.0.0.1`, `::1`, `localhost`), so
**no auth token is needed**. (`yapcode up` deliberately *unsets* `VC_AUTH_TOKEN` for localhost
even if it's in your config, so loopback callers aren't forced to present it — the token stays in
the file and applies only in network mode.) Open <http://localhost:3000>.

### LAN / phone (network mode, TLS + token required)

Network mode binds `0.0.0.0` over HTTPS/WSS and **fails closed without a `VC_AUTH_TOKEN`**.

```bash
# Terminal 1 — backend over TLS on https://0.0.0.0:8000 (needs VC_AUTH_TOKEN in backend/.env)
cd backend && ./run-network.sh

# Terminal 2 — frontend over TLS on https://0.0.0.0:3000
cd frontend && npm run dev:network
```

One-time per device:

1. **Generate dev TLS certs** at `frontend/.certs/dev-key.pem` and `dev-cert.pem`. The cert's
   SANs must include the device's IP. Edit `frontend/.certs/san.cnf` to set your LAN IP (it
   hard-codes one under `[alt]`), then:

   ```bash
   cd frontend/.certs
   openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
     -keyout dev-key.pem -out dev-cert.pem -config san.cnf
   ```

   Re-run this whenever your LAN IP changes.
2. Visit `https://<host>:8000` once and **accept the self-signed cert** — otherwise the `wss`
   terminal connection is silently blocked.
3. Open the app once as `https://<host>:3000/#vc_token=<VC_AUTH_TOKEN>`. The browser stores the
   token in `localStorage` and strips it from the URL. (The token is never baked into the JS
   bundle.)

A phone needs HTTPS (a "secure context") for the microphone to work off localhost.

If you ran the [first-run wizard](#first-run-setup-wizard), **a `VC_AUTH_TOKEN` was already
generated for you** — it's in `~/.config/yapcode/.env` (`yapcode config` to view). Manual setups
can generate one with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Windows (WSL2)

There is **no native Windows support** — yapcode drives Claude Code through tmux, which is
Unix-only. The supported path is WSL2.

```powershell
# 1. In PowerShell (Administrator), then reboot and set a Linux username/password:
wsl --install -d Ubuntu-24.04
```

> Use **Ubuntu 24.04** — it ships Python 3.12. Ubuntu 22.04 ships 3.10 and 20.04 ships 3.8,
> both too old (you'll hit the misleading `claude-agent-sdk` pip error).

```bash
# 2. Inside Ubuntu — system deps:
sudo apt update && sudo apt install -y tmux git python3 python3-venv curl

# 3. Node 20+ via nvm:
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
exec $SHELL
nvm install 20

# 4. Install Claude Code inside WSL and run `claude` once to sign in.

# 5. Clone into the LINUX home (NOT /mnt/c/...) and start:
git clone https://github.com/nithiink/yapcode.git ~/yapcode
cd ~/yapcode
./bin/yapcode up
```

Then open **<http://localhost:3000>** in your Windows browser (WSL2 forwards localhost, and the
mic only works in that secure context — don't use a LAN IP).

WSL gotchas:

- **Clone into the Linux home** (e.g. `~/yapcode`), not `/mnt/c/...` — the latter is slow and
  breaks file watching.
- Set `ALLOWED_PROJECT_ROOTS` to a **Linux path** (e.g. `/home/<you>/projects`) and keep edited
  projects on the Linux side.

---

## Configuration reference

Config lives at **`~/.config/yapcode/.env`** (chmod `600`). Override its location with
`YAPCODE_CONFIG_DIR` or `XDG_CONFIG_HOME`. Runtime state (logs, tmux store) lives at
**`~/.local/state/yapcode`** (override base via `XDG_STATE_HOME`); both survive `brew upgrade`
and uninstall.

The [first-run wizard](#first-run-setup-wizard) writes this file. Note the
[config precedence](#install-from-source): in a clone, a present `backend/.env` **overrides** the
config-dir file for any key set in both; the config-dir file is authoritative only when no
`backend/.env` exists (e.g. Homebrew).

### Core

| Variable | Required | Default | What it does |
| --- | --- | --- | --- |
| `ALLOWED_PROJECT_ROOTS` | **Yes** | — | Comma-separated dirs the agent may start sessions in. **Mandatory sandbox** — if unset, `start_session` refuses (fail closed). |
| `VC_AUTH_TOKEN` | Network mode | _(empty)_ | Shared secret. If **set**, required from every caller incl. loopback; if **unset**, only loopback is allowed and remote is refused. Auto-generated by the wizard. |
| `VOICE_PROVIDER` | Yes | `azure` | `azure` \| `openai` \| `gemini`. Also toggleable in the UI. |
| `CLAUDE_MODEL` | No | `opus` | `opus` or `sonnet`. |

### Voice providers (keys stay server-side)

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | OpenAI provider key. |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-mini` | OpenAI realtime model. |
| `REALTIME_VOICE` | `marin` | OpenAI/Azure realtime voice. |
| `AZURE_OPENAI_ENDPOINT` | — | Azure resource endpoint. |
| `AZURE_OPENAI_API_KEY` | — | Azure key. |
| `AZURE_OPENAI_DEPLOYMENT` | — | Default Azure realtime deployment. |
| `AZURE_OPENAI_DEPLOYMENTS` | _(the single deployment)_ | Comma-separated allowlist of deployments the UI may pick (best first). |
| `GEMINI_API_KEY` | — | Google AI Studio key (free tier available). |
| `GEMINI_MODEL` | `gemini-2.5-flash-native-audio-preview-12-2025` | Gemini native-audio model. |
| `GEMINI_VOICE` | `Kore` | Gemini voice. |

### Origins, state & advanced

| Variable | Default | Notes |
| --- | --- | --- |
| `VC_ALLOWED_ORIGINS` | empty | Extra exact cross-origin browser origins (comma-separated). |
| `VC_ALLOWED_ORIGIN_REGEX` | _(LAN regex)_ | Overrides the default private-LAN origin regex (localhost/127.0.0.1/::1 + 10.x/192.168.x/172.16–31.x); set empty to disable LAN origins. |
| `VC_SESSION_STORE` | `<repo>/.yapcode/tmux` | Session control-dir root (tmux sessions, `meta.json`, `events.jsonl`, …). |
| `VC_COST_LOG_PATH` | `<repo>/cost-log.jsonl` | Append-only cost log. |
| `VC_DEBUG_LOG_PATH` | `<repo>/debug-log.jsonl` | Append-only pipeline event log. |
| `VC_DEBUG_LOG_FILE` | `1` | Set `0` to keep debug events in-memory/SSE only (no file). |
| `VC_DEBUG_BUFFER` | `3000` | In-memory ring-buffer size for the event bus. |
| `VC_KILL_SESSIONS_ON_SHUTDOWN` | `0` | `1` kills CLI sessions on shutdown instead of detaching + rehydrating on restart. |
| `CLAUDE_CLI_CHROME` | _(on)_ | Set `0` to disable passing `--chrome` to the `claude` CLI. |
| `TOOL_TIMEOUT_S` | `600` | Tool-dispatch timeout (seconds). |

Frontend-side: `BACKEND_URL` (default `http://localhost:8000`) is what the Next `/api/*` proxy
forwards to; `dev:network` sets it to `https://localhost:8000` and `NODE_TLS_REJECT_UNAUTHORIZED=0`.

See [`backend/.env.example`](backend/.env.example) for the full annotated list of every variable.

> Under Homebrew, the launcher wrapper exports `YAPCODE_ROOT` (pins the install tree) and
> redirects runtime writes out of the read-only Cellar: `VC_SESSION_STORE` points at
> `~/.local/state/yapcode/tmux` (a subdirectory), while `VC_COST_LOG_PATH` and `VC_DEBUG_LOG_PATH`
> are files directly under `~/.local/state/yapcode`.

---

## Security & access control (details)

The two-layer model is summarized [up top](#security-model-read-this-first); this section adds the
code-level specifics. The security boundary is the **auth token**; the Origin allowlist is a
convenience for same-network devices, not the boundary.

### Layer 1 — authentication (`VC_AUTH_TOKEN`)

`_access_ok` in `backend/main.py` is the gate. A mismatch returns `401` (when a token is
configured) or `403` (when relying on loopback). Token comparison is **constant-time**
(`secrets.compare_digest`). Even loopback must present a configured token, so the same-origin Next
proxy can't launder a remote attacker's request into a "trusted" localhost call. `run-network.sh`
binds `0.0.0.0` and **refuses to start** unless a token is set (checks the env var or a non-empty
`VC_AUTH_TOKEN=` line in `backend/.env`).

**Token transport.** `Authorization: Bearer`, `X-VC-Token` header, or `?token=` query param
(SSE/WebSocket can't set headers). The browser supplies it once via
`https://<host>:3000/#vc_token=<token>` (persisted to `localStorage`, stripped from the URL). It
is **never baked into the JS bundle**. The token is **redacted from access logs** (it rides the
query string on the SSE debug stream and the terminal WebSocket). The terminal WebSocket
authorizes Origin + token **before** `ws.accept()`; a disallowed Origin closes with `4403`, a
bad/missing token with `4401`.

### Layer 2 — directory sandbox (`ALLOWED_PROJECT_ROOTS`)

`ALLOWED_PROJECT_ROOTS` confines every session's working directory. Every candidate path —
including fuzzy-matched ones — is `realpath`-normalized and containment-checked, so `..`,
symlinks, and absolute-path escapes are blocked. The wizard also rejects roots that are empty,
`/`, `$HOME`, or non-existent.

### Additional hardening

- **CORS** is restricted to trusted frontend origins (no wildcard), and the Origin allowlist is
  enforced **in-app** (not just via CORS response headers) — a disallowed Origin gets `403` even
  though CORS would hide the response.
- The Next `/api/*` proxy rejects **cross-site** requests (via `Sec-Fetch-Site`, with Origin/Host
  fallback) to block drive-by CSRF. The frontend dev server binds loopback only (`-H 127.0.0.1`),
  so in zero-config localhost mode the proxy isn't reachable from the LAN; LAN access requires
  `npm run dev:network`.
- Interactive API docs (`/docs`, `/openapi.json`) are **disabled** so the route/schema map isn't
  disclosed.

> The Origin allowlist is a convenience for same-network devices — **the token is the security
> boundary.**

---

## Co-driving: voice + keyboard on one session

Because each session runs in a tmux session named `vc_<id>` and tmux allows multiple clients on
one pane, you can drive a single Claude session by **voice and keyboard at the same time** —
single process, single transcript, no conflicts.

The bridge is the **`/voice-handoff`** slash command, shipped as a Claude Code plugin in
`integrations/claude-code-plugin/`. It registers the terminal session you're sitting in with
your local yapcode backend, so the voice agent can co-drive that exact session. Install the
plugin once (user scope, available in every session):

```sh
claude plugin marketplace add nithiink/yapcode
claude plugin install yapcode@yapcode
```

Then, with the backend running, either:

- **Start voice-ready (recommended):** run the plugin's `yapcode` launcher
  (`integrations/claude-code-plugin/bin/yapcode` — distinct from the top-level `yapcode` CLI)
  instead of plain `claude`. Work normally; type `/voice-handoff` whenever you want voice — it
  switches on instantly, no restart. Keep typing there, and open the app to talk.
- **From a plain `claude` session:** type `/voice-handoff`; yapcode reopens the session under
  voice management and prints a `tmux attach -t vc_…` command. Press **Ctrl-D** to leave the
  old process (single writer per session), then run the attach command to keep typing in the
  same session while the voice agent drives it too. Only want to drive by voice? Just open the
  app — no attach needed.

Reaching a **remote or tunneled backend** instead of localhost: set `YAPCODE_URL` (default
`http://localhost:8000`) and `YAPCODE_TOKEN` (required when the backend has `VC_AUTH_TOKEN`
set).

Both clients share one pane, so **take turns** — don't type and talk in the exact same instant.
More detail in the [plugin README](integrations/claude-code-plugin/README.md).

---

## Troubleshooting

| Symptom | Cause & fix |
| --- | --- |
| `pip` fails: `No matching distribution found for claude-agent-sdk==…` | **Python is too old** — must be **3.12+**. macOS: `brew install python@3.12` and build the venv with `python3.12`. Ubuntu: upgrade to 24.04 (20.04 ships 3.8, 22.04 ships 3.10). |
| `start_session` refuses / "no allowed roots" | `ALLOWED_PROJECT_ROOTS` is unset — the sandbox fails closed by design. Set it to existing folders (not `/`, `$HOME`, or empty). |
| `run-network.sh` exits `1` immediately | Network mode **fails closed without `VC_AUTH_TOKEN`** in `backend/.env`. Generate one and set it, then open the app once with `/#vc_token=<token>`. |
| `yapcode up` exits `1` on startup | **Port 8000 or 3000 is already in use.** Free the port and retry. |
| Live terminal connects but stays blank / silently fails (LAN/phone) | You skipped the one-time self-signed-cert accept at `https://<host>:8000`, so the `wss` terminal is blocked. Visit it once and accept. Also: an HTTPS page must use `wss` (no mixed content). |
| Mic doesn't work on phone | `getUserMedia` needs a secure context. Use `npm run dev:network` (HTTPS on `0.0.0.0`); plain `npm run dev` is HTTP + loopback-only. The device's IP must be in the cert SANs (`frontend/.certs/san.cnf`); regenerate the cert for a new IP. |
| App loads on a phone but toggles/buttons appear dead | Your LAN IP is outside the `192.168` / `10` / `172.16` private ranges, so Next 16 blocked `/_next/*` assets. Add the specific origin to `allowedDevOrigins` in `frontend/next.config.mjs`. |
| Voice model select is greyed out | The model is **locked while connected** — disconnect first to change it. |
| Token I set in config has no effect on localhost | Intentional: `yapcode up` **unsets `VC_AUTH_TOKEN` on localhost** so loopback stays zero-config. The token still applies in network mode. |
| Config changes don't take effect | Check [config precedence](#install-from-source): in a clone, `backend/.env` overrides `~/.config/yapcode/.env`. Edit the file that wins, or run `yapcode config`. The wizard only runs when the config-dir file is absent. |
| First page load is slow | Without a production build (`frontend/.next/BUILD_ID`), the launcher runs `npm run dev`, which compiles the UI on first load. A `next build` produces the `BUILD_ID` and switches it to `npm run start`. |
| Sessions vanished after restart | yapcode can run Claude two ways — the default interactive **CLI** backend or the Agent **SDK** backend (see [Architecture](#architecture-for-contributors)). Only the CLI backend rehydrates detached tmux sessions on restart; SDK subprocesses die with the backend. Set `VC_KILL_SESSIONS_ON_SHUTDOWN=1` to kill on shutdown instead. |
| `brew install yapcode` 404s | Homebrew distribution isn't live until the repo is public and a release tag exists. Use the [clone install](#install-from-source) meanwhile. |

---

## Architecture (for contributors)

The whole point: **a spoken instruction becomes a real tmux action driving the live Claude CLI.**
The high-level pipeline is in [How it works](#how-it-works); below is the path through the code.

```
   You speak
      │
      ▼
┌──────────────────────┐   ephemeral token only      ┌───────────────────────────────┐
│  Voice provider       │ ◀────── POST /api/session ── │  Frontend (Next.js 16/React19) │
│  OpenAI/Azure (WebRTC)│                              │  components/VoiceAgent.tsx     │
│  Gemini (WebSocket)   │ ── function_call ──────────▶ │  lib/realtime.ts | lib/gemini.ts│
└──────────────────────┘                              └───────────────┬───────────────┘
                                                       POST /api/tools/execute (proxy)
                                                                       │
                                                                       ▼
                                                        ┌──────────────────────────────┐
                                                        │  Backend (FastAPI, main.py)    │
                                                        │  POST /tools/execute           │
                                                        │  tools.dispatch_tool (tools.py)│
                                                        └───────────────┬───────────────┘
                                                                        │ tmux send-keys
                                                                        ▼
                                                        ┌──────────────────────────────┐
                                                        │  tmux session  vc_<id>         │
                                                        │  real `claude` CLI (TUI)       │
                                                        └───────────────┬───────────────┘
                                                          tmux capture-pane → narrate back
                                                          PTY over WS → live xterm terminal
```

(WebRTC and WebSocket are just the realtime audio transports for the two provider families.)

**How a voice tool call becomes a tmux action:**

1. The voice model emits a `function_call` (e.g. `tell_claude`, `start_session`,
   `answer_prompt`). The frontend POSTs it to the Next `/api/tools/execute` proxy, which forwards
   to the backend `POST /tools/execute`.
2. `tools.dispatch_tool` (`backend/tools.py`) runs the tool. Long-running ones (`tell_claude`,
   `run_slash_command`, `answer_prompt`) return `{status:"working"}` immediately; the frontend
   polls and narrates when Claude is done.
3. The CLI runner (`backend/tmux_runner.py`) creates a detached tmux session named
   `vc_<handle[:8]>` running the interactive `claude` CLI. Input is `tmux send-keys`; output is
   read with `tmux capture-pane`. Permission decisions flow through a PreToolUse hook in
   `backend/tmux_hooks/`.
4. The browser's live terminal (`frontend/components/LiveTerminal.tsx`, xterm.js) bridges a
   PTY-over-WebSocket to the same tmux pane — closing it just *detaches*; the session keeps
   running. (This shared pane is what enables [co-driving](#co-driving-voice--keyboard-on-one-session).)

**Two Claude execution backends** share a `ClaudeRunner` interface (`backend/session_manager.py`):
`cli` → `TmuxClaudeRunner` (default; real interactive CLI, uses your Max subscription, supports
`--chrome`) and `sdk` → `SDKClaudeRunner` (Claude Agent SDK). Each session handle is a uuid owned
by one backend.

**Unified pipeline event bus** (`backend/event_log.py`): every hop (voice ↔ backend ↔ Claude) is
captured into one ordered stream feeding an in-memory ring buffer, live SSE subscribers (the
in-app *Activity log* via `/debug/stream`), and an append-only `debug-log.jsonl`. This is your
best friend when debugging.

---

## Contributing

Contributions welcome — this is MIT-licensed and built to be hacked on.

### Dev-mode setup (hot reload)

Both servers reload on change, so the inner loop is fast:

```bash
cd backend && ./run.sh             # uvicorn main:app --port 8000 --reload --reload-dir .
cd frontend && npm run dev         # next dev -H 127.0.0.1 -p 3000
```

- **Backend** runs with `uvicorn --reload`. The watcher is scoped to `backend/` (`--reload-dir .`)
  so the rapidly-written events log doesn't trigger reloads. With detach-on-shutdown, reload
  **preserves running CLI sessions** (they rehydrate on restart) — you rarely lose state.
- **Frontend** runs with `npm run dev` (Next dev server, loopback-bound on `127.0.0.1`).

### Dependency management

```bash
# Backend deps are lock-pinned with hashes
uv pip sync requirements.lock                                              # install from the lock
uv pip compile requirements.txt -o requirements.lock --generate-hashes    # regenerate
uv run --with pip-audit python -m pip_audit -r requirements.lock          # audit

# Frontend
npm ci                                                                    # lockfile-exact install
```

### Repo layout map (where things live)

| Path | What's there |
| --- | --- |
| `backend/main.py` | FastAPI app, endpoints, auth (`_access_ok`, `require_auth`, WS auth), lifespan/rehydration. |
| `backend/tools.py` | The voice-model tool list (`TOOL_DEFINITIONS`) + `dispatch_tool`. |
| `backend/tmux_runner.py` | The CLI runner — spawns/drives tmux + the `claude` CLI. |
| `backend/session_manager.py` | `ClaudeRunner` interface, session handles, routing (cli vs sdk). |
| `backend/permissions.py` | Classifies tools as safe / question / risky. |
| `backend/slash_commands.py` | Slash-command discovery + execution. |
| `backend/event_log.py`, `cost_log.py` | Pipeline event bus + cost JSONL logging. |
| `backend/tmux_hooks/` | PreToolUse / Stop / Notification hooks wired into Claude. |
| `frontend/components/VoiceAgent.tsx` | The main UI: connect, timeline, sessions, prompts, cost. |
| `frontend/components/LiveTerminal.tsx` | xterm.js terminal over the PTY WebSocket. |
| `frontend/lib/realtime.ts`, `lib/gemini.ts` | The two voice transports (OpenAI/Azure WebRTC, Gemini Live WS). |
| `frontend/app/api/*` | Same-origin proxy to the backend (CSRF defense, auth pass-through). |
| `integrations/claude-code-plugin/` | The `/voice-handoff` co-driving plugin. |
| `packaging/yapcode.rb`, `release.sh` | Homebrew formula + release script. |

> Tip: the in-app **Activity log** (SSE from `/debug/stream`) shows every voice ↔ backend ↔
> Claude event live — a great way to understand the pipeline before changing it.

### Good first contributions

- **Docs** — clarify setup, expand troubleshooting, improve the WSL2 path, or fix anything in
  this README that tripped you up.
- **Voice providers** — both transports implement one `VoiceSession` interface
  (`frontend/lib/voice.ts`: `start` / `stop` / `injectUpdate` / `setMuted`). Adding or improving
  a provider is well-isolated work; the backend mint logic lives in `backend/main.py`
  (`POST /session`).
- **Integrations** — extend the Claude Code plugin in `integrations/claude-code-plugin/` (e.g.
  the `/voice-handoff` flow).
- **New voice tools** — add to `TOOL_DEFINITIONS` + `dispatch_tool` in `backend/tools.py`.

### PR expectations

- Keep changes scoped; match existing style. The backend pins deps with hashes — if you touch
  `requirements.txt`, regenerate `requirements.lock`.
- Note any new env var, command, port, or flag in the README's [configuration reference](#configuration-reference).
- Test the path you changed locally (the localhost run is zero-config); note which provider(s) /
  OS you tested on, since transports and tmux behavior vary.
- Be explicit if a change affects the **security model** (auth, the directory sandbox,
  CORS/Origin/CSRF handling, redaction) — those need careful review and shouldn't be weakened
  without discussion.
- Don't commit secrets. The committed `cost-log.jsonl` / `debug-log.jsonl` will be scrubbed from
  history before the repo goes public; don't add to them.

> Releases are cut with `packaging/release.sh vX.Y.Z` (clean tree required; idempotent on the
> tag). Homebrew distribution goes live only after the repo is public and a tag exists.

---

## License

[MIT](LICENSE) — Copyright (c) 2026 Nithiin Kathiresan.
