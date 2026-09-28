#!/usr/bin/env bash
# spotpl - one Spotify link in, one ZIP out.
# macOS / Linux: run ./start.sh (first run sets everything up).
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || {
  echo "Python 3.10+ is required. Install it, then run this script again."
  exit 1
}

if [ ! -d .venv ]; then
  echo "[1/3] Creating environment - this happens once..."
  "$PY" -m venv .venv
fi

echo "[2/3] Installing spotdl - this happens once..."
./.venv/bin/python -m pip install --upgrade pip -q
./.venv/bin/python -m pip install -r requirements.txt -q

echo "[3/3] Starting spotpl - your browser will open..."
echo
exec ./.venv/bin/python spotpl.py "$@"
