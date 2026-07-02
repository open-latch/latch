#!/usr/bin/env bash
# Wrapper for /latch-compact slash command.
# Compacts the INVOKING session: resolves the session id from the first
# positional arg, else ${CLAUDE_CODE_SESSION_ID} (set by Claude Code on the
# Bash tool subprocess), and selects that session's transcript explicitly.
# Only when neither is available does it fall back to the legacy
# newest-mtime heuristic — which can pick a CONCURRENT session's transcript
# (KB id=1523) — and it warns loudly when it does.
#
# Usage: run_compact_now.sh [session_id]
#
# Allow in user-scope ~/.claude/settings.json so /latch-compact runs unattended:
#   "Bash(bash ${LATCH_HOME}/bin/run_compact_now.sh)"
# Existing settings that use ${CLAUDE_KB_HOME} still work as the legacy alias.
#
# Manual /latch-compact is a rolling compact (no --final), session_summary stays
# in `staging` status. See src/compactor.py.

set -euo pipefail

# Resolve install root: ${LATCH_HOME} wins, then legacy ${CLAUDE_KB_HOME}, else
# infer from script location.
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

PROJECTS_DIR="${HOME}/.claude/projects"

SESSION_ID="${1:-${CLAUDE_CODE_SESSION_ID:-}}"
if [ -n "${SESSION_ID}" ]; then
  # Session ids are UUIDs, unique across projects — glob by id, not mtime.
  TRANSCRIPT=$(ls -t "${PROJECTS_DIR}"/*/"${SESSION_ID}.jsonl" 2>/dev/null | head -1 || true)
  if [ -z "${TRANSCRIPT}" ]; then
    echo "latch-compact: no transcript found for session ${SESSION_ID} under ${PROJECTS_DIR}" >&2
    exit 1
  fi
else
  TRANSCRIPT=$(ls -t "${PROJECTS_DIR}"/*/*.jsonl 2>/dev/null | head -1 || true)
  if [ -z "${TRANSCRIPT}" ]; then
    echo "latch-compact: no session transcripts found under ${PROJECTS_DIR}" >&2
    exit 1
  fi
  SESSION_ID=$(basename "${TRANSCRIPT}" .jsonl)
  echo "latch-compact: WARNING — no session id given (arg or CLAUDE_CODE_SESSION_ID);" >&2
  echo "latch-compact: falling back to newest-mtime transcript (session ${SESSION_ID})." >&2
  echo "latch-compact: if another Claude session is running, this may compact the WRONG session." >&2
fi
# pwd -W gives Windows-style path on Git Bash (compactor expects this);
# falls back to plain pwd on POSIX shells where -W is unsupported.
PROJECT_DIR=$(pwd -W 2>/dev/null || pwd)

# Resolve the interpreter the SAME way src/install_engine.py:resolve_python and
# the hooks do — prefer the repo venv (where latch's deps live). A bare `python`
# does not exist on macOS, and PATH python3 there is often the Rosetta/system
# interpreter that lacks numpy. The .venv checks fall through cleanly on installs
# that use a system/shared Python (no .venv present). See KB id=1467.
#   $LATCH_PYTHON > $CLAUDE_KB_PYTHON > $KB_HOME/.venv > python3 > python
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
  echo "latch-compact: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi

exec "${PY}" "${KB_HOME}/src/compactor.py" \
  "${SESSION_ID}" "${PROJECT_DIR}" "${TRANSCRIPT}"
