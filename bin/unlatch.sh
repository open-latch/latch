#!/usr/bin/env bash
#
# unlatch.sh - confirmed in-place vanilla-agent / escape-hatch mode.
#
# With no arguments this prints the current state and the exact confirmation the
# user must provide. State-changing calls must pass --confirm unlatch|latch.
set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

unlatched="${KB_HOME}/UNLATCHED"
disable="${KB_HOME}/DISABLE"
disable_write="${KB_HOME}/DISABLE_WRITE"
project_dir="$(pwd -W 2>/dev/null || pwd)"

resolve_python() {
  if [ -n "${LATCH_PYTHON:-}" ]; then
    printf '%s\n' "$LATCH_PYTHON"
  elif [ -n "${CLAUDE_KB_PYTHON:-}" ]; then
    printf '%s\n' "$CLAUDE_KB_PYTHON"
  elif [ -x "${KB_HOME}/.venv/bin/python" ]; then
    printf '%s\n' "${KB_HOME}/.venv/bin/python"
  elif [ -x "${KB_HOME}/.venv/Scripts/python.exe" ]; then
    printf '%s\n' "${KB_HOME}/.venv/Scripts/python.exe"
  elif command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "python3"
  elif command -v python >/dev/null 2>&1; then
    printf '%s\n' "python"
  else
    return 1
  fi
}

run_instruction_mask() {
  local action="$1"
  local py
  if ! py="$(resolve_python)"; then
    echo "unlatch: no Python found; project CLAUDE.md/AGENTS.md could not be masked/restored." >&2
    if [ "$action" = "status" ]; then
      echo "set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to inspect instruction-mask status." >&2
    else
      echo "set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) and re-run: bash ${KB_HOME}/bin/unlatch.sh --confirm ${action}" >&2
    fi
    return 1
  fi
  "$py" "${KB_HOME}/src/unlatch.py" "$action" --project "$project_dir"
}

is_unlatched() {
  [ -n "${LATCH_UNLATCHED:-}" ] || [ -e "$unlatched" ]
}

write_unlatched_receipt() {
  printf '%s\n' \
    "latch unlatched mode - created by bin/unlatch.sh" \
    "Latch is currently UNLATCHED." \
    "This is the agent without latch's project judgment layer." \
    "DISABLE enforces full influence-off; UNLATCHED makes the off state visible." \
    "Run /unlatch again to re-latch. KB data is not deleted." \
    "If LATCH_UNLATCHED is set, unset it too before expecting hooks to resume." \
    "UNLATCHED_LATCH_HOME=${KB_HOME}" \
    > "$unlatched"
}

write_disable_receipt() {
  printf '%s\n' \
    "latch kill switch - created by bin/unlatch.sh" \
    "Unlatched mode is active. Run /unlatch again to re-latch." \
    "If LATCH_UNLATCHED is set, unset it too before expecting hooks to resume." \
    > "$disable"
}

print_unlatched_facts() {
  echo "  disabled: prompt KB injection, gate guidance,"
  echo "            Stop/SessionEnd compaction, self-heal, maintenance, and"
  echo "            automatic latch writes for this latch install."
  echo "  still true: KB files stay local and unchanged; latch remains installed;"
  echo "              /unlatch and status commands remain available;"
  echo "              non-latch tools/hooks are unaffected."
  echo "  scope: install-level; if you change repos before re-latching, latch"
  echo "         remains off and will say so."
}

