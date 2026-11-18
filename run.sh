#!/usr/bin/env bash
# Start DocketScan. Usage: ./run.sh [port]
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-8000}"
if [ ! -d .venv ]; then
  echo "Creating venv + installing deps (first run only)…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi
echo "DocketScan → http://localhost:${PORT}"
exec ./.venv/bin/uvicorn app:app --host 0.0.0.0 --port "${PORT}"
