#!/usr/bin/env bash
#
# install_engine.sh — thin wrapper around src/install_engine.py, the engine
# half of the latch install. Wires the KB engine into Claude Code:
#   1. registers the MCP server via `claude mcp add --scope user` (the store
#      Claude Code actually reads — NOT settings.json mcpServers, which it
#      ignores);
#   2. merges the SessionStart/UserPromptSubmit/Stop/SessionEnd hooks into
#      ~/.claude/settings.json;
#   3. adds server-level `mcp__latch` and legacy `mcp__claude-kb` permission
#      rules so every latch_* tool is auto-approved (no per-tool prompts).
# Also removes dead latch-owned mcpServers blocks left by older installs.
#
# All logic lives in src/install_engine.py (stdlib-only; shared by this CLI and
# the PowerShell wrapper). Idempotent and non-destructive (settings.json is
# backed up to settings.json.latchbak).
#
# Usage:
#   bash bin/install_engine.sh [--python PATH] [--dry-run] [--check]
#
# Interpreter resolution for RUNNING the installer (the installer itself is
# stdlib-only; the interpreter it REGISTERS is resolved inside the script and
# prefers the repo .venv):
#   $LATCH_PYTHON -> $CLAUDE_KB_PYTHON -> repo .venv -> python3 -> python
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
if [ -n "${LATCH_PYTHON:-}" ]; then
  PY="$LATCH_PYTHON"
elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
  PY="$CLAUDE_KB_PYTHON"
elif [ -x "${KB_HOME}/.venv/bin/python" ]; then
  PY="${KB_HOME}/.venv/bin/python"
elif [ -x "${KB_HOME}/.venv/Scripts/python.exe" ]; then
  PY="${KB_HOME}/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "install_engine: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi
exec "$PY" "${KB_HOME}/src/install_engine.py" "$@"
