from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import paths  # noqa: E402
import vault_backup  # noqa: E402
import vault_identity  # noqa: E402


UTC = timezone.utc


def _seed(scope: Path) -> None:
    conn = db.connect(str(scope))
    try:
        conn.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','protected','survives','canonical')"
        )
        conn.commit()
    finally:
        conn.close()


def test_snapshot_is_external_atomic_verified_and_restorable(tmp_path):
    _seed(tmp_path)
    created = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    receipt = vault_backup.create_snapshot(str(tmp_path), reason="test", now=created)
    database = Path(receipt["database"])
    manifest_path = Path(receipt["manifest"])
    live = paths.project_dir(str(tmp_path))
    test_root = paths.validated_test_root()
    assert database.is_relative_to(test_root / "backups")
    assert not database.is_relative_to(live)
    assert database.name.endswith("--daily--test.db")
    assert not list(database.parent.glob("*.tmp"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(database.read_bytes()).hexdigest() == manifest["sha256"]
    assert manifest["integrity_check"] == "ok"
    assert manifest["protected_until"] == (created + timedelta(days=30)).isoformat()
    restored = vault_backup.verify_restore(manifest_path, work_root=tmp_path / "drills")
    assert restored["ok"] is True
    assert restored["nodes"] == 1
    assert list((tmp_path / "drills").iterdir()) == []


def test_daily_and_six_hour_retention_cannot_be_pruned_early(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    daily = vault_backup.create_snapshot(str(tmp_path), reason="cadence", now=start)
    frequent = vault_backup.create_snapshot(
        str(tmp_path), reason="cadence", now=start + timedelta(hours=6)
    )
    assert daily["tier"] == "daily"
    assert frequent["tier"] == "six-hour"
    assert daily["protected_until"] == (start + timedelta(days=30)).isoformat()
    assert frequent["protected_until"] == (
        start + timedelta(hours=6, days=5)
    ).isoformat()

    before = {
        Path(daily["database"]): Path(daily["database"]).read_bytes(),
        Path(frequent["database"]): Path(frequent["database"]).read_bytes(),
    }
    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=4, hours=23)
    )
    assert result["deleted"] == 0
    assert all(path.read_bytes() == body for path, body in before.items())


def test_prune_deletes_only_expired_verified_pair_and_keeps_newest(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    old_daily = vault_backup.create_snapshot(str(tmp_path), reason="old", now=start)
    old_frequent = vault_backup.create_snapshot(
        str(tmp_path), reason="old", now=start + timedelta(hours=6)
    )
    newest = vault_backup.create_snapshot(
        str(tmp_path), reason="new", now=start + timedelta(days=1)
    )
    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )
    assert result["deleted"] == 2
    assert not Path(old_daily["database"]).exists()
    assert not Path(old_frequent["database"]).exists()
    assert Path(newest["database"]).exists()


def test_corrupt_snapshot_is_never_pruned(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    corrupt = vault_backup.create_snapshot(str(tmp_path), reason="corrupt", now=start)
    vault_backup.create_snapshot(
        str(tmp_path), reason="new", now=start + timedelta(days=1)
    )
    database = Path(corrupt["database"])
    database.chmod(0o600)
    database.write_bytes(database.read_bytes() + b"tampered")
    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )
    assert result["skipped"] == 1
    assert database.exists()


def test_test_runtime_ignores_durability_root_spoof(tmp_path, monkeypatch):
    unsafe = ROOT / "would-be-test-backups"
    monkeypatch.setenv(vault_backup.DURABILITY_ROOT_ENV, str(unsafe))
    _seed(tmp_path)
    receipt = vault_backup.create_snapshot(str(tmp_path), reason="spoof")
    assert Path(receipt["database"]).is_relative_to(
        paths.validated_test_root() / "backups"
    )
    assert not unsafe.exists()


def test_production_classified_legacy_copy_still_backs_up_inside_test_root(tmp_path):
    vault = paths.project_dir(str(tmp_path))
    vault.mkdir(parents=True)
    legacy = sqlite3.connect(vault / "kb.db")
    try:
        legacy.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
        legacy.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','legacy production copy','safe','canonical')"
        )
        legacy.commit()
    finally:
        legacy.close()
    conn = db.connect(str(tmp_path))
    try:
        assert conn._kb_vault_identity.classification == vault_identity.CLASS_PRODUCTION
    finally:
        conn.close()
    receipt = vault_backup.create_snapshot(str(tmp_path), reason="legacy-copy")
    assert Path(receipt["database"]).is_relative_to(
        paths.validated_test_root() / "backups"
    )
