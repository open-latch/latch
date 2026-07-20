"""Read-only command surface for the local development detector."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detector_trace


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="latch-detector-trace",
        description="Trace or review local detector candidates without mutation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    trace = sub.add_parser("trace", help="trace one exact current/previous turn")
    trace.add_argument("--project", default=os.getcwd())
    trace.add_argument("--session-id")
    trace.add_argument("--transcript")
    trace.add_argument("--prompt-hash")
    trace.add_argument("--event-ts")
    trace.add_argument("--turn", type=int)
    trace.add_argument("--previous", action="count", default=0)
    trace.add_argument("--node-id", type=int, action="append", default=[])
    trace.add_argument("--trigger", action="append", default=[])

    review = sub.add_parser("review", help="list local candidates read-only")
    review.add_argument("--project", default=os.getcwd())
    review.add_argument("--limit", type=int, default=20)

    try:
        ns = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if os.environ.get("LATCH_DEV_DETECTOR") != "1":
        print(json.dumps({
            "ok": False,
            "reason": "detector_disabled",
            "required": "LATCH_DEV_DETECTOR=1",
        }))
        return 2

    if ns.command == "review":
        rows = detector_trace.read_incidents(ns.project, limit=ns.limit)
        print(json.dumps({"ok": True, "count": len(rows), "incidents": rows}))
        return 0

    sid = detector_trace.resolve_session_id(ns.session_id)
    if not sid:
        print(json.dumps({"ok": False, "reason": "session_id_required"}))
        return 2
    packet = detector_trace.build_trace(
        project_path=ns.project,
        session_id=sid,
        transcript_path=ns.transcript,
        prompt_hash=ns.prompt_hash,
        trigger_types=ns.trigger or ["manual_trace"],
        event_ts=ns.event_ts,
        node_ids=ns.node_id,
        prompt_turn=ns.turn,
        previous=ns.previous,
    )
    print(json.dumps({"ok": True, "packet": packet}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
