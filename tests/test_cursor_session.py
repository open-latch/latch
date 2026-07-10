"""Unit tests for Cursor session marker handoff."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cursor_session  # noqa: E402
import paths  # noqa: E402


def test_cursor_session_marker_round_trip():
    tmp = tempfile.mkdtemp(prefix="cursor-session-marker-")
    project_dir = paths.project_dir(tmp)
    try:
        marker = cursor_session.write_marker(tmp, " cursor-conversation ")
        assert marker == cursor_session.marker_path(tmp)
        payload = cursor_session.read_marker(tmp)
        assert payload["source"] == "cursor_session_start"
        assert cursor_session.read_session_id(tmp) == "cursor-conversation"
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
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
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
