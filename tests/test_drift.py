"""Tests for src/drift.py — deterministic nightly body-edge / state drift sweep.

Spec: KB id=1149 Part 3 / id=1157. Surface-only, no LLM, structural-only rows.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db        # noqa: E402
import drift     # noqa: E402
import log_utils # noqa: E402
import paths     # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_drift_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _node(conn, *, kind="idea", status="staging", title="n", body="b"):
    return db.insert_node(conn, kind=kind, title=title, body=body, status=status)


def _drift_rows(tmp):
    today = datetime.now(timezone.utc).date()
    return list(log_utils.read_log_range("drift", today, today, tmp))


# ---------- orphan_mention ----------

def test_orphan_mention_flagged():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn, title="target")
        subj = _node(conn, body=f"this re-parks id={tgt} with no edge")
        counts = drift.sweep(conn, tmp)
        _assert(counts["orphan_mention"] == 1, counts)
        _assert(counts["rows_emitted"] == 1, counts)
        rows = _drift_rows(tmp)
        _assert(len(rows) == 1, rows)
        r = rows[0]
        _assert(r["node_id"] == subj and r["referenced_id"] == tgt, r)
        _assert(r["finding"] == "orphan_mention", r)
        _assert("body" not in r and "excerpt" not in r and "title" not in r,
                f"row must be structural-only, got {r}")
        print("PASS orphan_mention_flagged")
    finally:
        _cleanup(tmp, conn)


def test_edged_mention_not_flagged():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn)
        subj = _node(conn, body=f"see id={tgt}")
        db.add_edge(conn, src=subj, dst=tgt, relation="related_to")
        counts = drift.sweep(conn, tmp)
        _assert(counts["orphan_mention"] == 0, counts)
        _assert(counts["rows_emitted"] == 0, counts)
        print("PASS edged_mention_not_flagged")
    finally:
        _cleanup(tmp, conn)


def test_code_span_excluded():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn)
        body = f"prose only\n```\nWHERE id={tgt}\n```\nand `id={tgt}` inline"
        subj = _node(conn, body=body)
        counts = drift.sweep(conn, tmp)
        _assert(counts["rows_emitted"] == 0,
                f"id=X only inside code must be ignored, got {counts}")
        print("PASS code_span_excluded")
    finally:
        _cleanup(tmp, conn)


def test_self_reference_ignored():
    tmp, conn = _fresh_db()
    try:
        subj = _node(conn, body="placeholder")
        db.update_node(conn, subj, body=f"this is id={subj} itself")
        counts = drift.sweep(conn, tmp)
        _assert(counts["rows_emitted"] == 0, counts)
        print("PASS self_reference_ignored")
    finally:
        _cleanup(tmp, conn)


def test_duplicate_mentions_one_row():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn)
        subj = _node(conn, body=f"id={tgt}, again id={tgt}, thrice id={tgt}")
        counts = drift.sweep(conn, tmp)
        _assert(counts["orphan_mention"] == 1, counts)
        _assert(len(_drift_rows(tmp)) == 1, "duplicates collapse to one row")
        print("PASS duplicate_mentions_one_row")
    finally:
        _cleanup(tmp, conn)


# ---------- stale_prereq ----------

def _staleprereq_setup(conn, *, ref_status, body):
    """subj (staging idea) edged to a ref of the given status."""
    ref = _node(conn, status=ref_status, title="prereq")
    subj = _node(conn, body=body.format(ref=ref))
    db.add_edge(conn, src=subj, dst=ref, relation="depends_on")
    return subj, ref


def test_stale_prereq_flagged():
    tmp, conn = _fresh_db()
    try:
        subj, ref = _staleprereq_setup(
            conn, ref_status="canonical", body="this work depends on id={ref}",
        )
        counts = drift.sweep(conn, tmp)
        _assert(counts["stale_prereq"] == 1, counts)
        _assert(counts["orphan_mention"] == 0, "edged => not an orphan")
        rows = _drift_rows(tmp)
        _assert(rows[0]["finding"] == "stale_prereq", rows)
        _assert(rows[0]["referenced_status"] == "canonical", rows)
        print("PASS stale_prereq_flagged")
    finally:
        _cleanup(tmp, conn)


def test_stale_prereq_suppressed_by_ack():
    tmp, conn = _fresh_db()
    try:
        subj, ref = _staleprereq_setup(
            conn, ref_status="canonical",
            body="depends on id={ref} — SHIPPED, done ✓",
        )
        counts = drift.sweep(conn, tmp)
        _assert(counts["stale_prereq"] == 0,
                f"ack token near mention must suppress, got {counts}")
        print("PASS stale_prereq_suppressed_by_ack")
    finally:
        _cleanup(tmp, conn)


def test_stale_prereq_requires_canonical_ref():
    tmp, conn = _fresh_db()
    try:
        subj, ref = _staleprereq_setup(
            conn, ref_status="staging", body="this work depends on id={ref}",
        )
        counts = drift.sweep(conn, tmp)
        _assert(counts["stale_prereq"] == 0,
                f"staging prereq is not 'settled', got {counts}")
        print("PASS stale_prereq_requires_canonical_ref")
    finally:
        _cleanup(tmp, conn)


def test_stale_prereq_requires_dependency_cue():
    tmp, conn = _fresh_db()
    try:
        # edged + canonical ref, but a plain citation (no dep cue) => clean
        subj, ref = _staleprereq_setup(
            conn, ref_status="canonical", body="for background see id={ref}",
        )
        counts = drift.sweep(conn, tmp)
        _assert(counts["stale_prereq"] == 0,
                f"plain citation must not flag, got {counts}")
        print("PASS stale_prereq_requires_dependency_cue")
    finally:
        _cleanup(tmp, conn)


# ---------- scan scoping ----------

def test_only_scans_target_kinds():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn, kind="fact")  # exists, but not a scannable kind
        # progress + fact nodes mention an un-edged id but must NOT be scanned
        _node(conn, kind="progress", body=f"shipped, see id={tgt}")
        _node(conn, kind="fact", body=f"per id={tgt}")
        counts = drift.sweep(conn, tmp)
        _assert(counts["nodes_scanned"] == 0,
                f"only idea/open_question/decision scanned, got {counts}")
        _assert(counts["rows_emitted"] == 0, counts)
        print("PASS only_scans_target_kinds")
    finally:
        _cleanup(tmp, conn)


def test_only_scans_staging():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn, kind="fact")  # not scannable
        _node(conn, kind="idea", status="canonical", body=f"see id={tgt}")
        counts = drift.sweep(conn, tmp)
        # canonical idea is skipped, fact target is not a scan kind => nothing
        _assert(counts["nodes_scanned"] == 0,
                f"canonical nodes not scanned, got {counts}")
        _assert(counts["rows_emitted"] == 0, counts)
        print("PASS only_scans_staging")
    finally:
        _cleanup(tmp, conn)


# ---------- latest_pending ----------

def test_latest_pending_counts_distinct_nodes():
    tmp, conn = _fresh_db()
    try:
        tgt = _node(conn)
        a = _node(conn, body=f"id={tgt}")
        b = _node(conn, body=f"id={tgt}")
        drift.sweep(conn, tmp)
        n, day = drift.latest_pending(tmp)
        _assert(n == 2, f"two distinct flagged nodes expected, got {n}")
        _assert(day == datetime.now(timezone.utc).strftime("%Y-%m-%d"), day)
        print("PASS latest_pending_counts_distinct_nodes")
    finally:
        _cleanup(tmp, conn)


def test_latest_pending_empty():
    tmp, conn = _fresh_db()
    try:
        n, day = drift.latest_pending(tmp)
        _assert(n == 0 and day is None, f"expected (0, None), got {(n, day)}")
        print("PASS latest_pending_empty")
    finally:
        _cleanup(tmp, conn)


def test_sweep_clean_db_no_rows():
    tmp, conn = _fresh_db()
    try:
        counts = drift.sweep(conn, tmp)
        _assert(counts == {"nodes_scanned": 0, "orphan_mention": 0,
                           "stale_prereq": 0, "rows_emitted": 0}, counts)
        print("PASS sweep_clean_db_no_rows")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL drift tests passed")
