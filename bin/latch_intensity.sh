#!/usr/bin/env bash
set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
PYTHON="${LATCH_PYTHON:-${CLAUDE_KB_PYTHON:-}}"
if [ -z "$PYTHON" ]; then
  if [ -x "$KB_HOME/.venv/bin/python" ]; then
    PYTHON="$KB_HOME/.venv/bin/python"
  elif [ -x "$KB_HOME/.venv/Scripts/python.exe" ]; then
    PYTHON="$KB_HOME/.venv/Scripts/python.exe"
  else
    PYTHON="python3"
  fi
fi
exec "$PYTHON" "$KB_HOME/src/intensity_cli.py" "$@"
