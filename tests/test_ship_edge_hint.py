"""Unit tests for ship_edge_hint + the plan_freshness widening (id=1194 §3/§4).

ship_edge_hint: a deterministic, structural A1 nudge that fires when a
`progress` node links to a spec node (idea/open_question/decision) via
`related_to` — a likely mis-typed ship edge that should be implements/
advances/depends_on. Scoped to src=progress so legitimate idea<->idea sibling
`related_to` is never flagged.

plan_freshness widening: PLAN_KINDS now includes idea/open_question, so a
progress node implementing a parked idea nudges a body refresh (folds axis-3
self-state drift into the existing axis-2 mechanism — the id=871 shape).
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import heal  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_ship_edge_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _ids(hints):
    return sorted(h["linked_id"] for h in hints)


# ---------- compute_ship_edge_hint ----------

def test_progress_related_to_idea_flags():
    tmp, conn = _fresh_db()
    try:
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        spec = db.insert_node(conn, kind="idea", title="parked spec", body="...")
        db.add_edge(conn, src=ship, dst=spec, relation="related_to")
        out = heal.compute_ship_edge_hint(conn, ship, "progress")
        _assert(_ids(out) == [spec], f"mis-typed related_to should flag, got {out}")
        print("PASS progress_related_to_idea_flags")
    finally:
        _cleanup(tmp, conn)


def test_progress_implements_idea_no_hint():
    tmp, conn = _fresh_db()
    try:
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        spec = db.insert_node(conn, kind="idea", title="parked spec", body="...")
        db.add_edge(conn, src=ship, dst=spec, relation="implements")
        out = heal.compute_ship_edge_hint(conn, ship, "progress")
        _assert(out == [], f"correct ship relation must not flag, got {out}")
        print("PASS progress_implements_idea_no_hint")
    finally:
        _cleanup(tmp, conn)


def test_idea_related_to_idea_no_hint():
    """The sibling case (id=1172 <-> id=1149): idea->idea related_to is
    legitimate and must never be flagged — src is not a progress node."""
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="idea", title="sib a", body="...")
        b = db.insert_node(conn, kind="idea", title="sib b", body="...")
        db.add_edge(conn, src=a, dst=b, relation="related_to")
        out = heal.compute_ship_edge_hint(conn, a, "idea")
        _assert(out == [], f"idea->idea sibling must not flag, got {out}")
        print("PASS idea_related_to_idea_no_hint")
    finally:
        _cleanup(tmp, conn)


def test_progress_related_to_nonspec_no_hint():
    """progress -> fact/entity/workstream via related_to is an ordinary
    citation, not a mis-typed ship edge — must not flag."""
    tmp, conn = _fresh_db()
    try:
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        for k in ("fact", "entity", "workstream", "progress"):
            tgt = db.insert_node(conn, kind=k, title=f"t-{k}", body="...")
            db.add_edge(conn, src=ship, dst=tgt, relation="related_to")
        out = heal.compute_ship_edge_hint(conn, ship, "progress")
        _assert(out == [], f"non-spec targets must not flag, got {out}")
        print("PASS progress_related_to_nonspec_no_hint")
    finally:
        _cleanup(tmp, conn)


def test_open_question_and_decision_targets_flag():
    tmp, conn = _fresh_db()
    try:
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        oq = db.insert_node(conn, kind="open_question", title="oq", body="...")
        dec = db.insert_node(conn, kind="decision", title="dec", body="...")
        db.add_edge(conn, src=ship, dst=oq, relation="related_to")
        db.add_edge(conn, src=ship, dst=dec, relation="related_to")
        out = heal.compute_ship_edge_hint(conn, ship, "progress")
        _assert(_ids(out) == sorted([oq, dec]), f"both spec kinds flag, got {out}")
        print("PASS open_question_and_decision_targets_flag")
    finally:
        _cleanup(tmp, conn)


def test_ship_edge_hint_skips_tombstoned():
    tmp, conn = _fresh_db()
    try:
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        spec = db.insert_node(conn, kind="idea", title="spec", body="...")
        db.add_edge(conn, src=ship, dst=spec, relation="related_to")
        db.tombstone_edge(conn, src=ship, dst=spec, relation="related_to")
        out = heal.compute_ship_edge_hint(conn, ship, "progress")
        _assert(out == [], f"tombstoned edge must not flag, got {out}")
        print("PASS ship_edge_hint_skips_tombstoned")
    finally:
        _cleanup(tmp, conn)


# ---------- insert_with_heal integration ----------

def test_insert_with_heal_surfaces_ship_edge_hint():
    tmp, conn = _fresh_db()
    try:
        spec = db.insert_node(conn, kind="idea", title="parked spec", body="...")
        res = heal.insert_with_heal(
            conn, kind="progress", title="shipped it",
            body="implemented the spec",
            links=[{"dst": spec, "relation": "related_to"}],
            use_llm=False,
        )
        _assert("ship_edge_hint" in res, f"return must carry field, got {list(res)}")
        _assert(_ids(res["ship_edge_hint"]) == [spec],
                f"related_to ship edge should surface, got {res['ship_edge_hint']}")
        print("PASS insert_with_heal_surfaces_ship_edge_hint")
    finally:
        _cleanup(tmp, conn)


def test_insert_with_heal_correct_relation_no_ship_hint():
    tmp, conn = _fresh_db()
    try:
        spec = db.insert_node(conn, kind="idea", title="parked spec", body="...")
        res = heal.insert_with_heal(
            conn, kind="progress", title="shipped it",
            body="implemented the spec",
            links=[{"dst": spec, "relation": "implements"}],
            use_llm=False,
        )
        _assert(res["ship_edge_hint"] == [],
                f"implements edge must not flag, got {res['ship_edge_hint']}")
        print("PASS insert_with_heal_correct_relation_no_ship_hint")
    finally:
        _cleanup(tmp, conn)


# ---------- plan_freshness widening to spec kinds (id=1194 §3) ----------

def test_plan_freshness_fires_on_idea_target():
    """Axis-3 fold-in: a progress node implementing a parked idea now nudges a
    body refresh, where previously idea targets were excluded (the id=871 miss)."""
    tmp, conn = _fresh_db()
    try:
        spec = db.insert_node(conn, kind="idea", title="parked spec", body="...")
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        db.add_edge(conn, src=ship, dst=spec, relation="implements")
        hint = heal.compute_plan_freshness_hint(conn, ship, "progress")
        _assert([h["linked_id"] for h in hint] == [spec],
                f"idea target should now nudge plan-freshness, got {hint}")
        print("PASS plan_freshness_fires_on_idea_target")
    finally:
        _cleanup(tmp, conn)


def test_plan_freshness_fires_on_open_question_target():
    tmp, conn = _fresh_db()
    try:
        oq = db.insert_node(conn, kind="open_question", title="oq", body="...")
        ship = db.insert_node(conn, kind="progress", title="ship", body="...")
        db.add_edge(conn, src=ship, dst=oq, relation="advances")
        hint = heal.compute_plan_freshness_hint(conn, ship, "progress")
        _assert([h["linked_id"] for h in hint] == [oq],
                f"open_question target should nudge, got {hint}")
        print("PASS plan_freshness_fires_on_open_question_target")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL ship_edge_hint tests passed")
