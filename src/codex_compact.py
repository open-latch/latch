#!/usr/bin/env python3
"""Manual Codex compaction entry point.

Resolve the invoking Codex thread to a rollout transcript, then run the shared
compactor.  This path is fail-closed and never searches Claude transcripts.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import codex_transcript
import compactor
import paths


DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
DEFAULT_WAIT_TIMEOUT_S = 300.0
DEFAULT_POLL_INTERVAL_S = 1.0


def _default_summarizer_backend() -> str:
    return (
        os.environ.get("CODEX_KB_COMPACTOR_BACKEND")
        or os.environ.get("CLAUDE_KB_COMPACTOR_BACKEND")
        or os.environ.get("LATCH_COMPACTOR_BACKEND")
        or "codex"
    )


def _start_background_process(
    *,
    session_id: str,
    project: str,
    final: bool,
    summarizer_backend: str,
) -> tuple[subprocess.Popen, Path, int, str]:
    """Start a Codex compaction child and return its log path + start offset."""
    script = Path(__file__).resolve()
    launch_id = uuid.uuid4().hex
    args = [
        sys.executable,
        str(script),
        session_id,
        "--project", project,
        "--summarizer", summarizer_backend,
        "--launch-id", launch_id,
    ]
    if final:
        args.append("--final")

    log_dir = paths.ensure_project_dir(project)
    log_path = log_dir / "codex_compact_background.log"
    start_offset = log_path.stat().st_size if log_path.exists() else 0
    popen_kwargs = dict(
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    with log_path.open("ab") as log:
        proc = subprocess.Popen(args, stdout=log, stderr=log, **popen_kwargs)

    return proc, log_path, start_offset, launch_id


def spawn_background(
    *,
    session_id: str,
    project: str,
    final: bool,
    summarizer_backend: str,
) -> dict:
    """Detach a Codex compaction child after transcript validation succeeds."""
    proc, log_path, _start_offset, launch_id = _start_background_process(
        session_id=session_id,
        project=project,
        final=final,
        summarizer_backend=summarizer_backend,
    )
    return {
        "ok": True,
        "background": True,
        "pid": proc.pid,
        "session_id": session_id,
        "launch_id": launch_id,
        "log_path": str(log_path),
        "summarizer_backend": summarizer_backend,
    }


def _matches_expected_result(
    obj: dict,
    *,
    expected_session_id: str | None,
    expected_launch_id: str | None,
) -> bool:
    if expected_launch_id and obj.get("launch_id") != expected_launch_id:
        return False
    if expected_session_id and obj.get("session_id") != expected_session_id:
        return False
    return True


def _latest_json_line_since(
    log_path: Path,
    start_offset: int,
    *,
    expected_session_id: str | None = None,
    expected_launch_id: str | None = None,
) -> tuple[dict | None, int]:
    if not log_path.exists():
        return None, 0
    try:
        with log_path.open("rb") as f:
            f.seek(start_offset)
            data = f.read()
    except OSError:
        return None, 0

    latest = None
    ignored = 0
    for raw_line in data.splitlines():
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if _matches_expected_result(
                obj,
                expected_session_id=expected_session_id,
                expected_launch_id=expected_launch_id,
            ):
                latest = obj
            else:
                ignored += 1
    return latest, ignored


def wait_for_background_result(
    proc: subprocess.Popen,
    log_path: Path,
    start_offset: int,
    *,
    expected_session_id: str | None = None,
    expected_launch_id: str | None = None,
    timeout_s: float = DEFAULT_WAIT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> dict:
    """Wait for the detached child to write its final JSON result."""
    deadline = time.monotonic() + timeout_s
    ignored_json = 0
    while True:
        latest, ignored = _latest_json_line_since(
            log_path,
            start_offset,
            expected_session_id=expected_session_id,
            expected_launch_id=expected_launch_id,
        )
        ignored_json = max(ignored_json, ignored)
        if latest is not None:
            return latest

        exit_code = proc.poll()
        if exit_code is not None:
            latest, ignored = _latest_json_line_since(
                log_path,
                start_offset,
                expected_session_id=expected_session_id,
                expected_launch_id=expected_launch_id,
            )
            ignored_json = max(ignored_json, ignored)
            if latest is not None:
                return latest
            if ignored_json:
                return {
                    "ok": False,
                    "reason": "background_no_matching_result",
                    "exit_code": exit_code,
                    "ignored_json": ignored_json,
                    "expected_session_id": expected_session_id,
                    "expected_launch_id": expected_launch_id,
                }
            return {
                "ok": False,
                "reason": "background_no_result",
                "exit_code": exit_code,
            }

        if time.monotonic() >= deadline:
            return {
                "ok": False,
                "reason": "background_timeout",
                "timeout_s": timeout_s,
                "ignored_json": ignored_json,
                "expected_session_id": expected_session_id,
                "expected_launch_id": expected_launch_id,
            }

        time.sleep(poll_interval_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run latch compaction for a Codex thread.")
    ap.add_argument("session_id", nargs="?",
                    help="Codex session/thread id (default: $CODEX_THREAD_ID)")
    ap.add_argument("--project", default=None,
                    help="project path for the KB (default: current working directory)")
    ap.add_argument("--final", action="store_true",
                    help="mark the session summary canonical and ended")
    ap.add_argument("--summarizer", choices=sorted(compactor.SUPPORTED_SUMMARIZER_BACKENDS),
                    default=_default_summarizer_backend(),
                    help="summarizer backend for Codex compact (default: codex)")
    ap.add_argument("--background", action="store_true",
                    help="validate the Codex transcript, then run compaction detached")
    ap.add_argument("--wait", action="store_true",
                    help="with --background, wait for the final child JSON result")
    ap.add_argument("--wait-timeout-s", type=float, default=DEFAULT_WAIT_TIMEOUT_S,
                    help="max seconds to wait with --background --wait")
    ap.add_argument("--poll-interval-s", type=float, default=DEFAULT_POLL_INTERVAL_S,
                    help="seconds between log polls with --background --wait")
    ap.add_argument("--launch-id", default=None,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.wait and not args.background:
        ap.error("--wait requires --background")

    try:
        session_id = codex_transcript.resolve_session_id(args.session_id)
        transcript = codex_transcript.find_transcript(session_id)
    except codex_transcript.CodexTranscriptError as e:
        print(f"codex-latch-compact: {e}", file=sys.stderr)
        return 1

    project = args.project or str(Path.cwd())
    if args.background:
        proc, log_path, start_offset, launch_id = _start_background_process(
            session_id=session_id,
            project=project,
            final=args.final,
            summarizer_backend=args.summarizer,
        )
        result = {
            "ok": True,
            "background": True,
            "pid": proc.pid,
            "session_id": session_id,
            "launch_id": launch_id,
            "log_path": str(log_path),
            "summarizer_backend": args.summarizer,
            "transcript_path": str(transcript),
        }
        if args.wait:
            result.update(
                wait_for_background_result(
                    proc,
                    log_path,
                    start_offset,
                    expected_session_id=session_id,
                    expected_launch_id=launch_id,
                    timeout_s=args.wait_timeout_s,
                    poll_interval_s=args.poll_interval_s,
                )
            )
            result["background"] = True
            result["pid"] = proc.pid
            result["session_id"] = session_id
            result["launch_id"] = launch_id
            result["log_path"] = str(log_path)
            result["transcript_path"] = str(transcript)
        print(json.dumps(result))
        return 0 if result.get("ok") else 1

    result = compactor.run_compaction(
        session_id,
        project,
        str(transcript),
        final=args.final,
        summarizer_backend=args.summarizer,
    )
    result["session_id"] = session_id
    if args.launch_id:
        result["launch_id"] = args.launch_id
    result["transcript_path"] = str(transcript)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
