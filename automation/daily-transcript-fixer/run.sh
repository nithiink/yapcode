#!/usr/bin/env bash
#
# Daily transcript fixer for voice-claude.
#
# Once per day (driven by launchd) this:
#   1. Finds voice-claude Claude session transcripts (.jsonl) touched in the last 24h
#   2. Runs `claude -p` headless to analyze them, identify issues, and IMPLEMENT fixes
#      on a fresh dated branch in this worktree
#   3. Opens a GitHub PR with the fixes
#   4. Posts a summary to Slack via chat.postMessage
#
# Usage:
#   ./run.sh              # full run: analyze + auto-fix + PR + Slack
#   ./run.sh --dry-run    # analyze + report + Slack only, NO code edits, NO PR
#
# Config lives in .env (see .env.example). Logs go to ./logs/.

set -euo pipefail

# --- make sure tools are findable under launchd (no shell profile is loaded) ---
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- load config -------------------------------------------------------------
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi

# Defaults (override in .env)
REPO_DIR="${REPO_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"     # the worktree root
BASE_BRANCH="${BASE_BRANCH:-main}"
PROJECTS_DIR="${PROJECTS_DIR:-$HOME/.claude/projects}"
# Only voice-claude sessions under Development/Assistant (excludes the codex worktree dir):
SESSION_GLOB="${SESSION_GLOB:-*Development-Assistant-voice-claude*}"
LOOKBACK_MIN="${LOOKBACK_MIN:-1440}"                         # 24h
MAX_TURNS="${MAX_TURNS:-60}"
CLAUDE_MODEL="${CLAUDE_MODEL:-}"                             # empty = account default
SLACK_CHANNEL="${SLACK_CHANNEL:-}"
SLACK_BOT_TOKEN="${SLACK_BOT_TOKEN:-}"

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

STAMP="$(date +%Y-%m-%d)"
TS="$(date +%Y-%m-%d_%H%M%S)"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"
REPORT_FILE="$LOG_DIR/report-$TS.md"
CLAUDE_JSON="$LOG_DIR/claude-$TS.json"
BRANCH="auto/transcript-fixes-$STAMP"

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# --- slack helper ------------------------------------------------------------
slack() {
  local text="$1"
  if [[ -z "$SLACK_BOT_TOKEN" || -z "$SLACK_CHANNEL" ]]; then
    log "Slack not configured (SLACK_BOT_TOKEN/SLACK_CHANNEL); skipping post."
    return 0
  fi
  local payload
  payload="$(jq -n --arg ch "$SLACK_CHANNEL" --arg txt "$text" '{channel:$ch, text:$txt}')"
  local resp
  resp="$(curl -sS -X POST https://slack.com/api/chat.postMessage \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-type: application/json; charset=utf-8" \
    --data "$payload" || true)"
  if [[ "$(echo "$resp" | jq -r '.ok' 2>/dev/null)" != "true" ]]; then
    log "Slack post failed: $resp"
  fi
}

fail() {
  log "ERROR: $*"
  slack ":x: *Daily transcript fixer failed* ($STAMP)
$*
See log: $LOG_DIR"
  exit 1
}

# --- 1. find recent voice-claude transcripts ---------------------------------
log "Scanning $PROJECTS_DIR for voice-claude sessions modified in the last ${LOOKBACK_MIN}m..."
SESSION_DIRS=()
while IFS= read -r d; do [[ -n "$d" ]] && SESSION_DIRS+=("$d"); done \
  < <(find "$PROJECTS_DIR" -maxdepth 1 -type d -name "$SESSION_GLOB" 2>/dev/null)
