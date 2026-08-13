"""SQLite helpers for claude_kb.

Connection lifecycle is intentionally simple: open, do work, close. WAL mode
makes concurrent reads/writes safe for our usage pattern (MCP server + hooks).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import log_utils
import schema_version
import vault_identity
from paths import SCHEMA_PATH, db_path, ensure_project_dir


VEC_DIM = 384  # all-MiniLM-L6-v2

# V3 authority lanes (ratified by KB id=5143).  Only kinds that represent
# human judgment require ratification before acquiring canonical authority.
# Evidence promotion is intentionally narrower than "all non-judgment kinds":
# entity/idea/open_question receive no new unattended promotion authority.
JUDGMENT_KINDS = frozenset({"decision", "preference"})
EVIDENCE_PROMOTION_KINDS = frozenset({"fact", "progress"})
RATIFICATION_ACTIONS = frozenset({"ratify", "reject"})
RATIFICATION_SCOPES = frozenset({"node"})
RATIFICATION_SOURCES = frozenset({"capture_decision", "latch_update"})

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

WORKSTREAM_OPS = frozenset({"OPEN", "MERGE", "CLOSE", "REOPEN", "ADOPT", "UNMERGE"})
WORKSTREAM_OP_STATES = frozenset({
    "pending", "applied", "rejected", "failed", "orphaned_by_restore",
})
WORKSTREAM_OP_ERROR_CODES = frozenset({
    "preflight_stale",
    "invalid_op",
    "invalid_target",
    "invalid_payload",
    "payload_insufficient",
    "conflict",
    "blocked",
    "awaiting_charter",
    "rank_conflict",
    "open_feeders",
    "quiescence",
    "mutation_failed",
    "orphaned_by_restore",
    "internal",
})
WORKSTREAM_EVENT_VERDICTS = frozenset({"agree", "disagree", "unsure"})
RETRIEVAL_DROPPED_META_KEY = "dropped_retrieval_events"

# Reversal sufficiency is checked when an operation becomes applied. Failed or
# rejected rows did not mutate state and therefore need no reversal payload.
WORKSTREAM_REVERSAL_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"assigned_member_ids", "watch_pair", "probation"}),
    "MERGE": frozenset({
        "repointed_member_ids",
        "prior_memberships",
        "rehomed_edge_ids",
        "tombstoned_edge_ids",
        "edge_rehomes",
        "retired_priority_ids",
        "readded_priority_ids",
        "overflow_retired_priority_ids",
        "priority_map",
        "priority_snapshots",
        "created_priority_snapshots",
        "src_focus",
        "dst_focus",
        "post_focus",
        "rolling_line",
        "rolling_op_key",
        "absorber_body_before",
        "absorber_body_before_hash",
        "absorber_body_after_hash",
        "source_body_hash",
        "source_title",
        "source_prior_status",
        "merge_edge_id",
    }),
    "CLOSE": frozenset({
        "feeder_disposition_edge_ids", "focus", "retired_priority_ids",
        "priority_snapshots",
    }),
    "REOPEN": frozenset({"prior_status"}),
    "ADOPT": frozenset({"assigned_member_ids"}),
    "UNMERGE": frozenset({"merge_op_key"}),
}


class SeedImportLedgerError(ValueError):
    """Base class for invalid seed-ledger operations."""


class RatificationRequiredError(ValueError):
    """A judgment node tried to acquire canonical authority without proof."""


class SeedImportConflictError(SeedImportLedgerError):
    """An import key was reused with different immutable metadata."""


class SeedImportStateError(SeedImportLedgerError):
    """A requested seed-ledger state transition is invalid."""


class WorkstreamLedgerError(ValueError):
    """Base class for invalid workstream-ledger operations."""


class WorkstreamLedgerConflictError(WorkstreamLedgerError):
    """An idempotency key was reused with different immutable metadata."""


class WorkstreamLedgerStateError(WorkstreamLedgerError):
    """A requested workstream-ledger state transition is invalid."""


class WorkstreamPayloadError(WorkstreamLedgerError):
    """A lifecycle payload cannot satisfy the operation's reversal contract."""


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
        existing_identity = (
            vault_identity.read_identity(Path(path)) if had_nodes else None
        )
        if had_nodes and installed_schema < schema_version.KB_SCHEMA_VERSION:
            schema_version.backup_connection(
                conn,
                Path(path),
                from_version=installed_schema,
                to_version=schema_version.KB_SCHEMA_VERSION,
            )
        elif had_nodes and existing_identity is None:
            # Identity adoption is itself a mutation. Freeze an external,
            # verified production baseline first even when no schema migration
            # is due.
            import vault_backup

            vault_backup.create_pre_migration_snapshot(
                conn,
                Path(path),
                from_version=installed_schema,
                to_version=installed_schema,
                reason="identity-adoption",
            )

        migration_due = installed_schema < schema_version.KB_SCHEMA_VERSION
        if existing_identity is not None:
            # Reserve the current compatibility boundary before any current-
            # only trigger/DDL repair. Old writers then refuse even if this
            # process stops partway through an idempotent migration.
            if migration_due:
                schema_version.stamp_current(conn, record_migration=True)
            conn._kb_vault_identity = vault_identity.ensure_identity(
                conn, Path(path).parent, new_vault=False
            )
            # Existing identity and registry are validated before ordinary
            # schema repair or optional native extension setup.
            _load_vec(conn)
            _ensure_schema(conn)
        else:
            # New or legacy-unidentified vaults have no identity to validate.
            # Complete the idempotent schema chain, durably fence this writer
            # version, and only then commit the first v3 identity row.
            _load_vec(conn)
            _ensure_schema(conn)
            schema_version.stamp_current(
                conn,
                record_migration=(not had_nodes or migration_due),
            )
            conn._kb_vault_identity = vault_identity.ensure_identity(
                conn, Path(path).parent, new_vault=not had_nodes
            )
        schema_version.stamp_current(
            conn,
            record_migration=(not had_nodes or migration_due),
        )
        return conn
    except Exception:
        conn.close()
        raise


def connect_readonly(cwd: str | None = None) -> sqlite3.Connection:
    """Open an existing, current-schema KB without any setup writes.

    Diagnostics and other read-only surfaces must remain usable when a pinned
    vault is readable but intentionally outside the host's writable sandbox.
    This connector never creates a directory or database, migrates a schema,
    stamps metadata, or commits.
    """
    path = Path(db_path(cwd)).expanduser().resolve()
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, factory=_Connection)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        installed_schema = schema_version.ensure_supported(conn)
        if installed_schema < schema_version.KB_SCHEMA_VERSION:
            raise schema_version.SchemaMigrationRequiredError(
                f"KB schema {installed_schema} must be migrated to "
                f"{schema_version.KB_SCHEMA_VERSION} before read-only access"
            )
        conn._kb_vault_identity = vault_identity.validate_existing_identity(
            conn, path.parent
        )
        _load_vec(conn)
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
    _migrate_rejected_path(conn)


