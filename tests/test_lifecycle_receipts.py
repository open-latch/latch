from __future__ import annotations

import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import lifecycle_receipts  # noqa: E402
import maintenance  # noqa: E402
import paths  # noqa: E402


def _connect(tmp_path: Path, monkeypatch) -> sqlite3.Connection:
    path = paths.project_dir(str(tmp_path)) / "kb.db"
    monkeypatch.setattr(db, "db_path", lambda _cwd=None: path)
    monkeypatch.setattr(
        db,
        "ensure_project_dir",
        lambda _cwd=None: path.parent.mkdir(parents=True, exist_ok=True),
    )
    return db.connect(str(tmp_path))


def test_restore_log_reconciliation_marks_missing_applied_key_terminal(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    monkeypatch.setattr(
        lifecycle_receipts.log_utils,
        "read_log_range",
        lambda *_args, **_kwargs: iter([{
            "ts": "2026-07-21T12:00:00.000Z",
            "session_id": "S1",
            "op_key": "lost-merge-key",
            "candidate_key": "candidate:merge",
            "op": "MERGE",
            "state": "applied",
            "workstream_id": 42,
        }]),
    )

    first = lifecycle_receipts.reconcile_orphaned_restore_ops(
        conn,
        project_path=str(tmp_path),
        now=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    assert first["orphaned_by_restore_count"] == 1
    row = db.get_workstream_op(conn, "lost-merge-key")
    assert row is not None
    assert row["state"] == "orphaned_by_restore"
    assert row["error_code"] == "orphaned_by_restore"
    assert row["candidate_key"] == "candidate:merge"

    second = lifecycle_receipts.reconcile_orphaned_restore_ops(
        conn,
        project_path=str(tmp_path),
        now=datetime(2026, 7, 22, tzinfo=timezone.utc),
    )
    assert second["orphaned_by_restore_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM workstream_ops WHERE op_key='lost-merge-key'"
    ).fetchone()[0] == 1
    conn.close()


def test_restore_log_reconciliation_ignores_non_applied_and_unknown_ops(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    monkeypatch.setattr(
        lifecycle_receipts.log_utils,
        "read_log_range",
        lambda *_args, **_kwargs: iter([
            {"op_key": "failed", "op": "OPEN", "state": "failed"},
            {"op_key": "unknown", "op": "SPLIT", "state": "applied"},
        ]),
    )
    result = lifecycle_receipts.reconcile_orphaned_restore_ops(conn)
    assert result["orphaned_by_restore_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM workstream_ops").fetchone()[0] == 0
    conn.close()


def test_legacy_active_workstream_gets_silent_non_auto_baseline(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = db.insert_node(
        conn,
        kind="workstream",
        title="Pre-lifecycle lane",
        body="Objective: preserve existing state.",
        status="canonical",
    )
    member = db.insert_node(
        conn,
        kind="progress",
        title="Existing member",
        body="Forward motion.",
        workstream_id=lane,
    )

    first = lifecycle_receipts.reconcile_legacy_workstream_baselines(conn)
    assert first == {
        "baseline_count": 1,
        "workstream_ids": [lane],
        "origin": "legacy",
    }
    row = db.get_workstream_op(conn, f"baseline:workstream:{lane}")
    assert row is not None
    assert row["state"] == "applied"
    assert row["origin"] == "legacy"
    assert row["payload"]["assigned_member_ids"] == [member]
    assert lifecycle_receipts.pending_receipts(conn) == []
    assert lifecycle_receipts.recent_receipts(conn) == []

    second = lifecycle_receipts.reconcile_legacy_workstream_baselines(conn)
    assert second["baseline_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM workstream_ops WHERE dst_workstream_id=?",
        (lane,),
    ).fetchone()[0] == 1
    conn.close()


def _record_applied_op(
    conn: sqlite3.Connection,
    *,
    op_key: str,
    op: str,
    payload: dict,
    src_workstream_id: int | None = None,
    dst_workstream_id: int | None = None,
) -> None:
    db.begin_workstream_op_nc(
        conn,
        op_key=op_key,
        candidate_key=f"candidate:{op_key}",
        op=op,
        origin="manual",
        payload=payload,
        src_workstream_id=src_workstream_id,
        dst_workstream_id=dst_workstream_id,
    )
    db.finish_workstream_op_nc(conn, op_key, state="applied")
    conn.commit()


def test_pending_surface_queue_reads_then_claims_exact_item(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    receipt = 'latch opened workstream "Foreground lane".'
    _record_applied_op(
        conn,
        op_key="open:foreground-lane",
        op="OPEN",
        payload={
            "assigned_member_ids": [],
            "watch_pair": None,
            "probation": {},
            "receipt": receipt,
        },
    )
    db.record_workstream_derivation(
        conn,
        derivation_key="foreground-suggestion-derivation",
        substrate_version="test-v1",
        candidates=[{
            "candidate_key": "candidate:foreground-open",
            "op": "OPEN",
            "signal": {
                "qualified": True,
                "member_ids": [1, 2, 3, 4],
            },
        }],
    )

    changes_before = conn.total_changes
    first = lifecycle_receipts.pending_surface_items(conn, limit=1)
    assert len(first) == 1
    assert first[0]["surface_kind"] == "receipt"
    assert first[0]["text"] == receipt
    assert conn.total_changes == changes_before
    assert conn.execute(
        "SELECT COUNT(*) FROM workstream_op_events"
    ).fetchone()[0] == 0

    claimed_receipt = lifecycle_receipts.claim_pending_surface_item(
        conn,
        first[0],
        session_id="foreground-read",
    )
    assert claimed_receipt == {
        "created": True,
        "surface_kind": "receipt",
    }
    second = lifecycle_receipts.pending_surface_items(conn, limit=1)
    assert len(second) == 1
    assert second[0]["surface_kind"] == "suggestion"
    assert "may merit a new workstream" in second[0]["text"]

    claimed_suggestion = lifecycle_receipts.claim_pending_surface_item(
        conn,
        second[0],
        session_id="foreground-read",
    )
    assert claimed_suggestion == {
        "created": True,
        "surface_kind": "suggestion",
    }
    assert lifecycle_receipts.pending_surface_items(conn, limit=1) == []
    conn.close()


def test_legacy_baseline_not_suppressed_by_applied_adopt(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = db.insert_node(
        conn,
        kind="workstream",
        title="Pre-v2 lane with adoption",
        body="Objective: retain legacy ownership.",
        status="canonical",
    )
    member = db.insert_node(
        conn,
        kind="progress",
        title="Adopted member",
        body="Already moved before migration.",
        workstream_id=lane,
    )
    _record_applied_op(
        conn,
        op_key="legacy-adopt",
        op="ADOPT",
        payload={"assigned_member_ids": [member]},
        dst_workstream_id=lane,
    )

    result = lifecycle_receipts.reconcile_legacy_workstream_baselines(conn)

    assert result["workstream_ids"] == [lane]
    assert db.get_workstream_op(conn, f"baseline:workstream:{lane}")["state"] == "applied"
    conn.close()


def test_legacy_baseline_not_suppressed_for_merge_absorber(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    source = db.insert_node(
        conn,
        kind="workstream",
        title="Absorbed lane",
        body="Historical source.",
        status="stale",
    )
    absorber = db.insert_node(
        conn,
        kind="workstream",
        title="Pre-v2 absorber",
        body="Objective: preserve the active lane.",
        status="canonical",
    )
    _record_applied_op(
        conn,
        op_key="legacy-merge",
        op="MERGE",
        payload={
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
            "src_focus": False,
            "dst_focus": False,
            "post_focus": [],
            "rolling_line": "Merged before lifecycle baseline migration.",
            "rolling_op_key": "legacy-merge",
            "absorber_body_before": "Objective: preserve the active lane.",
            "absorber_body_before_hash": "before-hash",
            "absorber_body_after_hash": "after-hash",
            "source_body_hash": "source-hash",
            "source_title": "Absorbed lane",
            "source_prior_status": "canonical",
            "merge_edge_id": 1,
        },
        src_workstream_id=source,
        dst_workstream_id=absorber,
    )

    result = lifecycle_receipts.reconcile_legacy_workstream_baselines(conn)

    assert result["workstream_ids"] == [absorber]
    assert db.get_workstream_op(
        conn, f"baseline:workstream:{absorber}"
    )["state"] == "applied"
    conn.close()


def test_governed_maintenance_logs_only_structural_summary(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    conn.close()
    secret_charter = "Objective: private acquisition target"
    secret_reason = "customer transcript says do not disclose"
    secret_body = "private node body"
    secret_backup = "/private/backups/customer-lane-before-merge.json"
    governed = {
        "mode": "governed",
        "plans": [{
            "candidate_key": "candidate:open",
            "op_key": "auto:open",
            "op": "OPEN",
            "eligible": True,
            "suggestion": False,
            "apply_request": {"objective": secret_charter},
        }, {
            "candidate_key": "candidate:close",
            "op_key": "auto:close",
            "op": "CLOSE",
            "eligible": True,
            "suggestion": False,
            "reason_codes": [secret_reason],
        }],
        "applied": [{
            "candidate_key": "candidate:open",
            "op_key": "auto:open",
            "op": "OPEN",
            "result": {"ok": True, "backup_path": secret_backup},
        }],
        "failed": [{
            "candidate_key": "candidate:close",
            "op_key": "auto:close",
            "op": "CLOSE",
            "error_code": "quiescence",
            "result": {"body": secret_body, "reason": secret_reason},
        }],
        "suggestion_count": 0,
    }
    captured: dict[str, dict] = {}
    monkeypatch.setattr(maintenance.paths, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(maintenance.paths, "is_disabled", lambda *_args: False)
    monkeypatch.setattr(
        maintenance.workstream_automation,
        "run_governed",
        lambda *_args, **_kwargs: governed,
    )
    monkeypatch.setattr(
        maintenance.log_utils,
        "emit_event",
        lambda _event, row, **_kwargs: captured.setdefault("event", row),
    )
    monkeypatch.setattr(
        maintenance,
        "_log",
        lambda _project, row, **_kwargs: captured.setdefault("maintenance", row),
    )

    result = maintenance.run_workstream_governed(str(tmp_path))

    assert result["plans"] == governed["plans"]
    assert captured["event"] == captured["maintenance"]
    assert captured["event"] == {
        "op": "workstream_governed",
        "ok": False,
        "mode": "governed",
        "plan_count": 2,
        "eligible_count": 2,
        "applied_count": 1,
        "failed_count": 1,
        "suggestion_count": 0,
        "candidate_keys": ["candidate:close", "candidate:open"],
        "applied_op_keys": ["auto:open"],
        "failed_op_keys": ["auto:close"],
        "operation_codes": ["CLOSE", "OPEN"],
        "error_codes": ["quiescence"],
    }
    serialized = json.dumps(captured, sort_keys=True)
    for secret in (secret_charter, secret_reason, secret_body, secret_backup):
        assert secret not in serialized


def test_shadow_maintenance_reconciles_baselines_and_restore_before_derivation(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    conn.close()
    calls = []

    def _assert_writer_excluded(phase: str) -> None:
        with maintenance.lockfile.compactor_lock(str(tmp_path)) as acquired:
            assert acquired is False, f"concurrent writer entered during {phase}"
        calls.append(phase)

    real_connect = maintenance.db.connect

    class _CommitCheckingConnection:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self):
            _assert_writer_excluded("persist")
            return self._inner.commit()

    monkeypatch.setattr(
        maintenance.db,
        "connect",
        lambda project_path=None, **_kwargs: _CommitCheckingConnection(
            real_connect(project_path)
        ),
    )
    monkeypatch.setattr(maintenance.paths, "is_unlatched_mode", lambda *_args: False)
    monkeypatch.setattr(maintenance.paths, "is_disabled", lambda *_args: False)
    monkeypatch.setattr(
        maintenance.lifecycle_receipts,
        "reconcile_legacy_workstream_baselines",
        lambda _conn: _assert_writer_excluded("baseline") or {"baseline_count": 2},
    )
    monkeypatch.setattr(
        maintenance.lifecycle_receipts,
        "reconcile_orphaned_restore_ops",
        lambda _conn, **_kwargs: _assert_writer_excluded("restore")
        or {"orphaned_by_restore_count": 3},
    )
    monkeypatch.setattr(
        maintenance.workstream_detector,
        "run_shadow_derivation",
        lambda _conn, **_kwargs: _assert_writer_excluded("detector") or {
            "substrate_version": "test-v1",
            "derivation_key": "derive:test",
            "eligible_session_count": 0,
            "candidates": [],
            "orphan_pressure": {},
            "counters": {},
        },
    )
    monkeypatch.setattr(maintenance.log_utils, "emit_event", lambda *_a, **_k: None)
    monkeypatch.setattr(maintenance, "_log", lambda *_a, **_k: None)

    result = maintenance.run_workstream_shadow(str(tmp_path))
    assert calls == ["baseline", "restore", "detector", "persist"]
    assert not maintenance.lockfile._lock_path(str(tmp_path)).exists()
    assert result["baseline_count"] == 2
    assert result["orphaned_by_restore_count"] == 3


def _claim_concurrently(project_path: str, claim):
    barrier = threading.Barrier(2)

    def run(index: int):
        conn = db.connect(project_path)
        try:
            barrier.wait(timeout=10)
            return claim(conn, index)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, index) for index in range(2)]
        return [future.result(timeout=20) for future in futures]


def _claim_one_pending_surface_item(connection, index: int) -> list[dict]:
    pending = lifecycle_receipts.pending_surface_items(connection, limit=1)
    if not pending:
        return []
    item = pending[0]
    result = lifecycle_receipts.claim_pending_surface_item(
        connection,
        item,
        session_id=f"surface-caller-{index}",
    )
    return [item] if result["created"] else []


def test_concurrent_receipt_callers_return_only_successful_claim(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    receipt = "latch opened workstream \"Atomic receipt lane\"."
    _record_applied_op(
        conn,
        op_key="open:atomic-receipt",
        op="OPEN",
        payload={
            "assigned_member_ids": [],
            "watch_pair": None,
            "probation": {},
            "receipt": receipt,
        },
    )
    conn.close()

    results = _claim_concurrently(
        str(tmp_path),
        _claim_one_pending_surface_item,
    )

    claimed = [item for batch in results for item in batch]
    assert [item["op_key"] for item in claimed] == ["open:atomic-receipt"]
    assert claimed[0]["receipt"] == receipt
    assert sorted(len(batch) for batch in results) == [0, 1]


def test_concurrent_suggestion_callers_return_only_successful_claim(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    db.record_workstream_derivation(
        conn,
        derivation_key="atomic-suggestion-derivation",
        substrate_version="test-v1",
        candidates=[{
            "candidate_key": "candidate:atomic-open",
            "op": "OPEN",
            "signal": {
                "qualified": True,
                "member_ids": [1, 2, 3, 4],
            },
        }],
    )
    conn.close()

    results = _claim_concurrently(
        str(tmp_path),
        _claim_one_pending_surface_item,
    )

    suggestions = [
        item["text"]
        for batch in results
        for item in batch
    ]
    assert len(suggestions) == 1
    assert "may merit a new workstream" in suggestions[0]
    assert sorted(len(batch) for batch in results) == [0, 1]

    check = db.connect(str(tmp_path))
    try:
        count = check.execute(
            "SELECT COUNT(*) FROM workstream_op_events "
            "WHERE event_type='suggestion_surfaced' "
            "AND candidate_key='candidate:atomic-open'"
        ).fetchone()[0]
        assert count == 1
    finally:
        check.close()
