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

installed=0
updated=0
removed=0
skipped=0
for f in "$SRC_DIR"/*.md; do
  [ -e "$f" ] || continue
  name="$(basename "$f")"
  sed "s|<KB_HOME>|$KB_HOME|g" "$f" > "$DEST_DIR/$name"
  echo "installed $name"
  installed=$((installed + 1))
done

is_latch_command() {
  local file="$1"
  [ -f "$file" ] || return 1
  grep -Fq "<KB_HOME>" "$file" && return 0
  grep -Fq "$KB_HOME" "$file" && return 0
  grep -Eq '/bin/(run_kb_gate|run_latch_gate|latch_gate_report|run_compact_now|run_latch_compact_now|run_kb_focus)\.sh|/bin/latch_direction\.sh|/src/(budget|maintenance)\.py' "$file"
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
  if [ "$legacy" = "kb-gate.md" ]; then
    sed "s|<KB_HOME>|$KB_HOME|g; s|/bin/run_latch_gate\\.sh|/bin/run_kb_gate.sh|g" "$primary_path" > "$legacy_path"
  else
    sed "s|<KB_HOME>|$KB_HOME|g" "$primary_path" > "$legacy_path"
  fi
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

for stale in kb-focus.md kb-project-direction.md; do
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
