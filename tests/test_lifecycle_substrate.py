from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.store import db  # noqa: E402
from latch.store import lifecycle_signals  # noqa: E402
from latch.store import paths  # noqa: E402
from latch.store import schema_version  # noqa: E402


def _connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    path = paths.project_dir(str(tmp_path)) / "kb.db"
    monkeypatch.setattr(db, "db_path", lambda _cwd=None: path)
    monkeypatch.setattr(
        db,
        "ensure_project_dir",
        lambda _cwd=None: path.parent.mkdir(parents=True, exist_ok=True),
    )
    return db.connect(str(tmp_path))


def _node(conn: sqlite3.Connection, *, kind: str = "fact", workstream_id=None) -> int:
    return db.insert_node(
        conn,
        kind=kind,
        title=f"{kind} node",
        body="body",
        status="canonical",
        workstream_id=workstream_id,
    )


def _open_payload() -> dict:
    return {"assigned_member_ids": [], "watch_pair": None, "probation": {}}


def test_current_schema_tables_and_reconnect_are_idempotent(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "retrieval_events",
        "workstream_ops",
        "workstream_derivations",
        "workstream_derivation_candidates",
        "workstream_op_events",
    } <= names
    assert schema_version.read(conn) == schema_version.KB_SCHEMA_VERSION
    conn.close()
    reopened = db.connect(str(tmp_path))
    assert schema_version.read(reopened) == schema_version.KB_SCHEMA_VERSION
    reopened.close()


