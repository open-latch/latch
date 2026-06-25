"""Priorities ("top of mind") store + gate/brief integration.

Exercises src/priorities.py and its wiring into gate.py against throwaway KBs:

- add/list/retire lifecycle; per-scope cap enforcement; oldest-first ordering
- priorities are stored unembedded (no vec_nodes row) → invisible to vector
  search / heal similarity
- priorities are FTS-indexed (trigger) but EXCLUDED from kb_gate seeds
- assemble_gate carries active priorities; build_classifier_prompt injects the
  ACTIVE PROJECT PRIORITIES block iff any exist
- retire is a soft-delete (stale), reversible, type-checked
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "hooks"))

import db          # noqa: E402
import embeddings  # noqa: E402
import gate        # noqa: E402
import priorities  # noqa: E402
import session_start  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_prio_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _ins(conn, kind, title, body, *, status="staging", workstream_id=None):
    """Insert a normal (embedded) node so hybrid_search can find it."""
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec), workstream_id=workstream_id,
    )


# ---------- store: add / list / retire ----------

def test_add_creates_active_priority_node():
    tmp, conn = _fresh_db()
    try:
        res = priorities.add_priority(conn, "always consider security")
        _assert(res.get("ok"), f"add should succeed: {res}")
        node = db.get_node(conn, res["id"])
        _assert(node["kind"] == "priority", f"kind: {node['kind']}")
        _assert(node["status"] == "canonical", f"status: {node['status']}")
        _assert(node["title"] == "always consider security", f"title: {node['title']}")
        print("PASS add_creates_active_priority_node")
    finally:
        _cleanup(tmp, conn)


def test_add_stores_no_embedding():
    """Surface-only: unembedded → no vec_nodes row → invisible to vector
    search and heal similarity."""
    tmp, conn = _fresh_db()
    try:
        res = priorities.add_priority(conn, "keep it cross-platform installable")
        node = db.get_node(conn, res["id"])
        _assert(node["embedding"] is None, "priority must have NULL embedding")
        if db.vec_loaded(conn):
            row = conn.execute(
                "SELECT count(*) AS c FROM vec_nodes WHERE rowid = ?", (res["id"],)
            ).fetchone()
            _assert(row["c"] == 0, "priority must not be in vec_nodes")
        print("PASS add_stores_no_embedding")
    finally:
        _cleanup(tmp, conn)


def test_list_floating_is_newest_first():
    """Generic (unranked) priorities float — newest stacks on top."""
    tmp, conn = _fresh_db()
    try:
        a = priorities.add_priority(conn, "first directive")["id"]
        b = priorities.add_priority(conn, "second directive")["id"]
        c = priorities.add_priority(conn, "third directive")["id"]
        ids = [p["id"] for p in priorities.list_priorities(conn)]
        _assert(ids == [c, b, a], f"newest-first order expected, got {ids}")
        # All floating → rank is None, none locked, positions 1..3 assigned.
        rows = priorities.list_priorities(conn)
        _assert(all(p["rank"] is None and not p["locked"] for p in rows),
                "generic adds must be floating (rank None, unlocked)")
        _assert([p["effective_rank"] for p in rows] == [1, 2, 3],
                "effective_rank must number 1..N in display order")
        print("PASS list_floating_is_newest_first")
    finally:
        _cleanup(tmp, conn)


def test_cap_refuses_overflow():
    tmp, conn = _fresh_db()
    try:
        for i in range(priorities.MAX_ACTIVE):
            r = priorities.add_priority(conn, f"directive {i}")
            _assert(r.get("ok"), f"add {i} should succeed: {r}")
        overflow = priorities.add_priority(conn, "one too many")
        _assert("error" in overflow, f"cap should refuse: {overflow}")
        _assert(len(overflow.get("active", [])) == priorities.MAX_ACTIVE,
                "refusal should report the active set")
        _assert(len(priorities.list_priorities(conn)) == priorities.MAX_ACTIVE,
                "no write should have happened on refusal")
        print("PASS cap_refuses_overflow")
    finally:
        _cleanup(tmp, conn)


def test_workstream_priority_scope_is_separate_from_overall():
    tmp, conn = _fresh_db()
    try:
        ws = _ins(conn, "workstream", "WS A", "active work", status="canonical")
        overall = priorities.add_priority(conn, "overall directive")["id"]
        scoped = priorities.add_priority(
            conn, "workstream directive", workstream_id=ws,
        )["id"]

        overall_rows = priorities.list_priorities(conn)
        scoped_rows = priorities.list_priorities(conn, workstream_id=ws)
        _assert([p["id"] for p in overall_rows] == [overall],
                f"overall list must not include scoped rows: {overall_rows}")
        _assert([p["id"] for p in scoped_rows] == [scoped],
                f"workstream list must include only scoped rows: {scoped_rows}")
        row = conn.execute(
            "SELECT workstream_id FROM nodes WHERE id = ?", (scoped,),
        ).fetchone()
        _assert(row["workstream_id"] == ws,
                f"scoped priority should persist workstream_id={ws}: {row}")
        _assert(scoped_rows[0]["scope"] == "workstream",
                f"scoped row should carry scope metadata: {scoped_rows[0]}")
        print("PASS workstream_priority_scope_is_separate_from_overall")
    finally:
        _cleanup(tmp, conn)


def test_workstream_priority_validates_scope_node():
    tmp, conn = _fresh_db()
    try:
        non_ws = _ins(conn, "decision", "D", "not a workstream")
        res = priorities.add_priority(
            conn, "bad scoped directive", workstream_id=non_ws,
        )
        _assert("error" in res and "not a workstream" in res["error"],
                f"non-workstream scope should be rejected: {res}")
        print("PASS workstream_priority_validates_scope_node")
    finally:
        _cleanup(tmp, conn)


def test_cap_is_per_scope():
    tmp, conn = _fresh_db()
    saved = priorities.MAX_ACTIVE
    try:
        priorities.MAX_ACTIVE = 2
        ws = _ins(conn, "workstream", "WS cap", "body", status="canonical")
        _assert(priorities.add_priority(conn, "overall one").get("ok"),
                "first overall")
        _assert(priorities.add_priority(conn, "overall two").get("ok"),
                "second overall")
        over = priorities.add_priority(conn, "overall three")
        _assert("error" in over and "overall" in over["error"],
                f"overall cap should refuse: {over}")

        _assert(priorities.add_priority(
            conn, "scoped one", workstream_id=ws,
        ).get("ok"), "first scoped")
        _assert(priorities.add_priority(
            conn, "scoped two", workstream_id=ws,
        ).get("ok"), "second scoped")
        scoped_over = priorities.add_priority(
            conn, "scoped three", workstream_id=ws,
        )
        _assert("error" in scoped_over and f"workstream {ws}" in scoped_over["error"],
                f"workstream cap should refuse independently: {scoped_over}")
        print("PASS cap_is_per_scope")
    finally:
        priorities.MAX_ACTIVE = saved
        _cleanup(tmp, conn)


def test_empty_text_refused():
    tmp, conn = _fresh_db()
    try:
        r = priorities.add_priority(conn, "   ")
        _assert("error" in r, f"empty text should be refused: {r}")
        print("PASS empty_text_refused")
    finally:
        _cleanup(tmp, conn)


def test_retire_soft_deletes_and_frees_a_slot():
    tmp, conn = _fresh_db()
    try:
        ids = [priorities.add_priority(conn, f"d{i}")["id"]
               for i in range(priorities.MAX_ACTIVE)]
        # Cap reached.
        _assert("error" in priorities.add_priority(conn, "blocked"), "cap reached")
        r = priorities.retire_priority(conn, ids[0])
        _assert(r.get("retired"), f"retire should succeed: {r}")
        active_ids = [p["id"] for p in priorities.list_priorities(conn)]
        _assert(ids[0] not in active_ids, "retired drops out of active list")
        # Slot freed → add now succeeds.
        _assert(priorities.add_priority(conn, "now allowed").get("ok"),
                "retiring should free a slot")
        # Retired still visible in the audit view.
        all_ids = [p["id"] for p in priorities.list_priorities(conn, include_retired=True)]
        _assert(ids[0] in all_ids, "retired must persist for audit")
        print("PASS retire_soft_deletes_and_frees_a_slot")
    finally:
        _cleanup(tmp, conn)


def test_retire_rejects_non_priority():
    tmp, conn = _fresh_db()
    try:
        nid = _ins(conn, "decision", "not a priority", "some decision body")
        r = priorities.retire_priority(conn, nid)
        _assert("error" in r, f"retiring a non-priority should error: {r}")
        node = db.get_node(conn, nid)
        _assert(node["status"] != "stale", "non-priority must be untouched")
        print("PASS retire_rejects_non_priority")
    finally:
        _cleanup(tmp, conn)


# ---------- ranking: locked vs floating ----------

def test_locked_not_displaced_by_generic_add():
    """The user's worked example: with ranks 1 and 2 locked, a generic add
    lands at rank 3 and pushes the old rank-3 down to 4 — locked items never
    move."""
    tmp, conn = _fresh_db()
    try:
        # Two locked at the top, then a floating one at 3.
        top = priorities.add_priority(conn, "locked top", rank=1)
        _assert(top.get("ok") and top["locked"] and top["rank"] == 1, f"lock@1: {top}")
        second = priorities.add_priority(conn, "locked second", rank=2)
        _assert(second.get("ok") and second["rank"] == 2, f"lock@2: {second}")
        old3 = priorities.add_priority(conn, "floating old third")["id"]
        order = [p["id"] for p in priorities.list_priorities(conn)]
        _assert(order == [top["id"], second["id"], old3],
                f"expected [1,2,old3], got {order}")
        # A new generic add: must take spot 3, bump old3 -> 4. Locked stay put.
        new3 = priorities.add_priority(conn, "floating newest")["id"]
        rows = priorities.list_priorities(conn)
        order = [p["id"] for p in rows]
        _assert(order == [top["id"], second["id"], new3, old3],
                f"generic add should take spot 3, push old3 to 4; got {order}")
        _assert(rows[0]["locked"] and rows[1]["locked"], "ranks 1,2 stay locked")
        _assert(not rows[2]["locked"] and not rows[3]["locked"], "3,4 float")
        print("PASS locked_not_displaced_by_generic_add")
    finally:
        _cleanup(tmp, conn)


def test_add_with_explicit_rank_collision_is_conflict():
    """Adding at a slot already locked returns a conflict and writes nothing."""
    tmp, conn = _fresh_db()
    try:
        priorities.add_priority(conn, "locked top", rank=1)
        before = len(priorities.list_priorities(conn))
        res = priorities.add_priority(conn, "wants top too", rank=1)
        _assert(res.get("conflict") == "rank_locked", f"expected conflict: {res}")
        _assert(res.get("held_by") is not None, "conflict must name the holder")
        _assert("active" in res, "conflict must include the active summary")
        _assert(len(priorities.list_priorities(conn)) == before,
                "conflict must not write a node")
        print("PASS add_with_explicit_rank_collision_is_conflict")
    finally:
        _cleanup(tmp, conn)


def test_reorder_locks_then_unlocks():
    tmp, conn = _fresh_db()
    try:
        a = priorities.add_priority(conn, "alpha")["id"]
        b = priorities.add_priority(conn, "bravo")["id"]
        c = priorities.add_priority(conn, "charlie")["id"]
        # Floating order: c, b, a. Lock a -> rank 1.
        r = priorities.reorder_priority(conn, a, 1)
        _assert(r.get("ok") and r["rank"] == 1 and r["locked"], f"lock a@1: {r}")
        order = [p["id"] for p in priorities.list_priorities(conn)]
        _assert(order[0] == a, f"a should lead after lock; got {order}")
        _assert(order == [a, c, b], f"floating b,c fill below newest-first; got {order}")
        # Unlock a -> floats back by recency (a is oldest -> last).
        r = priorities.reorder_priority(conn, a, None)
        _assert(r.get("ok") and r["rank"] is None and not r["locked"], f"unlock: {r}")
        order = [p["id"] for p in priorities.list_priorities(conn)]
        _assert(order == [c, b, a], f"unlocked a floats by recency; got {order}")
        print("PASS reorder_locks_then_unlocks")
    finally:
        _cleanup(tmp, conn)


def test_reorder_onto_locked_slot_is_conflict():
    tmp, conn = _fresh_db()
    try:
        a = priorities.add_priority(conn, "alpha", rank=1)["id"]
        b = priorities.add_priority(conn, "bravo")["id"]
        res = priorities.reorder_priority(conn, b, 1)  # slot 1 is locked by a
        _assert(res.get("conflict") == "rank_locked", f"expected conflict: {res}")
        _assert(res.get("held_by") == a, f"holder should be a: {res}")
        # a stays at 1.
        order = [p["id"] for p in priorities.list_priorities(conn)]
        _assert(order[0] == a, f"a must still lead; got {order}")
        print("PASS reorder_onto_locked_slot_is_conflict")
    finally:
        _cleanup(tmp, conn)


def test_reorder_rejects_non_priority_and_inactive():
    tmp, conn = _fresh_db()
    try:
        nid = _ins(conn, "decision", "not a priority", "body")
        _assert("error" in priorities.reorder_priority(conn, nid, 1),
                "reorder must reject a non-priority")
        pid = priorities.add_priority(conn, "x")["id"]
        priorities.retire_priority(conn, pid)
        _assert("error" in priorities.reorder_priority(conn, pid, 1),
                "reorder must reject a retired priority")
        print("PASS reorder_rejects_non_priority_and_inactive")
    finally:
        _cleanup(tmp, conn)


# ---------- graveyard ----------

def test_retire_stamps_graveyard_date():
    tmp, conn = _fresh_db()
    try:
        pid = priorities.add_priority(conn, "to be retired")["id"]
        res = priorities.retire_priority(conn, pid)
        _assert(res.get("retired") and res.get("retired_at"),
                f"retire must stamp retired_at: {res}")
        row = conn.execute(
            "SELECT rank, retired_at FROM priority_order WHERE node_id = ?", (pid,)
        ).fetchone()
        _assert(row["rank"] is None, "retired priority rank cleared")
        _assert(row["retired_at"] == res["retired_at"], "retired_at persisted")
        # Graveyard view carries the date; active view does not include it.
        grave = [p for p in priorities.list_priorities(conn, include_retired=True)
                 if p["id"] == pid]
        _assert(grave and grave[0]["retired_at"] == res["retired_at"],
                "graveyard row must carry retired_at")
        _assert(pid not in [p["id"] for p in priorities.list_priorities(conn)],
                "retired priority drops out of the active list")
        print("PASS retire_stamps_graveyard_date")
    finally:
        _cleanup(tmp, conn)


def test_retire_closes_rank_gap():
    tmp, conn = _fresh_db()
    try:
        a = priorities.add_priority(conn, "a")["id"]
        b = priorities.add_priority(conn, "b")["id"]
        c = priorities.add_priority(conn, "c")["id"]
        priorities.retire_priority(conn, b)
        rows = priorities.list_priorities(conn)
        _assert([p["effective_rank"] for p in rows] == [1, 2],
                f"remaining must renumber 1..2 contiguously; got {rows}")
        _assert(b not in [p["id"] for p in rows], "retired b is gone from active")
        print("PASS retire_closes_rank_gap")
    finally:
        _cleanup(tmp, conn)


# ---------- configurable cap ----------

def test_configurable_cap(monkeypatch=None):
    """MAX_ACTIVE is read from priorities.MAX_ACTIVE (sourced from
    CLAUDE_KB_PRIORITY_CAP at import); the cap honours whatever it is set to."""
    tmp, conn = _fresh_db()
    saved = priorities.MAX_ACTIVE
    try:
        priorities.MAX_ACTIVE = 2
        _assert(priorities.add_priority(conn, "one").get("ok"), "1st under cap")
        _assert(priorities.add_priority(conn, "two").get("ok"), "2nd at cap")
        over = priorities.add_priority(conn, "three")
        _assert("error" in over, f"3rd should be refused at cap=2: {over}")
        priorities.MAX_ACTIVE = 3
        _assert(priorities.add_priority(conn, "three").get("ok"),
                "raising the cap allows another add")
        print("PASS configurable_cap")
    finally:
        priorities.MAX_ACTIVE = saved
        _cleanup(tmp, conn)


# ---------- gate integration ----------

def test_priority_excluded_from_gate_seeds():
    """FTS indexes the priority (trigger), so a keyword query would surface it
    — but it must NOT become a gate seed."""
    tmp, conn = _fresh_db()
    try:
        priorities.add_priority(conn, "zylophone-token security guideline")
        dec = _ins(conn, "decision", "zylophone-token decision",
                   "a decision mentioning zylophone-token")
        out = gate.assemble_gate(conn, "zylophone-token")
        seed_kinds = {s["kind"] for s in out["seeds"]}
        seed_ids = {s["id"] for s in out["seeds"]}
        _assert("priority" not in seed_kinds,
                f"priority must not seed the gate: {out['seeds']}")
        _assert(dec in seed_ids, f"normal node should still seed: {seed_ids}")
        print("PASS priority_excluded_from_gate_seeds")
    finally:
        _cleanup(tmp, conn)


def test_assemble_gate_carries_priorities():
    tmp, conn = _fresh_db()
    try:
        pid = priorities.add_priority(conn, "always write tests")["id"]
        out = gate.assemble_gate(conn, "anything at all")
        _assert("priorities" in out, "assembly must include priorities key")
        ids = [p["id"] for p in out["priorities"]]
        _assert(pid in ids, f"active priority missing from assembly: {ids}")
        print("PASS assemble_gate_carries_priorities")
    finally:
        _cleanup(tmp, conn)


def test_assemble_gate_carries_scoped_priority_for_seed_workstream_only():
    tmp, conn = _fresh_db()
    try:
        ws_a = _ins(conn, "workstream", "WS Alpha", "alpha work", status="canonical")
        ws_b = _ins(conn, "workstream", "WS Bravo", "bravo work", status="canonical")
        overall = priorities.add_priority(conn, "overall tests")["id"]
        scoped_a = priorities.add_priority(
            conn, "alpha-only directive", workstream_id=ws_a,
        )["id"]
        scoped_b = priorities.add_priority(
            conn, "bravo-only directive", workstream_id=ws_b,
        )["id"]
        _ins(
            conn, "decision", "alpha-token decision",
            "body mentioning alpha-token", workstream_id=ws_a,
        )

        out = gate.assemble_gate(
            conn, "alpha-token", seed_top_k=1, focus_seed=False,
        )
        ids = {p["id"] for p in out["priorities"]}
        _assert(overall in ids, f"overall priority missing: {out['priorities']}")
        _assert(scoped_a in ids,
                f"seed workstream priority missing: {out['priorities']}")
        _assert(scoped_b not in ids,
                f"unrelated workstream priority leaked: {out['priorities']}")
        print("PASS assemble_gate_carries_scoped_priority_for_seed_workstream_only")
    finally:
        _cleanup(tmp, conn)


def test_assemble_gate_omits_scoped_priority_for_focus_only_workstream():
    tmp, conn = _fresh_db()
    try:
        ws = _ins(conn, "workstream", "Focused WS", "focused", status="canonical")
        db.set_focus(conn, ws)
        scoped = priorities.add_priority(
            conn, "focused directive", workstream_id=ws,
        )["id"]
        _ins(conn, "decision", "unrelated-token decision", "unrelated-token body")

        out = gate.assemble_gate(conn, "unrelated-token", seed_top_k=1)
        ids = {p["id"] for p in out["priorities"]}
        _assert(scoped not in ids,
                f"focus-only workstream priority leaked: {out['priorities']}")

        related = gate.assemble_gate(conn, "Focused WS", seed_top_k=1)
        related_ids = {p["id"] for p in related["priorities"]}
        _assert(scoped in related_ids,
                f"hybrid workstream priority missing: {related['priorities']}")
        print("PASS assemble_gate_omits_scoped_priority_for_focus_only_workstream")
    finally:
        _cleanup(tmp, conn)


def test_classifier_prompt_injects_block_when_present():
    assembly = {
        "query": "build a thing",
        "seeds": [], "chains": [],
        "priorities": [{"id": 7, "title": "always consider security"}],
    }
    prompt = gate.build_classifier_prompt(assembly)
    # Assert on the block DELIMITER — the bare phrase also appears in the
    # classifier system instructions, so it is always present.
    _assert("--- ACTIVE PROJECT PRIORITIES ---" in prompt, "block delimiter missing")
    _assert("P1 [id=7]" in prompt, "priority not rendered with tag/id")
    _assert("always consider security" in prompt, "priority text missing")
    print("PASS classifier_prompt_injects_block_when_present")


def test_classifier_prompt_groups_workstream_priorities():
    assembly = {
        "query": "build a thing",
        "seeds": [], "chains": [],
        "priorities": [
            {"id": 7, "title": "overall", "workstream_id": None},
            {
                "id": 8, "title": "scoped", "workstream_id": 42,
                "workstream_title": "Launch workstream",
            },
        ],
    }
    prompt = gate.build_classifier_prompt(assembly)
    _assert("Overall P1 [id=7]" in prompt, "overall priority not rendered")
    _assert("Workstream 42" in prompt and "Launch workstream" in prompt,
            "workstream heading missing")
    _assert("P1 [id=8] scoped" in prompt, "scoped priority not rendered")
    print("PASS classifier_prompt_groups_workstream_priorities")


def test_classifier_prompt_omits_block_when_empty():
    assembly = {"query": "build a thing", "seeds": [], "chains": [], "priorities": []}
    prompt = gate.build_classifier_prompt(assembly)
    _assert("--- ACTIVE PROJECT PRIORITIES ---" not in prompt,
            "block delimiter must be omitted when no priorities")
    print("PASS classifier_prompt_omits_block_when_empty")


# ---------- render helpers ----------

def test_render_helpers_empty_are_falsy():
    _assert(priorities.render_for_gate([]) == "", "empty gate render is ''")
    _assert(priorities.render_for_brief([]) == [], "empty brief render is []")
    print("PASS render_helpers_empty_are_falsy")


# ---------- SessionStart brief integration ----------

def test_brief_renders_priorities_section():
    tmp, conn = _fresh_db()
    try:
        priorities.add_priority(conn, "always consider security")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("## Top of mind (priorities)" in brief,
                f"priorities section missing from brief: {brief!r}")
        _assert("always consider security" in brief,
                "priority text missing from brief")
        shutil.rmtree(tmp, ignore_errors=True)
        print("PASS brief_renders_priorities_section")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_renders_workstream_priorities_under_workstream():
    tmp, conn = _fresh_db()
    try:
        ws = _ins(
            conn, "workstream", "Scoped brief WS", "brief body",
            status="canonical",
        )
        priorities.add_priority(conn, "overall brief directive")
        priorities.add_priority(conn, "scoped brief directive", workstream_id=ws)
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("## Top of mind (priorities)" in brief,
                f"overall priorities section missing: {brief!r}")
        _assert("overall brief directive" in brief,
                f"overall directive missing: {brief!r}")
        _assert("Scoped brief WS" in brief, f"workstream missing: {brief!r}")
        _assert("Workstream priorities:" in brief,
                f"workstream priority heading missing: {brief!r}")
        _assert("scoped brief directive" in brief,
                f"scoped directive missing: {brief!r}")
        print("PASS brief_renders_workstream_priorities_under_workstream")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_omits_priorities_section_when_none():
    tmp, conn = _fresh_db()
    try:
        # An idea so the brief still builds, but no priorities.
        _ins(conn, "idea", "some idea", "an idea body")
        conn.close()
        brief = session_start._build_briefing(tmp)
        _assert("## Top of mind (priorities)" not in brief,
                f"priorities section must be omitted when none: {brief!r}")
        print("PASS brief_omits_priorities_section_when_none")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_add_creates_active_priority_node()
    test_add_stores_no_embedding()
    test_list_floating_is_newest_first()
    test_cap_refuses_overflow()
    test_workstream_priority_scope_is_separate_from_overall()
    test_workstream_priority_validates_scope_node()
    test_cap_is_per_scope()
    test_empty_text_refused()
    test_retire_soft_deletes_and_frees_a_slot()
    test_retire_rejects_non_priority()
    test_locked_not_displaced_by_generic_add()
    test_add_with_explicit_rank_collision_is_conflict()
    test_reorder_locks_then_unlocks()
    test_reorder_onto_locked_slot_is_conflict()
    test_reorder_rejects_non_priority_and_inactive()
    test_retire_stamps_graveyard_date()
    test_retire_closes_rank_gap()
    test_configurable_cap()
    test_priority_excluded_from_gate_seeds()
    test_assemble_gate_carries_priorities()
    test_assemble_gate_carries_scoped_priority_for_seed_workstream_only()
    test_assemble_gate_omits_scoped_priority_for_focus_only_workstream()
    test_classifier_prompt_injects_block_when_present()
    test_classifier_prompt_groups_workstream_priorities()
    test_classifier_prompt_omits_block_when_empty()
    test_render_helpers_empty_are_falsy()
    test_brief_renders_priorities_section()
    test_brief_renders_workstream_priorities_under_workstream()
    test_brief_omits_priorities_section_when_none()
    print("\nAll priorities tests pass.")
