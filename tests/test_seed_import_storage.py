from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import lockfile  # noqa: E402
import schema_version  # noqa: E402


DEAD_PID = 9_999_991


def _source_args(**overrides):
    values = {
        "import_key": "source-key-1",
        "source_id": "claude:session-1",
        "source_agent": "claude",
        "source_path": "/local/history/session-1.jsonl",
        "source_mtime": "2026-07-16T12:00:00+00:00",
        "source_digest": "a" * 64,
        "project_path": "/workspace/example",
        "workstream_key": "activation",
        "extractor_name": "latch-seed",
        "extractor_version": "seed-v2",
    }
    values.update(overrides)
    return values


def _candidate_args(**overrides):
    values = {
        "import_key": "candidate-key-1",
        "source_import_keys": ["source-key-2", "source-key-1", "source-key-1"],
        "source_ids": ["claude:session-2", "claude:session-1"],
        "project_path": "/workspace/example",
        "workstream_key": "activation",
        "extractor_name": "latch-seed",
        "extractor_version": "seed-v2",
    }
    values.update(overrides)
    return values


def test_additive_seed_ledger_migration_survives_reconnect(tmp_path):
    project = str(tmp_path / "legacy-project")
    conn = db.connect(project)
    installed_version = schema_version.read(conn)
    conn.execute("DROP TABLE seed_import")
    conn.execute("DROP TABLE seed_source_import")
    conn.commit()
    conn.close()

    migrated = db.connect(project)
    try:
        tables = {
            row["name"]
            for row in migrated.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"seed_source_import", "seed_import"} <= tables
        assert schema_version.read(migrated) == installed_version
        db.begin_seed_source_import(migrated, **_source_args())
    finally:
        migrated.close()

    reconnected = db.connect(project)
    try:
        row = db.get_seed_source_import(reconnected, "source-key-1")
        assert row is not None
        assert row["state"] == "pending"
        assert row["attempt_count"] == 1
        assert schema_version.read(reconnected) == installed_version
    finally:
        reconnected.close()


def test_additive_seed_candidate_columns_migrate_existing_ledger(tmp_path):
    project = str(tmp_path / "legacy-candidate-ledger")
    conn = db.connect(project)
    conn.execute("DROP TABLE seed_import")
    conn.executescript(
        """
        CREATE TABLE seed_import (
            import_key              TEXT PRIMARY KEY,
            source_import_keys_json TEXT NOT NULL DEFAULT '[]',
            source_ids_json         TEXT NOT NULL DEFAULT '[]',
            project_path            TEXT NOT NULL,
            workstream_key          TEXT,
            workstream_id           INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
            extractor_name          TEXT NOT NULL,
            extractor_version       TEXT NOT NULL,
            state                   TEXT NOT NULL DEFAULT 'pending',
            error_code              TEXT,
            node_id                 INTEGER REFERENCES nodes(id),
            attempt_count           INTEGER NOT NULL DEFAULT 1,
            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at            TEXT
        );
        INSERT INTO seed_import (
            import_key, project_path, extractor_name, extractor_version
        ) VALUES ('legacy-key', '/workspace/legacy', 'latch-seed', 'seed-v1');
        """
    )
    conn.commit()
    conn.close()

    migrated = db.connect(project)
    try:
        columns = {
            row["name"] for row in migrated.execute(
                "PRAGMA table_info(seed_import)"
            ).fetchall()
        }
        assert {"claim_key", "observed_at"} <= columns
        legacy = db.get_seed_import(migrated, "legacy-key")
        assert legacy is not None
        assert legacy["claim_key"] is None
        assert legacy["observed_at"] is None
        indexes = {
            row["name"] for row in migrated.execute(
                "PRAGMA index_list(seed_import)"
            ).fetchall()
        }
        assert "idx_seed_import_claim" in indexes
        backfilled = db.begin_seed_import(
            migrated,
            import_key="legacy-key",
            claim_key="legacy-claim-v1",
            observed_at="2026-07-16T12:00:00-07:00",
            project_path="/workspace/legacy",
            extractor_name="latch-seed",
            extractor_version="seed-v1",
            retry_pending=True,
        )
        assert backfilled["claim_key"] == "legacy-claim-v1"
        assert backfilled["observed_at"] == "2026-07-16T19:00:00+00:00"

        before_conflict = dict(backfilled)
        with pytest.raises(db.SeedImportConflictError):
            db.begin_seed_import(
                migrated,
                import_key="legacy-key",
                claim_key="conflicting-claim",
                observed_at="2026-07-17T12:00:00+00:00",
                project_path="/workspace/different",
                extractor_name="latch-seed",
                extractor_version="seed-v1",
            )
        after_conflict = db.get_seed_import(migrated, "legacy-key")
        assert after_conflict["claim_key"] == before_conflict["claim_key"]
        assert after_conflict["observed_at"] == before_conflict["observed_at"]
        created = db.begin_seed_import(
            migrated,
            **_candidate_args(
                import_key="post-migration-key",
                claim_key="claim-v1",
                observed_at="2026-07-16T12:00:00+00:00",
            ),
        )
        assert created["claim_key"] == "claim-v1"
        assert created["observed_at"] == "2026-07-16T12:00:00+00:00"
        with pytest.raises(db.SeedImportLedgerError, match="timezone"):
            db.begin_seed_import(
                migrated,
                **_candidate_args(
                    import_key="naive-observed-at",
                    observed_at="2026-07-16T12:00:00",
                ),
            )
        assert db.get_seed_import(migrated, "naive-observed-at") is None
    finally:
        migrated.close()


