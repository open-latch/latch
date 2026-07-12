from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import schema_version  # noqa: E402


def _patch_db_path(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(db, "db_path", lambda _cwd=None: path)
    monkeypatch.setattr(db, "ensure_project_dir", lambda _cwd=None: path.parent.mkdir(parents=True, exist_ok=True))


def test_fresh_db_is_stamped_without_backup(tmp_path, monkeypatch):
    path = tmp_path / "kb.db"
    _patch_db_path(monkeypatch, path)
    conn = db.connect(str(tmp_path))
    try:
        assert schema_version.read(conn) == schema_version.KB_SCHEMA_VERSION
    finally:
        conn.close()
    assert list(tmp_path.glob("kb.db.bak.schema-*")) == []


def test_legacy_db_backs_up_once_before_stamp(tmp_path, monkeypatch):
    path = tmp_path / "kb.db"
    legacy = sqlite3.connect(path)
    legacy.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
    legacy.execute(
        "INSERT INTO nodes(kind,title,body,status) VALUES('decision','keep me','durable','canonical')"
    )
    legacy.commit()
    legacy.close()
    _patch_db_path(monkeypatch, path)

    conn = db.connect(str(tmp_path))
    conn.close()
    backups = list(tmp_path.glob("kb.db.bak.schema-0-to-1.*"))
    assert len(backups) == 1
    copied = sqlite3.connect(backups[0])
    try:
        assert copied.execute("SELECT title FROM nodes").fetchone()[0] == "keep me"
    finally:
        copied.close()

    conn = db.connect(str(tmp_path))
    conn.close()
    assert list(tmp_path.glob("kb.db.bak.schema-0-to-1.*")) == backups


def test_newer_schema_refuses_before_migration_or_backup(tmp_path, monkeypatch):
    path = tmp_path / "kb.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE latch_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO latch_meta VALUES('kb_schema_version', '999')")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    _patch_db_path(monkeypatch, path)
    with pytest.raises(schema_version.SchemaTooNewError):
        db.connect(str(tmp_path))
    assert path.read_bytes() == before
    assert list(tmp_path.glob("*.bak.*")) == []
