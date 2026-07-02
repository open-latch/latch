"""CLI backing the /latch-gate slash command.

Usage (called by `bin/run_latch_gate.sh`; legacy: `bin/run_kb_gate.sh`):

    python kb_gate_cli.py <project_cwd> <request...>

The request is the full user query, joined from argv[2:]. Always emits a
single JSON object with the run_gate() return shape (verdict, evidence,
chains, request). Exit 0 on success, 2 on argv error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import gate


def _emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stdout.write(json.dumps({
            "ok": False, "error": "usage: kb_gate_cli.py <cwd> <request...>"
        }))
        sys.stdout.write("\n")
        return 2
    cwd = argv[1]
    request = " ".join(argv[2:]).strip()
    if not request:
        return _emit({"ok": False, "error": "empty request"})

    conn = db.connect(cwd)
    try:
        out = gate.run_gate(conn, request, project_path=cwd)
    finally:
        conn.close()

    return _emit({"ok": True, **out})


if __name__ == "__main__":
    sys.exit(main(sys.argv))