def test_source_import_ledger_is_idempotent_and_retryable(tmp_path):
    conn = db.connect(str(tmp_path / "source-ledger"))
    try:
        first = db.begin_seed_source_import(conn, **_source_args())
        assert first["created"] is True
        assert first["retry_started"] is False
        assert first["state"] == "pending"

        duplicate = db.begin_seed_source_import(conn, **_source_args())
        assert duplicate["created"] is False
        assert duplicate["state"] == "pending"

        with pytest.raises(db.SeedImportConflictError):
            db.begin_seed_source_import(
                conn, **_source_args(source_digest="b" * 64)
            )

        with pytest.raises(db.SeedImportStateError):
            db.finish_seed_source_import(
                conn,
                "source-key-1",
                state="failed",
                error_code="raw exception text",
            )

        failed = db.finish_seed_source_import(
            conn,
            "source-key-1",
            state="failed",
            error_code="extractor_failed",
        )
        assert failed["state"] == "failed"
        assert failed["completed_at"] is not None
        assert db.finish_seed_source_import(
            conn,
            "source-key-1",
            state="failed",
            error_code="extractor_failed",
        ) == failed

        retried = db.begin_seed_source_import(
            conn, **_source_args(), retry_failed=True
        )
        assert retried["created"] is False
        assert retried["retry_started"] is True
        assert retried["state"] == "pending"
        assert retried["error_code"] is None
        assert retried["completed_at"] is None
        assert retried["attempt_count"] == 2

        applied = db.finish_seed_source_import(
            conn, "source-key-1", state="applied"
        )
        assert applied["state"] == "applied"
        assert applied["completed_at"] is not None
        terminal = db.begin_seed_source_import(
            conn,
            **_source_args(),
            retry_failed=True,
            retry_pending=True,
        )
        assert terminal["state"] == "applied"
        assert terminal["retry_started"] is False
        assert terminal["attempt_count"] == 2
    finally:
        conn.close()


