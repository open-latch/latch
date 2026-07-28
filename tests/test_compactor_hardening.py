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


def test_no_source_compaction_skips_before_budget_or_model():
    tmp = tempfile.mkdtemp(prefix="kb_no_source_compact_test_")
    old_budget = c.budget.check_and_record
    old_invoke = c._invoke_summarizer
    try:
        conn = db.connect(tmp)
        db.upsert_session(conn, "s-no-source", tmp, None)
        conn.close()

        def forbidden(*args, **kwargs):
            raise AssertionError("empty session must not spend budget or call model")

        c.budget.check_and_record = forbidden
        c._invoke_summarizer = forbidden
        out = c.run_compaction(
            "s-no-source",
            tmp,
            None,
            final=True,
            summarizer_backend="codex",
        )

        _assert(out["ok"] is True, out)
        _assert(out["skipped"] is True, out)
        _assert(out["reason"] == "no_substantive_turns", out)
        _assert(out["inserted_nodes"] == 0, out)
        _assert(out["summary_written"] is False, out)

        conn = db.connect(tmp)
        sess = db.get_session(conn, "s-no-source")
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        conn.close()
        _assert(sess["ended_at"] is not None, sess)
        _assert(node_count == 0, node_count)
    finally:
        c.budget.check_and_record = old_budget
        c._invoke_summarizer = old_invoke
        shutil.rmtree(tmp, ignore_errors=True)


def test_transcript_user_message_preflight_streams_past_tail_limit():
    tmp = Path(tempfile.mkdtemp(prefix="kb_compact_source_test_"))
    try:
        claude = tmp / "claude.jsonl"
        claude.write_text(
            "\n".join([
                json.dumps({
                    "type": "user",
                    "message": {"role": "user", "content": "do real work"},
                }),
                *[
                    json.dumps({
                        "type": "assistant",
                        "message": {"content": "x" * 500},
                    })
                    for _ in range(400)
                ],
            ]) + "\n",
            encoding="utf-8",
        )
        codex = tmp / "rollout-session.jsonl"
        codex.write_text(
            "\n".join([
                json.dumps({
                    "type": "session_meta",
                    "payload": {"id": "session"},
                }),
                json.dumps({
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "do real work"},
                }),
                *[
                    json.dumps({
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "x" * 500,
                        },
                    })
                    for _ in range(400)
                ],
            ]) + "\n",
            encoding="utf-8",
        )
        cursor = tmp / "cursor.jsonl"
        cursor.write_text(
            "\n".join([
                json.dumps({
                    "type": "message",
                    "message": {
                        "role": "human",
                        "content": "do real Cursor work",
                    },
                }),
                *[
                    json.dumps({
                        "type": "assistant",
                        "message": {"content": "x" * 500},
                    })
                    for _ in range(400)
                ],
            ]) + "\n",
            encoding="utf-8",
        )
        metadata_only = tmp / "metadata-only.jsonl"
        metadata_only.write_text(
            json.dumps({"type": "system", "message": {"content": "setup"}}) + "\n",
            encoding="utf-8",
        )
        tool_result_only = tmp / "tool-result-only.jsonl"
        tool_result_only.write_text(
            json.dumps({
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "content": "not a prompt"}],
                },
            }) + "\n",
            encoding="utf-8",
        )
        sdk_only = tmp / "sdk-only.jsonl"
        sdk_only.write_text(
            json.dumps({
                "type": "user",
                "promptSource": "sdk",
                "message": {"role": "user", "content": "synthetic prompt"},
            }) + "\n",
            encoding="utf-8",
        )
        nested_role = tmp / "nested-role.jsonl"
        nested_role.write_text(
            json.dumps({
                "type": "message",
                "message": {"role": "human", "content": "real Cursor prompt"},
            }) + "\n",
            encoding="utf-8",
        )
        metadata_free_codex = tmp / "metadata-free-codex.jsonl"
        metadata_free_codex.write_text(
            json.dumps({
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "unbound prompt"},
            }) + "\n",
            encoding="utf-8",
        )

        _assert("[user] do real work" not in c.read_transcript(claude), claude)
        _assert("[user] do real work" not in c.read_transcript(codex), codex)
        _assert("do real Cursor work" not in c.read_transcript(cursor), cursor)
        _assert(c._transcript_has_user_message(str(claude)), claude)
        _assert(c._transcript_has_user_message(str(codex)), codex)
        _assert(c._transcript_has_user_message(str(cursor)), cursor)
        _assert(not c._transcript_has_user_message(str(metadata_only)), metadata_only)
        _assert(
            not c._transcript_has_user_message(str(tool_result_only)),
            tool_result_only,
        )
        _assert(not c._transcript_has_user_message(str(sdk_only)), sdk_only)
        _assert(c._transcript_has_user_message(str(nested_role)), nested_role)
        _assert(
            not c._transcript_has_user_message(str(metadata_free_codex)),
            metadata_free_codex,
        )
        _assert(not c._transcript_has_user_message(str(tmp / "missing")), "missing")

        project = tmp / "project"
        project.mkdir()
        conn = db.connect(str(project))
        transcripts = {
            "long-claude-session": claude,
            "long-codex-session": codex,
            "long-cursor-session": cursor,
        }
        for session_id, transcript in transcripts.items():
            db.upsert_session(
                conn,
                session_id,
                str(project),
                str(transcript),
            )
        conn.close()
        for session_id, transcript in transcripts.items():
            source = c._compaction_source_preflight(
                session_id,
                str(project),
                str(transcript),
                final=True,
            )
            conn = db.connect(str(project))
            session = db.get_session(conn, session_id)
            conn.close()
            _assert(source["has_source"] is True, (session_id, source))
            _assert(session["ended_at"] is None, (session_id, session))
    finally:
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


