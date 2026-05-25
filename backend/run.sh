#!/usr/bin/env bash
# Backend over plain HTTP for local laptop use. Pair with `npm run dev` in
# ../frontend (http://localhost:3000 — mic works there via the localhost secure-
# context exception, and ws:// is not mixed content).
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -m uvicorn main:app --port 8000 --log-level info
