from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import paths  # noqa: E402
import schema_version  # noqa: E402
import vault_backup  # noqa: E402
import vault_identity  # noqa: E402


def test_fresh_db_is_stamped_without_backup(tmp_path):
    backup_root = paths.validated_test_root() / "backups" / "legacy-production"
    before = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    path = paths.db_path(str(tmp_path))
    conn = db.connect(str(tmp_path))
    try:
        assert schema_version.read(conn) == schema_version.KB_SCHEMA_VERSION
    finally:
        conn.close()
    after = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    assert after == before


def test_legacy_db_backs_up_once_before_stamp(tmp_path):
    backup_root = paths.validated_test_root() / "backups" / "legacy-production"
    before = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    path = paths.db_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(path)
    legacy.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
    legacy.execute(
        "INSERT INTO nodes(kind,title,body,status) VALUES('decision','keep me','durable','canonical')"
    )
    legacy.commit()
    legacy.close()
    conn = db.connect(str(tmp_path))
    conn.close()
    backups = list(set(backup_root.rglob("*schema-0-to-2.db")) - before)
    assert len(backups) == 1
    copied = sqlite3.connect(backups[0])
    try:
        assert copied.execute("SELECT title FROM nodes").fetchone()[0] == "keep me"
    finally:
        copied.close()

    conn = db.connect(str(tmp_path))
    conn.close()
    assert set(backup_root.rglob("*schema-0-to-2.db")) - before == set(backups)


def test_current_unidentified_db_is_backed_up_before_production_adoption(tmp_path):
    backup_root = paths.validated_test_root() / "backups" / "legacy-production"
    before = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    path = paths.db_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
        legacy.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','pre-adoption','must survive','canonical')"
        )
        schema_version.stamp_current(legacy, record_migration=False)
    finally:
        legacy.close()

    conn = db.connect(str(tmp_path))
    try:
        assert conn._kb_vault_identity.classification == vault_identity.CLASS_PRODUCTION
    finally:
        conn.close()
    backups = list(
        set(backup_root.rglob("*identity-adoption--schema-2-to-2.db")) - before
    )
    assert len(backups) == 1
    restored = vault_backup.verify_restore(backups[0].with_suffix(".json"))
    assert restored["ok"] is True
    assert restored["nodes"] == 1


def test_newer_schema_refuses_before_migration_or_backup(tmp_path):
    backup_root = paths.validated_test_root() / "backups" / "legacy-production"
    before_backups = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    path = paths.db_path(str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE latch_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO latch_meta VALUES('kb_schema_version', '999')")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    with pytest.raises(schema_version.SchemaTooNewError):
        db.connect(str(tmp_path))
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.bak.*")) == []
    after_backups = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    assert after_backups == before_backups


def test_current_schema_stamp_is_noop_on_readonly_connection(tmp_path):
    path = paths.db_path(str(tmp_path))
    conn = db.connect(str(tmp_path))
    conn.close()
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    readonly = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        schema_version.stamp_current(readonly, record_migration=False)
    finally:
        readonly.close()

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_connect_readonly_reads_current_schema_without_mutation(tmp_path):
    path = paths.db_path(str(tmp_path))
    conn = db.connect(str(tmp_path))
    conn.execute(
        "INSERT INTO nodes(kind,title,body,status) "
        "VALUES('fact','read me','durable','canonical')"
    )
    conn.commit()
    conn.close()
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    readonly = db.connect_readonly(str(tmp_path))
    try:
        assert readonly.execute(
            "SELECT COUNT(*) FROM nodes WHERE status != 'stale'"
        ).fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            db.upsert_session(readonly, "sid", str(tmp_path))
    finally:
        readonly.close()

    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_connect_readonly_does_not_create_or_migrate(tmp_path):
    missing = paths.db_path(str(tmp_path))
    with pytest.raises(sqlite3.OperationalError):
        db.connect_readonly(str(tmp_path))
    assert not missing.exists()

    missing.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(missing)
    legacy.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
    legacy.commit()
    legacy.close()
    with pytest.raises(schema_version.SchemaMigrationRequiredError):
        db.connect_readonly(str(tmp_path))
