#!/usr/bin/env bash
# Launch the Kelp One-Pager web UI. Run this in YOUR OWN terminal so the
# server's lifetime is owned by you, not by any external tool session.
#
#   ./run.sh            # serves at http://localhost:8000
#   PORT=8080 ./run.sh  # pick a different port
#
# Leave the terminal open while you use the site; Ctrl+C stops the server.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
exec python3 -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
