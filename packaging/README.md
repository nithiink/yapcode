# Packaging — Homebrew distribution

Homebrew formula and release tooling. **Live** since `v0.1.0`: the tap is
[`nithiink/homebrew-yapcode`](https://github.com/nithiink/homebrew-yapcode), and users install
with `brew tap nithiink/yapcode && brew install yapcode`.

## Files

- **`yapcode.rb`** — the formula (source-of-truth copy; mirrored to the tap). Declares the
  `node` / `python@3.12` / `tmux` dependencies, builds the Python venv and the
  Next.js production bundle at install time, and installs a `yapcode`
  launcher on `PATH`. The launcher redirects the backend's runtime writes
  (session store, cost/debug logs) into `~/.local/state/yapcode` so nothing
  is written into the read-only Cellar.
- **`release.sh`** — tags a release, computes the tarball `sha256`, and stamps
  `url` + `sha256` into `yapcode.rb`.

## Going-live sequence (after the repo is public)

1. **Scrub first** (separate from packaging): purge committed logs
   (`cost-log.jsonl`, `debug-log.jsonl`) from history, confirm no secrets in
   history, tighten `.gitignore`.
2. **Make the repo public.**
3. **Cut a release + stamp the formula:**
   ```bash
   packaging/release.sh v0.1.0
   ```
4. **Create the tap repo** `github.com/nithiink/homebrew-yapcode` (public),
   copy the stamped `yapcode.rb` to `Formula/yapcode.rb`, and push.
5. **Users install:**
   ```bash
   brew tap nithiink/yapcode
   brew install yapcode
   yapcode up
   ```

## Updating later

Bump the version, re-run `packaging/release.sh vX.Y.Z`, and copy the stamped
formula into the tap. Users upgrade with `brew upgrade yapcode`.

## Prerequisites the formula can't remove

`brew install` handles the system dependencies, but every user still needs
**Claude Code installed and logged in** and a **voice-provider key** — the
formula's `caveats` block tells them so, and the first `yapcode up` runs the
setup wizard.
