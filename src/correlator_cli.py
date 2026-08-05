"""CLI backing ``bin/run_kb_correlate.sh``.

Usage:

    python correlator_cli.py --project <path>
                             --start YYYY-MM-DD
                             --end   YYYY-MM-DD
                             [--window 1800]
                             [--correlator-version <semver>]
                             [--measurement-protocol-version <version>]

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
    p.add_argument("--version", "--correlator-version", dest="correlator_version",
                   default=correlator.CORRELATOR_VERSION_DEFAULT,
                   help=("correlator implementation semver; --version is a "
                         f"deprecated alias (default: {correlator.CORRELATOR_VERSION_DEFAULT})"))
    p.add_argument(
        "--measurement-protocol-version",
        default=correlator.MEASUREMENT_PROTOCOL_VERSION_DEFAULT,
        help=("protocol pin used only to validate diagnostic joins "
              f"(default: {correlator.MEASUREMENT_PROTOCOL_VERSION_DEFAULT})"),
    )
    p.add_argument(
        "--pinned-runtime-version",
        help=("runtime version pinned by the pre-T0 manifest; without it rows "
              "remain pilot/loss and cannot enter the clean cohort"),
    )
    p.add_argument(
        "--project-key-epoch",
        default=correlator.PROJECT_KEY_EPOCH_DEFAULT,
        help=("project-proof key epoch pinned by the pre-T0 manifest; required "
              "for proof-backed project attribution "
              f"(default: {correlator.PROJECT_KEY_EPOCH_DEFAULT})"),
    )
    try:
        ns = p.parse_args(argv[1:])
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2

    counts = correlator.correlate(
        ns.project, ns.start, ns.end,
        window_seconds=ns.window,
        correlator_version=ns.correlator_version,
        measurement_protocol_version=ns.measurement_protocol_version,
        pinned_runtime_version=ns.pinned_runtime_version,
        project_key_epoch=ns.project_key_epoch,
    )
    sys.stdout.write(json.dumps({"ok": True, **counts}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
