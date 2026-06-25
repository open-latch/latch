#!/usr/bin/env bash
#
# install_commands.sh — install latch's slash-command wrappers into the user's
# Claude Code commands directory.
#
# Claude Code only discovers slash commands under ~/.claude/commands/ (or a
# project's .claude/commands/); it does NOT scan this repo's commands/ folder.
# So the command source lives here and must be copied into a scanned location.
# This script is that copy step.
#
# Self-locating: it resolves the repo root from its own path and substitutes
# the <KB_HOME> placeholder in each command with the repo's ACTUAL location,
# so the installed commands work regardless of where the repo was cloned — no
# environment variable required. (The runtime LATCH_HOME env var remains an
# optional wrapper override; CLAUDE_KB_HOME is the legacy alias.)
#
# Idempotent — safe to re-run after editing any command in commands/.
#
# Override the destination with CLAUDE_COMMANDS_DIR if your commands live
# elsewhere.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"

# Normalize MSYS/Cygwin/git-bash "/c/foo" to "C:/foo" so the Windows shell that
# later runs the command resolves the path. No-op on Linux/macOS.
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    KB_HOME="$(printf '%s' "$KB_HOME" | sed -E 's|^/([a-zA-Z])/|\U\1:/|')"
    ;;
esac

SRC_DIR="$KB_HOME/commands"
DEST_DIR="${CLAUDE_COMMANDS_DIR:-$HOME/.claude/commands}"

if [ ! -d "$SRC_DIR" ]; then
  echo "error: no commands/ directory at $SRC_DIR" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"

count=0
for f in "$SRC_DIR"/*.md; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  sed "s|<KB_HOME>|$KB_HOME|g" "$f" > "$DEST_DIR/$name"
  echo "installed $name"
  count=$((count + 1))
done

echo "Done — $count command(s) installed to $DEST_DIR (KB_HOME=$KB_HOME)"
