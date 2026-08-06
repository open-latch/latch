#!/usr/bin/env bash
# Read-only status by default; confirmed project-local OFF boundary on request.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "${SCRIPT_DIR}/.." && pwd)}}"
PROJECT_INPUT="$(pwd -W 2>/dev/null || pwd)"

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
    printf '%s\n' python3
  elif command -v python >/dev/null 2>&1; then
    printf '%s\n' python
  else
    return 1
  fi
}

usage() {
  cat <<'EOF'
Usage: bash bin/unlatch.sh
       bash bin/unlatch.sh --confirm unlatch

With no arguments, unlatch only shows the effective root, state, policy, and
KB. Confirmed unlatch turns Latch off at this root and below without deleting
or changing its remembered KB. Other scopes remain unchanged.
EOF
}

PYTHON="$(resolve_python)" || {
  echo "unlatch: no Python found; set LATCH_PYTHON." >&2
  exit 1
}

if [ "$#" -eq 0 ]; then
  exec "$PYTHON" "${KB_HOME}/src/project_mode.py" status \
    --project "$PROJECT_INPUT" --intent unlatch
fi

case "$1" in
  -h|--help|help)
    usage
    exit 0
    ;;
  --confirm)
    [ "${2:-}" = "unlatch" ] || {
      echo "unlatch: confirmation must be exactly 'unlatch'." >&2
      exit 2
    }
    shift 2
    [ "$#" -eq 0 ] || {
      echo "unlatch: no options are accepted after confirmation." >&2
      exit 2
    }
    exec "$PYTHON" "${KB_HOME}/src/project_mode.py" unlatch \
      --project "$PROJECT_INPUT" --confirm unlatch
    ;;
  *)
    echo "unlatch: inspect without arguments, then use --confirm unlatch." >&2
    exit 2
    ;;
esac
