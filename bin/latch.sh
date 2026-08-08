#!/usr/bin/env bash
# Read-only status by default; confirmed scope configuration when requested.
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
Usage: bash bin/latch.sh
       bash bin/latch.sh --confirm latch [--shared | --private]
       bash bin/latch.sh --confirm latch --private [--new-kb | --kb-dir ABSOLUTE_PATH]
       bash bin/latch.sh --confirm latch --enable-project-scopes --shared
       bash bin/latch.sh --confirm latch --enable-project-scopes --private [--new-kb | --kb-dir ABSOLUTE_PATH]

With no arguments, latch only shows the effective root, state, policy, and KB.
Confirmed changes affect that filesystem root and its descendants. No KB
content is copied, merged, imported, or deleted.

Global Shared mode is unchanged unless --enable-project-scopes is explicitly
confirmed with Shared or Private. That one-way choice creates a boundary here
and makes every other unscoped location LOCKED.
EOF
}

PYTHON="$(resolve_python)" || {
  echo "latch: no Python found; set LATCH_PYTHON." >&2
  exit 1
}

if [ "$#" -eq 0 ]; then
  exec "$PYTHON" "${KB_HOME}/src/project_mode.py" status \
    --project "$PROJECT_INPUT" --intent latch
fi

case "$1" in
  -h|--help|help)
    usage
    exit 0
    ;;
  --confirm)
    [ "${2:-}" = "latch" ] || {
      echo "latch: confirmation must be exactly 'latch'." >&2
      exit 2
    }
    shift 2
    exec "$PYTHON" "${KB_HOME}/src/project_mode.py" latch \
      --project "$PROJECT_INPUT" --confirm latch "$@"
    ;;
  *)
    echo "latch: inspect without arguments, then use --confirm latch." >&2
    exit 2
    ;;
esac
