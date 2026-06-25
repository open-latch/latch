"""Unit tests for Codex hook-to-MCP session handoff helpers."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_session  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_write_and_read_session_marker():
    tmp = tempfile.mkdtemp(prefix="codex_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        marker = codex_session.write_marker(
            tmp,
            " session-123 ",
            transcript_path="/tmp/rollout.jsonl",
        )
        _assert(marker == codex_session.marker_path(tmp), marker)
        _assert(marker.exists(), "marker should be written")
        payload = codex_session.read_marker(tmp)
        _assert(payload["session_id"] == "session-123", payload)
        _assert(payload["transcript_path"] == "/tmp/rollout.jsonl", payload)
        _assert(payload["source"] == "codex_session_start", payload)
        _assert(codex_session.read_session_id(tmp) == "session-123", payload)
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS write_and_read_session_marker")


def test_read_session_marker_missing_or_invalid_returns_none():
    tmp = tempfile.mkdtemp(prefix="codex_session_marker_")
    project_dir = paths.project_dir(tmp)
    try:
        _assert(codex_session.read_marker(tmp) is None, "missing marker should be None")
        project_dir.mkdir(parents=True, exist_ok=True)
        codex_session.marker_path(tmp).write_text("{not json", encoding="utf-8")
        _assert(codex_session.read_marker(tmp) is None, "bad marker should be None")
        _assert(codex_session.read_session_id(tmp) is None, "bad marker session id should be None")
    finally:
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS read_session_marker_missing_or_invalid_returns_none")


if __name__ == "__main__":
    test_write_and_read_session_marker()
    test_read_session_marker_missing_or_invalid_returns_none()
    print("\nAll codex_session tests pass.")
