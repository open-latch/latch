"""CLI backing ``bin/run_kb_correlate.sh``.

Usage:

    python correlator_cli.py --project <path>
                             --start YYYY-MM-DD
                             --end   YYYY-MM-DD
                             [--window 1800]
                             [--version 0.1.0]

Emits a single JSON counts dict (rows_emitted, rows_skipped_*) to stdout.
Exit 0 on success, 2 on argv error. Spec: KB id=1098 clarification #8.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import correlator


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="kb-correlate",
        description="Offline gate.log -> gate_outcome.log correlator (id=1098).",
    )
    p.add_argument("--project", default=os.getcwd(),
                   help="project directory (default: cwd)")
    p.add_argument("--start", required=True, type=_parse_date,
                   help="inclusive start date YYYY-MM-DD")
    p.add_argument("--end", required=True, type=_parse_date,
                   help="inclusive end date YYYY-MM-DD")
    p.add_argument("--window", type=int,
                   default=correlator.WINDOW_SECONDS_DEFAULT,
                   help="attribution window in seconds (default: 1800)")
    p.add_argument("--version", dest="correlator_version",
                   default=correlator.CORRELATOR_VERSION_DEFAULT,
                   help="correlator semver tag (default: 0.1.0)")
    try:
        ns = p.parse_args(argv[1:])
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    counts = correlator.correlate(
        ns.project, ns.start, ns.end,
        window_seconds=ns.window,
        correlator_version=ns.correlator_version,
    )
    sys.stdout.write(json.dumps({"ok": True, **counts}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
