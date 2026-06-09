#!/usr/bin/env bash
# Backend over plain HTTP for local laptop use. Pair with `npm run dev` in
# ../frontend (http://localhost:3000 — mic works there via the localhost secure-
# context exception, and ws:// is not mixed content).
set -euo pipefail
cd "$(dirname "$0")"
# --reload: hot-reload on backend code changes. With detach-on-shutdown the
# reload preserves running CLI sessions (they're rehydrated on the restart).
# --reload-dir . limits the watcher to backend/ so the session store under the
# project root (rapidly-written events.jsonl) never triggers reloads.
# --timeout-graceful-shutdown: the app holds long-lived connections (the SSE
# debug/event stream, the poll loop, the xterm terminal WebSockets) that never
# drain on their own, so a reload would otherwise hang on "Waiting for
# connections to close" and the new worker can't bind :8000. 3s lets an
# in-flight request finish, then force-closes so the reload completes.
exec .venv/bin/python -m uvicorn main:app --port 8000 --log-level info \
  --reload --reload-dir . --timeout-graceful-shutdown 3
