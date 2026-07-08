#!/usr/bin/env bash
#
# latch_enable.sh — undo latch_disable.sh by removing the DISABLE sentinel, so
# latch's hooks + compactor resume on the next prompt.
#
# By default this removes the full-stop DISABLE file and the UNLATCHED receipt
# (both are full influence-off controls), while leaving DISABLE_WRITE alone. Pass
# --all to also remove DISABLE_WRITE and return to fully-default behavior.
set -euo pipefail
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
PROJECT_DIR=$(pwd -W 2>/dev/null || pwd)

remove_all=false
[ "${1:-}" = "--all" ] && remove_all=true

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

restore_unlatched_instructions() {
  local py
  if ! py="$(resolve_python)"; then
    echo "latch_enable: UNLATCHED is active but no Python was found to restore project instruction files." >&2
    echo "set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON), then run: bash ${KB_HOME}/bin/unlatch.sh --confirm latch" >&2
    exit 1
  fi
  "$py" "${KB_HOME}/src/unlatch.py" on --project "$PROJECT_DIR"
}

if [ -e "${KB_HOME}/UNLATCHED" ] || [ -e "${KB_HOME}/UNLATCH_STATE.json" ]; then
  restore_unlatched_instructions
fi

removed=false
if [ -e "${KB_HOME}/DISABLE" ]; then
  rm -f "${KB_HOME}/DISABLE"
  echo "removed ${KB_HOME}/DISABLE"
  removed=true
fi

if [ -e "${KB_HOME}/UNLATCHED" ]; then
  rm -f "${KB_HOME}/UNLATCHED"
  echo "removed ${KB_HOME}/UNLATCHED"
  removed=true
fi

if $remove_all && [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  rm -f "${KB_HOME}/DISABLE_WRITE"
  echo "removed ${KB_HOME}/DISABLE_WRITE"
  removed=true
elif [ -e "${KB_HOME}/DISABLE_WRITE" ]; then
  echo "note: ${KB_HOME}/DISABLE_WRITE still present — write-side hooks "
  echo "      (Stop/SessionEnd/compactor) stay OFF. Remove with: bash bin/latch_enable.sh --all"
fi

env_still_off=false
if [ -n "${LATCH_UNLATCHED:-}" ] || [ -n "${LATCH_DISABLE:-}" ] || [ -n "${CLAUDE_KB_DISABLE:-}" ]; then
  env_still_off=true
fi

if $removed && ! $env_still_off; then
  echo "latch ENABLED — hooks resume on the next prompt."
elif $env_still_off; then
  echo "latch files are ENABLED, but an environment disable flag is still set."
  echo "Unset it before expecting hooks to resume."
else
  echo "latch was not disabled (no DISABLE or UNLATCHED file). Nothing to do."
fi
