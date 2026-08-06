#!/usr/bin/env bash
# Install Claude slash commands through install_engine's single policy owner.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LATCH_COMMAND_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -x "$LATCH_COMMAND_HOME/.venv/bin/python" ]; then
  LATCH_COMMAND_PYTHON="$LATCH_COMMAND_HOME/.venv/bin/python"
elif [ -x "$LATCH_COMMAND_HOME/.venv/Scripts/python.exe" ]; then
  LATCH_COMMAND_PYTHON="$LATCH_COMMAND_HOME/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  LATCH_COMMAND_PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  LATCH_COMMAND_PYTHON="$(command -v python)"
else
  echo "error: Python is required to install Claude slash commands" >&2
  exit 1
fi

unset LATCH_HOME CLAUDE_KB_HOME
exec "$LATCH_COMMAND_PYTHON" \
  "$LATCH_COMMAND_HOME/src/install_engine.py" --commands-only "$@"
