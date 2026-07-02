#!/usr/bin/env bash
# Legacy wrapper for /kb-gate. Fresh installs prefer /latch-gate and
# bin/run_latch_gate.sh; keep this delegate so existing command files and docs
# keep working.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/run_latch_gate.sh" "$@"
