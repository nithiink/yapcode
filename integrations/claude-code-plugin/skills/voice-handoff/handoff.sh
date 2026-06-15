#!/usr/bin/env bash
# Registers the current Claude Code session with the local yapcode backend.
# Invoked by the /voice-handoff skill as: handoff.sh <claude-session-id>
#
# Lives in a script (rather than inline in SKILL.md) because Claude Code's
# shell-permission checker rejects inline commands that put quotes inside
# ${...} expansions ("expansion obfuscation") — which the token header needs.
# Always exits 0 and prints JSON; failures are reported in an "error" field.
set -u

url="${YAPCODE_URL:-http://localhost:8000}"
sid="${1:-}"

if [ -z "$sid" ]; then
  echo '{"error":"missing session id — expected: handoff.sh <claude-session-id>"}'
  exit 0
fi

args=(-s -X POST "$url/session/handoff" -H "Content-Type: application/json")
if [ -n "${YAPCODE_TOKEN:-}" ]; then
  args+=(-H "X-VC-Token: ${YAPCODE_TOKEN}")
fi

# Escape a string for safe inclusion in a JSON value (backslash and quote are the
# only characters a session-id/path/$TMUX realistically contains that break JSON).
json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '%s' "$s"
}

payload="$(printf '{"session_id":"%s","cwd":"%s","tmux":"%s"}' \
  "$(json_escape "$sid")" "$(json_escape "$(pwd)")" "$(json_escape "${TMUX:-}")")"

curl "${args[@]}" -d "$payload" ||
  printf '{"error":"could not reach yapcode — is the backend running at %s?"}\n' "$url"
exit 0
