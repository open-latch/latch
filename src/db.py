"""SQLite helpers for claude_kb.

Connection lifecycle is intentionally simple: open, do work, close. WAL mode
makes concurrent reads/writes safe for our usage pattern (MCP server + hooks).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import log_utils
import schema_version
from paths import SCHEMA_PATH, db_path, ensure_project_dir


VEC_DIM = 384  # all-MiniLM-L6-v2

# Closed, privacy-safe outcomes for the seed import ledgers.  Store the code,
# never raw exception text (which can contain transcript excerpts or secrets).
SEED_IMPORT_STATES = frozenset({"pending", "applied", "failed"})
SEED_IMPORT_ERROR_CODES = frozenset({
    "source_unavailable",
    "source_invalid",
    "extractor_failed",
    "candidate_invalid",
    "node_write_failed",
    "workstream_attach_failed",
    "interrupted",
    "internal",
})


class SeedImportLedgerError(ValueError):
    """Base class for invalid seed-ledger operations."""


class SeedImportConflictError(SeedImportLedgerError):
    """An import key was reused with different immutable metadata."""


class SeedImportStateError(SeedImportLedgerError):
    """A requested seed-ledger state transition is invalid."""


def _resolve_actor() -> str:
    """Identify the OS user running this process. Stamped on every user-facing
    write (insert_node / update_node / add_edge / upsert_session) for audit and
    'what has X been doing' filtering. NEVER used as input to ranking or
    healing arbitration — facts are facts regardless of author."""
    return (
        os.environ.get("CLAUDE_KB_USER")
        or os.environ.get("USERNAME")
        or os.environ.get("USER")
        or "unknown"
    )


_ACTOR = _resolve_actor()


class _Connection(sqlite3.Connection):
    """sqlite3.Connection subclass — the C base class forbids arbitrary
    attributes, so we need a subclass to stash the vec-loaded flag."""
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def connect(cwd: str | None = None) -> sqlite3.Connection:
    ensure_project_dir(cwd)
    path = db_path(cwd)
    conn = sqlite3.connect(str(path), factory=_Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        had_nodes = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone() is not None
        installed_schema = schema_version.ensure_supported(conn)
        if had_nodes and installed_schema < schema_version.KB_SCHEMA_VERSION:
            schema_version.backup_connection(
                conn,
                Path(path),
                from_version=installed_schema,
                to_version=schema_version.KB_SCHEMA_VERSION,
            )
        _load_vec(conn)
        _ensure_schema(conn)
        schema_version.stamp_current(
            conn,
            record_migration=(not had_nodes or installed_schema < schema_version.KB_SCHEMA_VERSION),
        )
        return conn
    except Exception:
        conn.close()
        raise


def _load_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension. Returns True on success, False otherwise
    (e.g. package missing, platform DLL mismatch). Callers should honour
    `vec_loaded(conn)` and fall back to brute-force cosine when it returns False."""
    try:
        import sqlite_vec  # type: ignore
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn._kb_vec_loaded = True
        return True
    except Exception:
        conn._kb_vec_loaded = False
        return False


def vec_loaded(conn: sqlite3.Connection) -> bool:
    return bool(getattr(conn, "_kb_vec_loaded", False))


def _ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'")
    if cur.fetchone() is None:
        conn.executescript(Path(SCHEMA_PATH).read_text(encoding="utf-8"))
        conn.commit()
    if vec_loaded(conn):
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes "
            f"USING vec0(embedding float[{VEC_DIM}] distance_metric=cosine)"
        )
        conn.commit()
    _migrate_session_retrievals(conn)