def test_retrieval_events_capture_event_time_lane_and_graph_pair(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _node(conn, kind="workstream")
    member = _node(conn, workstream_id=lane)
    assert db.record_retrievals(
        conn,
        session_id="S1",
        turn=3,
        items=[(member, 0.75)],
        source="graph",
        event_details={member: {"seed_node_id": lane, "reached_node_id": member}},
    ) == 1
    event = dict(conn.execute("SELECT * FROM retrieval_events").fetchone())
    assert event["workstream_id_at_event"] == lane
    assert event["seed_node_id"] == lane
    assert event["reached_node_id"] == member
    db.set_node_workstream(conn, [member], None)
    assert event["workstream_id_at_event"] == lane
    conn.close()


def test_retrieval_event_failure_preserves_primary_and_counts_drop(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    member = _node(conn)

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("event table unavailable")

    monkeypatch.setattr(db, "_insert_retrieval_events_nc", fail)
    assert db.record_retrievals(
        conn, session_id="S1", turn=1, items=[(member, 0.9)], source="prompt",
    ) == 1
    assert conn.execute("SELECT COUNT(*) FROM session_retrievals").fetchone()[0] == 1
    assert db.retrieval_events_dropped(conn) == 1
    conn.close()


def test_project_event_and_ninety_day_pruning(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    member = _node(conn)
    db.record_retrieval_events(
        conn, source="tool", items=[(member, None)], ts="2025-01-01 00:00:00",
    )
    db.record_retrieval_events(
        conn, source="tool", items=[(member, None)], ts="2025-04-02 00:00:00",
    )
    assert db.prune_retrieval_events(
        conn, retention_days=90, now="2025-04-02 00:00:00",
    ) == 1
    assert conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0] == 1
    conn.close()


def test_candidate_and_auto_op_keys_are_canonical():
    one = lifecycle_signals.make_candidate_key("merge", [8, 3, 8])
    two = lifecycle_signals.make_candidate_key("MERGE", [3, 8])
    assert one == two
    opened = lifecycle_signals.make_candidate_key("open", [], ["b", "a", "a"])
    assert opened == lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b"])
    assert lifecycle_signals.make_auto_op_key("merge", one, {"end": 40}) \
        == lifecycle_signals.make_auto_op_key("MERGE", two, {"end": 40})
    assert lifecycle_signals.make_auto_op_key("MERGE", two, {"end": 41}) \
        != lifecycle_signals.make_auto_op_key("MERGE", two, {"end": 40})
    with pytest.raises(ValueError):
        lifecycle_signals.make_candidate_key("OPEN", [], [])


def test_merge_payload_binding_is_semantic_and_directional():
    signal = {"left": 3, "right": 8}
    one = lifecycle_signals.make_candidate_payload_binding(
        "MERGE",
        signal,
        request={
            "source_workstream_id": 3,
            "absorber_workstream_id": 8,
            "dispositions": {"11": {"action": "keep"}},
        },
    )
    equivalent = lifecycle_signals.make_candidate_payload_binding(
        "merge",
        {"left": 8, "right": 3},
        request={
            "src_workstream_id": 3,
            "dst_workstream_id": 8,
            "dispositions": {11: "preserve"},
        },
    )
    reversed_direction = lifecycle_signals.make_candidate_payload_binding(
        "MERGE",
        signal,
        request={
            "source_workstream_id": 8,
            "absorber_workstream_id": 3,
            "dispositions": {11: "preserve"},
        },
    )
    assert one == equivalent
    assert one != reversed_direction
    base = lifecycle_signals.make_candidate_key("MERGE", [3, 8])
    assert lifecycle_signals.candidate_evidence_key(base, one) \
        == lifecycle_signals.candidate_evidence_key(base, equivalent)


def test_workstream_op_ledger_is_idempotent_cas_and_payload_checked(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    created = db.begin_workstream_op(
        conn, op_key="op-1", op="OPEN", origin="manual", payload=_open_payload(),
    )
    assert created["created"] is True
    duplicate = db.begin_workstream_op(
        conn, op_key="op-1", op="open", origin="manual", payload=_open_payload(),
    )
    assert duplicate["created"] is False
    with pytest.raises(db.WorkstreamLedgerConflictError):
        db.begin_workstream_op(
            conn, op_key="op-1", op="OPEN", origin="auto", payload=_open_payload(),
        )
    applied = db.finish_workstream_op(conn, "op-1", state="applied")
    assert applied["state"] == "applied" and applied["applied_at"]
    assert db.finish_workstream_op(conn, "op-1", state="applied")["state"] == "applied"
    with pytest.raises(db.WorkstreamLedgerStateError):
        db.finish_workstream_op(conn, "op-1", state="failed", error_code="internal")

    db.begin_workstream_op(
        conn, op_key="op-bad", op="OPEN", origin="manual", payload={},
    )
    with pytest.raises(db.WorkstreamPayloadError):
        db.finish_workstream_op(conn, "op-bad", state="applied")
    assert db.get_workstream_op(conn, "op-bad")["state"] == "pending"
    failed = db.finish_workstream_op(
        conn, "op-bad", state="failed", error_code="payload_insufficient",
    )
    assert failed["state"] == "failed"
    conn.close()


def test_reversal_payload_validation_requires_relationally_complete_snapshots():
    with pytest.raises(db.WorkstreamPayloadError, match="exactly cover"):
        db.validate_workstream_reversal_payload(
            "CLOSE",
            {
                "feeder_disposition_edge_ids": [],
                "focus": None,
                "retired_priority_ids": [7],
                "priority_snapshots": [],
            },
        )
    with pytest.raises(db.WorkstreamPayloadError, match="complete reversal rows"):
        db.validate_workstream_reversal_payload(
            "CLOSE",
            {
                "feeder_disposition_edge_ids": [],
                "focus": None,
                "retired_priority_ids": [7],
                "priority_snapshots": [{"id": 7, "status": "canonical"}],
            },
        )

    merge_payload = {
        "repointed_member_ids": [],
        "prior_memberships": {},
        "rehomed_edge_ids": [],
        "tombstoned_edge_ids": [],
        "edge_rehomes": [],
        "retired_priority_ids": [],
        "readded_priority_ids": [],
        "overflow_retired_priority_ids": [],
        "priority_map": [],
        "priority_snapshots": [],
        "created_priority_snapshots": [],
        "src_focus": None,
        "dst_focus": None,
        "post_focus": None,
        "rolling_line": "merged",
        "rolling_op_key": "merge:test",
        "absorber_body_before": "body",
        "absorber_body_before_hash": "before",
        "absorber_body_after_hash": "after",
        "source_body_hash": "source",
        "source_title": "Source",
        "source_prior_status": "staging",
        "merge_edge_id": 9,
    }
    db.validate_workstream_reversal_payload("MERGE", merge_payload)
    merge_payload["repointed_member_ids"] = [11]
    with pytest.raises(db.WorkstreamPayloadError, match="prior_memberships"):
        db.validate_workstream_reversal_payload("MERGE", merge_payload)


def test_nc_node_edge_membership_and_focus_changes_roll_back(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _node(conn, kind="workstream")
    member = _node(conn)
    conn.execute("BEGIN")
    transient = db.insert_node_nc(conn, kind="fact", title="temp", body="temp")
    db.set_node_workstream_nc(conn, [member], lane)
    edge_id = db.add_edge_nc(conn, member, lane, "related_to", created_by="lifecycle:test")
    db.bump_focus_nc(conn, lane, delta=2.0, set_by="lifecycle:test")
    conn.rollback()
    assert db.get_node(conn, transient) is None
    assert db.get_node(conn, member)["workstream_id"] is None
    assert conn.execute("SELECT 1 FROM edges WHERE id = ?", (edge_id,)).fetchone() is None
    assert db.get_focus_row(conn, lane) is None

    db.bump_focus(conn, lane, delta=2.0)
    captured = db.get_focus_row(conn, lane)
    conn.execute("BEGIN")
    db.set_focus_score_nc(conn, lane, 0.0)
    db.set_focus_pinned_nc(conn, lane, True)
    db.delete_focus_row_nc(conn, lane)
    db.restore_focus_row_nc(conn, captured)
    conn.commit()
    assert db.get_focus_row(conn, lane) == captured
    conn.close()


def test_derivation_snapshot_and_latest_candidate_event_guard(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    candidate = lifecycle_signals.make_candidate_key("MERGE", [1, 2])
    first = db.record_workstream_derivation(
        conn,
        derivation_key="d1",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{"candidate_key": candidate, "op": "MERGE", "signal": {"n": 4}}],
    )
    assert first["created"] is True
    event = db.append_workstream_op_event(
        conn,
        event_key="e1",
        candidate_key=candidate,
        event_type="attestation",
        verdict="agree",
        derivation_key="d1",
        require_latest_candidate=True,
    )
    assert event["created"] is True
    assert db.append_workstream_op_event(
        conn,
        event_key="e1",
        candidate_key=candidate,
        event_type="attestation",
        verdict="agree",
        derivation_key="d1",
        require_latest_candidate=True,
    )["created"] is False
    db.record_workstream_derivation(
        conn,
        derivation_key="d2",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[],
    )
    with pytest.raises(db.WorkstreamLedgerStateError):
        db.append_workstream_op_event(
            conn,
            event_key="e2",
            candidate_key=candidate,
            event_type="attestation",
            verdict="agree",
            derivation_key="d1",
            require_latest_candidate=True,
        )
    conn.close()


def test_contacts_exclude_turn_zero_null_session_and_focus_gate(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _node(conn, kind="workstream")
    member = _node(conn, workstream_id=lane)
    db.record_retrievals(
        conn, session_id="S1", turn=0, items=[(member, None)], source="session_start",
    )
    db.record_retrievals(
        conn, session_id="S1", turn=1, items=[(member, 0.9)], source="prompt",
    )
    db.record_retrieval_events(conn, source="tool", items=[(member, None)])
    lifecycle_signals.record_write_contact(conn, session_id="S2", node_id=member)
    lifecycle_signals.record_gate_contacts(
        conn,
        session_id="S3",
        turn=2,
        chain_assembly={
            "seeds": [
                {"id": member, "source": "focus", "score": 1.0},
                {"id": member, "source": "hybrid", "score": 0.8},
            ],
            "chains": [{"seed_id": member, "evidence": []}],
        },
    )
    contacts = lifecycle_signals.list_session_workstream_contacts(conn)
    assert {(row["session_id"], row["workstream_id"]) for row in contacts} == {
        ("S1", lane), ("S2", lane), ("S3", lane),
    }
    assert "session_start" not in {
        source for row in contacts for source in row["sources"]
    }
    rows = [dict(row) for row in conn.execute(
        "SELECT id,session_id,turn,source FROM retrieval_events ORDER BY id"
    ).fetchall()]
    python_ids = {
        int(row["id"])
        for row in rows
        if lifecycle_signals.is_eligible_contact_event(row)
    }
    sql_ids = {
        int(row["id"])
        for row in conn.execute(
            "SELECT id FROM retrieval_events WHERE "
            + lifecycle_signals.eligible_contact_sql()
        ).fetchall()
    }
    assert python_ids == sql_ids
    conn.close()