if [[ ${#SESSION_DIRS[@]} -eq 0 ]]; then
  fail "No voice-claude project dirs matched '$SESSION_GLOB' under $PROJECTS_DIR"
fi

TRANSCRIPTS=()
while IFS= read -r f; do [[ -n "$f" ]] && TRANSCRIPTS+=("$f"); done \
  < <(find "${SESSION_DIRS[@]}" -maxdepth 1 -name '*.jsonl' -mmin "-$LOOKBACK_MIN" 2>/dev/null | sort)
if [[ ${#TRANSCRIPTS[@]} -eq 0 ]]; then
  log "No transcripts in the last ${LOOKBACK_MIN}m. Nothing to do."
  slack ":information_source: *Daily transcript fixer* ($STAMP): no voice-claude sessions in the last 24h, nothing to analyze."
  exit 0
fi
log "Found ${#TRANSCRIPTS[@]} transcript(s)."
printf '  - %s\n' "${TRANSCRIPTS[@]}"

# --- 2. prepare a fresh branch in the worktree -------------------------------
cd "$REPO_DIR"
log "Repo: $REPO_DIR  (base: $BASE_BRANCH)"
git fetch origin "$BASE_BRANCH" --quiet || log "warn: git fetch failed (offline?)"
git checkout -B "$BRANCH" "origin/$BASE_BRANCH" --quiet 2>/dev/null \
  || git checkout -B "$BRANCH" "$BASE_BRANCH" --quiet
log "On branch $BRANCH"

# --- 3. build the prompt -----------------------------------------------------
TRANSCRIPT_LIST="$(printf '%s\n' "${TRANSCRIPTS[@]}")"

if $DRY_RUN; then
  TASK_INSTRUCTIONS="DO NOT modify any code or run git. Only analyze and write the report."
else
  TASK_INSTRUCTIONS="For each concrete, actionable issue you are confident about, implement the fix in this repo. Make focused commits with clear messages using git (git add + git commit). Do NOT push. If an issue is ambiguous or risky, describe it in the report instead of guessing."
fi

PROMPT="You are an automated daily maintenance agent for the voice-claude project (repo at $REPO_DIR, current branch $BRANCH off $BASE_BRANCH).

These are Claude Code session transcripts (JSONL) from the last 24 hours of work on voice-claude:
$TRANSCRIPT_LIST

Read each transcript. Identify concrete software issues that surfaced during these sessions: bugs that were hit, errors, broken behavior, regressions, TODOs explicitly called out, or problems that were discussed but left unresolved. Ignore chit-chat, resolved items, and anything already fixed in the current code.

$TASK_INSTRUCTIONS

When done, write a markdown report to this exact path: $REPORT_FILE
The report MUST contain these sections:
  # Daily Transcript Fixes — $STAMP
  ## Issues found        (bulleted; each with a one-line description + which transcript/session it came from)
  ## Fixes applied       (what you changed and in which files; 'none' if no commits)
  ## Skipped / needs human review   (issues you chose not to auto-fix and why)
  ## Summary             (2-3 sentence plain-English wrap-up)

If you found no actionable issues, still write the report saying so and make no commits."

# --- 4. run claude headless --------------------------------------------------
ALLOWED='Read,Grep,Glob,Edit,Write,Bash(git:*)'
log "Invoking claude -p (dry-run=$DRY_RUN, max-turns=$MAX_TURNS)..."
MODEL_ARG=()
[[ -n "$CLAUDE_MODEL" ]] && MODEL_ARG=(--model "$CLAUDE_MODEL")

set +e
claude -p "$PROMPT" \
  "${MODEL_ARG[@]}" \
  --add-dir "$PROJECTS_DIR" \
  --add-dir "$LOG_DIR" \
  --allowedTools "$ALLOWED" \
  --permission-mode acceptEdits \
  --max-turns "$MAX_TURNS" \
  --output-format json > "$CLAUDE_JSON" 2>>"$LOG_DIR/claude-stderr-$TS.log"
CLAUDE_RC=$?
set -e
log "claude exited rc=$CLAUDE_RC"
[[ $CLAUDE_RC -ne 0 ]] && log "warn: claude returned non-zero; continuing to collect whatever it produced"

# Fallback report if the agent didn't write one
if [[ ! -f "$REPORT_FILE" ]]; then
  log "warn: no report file written; synthesizing from claude JSON result"
  {
    echo "# Daily Transcript Fixes — $STAMP"
    echo "## Summary"
    jq -r '.result // "No result text returned."' "$CLAUDE_JSON" 2>/dev/null || echo "Could not parse claude output."
  } > "$REPORT_FILE"
fi

# --- 5. PR (skipped on dry-run) ----------------------------------------------
PR_URL=""
COMMITS=0
if ! $DRY_RUN; then
  COMMITS="$(git rev-list --count "origin/$BASE_BRANCH..$BRANCH" 2>/dev/null || git rev-list --count "$BASE_BRANCH..$BRANCH")"
  log "Commits on branch vs base: $COMMITS"
  if [[ "$COMMITS" -gt 0 ]]; then
    log "Pushing $BRANCH and opening PR..."
    git push -u origin "$BRANCH" --quiet || fail "git push failed"
    PR_URL="$(gh pr create \
      --base "$BASE_BRANCH" \
      --head "$BRANCH" \
      --title "Daily transcript fixes — $STAMP" \
      --body-file "$REPORT_FILE" 2>&1)" || fail "gh pr create failed: $PR_URL"
    log "PR: $PR_URL"
  else
    log "No commits produced; no PR."
  fi
fi

# --- 6. Slack report ---------------------------------------------------------
SUMMARY="$(awk '/^## Summary/{f=1;next} /^## /{f=0} f' "$REPORT_FILE" | sed '/^[[:space:]]*$/d')"
[[ -z "$SUMMARY" ]] && SUMMARY="(see report)"

if $DRY_RUN; then
  HEADER=":mag: *Daily transcript fixer — DRY RUN* ($STAMP)"
elif [[ -n "$PR_URL" ]]; then
  HEADER=":white_check_mark: *Daily transcript fixes* ($STAMP) — $COMMITS commit(s)
$PR_URL"
else
  HEADER=":information_source: *Daily transcript fixer* ($STAMP) — no actionable issues, no PR"
fi

slack "$HEADER

$SUMMARY

Analyzed ${#TRANSCRIPTS[@]} session(s). Full report: $REPORT_FILE"

log "Done. Report: $REPORT_FILE"