def test_source_import_batch_rolls_back_when_pending_precondition_changes(
    tmp_path, monkeypatch,
):
    project = str(tmp_path / "source-batch-precondition")

    class PreconditionRaceConnection(db._Connection):
        race_injected = False

        def execute(self, sql, parameters=(), /):
            normalized = " ".join(sql.split())
            guarded_source_finish = (
                normalized.startswith(
                    "UPDATE seed_source_import SET state = ?, error_code = ?"
                )
                and "WHERE import_key = ? AND state = 'pending'" in normalized
            )
            if guarded_source_finish \
                    and parameters[-1] == "source-key-1" \
                    and not self.race_injected:
                self.race_injected = True
                # Simulate another connection winning the second transition
                # after validation but before this batch starts writing.
                with sqlite3.connect(str(db.db_path(project))) as racer:
                    cur = racer.execute(
                        "UPDATE seed_source_import "
                        "SET state = 'applied', error_code = NULL, "
                        "updated_at = ?, completed_at = ? "
                        "WHERE import_key = 'source-key-2' "
                        "AND state = 'pending'",
                        (parameters[2], parameters[3]),
                    )
                    assert cur.rowcount == 1
            return super().execute(sql, parameters)

    monkeypatch.setattr(db, "_Connection", PreconditionRaceConnection)
    conn = db.connect(project)
    try:
        db.begin_seed_source_import(conn, **_source_args())
        db.begin_seed_source_import(
            conn,
            **_source_args(
                import_key="source-key-2",
                source_id="claude:session-2",
                source_path="/local/history/session-2.jsonl",
                source_digest="b" * 64,
            ),
        )

        with pytest.raises(
            db.SeedImportStateError,
            match="must remain pending through batch finalization",
        ):
            db.finish_seed_source_imports(
                conn,
                {
                    "source-key-1": ("applied", None),
                    "source-key-2": ("applied", None),
                },
            )

        assert conn.race_injected is True
        rolled_back = {
            key: db.get_seed_source_import(conn, key)
            for key in ("source-key-1", "source-key-2")
        }
        assert rolled_back["source-key-1"]["state"] == "pending"
        assert rolled_back["source-key-1"]["error_code"] is None
        assert rolled_back["source-key-1"]["completed_at"] is None
        assert rolled_back["source-key-2"]["state"] == "applied"
        assert rolled_back["source-key-2"]["completed_at"] is not None

        applied = db.finish_seed_source_imports(
            conn,
            {
                "source-key-1": ("applied", None),
                "source-key-2": ("applied", None),
            },
        )
        assert {row["state"] for row in applied.values()} == {"applied"}
        assert db.finish_seed_source_imports(
            conn,
            {
                "source-key-1": ("applied", None),
                "source-key-2": ("applied", None),
            },
        ) == applied
    finally:
        conn.close()


def test_candidate_ledger_preserves_provenance_node_and_failure_resume(tmp_path):
    conn = db.connect(str(tmp_path / "candidate-ledger"))
    try:
        workstream_id = db.insert_node(
            conn,
            kind="workstream",
            title="Activation",
            body="Reviewed workstream",
        )
        node_id = db.insert_node(
            conn,
            kind="decision",
            title="Seeded decision",
            body="Evidence-backed decision",
            workstream_id=workstream_id,
        )
        args = _candidate_args(workstream_id=workstream_id)
        pending = db.begin_seed_import(conn, **args)
        assert pending["created"] is True
        assert json.loads(pending["source_import_keys_json"]) == [
            "source-key-1",
            "source-key-2",
        ]
        assert json.loads(pending["source_ids_json"]) == [
            "claude:session-1",
            "claude:session-2",
        ]

        checkpoint = db.set_seed_import_node(conn, "candidate-key-1", node_id)
        assert checkpoint["state"] == "pending"
        assert checkpoint["node_id"] == node_id
        assert db.set_seed_import_node(conn, "candidate-key-1", node_id) == checkpoint

        failed = db.finish_seed_import(
            conn,
            "candidate-key-1",
            state="failed",
            error_code="workstream_attach_failed",
        )
        assert failed["node_id"] == node_id
        assert failed["state"] == "failed"

        resumed = db.begin_seed_import(conn, **args, retry_failed=True)
        assert resumed["state"] == "pending"
        assert resumed["node_id"] == node_id
        assert resumed["attempt_count"] == 2
        applied = db.finish_seed_import(
            conn, "candidate-key-1", state="applied"
        )
        assert applied["state"] == "applied"
        assert applied["node_id"] == node_id
        assert db.finish_seed_import(
            conn, "candidate-key-1", state="applied", node_id=node_id
        ) == applied

        duplicate = db.begin_seed_import(conn, **args)
        assert duplicate["created"] is False
        assert duplicate["state"] == "applied"
        with pytest.raises(db.SeedImportConflictError):
            db.begin_seed_import(
                conn,
                **_candidate_args(
                    workstream_id=workstream_id,
                    source_ids=["codex:different-source"],
                ),
            )
        with pytest.raises(db.SeedImportStateError):
            db.begin_seed_import(
                conn,
                **_candidate_args(import_key="missing-node", workstream_id=workstream_id),
            )
            db.finish_seed_import(conn, "missing-node", state="applied")
    finally:
        conn.close()


