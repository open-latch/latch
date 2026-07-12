#!/usr/bin/env bash
set -euo pipefail
LATCH_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
PY="${LATCH_PYTHON:-${CLAUDE_KB_PYTHON:-}}"
if [[ -z "$PY" ]]; then
  if [[ -x "$LATCH_HOME/.venv/bin/python" ]]; then PY="$LATCH_HOME/.venv/bin/python"
  elif [[ -x "$LATCH_HOME/.venv/Scripts/python.exe" ]]; then PY="$LATCH_HOME/.venv/Scripts/python.exe"
  else PY="$(command -v python3 || command -v python || true)"
  fi
fi
if [[ -z "$PY" ]]; then
  echo "latch_version: no Python found (set LATCH_PYTHON)." >&2
  exit 1
fi
exec "$PY" "$LATCH_HOME/src/versioning.py" "$@"
