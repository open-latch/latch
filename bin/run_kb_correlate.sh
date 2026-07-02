#!/usr/bin/env bash
# Wrapper for the offline gate.log -> gate_outcome.log correlator (id=1098).
# Usage:
#   bash run_kb_correlate.sh --start YYYY-MM-DD --end YYYY-MM-DD \
#                            [--window 1800] [--version 0.1.0] \
#                            [--project <path>]
#
# Defaults --project to the current working directory.

set -euo pipefail

KB_HOME="${LATCH_HOME:-${CLAUDE_KB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

exec python "${KB_HOME}/src/correlator_cli.py" "$@"
