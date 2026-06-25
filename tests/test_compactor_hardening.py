"""Unit tests for Step 2 — compactor JSON envelope parsing + failure archival.

No subprocess, no claude -p. Pure-Python tests around _parse_json_envelope,
_extract_json_object, and _save_failed_compact."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import compactor as c
import db
import paths


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_envelope_clean():
    inner = {"session_summary": {"title": "t", "body": "b"},
             "extracted_nodes": [], "links": []}
    envelope = json.dumps({"type": "result", "result": json.dumps(inner)})
    obj, err = c._parse_json_envelope(envelope)
    _assert(obj is not None and obj["session_summary"]["title"] == "t", (obj, err))
    print("PASS envelope clean")


def test_raw_no_envelope():
    raw = '{"session_summary":{"title":"x","body":"y"},"extracted_nodes":[]}'
    obj, err = c._parse_json_envelope(raw)
    _assert(obj is not None and obj["session_summary"]["title"] == "x", (obj, err))
    print("PASS raw no envelope")


def test_fenced():
    fenced = '```json\n{"session_summary":{"title":"z"}}\n```'
    obj, err = c._parse_json_envelope(fenced)
    _assert(obj is not None and obj["session_summary"]["title"] == "z", (obj, err))
    print("PASS fenced JSON")


def test_prose_prefix():
    prose = 'Here is the summary:\n{"session_summary":{"title":"q","body":"b"},"extracted_nodes":[]}'
    obj, err = c._parse_json_envelope(prose)
    _assert(obj is not None and obj["session_summary"]["title"] == "q", (obj, err))
    print("PASS prose prefix")


def test_envelope_then_prose_then_json():
    inner_text = 'Here is the summary:\n{"session_summary":{"title":"combo"}}'
    envelope = json.dumps({"type": "result", "result": inner_text})
    obj, err = c._parse_json_envelope(envelope)
    _assert(obj is not None and obj["session_summary"]["title"] == "combo", (obj, err))
    print("PASS envelope + prose + JSON (real-world model output)")


def test_truncated_missing_close():
    # No closing brace — fails at the delimiter-find step.
    truncated = '{"session_summary":{"title":"t","body":"b"'
    obj, err = c._parse_json_envelope(truncated)
    _assert(obj is None and "no JSON object delimiters" in err, (obj, err))
    print(f"PASS truncated missing close ({err[:40]})")


def test_malformed_but_delimited():
    # Has {...} but the content is not valid JSON — fails at json.loads.
    bad = '{"session_summary": {"title": "t", "body": bare-word}}'
    obj, err = c._parse_json_envelope(bad)
    _assert(obj is None and "JSONDecodeError" in err, (obj, err))
    print(f"PASS malformed-but-delimited ({err[:40]})")


def test_empty():
    obj, err = c._parse_json_envelope("")
    _assert(obj is None and err == "empty output", (obj, err))
    print("PASS empty")


def test_no_braces():
    obj, err = c._parse_json_envelope("just prose")
    _assert(obj is None and "no JSON object delimiters" in err, (obj, err))
    print("PASS no braces")


def test_failed_compact_archival():
    tmp = tempfile.mkdtemp(prefix="kb_fail_test_")
    try:
        payload = {"project_path": tmp, "session_id": "test-sess-abc"}
        c._save_failed_compact(payload, "first raw bad", "repair raw bad",
                               reason="first:X;repair:Y")
        fail_dir = paths.project_dir(tmp) / "failed_compact"
        files = list(fail_dir.iterdir())
        _assert(len(files) == 1, f"expected 1 file, got {files}")
        content = files[0].read_text(encoding="utf-8")
        for needle in ("test-sess-abc", "first:X", "first raw bad", "repair raw bad"):
            _assert(needle in content, f"missing {needle!r} in archive")
        print(f"PASS failed_compact archival ({files[0].name})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_compact_subprocess_none():
    """When first-attempt subprocess failed, raw1 is None — archive still succeeds."""
    tmp = tempfile.mkdtemp(prefix="kb_fail_test_")
    try:
        payload = {"project_path": tmp, "session_id": "s2"}
        c._save_failed_compact(payload, None, None, reason="subprocess:timeout")
        fail_dir = paths.project_dir(tmp) / "failed_compact"
        files = list(fail_dir.iterdir())
        _assert(len(files) == 1, f"expected 1 file, got {files}")
        content = files[0].read_text(encoding="utf-8")
        _assert("subprocess failed" in content, "missing subprocess-failed marker")
        print("PASS failed_compact handles None raw output")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_compaction_result_does_not_mark_session_compacted():
    tmp = tempfile.mkdtemp(prefix="kb_empty_compact_test_")
    old_invoke = c._invoke_summarizer
    old_read = c.read_transcript
    old_related = c._related_nodes_brief
    old_attach = c.artifacts.attach_observed_artifacts
    try:
        conn = db.connect(tmp)
        db.upsert_session(conn, "s-empty", tmp, None)
        conn.execute(
            "UPDATE sessions SET turn_count = 7, last_compact_turn = 2 WHERE id = ?",
            ("s-empty",),
        )
        conn.commit()
        conn.close()

        c._invoke_summarizer = lambda *args, **kwargs: {
            "session_summary": {"title": "Empty", "body": ""},
            "extracted_nodes": [],
            "links": [],
        }
        c.read_transcript = lambda path: "[user] do important work"
        c._related_nodes_brief = lambda *args, **kwargs: []
        c.artifacts.attach_observed_artifacts = lambda *args, **kwargs: 0

        out = c._run_compaction_locked(
            "s-empty", tmp, None, final=False, summarizer_backend="codex",
        )
        _assert(out["ok"] is False, out)
        _assert(out["reason"] == "empty_compaction_result", out)
        _assert(out["session_id"] == "s-empty", out)
        _assert(out["summary_node_id"] is None, out)
        _assert(out["inserted_nodes"] == 0, out)
        _assert(out["linked_edges"] == 0, out)

        conn = db.connect(tmp)
        row = conn.execute(
            "SELECT last_compact_turn, summary_node_id FROM sessions WHERE id = ?",
            ("s-empty",),
        ).fetchone()
        _assert(row["last_compact_turn"] == 2, dict(row))
        _assert(row["summary_node_id"] is None, dict(row))
        node_count = conn.execute("SELECT COUNT(*) AS n FROM nodes").fetchone()["n"]
        _assert(node_count == 0, f"expected no nodes, got {node_count}")
        conn.close()
        print("PASS empty_compaction_result_does_not_mark_session_compacted")
    finally:
        c._invoke_summarizer = old_invoke
        c.read_transcript = old_read
        c._related_nodes_brief = old_related
        c.artifacts.attach_observed_artifacts = old_attach
        shutil.rmtree(tmp, ignore_errors=True)


def test_empty_summary_does_not_clobber_prior_summary():
    tmp = tempfile.mkdtemp(prefix="kb_empty_summary_test_")
    try:
        conn = db.connect(tmp)
        sid = "s-prior"
        summary_id = db.insert_node(
            conn, kind="progress", title="Prior", body="keep this", status="staging",
        )
        out = c._apply_compaction(
            conn,
            sid,
            {
                "session_summary": {"title": "Blank", "body": ""},
                "extracted_nodes": [],
                "links": [],
            },
            final=False,
            prior_summary_id=summary_id,
        )
        _assert(out["summary_node_id"] == summary_id, out)
        _assert(out["summary_written"] is False, out)
        row = conn.execute("SELECT title, body FROM nodes WHERE id = ?", (summary_id,)).fetchone()
        _assert(row["title"] == "Prior", dict(row))
        _assert(row["body"] == "keep this", dict(row))
        conn.close()
        print("PASS empty_summary_does_not_clobber_prior_summary")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_envelope_clean()
    test_raw_no_envelope()
    test_fenced()
    test_prose_prefix()
    test_envelope_then_prose_then_json()
    test_truncated_missing_close()
    test_malformed_but_delimited()
    test_empty()
    test_no_braces()
    test_failed_compact_archival()
    test_failed_compact_subprocess_none()
    test_empty_compaction_result_does_not_mark_session_compacted()
    test_empty_summary_does_not_clobber_prior_summary()
    print("\nAll compactor hardening tests pass.")
