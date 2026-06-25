#!/usr/bin/env bash
# Codex preview wiring verifier. Checks Codex config.toml, AGENTS.md, the MCP
# command target, and Codex compact transcript resolution.

set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="${LATCH_PYTHON}"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="${CLAUDE_KB_PYTHON}"
elif [ -x "${KB_HOME}/.venv/bin/python" ]; then
  PY="${KB_HOME}/.venv/bin/python"
elif [ -x "${KB_HOME}/.venv/Scripts/python.exe" ]; then
  PY="${KB_HOME}/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "latch Codex doctor: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi

exec "${PY}" "${KB_HOME}/src/codex_doctor.py" "$@"
