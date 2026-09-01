#!/usr/bin/env bash
# Aggregate-only coverage for typed rejected_path predicates.
# Usage: bin/predicate_coverage.sh /path/to/vault.sqlite3

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec python3 "${SCRIPT_DIR}/../src/latch/gate/predicate_coverage.py" "$@"
