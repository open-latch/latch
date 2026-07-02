#!/usr/bin/env bash
#
# latch_public_release_check.sh — scan tracked source for public-release leaks.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif [ -x "${KB_HOME}/.venv/bin/python" ]; then
  PY="${KB_HOME}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "latch_public_release_check: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi
exec "$PY" "${KB_HOME}/src/public_release_check.py" "$@"
