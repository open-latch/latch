"""Unit tests for edge tombstoning (id=1088).

Exercises:
- `_migrate_edge_status` adds the `status` column on fresh + existing DBs;
  idempotent.
- `db.tombstone_edge` flips status='active' → 'tombstoned'; idempotent;
  canonicalizes the relation; nonexistent-edge no-op.
- `db.add_edge` re-activates a tombstoned row on re-link (UPSERT path);
  original `created_at` / `created_by` are preserved.
- Every edge-walking read site filters `status='active'`:
    * `db.neighbors`
    * `db.reconciliation_banner`
    * `gate._traverse_from` (BFS over TRAVERSAL_RELATIONS)
    * `heal.compute_plan_freshness_hint`
    * `heal.edge_exists_between`
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import gate  # noqa: E402
import heal  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_edges_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- migration ----------

def test_migrate_edge_status_adds_column():
    tmp, conn = _fresh_db()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        _assert("status" in cols, f"status column missing after connect: {cols}")
        # Default is 'active' — verify by inserting and reading back.
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        row = conn.execute(
            "SELECT status FROM edges WHERE src = ? AND dst = ?", (a, b)
        ).fetchone()
        _assert(row["status"] == "active", f"default status should be 'active', got {row['status']!r}")
        print("PASS migrate_edge_status_adds_column")
    finally:
        _cleanup(tmp, conn)


def test_migrate_edge_status_idempotent_on_reconnect():
    tmp, conn = _fresh_db()
    try:
        conn.close()
        # Second connect should be a no-op — no extra status columns.
        conn2 = db.connect(tmp)
        status_col_count = sum(
            1 for r in conn2.execute("PRAGMA table_info(edges)").fetchall()
            if r["name"] == "status"
        )
        _assert(status_col_count == 1, f"status appears {status_col_count} times")
        conn2.close()
        print("PASS migrate_edge_status_idempotent_on_reconnect")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- tombstone semantics ----------

def test_tombstone_removes_edge_from_neighbors():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        _assert(len(db.neighbors(conn, a)) == 1, "expected neighbor visible pre-tombstone")
        n = db.tombstone_edge(conn, src=a, dst=b, relation="related_to")
        _assert(n == 1, f"tombstone_edge should return 1 row touched, got {n}")
        _assert(len(db.neighbors(conn, a)) == 0,
                f"tombstoned edge must not appear in neighbors, got {db.neighbors(conn, a)}")
        # Row is still in the table — audit-preserving.
        row = conn.execute("SELECT status FROM edges WHERE src=? AND dst=?", (a, b)).fetchone()
        _assert(row is not None and row["status"] == "tombstoned",
                f"row should persist with status='tombstoned', got {row}")
        print("PASS tombstone_removes_edge_from_neighbors")
    finally:
        _cleanup(tmp, conn)


def test_tombstone_removes_edge_from_reconciliation_banner():
    tmp, conn = _fresh_db()
    try:
        old = db.insert_node(conn, kind="decision", title="old", body="o")
        new = db.insert_node(conn, kind="decision", title="new", body="n")
        db.add_edge(conn, src=old, dst=new, relation="reconciled_by")
        banner = db.reconciliation_banner(conn, old)
        _assert(len(banner) == 1, f"expected reconciliation banner pre-tombstone, got {banner}")
        db.tombstone_edge(conn, src=old, dst=new, relation="reconciled_by")
        banner = db.reconciliation_banner(conn, old)
        _assert(banner == [],
                f"tombstoned reconciled_by edge must not surface in banner, got {banner}")
        print("PASS tombstone_removes_edge_from_reconciliation_banner")
    finally:
        _cleanup(tmp, conn)


def test_tombstone_idempotent_second_call_noop():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        n1 = db.tombstone_edge(conn, src=a, dst=b, relation="related_to")
        n2 = db.tombstone_edge(conn, src=a, dst=b, relation="related_to")
        _assert(n1 == 1, f"first call should tombstone, got {n1}")
        _assert(n2 == 0, f"second call should be no-op, got {n2}")
        print("PASS tombstone_idempotent_second_call_noop")
    finally:
        _cleanup(tmp, conn)


def test_tombstone_nonexistent_edge_is_noop():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        # No edge ever added.
        n = db.tombstone_edge(conn, src=a, dst=b, relation="related_to")
        _assert(n == 0, f"missing-edge tombstone should be no-op, got {n}")
        print("PASS tombstone_nonexistent_edge_is_noop")
    finally:
        _cleanup(tmp, conn)


def test_tombstone_canonicalizes_relation():
    """`tombstone_edge` must hit the canonical relation row even when called
    with a synonym, mirroring add_edge's canonicalization (id=487)."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        # Caller uses the synonym — tombstone should still match the canonical row.
        n = db.tombstone_edge(conn, src=a, dst=b, relation="relates_to")
        _assert(n == 1, f"tombstone with synonym should match canonical row, got {n}")
        print("PASS tombstone_canonicalizes_relation")
    finally:
        _cleanup(tmp, conn)


