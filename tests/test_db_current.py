"""Foreground connections preserve vault safeguards without repeating setup."""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from latch.store import db, paths, schema_version, vault_identity


def _prepared(scope):
    conn = db.connect(str(scope))
    identity = conn._kb_vault_identity
    conn.close()
    return paths.db_path(str(scope)), identity


def test_connect_current_performs_no_setup_or_database_writes(tmp_path, monkeypatch):
    path, identity = _prepared(tmp_path)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    def forbidden(*args, **kwargs):
        raise AssertionError("foreground connection attempted vault setup")

    monkeypatch.setattr(db, "ensure_project_dir", forbidden)
    monkeypatch.setattr(db, "_ensure_schema", forbidden)
    monkeypatch.setattr(schema_version, "backup_connection", forbidden)
    monkeypatch.setattr(schema_version, "stamp_current", forbidden)
    monkeypatch.setattr(vault_identity, "ensure_identity", forbidden)
    statements = []
    original_connect = sqlite3.connect

    def observed_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        # A SQLite authorizer catches writes even if setup is accidentally
        # inlined instead of delegated to one of the guarded helpers.
        denied = {
            sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_VTABLE, sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_VTABLE,
            sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_REINDEX,
        }
        conn.set_authorizer(
            lambda action, *_args: (
                sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK
            )
        )
        return conn

    monkeypatch.setattr(sqlite3, "connect", observed_connect)
    conn = db.connect_current(str(tmp_path))
    try:
        assert conn._kb_vault_identity == identity
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not conn.in_transaction
    finally:
        conn.close()
    assert statements
    assert all(sql.lstrip().split()[0].upper() in {"SELECT", "PRAGMA"} for sql in statements)
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_connect_current_supports_session_writes_and_foreign_keys(tmp_path):
    _prepared(tmp_path)
    conn = db.connect_current(str(tmp_path))
    try:
        db.upsert_session(conn, "foreground-session", str(tmp_path), None)
        db.update_last_prompt_embedding(conn, "foreground-session", b"prompt-vector")
        row = db.get_session(conn, "foreground-session")
        assert row["project_path"] == str(tmp_path)
        assert db.get_last_prompt_embedding(conn, "foreground-session") == b"prompt-vector"
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO session_retrievals(session_id, node_id, source) "
                "VALUES(?, ?, 'prompt')",
                ("foreground-session", 999999),
            )
    finally:
        conn.close()


def test_connect_current_does_not_create_missing_vault(tmp_path):
    missing = paths.db_path(str(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        db.connect_current(str(tmp_path))
    assert not missing.exists()
    assert not missing.parent.exists()


@pytest.mark.parametrize("version", [0, schema_version.KB_SCHEMA_VERSION - 1, 999])
def test_connect_current_refuses_incompatible_schema_without_mutation(tmp_path, version):
    path, _ = _prepared(tmp_path)
    raw = sqlite3.connect(path)
    raw.execute(
        "UPDATE latch_meta SET value=? WHERE key=?",
        (str(version), schema_version.SCHEMA_KEY),
    )
    raw.commit()
    raw.close()
    before = path.read_bytes()
    error = (
        schema_version.SchemaTooNewError
        if version > schema_version.KB_SCHEMA_VERSION
        else schema_version.SchemaMigrationRequiredError
    )
    with pytest.raises(error):
        db.connect_current(str(tmp_path))
    assert path.read_bytes() == before


def test_connect_current_refuses_missing_identity_without_adoption(tmp_path):
    path, _ = _prepared(tmp_path)
    raw = sqlite3.connect(path)
    raw.execute("DROP TABLE vault_identity")
    raw.commit()
    raw.close()
    before = path.read_bytes()
    with pytest.raises(vault_identity.VaultSafetyError, match="no immutable identity"):
        db.connect_current(str(tmp_path))
    assert path.read_bytes() == before


@pytest.mark.parametrize("foreign", [False, True])
def test_connect_current_refuses_missing_or_foreign_registry(tmp_path, monkeypatch, foreign):
    path, identity = _prepared(tmp_path)
    substitute = tmp_path / "registry.json"
    if foreign:
        payload = vault_identity._registry_payload(identity)
        payload["registry_fingerprint"] = "0" * 64
        substitute.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(vault_identity, "_registry_path", lambda _identity: substitute)
    before = path.read_bytes()
    with pytest.raises(vault_identity.VaultSafetyError, match="mismatch|missing or unreadable"):
        db.connect_current(str(tmp_path))
    assert path.read_bytes() == before


def test_connect_current_deadline_exhaustion_does_not_open_database(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("expired connection attempted SQLite access")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(TimeoutError, match="deadline exhausted"):
        db.connect_current(str(tmp_path), deadline=time.perf_counter() - 1)


def test_connect_current_caps_writer_contention_by_remaining_deadline(tmp_path):
    path, _ = _prepared(tmp_path)
    writer = sqlite3.connect(path)
    writer.execute("BEGIN IMMEDIATE")
    conn = None
    try:
        started = time.perf_counter()
        conn = db.connect_current(str(tmp_path), timeout=5, deadline=started + 0.05)
        # WAL readers stay available during a writer transaction. A session
        # write must use the bounded connection allowance, not SQLite's 5 s.
        assert 0 <= conn.execute("PRAGMA busy_timeout").fetchone()[0] <= 50
        with pytest.raises(sqlite3.OperationalError, match="locked|interrupted"):
            db.upsert_session(conn, "contended", str(tmp_path))
        assert time.perf_counter() - started < 0.3
    finally:
        if conn is not None:
            conn.close()
        writer.rollback()
        writer.close()


def test_connect_current_progress_handler_interrupts_expensive_queries(tmp_path):
    _prepared(tmp_path)
    conn = db.connect_current(str(tmp_path), deadline=time.perf_counter() + 0.2)
    started = time.perf_counter()
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            conn.execute(
                "WITH RECURSIVE counter(x) AS (VALUES(0) UNION ALL "
                "SELECT x+1 FROM counter WHERE x < 1000000000) "
                "SELECT sum(x) FROM counter"
            ).fetchone()
        assert time.perf_counter() - started < 1.0
    finally:
        conn.close()


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan")])
def test_connect_current_rejects_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        db.connect_current(str(tmp_path), timeout=timeout)
