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

# Command templates that embed an executable path need shell-specific literals.
# Reuse install_engine's renderer so every installation path has one quoting
# contract, including commands-only installs.
COMMAND_RENDER_PYTHON="${LATCH_PYTHON:-${CLAUDE_KB_PYTHON:-}}"
if [ -z "$COMMAND_RENDER_PYTHON" ]; then
  for candidate in "$KB_HOME/.venv/bin/python" "$KB_HOME/.venv/Scripts/python.exe"; do
    if [ -x "$candidate" ]; then
      COMMAND_RENDER_PYTHON="$candidate"
      break
    fi
  done
fi
if [ -z "$COMMAND_RENDER_PYTHON" ]; then
  COMMAND_RENDER_PYTHON="$(command -v python3 || command -v python || true)"
fi
if [ -z "$COMMAND_RENDER_PYTHON" ]; then
  echo "error: Python is required to render command installation paths safely" >&2
  exit 1
fi

COMMAND_RENDER_CODE='import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import install_engine as ie
body = Path(sys.argv[3]).read_text(encoding="utf-8")
body = ie.render_command_template(body, kb_home=sys.argv[2])
if sys.argv[5] == "kb-gate.md":
    body = body.replace("/bin/run_latch_gate.sh", "/bin/run_kb_gate.sh")
Path(sys.argv[4]).write_text(body, encoding="utf-8")'

render_command_file() {
  local source="$1" target="$2" legacy="${3:-}"
  "$COMMAND_RENDER_PYTHON" -c "$COMMAND_RENDER_CODE" \
    "$KB_HOME/src" "$KB_HOME" "$source" "$target" "$legacy"
}

if [ ! -d "$SRC_DIR" ]; then
  echo "error: no commands/ directory at $SRC_DIR" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"

installed=0
updated=0
removed=0
skipped=0
for f in "$SRC_DIR"/*.md; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  render_command_file "$f" "$DEST_DIR/$name"
  echo "installed $name"
  installed=$((installed + 1))
done

is_latch_command() {
  local file="$1"
  [ -f "$file" ] || return 1
  grep -Fq "<KB_HOME>" "$file" && return 0
  grep -Fq "<KB_HOME_POSIX_LITERAL>" "$file" && return 0
  grep -Fq "<LATCH_REVIEW_POSIX_LITERAL>" "$file" && return 0
  grep -Fq "<LATCH_REVIEW_POWERSHELL_LITERAL>" "$file" && return 0
  grep -Fq "$KB_HOME" "$file" && return 0
  grep -Eq '/bin/(run_kb_gate|run_latch_gate|latch_baseline|unlatch|latch_gate_report|run_compact_now|run_latch_compact_now|run_kb_focus)\.sh|/bin/latch_direction\.sh|/bin/latch-review(\.ps1)?|/src/(budget|maintenance)\.py|kb_profile_(active|bind)|mission-control verification profile|trust-and-go verification profile' "$file"
}

update_legacy_alias() {
  local legacy="$1"
  local primary="$2"
  local legacy_path="$DEST_DIR/$legacy"
  local primary_path="$SRC_DIR/$primary"
  [ -f "$legacy_path" ] || return 0
  [ -f "$primary_path" ] || return 0
  if ! is_latch_command "$legacy_path"; then
    echo "skipped legacy alias $legacy (looks user-owned)"
    skipped=$((skipped + 1))
    return 0
  fi
  render_command_file "$primary_path" "$legacy_path" "$legacy"
  echo "updated legacy alias $legacy -> $primary"
  updated=$((updated + 1))
}

update_legacy_alias "kb-budget-approve.md" "latch-budget-approve.md"
update_legacy_alias "kb-compact.md" "latch-compact.md"
update_legacy_alias "kb-decay.md" "latch-decay.md"
update_legacy_alias "kb-gate.md" "latch-gate.md"
update_legacy_alias "kb-gate-report.md" "latch-gate-report.md"
update_legacy_alias "kb-heal.md" "latch-heal.md"
update_legacy_alias "kb-tree.md" "latch-tree.md"

for stale in latch-baseline.md kb-focus.md kb-project-direction.md mission-control.md trust-and-go.md; do
  stale_path="$DEST_DIR/$stale"
  [ -f "$stale_path" ] || continue
  if ! is_latch_command "$stale_path"; then
    echo "skipped stale legacy command $stale (looks user-owned)"
    skipped=$((skipped + 1))
    continue
  fi
  rm -f "$stale_path"
  echo "removed stale legacy command $stale"
  removed=$((removed + 1))
done

echo "Done — installed $installed command(s), updated $updated legacy alias(es), removed $removed stale command(s), skipped $skipped user-owned file(s) in $DEST_DIR (KB_HOME=$KB_HOME)"