status_prompt() {
  echo "latch unlatch status (KB_HOME=${KB_HOME})"
  if is_unlatched; then
    echo "  [UNLATCHED] Latch is currently UNLATCHED."
    print_unlatched_facts
    echo
    echo "Switch back to LATCHED mode?"
    if [ -n "${LATCH_UNLATCHED:-}" ]; then
      echo "Confirming latch cleans local unlatch files/state, but hooks stay off until LATCH_UNLATCHED is unset."
    else
      echo "Latch hooks will resume on the next prompt."
    fi
    echo "Reply exactly: latch"
  elif [ -n "${LATCH_DISABLE:-}" ] || [ -n "${CLAUDE_KB_DISABLE:-}" ] || [ -e "$disable" ]; then
    echo "  [DISABLED] latch kill switch is active, but Unlatched mode is not set."
    echo "             To re-enable the kill switch directly: bash ${KB_HOME}/bin/latch_enable.sh"
  else
    echo "  [LATCHED] Latch is currently LATCHED."
    echo
    echo "Switch to UNLATCHED mode?"
    echo "This turns latch's project-judgment layer off for this latch install, masks latch-managed CLAUDE.md/AGENTS.md regions in this project, and leaves KB data intact."
    echo "If you change repos before re-latching, latch remains off and will say so."
    echo "To re-latch later, run /unlatch again."
    echo "Reply exactly: unlatch"
  fi

  if [ -n "${LATCH_DISABLE_WRITE:-}" ] || [ -n "${CLAUDE_KB_DISABLE_WRITE:-}" ] || [ -e "$disable_write" ]; then
    echo "  [write-off] write-side kill switch is also active."
  fi
}

confirm=""
case "${1:-}" in
  -h|--help|help)
    cat <<'EOF'
Usage: bash bin/unlatch.sh
       bash bin/unlatch.sh --confirm unlatch
       bash bin/unlatch.sh --confirm latch

No-argument mode is safe: it prints the current LATCHED/UNLATCHED state and the
exact confirmation word. State changes require --confirm unlatch|latch.
EOF
    exit 0
    ;;
  "")
    status_prompt
    if ! run_instruction_mask status; then
      echo "  instruction mask status unavailable; no state changed."
    fi
    exit 0
    ;;
  --confirm)
    if [ "$#" -ne 2 ]; then
      echo "unlatch: --confirm requires exactly one word: unlatch or latch." >&2
      exit 2
    fi
    confirm="${2:-}"
    ;;
  *)
    echo "unlatch: no positional actions are accepted; run without args, then pass --confirm unlatch|latch after user confirmation." >&2
    exit 2
    ;;
esac

case "$confirm" in
  unlatch)
    if is_unlatched; then
      echo "Latch is already UNLATCHED for this latch install. Retrying instruction mask for the current project."
      run_instruction_mask off
      if [ ! -e "$unlatched" ]; then
        write_unlatched_receipt
        echo "created ${unlatched}"
      fi
      if [ ! -e "$disable" ]; then
        write_disable_receipt
        echo "created ${disable}"
      fi
      exit 0
    fi
    run_instruction_mask off
    write_unlatched_receipt
    echo "created ${unlatched}"
    if [ -e "$disable" ]; then
      echo "full kill switch already present - ${disable} exists."
    else
      write_disable_receipt
      echo "created ${disable}"
    fi
    echo "Latch is now UNLATCHED - this is the agent without latch's project judgment layer."
    print_unlatched_facts
    echo "  re-latch: run /unlatch again and confirm latch"
    ;;
  latch)
    if ! is_unlatched && [ ! -e "${KB_HOME}/UNLATCH_STATE.json" ]; then
      echo "Latch is already LATCHED. No action taken."
      exit 0
    fi
    run_instruction_mask on
    removed=false
    for f in "$unlatched" "$disable" "$disable_write"; do
      if [ -e "$f" ]; then
        rm -f "$f"
        echo "removed $f"
        removed=true
      fi
    done
    env_still_off=false
    if [ -n "${LATCH_UNLATCHED:-}" ] || [ -n "${LATCH_DISABLE:-}" ] || [ -n "${CLAUDE_KB_DISABLE:-}" ]; then
      env_still_off=true
    fi
    if $removed && ! $env_still_off; then
      echo "Latch is now LATCHED - hooks resume on the next prompt."
    elif $env_still_off; then
      echo "Latch files are LATCHED, but an environment disable flag is still set."
      echo "Unset it before expecting hooks to resume."
    else
      echo "Latch was already LATCHED. Nothing to do."
    fi
    ;;
  *)
    echo "unlatch: confirmation must be exactly 'unlatch' or 'latch'." >&2
    exit 2
    ;;
esac