# ---------- reactivation via add_edge ----------

def test_relink_reactivates_tombstoned_edge():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        original = conn.execute(
            "SELECT created_at, created_by FROM edges WHERE src=? AND dst=?", (a, b)
        ).fetchone()
        db.tombstone_edge(conn, src=a, dst=b, relation="related_to")
        # Re-link should reactivate the row, not create a duplicate.
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        rows = conn.execute(
            "SELECT status, created_at, created_by FROM edges WHERE src=? AND dst=?", (a, b)
        ).fetchall()
        _assert(len(rows) == 1, f"UNIQUE constraint must keep exactly one row, got {len(rows)}")
        _assert(rows[0]["status"] == "active",
                f"re-link must reactivate, got status={rows[0]['status']!r}")
        # Original created_at / created_by preserved (audit-stable).
        _assert(rows[0]["created_at"] == original["created_at"],
                "created_at must not change on reactivate")
        _assert(rows[0]["created_by"] == original["created_by"],
                "created_by must not change on reactivate")
        # And it surfaces in neighbors again.
        _assert(len(db.neighbors(conn, a)) == 1, "reactivated edge must appear in neighbors")
        print("PASS relink_reactivates_tombstoned_edge")
    finally:
        _cleanup(tmp, conn)


def test_add_edge_on_already_active_is_noop():
    """Re-adding an already-active edge must not duplicate the row (the existing
    INSERT OR IGNORE behavior must be preserved by the new UPSERT path)."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        rows = conn.execute(
            "SELECT COUNT(*) AS cnt FROM edges WHERE src=? AND dst=?", (a, b)
        ).fetchone()
        _assert(rows["cnt"] == 1, f"re-add must not duplicate, got cnt={rows['cnt']}")
        print("PASS add_edge_on_already_active_is_noop")
    finally:
        _cleanup(tmp, conn)


# ---------- read-site filtering ----------

def test_gate_traversal_skips_tombstoned_edge():
    """End-to-end: chain a → b → c with one tombstoned hop must not reach c."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="decision", title="A", body="a")
        b = db.insert_node(conn, kind="decision", title="B", body="b")
        c = db.insert_node(conn, kind="decision", title="C", body="c")
        # Use a canonical-traversal relation so the BFS actually walks it.
        db.add_edge(conn, src=a, dst=b, relation="depends_on")
        db.add_edge(conn, src=b, dst=c, relation="depends_on")
        # Sanity: pre-tombstone BFS reaches c from a.
        ev_pre = gate._traverse_from(
            conn, a, seed_ids={a}, max_hops=2, body_excerpt_chars=200,
        )
        ev_ids_pre = {e["id"] for e in ev_pre}
        _assert(c in ev_ids_pre, f"pre-tombstone: c should be reachable, got {ev_ids_pre}")
        # Tombstone the b→c edge; BFS must stop at b.
        db.tombstone_edge(conn, src=b, dst=c, relation="depends_on")
        ev_post = gate._traverse_from(
            conn, a, seed_ids={a}, max_hops=2, body_excerpt_chars=200,
        )
        ev_ids_post = {e["id"] for e in ev_post}
        _assert(c not in ev_ids_post,
                f"tombstoned hop must block traversal, but c is in {ev_ids_post}")
        _assert(b in ev_ids_post,
                f"b should still be reachable from a, got {ev_ids_post}")
        print("PASS gate_traversal_skips_tombstoned_edge")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_hint_skips_tombstoned_implements():
    """Regression for id=832: a tombstoned `implements`/`advances`/`depends_on`
    edge must not nag the agent to kb_update the linked plan."""
    tmp, conn = _fresh_db()
    try:
        plan = db.insert_node(conn, kind="decision", title="Plan", body="...")
        ship = db.insert_node(conn, kind="progress", title="Ship", body="...")
        db.add_edge(conn, src=ship, dst=plan, relation="implements")
        # Pre-tombstone: hint surfaces.
        hint_pre = heal.compute_plan_freshness_hint(conn, ship, "progress")
        _assert(len(hint_pre) == 1, f"pre-tombstone hint should fire, got {hint_pre}")
        db.tombstone_edge(conn, src=ship, dst=plan, relation="implements")
        hint_post = heal.compute_plan_freshness_hint(conn, ship, "progress")
        _assert(hint_post == [],
                f"tombstoned implements edge must not surface, got {hint_post}")
        print("PASS plan_freshness_hint_skips_tombstoned_implements")
    finally:
        _cleanup(tmp, conn)


