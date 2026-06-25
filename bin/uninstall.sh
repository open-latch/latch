#!/usr/bin/env bash
#
# uninstall.sh — thin wrapper around src/uninstall_engine.py, the strict inverse
# of bin/install_engine.sh. Removes latch's wiring from Claude Code:
#   1. deregisters latch-owned MCP servers via `claude mcp remove`;
#   2. removes the SessionStart/UserPromptSubmit/Stop/SessionEnd hooks + the
#      latch-owned MCP permission rules from ~/.claude/settings.json (preserving
#      every other hook/permission);
#   3. removes latch's slash commands from ~/.claude/commands/.
#
# Leaves your KB data (projects/) and the repo in place by default. Flags:
#   --dry-run             show what would change; write nothing
#   --check               verify removal only; exit 1 if any latch wiring remains
#   --claude-md PATH      also strip latch's managed region from this CLAUDE.md
#                         (repeatable)
#   --purge               also delete projects/ data + DISABLE kill-switch files
#   --yes / -y            skip the confirmation prompt
#
# All logic lives in src/uninstall_engine.py (stdlib-only; shared with the
# PowerShell wrapper). settings.json is backed up to a timestamped
# settings.json.latchbak-<UTC> before any write.
#
# Interpreter resolution (the uninstaller is stdlib-only):
#   $LATCH_PYTHON -> $CLAUDE_KB_PYTHON -> python3 -> python
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "uninstall: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi
exec "$PY" "${KB_HOME}/src/uninstall_engine.py" "$@"
