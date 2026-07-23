"""Stdlib-only KB schema compatibility and backup helpers."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from versioning import KB_SCHEMA_VERSION, LATCH_VERSION

META_TABLE = "latch_meta"
SCHEMA_KEY = "kb_schema_version"
MIGRATED_BY_KEY = "last_migrated_by_latch_version"


class SchemaTooNewError(RuntimeError):
    pass


class SchemaMigrationRequiredError(RuntimeError):
    pass


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def read(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, META_TABLE):
        return 0
    row = conn.execute(
        f"SELECT value FROM {META_TABLE} WHERE key = ?", (SCHEMA_KEY,)
    ).fetchone()
    if row is None:
        return 0
    try:
        value = int(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("KB schema metadata is invalid") from exc
    if value < 0:
        raise RuntimeError("KB schema metadata cannot be negative")
    return value


def ensure_supported(conn: sqlite3.Connection) -> int:
    installed = read(conn)
    if installed > KB_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"KB schema {installed} is newer than this latch engine supports "
            f"({KB_SCHEMA_VERSION}). Update latch; no migration was attempted."
        )
    return installed


def read_database(db_file: Path) -> int:
    """Read schema metadata through a read-only SQLite connection."""
    uri = db_file.expanduser().resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return read(conn)
    finally:
        conn.close()


def backup_connection(
    conn: sqlite3.Connection,
    db_file: Path,
    *,
    from_version: int,
    to_version: int = KB_SCHEMA_VERSION,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = db_file.with_name(
        f"{db_file.name}.bak.schema-{from_version}-to-{to_version}.{stamp}"
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(str(backup))
    try:
        conn.backup(dest)
    finally:
        dest.close()
    return backup


def backup_database(
    db_file: Path,
    *,
    from_version: int | None = None,
    to_version: int = KB_SCHEMA_VERSION,
) -> Path:
    source = sqlite3.connect(str(db_file))
    try:
        installed = read(source) if from_version is None else from_version
        return backup_connection(
            source, db_file, from_version=installed, to_version=to_version
        )
    finally:
        source.close()


def stamp_current(conn: sqlite3.Connection, *, record_migration: bool) -> None:
    # Reopening a current KB is a read operation.  In particular, SessionStart
    # hooks may be allowed to read an externally pinned vault without being
    # allowed to mutate it.  Avoid issuing even an idempotent UPSERT in the
    # overwhelmingly common already-current case.
    if not record_migration and read(conn) == KB_SCHEMA_VERSION:
        return

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        f"INSERT INTO {META_TABLE} (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
        f"WHERE {META_TABLE}.value != excluded.value",
        (SCHEMA_KEY, str(KB_SCHEMA_VERSION)),
    )
    if record_migration:
        conn.execute(
            f"INSERT INTO {META_TABLE} (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
            f"WHERE {META_TABLE}.value != excluded.value",
            (MIGRATED_BY_KEY, LATCH_VERSION),
        )
    conn.commit()
