"""Detached writer for local dev-detector candidate packets."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import detector_trace


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="latch-detector-worker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript")
    parser.add_argument("--prompt-hash")
    parser.add_argument("--event-ts", required=True)
    parser.add_argument("--turn", type=int)
    parser.add_argument("--trigger", action="append", required=True)
    parser.add_argument("--node-id", type=int, action="append", default=[])
    try:
        ns = parser.parse_args(argv[1:])
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    if os.environ.get("LATCH_DEV_DETECTOR") != "1":
        return 0
    packet = detector_trace.build_trace(
        project_path=ns.project,
        session_id=ns.session_id,
        transcript_path=ns.transcript,
        prompt_hash=ns.prompt_hash,
        trigger_types=ns.trigger,
        event_ts=ns.event_ts,
        node_ids=ns.node_id,
        prompt_turn=ns.turn,
    )
    if not packet.get("should_emit"):
        return 0
    path = detector_trace.write_incident(packet, ns.project)
    # Normally stdout is DEVNULL. Keeping a structural receipt makes direct
    # dogfood/debug invocation testable without exposing transcript content.
    print(json.dumps({
        "ok": True,
        "incident_id": packet["incident_id"],
        "path": str(path),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
