#!/usr/bin/env bash
#
# latch_status.sh — report whether latch's kill switch is engaged. A quick
# "is it off right now?" check for when behavior looks unexpected.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

echo "latch status (KB_HOME=${KB_HOME})"

intensity_rc=0
intensity_output="$(bash "${KB_HOME}/bin/latch_intensity.sh" 2>&1)" || intensity_rc=$?
if [ -n "$intensity_output" ]; then
  while IFS= read -r line; do
    echo "  $line"
  done <<< "$intensity_output"
else
  echo "  Latch intensity: unavailable (could not run bin/latch_intensity.sh)"
fi
if [ "$intensity_rc" -gt 1 ]; then
  echo "  warning: intensity status command exited ${intensity_rc}; kill-switch status follows"
fi

if [ -n "${LATCH_UNLATCHED:-}" ]; then
  echo "  [UNLATCHED] \$LATCH_UNLATCHED is set - latch influence is OFF for vanilla-agent mode."
  echo "             disabled: session briefs, prompt KB injection, compaction, self-heal, maintenance."
  echo "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  echo "             resume: unset LATCH_UNLATCHED, then run /unlatch"
elif [ -e "${KB_HOME}/UNLATCHED" ]; then
  echo "  [UNLATCHED] ${KB_HOME}/UNLATCHED exists - latch influence is OFF for vanilla-agent mode."
  echo "             disabled: session briefs, prompt KB injection, compaction, self-heal, maintenance."
  echo "             still true: KB files stay local/unchanged; latch remains installed; control commands/MCP registration remain."
  echo "             resume: run /unlatch"
elif [ -n "${LATCH_DISABLE:-}" ]; then
  echo "  [DISABLED] \$LATCH_DISABLE is set in this environment — all hooks + compactor no-op."
elif [ -n "${CLAUDE_KB_DISABLE:-}" ]; then
  echo "  [DISABLED] legacy \$CLAUDE_KB_DISABLE is set in this environment — all hooks + compactor no-op."
elif [ -e "${KB_HOME}/DISABLE" ]; then
  echo "  [DISABLED] ${KB_HOME}/DISABLE exists — all hooks + compactor no-op."
  echo "             resume: bash bin/latch_enable.sh"
else
  echo "  [ENABLED ] no UNLATCHED/DISABLE sentinel or env var - hooks active."
fi

if [ -n "${LATCH_DISABLE_WRITE:-}" ]; then
  echo "  [write-off] \$LATCH_DISABLE_WRITE is set — Stop/SessionEnd/compactor no-op; reads live."
elif [ -n "${CLAUDE_KB_DISABLE_WRITE:-}" ]; then
  echo "  [write-off] legacy \$CLAUDE_KB_DISABLE_WRITE is set — Stop/SessionEnd/compactor no-op; reads live."
elif [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  echo "  [write-off] ${KB_HOME}/DISABLE_WRITE exists — Stop/SessionEnd/compactor no-op; reads live."
fi
