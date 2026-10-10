"""Unit tests for Cursor session marker handoff."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.hosts import cursor_session  # noqa: E402
from latch.hosts import cursor_transcript  # noqa: E402
from latch.store import paths  # noqa: E402


def test_cursor_session_marker_round_trip():
    tmp = tempfile.mkdtemp(prefix="cursor-session-marker-")
    project_dir = paths.project_dir(tmp)
    try:
        marker = cursor_session.write_marker(tmp, " cursor-conversation ")
        assert marker == cursor_session.marker_path(tmp)
        payload = cursor_session.read_marker(tmp)
        assert payload["source"] == "cursor_session_start"
        assert cursor_session.read_session_id(tmp) == "cursor-conversation"
        assert cursor_session.read_marker(
            tmp, session_id="cursor-conversation",
        ) == payload
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cursor_session_markers_are_scoped_across_interleaved_conversations():
    tmp = tempfile.mkdtemp(prefix="cursor-session-scoped-")
    project_dir = paths.project_dir(tmp)
    try:
        cursor_session.write_marker(tmp, "conversation-a", transcript_path="/tmp/a.jsonl")
        cursor_session.write_marker(tmp, "conversation-b", transcript_path="/tmp/b.jsonl")
        assert cursor_session.read_marker(tmp)["session_id"] == "conversation-b"
        assert cursor_session.read_marker(
            tmp, session_id="conversation-a",
        )["transcript_path"] == "/tmp/a.jsonl"
        assert cursor_session.read_marker(
            tmp, session_id="conversation-b",
        )["transcript_path"] == "/tmp/b.jsonl"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_late_transcript_refresh_fills_once_and_rejects_changes():
    tmp = tempfile.mkdtemp(prefix="cursor-session-late-transcript-")
    project_dir = paths.project_dir(tmp)
    try:
        cursor_session.write_marker(tmp, "conversation", transcript_path=None)
        cursor_session.refresh_transcript_path(tmp, "conversation", "/tmp/current.jsonl")
        marker = cursor_session.read_marker(tmp, session_id="conversation")
        assert marker is not None
        assert marker["transcript_path"] == "/tmp/current.jsonl"

        cursor_session.refresh_transcript_path(tmp, "conversation", "/tmp/current.jsonl")
        try:
            cursor_session.refresh_transcript_path(tmp, "conversation", "/tmp/other.jsonl")
        except ValueError as exc:
            assert "changed within one session" in str(exc)
        else:
            raise AssertionError("expected a changed transcript path to fail closed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cursor_session_marker_missing_or_invalid():
    tmp = tempfile.mkdtemp(prefix="cursor-session-marker-")
    project_dir = paths.project_dir(tmp)
    try:
        assert cursor_session.read_marker(tmp) is None
        cursor_session.marker_path(tmp).parent.mkdir(parents=True, exist_ok=True)
        cursor_session.marker_path(tmp).write_text("{bad", encoding="utf-8")
        assert cursor_session.read_session_id(tmp) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cursor_transcript_resolution_is_exact_and_fail_closed():
    tmp = Path(tempfile.mkdtemp(prefix="cursor-session-transcript-"))
    project_dir = paths.project_dir(tmp)
    transcript = tmp / "current.jsonl"
    transcript.write_text('{"type":"user","message":"hi"}\n', encoding="utf-8")
    try:
        cursor_session.write_marker(tmp, "cursor-conversation", transcript_path=str(transcript))
        sid, resolved = cursor_transcript.resolve_current(
            str(tmp), session_id="cursor-conversation", transcript_path=str(transcript),
        )
        assert sid == "cursor-conversation"
        assert resolved == transcript.resolve()

        for kwargs in (
            {"session_id": "other"},
            {"transcript_path": str(tmp / "other.jsonl")},
        ):
            try:
                cursor_transcript.resolve_current(str(tmp), **kwargs)
            except cursor_transcript.CursorTranscriptError:
                pass
            else:
                raise AssertionError(f"expected fail-closed mismatch for {kwargs}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
