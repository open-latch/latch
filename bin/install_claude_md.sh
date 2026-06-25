#!/usr/bin/env bash
#
# install_claude_md.sh — thin wrapper. Sync latch's CLAUDE.md engine-contract
# region into a project's CLAUDE.md from claude_md_snippet.md (the single source
# of truth). All logic lives in src/claude_md_sync.py so this CLI, the
# SessionStart hook auto-sync, and the drift gate share ONE implementation.
#
# Usage:
#   bash install_claude_md.sh [TARGET_CLAUDE_MD]   # default ./CLAUDE.md ; sync region
#   bash install_claude_md.sh --check [TARGET]     # verify only; exit 1 on missing/drift
#
# Non-destructive: backs up the target to <name>.latchbak, only rewrites the
# managed region, never deletes the file. Idempotent.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
# Interpreter resolution (claude_md_sync.py is py3; a bare `python` may be py2):
#   $LATCH_PYTHON -> $CLAUDE_KB_PYTHON -> python3 -> python
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "install_claude_md: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi
exec "$PY" "${KB_HOME}/src/claude_md_sync.py" "$@"
