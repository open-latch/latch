#!/usr/bin/env bash
# Build or verify the public latch proof packet.
set -euo pipefail
LATCH_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif [ -x "${LATCH_HOME}/.venv/bin/python" ]; then
  PY="${LATCH_HOME}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "latch_proof_packet: no Python found (set LATCH_PYTHON)." >&2
  exit 2
fi
exec "$PY" "${LATCH_HOME}/src/proof_packet.py" "$@"
