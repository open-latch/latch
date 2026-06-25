"""Unit tests for kb_verify + kb_correct (src/verify.py).

Spec: KB id=1151 (resolves id=886). Exercises:
- `verify`: OK / RECONCILED / STALE (status) / STALE (incoming supersedes) /
  NOT_FOUND precedence.
- `correct_plan`: read-only — snapshot + blast radius + framing-carrier
  candidates; mutates nothing.
- `correct_apply` supersede: inserts corrected node, supersedes edge new→old,
  stales old (body untouched), reconciled_by edges on the judged subset.
- `correct_apply` reconcile: bad node NOT staled, surfaces corrected via banner.
- correction.log: common header, event_type='correction', structural-only,
  capture-before-mutation (bad_node_status_before is PRE-stale).
- invalid mode → error; reconcile_ids dropping the bad node / missing ids.

The embedder stub is installed/removed per test (setup_function/teardown_function),
NOT at module scope — a module-level monkeypatch runs at pytest collection time
and would clobber the shared `embeddings` module for the whole session.

The embedder is monkeypatched to a no-op so tests don't pay the model
cold-load and don't touch the vec table (embedding=None path in insert_node).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import verify  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402

# The embedder is stubbed PER TEST, never at module scope. A module-level
# monkeypatch executes at pytest COLLECTION time and would clobber the shared
# `embeddings` module for the whole session — poisoning every embedding-
# dependent suite (test_tree / test_nightly_heal / test_prompt_inject). The
# setup/teardown hooks below install a no-op stub around each test and restore
# the real functions after. The stub dodges the model cold-load and the vec
# table (insert_node skips vec_nodes when embedding is None).
_orig_embed = None
_orig_to_blob = None


def setup_function(function=None):
    global _orig_embed, _orig_to_blob
    _orig_embed = verify.embeddings.embed
    _orig_to_blob = verify.embeddings.to_blob
    verify.embeddings.embed = lambda text: "stub-vec"
    verify.embeddings.to_blob = lambda vec: None


def teardown_function(function=None):
    if _orig_embed is not None:
        verify.embeddings.embed = _orig_embed
    if _orig_to_blob is not None:
        verify.embeddings.to_blob = _orig_to_blob


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_verify_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _wipe_project_dir(tmp):
    proj_dir = paths.project_dir(tmp)
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)


def _read_correction_rows(tmp):
    path = log_utils.today_log_path("correction", tmp)
    if not path.exists():
        return []
    return [
        json.loads(l)
        for l in path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


# ---------- kb_verify ----------

def test_verify_ok():
    tmp, conn = _fresh_db()
    try:
        n = db.insert_node(conn, kind="fact", title="A", body="a", status="canonical")
        v = verify.verify(conn, n)
        _assert(v["verdict"] == verify.VERIFY_OK, v)
        print("PASS verify_ok")
    finally:
        _cleanup(tmp, conn)


def test_verify_not_found():
    tmp, conn = _fresh_db()
    try:
        v = verify.verify(conn, 999999)
        _assert(v["verdict"] == verify.VERIFY_NOT_FOUND, v)
        print("PASS verify_not_found")
    finally:
        _cleanup(tmp, conn)


def test_verify_stale_by_status():
    tmp, conn = _fresh_db()
    try:
        n = db.insert_node(conn, kind="fact", title="A", body="a")
        db.update_node(conn, n, status="stale")
        v = verify.verify(conn, n)
        _assert(v["verdict"] == verify.VERIFY_STALE, v)
        print("PASS verify_stale_by_status")
    finally:
        _cleanup(tmp, conn)


def test_verify_stale_by_incoming_supersedes():
    """A node that is still 'canonical' but has an active incoming supersedes
    edge must read STALE — it lost a replacement."""
    tmp, conn = _fresh_db()
    try:
        winner = db.insert_node(conn, kind="fact", title="new", body="n", status="canonical")
        loser = db.insert_node(conn, kind="fact", title="old", body="o", status="canonical")
        db.add_edge(conn, src=winner, dst=loser, relation="supersedes")
        v = verify.verify(conn, loser)
        _assert(v["verdict"] == verify.VERIFY_STALE, v)
        _assert(v["superseded_by"] == [winner], v)
        print("PASS verify_stale_by_incoming_supersedes")
    finally:
        _cleanup(tmp, conn)


def test_verify_reconciled():
    tmp, conn = _fresh_db()
    try:
        older = db.insert_node(conn, kind="decision", title="old", body="o", status="canonical")
        newer = db.insert_node(conn, kind="decision", title="new", body="n", status="canonical")
        db.add_edge(conn, src=older, dst=newer, relation="reconciled_by")
        v = verify.verify(conn, older)
        _assert(v["verdict"] == verify.VERIFY_RECONCILED, v)
        _assert(v["reconciled_by"] == [newer], v)
        print("PASS verify_reconciled")
    finally:
        _cleanup(tmp, conn)


def test_verify_stale_precedence_over_reconciled():
    """A stale node with both an incoming supersedes and an outbound
    reconciled_by must read STALE (higher precedence)."""
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="bad", body="b")
        win = db.insert_node(conn, kind="fact", title="win", body="w")
        other = db.insert_node(conn, kind="fact", title="other", body="o")
        db.add_edge(conn, src=win, dst=bad, relation="supersedes")
        db.add_edge(conn, src=bad, dst=other, relation="reconciled_by")
        v = verify.verify(conn, bad)
        _assert(v["verdict"] == verify.VERIFY_STALE, v)
        print("PASS verify_stale_precedence_over_reconciled")
    finally:
        _cleanup(tmp, conn)


# ---------- kb_correct_plan (read-only) ----------

def test_correct_plan_is_read_only():
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="bad", body="b", status="canonical")
        carrier = db.insert_node(conn, kind="decision", title="c", body="c")
        db.add_edge(conn, src=carrier, dst=bad, relation="depends_on")
        node_count_before = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

        plan = verify.correct_plan(conn, bad)
        _assert(plan["bad_node_id"] == bad, plan)
        _assert(plan["snapshot"]["status"] == "canonical", plan)
        _assert(plan["blast_radius_size"] >= 1, plan)
        # carrier points AT bad over a canonical relation → high-confidence candidate
        cand_ids = {c["id"] for c in plan["framing_carrier_candidates"]}
        _assert(carrier in cand_ids, plan["framing_carrier_candidates"])
        hi = [c for c in plan["framing_carrier_candidates"] if c["id"] == carrier][0]
        _assert(hi["confidence"] == "high", hi)

        # Nothing mutated.
        node_count_after = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        _assert(node_count_after == node_count_before, "plan must not insert nodes")
        _assert(db.get_node(conn, bad)["status"] == "canonical", "plan must not stale the node")
        print("PASS correct_plan_is_read_only")
    finally:
        _cleanup(tmp, conn)


def test_correct_plan_not_found():
    tmp, conn = _fresh_db()
    try:
        plan = verify.correct_plan(conn, 999999)
        _assert("error" in plan, plan)
        print("PASS correct_plan_not_found")
    finally:
        _cleanup(tmp, conn)


def test_correct_plan_has_report_format():
    # The plan must steer the agent to LEAD its user-facing report with a
    # plain-English what/why/fix summary before any graph detail — most users
    # don't read edges/modes (UX intent). Field present + directive, and the
    # structured fields stay intact underneath.
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="bad", body="b", status="canonical")
        plan = verify.correct_plan(conn, bad)
        rf = plan.get("report_format", "")
        _assert(isinstance(rf, str) and rf.strip(), "report_format must be a non-empty string")
        low = rf.lower()
        _assert("plain-english" in low or "plain english" in low, rf)
        _assert("before" in low and "summary" in low, rf)
        # the technical fields must remain available alongside the new steer
        for k in ("snapshot", "blast_radius", "framing_carrier_candidates", "recommended_mode"):
            _assert(k in plan, f"{k} must remain in the plan")
        print("PASS correct_plan_has_report_format")
    finally:
        _cleanup(tmp, conn)


# ---------- kb_correct_apply: supersede ----------

def test_correct_apply_supersede_stales_old_and_wires_edge():
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="bad fact", body="wrong",
                             status="canonical")
        carrier = db.insert_node(conn, kind="decision", title="carrier", body="c",
                                 status="canonical")
        db.add_edge(conn, src=carrier, dst=bad, relation="depends_on")

        res = verify.correct_apply(
            conn, bad, mode="supersede",
            title="corrected fact", body="the right answer", kind="fact",
            reconcile_ids=[carrier], project_path=tmp, session_id="sess-x",
            trigger=verify.TRIGGER_USER_ASSERTION,
        )
        _assert(res["ok"], res)
        cid = res["corrected_node_id"]
        _assert(res["staled"] is True, res)

        # Old node is stale; its body is UNTOUCHED.
        old = db.get_node(conn, bad)
        _assert(old["status"] == "stale", old)
        _assert(old["body"] == "wrong", "supersede must NOT edit the old body")

        # supersedes edge new -> old.
        e = conn.execute(
            "SELECT 1 FROM edges WHERE src=? AND dst=? AND relation='supersedes' "
            "AND status='active'", (cid, bad),
        ).fetchone()
        _assert(e is not None, "missing corrected --supersedes--> bad edge")

        # carrier now reconciled_by -> corrected (surfaces in its banner).
        banner = db.reconciliation_banner(conn, carrier)
        _assert([b["linked_id"] for b in banner] == [cid], banner)
        _assert(res["reconcile_ids_applied"] == [carrier], res)

        # verify() now reports the corrected node OK and the bad node STALE.
        _assert(verify.verify(conn, cid)["verdict"] == verify.VERIFY_OK, "corrected should be OK")
        _assert(verify.verify(conn, bad)["verdict"] == verify.VERIFY_STALE, "bad should be STALE")
        print("PASS correct_apply_supersede_stales_old_and_wires_edge")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_correct_apply_supersede_captures_pre_stale_status_in_log():
    """Capture-before-mutation (id=1121): the correction.log row AND the
    reconciliation.log row from the supersedes edge must show the bad node's
    PRE-stale status ('canonical'), never the post-mutation 'stale'."""
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="bad", body="b",
                             status="canonical")
        verify.correct_apply(
            conn, bad, mode="supersede",
            title="fix", body="fixed", kind="fact",
            project_path=tmp, session_id="sess-cap",
        )
        _assert(db.get_node(conn, bad)["status"] == "stale", "bad should be staled")
        rows = _read_correction_rows(tmp)
        _assert(len(rows) == 1, rows)
        _assert(rows[0]["bad_node_status_before"] == "canonical",
                f"must capture PRE-stale status, got {rows[0]['bad_node_status_before']!r}")
        _assert(rows[0]["mode"] == "supersede" and rows[0]["staled"] is True, rows[0])
        print("PASS correct_apply_supersede_captures_pre_stale_status_in_log")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


# ---------- kb_correct_apply: reconcile ----------

def test_correct_apply_reconcile_keeps_both_canonical():
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="decision", title="over-applied", body="scoped",
                             status="canonical")
        res = verify.correct_apply(
            conn, bad, mode="reconcile",
            title="constraint", body="narrower framing", kind="decision",
            project_path=tmp, session_id="sess-r",
        )
        _assert(res["ok"], res)
        cid = res["corrected_node_id"]
        _assert(res["staled"] is False, res)
        # Bad node stays canonical and surfaces the correction via its banner.
        _assert(db.get_node(conn, bad)["status"] == "canonical", "reconcile must NOT stale")
        banner = db.reconciliation_banner(conn, bad)
        _assert([b["linked_id"] for b in banner] == [cid], banner)
        _assert(verify.verify(conn, bad)["verdict"] == verify.VERIFY_RECONCILED, "bad should read RECONCILED")
        print("PASS correct_apply_reconcile_keeps_both_canonical")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


# ---------- correction.log conventions ----------

def test_correction_log_header_and_structural_only():
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="secret title", body="secret body",
                             status="canonical")
        verify.correct_apply(
            conn, bad, mode="supersede",
            title="new secret title", body="new secret body", kind="fact",
            project_path=tmp, session_id="sess-hdr",
            trigger=verify.TRIGGER_USER_ASSERTION, prompt_hash="abc123def456",
        )
        rows = _read_correction_rows(tmp)
        _assert(len(rows) == 1, rows)
        r = rows[0]
        for key in ("ts", "project", "session_id", "event_type"):
            _assert(key in r, f"missing header field {key!r}: {r}")
        _assert(r["event_type"] == "correction", r)
        _assert(r["session_id"] == "sess-hdr", r)
        _assert(r["trigger"] == verify.TRIGGER_USER_ASSERTION, r)
        _assert(r["prompt_hash"] == "abc123def456", r)
        # Structural-only: no titles, bodies, or free text anywhere in the row.
        forbidden = {"title", "body", "bad_node_title", "bad_node_body",
                     "corrected_title", "corrected_body", "description", "reason",
                     "prompt", "raw_prompt"}
        leaked = set(r.keys()) & forbidden
        _assert(leaked == set(), f"forbidden fields leaked: {leaked} in {r}")
        # And no value accidentally carries the secret text.
        blob = json.dumps(r)
        _assert("secret" not in blob, f"raw text leaked into log row: {r}")
        print("PASS correction_log_header_and_structural_only")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


# ---------- guards ----------

def test_correct_apply_invalid_mode():
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="b", body="b")
        res = verify.correct_apply(
            conn, bad, mode="delete", title="x", body="y", project_path=tmp,
        )
        _assert("error" in res, res)
        print("PASS correct_apply_invalid_mode")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def test_correct_apply_drops_bad_node_and_missing_from_reconcile_ids():
    """reconcile_ids must never include the bad node itself, and missing ids
    are silently skipped — only real, distinct carriers get edges."""
    tmp, conn = _fresh_db()
    try:
        bad = db.insert_node(conn, kind="fact", title="b", body="b", status="canonical")
        carrier = db.insert_node(conn, kind="fact", title="c", body="c", status="canonical")
        res = verify.correct_apply(
            conn, bad, mode="supersede", title="x", body="y", kind="fact",
            reconcile_ids=[bad, carrier, 999999], project_path=tmp,
        )
        _assert(res["reconcile_ids_applied"] == [carrier],
                f"only the real carrier should get an edge: {res}")
        print("PASS correct_apply_drops_bad_node_and_missing_from_reconcile_ids")
    finally:
        _wipe_project_dir(tmp)
        _cleanup(tmp, conn)


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        setup_function(fn)
        try:
            fn()
        finally:
            teardown_function(fn)
    print(f"\nAll {len(fns)} verify tests passed.")


if __name__ == "__main__":
    _run_all()