def _migrate_session_retrievals(conn: sqlite3.Connection) -> None:
    """Idempotent additive migration for the UserPromptSubmit feature.

    SQLite's CREATE TABLE IF NOT EXISTS handles the new table; ALTER TABLE
    can't be guarded that way, so we PRAGMA-check existing columns first."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_retrievals (
            session_id        TEXT    NOT NULL,
            node_id           INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            first_injected_at TEXT    NOT NULL DEFAULT (datetime('now')),
            last_injected_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            first_injected_turn INTEGER NOT NULL DEFAULT 0,
            last_injected_turn  INTEGER NOT NULL DEFAULT 0,
            hit_count         INTEGER NOT NULL DEFAULT 1,
            sim_at_first      REAL,
            source            TEXT    NOT NULL,
            PRIMARY KEY (session_id, node_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_retrievals_sid "
        "ON session_retrievals(session_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_retrievals_last_turn "
        "ON session_retrievals(last_injected_turn)"
    )
    existing_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    if "last_prompt_embedding" not in existing_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_prompt_embedding BLOB")
    if "last_prompt_at" not in existing_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN last_prompt_at TEXT")
    conn.commit()
    _migrate_user_attribution(conn)


def _migrate_user_attribution(conn: sqlite3.Connection) -> None:
    """Add created_by / updated_by columns to nodes, edges, sessions.
    Idempotent — safe to re-run. Existing rows get NULL author (harmless)."""
    nodes_cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "created_by" not in nodes_cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN created_by TEXT")
    if "updated_by" not in nodes_cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN updated_by TEXT")
    edges_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    if "created_by" not in edges_cols:
        conn.execute("ALTER TABLE edges ADD COLUMN created_by TEXT")
    sessions_cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "created_by" not in sessions_cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN created_by TEXT")
    conn.commit()
    _migrate_step9_focus(conn)


def _migrate_step9_focus(conn: sqlite3.Connection) -> None:
    """Step 9 schema: workstream_id column on nodes + focus table.
    Idempotent — PRAGMA-checks columns and CREATE TABLE IF NOT EXISTS."""
    nodes_cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "workstream_id" not in nodes_cols:
        conn.execute(
            "ALTER TABLE nodes ADD COLUMN workstream_id INTEGER "
            "REFERENCES nodes(id) ON DELETE SET NULL"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_workstream ON nodes(workstream_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS focus (
            workstream_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            rank          INTEGER NOT NULL,
            score         REAL    NOT NULL,
            set_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            set_by        TEXT    NOT NULL,
            pinned        INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_focus_score ON focus(score DESC)")
    conn.commit()
    _migrate_tree_content_hash(conn)


def _migrate_tree_content_hash(conn: sqlite3.Connection) -> None:
    """Add content_hash to nodes for tree.build_tree's hash-based skip.
    Idempotent — PRAGMA-checks before ALTER. Existing rows get NULL and
    are backfilled opportunistically on the next build_tree run."""
    nodes_cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    if "content_hash" not in nodes_cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN content_hash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_nodes_content_hash ON nodes(content_hash) "
        "WHERE content_hash IS NOT NULL"
    )
    conn.commit()
    _migrate_edge_status(conn)


def _migrate_edge_status(conn: sqlite3.Connection) -> None:
    """Add `status` column to edges. Mirrors the node-stale idiom — 'active' is
    the default; 'tombstoned' edges are kept for audit but filtered from reads.
    Idempotent — PRAGMA-checks before ALTER."""
    edges_cols = {r["name"] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    if "status" not in edges_cols:
        conn.execute(
            "ALTER TABLE edges ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    conn.commit()
    _migrate_priorities_order(conn)


def _migrate_priorities_order(conn: sqlite3.Connection) -> None:
    """Side table carrying priority ordering + graveyard date (see priorities.py).

    `rank` non-NULL = a user-locked absolute slot (immutable to automatic adds);
    NULL = floating (ordered by recency at read time). `retired_at` is the
    immutable date a priority entered the graveyard — distinct from nodes.updated_at,
    which moves on any edit. Lifecycle (active vs retired) still lives on
    nodes.status (id=1324); this table only carries ordering + the graveyard stamp.

    Idempotent — CREATE TABLE IF NOT EXISTS + backfills only priority nodes that
    lack a row. Existing priorities backfill as floating (rank NULL); retired ones
    get a best-effort retired_at from their last updated_at."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS priority_order (
            node_id    INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            rank       INTEGER,
            retired_at TEXT
        )
        """
    )
    missing = conn.execute(
        """
        SELECT n.id AS id, n.status AS status, n.updated_at AS updated_at
        FROM nodes n
        WHERE n.kind = 'priority'
          AND NOT EXISTS (SELECT 1 FROM priority_order po WHERE po.node_id = n.id)
        """
    ).fetchall()
    for r in missing:
        retired_at = r["updated_at"] if r["status"] == "stale" else None
        conn.execute(
            "INSERT OR IGNORE INTO priority_order (node_id, rank, retired_at) "
            "VALUES (?, NULL, ?)",
            (r["id"], retired_at),
        )
    conn.commit()
    _migrate_profiles(conn)


# EXPERIMENTAL — mission-control / verification profiles. NOT recommended for use;
# planned to be unshipped to a separate branch later (observed unhelpful on
# pmeyer's workspace, 2026-06-10). See KB decision id=1550. Don't rely on / extend.
def _migrate_profiles(conn: sqlite3.Connection) -> None:
    """Side tables for verification profiles (see profiles.py).

    `profile_config` carries the typed gate-behaviour parameters keyed by the
    profile node id — NOT crammed into the free-text node body (the
    "don't merge into priority rows" line, id=1406). `profile_binding` maps a
    resolved actor (db._ACTOR — CLAUDE_KB_USER/USERNAME/USER, id=1405) to the
    profile node currently active for that user.

    Idempotent — CREATE TABLE IF NOT EXISTS only; no backfill. The built-in
    presets (trust-and-go / mission-control) are materialised lazily as profile
    nodes by profiles.ensure_presets, not seeded here (migrations stay
    schema-only)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_config (
            profile_node_id      INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            gate_surface         TEXT NOT NULL,
            verdict_posture      TEXT NOT NULL,
            claim_backing_policy TEXT NOT NULL,
            adversary            TEXT NOT NULL,
            user_authority       TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS profile_binding (
            actor           TEXT    PRIMARY KEY,
            profile_node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            bound_at        TEXT    NOT NULL
        )
        """
    )
    conn.commit()
    _migrate_cite_nudge(conn)


def _migrate_cite_nudge(conn: sqlite3.Connection) -> None:
    """Per-session pending cite-nudge marker for mission control's Slice 3-B
    (KB id=1436). The Stop-hook cite detector sets a small count when it flags
    an uncited current-value/code claim; the next UserPromptSubmit reads+resets
    it and surfaces the advisory correction directive.

    Lives as a column on the `sessions` row, not a side table: it is transient
    per-session state (set then consumed within a session), not auditable
    history — the audit trail is detection.log. Idempotent: PRAGMA-checks
    before ALTER (CREATE TABLE IF NOT EXISTS can't guard a column add)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "pending_cite_nudge" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN pending_cite_nudge INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()
    _migrate_artifacts(conn)


def _migrate_artifacts(conn: sqlite3.Connection) -> None:
    """Artifact provenance side structure (see artifacts.py / KB id=1515/id=1516).

    `artifact` is the shared coordinate dimension (repo + optional file), keyed
    UNIQUE(repo, path); it does NOT cascade on node delete because coordinates
    are historical and outlive any single node. `node_artifact` is the
    append-only provenance junction and DOES cascade on node delete.

    `path` is NOT NULL DEFAULT '' (deviation from the id=1515 'NULL' sketch):
    SQLite treats NULLs as distinct in a UNIQUE index, so a nullable path would
    let duplicate repo-level coordinates slip past UNIQUE(repo, path); '' as the
    repo-level sentinel keeps the dedup real. `status` / `missing_since` /
    `successor_id` are the lifecycle columns (id=1517) — present from Slice 1 so
    the later liveness/rename slice needs no migration; unused until then.

    Idempotent — CREATE TABLE/INDEX IF NOT EXISTS only; no backfill."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifact (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            repo          TEXT    NOT NULL,
            path          TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'live',
            missing_since TEXT,
            successor_id  INTEGER REFERENCES artifact(id),
            UNIQUE(repo, path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS node_artifact (
            node_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            artifact_id INTEGER NOT NULL REFERENCES artifact(id),
            PRIMARY KEY (node_id, artifact_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_repo ON artifact(repo)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifact_path ON artifact(path)")
    conn.commit()
    _migrate_seed_import_ledgers(conn)


def _migrate_seed_import_ledgers(conn: sqlite3.Connection) -> None:
    """Add the history-seeding idempotency ledgers without a version bump.

    These tables are additive side state: legacy databases need no row
    backfill and reconnecting is a no-op.  ``seed_source_import`` tracks one
    exact transcript revision through extraction; ``seed_import`` tracks one
    exact reviewed candidate through node creation.  The latter may retain a
    ``node_id`` while failed so a retry can finish attachment without creating
    a duplicate node.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS seed_source_import (
            import_key        TEXT PRIMARY KEY,
            source_id         TEXT NOT NULL,
            source_agent      TEXT NOT NULL,
            source_path       TEXT NOT NULL,
            source_mtime      TEXT NOT NULL,
            source_digest     TEXT NOT NULL,
            project_path      TEXT NOT NULL,
            workstream_key    TEXT,
            extractor_name    TEXT NOT NULL,
            extractor_version TEXT NOT NULL,
            state             TEXT NOT NULL DEFAULT 'pending'
                                      CHECK (state IN ('pending', 'applied', 'failed')),
            error_code        TEXT CHECK (error_code IN (
                                      'source_unavailable', 'source_invalid',
                                      'extractor_failed', 'candidate_invalid',
                                      'node_write_failed', 'workstream_attach_failed',
                                      'interrupted', 'internal'
                                  )),
            attempt_count     INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at      TEXT,
            CHECK (
                (state = 'failed' AND error_code IS NOT NULL AND completed_at IS NOT NULL)
                OR (state = 'applied' AND error_code IS NULL AND completed_at IS NOT NULL)
                OR (state = 'pending' AND error_code IS NULL AND completed_at IS NULL)
            )
        );

        CREATE INDEX IF NOT EXISTS idx_seed_source_import_state
            ON seed_source_import(state);
        CREATE INDEX IF NOT EXISTS idx_seed_source_import_source
            ON seed_source_import(source_id, source_digest);
        CREATE INDEX IF NOT EXISTS idx_seed_source_import_scope
            ON seed_source_import(project_path, workstream_key);

        CREATE TABLE IF NOT EXISTS seed_import (
            import_key              TEXT PRIMARY KEY,
            claim_key               TEXT,
            source_import_keys_json TEXT NOT NULL DEFAULT '[]',
            source_ids_json         TEXT NOT NULL DEFAULT '[]',
            project_path            TEXT NOT NULL,
            workstream_key          TEXT,
            workstream_id           INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
            extractor_name          TEXT NOT NULL,
            extractor_version       TEXT NOT NULL,
            observed_at             TEXT,
            state                   TEXT NOT NULL DEFAULT 'pending'
                                          CHECK (state IN ('pending', 'applied', 'failed')),
            error_code              TEXT CHECK (error_code IN (
                                          'source_unavailable', 'source_invalid',
                                          'extractor_failed', 'candidate_invalid',
                                          'node_write_failed', 'workstream_attach_failed',
                                          'interrupted', 'internal'
                                      )),
            node_id                 INTEGER REFERENCES nodes(id),
            attempt_count           INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
            created_at              TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at            TEXT,
            CHECK (
                (state = 'failed' AND error_code IS NOT NULL AND completed_at IS NOT NULL)
                OR (state = 'applied' AND error_code IS NULL AND completed_at IS NOT NULL
                                   AND node_id IS NOT NULL)
                OR (state = 'pending' AND error_code IS NULL AND completed_at IS NULL)
            )
        );

        CREATE INDEX IF NOT EXISTS idx_seed_import_state ON seed_import(state);
        CREATE INDEX IF NOT EXISTS idx_seed_import_node ON seed_import(node_id);
        CREATE INDEX IF NOT EXISTS idx_seed_import_scope
            ON seed_import(project_path, workstream_key);
        """
    )
    seed_import_cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(seed_import)").fetchall()
    }
    if "observed_at" not in seed_import_cols:
        conn.execute("ALTER TABLE seed_import ADD COLUMN observed_at TEXT")
    if "claim_key" not in seed_import_cols:
        conn.execute("ALTER TABLE seed_import ADD COLUMN claim_key TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_seed_import_claim "
        "ON seed_import(claim_key, state)"
    )
    conn.commit()


# ---------- history seed import ledgers ----------

def _required_seed_metadata(name: str, value: str) -> str:
    text = str(value)
    if not text.strip():
        raise SeedImportLedgerError(f"{name} must be non-empty")
    return text


def _optional_seed_metadata(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _seed_observed_at(value: str | None) -> str | None:
    """Validate and UTC-normalize timestamps used for evidence recency."""
    text = _optional_seed_metadata(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SeedImportLedgerError("observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SeedImportLedgerError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _seed_string_list(values: Sequence[str]) -> str:
    """Canonical JSON for set-like seed provenance fields."""
    normalized = sorted({str(value) for value in values if str(value)})
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _seed_ledger_row(
    conn: sqlite3.Connection, table: str, import_key: str,
) -> dict | None:
    # ``table`` is always one of the two internal constants supplied below;
    # it is never caller-controlled input.
    row = conn.execute(
        f"SELECT * FROM {table} WHERE import_key = ?", (import_key,)
    ).fetchone()
    return dict(row) if row else None


def _assert_seed_metadata(
    row: dict, expected: dict[str, Any], *, table: str,
) -> None:
    mismatched = [name for name, value in expected.items() if row[name] != value]
    if mismatched:
        # Do not include values: paths are local provenance and should not leak
        # into exception/log output.  Field names are enough to diagnose a key
        # construction bug.
        raise SeedImportConflictError(
            f"{table} import_key metadata mismatch: {', '.join(mismatched)}"
        )


def _retry_seed_ledger(
    conn: sqlite3.Connection,
    *,
    table: str,
    import_key: str,
    retry_failed: bool,
    retry_pending: bool,
) -> bool:
    row = _seed_ledger_row(conn, table, import_key)
    if row is None:
        raise KeyError(f"unknown {table} import_key")
    retry = (
        (row["state"] == "failed" and retry_failed)
        or (row["state"] == "pending" and retry_pending)
    )
    if not retry:
        return False
    conn.execute(
        f"UPDATE {table} SET state = 'pending', error_code = NULL, "
        "completed_at = NULL, updated_at = ?, attempt_count = attempt_count + 1 "
        "WHERE import_key = ?",
        (_now(), import_key),
    )
    conn.commit()
    return True


def get_seed_source_import(
    conn: sqlite3.Connection, import_key: str,
) -> dict | None:
    """Return one source-extraction ledger row without changing it."""
    return _seed_ledger_row(conn, "seed_source_import", import_key)


def begin_seed_source_import(
    conn: sqlite3.Connection,
    *,
    import_key: str,
    source_id: str,
    source_agent: str,
    source_path: str,
    source_mtime: str,
    source_digest: str,
    project_path: str,
    extractor_name: str,
    extractor_version: str,
    workstream_key: str | None = None,
    retry_failed: bool = False,
    retry_pending: bool = False,
) -> dict:
    """Idempotently create (or fetch) a pending source import.

    ``import_key`` identity metadata is immutable. Reusing a key with different
    source digest, scope, or extractor identity raises
    ``SeedImportConflictError``; path/mtime are retained provenance observations
    and may differ after an unchanged source is moved or touched.
    Failed or crash-left pending rows are reset only when the matching explicit
    retry flag is set; successful rows are terminal.  The returned dict adds
    transient ``created`` and ``retry_started`` booleans.
    """
    metadata = {
        "source_id": _required_seed_metadata("source_id", source_id),
        "source_agent": _required_seed_metadata("source_agent", source_agent),
        "source_path": _required_seed_metadata("source_path", source_path),
        "source_mtime": _required_seed_metadata("source_mtime", source_mtime),
        "source_digest": _required_seed_metadata("source_digest", source_digest),
        "project_path": _required_seed_metadata("project_path", project_path),
        "workstream_key": _optional_seed_metadata(workstream_key),
        "extractor_name": _required_seed_metadata("extractor_name", extractor_name),
        "extractor_version": _required_seed_metadata(
            "extractor_version", extractor_version
        ),
    }
    key = _required_seed_metadata("import_key", import_key)
    cur = conn.execute(
        """
        INSERT INTO seed_source_import (
            import_key, source_id, source_agent, source_path, source_mtime,
            source_digest, project_path, workstream_key, extractor_name,
            extractor_version, state, error_code, attempt_count,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, 1, ?, ?, NULL)
        ON CONFLICT(import_key) DO NOTHING
        """,
        (
            key,
            metadata["source_id"],
            metadata["source_agent"],
            metadata["source_path"],
            metadata["source_mtime"],
            metadata["source_digest"],
            metadata["project_path"],
            metadata["workstream_key"],
            metadata["extractor_name"],
            metadata["extractor_version"],
            _now(),
            _now(),
        ),
    )
    created = bool(cur.rowcount)
    conn.commit()
    row = get_seed_source_import(conn, key)
    if row is None:  # pragma: no cover - SQLite acknowledged the insert/read
        raise SeedImportLedgerError("seed_source_import insert was not readable")
    # Path and mtime are provenance observations, not source identity. An exact
    # redacted digest may be touched or moved between an applied run and an
    # explicit force-reimport; retain the original receipt without turning that
    # harmless locator change into an identity collision.
    immutable_metadata = {
        name: value for name, value in metadata.items()
        if name not in {"source_path", "source_mtime"}
    }
    _assert_seed_metadata(row, immutable_metadata, table="seed_source_import")
    retry_started = False
    if not created:
        retry_started = _retry_seed_ledger(
            conn,
            table="seed_source_import",
            import_key=key,
            retry_failed=retry_failed,
            retry_pending=retry_pending,
        )
        if retry_started:
            row = get_seed_source_import(conn, key)
            assert row is not None
    row["created"] = created
    row["retry_started"] = retry_started
    return row


def finish_seed_source_import(
    conn: sqlite3.Connection,
    import_key: str,
    *,
    state: str,
    error_code: str | None = None,
) -> dict:
    """Move a pending source import to terminal ``applied`` or ``failed``.

    Repeating the exact terminal transition is idempotent.  Other transitions
    from a terminal row require a new ``begin_*`` call with ``retry_failed``.
    """
    return finish_seed_source_imports(
        conn, {import_key: (state, error_code)}
    )[import_key]


def finish_seed_source_imports(
    conn: sqlite3.Connection,
    outcomes: dict[str, tuple[str, str | None]],
) -> dict[str, dict]:
    """Atomically finalize every source revision in one approved apply batch."""
    pending: list[tuple[str, str | None, str]] = []
    results: dict[str, dict] = {}
    for import_key, (state, error_code) in outcomes.items():
        if state not in {"applied", "failed"}:
            raise SeedImportStateError(
                "source import can finish only applied or failed"
            )
        if state == "failed":
            if error_code not in SEED_IMPORT_ERROR_CODES:
                raise SeedImportStateError(
                    "failed source import needs a closed error_code"
                )
        elif error_code is not None:
            raise SeedImportStateError(
                "applied source import cannot carry error_code"
            )
        row = get_seed_source_import(conn, import_key)
        if row is None:
            raise KeyError("unknown seed_source_import import_key")
        if row["state"] == state and row["error_code"] == error_code:
            results[import_key] = row
            continue
        if row["state"] != "pending":
            raise SeedImportStateError(
                f"cannot transition seed_source_import {row['state']} to {state}"
            )
        pending.append((state, error_code, import_key))

    now = _now()
    try:
        for state, error_code, import_key in pending:
            conn.execute(
                "UPDATE seed_source_import SET state = ?, error_code = ?, "
                "updated_at = ?, completed_at = ? WHERE import_key = ?",
                (state, error_code, now, now, import_key),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    for _state, _error_code, import_key in pending:
        row = get_seed_source_import(conn, import_key)
        assert row is not None
        results[import_key] = row
    return results


def get_seed_import(conn: sqlite3.Connection, import_key: str) -> dict | None:
    """Return one reviewed-candidate ledger row without changing it."""
    return _seed_ledger_row(conn, "seed_import", import_key)


def find_seed_workstream_nodes(
    conn: sqlite3.Connection, *, project_path: str, workstream_key: str,
) -> list[dict]:
    """Return distinct workstream checkpoints previously bound to a seed key."""
    rows = conn.execute(
        """
        SELECT DISTINCT n.id, n.kind, n.status
        FROM seed_import si
        JOIN nodes n ON n.id = si.node_id
        WHERE si.project_path = ?
          AND si.workstream_key = ?
          AND n.kind = 'workstream'
        ORDER BY n.id
        """,
        (project_path, workstream_key),
    ).fetchall()
    return [dict(row) for row in rows]


def find_seed_claim_nodes(
    conn: sqlite3.Connection, *, claim_key: str,
) -> list[dict]:
    """Return nodes checkpointed by any import of one exact seeded claim.

    Pending and failed checkpoints matter: ignoring them permits a later source
    batch to create a duplicate during recovery.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT n.id, n.kind, n.status, n.workstream_id,
                        n.title, n.body
        FROM seed_import si
        JOIN nodes n ON n.id = si.node_id
        WHERE si.claim_key = ?
        ORDER BY n.id
        """,
        (claim_key,),
    ).fetchall()
    return [dict(row) for row in rows]


def begin_seed_import(
    conn: sqlite3.Connection,
    *,
    import_key: str,
    claim_key: str | None = None,
    project_path: str,
    extractor_name: str,
    extractor_version: str,
    observed_at: str | None = None,
    source_import_keys: Sequence[str] = (),
    source_ids: Sequence[str] = (),
    workstream_key: str | None = None,
    workstream_id: int | None = None,
    retry_failed: bool = False,
    retry_pending: bool = False,
) -> dict:
    """Idempotently create (or fetch) a pending reviewed-candidate import."""
    metadata = {
        "claim_key": _optional_seed_metadata(claim_key),
        "source_import_keys_json": _seed_string_list(source_import_keys),
        "source_ids_json": _seed_string_list(source_ids),
        "project_path": _required_seed_metadata("project_path", project_path),
        "workstream_key": _optional_seed_metadata(workstream_key),
        "workstream_id": workstream_id,
        "extractor_name": _required_seed_metadata("extractor_name", extractor_name),
        "extractor_version": _required_seed_metadata(
            "extractor_version", extractor_version
        ),
        "observed_at": _seed_observed_at(observed_at),
    }
    key = _required_seed_metadata("import_key", import_key)
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO seed_import (
            import_key, claim_key, source_import_keys_json, source_ids_json, project_path,
            workstream_key, workstream_id, extractor_name, extractor_version,
            observed_at, state, error_code, node_id, attempt_count,
            created_at, updated_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL, 1, ?, ?, NULL)
        ON CONFLICT(import_key) DO NOTHING
        """,
        (
            key,
            metadata["claim_key"],
            metadata["source_import_keys_json"],
            metadata["source_ids_json"],
            metadata["project_path"],
            metadata["workstream_key"],
            metadata["workstream_id"],
            metadata["extractor_name"],
            metadata["extractor_version"],
            metadata["observed_at"],
            now,
            now,
        ),
    )
    created = bool(cur.rowcount)
    conn.commit()
    row = get_seed_import(conn, key)
    if row is None:  # pragma: no cover - SQLite acknowledged the insert/read
        raise SeedImportLedgerError("seed_import insert was not readable")
    # Validate every pre-existing immutable field before mutating a legacy row.
    # ``claim_key`` is omitted only when the old schema could not have stored it.
    pre_backfill_metadata = {
        name: value
        for name, value in metadata.items()
        if name != "observed_at"
        and not (name == "claim_key" and row.get("claim_key") is None)
    }
    _assert_seed_metadata(
        row, pre_backfill_metadata, table="seed_import",
    )
    # The columns were added after the original seed-v2 ledger shipped. An
    # exact import key plus matching original metadata is sufficient to safely
    # backfill their previously-NULL values on first reuse.
    backfill = {
        name: metadata[name]
        for name in ("claim_key", "observed_at")
        if row.get(name) is None and metadata[name] is not None
    }
    if backfill:
        assignments = ", ".join(f"{name} = ?" for name in backfill)
        conn.execute(
            f"UPDATE seed_import SET {assignments}, updated_at = ? "
            "WHERE import_key = ?",
            [*backfill.values(), _now(), key],
        )
        conn.commit()
        row = get_seed_import(conn, key)
        assert row is not None
    immutable_metadata = {
        name: value for name, value in metadata.items() if name != "observed_at"
    }
    _assert_seed_metadata(row, immutable_metadata, table="seed_import")
    retry_started = False
    if not created:
        retry_started = _retry_seed_ledger(
            conn,
            table="seed_import",
            import_key=key,
            retry_failed=retry_failed,
            retry_pending=retry_pending,
        )
        if retry_started:
            row = get_seed_import(conn, key)
            assert row is not None
    row["created"] = created
    row["retry_started"] = retry_started
    return row


def backfill_seed_import_receipt(
    conn: sqlite3.Connection,
    import_key: str,
    *,
    claim_key: str,
    observed_at: str | None = None,
) -> dict:
    """Fill additive receipt columns for an already-applied legacy row."""
    row = get_seed_import(conn, import_key)
    if row is None:
        raise KeyError("unknown seed_import import_key")
    normalized_claim = _required_seed_metadata("claim_key", claim_key)
    normalized_observed = _seed_observed_at(observed_at)
    if row.get("claim_key") not in {None, normalized_claim}:
        raise SeedImportConflictError("seed_import claim_key metadata mismatch")
    updates: dict[str, str] = {}
    if row.get("claim_key") is None:
        updates["claim_key"] = normalized_claim
    if row.get("observed_at") is None and normalized_observed is not None:
        updates["observed_at"] = normalized_observed
    if updates:
        assignments = ", ".join(f"{name} = ?" for name in updates)
        conn.execute(
            f"UPDATE seed_import SET {assignments}, updated_at = ? "
            "WHERE import_key = ?",
            [*updates.values(), _now(), import_key],
        )
        conn.commit()
        row = get_seed_import(conn, import_key)
        assert row is not None
    return row


def set_seed_import_node(
    conn: sqlite3.Connection, import_key: str, node_id: int,
) -> dict:
    """Attach a created node to a pending import before later batch work.

    This checkpoint lets a failed workstream-attachment step resume without
    creating the node twice.  The same node is idempotent; changing the node is
    a collision and is rejected.
    """
    row = get_seed_import(conn, import_key)
    if row is None:
        raise KeyError("unknown seed_import import_key")
    if row["node_id"] is not None:
        if row["node_id"] != node_id:
            raise SeedImportConflictError("seed_import already references another node")
        return row
    if row["state"] != "pending":
        raise SeedImportStateError("node_id can be attached only while pending")
    conn.execute(
        "UPDATE seed_import SET node_id = ?, updated_at = ? WHERE import_key = ?",
        (node_id, _now(), import_key),
    )
    conn.commit()
    result = get_seed_import(conn, import_key)
    assert result is not None
    return result


def finish_seed_import(
    conn: sqlite3.Connection,
    import_key: str,
    *,
    state: str,
    node_id: int | None = None,
    error_code: str | None = None,
) -> dict:
    """Move a pending candidate import to terminal ``applied`` or ``failed``."""
    if state not in {"applied", "failed"}:
        raise SeedImportStateError("candidate import can finish only applied or failed")
    if state == "failed":
        if error_code not in SEED_IMPORT_ERROR_CODES:
            raise SeedImportStateError("failed candidate import needs a closed error_code")
    elif error_code is not None:
        raise SeedImportStateError("applied candidate import cannot carry error_code")
    row = get_seed_import(conn, import_key)
    if row is None:
        raise KeyError("unknown seed_import import_key")
    if row["node_id"] is not None and node_id is not None and row["node_id"] != node_id:
        raise SeedImportConflictError("seed_import already references another node")
    resolved_node_id = node_id if node_id is not None else row["node_id"]
    if state == "applied" and resolved_node_id is None:
        raise SeedImportStateError("applied candidate import needs node_id")
    if (
        row["state"] == state
        and row["error_code"] == error_code
        and row["node_id"] == resolved_node_id
    ):
        return row
    if row["state"] != "pending":
        raise SeedImportStateError(
            f"cannot transition seed_import {row['state']} to {state}"
        )
    now = _now()
    conn.execute(
        "UPDATE seed_import SET state = ?, error_code = ?, node_id = ?, "
        "updated_at = ?, completed_at = ? WHERE import_key = ?",
        (state, error_code, resolved_node_id, now, now, import_key),
    )
    conn.commit()
    result = get_seed_import(conn, import_key)
    assert result is not None
    return result


# ---------- nodes ----------

def insert_node(
    conn: sqlite3.Connection,
    *,
    kind: str,
    title: str,
    body: str,
    status: str = "staging",
    session_id: str | None = None,
    embedding: bytes | None = None,
    workstream_id: int | None = None,
) -> int:
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO nodes (kind, title, body, status, session_id, embedding,
                           created_at, updated_at, last_referenced_at,
                           created_by, updated_by, workstream_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, title, body, status, session_id, embedding, now, now, now,
         _ACTOR, _ACTOR, workstream_id),
    )
    nid = cur.lastrowid
    if embedding is not None and vec_loaded(conn):
        conn.execute(
            "INSERT INTO vec_nodes(rowid, embedding) VALUES (?, ?)",
            (nid, embedding),
        )
    conn.commit()
    return nid


