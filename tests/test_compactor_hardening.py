"""Unit tests for Step 2 — compactor JSON envelope parsing + failure archival.

No subprocess, no claude -p. Pure-Python tests around _parse_json_envelope,
_extract_json_object, and _save_failed_compact."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.gate import predicate
from latch.gate import predicate_policy
from latch.pipeline import compactor as c
from latch.pipeline import heal
from latch.store import db
from latch.store import paths


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



# ---------- authority edges against ratified judgments ----------

_POLICY_DOMAIN = "domain:synthetic-compactor"
_BLOCKED_PATH = "src/blocked.py"


@pytest.fixture()
def policy_conn(tmp_path):
    connection = db.connect(str(tmp_path))
    try:
        yield connection
    finally:
        connection.close()


def _policy_owner(conn, *, ratified: bool) -> int:
    """A canonical decision owning one declared, domain-bound rejection."""
    owner_id = db.insert_node(
        conn,
        kind="decision",
        title="Synthetic ruling: never edit the blocked module",
        body="Synthetic authority fixture for compactor edge writes.",
        status="canonical",
    )
    if ratified:
        db.insert_ratification_nc(
            conn,
            owner_id,
            ratifier="fixture:founder",
            decided_at="2026-08-27 08:00:00",
            action="ratify",
            source="capture_decision",
        )
    row_id = db.insert_rejected_path_nc(
        conn,
        owner_id,
        option="synthetic option: edit the blocked module",
        reason="synthetic reason: the module is retired",
        ratifier="fixture:founder",
        decided_at="2026-08-27 08:00:00",
        scope_predicate=f"file:{_BLOCKED_PATH}",
        source="declared",
        policy_domain_id=_POLICY_DOMAIN,
    )
    assert row_id is not None
    conn.commit()
    return owner_id


def _blocked_path_verdict(conn) -> tuple[object, str]:
    projection = predicate_policy.project_policy_domain(conn, _POLICY_DOMAIN)
    verdict = predicate.evaluate(
        predicate.compile_predicates(projection.binding_rows),
        predicate.ToolCallContext(
            policy_domain_id=_POLICY_DOMAIN,
            project_root="/repo/synthetic-compactor",
            cwd="/repo/synthetic-compactor",
            tool_name="synthetic-edit",
            proposed_file_paths=(_BLOCKED_PATH,),
            diff_paths=(),
            staged_paths=(),
            import_names=(),
            api_names=(),
            evidence_complete=True,
            evidence_provenance={"fixture": "test_compactor_hardening"},
        ),
    )
    return projection, str(verdict["decision"])


def _compact_with_link(conn, owner_id: int, relation: str) -> dict:
    return c._apply_compaction(
        conn,
        "authority-edge-session",
        {
            "session_summary": {"title": "No summary", "body": ""},
            "extracted_nodes": [{
                "kind": "decision",
                "title": "Replacement ruling",
                "body": "Synthetic replacement proposed by the summarizer.",
            }],
            "links": [{
                "src_title": "Replacement ruling",
                "dst_id": owner_id,
                "relation": relation,
            }],
        },
        final=False,
        prior_summary_id=None,
    )


def _replacement_id(conn) -> int:
    row = conn.execute(
        "SELECT id FROM nodes WHERE title = 'Replacement ruling'"
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _authority_edges(conn, left_id: int, right_id: int) -> list[tuple]:
    rows = conn.execute(
        "SELECT src, dst, relation FROM edges "
        "WHERE status = 'active' "
        "AND relation IN ('supersedes', 'replaces', 'reconciled_by') "
        "AND ((src = ? AND dst = ?) OR (src = ? AND dst = ?))",
        (left_id, right_id, right_id, left_id),
    ).fetchall()
    return [(int(r["src"]), int(r["dst"]), r["relation"]) for r in rows]


def _referral_ids(conn, left_id: int, right_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT q.id
        FROM nodes q
        JOIN edges a ON a.src = q.id AND a.dst = ?
             AND a.relation = 'related_to' AND a.status = 'active'
        JOIN edges b ON b.src = q.id AND b.dst = ?
             AND b.relation = 'related_to' AND b.status = 'active'
        WHERE q.kind = 'open_question' AND q.status = 'staging'
        ORDER BY q.id
        """,
        (left_id, right_id),
    ).fetchall()
    return [int(r["id"]) for r in rows]


@pytest.mark.parametrize("relation", ["supersedes", "replaces", "replaced_by"])
def test_compactor_link_cannot_demote_ratified_policy(policy_conn, relation):
    conn = policy_conn
    owner_id = _policy_owner(conn, ratified=True)
    before, before_decision = _blocked_path_verdict(conn)
    assert len(before.binding_rows) == 1, before
    assert before_decision == "block"

    result = _compact_with_link(conn, owner_id, relation)

    after, after_decision = _blocked_path_verdict(conn)
    assert after.advisory_rows == (), after
    assert [row.rejected_path_id for row in after.binding_rows] == [
        row.rejected_path_id for row in before.binding_rows
    ], after
    assert after_decision == "block"
    assert db.node_is_ratified(conn, owner_id)
    replacement_id = _replacement_id(conn)
    assert _authority_edges(conn, replacement_id, owner_id) == []
    assert result["linked_edges"] == 0, result


@pytest.mark.parametrize(
    "relation, canonical",
    [
        ("supersedes", "supersedes"),
        ("replaces", "replaces"),
        ("reconciled_by", "reconciled_by"),
    ],
)
def test_compactor_link_to_ratified_judgment_refers_to_human(
    policy_conn, monkeypatch, relation, canonical,
):
    conn = policy_conn
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        heal.log_utils,
        "emit_event",
        lambda event_type, row, **_kwargs: events.append((event_type, row)),
    )
    owner_id = _policy_owner(conn, ratified=True)

    result = _compact_with_link(conn, owner_id, relation)

    replacement_id = _replacement_id(conn)
    assert _authority_edges(conn, replacement_id, owner_id) == []
    assert result["human_referrals"] == 1, result
    referrals = _referral_ids(conn, *sorted((replacement_id, owner_id)))
    assert len(referrals) == 1, referrals
    referral_events = [
        row for event_type, row in events
        if event_type == "heal_human_referral"
    ]
    assert len(referral_events) == 1, events
    assert referral_events[0]["trigger"] == f"compactor_{canonical}"
    assert referral_events[0]["ratified_node_ids"] == [owner_id]
    # The referral's parking edge keeps nightly heal from arbitrating the
    # pair without the human.
    assert heal.edge_exists_between(conn, replacement_id, owner_id)


def test_compactor_link_to_unratified_judgment_still_writes_edge(policy_conn):
    conn = policy_conn
    owner_id = _policy_owner(conn, ratified=False)

    result = _compact_with_link(conn, owner_id, "supersedes")

    replacement_id = _replacement_id(conn)
    assert _authority_edges(conn, replacement_id, owner_id) == [
        (replacement_id, owner_id, "supersedes"),
    ]
    assert result["linked_edges"] == 1, result
    assert result["human_referrals"] == 0, result
    assert _referral_ids(conn, *sorted((replacement_id, owner_id))) == []


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
    test_compactor_hard_allowlist_and_lifecycle_relation_boundary()
    print("\nAll compactor hardening tests pass.")
