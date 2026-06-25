#!/usr/bin/env bash
# Wrapper for /kb-gate slash command.
# Usage:
#   bash run_kb_gate.sh "<request>"
#
# Allow in user-scope ~/.claude/settings.json so /kb-gate runs unattended:
#   "Bash(bash ${LATCH_HOME}/bin/run_kb_gate.sh:*)"
# Existing settings that use ${CLAUDE_KB_HOME} still work as the legacy alias.

set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif [ -x "${KB_HOME}/.venv/bin/python" ]; then
  PY="${KB_HOME}/.venv/bin/python"
elif [ -x "${KB_HOME}/.venv/Scripts/python.exe" ]; then
  PY="${KB_HOME}/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "kb-gate: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi

PROJECT_DIR=$(pwd -W 2>/dev/null || pwd)

exec "$PY" "${KB_HOME}/src/kb_gate_cli.py" \
  "${PROJECT_DIR}" "$@"