def update_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    embedding: bytes | None = None,
) -> None:
    fields, values = [], []
    if title is not None:
        fields.append("title = ?"); values.append(title)
    if body is not None:
        fields.append("body = ?"); values.append(body)
    if status is not None:
        fields.append("status = ?"); values.append(status)
    if embedding is not None:
        fields.append("embedding = ?"); values.append(embedding)
    if not fields:
        return
    fields.append("updated_at = ?"); values.append(_now())
    fields.append("updated_by = ?"); values.append(_ACTOR)
    values.append(node_id)
    conn.execute(f"UPDATE nodes SET {', '.join(fields)} WHERE id = ?", values)
    if embedding is not None and vec_loaded(conn):
        conn.execute("DELETE FROM vec_nodes WHERE rowid = ?", (node_id,))
        conn.execute(
            "INSERT INTO vec_nodes(rowid, embedding) VALUES (?, ?)",
            (node_id, embedding),
        )
    conn.commit()


# ---------- payload size guardrails (compact-by-default for MCP tool returns) ----------
#
# Default excerpt length for `body_excerpt` when a row is compacted. Sized to
# fit one decision-rationale paragraph for typical fact/decision nodes — see
# docs/claude_kb/mcp_payload_guards.md for the rationale and tuning surfaces.
COMPACT_BODY_CHARS = 800


