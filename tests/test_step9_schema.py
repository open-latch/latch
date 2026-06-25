"""Unit tests for Step 9 — schema additions + canonicalize_relation helper
+ migrate_v3 data passes (relates_to normalization, workstream_id backfill).

Exercises:
- canonicalize_relation: known synonyms map to canonical; canonicals unchanged;
  unknown free-form unchanged.
- is_traversal_relation: True for canonical set + synonyms; False for free-form.
- _migrate_step9_focus: workstream_id column + focus table appear on fresh
  connect; running connect twice is a no-op.
- migrate_v3.normalize_relations: rewrites synonyms; idempotent.
- migrate_v3.backfill_workstream_id: walks parent_id chain to nearest workstream;
  leaves NULL on orphan; idempotent.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import compactor  # noqa: E402
import heal  # noqa: E402
import migrate_v3  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_step9_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- canonicalize_relation ----------

def test_canonicalize_traversal_synonyms_map_to_canonical():
    _assert(db.canonicalize_relation("replaced_by") == "replaces", "replaced_by → replaces")
    _assert(db.canonicalize_relation("requires") == "depends_on", "requires → depends_on")
    _assert(db.canonicalize_relation("constrained_by") == "constrains", "constrained_by → constrains")
    _assert(db.canonicalize_relation("motivated_by") == "motivates", "motivated_by → motivates")
    _assert(db.canonicalize_relation("tested") == "tested_against", "tested → tested_against")
    print("PASS canonicalize_traversal_synonyms_map_to_canonical")


def test_canonicalize_freeform_synonyms_map_to_canonical():
    _assert(db.canonicalize_relation("relates_to") == "related_to", "relates_to → related_to")
    print("PASS canonicalize_freeform_synonyms_map_to_canonical")


def test_canonicalize_canonical_relations_unchanged():
    for rel in db.CANONICAL_TRAVERSAL_RELATIONS:
        _assert(db.canonicalize_relation(rel) == rel, f"{rel!r} should pass through unchanged")
    print("PASS canonicalize_canonical_relations_unchanged")


def test_canonicalize_unknown_relations_unchanged():
    _assert(db.canonicalize_relation("implements") == "implements", "implements untouched")
    _assert(db.canonicalize_relation("answers") == "answers", "answers untouched")
    _assert(db.canonicalize_relation("xyzzy") == "xyzzy", "unknown relation passes through")
    print("PASS canonicalize_unknown_relations_unchanged")


# ---------- is_traversal_relation ----------

def test_is_traversal_relation_true_for_canonical():
    for rel in db.CANONICAL_TRAVERSAL_RELATIONS:
        _assert(db.is_traversal_relation(rel), f"{rel!r} should be a traversal relation")
    print("PASS is_traversal_relation_true_for_canonical")


def test_is_traversal_relation_true_for_synonyms():
    _assert(db.is_traversal_relation("replaced_by"), "replaced_by → replaces (traversal)")
    _assert(db.is_traversal_relation("requires"), "requires → depends_on (traversal)")
    print("PASS is_traversal_relation_true_for_synonyms")


def test_is_traversal_relation_false_for_freeform():
    _assert(not db.is_traversal_relation("related_to"), "related_to is free-form, not traversal")
    _assert(not db.is_traversal_relation("relates_to"), "relates_to → related_to (free-form)")
    _assert(not db.is_traversal_relation("implements"), "implements is free-form")
    _assert(not db.is_traversal_relation("xyzzy"), "unknown relation is not traversal")
    print("PASS is_traversal_relation_false_for_freeform")


# ---------- _migrate_step9_focus (live additive) ----------

def test_step9_schema_workstream_id_column_added_on_fresh_connect():
    tmp, conn = _fresh_db()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        _assert("workstream_id" in cols, f"workstream_id missing from nodes: {cols}")
        print("PASS step9_schema_workstream_id_column_added_on_fresh_connect")
    finally:
        _cleanup(tmp, conn)


def test_step9_schema_focus_table_created():
    tmp, conn = _fresh_db()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='focus'"
        ).fetchone()
        _assert(row is not None, "focus table not created")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(focus)").fetchall()}
        for required in ("workstream_id", "rank", "score", "set_at", "set_by", "pinned"):
            _assert(required in cols, f"focus.{required} missing: {cols}")
        print("PASS step9_schema_focus_table_created")
    finally:
        _cleanup(tmp, conn)


def test_step9_schema_idempotent_on_reconnect():
    tmp, conn = _fresh_db()
    try:
        conn.close()
        conn2 = db.connect(tmp)
        cols = {r["name"] for r in conn2.execute("PRAGMA table_info(nodes)").fetchall()}
        _assert("workstream_id" in cols, "workstream_id should still exist after reconnect")
        # Workstream count column count should not have grown — re-connect must
        # not re-add the same column.
        ws_col_count = sum(1 for r in conn2.execute("PRAGMA table_info(nodes)").fetchall()
                           if r["name"] == "workstream_id")
        _assert(ws_col_count == 1, f"workstream_id appears {ws_col_count} times")
        conn2.close()
        print("PASS step9_schema_idempotent_on_reconnect")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- migrate_v3.normalize_relations ----------

def test_normalize_relations_rewrites_synonyms():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        # Bypass add_edge so we can write a synonym value directly.
        conn.execute("INSERT INTO edges (src, dst, relation) VALUES (?, ?, 'relates_to')", (a, b))
        conn.execute("INSERT INTO edges (src, dst, relation) VALUES (?, ?, 'requires')", (b, a))
        conn.commit()
        out = migrate_v3.normalize_relations(conn)
        _assert(out.get("relates_to", {}).get("rows") == 1, f"expected 1 relates_to rewrite, got {out}")
        _assert(out.get("requires", {}).get("rows") == 1, f"expected 1 requires rewrite, got {out}")
        # Verify post-state
        rels = {r["relation"]: r["cnt"] for r in conn.execute(
            "SELECT relation, COUNT(*) cnt FROM edges GROUP BY relation").fetchall()}
        _assert(rels.get("related_to") == 1, f"expected 1 related_to, got {rels}")
        _assert(rels.get("depends_on") == 1, f"expected 1 depends_on, got {rels}")
        _assert("relates_to" not in rels, f"relates_to should be gone: {rels}")
        _assert("requires" not in rels, f"requires should be gone: {rels}")
        print("PASS normalize_relations_rewrites_synonyms")
    finally:
        _cleanup(tmp, conn)


def test_normalize_relations_idempotent():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        conn.execute("INSERT INTO edges (src, dst, relation) VALUES (?, ?, 'relates_to')", (a, b))
        conn.commit()
        migrate_v3.normalize_relations(conn)
        out2 = migrate_v3.normalize_relations(conn)
        _assert(out2 == {}, f"second pass should be empty, got {out2}")
        print("PASS normalize_relations_idempotent")
    finally:
        _cleanup(tmp, conn)


# ---------- migrate_v3.backfill_workstream_id ----------

def test_backfill_workstream_id_walks_parent_chain():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS A", body="...")
        # A leaf parented under the workstream directly (depth 1).
        leaf = db.insert_node(conn, kind="decision", title="D1", body="d")
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (ws, leaf))
        # A grandchild via a depth-1 summary node (depth 2).
        summary = db.insert_node(conn, kind="summary", title="S1", body="s")
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (ws, summary))
        gc = db.insert_node(conn, kind="fact", title="F1", body="f")
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (summary, gc))
        conn.commit()
        out = migrate_v3.backfill_workstream_id(conn)
        _assert(out["workstream_id_set"] == 3,
                f"expected 3 set (leaf + summary + gc), got {out}")
        for nid in (leaf, summary, gc):
            row = conn.execute("SELECT workstream_id FROM nodes WHERE id = ?", (nid,)).fetchone()
            _assert(row["workstream_id"] == ws, f"node {nid} workstream_id={row['workstream_id']}, expected {ws}")
        print("PASS backfill_workstream_id_walks_parent_chain")
    finally:
        _cleanup(tmp, conn)


def test_backfill_workstream_id_leaves_orphans_null():
    tmp, conn = _fresh_db()
    try:
        # No workstream in chain — orphan.
        orphan = db.insert_node(conn, kind="fact", title="orphan", body="...")
        out = migrate_v3.backfill_workstream_id(conn)
        _assert(out["workstream_id_set"] == 0, f"expected 0 sets, got {out}")
        row = conn.execute("SELECT workstream_id FROM nodes WHERE id = ?", (orphan,)).fetchone()
        _assert(row["workstream_id"] is None, "orphan should remain NULL")
        print("PASS backfill_workstream_id_leaves_orphans_null")
    finally:
        _cleanup(tmp, conn)


def test_backfill_workstream_id_idempotent():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        leaf = db.insert_node(conn, kind="decision", title="D", body="d")
        conn.execute("UPDATE nodes SET parent_id = ? WHERE id = ?", (ws, leaf))
        conn.commit()
        out1 = migrate_v3.backfill_workstream_id(conn)
        _assert(out1["workstream_id_set"] == 1, f"first pass: {out1}")
        out2 = migrate_v3.backfill_workstream_id(conn)
        # Second pass: leaf already has workstream_id; not a candidate.
        _assert(out2["candidates_scanned"] == 0, f"second pass should have 0 candidates: {out2}")
        _assert(out2["workstream_id_set"] == 0, f"second pass should set 0: {out2}")
        print("PASS backfill_workstream_id_idempotent")
    finally:
        _cleanup(tmp, conn)


def test_backfill_workstream_id_skips_workstream_nodes():
    """Workstream nodes themselves shouldn't be candidates for backfill —
    they don't need a workstream_id pointing at themselves."""
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        out = migrate_v3.backfill_workstream_id(conn)
        # Only candidate would be ws itself; the WHERE clause excludes kind='workstream'.
        _assert(out["candidates_scanned"] == 0, f"no candidates expected, got {out}")
        row = conn.execute("SELECT workstream_id FROM nodes WHERE id = ?", (ws,)).fetchone()
        _assert(row["workstream_id"] is None, "workstream's own workstream_id stays NULL")
        print("PASS backfill_workstream_id_skips_workstream_nodes")
    finally:
        _cleanup(tmp, conn)


