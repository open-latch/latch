"""v3 data migration — one-time data ops for step 9 PM-frame infra.

Schema additions (workstream_id column, focus table) are handled live via
db._migrate_step9_focus on every connect. This script handles the one-time
data backfills:

  1. Edge-relation normalization sweep — unify free-form synonyms via
     db.canonicalize_relation. Today this is `relates_to` → `related_to`
     (~47% of edges in a representative migration sample).
  2. workstream_id backfill — walk each node's parent_id chain up to the
     nearest workstream ancestor and stamp it. Sparse population is fine —
     orphan nodes are tolerated by design.

Both passes are idempotent: running again after first pass affects 0 rows.

Run: python migrate_v3.py <project_path>
     python migrate_v3.py --all"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import paths  # noqa: E402


def normalize_relations(conn) -> dict:
    """One-time UPDATE pass: rewrite known synonyms to canonical names.

    Walks db._TRAVERSAL_SYNONYMS + db._FREEFORM_SYNONYMS so adding a new
    synonym map entry automatically picks it up on the next migrate run.

    Idempotent: after first pass, no rows have synonym values, so re-runs
    affect 0 rows."""
    updates = {}
    for synonyms in (db._TRAVERSAL_SYNONYMS, db._FREEFORM_SYNONYMS):
        for synonym, canonical in synonyms.items():
            cur = conn.execute(
                "UPDATE edges SET relation = ? WHERE relation = ?",
                (canonical, synonym),
            )
            if cur.rowcount:
                updates[synonym] = {"to": canonical, "rows": cur.rowcount}
    conn.commit()
    return updates


def backfill_workstream_id(conn) -> dict:
    """For nodes with NULL workstream_id, walk parent_id up to find the
    nearest workstream ancestor. If found, stamp it. Most leaves will not
    find one — that's fine; a leaf that's never been parented under a
    workstream summary just stays NULL and doesn't contribute to focus
    auto-bump."""
    rows = conn.execute(
        "SELECT id, parent_id FROM nodes WHERE workstream_id IS NULL "
        "AND kind != 'workstream'"
    ).fetchall()
    set_count = 0
    visited_walks = 0
    for r in rows:
        ws = _walk_to_workstream(conn, r["id"])
        if ws is not None:
            conn.execute(
                "UPDATE nodes SET workstream_id = ? WHERE id = ?",
                (ws, r["id"]),
            )
            set_count += 1
        visited_walks += 1
    conn.commit()
    return {
        "candidates_scanned": visited_walks,
        "workstream_id_set":  set_count,
        "still_orphan":       visited_walks - set_count,
    }


def _walk_to_workstream(conn, node_id: int, max_depth: int = 8) -> int | None:
    """Walk parent_id chain up to max_depth looking for a workstream node."""
    seen = set()
    cur = node_id
    for _ in range(max_depth):
        if cur in seen:
            return None
        seen.add(cur)
        row = conn.execute(
            "SELECT id, kind, parent_id FROM nodes WHERE id = ?", (cur,)
        ).fetchone()
        if row is None:
            return None
        if row["kind"] == "workstream":
            return row["id"]
        if row["parent_id"] is None:
            return None
        cur = row["parent_id"]
    return None


def migrate_one(project_path: str) -> dict:
    """Open the project DB (which auto-runs schema migrations via db.connect)
    then run the one-time data passes."""
    conn = db.connect(project_path)
    try:
        rels = normalize_relations(conn)
        ws = backfill_workstream_id(conn)
        edge_dist = _edge_relation_distribution(conn)
        return {
            "ok": True,
            "project": project_path,
            "relations_normalized": rels,
            "workstream_id_backfill": ws,
            "edges_by_relation_post_migration": edge_dist,
        }
    finally:
        conn.close()


def _edge_relation_distribution(conn) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT relation, COUNT(*) as cnt FROM edges "
        "GROUP BY relation ORDER BY cnt DESC"
    ).fetchall()
    return [(r["relation"], r["cnt"]) for r in rows]


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
        print("usage: migrate_v3.py <project_path>")
        print("       migrate_v3.py --all")
        sys.exit(2)
    if sys.argv[1] == "--all":
        out = migrate_all()
    else:
        out = migrate_one(sys.argv[1])
    print(json.dumps(out, indent=2, default=str))
