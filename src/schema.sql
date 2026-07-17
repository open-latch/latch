-- claude_kb: per-project knowledge base
-- Loose node/edge model + sessions table for compact orchestration.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS nodes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    body                TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'staging',
    session_id          TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    embedding           BLOB,
    -- v2 columns. last_referenced_at MUST stay independent of updated_at so
    -- recency and reference-frequency are orthogonal signals in the healer.
    ref_count           INTEGER NOT NULL DEFAULT 0,
    last_referenced_at  TEXT,
    retention_tier      TEXT    NOT NULL DEFAULT 'deep',  -- deep|lite|ephemeral; activated in a later step
    parent_id           INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    depth               INTEGER NOT NULL DEFAULT 0,
    -- Attribution (metadata only — never input to ranking/arbitration).
    -- Resolved from os.environ USERNAME/USER at MCP startup; multi-user
    -- machines get per-account stamping for free.
    created_by          TEXT,
    updated_by          TEXT,
    -- Step 9: workstream membership. Denormalized pointer to the workstream
    -- node a leaf belongs to. NULL = orphan (tolerated). Used by focus
    -- auto-bump and kb_gate traversal seeding.
    workstream_id       INTEGER REFERENCES nodes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_kind           ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_status         ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_session        ON nodes(session_id);
CREATE INDEX IF NOT EXISTS idx_nodes_updated_at     ON nodes(updated_at);
CREATE INDEX IF NOT EXISTS idx_nodes_ref_count      ON nodes(ref_count);
CREATE INDEX IF NOT EXISTS idx_nodes_last_ref_at    ON nodes(last_referenced_at);
CREATE INDEX IF NOT EXISTS idx_nodes_parent         ON nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_workstream     ON nodes(workstream_id);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst         INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by  TEXT,
    -- Edge lifecycle: 'active' edges participate in all reads (neighbors,
    -- reconciliation_banner, gate traversal, plan_freshness_hint,
    -- UserPromptSubmit graph hop). 'tombstoned' rows are kept for audit
    -- (mirrors the node-stale idiom — supersede never deletes) but filtered
    -- out of read sites. Set via db.tombstone_edge / kb_unlink MCP tool.
    -- add_edge re-activates a tombstoned row on re-link.
    status      TEXT    NOT NULL DEFAULT 'active',
    UNIQUE(src, dst, relation)
);

CREATE INDEX IF NOT EXISTS idx_edges_src      ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst      ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);

CREATE TABLE IF NOT EXISTS sessions (
    id                 TEXT PRIMARY KEY,
    project_path       TEXT NOT NULL,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at           TEXT,
    turn_count         INTEGER NOT NULL DEFAULT 0,
    last_compact_turn  INTEGER NOT NULL DEFAULT 0,
    transcript_path    TEXT,
    summary_node_id    INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
    -- Topic-shift detector (C2): cosine(new_prompt, last_prompt_embedding)
    -- distinguishes drill-downs from topic switches.
    last_prompt_embedding BLOB,
    last_prompt_at        TEXT,
    created_by            TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_ended ON sessions(ended_at);

-- Per-session active set: which nodes have already been injected into context
-- (via SessionStart brief or per-prompt retrieval). Used by UserPromptSubmit
-- to dedupe and to power graph traversal on drill-down follow-ups.
CREATE TABLE IF NOT EXISTS session_retrievals (
    session_id        TEXT    NOT NULL,
    node_id           INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    first_injected_at TEXT    NOT NULL DEFAULT (datetime('now')),
    last_injected_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    first_injected_turn INTEGER NOT NULL DEFAULT 0,
    last_injected_turn  INTEGER NOT NULL DEFAULT 0,
    hit_count         INTEGER NOT NULL DEFAULT 1,
    sim_at_first      REAL,
    source            TEXT    NOT NULL,  -- 'session_start' | 'prompt' | 'graph'
    PRIMARY KEY (session_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_session_retrievals_sid ON session_retrievals(session_id);
CREATE INDEX IF NOT EXISTS idx_session_retrievals_last_turn ON session_retrievals(last_injected_turn);

-- Step 9: held-object focus pointers. Activity-bumped + decay-weighted.
-- Top-K rows by score = "current active workstreams". Read by SessionStart
-- brief and kb_gate traversal seeding. Cap (3) is enforced in code,
-- not schema, so manual focus pin operations can transiently exceed it.
CREATE TABLE IF NOT EXISTS focus (
    workstream_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    rank          INTEGER NOT NULL,
    score         REAL    NOT NULL,
    set_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    set_by        TEXT    NOT NULL,           -- 'auto' | 'user' | 'session_start'
    pinned        INTEGER NOT NULL DEFAULT 0  -- 1 = never auto-evict
);

CREATE INDEX IF NOT EXISTS idx_focus_score ON focus(score DESC);

-- Idempotency ledgers for history seeding.  The source ledger answers
-- "did this exact transcript revision already complete extraction?"; the
-- candidate ledger answers "did this exact reviewed candidate already create
-- a node?".  Metadata is deliberately structured and error_code is a closed,
-- privacy-safe enum: raw exception text and transcript content never belong in
-- either table.
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
CREATE INDEX IF NOT EXISTS idx_seed_import_claim ON seed_import(claim_key, state);
CREATE INDEX IF NOT EXISTS idx_seed_import_scope
    ON seed_import(project_path, workstream_key);

-- FTS5 virtual table mirrors nodes(title, body). Kept in sync via triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    title,
    body,
    content='nodes',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS nodes_ai AFTER INSERT ON nodes BEGIN
    INSERT INTO nodes_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE TRIGGER IF NOT EXISTS nodes_ad AFTER DELETE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
END;

CREATE TRIGGER IF NOT EXISTS nodes_au AFTER UPDATE ON nodes BEGIN
    INSERT INTO nodes_fts(nodes_fts, rowid, title, body) VALUES('delete', old.id, old.title, old.body);
    INSERT INTO nodes_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
