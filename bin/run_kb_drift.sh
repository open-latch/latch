#!/usr/bin/env bash
# Wrapper for the deterministic body-edge / state drift sweep (id=1149 Part 3).
# Usage:
#   bash run_kb_drift.sh [--project <path>]
#
# Defaults --project to the current working directory. Prints a JSON counts
# dict (nodes_scanned, orphan_mention, stale_prereq, rows_emitted).

set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

exec python "${KB_HOME}/src/drift_cli.py" "$@"