def test_error_code_enum_is_closed_in_sqlite_too(tmp_path):
    conn = db.connect(str(tmp_path / "closed-error-code"))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO seed_source_import (
                    import_key, source_id, source_agent, source_path,
                    source_mtime, source_digest, project_path,
                    extractor_name, extractor_version, state, error_code,
                    completed_at
                ) VALUES (
                    'bad', 'source', 'claude', '/local/source', 'now', 'digest',
                    '/project', 'latch-seed', 'seed-v2', 'failed',
                    'secret-bearing raw error', datetime('now')
                )
                """
            )
        conn.rollback()
    finally:
        conn.close()


def test_writer_lock_holds_shared_lock_for_entire_batch(tmp_path):
    project = str(tmp_path / "writer-batch")
    lock_path = lockfile._lock_path(project)
    with lockfile.writer_lock(project, timeout_s=0.2, poll_interval_s=0.01):
        assert lock_path.exists()
        pid, acquired_at = lockfile._read_lock(lock_path)
        assert pid == os.getpid()
        assert acquired_at is not None
        with lockfile.compactor_lock(project) as acquired:
            assert acquired is False
    assert not lock_path.exists()
    with lockfile.compactor_lock(project) as acquired:
        assert acquired is True


def test_writer_lock_times_out_on_live_holder_and_releases_after_error(tmp_path):
    project = str(tmp_path / "writer-timeout")
    ready = threading.Event()
    release = threading.Event()
    holder_errors: list[BaseException] = []

    def hold_from_other_thread() -> None:
        try:
            with lockfile.compactor_lock(project) as acquired:
                assert acquired is True
                ready.set()
                assert release.wait(2.0)
        except BaseException as exc:  # pragma: no cover - asserted below
            holder_errors.append(exc)
            ready.set()

    holder = threading.Thread(target=hold_from_other_thread, daemon=True)
    holder.start()
    assert ready.wait(1.0)
    try:
        with pytest.raises(lockfile.CompactionInProgressError):
            with lockfile.writer_lock(
                project, timeout_s=0.05, poll_interval_s=0.01
            ):
                pytest.fail("live lock must not be stolen")
        assert lockfile._lock_path(project).exists()
    finally:
        release.set()
        holder.join(timeout=2.0)
    assert not holder.is_alive()
    assert holder_errors == []

    with pytest.raises(RuntimeError, match="batch failed"):
        with lockfile.writer_lock(project, timeout_s=0.2):
            raise RuntimeError("batch failed")
    assert not lockfile._lock_path(project).exists()


def test_writer_lock_evicts_stale_dead_pid(tmp_path):
    project = str(tmp_path / "writer-stale")
    lock_path = lockfile._lock_path(project)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        f"{DEAD_PID}\n2026-01-01T00:00:00+00:00", encoding="utf-8"
    )
    with lockfile.writer_lock(project, timeout_s=0.2, poll_interval_s=0.01):
        pid, _ = lockfile._read_lock(lock_path)
        assert pid == os.getpid()
    assert not lock_path.exists()
