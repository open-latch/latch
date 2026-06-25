"""Unit tests for edge inheritance on supersede (heal._inherit_edges).

When heal supersedes a node, the loser's structural edges must re-point to the
winner (not orphan on the stale node). Lineage edges (supersedes/replaces) stay
on the loser for audit; self-loops / loser<->winner links are retired; the
supersedes audit edge is preserved.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import embeddings  # noqa: E402
import heal  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_edgeinherit_test_")
    return tmp, db.connect(tmp)


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _mk(conn, title):
    v = embeddings.to_blob(embeddings.embed(f"{title}\n\nbody"))
    return db.insert_node(conn, kind="fact", title=title, body="body",
                          status="canonical", embedding=v)


def _edge_status(conn, src, dst, relation):
    row = conn.execute(
        "SELECT status FROM edges WHERE src=? AND dst=? AND relation=?",
        (src, dst, relation),
    ).fetchone()
    return row["status"] if row else None


# ---------- core inheritance ----------

def test_structural_edges_migrate_to_winner():
    tmp, conn = _fresh_db()
    try:
        winner, loser, A, B, C = (_mk(conn, t) for t in "wlABC")
        db.add_edge(conn, src=loser, dst=A, relation="implements")   # outbound
        db.add_edge(conn, src=loser, dst=B, relation="related_to")   # outbound
        db.add_edge(conn, src=C, dst=loser, relation="depends_on")   # inbound

        heal.apply_nightly_supersede(conn, winner, loser)

        # Winner inherits all three (anchored on winner).
        _assert(_edge_status(conn, winner, A, "implements") == "active", "impl not inherited")
        _assert(_edge_status(conn, winner, B, "related_to") == "active", "rel not inherited")
        _assert(_edge_status(conn, C, winner, "depends_on") == "active", "dep not inherited")
        # Loser's originals tombstoned.
        _assert(_edge_status(conn, loser, A, "implements") == "tombstoned", "loser impl not retired")
        _assert(_edge_status(conn, loser, B, "related_to") == "tombstoned", "loser rel not retired")
        _assert(_edge_status(conn, C, loser, "depends_on") == "tombstoned", "loser dep not retired")
        # Supersede audit edge present + active; loser stale.
        _assert(_edge_status(conn, winner, loser, "supersedes") == "active", "audit edge missing")
        _assert(db.get_node(conn, loser)["status"] == "stale", "loser not staled")
        print("PASS structural_edges_migrate_to_winner")
    finally:
        _cleanup(tmp, conn)


def test_lineage_edges_stay_on_loser():
    """A prior supersede chain (loser -> W) must NOT migrate to the winner."""
    tmp, conn = _fresh_db()
    try:
        winner, loser, W = (_mk(conn, t) for t in ("w", "l", "W"))
        db.add_edge(conn, src=loser, dst=W, relation="supersedes")

        heal.apply_nightly_supersede(conn, winner, loser)

        _assert(_edge_status(conn, loser, W, "supersedes") == "active",
                "lineage edge should stay active on loser")
        _assert(_edge_status(conn, winner, W, "supersedes") is None,
                "winner must NOT inherit a supersedes lineage edge")
        print("PASS lineage_edges_stay_on_loser")
    finally:
        _cleanup(tmp, conn)


def test_loser_to_winner_link_retired_no_selfloop():
    tmp, conn = _fresh_db()
    try:
        winner, loser = _mk(conn, "w"), _mk(conn, "l")
        db.add_edge(conn, src=loser, dst=winner, relation="related_to")

        heal.apply_nightly_supersede(conn, winner, loser)

        _assert(_edge_status(conn, winner, winner, "related_to") is None,
                "must not create a winner self-loop")
        _assert(_edge_status(conn, loser, winner, "related_to") == "tombstoned",
                "redundant loser->winner link should be retired")
        print("PASS loser_to_winner_link_retired_no_selfloop")
    finally:
        _cleanup(tmp, conn)


def test_idempotent_when_winner_already_has_edge():
    tmp, conn = _fresh_db()
    try:
        winner, loser, A = _mk(conn, "w"), _mk(conn, "l"), _mk(conn, "A")
        db.add_edge(conn, src=winner, dst=A, relation="related_to")  # winner already linked
        db.add_edge(conn, src=loser, dst=A, relation="related_to")

        heal.apply_nightly_supersede(conn, winner, loser)

        _assert(_edge_status(conn, winner, A, "related_to") == "active", "winner edge lost")
        # Exactly one winner->A related_to row (UNIQUE constraint).
        n = conn.execute(
            "SELECT COUNT(*) c FROM edges WHERE src=? AND dst=? AND relation=?",
            (winner, A, "related_to"),
        ).fetchone()["c"]
        _assert(n == 1, f"expected single winner->A edge row, got {n}")
        _assert(_edge_status(conn, loser, A, "related_to") == "tombstoned", "loser edge not retired")
        print("PASS idempotent_when_winner_already_has_edge")
    finally:
        _cleanup(tmp, conn)


def test_reconciled_by_inbound_migrates():
    """Y reconciled_by loser  ->  Y reconciled_by winner (banner follows winner)."""
    tmp, conn = _fresh_db()
    try:
        winner, loser, Y = _mk(conn, "w"), _mk(conn, "l"), _mk(conn, "Y")
        db.add_edge(conn, src=Y, dst=loser, relation="reconciled_by")

        heal.apply_nightly_supersede(conn, winner, loser)

        _assert(_edge_status(conn, Y, winner, "reconciled_by") == "active",
                "reconciled_by not re-pointed to winner")
        _assert(_edge_status(conn, Y, loser, "reconciled_by") == "tombstoned",
                "loser reconciled_by not retired")
        banner_ids = [b["linked_id"] for b in db.reconciliation_banner(conn, Y)]
        _assert(winner in banner_ids, f"winner should surface in Y's banner: {banner_ids}")
        print("PASS reconciled_by_inbound_migrates")
    finally:
        _cleanup(tmp, conn)


def test_oninsert_apply_supersede_also_inherits():
    """The on-insert path (apply_supersede: new supersedes old) inherits too."""
    tmp, conn = _fresh_db()
    try:
        new, old, A = _mk(conn, "new"), _mk(conn, "old"), _mk(conn, "A")
        db.add_edge(conn, src=old, dst=A, relation="implements")

        heal.apply_supersede(conn, new, old)

        _assert(_edge_status(conn, new, A, "implements") == "active", "on-insert path didn't inherit")
        _assert(_edge_status(conn, old, A, "implements") == "tombstoned", "old edge not retired")
        _assert(_edge_status(conn, new, old, "supersedes") == "active", "audit edge missing")
        _assert(db.get_node(conn, old)["status"] == "stale", "old not staled")
        print("PASS oninsert_apply_supersede_also_inherits")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_structural_edges_migrate_to_winner()
    test_lineage_edges_stay_on_loser()
    test_loser_to_winner_link_retired_no_selfloop()
    test_idempotent_when_winner_already_has_edge()
    test_reconciled_by_inbound_migrates()
    test_oninsert_apply_supersede_also_inherits()
    print("\nAll edge-inheritance tests pass.")
