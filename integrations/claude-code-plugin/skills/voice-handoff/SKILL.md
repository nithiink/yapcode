---
name: voice-handoff
description: Hand off THIS terminal Claude Code session to the yapcode voice agent so you can continue it by voice (while still typing in the same session). Runs on /voice-handoff.
allowed-tools: Bash(curl:*)
disable-model-invocation: true
---

# voice-handoff

Register the current session with the local yapcode backend. `${CLAUDE_SESSION_ID}`
is substituted by Claude Code; `$(pwd)` / `$TMUX` are evaluated by the shell. The backend
URL/token default to localhost and can be overridden with the `YAPCODE_URL` /
`YAPCODE_TOKEN` environment variables.

Backend response:

!`curl -s -X POST "${YAPCODE_URL:-http://localhost:8000}/session/handoff" -H "Content-Type: application/json" ${YAPCODE_TOKEN:+-H "X-VC-Token: ${YAPCODE_TOKEN}"} -d "{\"session_id\":\"${CLAUDE_SESSION_ID}\",\"cwd\":\"$(pwd)\",\"tmux\":\"${TMUX:-}\"}" || echo '{"error":"could not reach yapcode — is the backend running at '"${YAPCODE_URL:-http://localhost:8000}"'?"}'`

Using the JSON response above, tell the user in one or two short sentences:

- If it has a `message` / `attach` field: voice is ready on this session in the yapcode
  app. Show the `attach` command and explain they can run it (after pressing **Ctrl-D** to
  leave this terminal session, so only one process writes the session) to keep typing in the
  **same** session while also talking to the voice agent. If they only want to drive by voice,
  they can just open the app — no attach needed.
- If it has an `error`: relay it plainly (most often: the yapcode backend isn't running,
  or `YAPCODE_URL`/`YAPCODE_TOKEN` need to be set for a remote backend).

Do not run any other commands.
