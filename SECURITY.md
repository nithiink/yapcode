# Security Policy

yapcode's backend turns voice into **real command execution on your machine**, so security
reports get top priority here.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting instead: **[Report a vulnerability](https://github.com/nithiink/yapcode/security/advisories/new)**
(Security tab → "Report a vulnerability"). This is a solo-maintained project — I read every
report and prioritize confirmed vulnerabilities above all other work, but I'm not promising
formal response SLAs.

## Scope — what counts

The interesting attack surface, in rough order of severity:

1. **Auth bypass** — reaching session-control endpoints or the terminal WebSocket without the
   `VC_AUTH_TOKEN` when one is required (or from a non-loopback caller when one isn't set).
2. **Directory-sandbox escape** — starting a session, or steering an existing one, outside
   `ALLOWED_PROJECT_ROOTS` (path traversal, symlink tricks, fuzzy-match abuse).
3. **CSRF / drive-by** — a malicious web page driving the backend through the Next `/api/*`
   proxy or the WebSocket from a victim's browser.
4. **Token leakage** — the auth token or provider keys reaching logs, the JS bundle, URLs that
   persist, or any client that shouldn't hold them.
5. **Injection into the tmux/Claude pipeline** — crafted input that escapes the session and
   executes outside the intended `claude` process.

The two-layer model these defend (auth + sandbox, both fail-closed) is documented in the
[README's security section](README.md#security-model-read-this-first).

## Out of scope

- Anything requiring an already-compromised machine or an attacker who legitimately holds the
  `VC_AUTH_TOKEN`
- The behavior of Claude Code itself (report to [Anthropic](https://www.anthropic.com/responsible-disclosure-policy))
  or of the voice providers
- Self-inflicted configurations the docs explicitly warn against (e.g. exposing network mode
  without TLS/token, setting `ALLOWED_PROJECT_ROOTS=/`)

## Supported versions

The latest release / `main` branch. There are no security backports to older tags.

Thanks for looking — reports that include a repro get attention fastest.
