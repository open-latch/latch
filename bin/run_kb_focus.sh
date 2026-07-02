#!/usr/bin/env bash
# Wrapper for /kb-focus slash command.
# Forwards subcommand + args to the Python CLI, with the project cwd
# auto-resolved from the shell working directory. Mirrors run_compact_now.sh.
#
# Usage (called by /kb-focus):
#   bash run_kb_focus.sh                    -> list current focus
#   bash run_kb_focus.sh list
#   bash run_kb_focus.sh set <id>
#   bash run_kb_focus.sh pin <id>
#   bash run_kb_focus.sh unpin <id>
#   bash run_kb_focus.sh drop <id>
#
# Allow in user-scope ~/.claude/settings.json so /kb-focus runs unattended:
#   "Bash(bash ${LATCH_HOME}/bin/run_kb_focus.sh:*)"
# Existing settings that use ${CLAUDE_KB_HOME} still work as the legacy alias.

set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

# pwd -W gives Windows-style path on Git Bash (CLI sanitize_cwd expects that
# form to match the MCP server's project dir); falls back on POSIX.
PROJECT_DIR=$(pwd -W 2>/dev/null || pwd)

SUB="${1:-list}"
shift || true

exec python "${KB_HOME}/src/kb_focus_cli.py" \
  "${PROJECT_DIR}" "${SUB}" "$@"