def _migrate_rejected_path(conn: sqlite3.Connection) -> None:
    """Add the typed rejected-option table (roadmap V2, id=3948).

    Additive side state, so it follows the ``_migrate_seed_import_ledgers``
    precedent and needs **no KB_SCHEMA_VERSION bump**: legacy databases need no
    row backfill, an older engine simply never reads the table, and reconnecting
    is a no-op. Bumping here would stamp the vault past the installed engine and
    trip ``SchemaTooNewError`` (id=2694) for a purely additive change.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS rejected_path (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            option          TEXT    NOT NULL,
            reason          TEXT    NOT NULL,
            ratifier        TEXT,
            decided_at      TEXT,
            scope_predicate TEXT,
            source          TEXT    NOT NULL DEFAULT 'declared'
                                    CHECK (source IN ('declared', 'backfill')),
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(node_id, option)
        );

        CREATE INDEX IF NOT EXISTS idx_rejected_path_node ON rejected_path(node_id);
        CREATE INDEX IF NOT EXISTS idx_rejected_path_source ON rejected_path(source);
        """
    )
    conn.commit()
    _migrate_ratification(conn)


def _migrate_ratification(conn: sqlite3.Connection) -> None:
    """Add V3's authority-bearing judgment ratification rows.

    This is additive side state on the same ratified precedent as
    ``rejected_path``: legacy canonical nodes remain valid as existing state,
    no synthetic rows are backfilled, and the compatibility boundary does not
    move for a table older binaries never read.  The first V3 build briefly
    created ``UNIQUE(node_id)``.  Rebuild that exact shape transactionally so
    each node instead keeps an append-only history of human outcomes.
    """
    def create_table(table_name: str) -> None:
        if table_name not in {"ratification", "ratification_append_history"}:
            raise ValueError("unexpected ratification migration table")
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id    INTEGER NOT NULL
                       REFERENCES nodes(id) ON DELETE CASCADE,
            ratifier   TEXT    NOT NULL CHECK (length(trim(ratifier)) > 0),
            decided_at TEXT    NOT NULL DEFAULT (datetime('now')),
            action     TEXT    NOT NULL CHECK (action IN ('ratify', 'reject')),
            scope      TEXT    NOT NULL DEFAULT 'node'
                               CHECK (scope IN ('node')),
            source     TEXT    NOT NULL
                               CHECK (source IN ('capture_decision', 'latch_update'))
            )
            """
        )

    def node_id_is_unique() -> bool:
        for index in conn.execute("PRAGMA index_list('ratification')").fetchall():
            if not int(index["unique"]):
                continue
            index_name = str(index["name"]).replace("'", "''")
            columns = [
                row["name"]
                for row in conn.execute(
                    f"PRAGMA index_info('{index_name}')"
                ).fetchall()
            ]
            if columns == ["node_id"]:
                return True
        return False

    def create_indexes() -> None:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ratification_node "
            "ON ratification(node_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ratification_action "
            "ON ratification(action)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ratification_source "
            "ON ratification(source)"
        )

    create_table("ratification")
    conn.commit()
    if node_id_is_unique():
        leftover = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'ratification_append_history'"
        ).fetchone()
        if leftover is not None:
            raise RuntimeError(
                "cannot migrate ratification: temporary table already exists"
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            create_table("ratification_append_history")
            conn.execute(
                """
                INSERT INTO ratification_append_history
                    (id, node_id, ratifier, decided_at, action, scope, source)
                SELECT id, node_id, ratifier, decided_at, action, scope, source
                FROM ratification
                ORDER BY id
                """
            )
            conn.execute("DROP TABLE ratification")
            conn.execute(
                "ALTER TABLE ratification_append_history RENAME TO ratification"
            )
            create_indexes()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    else:
        create_indexes()
        conn.commit()
    _migrate_lifecycle_substrate(conn)


def _migrate_lifecycle_substrate(conn: sqlite3.Connection) -> None:
    """Add append-only lifecycle telemetry, operation, and derivation ledgers.

    The migration is deliberately additive and idempotent. The current schema
    version is the minimum-writer gate: old binaries refuse the stamped
    database before reaching this chain, while current binaries can safely
    reconnect.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS retrieval_events (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id             TEXT,
            ts                     TEXT    NOT NULL DEFAULT (datetime('now')),
            turn                   INTEGER,
            node_id                INTEGER NOT NULL,
            source                 TEXT    NOT NULL,
            seed_node_id           INTEGER,
            reached_node_id        INTEGER,
            sim                    REAL,
            workstream_id_at_event INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_ts
            ON retrieval_events(ts);
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_node
            ON retrieval_events(node_id);
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_session_turn
            ON retrieval_events(session_id, turn);
        CREATE INDEX IF NOT EXISTS idx_retrieval_events_workstream
            ON retrieval_events(workstream_id_at_event, ts);

        CREATE TABLE IF NOT EXISTS workstream_ops (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            op_key            TEXT    NOT NULL UNIQUE,
            candidate_key     TEXT,
            op                TEXT    NOT NULL CHECK (op IN (
                                      'OPEN', 'MERGE', 'CLOSE', 'REOPEN', 'ADOPT', 'UNMERGE'
                                  )),
            state             TEXT    NOT NULL DEFAULT 'pending' CHECK (state IN (
                                      'pending', 'applied', 'rejected', 'failed',
                                      'orphaned_by_restore'
                                  )),
            origin            TEXT    NOT NULL,
            actor             TEXT,
            session_id        TEXT,
            src_workstream_id INTEGER,
            dst_workstream_id INTEGER,
            forced            INTEGER NOT NULL DEFAULT 0 CHECK (forced IN (0, 1)),
            preflight_token   TEXT,
            payload_json      TEXT    NOT NULL,
            payload_hash      TEXT    NOT NULL,
            error_code        TEXT CHECK (error_code IN (
                                      'preflight_stale', 'invalid_op', 'invalid_target',
                                      'invalid_payload', 'payload_insufficient', 'conflict',
                                      'blocked', 'awaiting_charter', 'rank_conflict',
                                      'open_feeders', 'quiescence', 'mutation_failed',
                                      'orphaned_by_restore', 'internal'
                                  )),
            created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            applied_at        TEXT,
            CHECK (
                (state = 'pending' AND error_code IS NULL AND applied_at IS NULL)
                OR (state = 'applied' AND error_code IS NULL AND applied_at IS NOT NULL)
                OR (state IN ('rejected', 'failed', 'orphaned_by_restore')
                    AND error_code IS NOT NULL AND applied_at IS NULL)
            )
        );
        CREATE INDEX IF NOT EXISTS idx_workstream_ops_candidate
            ON workstream_ops(candidate_key, created_at);
        CREATE INDEX IF NOT EXISTS idx_workstream_ops_state
            ON workstream_ops(state, created_at);
        CREATE INDEX IF NOT EXISTS idx_workstream_ops_src
            ON workstream_ops(src_workstream_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_workstream_ops_dst
            ON workstream_ops(dst_workstream_id, created_at);

        CREATE TABLE IF NOT EXISTS workstream_derivations (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            derivation_key    TEXT    NOT NULL UNIQUE,
            substrate_version TEXT    NOT NULL,
            window_start      TEXT,
            window_end        TEXT,
            snapshot_hash     TEXT    NOT NULL,
            created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_workstream_derivations_created
            ON workstream_derivations(created_at, id);

        CREATE TABLE IF NOT EXISTS workstream_derivation_candidates (
            derivation_id INTEGER NOT NULL REFERENCES workstream_derivations(id) ON DELETE CASCADE,
            candidate_key TEXT    NOT NULL,
            op            TEXT    NOT NULL CHECK (op IN ('OPEN', 'MERGE', 'CLOSE', 'ADOPT')),
            rank          INTEGER NOT NULL,
            signal_json   TEXT    NOT NULL,
            PRIMARY KEY (derivation_id, candidate_key)
        );
        CREATE INDEX IF NOT EXISTS idx_workstream_derivation_candidate_key
            ON workstream_derivation_candidates(candidate_key, derivation_id);

        CREATE TABLE IF NOT EXISTS workstream_op_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            event_key      TEXT    NOT NULL UNIQUE,
            candidate_key  TEXT    NOT NULL,
            op_key         TEXT,
            derivation_id  INTEGER REFERENCES workstream_derivations(id),
            event_type     TEXT    NOT NULL,
            verdict        TEXT CHECK (verdict IS NULL OR verdict IN (
                                   'agree', 'disagree', 'unsure'
                               )),
            session_id     TEXT,
            actor          TEXT,
            payload_json   TEXT    NOT NULL DEFAULT '{}',
            payload_hash   TEXT    NOT NULL,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_workstream_op_events_candidate
            ON workstream_op_events(candidate_key, created_at);
        CREATE INDEX IF NOT EXISTS idx_workstream_op_events_derivation
            ON workstream_op_events(derivation_id, candidate_key);
        """
    )
    conn.commit()


# ---------- workstream lifecycle ledgers ----------

def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise WorkstreamPayloadError("payload must be canonical JSON data") from exc


def _json_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _required_ledger_text(name: str, value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise WorkstreamLedgerError(f"{name} must be a non-empty string")
    return normalized


def validate_workstream_reversal_payload(
    op: str, payload: Mapping[str, Any],
) -> None:
    """Reject applied receipts that cannot support an exact reversal."""
    normalized_op = str(op).upper()
    required = WORKSTREAM_REVERSAL_PAYLOAD_KEYS.get(normalized_op)
    if required is None:
        raise WorkstreamPayloadError("unknown workstream operation")
    if not isinstance(payload, Mapping):
        raise WorkstreamPayloadError("operation payload must be an object")
    missing = sorted(required.difference(payload.keys()))
    if missing:
        raise WorkstreamPayloadError(
            "reversal payload missing: " + ", ".join(missing)
        )
    list_fields = {
        "assigned_member_ids",
        "repointed_member_ids",
        "rehomed_edge_ids",
        "tombstoned_edge_ids",
        "retired_priority_ids",
        "readded_priority_ids",
        "overflow_retired_priority_ids",
        "feeder_disposition_edge_ids",
        "priority_snapshots",
        "edge_rehomes",
        "priority_map",
        "created_priority_snapshots",
    }
    for name in required.intersection(list_fields):
        if not isinstance(payload[name], list):
            raise WorkstreamPayloadError(f"{name} must be a list")
    if normalized_op == "OPEN" and not isinstance(payload["probation"], Mapping):
        raise WorkstreamPayloadError("probation must be an object")
    if normalized_op in {"CLOSE", "MERGE"}:
        retired_ids = payload.get("retired_priority_ids")
        snapshots = payload.get("priority_snapshots")
        if not isinstance(retired_ids, list) or not isinstance(snapshots, list):
            raise WorkstreamPayloadError("priority reversal fields must be lists")
        try:
            retired = {int(value) for value in retired_ids}
        except (TypeError, ValueError) as exc:
            raise WorkstreamPayloadError(
                "retired_priority_ids must contain integer ids"
            ) from exc
        required_snapshot = {
            "id", "title", "body", "status", "workstream_id", "updated_at",
            "updated_by", "rank", "retired_at",
        }
        snapshot_ids: set[int] = set()
        for item in snapshots:
            if not isinstance(item, Mapping) or not required_snapshot.issubset(item):
                raise WorkstreamPayloadError(
                    "priority_snapshots must contain complete reversal rows"
                )
            try:
                snapshot_ids.add(int(item["id"]))
            except (TypeError, ValueError) as exc:
                raise WorkstreamPayloadError(
                    "priority snapshot id must be an integer"
                ) from exc
        if (
            len(snapshot_ids) != len(snapshots)
            or len(retired) != len(retired_ids)
            or snapshot_ids != retired
        ):
            raise WorkstreamPayloadError(
                "priority_snapshots must exactly cover retired_priority_ids"
            )
    if normalized_op == "MERGE":
        if not isinstance(payload["prior_memberships"], Mapping):
            raise WorkstreamPayloadError("prior_memberships must be an object")
        try:
            member_ids = {int(value) for value in payload["repointed_member_ids"]}
            membership_ids = {int(value) for value in payload["prior_memberships"]}
        except (TypeError, ValueError) as exc:
            raise WorkstreamPayloadError("MERGE membership ids must be integers") from exc
        if member_ids != membership_ids:
            raise WorkstreamPayloadError(
                "prior_memberships must exactly cover repointed_member_ids"
            )
        copy_ids = {int(value) for value in payload["readded_priority_ids"]}
        created = payload["created_priority_snapshots"]
        required_copy_snapshot = {
            "id", "kind", "title", "body", "status", "session_id",
            "created_at", "updated_at", "ref_count", "last_referenced_at",
            "retention_tier", "parent_id", "depth", "created_by", "updated_by",
            "workstream_id", "content_hash", "embedding_is_null",
            "priority_rank", "priority_retired_at", "priority_order_present",
        }
        if any(
            not isinstance(item, Mapping)
            or not required_copy_snapshot.issubset(item)
            for item in created
        ):
            raise WorkstreamPayloadError(
                "created_priority_snapshots must contain complete rows"
            )
        priority_map = payload["priority_map"]
        if any(
            not isinstance(item, Mapping)
            or not {"copy_priority_id", "source_priority_id"}.issubset(item)
            for item in priority_map
        ):
            raise WorkstreamPayloadError("priority_map must contain complete rows")
        try:
            created_ids = {int(item["id"]) for item in created}
            mapped_copy_ids = {
                int(item["copy_priority_id"])
                for item in priority_map
            }
        except (TypeError, ValueError) as exc:
            raise WorkstreamPayloadError("MERGE priority map ids must be integers") from exc
        if (
            len(copy_ids) != len(payload["readded_priority_ids"])
            or len(created_ids) != len(created)
            or len(mapped_copy_ids) != len(priority_map)
            or created_ids != copy_ids
            or mapped_copy_ids != copy_ids
        ):
            raise WorkstreamPayloadError(
                "MERGE priority copy metadata must exactly cover readded ids"
            )
        if any(
            not isinstance(item, Mapping)
            or not {"old_edge", "action", "new_edge_id", "target_before"}.issubset(item)
            or not isinstance(item.get("old_edge"), Mapping)
            or not {"id", "src", "dst", "relation"}.issubset(item["old_edge"])
            for item in payload["edge_rehomes"]
        ):
            raise WorkstreamPayloadError("edge_rehomes must contain complete reversal rows")


def get_workstream_op(
    conn: sqlite3.Connection, op_key: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM workstream_ops WHERE op_key = ?", (op_key,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["payload"] = json.loads(result["payload_json"])
    except (TypeError, json.JSONDecodeError):
        result["payload"] = None
    return result


def begin_workstream_op_nc(
    conn: sqlite3.Connection,
    *,
    op_key: str,
    op: str,
    origin: str,
    payload: Mapping[str, Any],
    candidate_key: str | None = None,
    session_id: str | None = None,
    src_workstream_id: int | None = None,
    dst_workstream_id: int | None = None,
    forced: bool = False,
    preflight_token: str | None = None,
    actor: str | None = None,
) -> dict:
    """Create/fetch a pending operation receipt without committing.

    The operation key is immutable across every state, including failure. An
    exact retry is idempotent; any metadata drift is a hard collision.
    """
    key = _required_ledger_text("op_key", op_key)
    normalized_op = str(op).upper()
    if normalized_op not in WORKSTREAM_OPS:
        raise WorkstreamLedgerError("unknown workstream operation")
    normalized_origin = _required_ledger_text("origin", origin)
    if not isinstance(payload, Mapping):
        raise WorkstreamPayloadError("operation payload must be an object")
    payload_json = _canonical_json(dict(payload))
    payload_hash = _json_hash(payload_json)
    immutable = {
        "candidate_key": candidate_key,
        "op": normalized_op,
        "origin": normalized_origin,
        "actor": actor or _ACTOR,
        "session_id": session_id,
        "src_workstream_id": src_workstream_id,
        "dst_workstream_id": dst_workstream_id,
        "forced": int(bool(forced)),
        "preflight_token": preflight_token,
        "payload_hash": payload_hash,
    }
    now = _now()
    cur = conn.execute(
        "INSERT INTO workstream_ops "
        "(op_key, candidate_key, op, state, origin, actor, session_id, "
        " src_workstream_id, dst_workstream_id, forced, preflight_token, "
        " payload_json, payload_hash, created_at, updated_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(op_key) DO NOTHING",
        (
            key,
            candidate_key,
            normalized_op,
            normalized_origin,
            immutable["actor"],
            session_id,
            src_workstream_id,
            dst_workstream_id,
            immutable["forced"],
            preflight_token,
            payload_json,
            payload_hash,
            now,
            now,
        ),
    )
    row = get_workstream_op(conn, key)
    if row is None:  # pragma: no cover - SQLite acknowledged insert/read
        raise WorkstreamLedgerError("workstream operation was not readable")
    for name, expected in immutable.items():
        if row[name] != expected:
            raise WorkstreamLedgerConflictError(
                f"workstream operation {name} metadata mismatch"
            )
    row["created"] = bool(cur.rowcount)
    return row


def begin_workstream_op(conn: sqlite3.Connection, **kwargs: Any) -> dict:
    try:
        row = begin_workstream_op_nc(conn, **kwargs)
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def finish_workstream_op_nc(
    conn: sqlite3.Connection,
    op_key: str,
    *,
    state: str,
    error_code: str | None = None,
) -> dict:
    """CAS a pending operation to one immutable terminal state."""
    if state not in WORKSTREAM_OP_STATES or state == "pending":
        raise WorkstreamLedgerStateError("operation must finish in a terminal state")
    if state == "applied":
        if error_code is not None:
            raise WorkstreamLedgerStateError("applied operation cannot carry error_code")
    else:
        if error_code not in WORKSTREAM_OP_ERROR_CODES:
            raise WorkstreamLedgerStateError(
                "non-applied operation needs a closed error_code"
            )
        if state == "orphaned_by_restore" and error_code != "orphaned_by_restore":
            raise WorkstreamLedgerStateError(
                "orphaned_by_restore state requires matching error_code"
            )
    row = get_workstream_op(conn, op_key)
    if row is None:
        raise KeyError("unknown workstream operation key")
    if row["state"] == state and row["error_code"] == error_code:
        return row
    if row["state"] != "pending":
        raise WorkstreamLedgerStateError(
            f"cannot transition workstream operation {row['state']} to {state}"
        )
    if state == "applied":
        validate_workstream_reversal_payload(row["op"], row["payload"])
    now = _now()
    applied_at = now if state == "applied" else None
    cur = conn.execute(
        "UPDATE workstream_ops SET state = ?, error_code = ?, updated_at = ?, "
        "applied_at = ? WHERE op_key = ? AND state = 'pending'",
        (state, error_code, now, applied_at, op_key),
    )
    if cur.rowcount != 1:
        current = get_workstream_op(conn, op_key)
        if current is not None and current["state"] == state \
                and current["error_code"] == error_code:
            return current
        raise WorkstreamLedgerStateError("operation CAS lost to another terminal state")
    result = get_workstream_op(conn, op_key)
    assert result is not None
    return result


def finish_workstream_op(
    conn: sqlite3.Connection,
    op_key: str,
    *,
    state: str,
    error_code: str | None = None,
) -> dict:
    try:
        row = finish_workstream_op_nc(
            conn, op_key, state=state, error_code=error_code,
        )
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def latest_workstream_derivation(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM workstream_derivations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row is not None else None


def latest_workstream_derivation_candidates(
    conn: sqlite3.Connection,
) -> list[dict]:
    latest = latest_workstream_derivation(conn)
    if latest is None:
        return []
    rows = conn.execute(
        "SELECT * FROM workstream_derivation_candidates "
        "WHERE derivation_id = ? ORDER BY rank, candidate_key",
        (latest["id"],),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        item["signal"] = json.loads(item["signal_json"])
        item["derivation_key"] = latest["derivation_key"]
        item["substrate_version"] = latest["substrate_version"]
        out.append(item)
    return out


def record_workstream_derivation_nc(
    conn: sqlite3.Connection,
    *,
    derivation_key: str,
    substrate_version: str,
    candidates: Sequence[Mapping[str, Any]],
    window_start: str | None = None,
    window_end: str | None = None,
) -> dict:
    key = _required_ledger_text("derivation_key", derivation_key)
    substrate = _required_ledger_text("substrate_version", substrate_version)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, candidate in enumerate(candidates, start=1):
        candidate_key = _required_ledger_text(
            "candidate_key", str(candidate.get("candidate_key", "")),
        )
        if candidate_key in seen:
            raise WorkstreamLedgerConflictError("duplicate derivation candidate key")
        seen.add(candidate_key)
        op = str(candidate.get("op", "")).upper()
        if op not in {"OPEN", "MERGE", "CLOSE", "ADOPT"}:
            raise WorkstreamLedgerError("invalid derivation candidate operation")
        rank = int(candidate.get("rank", index))
        signal = candidate.get("signal")
        if signal is None:
            signal = {
                name: value for name, value in candidate.items()
                if name not in {"candidate_key", "op", "rank"}
            }
        signal_json = _canonical_json(signal)
        normalized.append({
            "candidate_key": candidate_key,
            "op": op,
            "rank": rank,
            "signal_json": signal_json,
        })
    normalized.sort(key=lambda row: (row["rank"], row["candidate_key"]))
    snapshot_json = _canonical_json(normalized)
    snapshot_hash = _json_hash(snapshot_json)
    cur = conn.execute(
        "INSERT INTO workstream_derivations "
        "(derivation_key, substrate_version, window_start, window_end, snapshot_hash) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(derivation_key) DO NOTHING",
        (key, substrate, window_start, window_end, snapshot_hash),
    )
    row = conn.execute(
        "SELECT * FROM workstream_derivations WHERE derivation_key = ?", (key,),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise WorkstreamLedgerError("workstream derivation was not readable")
    result = dict(row)
    for name, expected in {
        "substrate_version": substrate,
        "window_start": window_start,
        "window_end": window_end,
        "snapshot_hash": snapshot_hash,
    }.items():
        if result[name] != expected:
            raise WorkstreamLedgerConflictError(
                f"workstream derivation {name} metadata mismatch"
            )
    if cur.rowcount:
        conn.executemany(
            "INSERT INTO workstream_derivation_candidates "
            "(derivation_id, candidate_key, op, rank, signal_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    result["id"],
                    item["candidate_key"],
                    item["op"],
                    item["rank"],
                    item["signal_json"],
                )
                for item in normalized
            ],
        )
    result["created"] = bool(cur.rowcount)
    return result


def record_workstream_derivation(conn: sqlite3.Connection, **kwargs: Any) -> dict:
    try:
        row = record_workstream_derivation_nc(conn, **kwargs)
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def append_workstream_op_event_nc(
    conn: sqlite3.Connection,
    *,
    event_key: str,
    candidate_key: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    op_key: str | None = None,
    derivation_key: str | None = None,
    verdict: str | None = None,
    session_id: str | None = None,
    actor: str | None = None,
    require_latest_candidate: bool = False,
) -> dict:
    key = _required_ledger_text("event_key", event_key)
    candidate = _required_ledger_text("candidate_key", candidate_key)
    kind = _required_ledger_text("event_type", event_type)
    if verdict is not None and verdict not in WORKSTREAM_EVENT_VERDICTS:
        raise WorkstreamLedgerError("invalid candidate verdict")
    derivation: dict | None = None
    if derivation_key is not None:
        row = conn.execute(
            "SELECT * FROM workstream_derivations WHERE derivation_key = ?",
            (derivation_key,),
        ).fetchone()
        derivation = dict(row) if row is not None else None
        if derivation is None:
            raise KeyError("unknown workstream derivation key")
    if require_latest_candidate:
        latest = latest_workstream_derivation(conn)
        if latest is None:
            raise WorkstreamLedgerStateError("no current derivation exists")
        if derivation is not None and derivation["id"] != latest["id"]:
            raise WorkstreamLedgerStateError("candidate is not from latest derivation")
        derivation = latest
        exists = conn.execute(
            "SELECT 1 FROM workstream_derivation_candidates "
            "WHERE derivation_id = ? AND candidate_key = ?",
            (derivation["id"], candidate),
        ).fetchone()
        if exists is None:
            raise WorkstreamLedgerStateError("candidate is not in latest derivation")
    payload_json = _canonical_json(dict(payload or {}))
    payload_hash = _json_hash(payload_json)
    immutable = {
        "candidate_key": candidate,
        "op_key": op_key,
        "derivation_id": derivation["id"] if derivation is not None else None,
        "event_type": kind,
        "verdict": verdict,
        "session_id": session_id,
        "actor": actor or _ACTOR,
        "payload_hash": payload_hash,
    }
    cur = conn.execute(
        "INSERT INTO workstream_op_events "
        "(event_key, candidate_key, op_key, derivation_id, event_type, verdict, "
        " session_id, actor, payload_json, payload_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(event_key) DO NOTHING",
        (
            key,
            candidate,
            op_key,
            immutable["derivation_id"],
            kind,
            verdict,
            session_id,
            immutable["actor"],
            payload_json,
            payload_hash,
        ),
    )
    row = conn.execute(
        "SELECT * FROM workstream_op_events WHERE event_key = ?", (key,),
    ).fetchone()
    if row is None:  # pragma: no cover
        raise WorkstreamLedgerError("workstream event was not readable")
    result = dict(row)
    for name, expected in immutable.items():
        if result[name] != expected:
            raise WorkstreamLedgerConflictError(
                f"workstream event {name} metadata mismatch"
            )
    result["payload"] = json.loads(result["payload_json"])
    result["created"] = bool(cur.rowcount)
    return result


def append_workstream_op_event(conn: sqlite3.Connection, **kwargs: Any) -> dict:
    try:
        row = append_workstream_op_event_nc(conn, **kwargs)
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


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
    """Atomically finalize every source revision in one approved apply batch.

    Every non-idempotent transition is a compare-and-set from ``pending``.
    If any row loses that precondition after validation, the entire batch is
    rolled back so callers can safely retry it.
    """
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
            cur = conn.execute(
                "UPDATE seed_source_import SET state = ?, error_code = ?, "
                "updated_at = ?, completed_at = ? "
                "WHERE import_key = ? AND state = 'pending'",
                (state, error_code, now, now, import_key),
            )
            if cur.rowcount != 1:
                raise SeedImportStateError(
                    "source import must remain pending through batch finalization"
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


def _has_ratifying_row(conn: sqlite3.Connection, node_id: int) -> bool:
    row = conn.execute(
        "SELECT action FROM ratification WHERE node_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (int(node_id),),
    ).fetchone()
    return row is not None and row["action"] == "ratify"


def insert_node_nc(
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
    """Insert a node without committing the surrounding transaction."""
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
    return int(nid)


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
    nid = insert_node_nc(
        conn,
        kind=kind,
        title=title,
        body=body,
        status=status,
        session_id=session_id,
        embedding=embedding,
        workstream_id=workstream_id,
    )
    conn.commit()
    return nid


def update_node_nc(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    embedding: bytes | None = None,
) -> None:
    """Update a node without committing the surrounding transaction."""
    if status == "canonical":
        current = conn.execute(
            "SELECT kind, status FROM nodes WHERE id = ?", (int(node_id),)
        ).fetchone()
        if (
            current is not None
            and current["kind"] in JUDGMENT_KINDS
            and current["status"] != "canonical"
            and not _has_ratifying_row(conn, node_id)
        ):
            raise RatificationRequiredError(
                f"{current['kind']} node {int(node_id)} requires ratification "
                "before canonical promotion"
            )
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


def update_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    embedding: bytes | None = None,
) -> None:
    update_node_nc(
        conn,
        node_id,
        title=title,
        body=body,
        status=status,
        embedding=embedding,
    )
    conn.commit()


def set_node_workstream_nc(
    conn: sqlite3.Connection,
    node_ids: Iterable[int],
    workstream_id: int | None,
) -> int:
    """Assign node membership without committing; empty input is a no-op."""
    ids = sorted({int(node_id) for node_id in node_ids})
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE nodes SET workstream_id = ?, updated_at = ?, updated_by = ? "
        f"WHERE id IN ({placeholders})",
        [workstream_id, _now(), _ACTOR, *ids],
    )
    return int(cur.rowcount or 0)


def set_node_workstream(
    conn: sqlite3.Connection,
    node_ids: Iterable[int],
    workstream_id: int | None,
) -> int:
    count = set_node_workstream_nc(conn, node_ids, workstream_id)
    conn.commit()
    return count


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


def insert_ratification_nc(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    ratifier: str,
    action: str,
    source: str,
    scope: str = "node",
    decided_at: str | None = None,
) -> int:
    """Record one closed, authority-bearing outcome without committing.

    The caller owns the transaction that binds this row to a node transition.
    In particular, the two authorized public surfaces insert this row and then
    call :func:`update_node_nc` in the same transaction.  No prose is accepted.
    """
    if not isinstance(ratifier, str) or not ratifier.strip():
        raise ValueError("ratification.ratifier must be non-empty")
    if action not in RATIFICATION_ACTIONS:
        raise ValueError(
            f"ratification.action must be one of {sorted(RATIFICATION_ACTIONS)}"
        )
    if scope not in RATIFICATION_SCOPES:
        raise ValueError(
            f"ratification.scope must be one of {sorted(RATIFICATION_SCOPES)}"
        )
    if source not in RATIFICATION_SOURCES:
        raise ValueError(
            f"ratification.source must be one of {sorted(RATIFICATION_SOURCES)}"
        )
    if decided_at is None:
        decided_at = _now()
    elif not isinstance(decided_at, str) or not decided_at.strip():
        raise ValueError("ratification.decided_at must be non-empty")
    cur = conn.execute(
        """
        INSERT INTO ratification
            (node_id, ratifier, decided_at, action, scope, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            int(node_id),
            ratifier.strip(),
            decided_at.strip(),
            action,
            scope,
            source,
        ),
    )
    return int(cur.lastrowid)


def ratification_for_node(
    conn: sqlite3.Connection, node_id: int,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM ratification WHERE node_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (int(node_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def list_ratifications(
    conn: sqlite3.Connection,
    *,
    action: str | None = None,
    source: str | None = None,
) -> list[dict]:
    if action is not None and action not in RATIFICATION_ACTIONS:
        raise ValueError(
            f"ratification.action must be one of {sorted(RATIFICATION_ACTIONS)}"
        )
    if source is not None and source not in RATIFICATION_SOURCES:
        raise ValueError(
            f"ratification.source must be one of {sorted(RATIFICATION_SOURCES)}"
        )
    where: list[str] = []
    params: list[str] = []
    if action is not None:
        where.append("action = ?")
        params.append(action)
    if source is not None:
        where.append("source = ?")
        params.append(source)
    query = "SELECT * FROM ratification"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY id"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def count_ratifications(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM ratification").fetchone()[0])


def insert_rejected_path_nc(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    option: str,
    reason: str,
    ratifier: str | None = None,
    decided_at: str | None = None,
    scope_predicate: str | None = None,
    source: str = "declared",
) -> int | None:
    """Record one rejected option against the node that documents it (id=3948 V2).

    `option` and `reason` are both required and must be non-blank: an
    unexplained rejection cannot support a revival check, and a row with an
    empty reason would read as authoritative while carrying nothing a future
    agent could act on. Returns the new row id, or None when the
    UNIQUE(node_id, option) pair already exists — re-recording is a no-op, not
    an error, so backfill is safely re-runnable.
    """
    if not option or not option.strip():
        raise ValueError("rejected_path.option must be non-empty")
    if not reason or not reason.strip():
        raise ValueError("rejected_path.reason must be non-empty")
    if source not in ("declared", "backfill"):
        raise ValueError(f"rejected_path.source must be declared|backfill, got {source!r}")
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO rejected_path
            (node_id, option, reason, ratifier, decided_at, scope_predicate, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            option.strip(),
            reason.strip(),
            ratifier,
            decided_at,
            scope_predicate,
            source,
        ),
    )
    return cur.lastrowid if cur.rowcount else None


def insert_rejected_path(conn: sqlite3.Connection, node_id: int, **kwargs) -> int | None:
    row_id = insert_rejected_path_nc(conn, node_id, **kwargs)
    conn.commit()
    return row_id


def rejected_paths_for_node(conn: sqlite3.Connection, node_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM rejected_path WHERE node_id = ? ORDER BY id", (node_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def list_rejected_paths(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
    include_stale_nodes: bool = True,
) -> list[dict]:
    """All declared rejections, newest node first.

    `include_stale_nodes` defaults True: a rejection recorded on a node that was
    later superseded was still a genuine rejection when it was made, and the
    V2 count (docs/v2_rejection_rubric.md) is taken over all statuses.
    """
    where, params = ["1=1"], []
    if source is not None:
        where.append("r.source = ?")
        params.append(source)
    if not include_stale_nodes:
        where.append("n.status != 'stale'")
    rows = conn.execute(
        f"""
        SELECT r.*, n.title AS node_title, n.kind AS node_kind, n.status AS node_status
        FROM rejected_path r
        JOIN nodes n ON n.id = r.node_id
        WHERE {' AND '.join(where)}
        ORDER BY r.node_id DESC, r.id
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def count_rejected_paths(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COUNT(*) FROM rejected_path").fetchone()[0]
    )


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

    Only the ratified evidence lane (fact/progress) is eligible. Judgment kinds
    and every unclassified/machine kind remain staging; citation volume is
    relevance evidence, not user authority. Imported seed nodes also remain
    staging until explicit review. Status change IS an edit, so updated_at
    bumps here (unlike bump_ref_count). Stale nodes are untouched.
    """
    evidence_kinds = tuple(sorted(EVIDENCE_PROMOTION_KINDS))
    rows = conn.execute(
        "SELECT n.id FROM nodes n "
        "WHERE n.status = 'staging' AND n.ref_count >= ? "
        "AND n.kind IN (?, ?) "
        "AND instr(COALESCE(n.body, ''), 'Latch-Seed-Import-Key:') = 0 "
        "AND NOT EXISTS (SELECT 1 FROM seed_import si "
        "WHERE si.node_id = n.id)",
        (min_ref_count, *evidence_kinds),
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


def add_edge_nc(
    conn: sqlite3.Connection,
    src: int,
    dst: int,
    relation: str,
    *,
    created_by: str | None = None,
) -> int:
    """Insert/reactivate an edge without committing or emitting telemetry."""
    canonical = canonicalize_relation(relation)
    conn.execute(
        "INSERT INTO edges (src, dst, relation, status, created_at, created_by) "
        "VALUES (?, ?, ?, 'active', ?, ?) "
        "ON CONFLICT(src, dst,relation) DO UPDATE SET status = 'active' "
        "WHERE edges.status = 'tombstoned'",
        (src, dst, canonical, _now(), created_by or _ACTOR),
    )
    row = conn.execute(
        "SELECT id FROM edges WHERE src = ? AND dst = ? AND relation = ?",
        (src, dst, canonical),
    ).fetchone()
    if row is None:  # pragma: no cover - insert/read invariant
        raise RuntimeError("edge was not readable after insert")
    return int(row["id"])


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

    add_edge_nc(conn, src, dst, canonical)
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


def tombstone_edge_nc(
    conn: sqlite3.Connection, src: int, dst: int, relation: str,
) -> int:
    """Tombstone an active edge without committing."""
    cur = conn.execute(
        "UPDATE edges SET status = 'tombstoned' "
        "WHERE src = ? AND dst = ? AND relation = ? AND status = 'active'",
        (src, dst, canonicalize_relation(relation)),
    )
    return int(cur.rowcount or 0)


def tombstone_edge_id_nc(conn: sqlite3.Connection, edge_id: int) -> int:
    """Tombstone one active edge by durable ledger id, without committing."""
    cur = conn.execute(
        "UPDATE edges SET status = 'tombstoned' WHERE id = ? AND status = 'active'",
        (edge_id,),
    )
    return int(cur.rowcount or 0)


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
    count = tombstone_edge_nc(conn, src, dst, relation)
    conn.commit()
    return count


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
    event_details: Mapping[int, Mapping[str, Any]] | None = None,
) -> int:
    """Upsert (session_id, node_id) rows. New rows record sim_at_first; repeat
    hits bump hit_count + last_injected_at/turn. Every hit is also appended to
    ``retrieval_events`` with event-time lane attribution.

    Retrieval telemetry is deliberately best effort: if only the append fails,
    the active-set writes still commit and a persistent dropped-event counter is
    incremented. Primary write failures continue to propagate.
    """
    items = list(items)
    if not items:
        return 0
    now = _now()
    n = _record_session_retrievals_nc(
        conn,
        session_id=session_id,
        turn=turn,
        items=items,
        source=source,
        now=now,
    )
    conn.execute("SAVEPOINT retrieval_event_append")
    try:
        _insert_retrieval_events_nc(
            conn,
            session_id=session_id,
            turn=turn,
            items=items,
            source=source,
            event_details=event_details,
            ts=now,
        )
    except sqlite3.Error:
        conn.execute("ROLLBACK TO SAVEPOINT retrieval_event_append")
        conn.execute("RELEASE SAVEPOINT retrieval_event_append")
        _increment_retrieval_dropped_nc(conn, len(items))
    else:
        conn.execute("RELEASE SAVEPOINT retrieval_event_append")
    conn.commit()
    return n


def _record_session_retrievals_nc(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn: int,
    items: Sequence[tuple[int, float | None]],
    source: str,
    now: str,
) -> int:
    """Update the active-set table without committing."""
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
    return n


def _event_workstream_id(conn: sqlite3.Connection, node_id: int) -> int | None:
    row = conn.execute(
        "SELECT id, kind, workstream_id FROM nodes WHERE id = ?", (node_id,),
    ).fetchone()
    if row is None:
        return None
    if row["kind"] == "workstream":
        return int(row["id"])
    return int(row["workstream_id"]) if row["workstream_id"] is not None else None


def _insert_retrieval_events_nc(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    turn: int | None,
    items: Sequence[tuple[int, float | None]],
    source: str,
    event_details: Mapping[int, Mapping[str, Any]] | None = None,
    ts: str | None = None,
) -> int:
    """Append retrieval event rows without committing."""
    event_ts = ts or _now()
    count = 0
    for node_id, sim in items:
        detail = (event_details or {}).get(int(node_id), {})
        workstream_id = detail.get("workstream_id_at_event")
        if workstream_id is None:
            workstream_id = _event_workstream_id(conn, int(node_id))
        conn.execute(
            "INSERT INTO retrieval_events "
            "(session_id, ts, turn, node_id, source, seed_node_id, "
            " reached_node_id, sim, workstream_id_at_event) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                event_ts,
                turn,
                int(node_id),
                source,
                detail.get("seed_node_id"),
                detail.get("reached_node_id"),
                sim,
                workstream_id,
            ),
        )
        count += 1
    return count


def _increment_retrieval_dropped_nc(
    conn: sqlite3.Connection, amount: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO latch_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = "
        "CAST(latch_meta.value AS INTEGER) + excluded.value",
        (RETRIEVAL_DROPPED_META_KEY, str(int(amount))),
    )


def retrieval_events_dropped(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM latch_meta WHERE key = ?",
        (RETRIEVAL_DROPPED_META_KEY,),
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def record_retrieval_events(
    conn: sqlite3.Connection,
    *,
    source: str,
    items: Iterable[tuple[int, float | None]],
    session_id: str | None = None,
    turn: int | None = None,
    event_details: Mapping[int, Mapping[str, Any]] | None = None,
    ts: str | None = None,
) -> int:
    """Append non-active-set contacts such as tool, write, and gate events.

    This path is best effort by definition and returns zero after recording a
    persistent drop when SQLite rejects the event append.
    """
    normalized = [(int(node_id), sim) for node_id, sim in items]
    if not normalized:
        return 0
    resolved_turn = turn
    if session_id is not None and resolved_turn is None:
        row = conn.execute(
            "SELECT turn_count FROM sessions WHERE id = ?", (session_id,),
        ).fetchone()
        resolved_turn = int(row["turn_count"]) if row is not None else None
    conn.execute("SAVEPOINT retrieval_event_only_append")
    try:
        count = _insert_retrieval_events_nc(
            conn,
            session_id=session_id,
            turn=resolved_turn,
            items=normalized,
            source=source,
            event_details=event_details,
            ts=ts,
        )
    except sqlite3.Error:
        conn.execute("ROLLBACK TO SAVEPOINT retrieval_event_only_append")
        conn.execute("RELEASE SAVEPOINT retrieval_event_only_append")
        _increment_retrieval_dropped_nc(conn, len(normalized))
        conn.commit()
        return 0
    conn.execute("RELEASE SAVEPOINT retrieval_event_only_append")
    conn.commit()
    return count


def prune_retrieval_events(
    conn: sqlite3.Connection,
    *,
    retention_days: int = 90,
    now: datetime | str | None = None,
) -> int:
    """Delete append-only retrieval events older than the retention window."""
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    if isinstance(now, str):
        anchor = _parse_ts(now)
        if anchor is None:
            raise ValueError("now must be an ISO-like UTC timestamp")
    elif isinstance(now, datetime):
        anchor = now
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        else:
            anchor = anchor.astimezone(timezone.utc)
    else:
        anchor = datetime.now(timezone.utc)
    cutoff = (anchor - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute("DELETE FROM retrieval_events WHERE ts < ?", (cutoff,))
    conn.commit()
    return int(cur.rowcount or 0)


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

# Cap on auto-bumped active rows. Pinned rows persist beyond the cap.
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


def get_focus_row(
    conn: sqlite3.Connection, workstream_id: int,
) -> dict | None:
    row = conn.execute(
        "SELECT workstream_id, rank, score, set_at, set_by, pinned "
        "FROM focus WHERE workstream_id = ?",
        (workstream_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def set_focus_row_nc(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    score: float,
    set_at: str,
    set_by: str,
    pinned: bool | int,
    rank: int = 0,
) -> None:
    """Write a complete focus row without committing (used by rollback)."""
    conn.execute(
        "INSERT INTO focus(workstream_id, rank, score, set_at, set_by, pinned) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(workstream_id) DO UPDATE SET "
        "rank=excluded.rank, score=excluded.score, set_at=excluded.set_at, "
        "set_by=excluded.set_by, pinned=excluded.pinned",
        (workstream_id, int(rank), float(score), set_at, set_by, int(bool(pinned))),
    )


def restore_focus_row_nc(
    conn: sqlite3.Connection, row: Mapping[str, Any],
) -> None:
    """Restore a row captured by :func:`get_focus_row`, without committing."""
    set_focus_row_nc(
        conn,
        int(row["workstream_id"]),
        rank=int(row.get("rank", 0)),
        score=float(row["score"]),
        set_at=str(row["set_at"]),
        set_by=str(row["set_by"]),
        pinned=int(row["pinned"]),
    )


def delete_focus_row_nc(conn: sqlite3.Connection, workstream_id: int) -> int:
    cur = conn.execute("DELETE FROM focus WHERE workstream_id = ?", (workstream_id,))
    return int(cur.rowcount or 0)


def set_focus_score_nc(
    conn: sqlite3.Connection,
    workstream_id: int,
    score: float,
    *,
    set_at: str | None = None,
    set_by: str | None = None,
) -> int:
    fields = ["score = ?"]
    values: list[Any] = [float(score)]
    if set_at is not None:
        fields.append("set_at = ?")
        values.append(set_at)
    if set_by is not None:
        fields.append("set_by = ?")
        values.append(set_by)
    values.append(workstream_id)
    cur = conn.execute(
        f"UPDATE focus SET {', '.join(fields)} WHERE workstream_id = ?", values,
    )
    return int(cur.rowcount or 0)


def set_focus_pinned_nc(
    conn: sqlite3.Connection, workstream_id: int, pinned: bool | int,
) -> int:
    cur = conn.execute(
        "UPDATE focus SET pinned = ? WHERE workstream_id = ?",
        (int(bool(pinned)), workstream_id),
    )
    return int(cur.rowcount or 0)


def bump_focus_nc(
    conn: sqlite3.Connection,
    workstream_id: int | None,
    *,
    delta: float = FOCUS_DEFAULT_DELTA,
    set_by: str = "auto",
) -> bool:
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
        return False
    # Defensive: the focus row's PK is FK'd to nodes(id) ON DELETE CASCADE.
    # If the node id doesn't exist or isn't kind='workstream', skip — we'd
    # otherwise create a row that fails FK at commit (or worse, attaches focus
    # to a leaf node).
    row = conn.execute(
        "SELECT kind, status FROM nodes WHERE id = ?", (workstream_id,)
    ).fetchone()
    if row is None or row["kind"] != "workstream":
        return False
    if row["status"] == "stale":
        # Redirect merged identities, but never revive focus for a permanently
        # closed lane.  Local import keeps db.py independent at module load.
        try:
            import workstreams
            active_id = workstreams.resolve_active(
                conn, int(workstream_id),
            ).get("active_id")
        except (ImportError, AttributeError, TypeError, ValueError):
            active_id = None
        if active_id is None:
            return False
        workstream_id = int(active_id)

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
    recompute_focus_ranks_nc(conn)
    return True


def bump_focus(
    conn: sqlite3.Connection,
    workstream_id: int | None,
    *,
    delta: float = FOCUS_DEFAULT_DELTA,
    set_by: str = "auto",
) -> None:
    bump_focus_nc(conn, workstream_id, delta=delta, set_by=set_by)
    conn.commit()


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
    if not bump_focus_nc(conn, workstream_id, delta=0.0, set_by="user"):
        return False
    set_focus_pinned_nc(conn, workstream_id, True)
    recompute_focus_ranks_nc(conn)
    conn.commit()
    return True


def unpin_focus(conn: sqlite3.Connection, workstream_id: int) -> None:
    set_focus_pinned_nc(conn, workstream_id, False)
    recompute_focus_ranks_nc(conn)
    conn.commit()


def drop_focus(conn: sqlite3.Connection, workstream_id: int) -> None:
    """Hard remove from focus table (loses score history). Use sparingly —
    decay alone usually suffices for stale workstreams."""
    delete_focus_row_nc(conn, workstream_id)
    recompute_focus_ranks_nc(conn)
    conn.commit()


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
    recompute_focus_ranks_nc(conn)
    conn.commit()
    return len(to_evict)


def recompute_focus_ranks_nc(conn: sqlite3.Connection) -> None:
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


def _recompute_focus_ranks(conn: sqlite3.Connection) -> None:
    """Backward-compatible committing wrapper for older internal callers."""
    recompute_focus_ranks_nc(conn)
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
        WHERE n.kind = 'workstream' AND n.status != 'stale'
        """
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["effective_score"] = _decay_score(r["score"], r["set_at"])
        out.append(d)
    out.sort(key=lambda d: (-int(d["pinned"]), -d["effective_score"]))
    return out[:limit] if limit and limit > 0 else out
