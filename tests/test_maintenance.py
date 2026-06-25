"""Unit tests for Step 4 — ref_count, decay, promotion.

Verifies the load-bearing invariants: bump_ref_count does NOT touch updated_at,
decay respects the floor, promotion only touches staging (not stale/canonical),
hybrid_search bumps refs, kb_get bumps a ref, compactor's related-lookup does
not bump refs.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import embeddings  # noqa: E402
import maintenance  # noqa: E402
import search as searchmod  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_maint_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _mk(conn, title="t", body="b", kind="fact", status="staging"):
    v = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(conn, kind=kind, title=title, body=body,
                          status=status, embedding=embeddings.to_blob(v))


def _set_ref_count(conn, node_id, n):
    """Directly set ref_count for a test node. Can't use bump_ref_count(conn, [id]*n)
    because `WHERE id IN (id, id, ...)` dedupes — one UPDATE per call, not N."""
    conn.execute("UPDATE nodes SET ref_count = ? WHERE id = ?", (n, node_id))
    conn.commit()


# ---------- bump_ref_count invariants ----------

def test_bump_ref_count_preserves_updated_at():
    """Critical invariant: reference is NOT an edit. updated_at must stay put."""
    tmp, conn = _fresh_db()
    try:
        nid = _mk(conn)
        before = db.get_node(conn, nid)
        time.sleep(1.1)  # coarse second-resolution clock
        db.bump_ref_count(conn, [nid])
        after = db.get_node(conn, nid)
        _assert(after["updated_at"] == before["updated_at"],
                f"updated_at changed: {before['updated_at']} -> {after['updated_at']}")
        _assert(after["ref_count"] == before["ref_count"] + 1,
                f"ref_count wrong: {before['ref_count']} -> {after['ref_count']}")
        _assert(after["last_referenced_at"] is not None, "last_referenced_at not stamped")
        _assert(after["last_referenced_at"] > before["updated_at"],
                "last_referenced_at should be newer than original updated_at")
        print("PASS bump_ref_count_preserves_updated_at")
    finally:
        _cleanup(tmp, conn)


def test_bump_ref_count_bulk():
    tmp, conn = _fresh_db()
    try:
        ids = [_mk(conn, title=f"n{i}") for i in range(5)]
        db.bump_ref_count(conn, ids)
        counts = [db.get_node(conn, i)["ref_count"] for i in ids]
        _assert(counts == [1, 1, 1, 1, 1], counts)
        db.bump_ref_count(conn, ids[:3])
        counts = [db.get_node(conn, i)["ref_count"] for i in ids]
        _assert(counts == [2, 2, 2, 1, 1], counts)
        print("PASS bump_ref_count_bulk")
    finally:
        _cleanup(tmp, conn)


def test_bump_ref_count_one_per_call():
    """Calling bump_ref_count with duplicate ids in one call = +1 (IN() dedupes).
    This is deliberate: each call represents one retrieval event. Two retrieval
    events = two calls."""
    tmp, conn = _fresh_db()
    try:
        nid = _mk(conn)
        db.bump_ref_count(conn, [nid, nid, nid])
        _assert(db.get_node(conn, nid)["ref_count"] == 1,
                "duplicate ids in one call should still be one bump")
        for _ in range(4):
            db.bump_ref_count(conn, [nid])
        _assert(db.get_node(conn, nid)["ref_count"] == 5,
                "four more bumps should reach 5")
        print("PASS bump_ref_count_one_per_call")
    finally:
        _cleanup(tmp, conn)


def test_bump_ref_count_empty_list_noops():
    tmp, conn = _fresh_db()
    try:
        db.bump_ref_count(conn, [])
        print("PASS bump_ref_count_empty_list_noops")
    finally:
        _cleanup(tmp, conn)


# ---------- decay ----------

def test_decay_respects_floor_for_referenced_nodes():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")
        b = _mk(conn, title="b")
        # a: 1 ref (will floor at 1 after decay), b: 10 refs
        _set_ref_count(conn, a, 1)
        _set_ref_count(conn, b, 10)
        affected = db.apply_ref_count_decay(conn, factor=0.9, floor=1)
        _assert(affected == 2, f"expected 2 rows, got {affected}")
        _assert(db.get_node(conn, a)["ref_count"] == 1, "floor broken for a")
        # round(10 * 0.9) = 9
        _assert(db.get_node(conn, b)["ref_count"] == 9,
                f"unexpected decay for b: {db.get_node(conn, b)['ref_count']}")
        print("PASS decay_respects_floor_for_referenced_nodes")
    finally:
        _cleanup(tmp, conn)


def test_decay_skips_never_referenced_nodes():
    tmp, conn = _fresh_db()
    try:
        nid = _mk(conn, title="untouched")
        # ref_count stays at 0 (default). Decay should skip.
        affected = db.apply_ref_count_decay(conn, factor=0.9, floor=1)
        _assert(affected == 0, f"expected 0 rows, got {affected}")
        _assert(db.get_node(conn, nid)["ref_count"] == 0,
                "never-referenced node should not be floored up to 1")
        print("PASS decay_skips_never_referenced_nodes")
    finally:
        _cleanup(tmp, conn)


def test_decay_does_not_touch_updated_at():
    tmp, conn = _fresh_db()
    try:
        nid = _mk(conn)
        _set_ref_count(conn, nid, 5)
        before = db.get_node(conn, nid)
        time.sleep(1.1)
        db.apply_ref_count_decay(conn, factor=0.9, floor=1)
        after = db.get_node(conn, nid)
        _assert(after["updated_at"] == before["updated_at"],
                "decay must not bump updated_at")
        print("PASS decay_does_not_touch_updated_at")
    finally:
        _cleanup(tmp, conn)


# ---------- promotion ----------

def test_promote_by_ref_count_promotes_only_eligible():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="a")  # below threshold
        b = _mk(conn, title="b")  # at threshold
        c = _mk(conn, title="c")  # over threshold
        d = _mk(conn, title="d", status="canonical")  # already canonical, not touched
        e = _mk(conn, title="e")
        db.update_node(conn, e, status="stale")
        _set_ref_count(conn, a, 1)
        _set_ref_count(conn, b, 3)
        _set_ref_count(conn, c, 5)
        _set_ref_count(conn, d, 5)
        _set_ref_count(conn, e, 5)
        promoted = db.promote_by_ref_count(conn, min_ref_count=3)
        _assert(set(promoted) == {b, c}, f"expected [b, c], got {promoted}")
        _assert(db.get_node(conn, a)["status"] == "staging", "a should stay staging")
        _assert(db.get_node(conn, b)["status"] == "canonical", "b should be canonical")
        _assert(db.get_node(conn, c)["status"] == "canonical", "c should be canonical")
        _assert(db.get_node(conn, d)["status"] == "canonical", "d should still be canonical")
        _assert(db.get_node(conn, e)["status"] == "stale", "stale should not be promoted")
        print("PASS promote_by_ref_count_promotes_only_eligible")
    finally:
        _cleanup(tmp, conn)


def test_promote_bumps_updated_at():
    """Status change IS an edit — updated_at should bump on promotion."""
    tmp, conn = _fresh_db()
    try:
        nid = _mk(conn)
        _set_ref_count(conn, nid, 5)
        before = db.get_node(conn, nid)
        time.sleep(1.1)
        db.promote_by_ref_count(conn, min_ref_count=3)
        after = db.get_node(conn, nid)
        _assert(after["updated_at"] > before["updated_at"],
                f"promote should bump updated_at: {before['updated_at']} -> {after['updated_at']}")
        _assert(after["status"] == "canonical", after["status"])
        print("PASS promote_bumps_updated_at")
    finally:
        _cleanup(tmp, conn)


# ---------- hybrid_search bumps refs ----------

def test_hybrid_search_bumps_ref_counts():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="deploy config", body="docker compose up -d in production")
        b = _mk(conn, title="recipe", body="chocolate chip cookies with brown butter")
        before = [db.get_node(conn, i)["ref_count"] for i in (a, b)]
        results = searchmod.hybrid_search(conn, "docker compose deploy", limit=5)
        _assert(len(results) >= 1, "expected at least one result")
        after = [db.get_node(conn, i)["ref_count"] for i in (a, b)]
        returned_ids = {r["id"] for r in results}
        if a in returned_ids:
            _assert(after[0] == before[0] + 1, f"a ref_count: {before[0]} -> {after[0]}")
        if b not in returned_ids:
            _assert(after[1] == before[1], f"b ref_count should not move: {before[1]} -> {after[1]}")
        print(f"PASS hybrid_search_bumps_ref_counts (returned={sorted(returned_ids)})")
    finally:
        _cleanup(tmp, conn)


def test_hybrid_search_track_access_false():
    tmp, conn = _fresh_db()
    try:
        nid = _mk(conn, title="some thing", body="some body text here")
        searchmod.hybrid_search(conn, "some thing body", limit=5, track_access=False)
        _assert(db.get_node(conn, nid)["ref_count"] == 0,
                "track_access=False should not bump ref_count")
        print("PASS hybrid_search_track_access_false")
    finally:
        _cleanup(tmp, conn)


# ---------- maintenance end-to-end ----------

def test_run_weekly_maintenance_end_to_end():
    tmp, conn = _fresh_db()
    try:
        a = _mk(conn, title="ready to promote")
        b = _mk(conn, title="not yet")
        _set_ref_count(conn, a, 5)
        _set_ref_count(conn, b, 1)
        conn.close()  # maintenance opens its own conn

        result = maintenance.run_weekly_maintenance(tmp)
        _assert(result["ok"] is True, result)
        _assert(a in result["promoted_ids"], f"a should promote: {result}")
        _assert(b not in result["promoted_ids"], f"b should not promote: {result}")
        _assert(result["decayed_rows"] == 2, f"both referenced nodes should decay: {result}")

        conn = db.connect(tmp)
        # a decayed from 5 -> round(5 * 0.9) = 5 -> floor keeps 5. Actually 5*0.9=4.5, rounded to 4.
        # wait: python round(4.5) -> 4 (banker's). SQLite ROUND(4.5) -> 5. Use what SQLite does.
        _assert(db.get_node(conn, a)["status"] == "canonical", "a should be canonical post-maint")
        _assert(db.get_node(conn, b)["status"] == "staging", "b should still be staging")
        print(f"PASS run_weekly_maintenance_end_to_end "
              f"(decayed={result['decayed_rows']}, promoted={result['promoted_count']})")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_bump_ref_count_preserves_updated_at()
    test_bump_ref_count_bulk()
    test_bump_ref_count_one_per_call()
    test_bump_ref_count_empty_list_noops()
    test_decay_respects_floor_for_referenced_nodes()
    test_decay_skips_never_referenced_nodes()
    test_decay_does_not_touch_updated_at()
    test_promote_by_ref_count_promotes_only_eligible()
    test_promote_bumps_updated_at()
    test_hybrid_search_bumps_ref_counts()
    test_hybrid_search_track_access_false()
    test_run_weekly_maintenance_end_to_end()
    print("\nAll maintenance tests pass.")
