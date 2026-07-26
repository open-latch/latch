from __future__ import annotations

import json
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
    backups = list(set(backup_root.rglob("*schema-0-to-3.db")) - before)
    assert len(backups) == 1
    copied = sqlite3.connect(backups[0])
    try:
        assert copied.execute("SELECT title FROM nodes").fetchone()[0] == "keep me"
    finally:
        copied.close()

    conn = db.connect(str(tmp_path))
    conn.close()
    assert set(backup_root.rglob("*schema-0-to-3.db")) - before == set(backups)


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
        set(backup_root.rglob("*identity-adoption--schema-3-to-3.db")) - before
    )
    assert len(backups) == 1
    restored = vault_backup.verify_restore(backups[0].with_suffix(".json"))
    assert restored["ok"] is True
    assert restored["nodes"] == 1


@pytest.mark.parametrize("starting_schema", ["fresh", "legacy-0", "legacy-2"])
def test_schema_three_is_fenced_before_first_identity_commit(
    tmp_path, monkeypatch, starting_schema
):
    scope = tmp_path / starting_schema
    path = paths.db_path(str(scope))
    backup_root = paths.validated_test_root() / "backups" / "legacy-production"
    before = set(backup_root.rglob("*.db")) if backup_root.exists() else set()
    if starting_schema != "fresh":
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = sqlite3.connect(path)
        legacy.executescript(
            (ROOT / "src" / "schema.sql").read_text(encoding="utf-8")
        )
        legacy.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','pre-v3','must survive','canonical')"
        )
        if starting_schema == "legacy-2":
            legacy.execute(
                "CREATE TABLE IF NOT EXISTS latch_meta("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            legacy.execute(
                "INSERT OR REPLACE INTO latch_meta VALUES("
                "'kb_schema_version', '2')"
            )
        legacy.commit()
        legacy.close()

    observed_versions: list[int] = []

    def fail_before_identity(conn, _vault_dir, *, new_vault):
        observed_versions.append(schema_version.read(conn))
        raise RuntimeError(f"injected identity failure, new={new_vault}")

    monkeypatch.setattr(vault_identity, "ensure_identity", fail_before_identity)
    with pytest.raises(RuntimeError, match="injected identity failure"):
        db.connect(str(scope))

    raw = sqlite3.connect(path)
    try:
        assert schema_version.read(raw) == 3
    finally:
        raw.close()
    assert observed_versions == [3]
    if starting_schema == "legacy-2":
        backups = set(backup_root.rglob("*schema-2-to-3.db")) - before
        assert len(backups) == 1


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


def test_connect_readonly_fails_closed_on_missing_or_mismatched_registry(
    tmp_path, monkeypatch
):
    scope = tmp_path / "readonly-identity"
    path = paths.db_path(str(scope))
    conn = db.connect(str(scope))
    try:
        identity = conn._kb_vault_identity
    finally:
        conn.close()

    substitute = tmp_path / "registry-record.json"
    monkeypatch.setattr(
        vault_identity,
        "_registry_path",
        lambda _identity: substitute,
    )
    with pytest.raises(vault_identity.VaultSafetyError, match="missing or unreadable"):
        db.connect_readonly(str(scope))

    payload = vault_identity._registry_payload(identity)
    payload["registry_fingerprint"] = "0" * 64
    substitute.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(vault_identity.VaultSafetyError, match="mismatch"):
        db.connect_readonly(str(scope))
    assert path.is_file()


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