def test_compactor_hard_allowlist_and_lifecycle_relation_boundary():
    tmp = tempfile.mkdtemp(prefix="kb_compactor_boundary_test_")
    try:
        conn = db.connect(tmp)
        destination = db.insert_node(
            conn, kind="decision", title="Existing target", body="target",
        )
        allowed_kinds = [
            "fact", "decision", "progress", "entity", "preference",
            "open_question", "idea",
        ]
        extracted = [
            {"kind": kind, "title": f"Allowed {kind}", "body": f"body {kind}"}
            for kind in allowed_kinds
        ] + [
            {"kind": kind, "title": f"Rejected {kind}", "body": "must not persist"}
            for kind in ("workstream", "priority", "summary", "unsupported")
        ] + [
            {"title": "Rejected missing kind", "body": "must not persist"},
            {"kind": [], "title": "Rejected nonstring kind", "body": "must not persist"},
        ]
        links = [
            {
                "src_title": "Allowed fact",
                "dst_id": destination,
                "relation": relation,
            }
            for relation in (
                "related_to", "merged_into", "closed_in_favor_of", "branched_from",
            )
        ]

        result = c._apply_compaction(
            conn,
            "boundary-session",
            {
                "session_summary": {"title": "No summary", "body": ""},
                "extracted_nodes": extracted,
                "links": links,
            },
            final=False,
            prior_summary_id=None,
        )

        _assert(result["inserted_nodes"] == len(allowed_kinds), result)
        _assert(result["linked_edges"] == 1, result)
        stored = conn.execute(
            "SELECT kind,title FROM nodes WHERE title LIKE 'Allowed %' ORDER BY kind"
        ).fetchall()
        _assert({row["kind"] for row in stored} == set(allowed_kinds), stored)
        rejected = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE title LIKE 'Rejected %'"
        ).fetchone()[0]
        _assert(rejected == 0, f"unsupported extracted nodes persisted: {rejected}")
        relations = {
            row["relation"] for row in conn.execute(
                "SELECT relation FROM edges WHERE src=(SELECT id FROM nodes "
                "WHERE title='Allowed fact')"
            ).fetchall()
        }
        _assert(relations == {"related_to"}, relations)
        _assert(
            not c._has_compaction_content({
                "session_summary": {"body": ""},
                "extracted_nodes": [{
                    "kind": "workstream", "title": "bad", "body": "bad",
                }],
            }),
            "unsupported extracted kind must not qualify as compaction content",
        )
        conn.close()
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
    test_no_source_compaction_skips_before_budget_or_model()
    test_transcript_user_message_preflight_streams_past_tail_limit()
    test_empty_summary_does_not_clobber_prior_summary()
    test_compactor_hard_allowlist_and_lifecycle_relation_boundary()
    print("\nAll compactor hardening tests pass.")
