"""CLI backing ``bin/run_kb_drift.sh``.

Usage:

    python drift_cli.py [--project <path>]

Runs the deterministic body-edge / state drift sweep against the current DB
state (point-in-time — no date range, unlike the correlator). Emits a single
JSON counts dict (nodes_scanned, orphan_mention, stale_prereq, rows_emitted)
to stdout. Exit 0 on success, 2 on argv error. Spec: KB id=1149 Part 3.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db      # noqa: E402
import drift   # noqa: E402


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="kb-drift",
        description="Deterministic body-edge / state drift sweep (id=1149 Part 3).",
    )
    p.add_argument("--project", default=os.getcwd(),
                   help="project directory (default: cwd)")
    try:
        ns = p.parse_args(argv[1:])
    except SystemExit:
        return 2
    conn = db.connect(ns.project)
    try:
        counts = drift.sweep(conn, ns.project)
    finally:
        conn.close()
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
