#!/usr/bin/env bash
#
# latch_status.sh — report whether latch's kill switch is engaged. A quick
# "is it off right now?" check for when behavior looks unexpected.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

echo "latch status (KB_HOME=${KB_HOME})"

if [ -n "${LATCH_DISABLE:-}" ]; then
  echo "  [DISABLED] \$LATCH_DISABLE is set in this environment — all hooks + compactor no-op."
elif [ -n "${CLAUDE_KB_DISABLE:-}" ]; then
  echo "  [DISABLED] legacy \$CLAUDE_KB_DISABLE is set in this environment — all hooks + compactor no-op."
elif [ -e "${KB_HOME}/DISABLE" ]; then
  echo "  [DISABLED] ${KB_HOME}/DISABLE exists — all hooks + compactor no-op."
  echo "             resume: bash bin/latch_enable.sh"
else
  echo "  [ENABLED ] no DISABLE sentinel / env var — hooks active."
fi

if [ -n "${LATCH_DISABLE_WRITE:-}" ]; then
  echo "  [write-off] \$LATCH_DISABLE_WRITE is set — Stop/SessionEnd/compactor no-op; reads live."
elif [ -n "${CLAUDE_KB_DISABLE_WRITE:-}" ]; then
  echo "  [write-off] legacy \$CLAUDE_KB_DISABLE_WRITE is set — Stop/SessionEnd/compactor no-op; reads live."
elif [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  echo "  [write-off] ${KB_HOME}/DISABLE_WRITE exists — Stop/SessionEnd/compactor no-op; reads live."
fi
