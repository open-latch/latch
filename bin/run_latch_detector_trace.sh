#!/usr/bin/env bash
# Local/dev-only read-only detector trace and review command.

set -euo pipefail

LATCH_ROOT="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  LATCH_DETECTOR_PYTHON="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  LATCH_DETECTOR_PYTHON="$CLAUDE_KB_PYTHON"
elif [ -x "${LATCH_ROOT}/.venv/bin/python" ]; then
  LATCH_DETECTOR_PYTHON="${LATCH_ROOT}/.venv/bin/python"
elif [ -x "${LATCH_ROOT}/.venv/Scripts/python.exe" ]; then
  LATCH_DETECTOR_PYTHON="${LATCH_ROOT}/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  LATCH_DETECTOR_PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
  LATCH_DETECTOR_PYTHON="python"
else
  echo "latch-detector-trace: no Python found (set LATCH_PYTHON; legacy: CLAUDE_KB_PYTHON)." >&2
  exit 2
fi

exec "${LATCH_DETECTOR_PYTHON}" "${LATCH_ROOT}/src/detector_trace_cli.py" "$@"
