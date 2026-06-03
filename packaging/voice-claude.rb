# voice-claude Homebrew formula (DRAFT — staged for when the repo goes public).
#
# This is the source-of-truth copy. To publish: run packaging/release.sh vX.Y.Z
# (it tags a release, computes the sha256, and stamps `url`/`sha256` below), then
# copy this file into the tap repo (github.com/nithiink/homebrew-voice-claude) at
# Formula/voice-claude.rb and push. See packaging/README.md.
#
# It will NOT install while the repo is private — `brew` fetches the source
# tarball over anonymous HTTPS, which 404s on a private repo. Publish only after
# the repo is public and a release tag exists.
class VoiceClaude < Formula
  desc "Voice front-end for Claude Code — talk to drive real Claude Code sessions"
  homepage "https://github.com/nithiink/voice-claude"
  url "https://github.com/nithiink/voice-claude/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000" # PLACEHOLDER — release.sh stamps this
  license "MIT"

  depends_on "node"
  depends_on "python@3.12"
  depends_on "tmux"

  def install
    # Full source tree -> Cellar/voice-claude/<ver>/libexec
    libexec.install Dir["*"]

    # Python backend virtualenv (built once, here, not at runtime)
    python = Formula["python@3.12"].opt_bin/"python3.12"
    system python, "-m", "venv", libexec/"backend/.venv"
    system libexec/"backend/.venv/bin/pip", "install", "--no-input",
           "-r", libexec/"backend/requirements.txt"

    # Frontend production build (so the launcher runs `next start`, no compile-on-launch)
    cd libexec/"frontend" do
      system "npm", "ci"
      system "npm", "run", "build"
    end

    # Launcher wrapper on PATH. It (1) pins the install tree via VOICE_CLAUDE_ROOT
    # and (2) redirects the backend's runtime writes (session store + logs) into
    # the user's state dir, since the Cellar must be treated as read-only. Each
    # `:=` respects a value the user already set, so overrides still win.
    (bin/"voice-claude").write <<~SH
      #!/bin/bash
      export VOICE_CLAUDE_ROOT="#{libexec}"
      state="${XDG_STATE_HOME:-$HOME/.local/state}/voice-claude"
      mkdir -p "$state/tmux"
      : "${VC_SESSION_STORE:=$state/tmux}";             export VC_SESSION_STORE
      : "${VC_COST_LOG_PATH:=$state/cost-log.jsonl}";   export VC_COST_LOG_PATH
      : "${VC_DEBUG_LOG_PATH:=$state/debug-log.jsonl}"; export VC_DEBUG_LOG_PATH
      exec "#{libexec}/bin/voice-claude" "$@"
    SH
    chmod 0755, bin/"voice-claude"
  end

  def caveats
    <<~EOS
      voice-claude is a front-end for Claude Code — it needs Claude Code
      installed and logged in (uses your existing subscription, no API key):
        https://claude.com/claude-code   (run `claude` once and sign in)

      First launch runs a short setup wizard (voice provider, key, allowed
      folders) and writes ~/.config/voice-claude/.env. Then:

        voice-claude up        # start the app and open it in your browser
        voice-claude config    # edit settings later

      Config (~/.config/voice-claude) and runtime state
      (~/.local/state/voice-claude) live outside the Cellar and survive
      upgrades and uninstall.
    EOS
  end

  test do
    # Unknown subcommand prints usage and exits 2 — exercises the launcher
    # without needing Claude Code, a voice key, or any prompt.
    output = shell_output("#{bin}/voice-claude bogus-subcommand 2>&1", 2)
    assert_match "usage: voice-claude", output
  end
end
