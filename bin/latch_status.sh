#!/usr/bin/env bash
#
# latch_status.sh — report whether latch's kill switch is engaged. A quick
# "is it off right now?" check for when behavior looks unexpected.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

echo "latch status (KB_HOME=${KB_HOME})"

if [ -n "${LATCH_UNLATCHED:-}" ]; then
  echo "  [UNLATCHED] \$LATCH_UNLATCHED is set - latch influence is OFF for vanilla-agent mode."
  echo "             disabled: prompt KB injection, compaction, self-heal, maintenance."
  echo "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  echo "             resume: unset LATCH_UNLATCHED, then run /latch"
elif [ -e "${KB_HOME}/UNLATCHED" ]; then
  echo "  [UNLATCHED-GLOBAL] legacy ${KB_HOME}/UNLATCHED exists - every project is OFF."
  echo "             disabled: prompt KB injection, compaction, self-heal, maintenance."
  echo "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  echo "             resume: bash bin/latch_enable.sh"
elif [ -n "${LATCH_DISABLE:-}" ]; then
  echo "  [DISABLED] \$LATCH_DISABLE is set in this environment — all hooks + compactor no-op."
elif [ -n "${CLAUDE_KB_DISABLE:-}" ]; then
  echo "  [DISABLED] legacy \$CLAUDE_KB_DISABLE is set in this environment — all hooks + compactor no-op."
elif [ -e "${KB_HOME}/DISABLE" ]; then
  echo "  [DISABLED] ${KB_HOME}/DISABLE exists — all hooks + compactor no-op."
  echo "             resume: bash bin/latch_enable.sh"
else
  echo "  [GLOBAL-CLEAR] no install-wide UNLATCHED/DISABLE switch is active."
fi

if [ -n "${LATCH_PYTHON:-}" ]; then
  status_python="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  status_python="$CLAUDE_KB_PYTHON"
elif [ -x "${KB_HOME}/.venv/bin/python" ]; then
  status_python="${KB_HOME}/.venv/bin/python"
elif [ -x "${KB_HOME}/.venv/Scripts/python.exe" ]; then
  status_python="${KB_HOME}/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  status_python="python3"
elif command -v python >/dev/null 2>&1; then
  status_python="python"
else
  status_python=""
fi
if [ -n "$status_python" ]; then
  echo
  "$status_python" "${KB_HOME}/src/project_mode.py" status --project "$(pwd -W 2>/dev/null || pwd)"
else
  echo
  echo "  [UNKNOWN] project mode unavailable because Python was not found; no state changed."
fi

if [ -n "${LATCH_DISABLE_WRITE:-}" ]; then
  echo "  [write-off] \$LATCH_DISABLE_WRITE is set — Stop/SessionEnd/compactor no-op; reads live."
elif [ -n "${CLAUDE_KB_DISABLE_WRITE:-}" ]; then
  echo "  [write-off] legacy \$CLAUDE_KB_DISABLE_WRITE is set — Stop/SessionEnd/compactor no-op; reads live."
elif [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  echo "  [write-off] ${KB_HOME}/DISABLE_WRITE exists — Stop/SessionEnd/compactor no-op; reads live."
fi
