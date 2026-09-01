#!/usr/bin/env bash
# Explicit offline wrapper for one canonical outcome-measurement audit.
set -euo pipefail

LATCH_ROOT="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  PYTHON_BIN="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PYTHON_BIN="$CLAUDE_KB_PYTHON"
elif [ -x "${LATCH_ROOT}/.venv/bin/python" ]; then
  PYTHON_BIN="${LATCH_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  PYTHON_BIN="python"
fi

exec "$PYTHON_BIN" "${LATCH_ROOT}/src/latch/evals/outcome_measurement_cli.py" "$@"
