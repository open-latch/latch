"""Fail-closed current Cursor session transcript resolution.

Cursor SessionStart supplies both conversation identity and ``transcript_path``.
Latch records that pair in its project marker and resolves only that explicit
handoff.  This module never scans Cursor databases or guesses from recent files.
"""
from __future__ import annotations

from pathlib import Path

import cursor_session


class CursorTranscriptError(RuntimeError):
    pass


def resolve_current(
    project_path: str,
    *,
    session_id: str | None = None,
    transcript_path: str | None = None,
) -> tuple[str, Path]:
    explicit_sid = (session_id or "").strip()
    if not explicit_sid:
        raise CursorTranscriptError(
            "an explicit current Cursor session id is required; use the id "
            "surfaced by the SessionStart hook"
        )
    marker = cursor_session.read_marker(project_path, session_id=explicit_sid)
    if not marker:
        raise CursorTranscriptError(
            f"no SessionStart marker for requested Cursor session {explicit_sid}"
        )
    marker_sid = marker.get("session_id")
    if not isinstance(marker_sid, str) or not marker_sid.strip():
        raise CursorTranscriptError("current Cursor marker has no session id")
    marker_sid = marker_sid.strip()
    if explicit_sid != marker_sid:
        raise CursorTranscriptError(
            f"requested Cursor session {explicit_sid} does not match current session {marker_sid}"
        )

    marker_transcript = marker.get("transcript_path")
    if not isinstance(marker_transcript, str) or not marker_transcript.strip():
        raise CursorTranscriptError(
            "current Cursor marker has no transcript_path; refusing to discover "
            "or guess from undocumented Cursor storage"
        )
    marker_path = Path(marker_transcript).expanduser().resolve()
    if transcript_path:
        explicit_path = Path(transcript_path).expanduser().resolve()
        if explicit_path != marker_path:
            raise CursorTranscriptError(
                f"requested transcript {explicit_path} does not match current Cursor marker {marker_path}"
            )
    if not marker_path.is_file():
        raise CursorTranscriptError(f"current Cursor transcript is not a readable file: {marker_path}")
    return marker_sid, marker_path
