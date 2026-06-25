#!/usr/bin/env bash
#
# latch_disable.sh — the panic button. Instantly no-op ALL latch hooks + the
# compactor by creating the DISABLE sentinel at the repo root. Checked first
# thing by every hook (`is_disabled()` in src/paths.py) and the compactor, so it
# takes effect on the very next prompt — no Claude Code restart needed.
#
# This is latch-scoped: it stops ONLY latch. Your other Claude Code skills,
# plugins, and hooks keep working (unlike settings.json `disableAllHooks`).
# The MCP server stays registered but is inert unless a tool is explicitly
# called — so "haywire" autonomous behavior (per-prompt injection, compaction,
# heal) is fully halted.
#
# Re-enable with:  bash bin/latch_enable.sh
#
# Flags:
#   --write-only   create DISABLE_WRITE instead of DISABLE — no-ops only the
#                  write-side hooks (Stop / SessionEnd / compactor) while leaving
#                  the SessionStart brief + per-prompt KB injection live.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

flag="DISABLE"
scope="all latch hooks + compactor"
if [ "${1:-}" = "--write-only" ]; then
  flag="DISABLE_WRITE"
  scope="write-side hooks only (Stop/SessionEnd/compactor); reads stay live"
fi

target="${KB_HOME}/${flag}"
if [ -e "$target" ]; then
  echo "latch already disabled: ${target} exists (${scope})."
else
  printf '%s\n' \
    "latch kill switch — created by bin/latch_disable.sh" \
    "Delete this file (or run bin/latch_enable.sh) to resume." \
    > "$target"
  echo "latch DISABLED — created ${target}"
  echo "  scope: ${scope}"
fi
echo "  re-enable: bash ${KB_HOME}/bin/latch_enable.sh"
