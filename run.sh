#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run.sh — one-command launcher for the Pokémon TCG Tracker (macOS / Linux).
#
#   ./run.sh              # set up (if needed) and start the app
#   PORT=8600 ./run.sh    # start on a custom port (default 8501)
#
# It creates a local virtualenv in .venv, installs requirements the first time
# (or whenever requirements.txt changes), then launches Streamlit.
# ---------------------------------------------------------------------------
set -euo pipefail

# Always run from the script's own directory so it works from anywhere.
cd "$(dirname "$0")"

VENV_DIR=".venv"
PORT="${PORT:-8501}"
STAMP="$VENV_DIR/.deps-installed"

# Pick a Python 3 interpreter.
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "❌ Python 3 not found. Install it from https://www.python.org/ and retry." >&2
  exit 1
fi

# 1. Create the virtualenv the first time.
if [ ! -d "$VENV_DIR" ]; then
  echo "📦 Creating virtualenv in $VENV_DIR ..."
  "$PY" -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# 2. Install/refresh dependencies only when requirements.txt is newer than the
#    last successful install (keeps subsequent startups fast).
if [ ! -f "$STAMP" ] || [ requirements.txt -nt "$STAMP" ]; then
  echo "📥 Installing dependencies ..."
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt
  touch "$STAMP"
fi

# 3. Launch the app.
echo "🎴 Starting Pokémon TCG Tracker on http://localhost:$PORT (Ctrl+C to stop) ..."
exec streamlit run app.py --server.port "$PORT"
