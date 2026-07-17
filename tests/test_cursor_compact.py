"""Tests for fail-closed current-session Cursor compaction."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import compactor  # noqa: E402
import cursor_compact  # noqa: E402
import cursor_session  # noqa: E402
import cursor_transcript  # noqa: E402
import paths  # noqa: E402


def _tmp():
    root = Path(tempfile.mkdtemp(prefix="cursor-compact-"))
    project_dir = paths.project_dir(str(root))
    return root, project_dir


def _transcript(root: Path) -> Path:
    path = root / "current-cursor.jsonl"
    rows = [
        {"type": "user", "message": {"content": "remember this decision"}},
        {"type": "assistant", "message": {"content": "captured"}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_resolve_current_uses_only_explicit_scoped_marker_pair():
    root, project_dir = _tmp()
    try:
        transcript = _transcript(root)
        cursor_session.write_marker(str(root), "cursor-session", transcript_path=str(transcript))
        sid, path = cursor_transcript.resolve_current(
            str(root), session_id="cursor-session", transcript_path=str(transcript),
        )
        assert sid == "cursor-session" and path == transcript.resolve()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_resolve_current_fails_closed_without_exact_marker_pair():
    root, project_dir = _tmp()
    try:
        transcript = _transcript(root)
        try:
            cursor_transcript.resolve_current(str(root))
        except cursor_transcript.CursorTranscriptError as e:
            assert "explicit current Cursor session id" in str(e)
        else:
            raise AssertionError("missing marker must fail")

        cursor_session.write_marker(str(root), "cursor-session", transcript_path=str(transcript))
        for kwargs, expected in [
            ({"session_id": "other"}, "no current-session marker for requested"),
            ({
                "session_id": "cursor-session",
                "transcript_path": str(root / "other.jsonl"),
            }, "does not match current Cursor marker"),
        ]:
            try:
                cursor_transcript.resolve_current(str(root), **kwargs)
            except cursor_transcript.CursorTranscriptError as e:
                assert expected in str(e)
            else:
                raise AssertionError(f"expected failure for {kwargs}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_resolve_current_refuses_marker_without_transcript_path():
    root, project_dir = _tmp()
    try:
        cursor_session.write_marker(str(root), "cursor-session")
        try:
            cursor_transcript.resolve_current(str(root), session_id="cursor-session")
        except cursor_transcript.CursorTranscriptError as e:
            assert "undocumented Cursor storage" in str(e)
        else:
            raise AssertionError("missing transcript_path must fail")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_cursor_compact_main_passes_current_pair_to_shared_compactor(capsys):
    root, project_dir = _tmp()
    original = cursor_compact.compactor.run_compaction
    captured = {}
    try:
        transcript = _transcript(root)
        cursor_session.write_marker(str(root), "cursor-session", transcript_path=str(transcript))

        def fake_run(session_id, project_path, transcript_path, **kwargs):
            captured.update({
                "session_id": session_id,
                "project_path": project_path,
                "transcript_path": transcript_path,
                **kwargs,
            })
            return {"ok": True, "summary_node_id": 42}

        cursor_compact.compactor.run_compaction = fake_run
        rc = cursor_compact.main([
            "cursor-session", "--project", str(root), "--summarizer", "cursor",
        ])
        assert rc == 0
        output = json.loads(capsys.readouterr().out)
        assert output["current_session_only"] is True
        assert captured["session_id"] == "cursor-session"
        assert captured["summarizer_backend"] == "cursor"
        assert captured["transcript_path"] == str(transcript.resolve())
    finally:
        cursor_compact.compactor.run_compaction = original
        shutil.rmtree(root, ignore_errors=True)


def test_shared_compactor_flattens_hook_provided_cursor_jsonl():
    root, project_dir = _tmp()
    try:
        transcript = _transcript(root)
        text = compactor.read_transcript(transcript)
        assert "[user] remember this decision" in text
        assert "[assistant] captured" in text
    finally:
        shutil.rmtree(root, ignore_errors=True)