# ---------- Step 2 plumbing: insert_node + insert_with_heal accept workstream_id ----------

def test_insert_node_persists_workstream_id():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        nid = db.insert_node(conn, kind="fact", title="F", body="f", workstream_id=ws)
        row = conn.execute("SELECT workstream_id FROM nodes WHERE id = ?", (nid,)).fetchone()
        _assert(row["workstream_id"] == ws, f"expected workstream_id={ws}, got {row['workstream_id']}")
        # Default (no arg) is NULL.
        nid2 = db.insert_node(conn, kind="fact", title="F2", body="f2")
        row2 = conn.execute("SELECT workstream_id FROM nodes WHERE id = ?", (nid2,)).fetchone()
        _assert(row2["workstream_id"] is None, "default workstream_id should be NULL")
        print("PASS insert_node_persists_workstream_id")
    finally:
        _cleanup(tmp, conn)


def test_insert_with_heal_forwards_workstream_id():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        out = heal.insert_with_heal(
            conn, kind="fact", title="F", body="f", workstream_id=ws, use_llm=False,
        )
        row = conn.execute("SELECT workstream_id FROM nodes WHERE id = ?", (out["id"],)).fetchone()
        _assert(row["workstream_id"] == ws,
                f"insert_with_heal should forward workstream_id; got {row['workstream_id']}")
        print("PASS insert_with_heal_forwards_workstream_id")
    finally:
        _cleanup(tmp, conn)


