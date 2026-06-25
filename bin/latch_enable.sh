#!/usr/bin/env bash
#
# latch_enable.sh — undo latch_disable.sh by removing the DISABLE sentinel, so
# latch's hooks + compactor resume on the next prompt.
#
# By default this removes ONLY the full-stop DISABLE file and leaves
# DISABLE_WRITE alone (it is a deliberate finer-grained control — e.g. a machine
# that intentionally keeps the write-side hooks off). Pass --all to also remove
# DISABLE_WRITE and return to fully-default behavior.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

remove_all=false
[ "${1:-}" = "--all" ] && remove_all=true

removed=false
if [ -e "${KB_HOME}/DISABLE" ]; then
  rm -f "${KB_HOME}/DISABLE"
  echo "removed ${KB_HOME}/DISABLE"
  removed=true
fi

if $remove_all && [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  rm -f "${KB_HOME}/DISABLE_WRITE"
  echo "removed ${KB_HOME}/DISABLE_WRITE"
  removed=true
elif [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  echo "note: ${KB_HOME}/DISABLE_WRITE still present — write-side hooks "
  echo "      (Stop/SessionEnd/compactor) stay OFF. Remove with: bash bin/latch_enable.sh --all"
fi

if $removed; then
  echo "latch ENABLED — hooks resume on the next prompt."
else
  echo "latch was not disabled (no DISABLE file). Nothing to do."
fi
