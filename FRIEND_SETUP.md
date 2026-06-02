# voice-claude — tester setup (private beta)

Thanks for testing voice-claude! You'll run it **on your own Mac** — it drives
**your** Claude Code on **your** projects, with **your** keys. Nothing you do
touches anyone else's machine.

Access is **read-only**: you can clone and pull updates, but you can't push.
That's expected for the beta.

---

## Phase 1 — Get access (one time)

You'll create a throwaway SSH key just for this repo and send me the **public**
half. Paste this into your terminal:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/voiceclaude_deploy -N "" -C "voice-claude readonly"
echo "---- send me everything below this line ----"
cat ~/.ssh/voiceclaude_deploy.pub
```

Copy the `ssh-ed25519 ...` line it prints and send it to me. **Wait until I
confirm it's added** before the next step. (Keep the private key
`~/.ssh/voiceclaude_deploy` to yourself — never share it.)

Then add an SSH alias so `git` uses this key automatically for this repo:

```bash
cat >> ~/.ssh/config <<'EOF'

Host github-voiceclaude
  HostName github.com
  User git
  IdentityFile ~/.ssh/voiceclaude_deploy
  IdentitiesOnly yes
EOF
```

## Phase 2 — Clone (after I confirm your key is added)

```bash
git clone git@github-voiceclaude:nithiink/voice-claude.git
cd voice-claude
```

To get updates later, just `git pull` from inside the folder. (A `git push`
will be rejected — that's intentional.)

## Phase 3 — Prerequisites

- **macOS**, **Python 3.12+**, **Node 20+**, and **tmux** (`brew install tmux`).
- **Claude Code installed and logged in** — this is the engine. Install from
  <https://claude.com/claude-code>, then run `claude` once and sign in. (Uses
  your own Claude subscription — no Anthropic API key needed.)
- **One realtime voice key.** Easiest is **OpenAI**: create a key at
  <https://platform.openai.com/api-keys> (Azure OpenAI and Google Gemini also
  work if you prefer).

## Phase 4 — Set up & run (localhost)

On localhost the mic works without certs and **no auth token is needed**.

```bash
# 1. Backend deps
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Config — copy the template, then edit .env (see below)
cp .env.example .env
```

Open `backend/.env` and set just these three (using OpenAI as the provider):

```ini
VOICE_PROVIDER=openai
OPENAI_API_KEY=sk-...your-key...
# Folders the agent is allowed to work in (comma-separated). REQUIRED.
ALLOWED_PROJECT_ROOTS=/Users/YOUR_NAME/Development
```

Then run it (two terminals):

```bash
# terminal 1 — backend on http://localhost:8000
cd backend && ./run.sh

# terminal 2 — frontend on http://localhost:3000
cd frontend && npm ci && npm run dev
```

Open <http://localhost:3000>, allow the mic, and start talking. Try:
*"start a session in <one of your projects> and add a hello world file."*

## Windows? Run it under WSL2

voice-claude needs a Unix environment (it drives Claude through **tmux**, which
doesn't run on native Windows). The fix is **WSL2** — a real Ubuntu Linux inside
Windows. WSL2 forwards `localhost` to your Windows browser, so the app and the
mic work exactly like on Mac. Everything below runs **inside the Ubuntu
terminal**, not PowerShell.

**1. Install WSL2** — in **PowerShell (Run as administrator)**:

```powershell
wsl --install
```

Reboot, then launch **Ubuntu** from the Start menu and set up a username/password.

**2. Install the toolchain** (in the Ubuntu terminal):

```bash
sudo apt update && sudo apt install -y tmux git python3 python3-venv
# Node 20+ via nvm:
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
exec $SHELL
nvm install 20
```

**3. Install Claude Code *inside WSL* and log in** — it must live on the Linux
side. Install per <https://claude.com/claude-code>, then run `claude` once and
sign in.

**4. Now follow the normal steps above, all inside Ubuntu:** Phase 1 (generate
the SSH key — your `~/.ssh` is the Linux home), Phase 2 (clone), and Phase 4
(venv, `.env`, `./run.sh`, `npm ci && npm run dev`).

**WSL-specific gotchas:**

- **Clone into your Linux home** (e.g. `~/voice-claude`), **not** `/mnt/c/...` —
  the Windows-mounted drive is slow and breaks file watching.
- Set `ALLOWED_PROJECT_ROOTS` to a **Linux path** (e.g.
  `/home/YOUR_NAME/projects`), and keep the projects you want Claude to edit on
  the Linux side too.
- Open **`http://localhost:3000` in your Windows browser** (Chrome/Edge) — WSL2
  forwards localhost, and the mic works because it's localhost.

## Voice + terminal at the same time (optional)

You can talk to the voice agent **and** keep typing in your terminal on the same live session.
Install the Claude Code plugin in `integrations/claude-code-plugin/` (see its README), then
either start sessions with the `voice-claude` launcher (instead of `claude`) and type
`/voice-handoff` to flip voice on with no restart, or run `/voice-handoff` in a plain `claude`
session and follow the printed `tmux attach` command. Take turns — don't type and talk in the
exact same instant.

## Using it from your phone (optional)

Localhost is laptop-only. To use it from your phone you'd run network mode
(`run-network.sh` + `npm run dev:network`) with a `VC_AUTH_TOKEN` and dev TLS
certs — see the main `README.md` → "LAN / phone (network mode)". Skip this for
first testing.

## Notes & feedback

- ⚠️ This runs real commands on **your** machine inside `ALLOWED_PROJECT_ROOTS`.
  Start sessions in test/scratch folders while you kick the tires.
- Found a bug or have feedback? Message me directly (you won't have GitHub
  issue access on the private repo). If you want to share a code fix, run
  `git format-patch` and send me the patch.

## Troubleshooting

- `git@github-voiceclaude: Permission denied (publickey)` → your key isn't added
  yet (ping me) or the `~/.ssh/config` alias above is missing.
- `start_session refuses` / "no allowed roots" → `ALLOWED_PROJECT_ROOTS` isn't
  set in `backend/.env`.
- Mic doesn't work → make sure you're on `http://localhost:3000` (not a LAN IP),
  and allow mic permission for the site.
