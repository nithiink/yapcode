# Daily transcript fixer

A local, scheduled pipeline that reads each day's voice-claude Claude Code session
transcripts, has a headless `claude -p` agent analyze them and **auto-implement fixes**
on a fresh branch, opens a GitHub PR, and posts a summary to Slack.

## Why local (not a cloud routine)

The transcripts live only on this machine at `~/.claude/projects/<encoded-cwd>/<id>.jsonl`.
Cloud routines (`/schedule`) run on Anthropic infra against a fresh clone and **cannot read
local files**, so they can't see the transcripts. A local `launchd` job driving `claude -p`
is the only option that is both unattended and able to read the source data.

## Pipeline

```
launchd (daily 09:00, via caffeinate)
   └─ run.sh
        1. find voice-claude *.jsonl modified in last 24h
        2. branch auto/transcript-fixes-YYYY-MM-DD off origin/main
        3. claude -p  → analyze + edit code + git commit   (--permission-mode acceptEdits)
        4. git push + gh pr create
        5. Slack chat.postMessage with the summary
```

## Setup

1. **Secrets**
   ```sh
   cp .env.example .env
   # edit .env: SLACK_BOT_TOKEN (xoxb-...), SLACK_CHANNEL
   ```
   The Slack app needs the `chat:write` scope and must be invited to the channel.

2. **Auth check** — these must already be logged in as your user:
   - `claude` (Claude Code credentials in `~/.claude`)
   - `gh auth status` (GitHub CLI, push rights to `nithiink/yapcode`)

3. **Dry run first** (no edits, no PR — just analyze + report + Slack):
   ```sh
   ./run.sh --dry-run
   ```
   Inspect `logs/report-*.md` and the Slack message before trusting auto-fix.

4. **Full run once, by hand**, to confirm PR creation:
   ```sh
   ./run.sh
   ```

5. **Schedule it** (macOS launchd):
   ```sh
   cp com.nithiin.voice-claude-transcript-fixer.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.nithiin.voice-claude-transcript-fixer.plist
   # run immediately to test the scheduled path:
   launchctl start com.nithiin.voice-claude-transcript-fixer
   ```
   To change the time, edit `StartCalendarInterval` in the plist, then unload + load again.
   To stop scheduling:
   ```sh
   launchctl unload ~/Library/LaunchAgents/com.nithiin.voice-claude-transcript-fixer.plist
   ```

## Knobs (set in `.env`)

| Var | Default | Meaning |
|-----|---------|---------|
| `LOOKBACK_MIN` | `1440` | how far back to scan transcripts (minutes) |
| `MAX_TURNS` | `60` | safety cap on the agent's iterations |
| `BASE_BRANCH` | `main` | PR base |
| `CLAUDE_MODEL` | account default | e.g. `claude-opus-4-8` |
| `SESSION_GLOB` | `*Development-Assistant-voice-claude*` | which project dirs count as voice-claude |

## Safety notes

- Auto-fix is **on**. The agent only has `Read, Grep, Glob, Edit, Write, Bash(git:*)` — it can
  edit code and commit, but cannot run arbitrary shell commands or push (the script pushes).
- Everything lands on a dated branch behind a **PR** — nothing is auto-merged to `main`. Review
  the PR before merging.
- `--dry-run` flips off all code edits and PR creation for safe testing.
- Logs and the per-run report are in `logs/` (gitignored). Start with `--dry-run` for a week
  if you want to gauge fix quality before letting it open real PRs.
```