# ---------- Step 2: db.add_edge canonicalizes synonyms on insert ----------

def test_add_edge_canonicalizes_traversal_synonym():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="requires")
        row = conn.execute("SELECT relation FROM edges WHERE src = ? AND dst = ?", (a, b)).fetchone()
        _assert(row["relation"] == "depends_on",
                f"requires should canonicalize to depends_on, got {row['relation']!r}")
        print("PASS add_edge_canonicalizes_traversal_synonym")
    finally:
        _cleanup(tmp, conn)


def test_add_edge_canonicalizes_freeform_synonym():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="relates_to")
        row = conn.execute("SELECT relation FROM edges WHERE src = ? AND dst = ?", (a, b)).fetchone()
        _assert(row["relation"] == "related_to",
                f"relates_to should canonicalize to related_to, got {row['relation']!r}")
        print("PASS add_edge_canonicalizes_freeform_synonym")
    finally:
        _cleanup(tmp, conn)


def test_add_edge_passes_through_unknown_relation():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="explains_failure_of")
        row = conn.execute("SELECT relation FROM edges WHERE src = ? AND dst = ?", (a, b)).fetchone()
        _assert(row["relation"] == "explains_failure_of",
                f"unknown free-form should pass through, got {row['relation']!r}")
        print("PASS add_edge_passes_through_unknown_relation")
    finally:
        _cleanup(tmp, conn)


# ---------- Step 2: compactor LLM result handling ----------

