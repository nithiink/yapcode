#!/usr/bin/env bash
# Backend over TLS (https + wss) for LAN/phone use. Binds all interfaces so the
# phone can reach it, and serves wss so the live terminal works from the HTTPS
# frontend. Pair with `npm run dev:network` in ../frontend.
# One-time per device: open https://<host>:8000 in the browser and accept the
# self-signed cert, otherwise the wss terminal connection is silently blocked.
#
# SECURITY: this binds 0.0.0.0, so the backend is reachable from every device on
# the LAN. You MUST set VC_AUTH_TOKEN in .env first (see .env.example) — without
# it the backend refuses all non-loopback requests. Open the app on your devices
# once as  https://<host>:3000/#vc_token=<the token>  to register the secret.
set -euo pipefail
cd "$(dirname "$0")"

# Fail closed with a clear message: binding 0.0.0.0 without VC_AUTH_TOKEN leaves
# the backend refusing every remote request (loopback-only), i.e. a non-functional
# LAN app. Require the secret to be configured before exposing the port.
if [ -z "${VC_AUTH_TOKEN:-}" ] && ! grep -qE '^[[:space:]]*VC_AUTH_TOKEN=[^[:space:]]+' .env 2>/dev/null; then
  echo "ERROR: run-network.sh binds 0.0.0.0 but VC_AUTH_TOKEN is not set." >&2
  echo "Set VC_AUTH_TOKEN in backend/.env (see .env.example) so remote/phone" >&2
  echo "requests can authenticate; otherwise all non-loopback requests are refused." >&2
  exit 1
fi

# --reload: hot-reload on backend code changes. With detach-on-shutdown the
# reload preserves running CLI sessions (they're rehydrated on the restart).
# --reload-dir . limits the watcher to backend/ so the session store under the
# project root (rapidly-written events.jsonl) never triggers reloads.
# --timeout-graceful-shutdown: long-lived SSE/poll/terminal-WS connections
# never drain on their own, so a reload (or stop) would hang on "Waiting for
# connections to close". 3s lets an in-flight request finish, then force-closes.
exec .venv/bin/python -m uvicorn main:app \
  --host 0.0.0.0 --port 8000 \
  --ssl-keyfile ../frontend/.certs/dev-key.pem \
  --ssl-certfile ../frontend/.certs/dev-cert.pem \
  --log-level info \
  --reload --reload-dir . --timeout-graceful-shutdown 3