def test_edge_exists_between_skips_tombstoned():
    """heal.edge_exists_between is used to avoid duplicate edges during heal.
    A tombstoned edge must be treated as absent so heal will recreate (via
    add_edge, which reactivates the row in place)."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        _assert(heal.edge_exists_between(conn, a, b), "active edge should be visible")
        db.tombstone_edge(conn, src=a, dst=b, relation="related_to")
        _assert(not heal.edge_exists_between(conn, a, b),
                "tombstoned edge must be treated as absent")
        print("PASS edge_exists_between_skips_tombstoned")
    finally:
        _cleanup(tmp, conn)


# ---------- reconciliation.log emission (KB id=1097) ----------

def _read_recon_rows(tmp):
    path = log_utils.today_log_path("reconciliation", tmp)
    if not path.exists():
        return []
    return [
        json.loads(l)
        for l in path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


def _wipe_project_dir(tmp):
    proj_dir = paths.project_dir(tmp)
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)


def test_reconciliation_log_supersedes_emits_row():
    """supersedes link → one row with relation='supersedes', src_id is the
    LOSER (edge_dst), dst_id is the WINNER (edge_src)."""
    tmp, conn = _fresh_db()
    try:
        winner = db.insert_node(conn, kind="fact", title="winner", body="w")
        loser = db.insert_node(conn, kind="fact", title="loser", body="l")
        db.add_edge(
            conn, src=winner, dst=loser, relation="supersedes",
            project_path=tmp, session_id="sess-supersedes",
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row, got {len(rows)}: {rows}")
        r = rows[0]
        _assert(r["relation"] == "supersedes", r)
        _assert(r["src_id"] == loser, f"src_id should be loser: {r}")
        _assert(r["dst_id"] == winner, f"dst_id should be winner: {r}")
        _assert(r["src_kind"] == "fact", r)
        _assert(r["dst_kind"] == "fact", r)
        _assert(r["session_id"] == "sess-supersedes", r)
        print("PASS reconciliation_log_supersedes_emits_row")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_reconciled_by_emits_row():
    """reconciled_by link → one row with relation='reconciled_by', src_id is
    the OLDER (edge_src), dst_id is the NEWER (edge_dst). Both nodes remain
    canonical (no status mutation)."""
    tmp, conn = _fresh_db()
    try:
        older = db.insert_node(conn, kind="fact", title="older", body="o")
        newer = db.insert_node(conn, kind="decision", title="newer", body="n")
        db.add_edge(
            conn, src=older, dst=newer, relation="reconciled_by",
            project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row: {rows}")
        r = rows[0]
        _assert(r["relation"] == "reconciled_by", r)
        _assert(r["src_id"] == older and r["dst_id"] == newer, r)
        _assert(r["src_kind"] == "fact", r)
        _assert(r["dst_kind"] == "decision", r)
        # Neither node was mutated.
        _assert(db.get_node(conn, older)["status"] == "staging", "older should not be stale")
        _assert(db.get_node(conn, newer)["status"] == "staging", "newer should not be stale")
        print("PASS reconciliation_log_reconciled_by_emits_row")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_replaces_emits_row():
    """replaces link → one row with relation='replaces', src_id is edge_dst
    (the replaced node) per the supersedes-style winner→loser convention."""
    tmp, conn = _fresh_db()
    try:
        replacement = db.insert_node(conn, kind="fact", title="new", body="n")
        replaced = db.insert_node(conn, kind="fact", title="old", body="o")
        db.add_edge(
            conn, src=replacement, dst=replaced, relation="replaces",
            project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row: {rows}")
        r = rows[0]
        _assert(r["relation"] == "replaces", r)
        _assert(r["src_id"] == replaced and r["dst_id"] == replacement, r)
        print("PASS reconciliation_log_replaces_emits_row")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_non_tracked_relations_do_not_emit():
    """related_to / depends_on / implements / constrains / motivates /
    tested_against — none should emit a reconciliation row."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="A", body="a")
        b = db.insert_node(conn, kind="fact", title="B", body="b")
        for rel in ("related_to", "depends_on", "implements", "constrains",
                    "motivates", "tested_against"):
            db.add_edge(
                conn, src=a, dst=b, relation=rel, project_path=tmp,
            )
        rows = _read_recon_rows(tmp)
        _assert(rows == [], f"non-reconciliation relations leaked: {rows}")
        print("PASS reconciliation_log_non_tracked_relations_do_not_emit")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_captures_pre_supersede_status():
    """Regression guard for KB id=1097 capture-before-mutation rule:
    even after `heal.apply_supersede` runs (which reorders to add_edge FIRST,
    then update_node), the emitted row MUST show the loser's PRE-stale
    status — 'staging' — never the post-mutation 'stale'."""
    tmp, conn = _fresh_db()
    try:
        winner = db.insert_node(conn, kind="fact", title="winner", body="w")
        loser = db.insert_node(conn, kind="fact", title="loser", body="l")
        # Sanity check: loser starts as 'staging'.
        _assert(db.get_node(conn, loser)["status"] == "staging",
                "loser should start as staging")
        heal.apply_supersede(
            conn, new_id=winner, old_id=loser,
            project_path=tmp, session_id="sess-pre-mutation",
        )
        # After apply_supersede, loser IS now stale.
        _assert(db.get_node(conn, loser)["status"] == "stale",
                "apply_supersede should have marked loser stale")
        # But the emitted row must show pre-stale.
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row: {rows}")
        _assert(rows[0]["src_status_before"] == "staging",
                f"src_status_before MUST be pre-supersede, got "
                f"{rows[0]['src_status_before']!r} (regression)")
        print("PASS reconciliation_log_captures_pre_supersede_status")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_captures_pre_mutation_ref_count():
    """Point-in-time capture: ref_count at the moment of the supersede edge."""
    tmp, conn = _fresh_db()
    try:
        winner = db.insert_node(conn, kind="fact", title="winner", body="w")
        loser = db.insert_node(conn, kind="fact", title="loser", body="l")
        # Bump ref_count on loser to 4.
        for _ in range(4):
            db.bump_ref_count(conn, [loser])
        loser_state = db.get_node(conn, loser)
        _assert(loser_state["ref_count"] == 4,
                f"setup: ref_count should be 4 after 4 bumps, got "
                f"{loser_state['ref_count']}")
        heal.apply_supersede(
            conn, new_id=winner, old_id=loser, project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, rows)
        _assert(rows[0]["src_ref_count_at_event"] == 4,
                f"ref_count at event should be 4: {rows[0]}")
        print("PASS reconciliation_log_captures_pre_mutation_ref_count")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_src_age_days_is_nonnegative_float():
    """src_age_days is computed from src.created_at; for a just-inserted
    node it's near zero, but it's emitted as a float, not None."""
    tmp, conn = _fresh_db()
    try:
        winner = db.insert_node(conn, kind="fact", title="winner", body="w")
        loser = db.insert_node(conn, kind="fact", title="loser", body="l")
        db.add_edge(
            conn, src=winner, dst=loser, relation="supersedes",
            project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, rows)
        age = rows[0]["src_age_days"]
        _assert(isinstance(age, float), f"age should be float, got {type(age)}")
        _assert(age >= 0.0, f"age should be >= 0: {age}")
        _assert(age < 1.0, f"freshly-inserted node should be < 1 day old: {age}")
        print("PASS reconciliation_log_src_age_days_is_nonnegative_float")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_session_touch_count_from_session_retrievals():
    """src_session_touch_count = COUNT(DISTINCT session_id) from
    session_retrievals where node_id = src."""
    tmp, conn = _fresh_db()
    try:
        winner = db.insert_node(conn, kind="fact", title="winner", body="w")
        loser = db.insert_node(conn, kind="fact", title="loser", body="l")
        # Three distinct sessions touched loser.
        for sid in ("s-1", "s-2", "s-3"):
            db.record_retrievals(
                conn, session_id=sid, turn=0,
                items=[(loser, 0.9)], source="prompt",
            )
        # And one session touched it twice (same session_id, two records →
        # dedup at the (session_id, node_id) PK; still distinct sessions=3).
        db.record_retrievals(
            conn, session_id="s-1", turn=1,
            items=[(loser, 0.95)], source="prompt",
        )
        db.add_edge(
            conn, src=winner, dst=loser, relation="supersedes",
            project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, rows)
        _assert(rows[0]["src_session_touch_count"] == 3,
                f"expected 3 distinct sessions, got "
                f"{rows[0]['src_session_touch_count']}: {rows[0]}")
        print("PASS reconciliation_log_session_touch_count_from_session_retrievals")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_canonicalizes_synonym():
    """`replaced_by` is a synonym that canonicalizes to `replaces`; emitted
    row carries the canonical form, not the synonym."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="a")
        b = db.insert_node(conn, kind="fact", title="b", body="b")
        # Synonym at the edge level; canonical at the row level.
        db.add_edge(
            conn, src=a, dst=b, relation="replaced_by", project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, rows)
        _assert(rows[0]["relation"] == "replaces",
                f"row should carry canonical 'replaces', got {rows[0]['relation']!r}")
        print("PASS reconciliation_log_canonicalizes_synonym")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_common_header_fields_present():
    """ts, project, session_id, event_type — per KB id=1091 §2."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="a")
        b = db.insert_node(conn, kind="fact", title="b", body="b")
        db.add_edge(
            conn, src=a, dst=b, relation="reconciled_by",
            project_path=tmp, session_id="sess-hdr",
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, rows)
        for key in ("ts", "project", "session_id", "event_type"):
            _assert(key in rows[0], f"missing header field {key!r}: {rows[0]}")
        _assert(rows[0]["event_type"] == "reconciliation", rows[0])
        _assert(rows[0]["session_id"] == "sess-hdr", rows[0])
        print("PASS reconciliation_log_common_header_fields_present")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_no_forbidden_fields():
    """Structural-only invariant per KB id=1091 §3: no titles, bodies, or
    free-form descriptions in the emitted row."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="seed", body="seed body")
        b = db.insert_node(conn, kind="fact", title="other", body="other body")
        db.add_edge(
            conn, src=a, dst=b, relation="supersedes", project_path=tmp,
        )
        rows = _read_recon_rows(tmp)
        _assert(len(rows) == 1, rows)
        forbidden = {"title", "body", "src_title", "src_body",
                     "dst_title", "dst_body", "description", "reason"}
        leaked = set(rows[0].keys()) & forbidden
        _assert(leaked == set(),
                f"forbidden fields leaked: {leaked} in row {rows[0]}")
        print("PASS reconciliation_log_no_forbidden_fields")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_reconciliation_log_failure_isolation():
    """If the emit_event write blows up, add_edge MUST still succeed and the
    edge row MUST still be in the DB."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="a")
        b = db.insert_node(conn, kind="fact", title="b", body="b")
        original_open = Path.open
        target = log_utils.today_log_path("reconciliation", tmp)

        def _selective_raise(self, *a_, **kw):
            if self == target:
                raise IOError("simulated disk failure")
            return original_open(self, *a_, **kw)

        Path.open = _selective_raise
        try:
            # Must NOT raise.
            db.add_edge(
                conn, src=a, dst=b, relation="supersedes", project_path=tmp,
            )
        finally:
            Path.open = original_open
        # Edge IS in the DB despite the logging failure.
        row = conn.execute(
            "SELECT 1 FROM edges WHERE src = ? AND dst = ? "
            "AND relation = 'supersedes' AND status = 'active'",
            (a, b),
        ).fetchone()
        _assert(row is not None, "edge row should be persisted under log failure")
        print("PASS reconciliation_log_failure_isolation")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)
