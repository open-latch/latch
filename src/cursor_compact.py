#!/usr/bin/env python3
"""Manual, current-session-only Cursor compaction entry point."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import compactor
import cursor_transcript


def _default_summarizer_backend() -> str:
    return (
        os.environ.get("CURSOR_KB_COMPACTOR_BACKEND")
        or os.environ.get("LATCH_COMPACTOR_BACKEND")
        or os.environ.get("LATCH_MODEL_BACKEND")
        or "cursor"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Compact the current Cursor conversation into latch."
    )
    ap.add_argument("session_id", nargs="?",
                    help="optional session id; must match the current Cursor marker")
    ap.add_argument("--project", default=None,
                    help="project path for the KB (default: current working directory)")
    ap.add_argument("--transcript", default=None,
                    help="optional transcript path; must match the current Cursor marker")
    ap.add_argument("--final", action="store_true",
                    help="mark the session summary canonical and ended")
    ap.add_argument("--summarizer", choices=sorted(compactor.SUPPORTED_SUMMARIZER_BACKENDS),
                    default=_default_summarizer_backend(),
                    help="summarizer backend (default: cursor)")
    args = ap.parse_args(argv)

    project = str(Path(args.project or Path.cwd()).expanduser().resolve())
    try:
        sid, transcript = cursor_transcript.resolve_current(
            project,
            session_id=args.session_id,
            transcript_path=args.transcript,
        )
    except cursor_transcript.CursorTranscriptError as e:
        print(f"cursor-latch-compact: {e}", file=sys.stderr)
        return 1

    result = compactor.run_compaction(
        sid,
        project,
        str(transcript),
        final=args.final,
        summarizer_backend=args.summarizer,
    )
    result["session_id"] = sid
    result["transcript_path"] = str(transcript)
    result["current_session_only"] = True
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
