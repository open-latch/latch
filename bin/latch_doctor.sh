#!/usr/bin/env bash
#
# latch_doctor.sh — thin wrapper around src/doctor.py, the cross-platform
# install verifier. Run it AFTER installing latch to confirm the environment
# can actually load and run the tool (deps fully installed, no CPU-arch
# mismatch / Rosetta SIGILL). All logic lives in src/doctor.py.
#
# Usage:
#   bash bin/latch_doctor.sh [--json] [--skip-embed] [--no-arch]
#
# Exit code: 0 = healthy; non-zero = at least one hard check failed.
#
# Interpreter resolution — must match src/install_engine.py:resolve_python so
# the doctor tests the SAME interpreter the hooks run under. Prefer the repo
# venv: on Apple Silicon a PATH python3 is often the Rosetta/system interpreter,
# so without this the doctor would report a CPU-arch FAIL even after the venv was
# correctly rebuilt native-arm64. The .venv checks fall through on system/shared
# Python installs (no .venv present). See KB id=1467.
#   $LATCH_PYTHON -> $CLAUDE_KB_PYTHON -> $KB_HOME/.venv -> python3 -> python
# Set LATCH_PYTHON to force a specific interpreter; CLAUDE_KB_PYTHON is the
# legacy alias.
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
  echo "latch doctor: no Python found (set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to your interpreter)." >&2
  exit 2
fi
exec "$PY" "${KB_HOME}/src/doctor.py" "$@"
