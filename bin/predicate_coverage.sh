#!/usr/bin/env bash
# Aggregate-only coverage for typed rejected_path predicates.
# Usage: bin/predicate_coverage.sh /path/to/vault.sqlite3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve the interpreter like the other wrappers: a bare python3 is absent
# under Git Bash on Windows and often lacks latch's dependencies on macOS.
#   $LATCH_PYTHON > $CLAUDE_KB_PYTHON > checkout .venv > python3 > python
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif [ -x "${SCRIPT_DIR}/../.venv/bin/python" ]; then
  PY="${SCRIPT_DIR}/../.venv/bin/python"
elif [ -x "${SCRIPT_DIR}/../.venv/Scripts/python.exe" ]; then
  PY="${SCRIPT_DIR}/../.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "predicate_coverage: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi

exec "$PY" "${SCRIPT_DIR}/../src/latch/gate/predicate_coverage.py" "$@"
