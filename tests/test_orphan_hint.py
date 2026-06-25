"""Unit tests for orphan_hint — body-id mentions must be edges (id=1149 Part 2).

Exercises heal.compute_orphan_hint directly (regex + edge-existence probe +
code-span stripping) and the insert_with_heal integration that surfaces the
field. A1 nudge — non-blocking; see CLAUDE.md "Body-id mentions must be edges".
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
    tmp = tempfile.mkdtemp(prefix="kb_orphan_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _node(conn, title="n", body="b"):
    return db.insert_node(conn, kind="idea", title=title, body=body)


def _ids(hints):
    return sorted(h["referenced_id"] for h in hints)


# ---------- compute_orphan_hint: core behavior ----------

def test_empty_body_no_hint():
    tmp, conn = _fresh_db()
    try:
        nid = _node(conn)
        _assert(heal.compute_orphan_hint(conn, nid, "") == [], "empty body")
        _assert(heal.compute_orphan_hint(conn, nid, None) == [], "None body")
        print("PASS empty_body_no_hint")
    finally:
        _cleanup(tmp, conn)


def test_body_with_no_id_mentions():
    tmp, conn = _fresh_db()
    try:
        nid = _node(conn)
        out = heal.compute_orphan_hint(conn, nid, "prose with no node refs at all")
        _assert(out == [], f"expected [], got {out}")
        print("PASS body_with_no_id_mentions")
    finally:
        _cleanup(tmp, conn)


def test_matched_mention_is_satisfied():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn, title="subject")
        tgt = _node(conn, title="target")
        db.add_edge(conn, src=subj, dst=tgt, relation="related_to")
        out = heal.compute_orphan_hint(conn, subj, f"this depends on id={tgt}")
        _assert(out == [], f"edged mention should be satisfied, got {out}")
        print("PASS matched_mention_is_satisfied")
    finally:
        _cleanup(tmp, conn)


def test_one_matched_one_orphan():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        edged = _node(conn)
        orphan = _node(conn)
        db.add_edge(conn, src=subj, dst=edged, relation="related_to")
        body = f"see id={edged} for the spec; supersedes id={orphan}"
        out = heal.compute_orphan_hint(conn, subj, body)
        _assert(_ids(out) == [orphan], f"only the un-edged id should surface, got {out}")
        _assert("body_excerpt" in out[0] and str(orphan) in out[0]["body_excerpt"],
                f"excerpt should contain the mention, got {out}")
        print("PASS one_matched_one_orphan")
    finally:
        _cleanup(tmp, conn)


def test_self_reference_ignored():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        out = heal.compute_orphan_hint(conn, subj, f"this is id={subj}, the node itself")
        _assert(out == [], f"self-reference must be ignored, got {out}")
        print("PASS self_reference_ignored")
    finally:
        _cleanup(tmp, conn)


def test_nonexistent_target_reported():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        out = heal.compute_orphan_hint(conn, subj, "references a dangling id=99999")
        _assert(_ids(out) == [99999], f"dangling ref should surface as orphan, got {out}")
        print("PASS nonexistent_target_reported")
    finally:
        _cleanup(tmp, conn)


def test_either_direction_satisfies():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        tgt = _node(conn)
        # edge points target -> subject (reverse of the prose direction)
        db.add_edge(conn, src=tgt, dst=subj, relation="related_to")
        out = heal.compute_orphan_hint(conn, subj, f"constrained by id={tgt}")
        _assert(out == [], f"either-direction edge should satisfy, got {out}")
        print("PASS either_direction_satisfies")
    finally:
        _cleanup(tmp, conn)


def test_tombstoned_edge_does_not_satisfy():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        tgt = _node(conn)
        db.add_edge(conn, src=subj, dst=tgt, relation="related_to")
        db.tombstone_edge(conn, src=subj, dst=tgt, relation="related_to")
        out = heal.compute_orphan_hint(conn, subj, f"see id={tgt}")
        _assert(_ids(out) == [tgt], f"tombstoned edge must NOT satisfy, got {out}")
        print("PASS tombstoned_edge_does_not_satisfy")
    finally:
        _cleanup(tmp, conn)


def test_duplicate_mentions_deduped():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        body = f"id={orphan} appears, then id={orphan} again, and id={orphan} thrice"
        out = heal.compute_orphan_hint(conn, subj, body)
        _assert(_ids(out) == [orphan], f"duplicates should collapse to one, got {out}")
        print("PASS duplicate_mentions_deduped")
    finally:
        _cleanup(tmp, conn)


# ---------- code-span false-positive guard ----------

def test_fenced_code_block_excluded():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        body = (
            "real prose with no refs\n"
            "```python\n"
            f"row = conn.execute('... WHERE id={orphan}')\n"
            "```\n"
            "more prose"
        )
        out = heal.compute_orphan_hint(conn, subj, body)
        _assert(out == [], f"id=X inside a fenced block must be ignored, got {out}")
        print("PASS fenced_code_block_excluded")
    finally:
        _cleanup(tmp, conn)


def test_inline_code_excluded():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        body = f"the query filters on `id={orphan}` in the WHERE clause"
        out = heal.compute_orphan_hint(conn, subj, body)
        _assert(out == [], f"id=X inside inline code must be ignored, got {out}")
        print("PASS inline_code_excluded")
    finally:
        _cleanup(tmp, conn)


def test_prose_mention_outside_code_still_caught():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        # same id in BOTH a code span (ignored) and prose (caught)
        body = (
            f"This work depends on id={orphan} per the spec.\n"
            f"```\nWHERE id={orphan}\n```\n"
        )
        out = heal.compute_orphan_hint(conn, subj, body)
        _assert(_ids(out) == [orphan], f"prose mention must still surface, got {out}")
        print("PASS prose_mention_outside_code_still_caught")
    finally:
        _cleanup(tmp, conn)


# ---------- word-boundary guard ----------

def test_word_boundary_no_false_match():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        # "uuid=123" / "grid=4" should NOT match \bid=\d+
        out = heal.compute_orphan_hint(conn, subj, "uuid=12345 and grid=4 are not node refs")
        _assert(out == [], f"uuid=/grid= must not match, got {out}")
        print("PASS word_boundary_no_false_match")
    finally:
        _cleanup(tmp, conn)


# ---------- kind-scope (id=1194 §1/§2) ----------

def test_kind_scope_workstream_exempt():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        body = f"ship reports: see id={orphan} for the spec"
        # As a workstream (index/summary kind) the un-edged mention is exempt.
        out = heal.compute_orphan_hint(conn, subj, body, "workstream")
        _assert(out == [], f"workstream kind must be exempt, got {out}")
        # ...but with no kind (pure-scanner contract) it still surfaces.
        out_unscoped = heal.compute_orphan_hint(conn, subj, body)
        _assert(_ids(out_unscoped) == [orphan],
                f"kind=None must preserve scan, got {out_unscoped}")
        print("PASS kind_scope_workstream_exempt")
    finally:
        _cleanup(tmp, conn)


def test_kind_scope_progress_and_fact_exempt():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        body = f"cites id={orphan}"
        for k in ("progress", "fact", "entity"):
            _assert(heal.compute_orphan_hint(conn, subj, body, k) == [],
                    f"{k} kind must be exempt")
        print("PASS kind_scope_progress_and_fact_exempt")
    finally:
        _cleanup(tmp, conn)


def test_kind_scope_spec_kinds_still_scanned():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn)
        orphan = _node(conn)
        body = f"depends on id={orphan}"
        for k in ("idea", "open_question", "decision"):
            out = heal.compute_orphan_hint(conn, subj, body, k)
            _assert(_ids(out) == [orphan], f"{k} kind must still scan, got {out}")
        print("PASS kind_scope_spec_kinds_still_scanned")
    finally:
        _cleanup(tmp, conn)


def test_insert_with_heal_workstream_no_orphan():
    """Regression for id=1194 §1: an index/summary node (workstream) with many
    un-edged history citations must not over-fire orphan_hint."""
    tmp, conn = _fresh_db()
    try:
        a = _node(conn)
        b = _node(conn)
        res = heal.insert_with_heal(
            conn, kind="workstream", title="topic index",
            body=f"ship reports: id={a}, id={b} — curated history pointers",
            use_llm=False,
        )
        _assert(res["orphan_hint"] == [],
                f"workstream insert must not over-fire, got {res['orphan_hint']}")
        print("PASS insert_with_heal_workstream_no_orphan")
    finally:
        _cleanup(tmp, conn)


# ---------- insert_with_heal integration ----------

def test_insert_with_heal_surfaces_orphan_hint():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn, title="target spec", body="unrelated content xyz")
        res = heal.insert_with_heal(
            conn, kind="idea", title="re-surface",
            body=f"brief re-park of id={tgt}; full spec lives there",
            use_llm=False,
        )
        _assert("orphan_hint" in res, f"return must carry orphan_hint, got keys {list(res)}")
        _assert(_ids(res["orphan_hint"]) == [tgt],
                f"un-edged mention should surface, got {res['orphan_hint']}")
        print("PASS insert_with_heal_surfaces_orphan_hint")
    finally:
        _cleanup(tmp, conn)


def test_insert_with_heal_linked_mention_is_clean():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn, title="target spec", body="unrelated content xyz")
        res = heal.insert_with_heal(
            conn, kind="idea", title="re-surface",
            body=f"brief re-park of id={tgt}; full spec lives there",
            links=[{"dst": tgt, "relation": "related_to"}],
            use_llm=False,
        )
        _assert(res["orphan_hint"] == [],
                f"linked mention should be clean, got {res['orphan_hint']}")
        print("PASS insert_with_heal_linked_mention_is_clean")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL orphan_hint tests passed")