def compact_row(row: dict, *, body_chars: int = COMPACT_BODY_CHARS,
                snippet_text: str | None = None) -> dict:
    """Return a copy of `row` with `body` replaced by a bounded `body_excerpt`
    (+ a `body_chars` field carrying the true length) so MCP tool responses
    stay under the response cap. The full body is still on disk; the agent
    drills in via `kb_get(<id>)`.

    `snippet_text` (optional) — pre-computed FTS5 snippet for the matched span.
    Used by `kb_search` so the excerpt surfaces *what matched* rather than the
    leading prefix. Falls back to a prefix excerpt when None.
    """
    out = dict(row)
    out.pop("embedding", None)
    body = out.pop("body", None)
    if body is None:
        out["body_excerpt"] = ""
        out["body_chars"] = 0
        return out
    full_len = len(body)
    out["body_chars"] = full_len
    if snippet_text:
        out["body_excerpt"] = snippet_text
    elif full_len <= body_chars:
        out["body_excerpt"] = body
    else:
        out["body_excerpt"] = body[:body_chars].rstrip() + "…"
    return out


def get_node(conn: sqlite3.Connection, node_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return dict(row) if row else None


def neighbors(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT e.relation, e.src, e.dst, n.id, n.kind, n.title, n.status
        FROM edges e
        JOIN nodes n ON n.id = CASE WHEN e.src = ? THEN e.dst ELSE e.src END
        WHERE (e.src = ? OR e.dst = ?)
          AND e.status = 'active'
        """,
        (node_id, node_id, node_id),
    ).fetchall()
    return [dict(r) for r in rows]


def reconciliation_banner(
    conn: sqlite3.Connection, node_id: int,
) -> list[dict]:
    """Return non-stale nodes that this node has been reconciled by.

    Edge convention: outgoing `reconciled_by` (src=this_node, dst=reconciler).
    When non-empty, the reader MUST also fetch the reconciling nodes before
    treating this node's framing as authoritative — see CLAUDE.md
    "KB read hygiene" / "reconciled_by" rule.

    Distinct from `supersedes` (full replacement, marks old stale). Here the
    old node is still factually true in its scope; the newer node constrains
    or updates a parameter / framing element. Both stay canonical.
    """
    rows = conn.execute(
        """
        SELECT e.dst AS linked_id, n.kind AS kind, n.title AS title,
               n.status AS status
        FROM edges e
        JOIN nodes n ON n.id = e.dst
        WHERE e.src = ?
          AND e.relation = 'reconciled_by'
          AND e.status = 'active'
          AND COALESCE(n.status, '') != 'stale'
        ORDER BY e.dst ASC
        """,
        (node_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_nodes(
    conn: sqlite3.Connection,
    *,
    session_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    created_by: str | None = None,
    limit: int = 20,
    include_stale: bool = False,
) -> list[dict]:
    """Stale nodes are excluded by default; pass status='stale' for audits,
    or include_stale=True to see everything."""
    where, params = [], []
    if session_id is not None:
        where.append("session_id = ?"); params.append(session_id)
    if kind is not None:
        where.append("kind = ?"); params.append(kind)
    if status is not None:
        where.append("status = ?"); params.append(status)
    elif not include_stale:
        where.append("status != 'stale'")
    if created_by is not None:
        where.append("created_by = ?"); params.append(created_by)
    sql = "SELECT * FROM nodes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def node_count(conn: sqlite3.Connection, *, include_stale: bool = False) -> int:
    """Total node count, stale excluded by default. A single COUNT(*) — no
    embeddings/numpy — so it is safe to call on the hot SessionStart path.
    Used to detect a near-empty (new-user) KB for the getting-started brief."""
    sql = "SELECT COUNT(*) FROM nodes"
    if not include_stale:
        sql += " WHERE status != 'stale'"
    return int(conn.execute(sql).fetchone()[0])


# ---------- ref-count / promotion / decay (step 4) ----------

def bump_ref_count(conn: sqlite3.Connection, node_ids: Sequence[int]) -> None:
    """Increment ref_count and stamp last_referenced_at for the given nodes.

    Critical invariant: DOES NOT touch `updated_at`. Recency-of-reference and
    recency-of-edit are independent signals for the healer — conflating them
    would make heavily-referenced nodes look perpetually fresh and beat
    genuine updates.
    """
    if not node_ids:
        return
    now = _now()
    placeholders = ",".join("?" for _ in node_ids)
    conn.execute(
        f"UPDATE nodes SET ref_count = ref_count + 1, last_referenced_at = ? "
        f"WHERE id IN ({placeholders})",
        [now, *node_ids],
    )
    conn.commit()


def promote_by_ref_count(
    conn: sqlite3.Connection,
    *,
    min_ref_count: int = 3,
) -> list[int]:
    """Promote eligible staging nodes when ref_count reaches the threshold.

    Imported seed nodes remain staging until explicit review; citation volume
    is relevance evidence, not user authority. Status change IS an edit, so
    updated_at bumps here (unlike bump_ref_count). Stale nodes are untouched.
    """
    rows = conn.execute(
        "SELECT n.id FROM nodes n "
        "WHERE n.status = 'staging' AND n.ref_count >= ? "
        "AND instr(COALESCE(n.body, ''), 'Latch-Seed-Import-Key:') = 0 "
        "AND NOT EXISTS (SELECT 1 FROM seed_import si "
        "WHERE si.node_id = n.id)",
        (min_ref_count,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    if not ids:
        return []
    now = _now()
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE nodes SET status = 'canonical', updated_at = ? "
        f"WHERE id IN ({placeholders})",
        [now, *ids],
    )
    conn.commit()
    return ids


def apply_ref_count_decay(
    conn: sqlite3.Connection,
    *,
    factor: float = 0.9,
    floor: int = 1,
) -> int:
    """Multiplicative decay on ref_count — weekly job.

    Only nodes with ref_count >= 1 are touched (never-accessed nodes stay at 0).
    Post-decay value is max(floor, round(ref_count * factor)), so once a node
    has been referenced it survives decay indefinitely. Does not touch
    updated_at. Returns rows affected."""
    cur = conn.execute(
        "UPDATE nodes SET ref_count = MAX(?, CAST(ROUND(ref_count * ?) AS INTEGER)) "
        "WHERE ref_count > 0",
        (floor, factor),
    )
    conn.commit()
    return cur.rowcount or 0


# ---------- edges ----------

# Step 9 canonical traversal set — kb_gate walks these direction-aware.
# Anything not in this set stays free-form (visible in neighborhoods, not
# directionally walked). See docs/claude_kb/step9_infra_design.md §3.1.
CANONICAL_TRAVERSAL_RELATIONS = frozenset({
    "supersedes",
    "replaces",
    "constrains",
    "motivates",
    "tested_against",
    "depends_on",
})

# Synonyms map onto a canonical traversal relation.
_TRAVERSAL_SYNONYMS = {
    "replaced_by":      "replaces",       # caller responsible for direction flip
    "requires":         "depends_on",
    "constrained_by":   "constrains",
    "motivated_by":     "motivates",
    "tested":           "tested_against",
}

# Free-form synonyms (not canonical, just hygiene — unify spellings).
_FREEFORM_SYNONYMS = {
    "relates_to":   "related_to",
}


def canonicalize_relation(rel: str) -> str:
    """Return the canonical spelling of a relation. Maps known synonyms;
    returns the input unchanged otherwise."""
    if rel in _TRAVERSAL_SYNONYMS:
        return _TRAVERSAL_SYNONYMS[rel]
    if rel in _FREEFORM_SYNONYMS:
        return _FREEFORM_SYNONYMS[rel]
    return rel


def is_traversal_relation(rel: str) -> bool:
    """True if this relation is in the canonical traversal set used by kb_gate."""
    return canonicalize_relation(rel) in CANONICAL_TRAVERSAL_RELATIONS


# Relations whose linkage represents a judgment-quality event worth logging:
# old framing got replaced (supersedes/replaces) or partially constrained
# (reconciled_by). Emits one reconciliation.log row per call per KB id=1097.
RECONCILIATION_RELATIONS = frozenset({"supersedes", "replaces", "reconciled_by"})


def add_edge(
    conn: sqlite3.Connection,
    src: int,
    dst: int,
    relation: str,
    *,
    project_path: str | None = None,
    session_id: str | None = None,
) -> None:
    """Add an edge. The `relation` value is canonicalized on insert via
    `canonicalize_relation` so synonyms like `relates_to` / `requires` are
    rewritten to their canonical forms (`related_to` / `depends_on`) before
    storage. New synonyms added to the maps automatically apply going forward.

    Re-linking a tombstoned edge re-activates it (status flipped back to
    'active'). Original created_at / created_by are preserved — the row is
    audit-stable. The UNIQUE(src, dst, relation) constraint keeps at most one
    row per logical edge regardless of lifecycle.

    `project_path` and `session_id` are used only for reconciliation.log
    emission (KB id=1097). Callers that route through the on-insert heal or
    nightly heal paths pass them through; direct kb_link MCP callers pass
    them from session context. Missing values fall back to null in the row's
    common header.
    """
    canonical = canonicalize_relation(relation)
    t0 = time.perf_counter()
    pre_capture: dict | None = None
    if canonical in RECONCILIATION_RELATIONS:
        pre_capture = _capture_reconciliation_state(conn, src, dst, canonical)

    conn.execute(
        "INSERT INTO edges (src, dst, relation, status, created_at, created_by) "
        "VALUES (?, ?, ?, 'active', ?, ?) "
        "ON CONFLICT(src, dst, relation) DO UPDATE SET status = 'active' "
        "WHERE edges.status = 'tombstoned'",
        (src, dst, canonical, _now(), _ACTOR),
    )
    conn.commit()

    if pre_capture is not None:
        pre_capture["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        log_utils.emit_event(
            "reconciliation", pre_capture,
            project_path=project_path,
            session_id=session_id,
        )


def _capture_reconciliation_state(
    conn: sqlite3.Connection, edge_src: int, edge_dst: int, canonical_relation: str,
) -> dict | None:
    """Capture point-in-time scalars for a reconciliation.log row.

    Resolves the SEMANTIC src — the "constrained" node — which is:
      * `edge_dst` for `supersedes` and `replaces` (winner→loser convention)
      * `edge_src` for `reconciled_by` (older→newer convention,
        per `heal.apply_nightly_reconciled_by`)

    Captures status/ref_count/created_at on the constrained node BEFORE the
    edge is inserted; callers that mutate the node (e.g. `apply_supersede`)
    MUST run their status update AFTER `add_edge` returns, or the capture
    will reflect post-mutation state — the regression guarded by
    `test_reconciliation_log_captures_pre_supersede_status`.

    Returns None when either node is missing (the edge INSERT will then fail
    on FK and no row should be emitted).
    """
    if canonical_relation in ("supersedes", "replaces"):
        constrained_id, other_id = edge_dst, edge_src
    else:  # reconciled_by
        constrained_id, other_id = edge_src, edge_dst

    src_row = conn.execute(
        "SELECT kind, status, created_at, ref_count "
        "FROM nodes WHERE id = ?",
        (constrained_id,),
    ).fetchone()
    if src_row is None:
        return None
    dst_row = conn.execute(
        "SELECT kind FROM nodes WHERE id = ?",
        (other_id,),
    ).fetchone()
    if dst_row is None:
        return None

    session_touch = conn.execute(
        "SELECT COUNT(DISTINCT session_id) "
        "FROM session_retrievals WHERE node_id = ?",
        (constrained_id,),
    ).fetchone()
    session_touch_count = int(session_touch[0]) if session_touch else 0

    age_days = _days_since(src_row["created_at"])

    return {
        "src_id": constrained_id,
        "src_kind": src_row["kind"],
        "src_status_before": src_row["status"],
        "dst_id": other_id,
        "dst_kind": dst_row["kind"],
        "relation": canonical_relation,
        "src_ref_count_at_event": int(src_row["ref_count"] or 0),
        "src_age_days": age_days,
        "src_session_touch_count": session_touch_count,
    }


def _days_since(created_at_str: str | None) -> float | None:
    """Return (now - created_at) in days as a float; None if unparseable."""
    t = _parse_ts(created_at_str)
    if t is None:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 86400.0


def tombstone_edge(
    conn: sqlite3.Connection, src: int, dst: int, relation: str,
) -> int:
    """Soft-delete an edge by flipping `status` to 'tombstoned'. Idempotent —
    a missing or already-tombstoned edge is a no-op. Returns the number of
    rows touched (0 = no-op, 1 = tombstoned an active edge).

    Mirrors the node-stale idiom: rows persist for audit but are filtered out
    of every edge-walking read site (neighbors, reconciliation_banner, gate
    traversal, plan_freshness_hint, UserPromptSubmit graph hop). Use when a
    body refactor invalidates an existing edge so the body and edge structure
    stay in sync.

    Relation is canonicalized before lookup (mirrors `add_edge`), so
    `tombstone_edge(a, b, "relates_to")` hits the canonical `related_to` row.
    """
    cur = conn.execute(
        "UPDATE edges SET status = 'tombstoned' "
        "WHERE src = ? AND dst = ? AND relation = ? AND status = 'active'",
        (src, dst, canonicalize_relation(relation)),
    )
    conn.commit()
    return cur.rowcount or 0


# ---------- sessions ----------

def upsert_session(
    conn: sqlite3.Connection,
    session_id: str,
    project_path: str,
    transcript_path: str | None = None,
) -> dict:
    existing = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO sessions (id, project_path, started_at, transcript_path, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, project_path, _now(), transcript_path, _ACTOR),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())
    if transcript_path and not existing["transcript_path"]:
        conn.execute("UPDATE sessions SET transcript_path = ? WHERE id = ?", (transcript_path, session_id))
        conn.commit()
    return dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


def get_session(conn: sqlite3.Connection, session_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def increment_turn(conn: sqlite3.Connection, session_id: str) -> int:
    conn.execute("UPDATE sessions SET turn_count = turn_count + 1 WHERE id = ?", (session_id,))
    conn.commit()
    row = conn.execute("SELECT turn_count FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row["turn_count"] if row else 0


def set_pending_cite_nudge(conn: sqlite3.Connection, session_id: str, count: int) -> None:
    """Set the session's pending cite-nudge count (Stop-hook 3-B detector).
    No-op if the session row doesn't exist yet (the Stop hook upserts it first)."""
    conn.execute(
        "UPDATE sessions SET pending_cite_nudge = ? WHERE id = ?",
        (int(count), session_id),
    )
    conn.commit()


def take_pending_cite_nudge(conn: sqlite3.Connection, session_id: str) -> int:
    """Read AND reset the pending cite-nudge marker (consumed by the next
    UserPromptSubmit). Returns the count (0 when absent / no session row).
    Only writes when there was something to clear, so the common unbound /
    no-flag case stays read-only."""
    row = conn.execute(
        "SELECT pending_cite_nudge FROM sessions WHERE id = ?", (session_id,),
    ).fetchone()
    if row is None:
        return 0
    count = row["pending_cite_nudge"] or 0
    if count:
        conn.execute(
            "UPDATE sessions SET pending_cite_nudge = 0 WHERE id = ?", (session_id,),
        )
        conn.commit()
    return int(count)


def mark_compacted(conn: sqlite3.Connection, session_id: str, turn: int, summary_node_id: int | None = None) -> None:
    if summary_node_id is not None:
        conn.execute(
            "UPDATE sessions SET last_compact_turn = ?, summary_node_id = ? WHERE id = ?",
            (turn, summary_node_id, session_id),
        )
    else:
        conn.execute(
            "UPDATE sessions SET last_compact_turn = ? WHERE id = ?",
            (turn, session_id),
        )
    conn.commit()


def mark_ended(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                 (_now(), session_id))
    conn.commit()


def update_last_prompt_embedding(
    conn: sqlite3.Connection, session_id: str, embedding: bytes,
) -> None:
    """Stash the most recent user prompt embedding on the session row so
    UserPromptSubmit can compute topic-shift cosine for the next turn."""
    conn.execute(
        "UPDATE sessions SET last_prompt_embedding = ?, last_prompt_at = ? WHERE id = ?",
        (embedding, _now(), session_id),
    )
    conn.commit()


def get_last_prompt_embedding(
    conn: sqlite3.Connection, session_id: str,
) -> bytes | None:
    row = conn.execute(
        "SELECT last_prompt_embedding FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    return row["last_prompt_embedding"]


# ---------- per-session active set (UserPromptSubmit dedupe) ----------

ACTIVE_SET_TTL_TURNS = 20  # nodes injected this many turns ago drop out of active


def record_retrievals(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn: int,
    items: Iterable[tuple[int, float | None]],
    source: str,
) -> int:
    """Upsert (session_id, node_id) rows. New rows record sim_at_first; repeat
    hits bump hit_count + last_injected_at/turn. Returns rows touched."""
    items = list(items)
    if not items:
        return 0
    now = _now()
    n = 0
    for node_id, sim in items:
        existing = conn.execute(
            "SELECT hit_count FROM session_retrievals WHERE session_id = ? AND node_id = ?",
            (session_id, node_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO session_retrievals
                  (session_id, node_id, first_injected_at, last_injected_at,
                   first_injected_turn, last_injected_turn, hit_count, sim_at_first, source)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (session_id, node_id, now, now, turn, turn, sim, source),
            )
        else:
            conn.execute(
                """
                UPDATE session_retrievals
                  SET last_injected_at = ?, last_injected_turn = ?, hit_count = hit_count + 1
                WHERE session_id = ? AND node_id = ?
                """,
                (now, turn, session_id, node_id),
            )
        n += 1
    conn.commit()
    return n


def get_active_set(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    current_turn: int,
    ttl_turns: int = ACTIVE_SET_TTL_TURNS,
) -> set[int]:
    """Active node ids for this session — rows whose last_injected_turn falls
    within the TTL window. Older rows are still in the table (audit trail) but
    not 'active' — so a node injected 21+ turns ago can re-surface in retrieval."""
    cutoff = current_turn - ttl_turns
    rows = conn.execute(
        """
        SELECT node_id FROM session_retrievals
        WHERE session_id = ? AND last_injected_turn >= ?
        """,
        (session_id, cutoff),
    ).fetchall()
    return {r["node_id"] for r in rows}


def get_active_with_meta(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    current_turn: int,
    ttl_turns: int = ACTIVE_SET_TTL_TURNS,
) -> list[dict]:
    """Active set rows joined to nodes — for graph traversal C2 path. Sorted
    by recency-of-last-inject DESC so the most recent context is first."""
    cutoff = current_turn - ttl_turns
    rows = conn.execute(
        """
        SELECT sr.node_id AS id, sr.last_injected_turn, sr.hit_count, sr.source,
               n.kind, n.title
        FROM session_retrievals sr
        JOIN nodes n ON n.id = sr.node_id
        WHERE sr.session_id = ? AND sr.last_injected_turn >= ?
        ORDER BY sr.last_injected_turn DESC, sr.hit_count DESC
        """,
        (session_id, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def orphaned_sessions(conn: sqlite3.Connection, project_path: str) -> list[dict]:
    """Sessions that never got a SessionEnd but had work since their last compact."""
    rows = conn.execute(
        """
        SELECT * FROM sessions
        WHERE project_path = ?
          AND ended_at IS NULL
          AND turn_count > last_compact_turn
        """,
        (project_path,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------- FTS ----------

def fts_search(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 50,
    *,
    include_stale: bool = False,
) -> list[dict]:
    if not query.strip():
        return []
    safe = _sanitize_fts(query)
    stale_clause = "" if include_stale else " AND n.status != 'stale'"
    # FTS5 snippet on the body column (col index 1 in nodes_fts: title=0, body=1).
    # 32-token window with "…" delimiter — surfaces *what matched* rather than
    # a leading prefix, so kb_search compact returns highlight the relevant
    # span. Consumed by db.compact_row(snippet_text=...).
    rows = conn.execute(
        f"""
        SELECT n.*, bm25(nodes_fts) AS score,
               snippet(nodes_fts, 1, '', '', '…', 32) AS _fts_snippet
        FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.rowid
        WHERE nodes_fts MATCH ?{stale_clause}
        ORDER BY score
        LIMIT ?
        """,
        (safe, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _sanitize_fts(query: str) -> str:
    """FTS5 has reserved characters; quote tokens to keep the query literal."""
    tokens = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in tokens) if tokens else '""'


# ---------- focus (step 9 §4.3) ----------

# Cap on auto-bumped active rows. Pinned rows persist beyond the cap. The
# render path (SessionStart brief) shows pinned + top-FOCUS_CAP auto.
FOCUS_CAP = 3
# Multiplicative decay applied per hour elapsed since the row was last
# bumped. Stored score drifts over time — true score = stored * decay^h.
FOCUS_DECAY_PER_HOUR = 0.95
# Default activity bump (kb_get / kb_insert / kb_update / search-survives).
FOCUS_DEFAULT_DELTA = 1.0
# Larger boost when an advanced/internal caller explicitly sets focus.
FOCUS_USER_BOOST = 5.0


def _decay_score(stored: float, set_at_str: str | None) -> float:
    """Apply continuous hourly decay since `set_at`. The score column on disk
    drifts because bumping only stamps set_at on touched rows; rank-time call
    rehydrates the effective score for ordering."""
    ts = _parse_ts(set_at_str)
    if ts is None:
        return float(stored)
    elapsed_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    if elapsed_hours <= 0:
        return float(stored)
    return float(stored) * (FOCUS_DECAY_PER_HOUR ** elapsed_hours)


def _resolve_workstream_id(conn: sqlite3.Connection, node_id: int) -> int | None:
    """Return the workstream id this node belongs to. If the node itself is a
    workstream, returns its own id. Otherwise returns nodes.workstream_id (may
    be NULL — orphan nodes are tolerated and don't drive focus)."""
    row = conn.execute(
        "SELECT id, kind, workstream_id FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None:
        return None
    if row["kind"] == "workstream":
        return int(row["id"])
    wid = row["workstream_id"]
    return int(wid) if wid is not None else None


def bump_focus(
    conn: sqlite3.Connection,
    workstream_id: int | None,
    *,
    delta: float = FOCUS_DEFAULT_DELTA,
    set_by: str = "auto",
) -> None:
    """Add `delta` to the focus row for `workstream_id` (creating it if absent).
    Decay is applied to the existing stored score before adding delta, so
    `score` on disk reflects the freshly-decayed-then-bumped value at
    `set_at = now`. Re-ranks but does NOT evict — eviction-on-every-bump
    starves fresh workstreams that haven't yet outscored stale ones. The
    "top 3 active" cap is enforced at read time by `get_focus(limit=3)`;
    decay handles long-term fade. `prune_focus` is the explicit storage-hygiene
    knob for callers who want to bound table growth.

    `workstream_id` may be NULL — silently no-op (orphan nodes don't drive focus).
    """
    if workstream_id is None:
        return
    # Defensive: the focus row's PK is FK'd to nodes(id) ON DELETE CASCADE.
    # If the node id doesn't exist or isn't kind='workstream', skip — we'd
    # otherwise create a row that fails FK at commit (or worse, attaches focus
    # to a leaf node).
    row = conn.execute(
        "SELECT kind FROM nodes WHERE id = ?", (workstream_id,)
    ).fetchone()
    if row is None or row["kind"] != "workstream":
        return

    now = _now()
    existing = conn.execute(
        "SELECT score, set_at FROM focus WHERE workstream_id = ?", (workstream_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO focus (workstream_id, rank, score, set_at, set_by, pinned) "
            "VALUES (?, 0, ?, ?, ?, 0)",
            (workstream_id, float(delta), now, set_by),
        )
    else:
        decayed = _decay_score(existing["score"], existing["set_at"])
        new_score = decayed + float(delta)
        conn.execute(
            "UPDATE focus SET score = ?, set_at = ?, set_by = ? WHERE workstream_id = ?",
            (new_score, now, set_by, workstream_id),
        )
    conn.commit()
    _recompute_focus_ranks(conn)


def bump_focus_for_nodes(
    conn: sqlite3.Connection,
    node_ids: Iterable[int],
    *,
    delta: float = FOCUS_DEFAULT_DELTA,
    set_by: str = "auto",
) -> None:
    """Resolve each node's workstream and bump it. Multiple nodes pointing at
    the same workstream collapse to a single bump (avoid pile-on from a
    search that returns 5 nodes from one workstream)."""
    workstreams: set[int] = set()
    for nid in node_ids:
        wid = _resolve_workstream_id(conn, nid)
        if wid is not None:
            workstreams.add(wid)
    for wid in workstreams:
        bump_focus(conn, wid, delta=delta, set_by=set_by)


def set_focus(
    conn: sqlite3.Connection, workstream_id: int, *, set_by: str = "user",
) -> None:
    """Explicit focus set. Heavy boost so it lands at top."""
    bump_focus(conn, workstream_id, delta=FOCUS_USER_BOOST, set_by=set_by)


def pin_focus(conn: sqlite3.Connection, workstream_id: int) -> bool:
    """Insert focus row if missing, set pinned=1. Pinned rows are exempt from
    eviction. Returns True if the workstream id is a valid workstream node."""
    row = conn.execute(
        "SELECT kind FROM nodes WHERE id = ?", (workstream_id,)
    ).fetchone()
    if row is None or row["kind"] != "workstream":
        return False
    bump_focus(conn, workstream_id, delta=0.0, set_by="user")
    conn.execute(
        "UPDATE focus SET pinned = 1 WHERE workstream_id = ?", (workstream_id,)
    )
    conn.commit()
    _recompute_focus_ranks(conn)
    return True


def unpin_focus(conn: sqlite3.Connection, workstream_id: int) -> None:
    conn.execute(
        "UPDATE focus SET pinned = 0 WHERE workstream_id = ?", (workstream_id,)
    )
    conn.commit()
    _recompute_focus_ranks(conn)


def drop_focus(conn: sqlite3.Connection, workstream_id: int) -> None:
    """Hard remove from focus table (loses score history). Use sparingly —
    decay alone usually suffices for stale workstreams."""
    conn.execute("DELETE FROM focus WHERE workstream_id = ?", (workstream_id,))
    conn.commit()
    _recompute_focus_ranks(conn)


def prune_focus(
    conn: sqlite3.Connection, *, cap: int = FOCUS_CAP,
) -> int:
    """Storage hygiene — keep top `cap` non-pinned rows (by decayed score)
    plus all pinned rows; delete the rest. Returns rows deleted.

    NOT called on every bump (that starves freshly-bumped workstreams). Call
    explicitly from maintenance jobs, focus prune, or when the table grows
    large. Decay alone usually handles natural fade — pruning is for callers
    who want a tight bound on table size."""
    rows = conn.execute(
        "SELECT workstream_id, score, set_at, pinned FROM focus"
    ).fetchall()
    if not rows:
        return 0
    ranked: list[tuple[float, int, int]] = []
    for r in rows:
        eff = _decay_score(r["score"], r["set_at"])
        ranked.append((eff, int(r["workstream_id"]), int(r["pinned"])))
    ranked.sort(key=lambda t: -t[0])
    survivors: set[int] = set()
    auto_seen = 0
    for _eff, wid, pinned in ranked:
        if pinned:
            survivors.add(wid)
        elif auto_seen < cap:
            survivors.add(wid)
            auto_seen += 1
    to_evict = [wid for _, wid, _ in ranked if wid not in survivors]
    if not to_evict:
        return 0
    placeholders = ",".join("?" for _ in to_evict)
    conn.execute(
        f"DELETE FROM focus WHERE workstream_id IN ({placeholders})", to_evict
    )
    conn.commit()
    _recompute_focus_ranks(conn)
    return len(to_evict)


def _recompute_focus_ranks(conn: sqlite3.Connection) -> None:
    """Set `rank` to 1..N from the current decayed-score order, pinned first.
    rank is informational — get_focus re-sorts on read."""
    rows = conn.execute(
        "SELECT workstream_id, score, set_at, pinned FROM focus"
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda r: (-int(r["pinned"]), -_decay_score(r["score"], r["set_at"])),
    )
    for i, r in enumerate(ranked, start=1):
        conn.execute(
            "UPDATE focus SET rank = ? WHERE workstream_id = ?",
            (i, r["workstream_id"]),
        )
    conn.commit()


def get_focus(
    conn: sqlite3.Connection, *, limit: int = FOCUS_CAP,
) -> list[dict]:
    """Return active focus rows joined with workstream node fields, sorted
    pinned-first then decayed-score-desc. Skips stale workstream nodes.
    `effective_score` field carries the decay-adjusted ranking score."""
    rows = conn.execute(
        """
        SELECT f.workstream_id, f.score, f.set_at, f.set_by, f.pinned, f.rank,
               n.id, n.kind, n.title, n.body, n.status, n.updated_at,
               n.created_by, n.updated_by
        FROM focus f
        JOIN nodes n ON n.id = f.workstream_id
        WHERE n.status != 'stale'
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["effective_score"] = _decay_score(r["score"], r["set_at"])
        out.append(d)
    out.sort(key=lambda d: (-int(d["pinned"]), -d["effective_score"]))
    return out[:limit] if limit and limit > 0 else out
