#!/usr/bin/env bash
# Wrapper for manual Codex KB compaction.
# Resolves the Codex session from the first arg or $CODEX_THREAD_ID, then lets
# src/codex_compact.py find and validate the matching ~/.codex rollout JSONL.
# This path is fail-closed: it never falls back to ~/.claude/projects.
# By default it uses Codex's own `codex exec` summarizer backend; pass
# --summarizer claude to exercise the legacy shared Claude CLI backend.
# Pass --background to validate the transcript and detach the compactor.

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
  echo "codex-kb-compact: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi

exec "${PY}" "${KB_HOME}/src/codex_compact.py" "$@"