def test_apply_compaction_forwards_workstream_id():
    """The LLM puts workstream_id on extracted_nodes; _apply_compaction must
    pass it to insert_with_heal so the new node is tagged."""
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS demo", body="...")
        result = {
            "session_summary": {"title": "T", "body": "B"},
            "extracted_nodes": [
                {"kind": "fact", "title": "F1", "body": "f1", "workstream_id": ws},
                {"kind": "fact", "title": "F2", "body": "f2"},  # no workstream_id
            ],
            "links": [],
        }
        compactor._apply_compaction(
            conn, session_id="S1", result=result, final=False, prior_summary_id=None,
        )
        f1 = conn.execute("SELECT workstream_id FROM nodes WHERE title = 'F1'").fetchone()
        f2 = conn.execute("SELECT workstream_id FROM nodes WHERE title = 'F2'").fetchone()
        _assert(f1["workstream_id"] == ws, f"F1 should be tagged ws={ws}, got {f1['workstream_id']}")
        _assert(f2["workstream_id"] is None, f"F2 should be untagged, got {f2['workstream_id']}")
        print("PASS apply_compaction_forwards_workstream_id")
    finally:
        _cleanup(tmp, conn)


def test_apply_compaction_handles_bad_workstream_id_type():
    """LLM may emit a string or other non-int. Should not crash; should
    coerce to None (defensive)."""
    tmp, conn = _fresh_db()
    try:
        result = {
            "session_summary": {"title": "T", "body": "B"},
            "extracted_nodes": [
                {"kind": "fact", "title": "BadStr", "body": "x", "workstream_id": "not-an-int"},
                {"kind": "fact", "title": "BadObj", "body": "x", "workstream_id": {"foo": 1}},
            ],
            "links": [],
        }
        compactor._apply_compaction(
            conn, session_id="S1", result=result, final=False, prior_summary_id=None,
        )
        for t in ("BadStr", "BadObj"):
            r = conn.execute("SELECT workstream_id FROM nodes WHERE title = ?", (t,)).fetchone()
            _assert(r["workstream_id"] is None,
                    f"{t} should coerce to NULL, got {r['workstream_id']}")
        print("PASS apply_compaction_handles_bad_workstream_id_type")
    finally:
        _cleanup(tmp, conn)


# ---------- Step 2: compactor prompt vocabulary ----------

def test_compact_prompt_documents_canonical_relations():
    """Static check: COMPACT_PROMPT must mention each canonical traversal
    relation by name so the LLM has the vocabulary."""
    for rel in db.CANONICAL_TRAVERSAL_RELATIONS:
        _assert(rel in compactor.COMPACT_PROMPT,
                f"COMPACT_PROMPT does not mention canonical relation {rel!r}")
    print("PASS compact_prompt_documents_canonical_relations")


def test_compact_prompt_documents_workstream_id_field():
    """Static check: COMPACT_PROMPT must instruct the LLM about workstream_id."""
    _assert("workstream_id" in compactor.COMPACT_PROMPT,
            "COMPACT_PROMPT does not mention workstream_id")
    print("PASS compact_prompt_documents_workstream_id_field")


if __name__ == "__main__":
    test_canonicalize_traversal_synonyms_map_to_canonical()
    test_canonicalize_freeform_synonyms_map_to_canonical()
    test_canonicalize_canonical_relations_unchanged()
    test_canonicalize_unknown_relations_unchanged()
    test_is_traversal_relation_true_for_canonical()
    test_is_traversal_relation_true_for_synonyms()
    test_is_traversal_relation_false_for_freeform()
    test_step9_schema_workstream_id_column_added_on_fresh_connect()
    test_step9_schema_focus_table_created()
    test_step9_schema_idempotent_on_reconnect()
    test_normalize_relations_rewrites_synonyms()
    test_normalize_relations_idempotent()
    test_backfill_workstream_id_walks_parent_chain()
    test_backfill_workstream_id_leaves_orphans_null()
    test_backfill_workstream_id_idempotent()
    test_backfill_workstream_id_skips_workstream_nodes()
    test_insert_node_persists_workstream_id()
    test_insert_with_heal_forwards_workstream_id()
    test_add_edge_canonicalizes_traversal_synonym()
    test_add_edge_canonicalizes_freeform_synonym()
    test_add_edge_passes_through_unknown_relation()
    test_apply_compaction_forwards_workstream_id()
    test_apply_compaction_handles_bad_workstream_id_type()
    test_compact_prompt_documents_canonical_relations()
    test_compact_prompt_documents_workstream_id_field()
    print("\nAll step9 schema tests pass.")
