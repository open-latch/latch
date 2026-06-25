"""v2 schema migration — idempotent. Adds ref_count, last_referenced_at,
retention_tier, parent_id, depth to `nodes`; backfills last_referenced_at
from updated_at; creates/populates `vec_nodes` via sqlite-vec.

Run: python migrate_v2.py <project_path>
     python migrate_v2.py --all          # every dir under projects/

Safe to re-run; skips columns already present and vec_nodes rows already keyed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import paths  # noqa: E402


NEW_COLUMNS = [
    ("ref_count",          "INTEGER NOT NULL DEFAULT 0"),
    ("last_referenced_at", "TEXT"),
    ("retention_tier",     "TEXT NOT NULL DEFAULT 'deep'"),
    ("parent_id",          "INTEGER REFERENCES nodes(id) ON DELETE SET NULL"),
    ("depth",              "INTEGER NOT NULL DEFAULT 0"),
]

NEW_INDEXES = [
    ("idx_nodes_ref_count",    "CREATE INDEX IF NOT EXISTS idx_nodes_ref_count   ON nodes(ref_count)"),
    ("idx_nodes_last_ref_at",  "CREATE INDEX IF NOT EXISTS idx_nodes_last_ref_at ON nodes(last_referenced_at)"),
    ("idx_nodes_parent",       "CREATE INDEX IF NOT EXISTS idx_nodes_parent      ON nodes(parent_id)"),
]


def migrate_one(project_path: str) -> dict:
    conn = db.connect(project_path)
    try:
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        added = []
        for name, ddl in NEW_COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE nodes ADD COLUMN {name} {ddl}")
            added.append(name)
        conn.execute(
            "UPDATE nodes SET last_referenced_at = updated_at WHERE last_referenced_at IS NULL"
        )
        for _, sql in NEW_INDEXES:
            conn.execute(sql)
        conn.commit()
        vec = _populate_vec_nodes(conn)
        return {"ok": True, "project": project_path, "added_columns": added, "vec": vec}
    finally:
        conn.close()


def _populate_vec_nodes(conn) -> dict:
    if not db.vec_loaded(conn):
        return {"ok": False, "reason": "sqlite-vec not loaded"}
    existing_ids = {r[0] for r in conn.execute("SELECT rowid FROM vec_nodes").fetchall()}
    rows = conn.execute(
        "SELECT id, embedding FROM nodes WHERE embedding IS NOT NULL"
    ).fetchall()
    inserted = 0
    skipped_bad_dim = 0
    for r in rows:
        if r["id"] in existing_ids:
            continue
        blob = r["embedding"]
        if blob is None or len(blob) != db.VEC_DIM * 4:
            skipped_bad_dim += 1
            continue
        conn.execute(
            "INSERT INTO vec_nodes(rowid, embedding) VALUES (?, ?)",
            (r["id"], blob),
        )
        inserted += 1
    conn.commit()
    return {
        "ok": True,
        "inserted": inserted,
        "already_present": len(existing_ids),
        "skipped_bad_dim": skipped_bad_dim,
        "total_nodes_with_embedding": len(rows),
    }


def migrate_all() -> list[dict]:
    root = paths.PROJECTS_ROOT
    results = []
    if not root.exists():
        return results
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "kb.db").exists():
            continue
        results.append(migrate_one(str(d)))
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: migrate_v2.py <project_path>")
        print("       migrate_v2.py --all")
        sys.exit(2)
    if sys.argv[1] == "--all":
        out = migrate_all()
    else:
        # argv[1] is a project_path used to derive the project dir. We accept
        # either the original source cwd (e.g. C:/path/to/your/project)
        # OR the sanitized project dir itself — db.connect() uses sanitize_cwd
        # which is stable under double-sanitization because paths do not survive
        # re-resolution. Prefer passing the original cwd.
        out = migrate_one(sys.argv[1])
    print(json.dumps(out, indent=2, default=str))
