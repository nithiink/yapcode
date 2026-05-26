#!/usr/bin/env bash
# Backend over TLS (https + wss) for LAN/phone use. Binds all interfaces so the
# phone can reach it, and serves wss so the live terminal works from the HTTPS
# frontend. Pair with `npm run dev:network` in ../frontend.
# One-time per device: open https://<host>:8000 in the browser and accept the
# self-signed cert, otherwise the wss terminal connection is silently blocked.
set -euo pipefail
cd "$(dirname "$0")"
# --reload: hot-reload on backend code changes. With detach-on-shutdown the
# reload preserves running CLI sessions (they're rehydrated on the restart).
# --reload-dir . limits the watcher to backend/ so the session store under the
# project root (rapidly-written events.jsonl) never triggers reloads.
exec .venv/bin/python -m uvicorn main:app \
  --host 0.0.0.0 --port 8000 \
  --ssl-keyfile ../frontend/.certs/dev-key.pem \
  --ssl-certfile ../frontend/.certs/dev-cert.pem \
  --log-level info \
  --reload --reload-dir .
