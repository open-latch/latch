"""Tests for src/correlator.py — offline correlator that joins gate.log
verdicts to in-session activity and emits gate_outcome.log rows.

Spec: KB id=1098.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import correlator  # noqa: E402
import db          # noqa: E402
import log_utils   # noqa: E402
import paths       # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_correlator_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- synthetic-row helpers ----------

DEFAULT_SID = "11111111-1111-1111-1111-111111111111"


def _gate_log_path(tmp: str, date_str: str) -> Path:
    return paths.project_dir(tmp) / f"gate-{date_str}.log"


def _outcome_log_path(tmp: str, date_str: str) -> Path:
    return paths.project_dir(tmp) / f"gate_outcome-{date_str}.log"


def _write_gate_row(
    tmp: str, *, date_str: str = "2026-05-25", hour: int = 12, minute: int = 0,
    session_id: str | None = DEFAULT_SID, query_hash: str = "abc12345",
    recommendation: str = "PROCEED", evidence_ids: list[int] | None = None,
    skipped: bool = False,
) -> dict:
    """Append a synthetic gate.log row to the daily file."""
    proj_dir = paths.project_dir(tmp)
    proj_dir.mkdir(parents=True, exist_ok=True)
    ts = f"{date_str}T{hour:02d}:{minute:02d}:00.000Z"
    row = {
        "ts": ts,
        "project": paths.sanitize_cwd(tmp),
        "session_id": session_id,
        "event_type": "gate",
        "query_hash": query_hash,
        "query_excerpt": "",
        "query_chars": 0,
        "recommendation": recommendation,
        "skipped": skipped,
        "error": None,
        "evidence_ids": list(evidence_ids or []),
        "decision_chain": [],
        "seed_count": 0,
        "seed_ids": [],
        "reachable_count": 0,
        "elapsed_ms": 100.0,
        "budget_count": 0,
    }
    with _gate_log_path(tmp, date_str).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row


def _write_reconciliation_row(
    tmp: str, *, date_str: str, hour: int, minute: int,
    session_id: str = DEFAULT_SID,
) -> None:
    proj_dir = paths.project_dir(tmp)
    proj_dir.mkdir(parents=True, exist_ok=True)
    ts = f"{date_str}T{hour:02d}:{minute:02d}:00.000Z"
    row = {
        "ts": ts,
        "project": paths.sanitize_cwd(tmp),
        "session_id": session_id,
        "event_type": "reconciliation",
        "src_id": 1, "dst_id": 2, "relation": "supersedes",
        "src_status_before": "staging",
        "src_ref_count": 0,
        "src_age_days": 0.0,
        "src_session_touch_count": 0,
        "src_kind": "fact", "dst_kind": "fact",
        "elapsed_ms": 1,
    }
    path = proj_dir / f"reconciliation-{date_str}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _write_correction_row(
    tmp: str, *, date_str: str, hour: int, minute: int,
    bad_node_id: int, session_id: str = DEFAULT_SID, mode: str = "supersede",
) -> None:
    """Append a synthetic correction.log row (structural-only)."""
    proj_dir = paths.project_dir(tmp)
    proj_dir.mkdir(parents=True, exist_ok=True)
    ts = f"{date_str}T{hour:02d}:{minute:02d}:00.000Z"
    row = {
        "ts": ts,
        "project": paths.sanitize_cwd(tmp),
        "session_id": session_id,
        "event_type": "correction",
        "bad_node_id": bad_node_id,
        "bad_node_kind": "fact",
        "bad_node_status_before": "canonical",
        "bad_node_ref_count": 0,
        "bad_node_age_days": 1.0,
        "bad_node_inbound_edges": 0,
        "mode": mode,
        "staled": mode == "supersede",
        "corrected_node_id": bad_node_id + 1000,
        "corrected_node_kind": "fact",
        "reconcile_ids": [],
        "reconcile_count": 0,
        "trigger": "user_assertion",
        "prompt_hash": None,
        "elapsed_ms": 1,
    }
    path = proj_dir / f"correction-{date_str}.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def _insert_node_at(
    conn, *, kind: str = "fact", session_id: str | None = DEFAULT_SID,
    created_at: str, title: str = "t", body: str = "b",
) -> int:
    """Insert a node and force ``created_at`` / ``updated_at`` to a fixed
    DB-format timestamp."""
    nid = db.insert_node(conn, kind=kind, title=title, body=body,
                        session_id=session_id, status="staging")
    conn.execute(
        "UPDATE nodes SET created_at = ?, updated_at = ? WHERE id = ?",
        (created_at, created_at, nid),
    )
    conn.commit()
    return nid


def _set_updated_at(conn, node_id: int, updated_at: str) -> None:
    conn.execute(
        "UPDATE nodes SET updated_at = ? WHERE id = ?", (updated_at, node_id),
    )
    conn.commit()


def _add_edge_at(
    conn, *, src: int, dst: int, relation: str, created_at: str,
) -> None:
    """Add an active edge with a controlled ``created_at``."""
    conn.execute(
        "INSERT INTO edges (src, dst, relation, status, created_at, created_by) "
        "VALUES (?, ?, ?, 'active', ?, 'test') "
        "ON CONFLICT(src, dst, relation) DO UPDATE SET status='active', created_at=excluded.created_at",
        (src, dst, relation, created_at),
    )
    conn.commit()


def _bump_retrieval(
    conn, *, session_id: str = DEFAULT_SID, node_id: int,
    last_injected_at: str,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO session_retrievals "
        "(session_id, node_id, last_injected_at, first_injected_at, "
        " first_injected_turn, last_injected_turn, hit_count, source) "
        "VALUES (?, ?, ?, ?, 0, 0, 1, 'test')",
        (session_id, node_id, last_injected_at, last_injected_at),
    )
    conn.commit()


def _read_outcome_rows(tmp: str, date_str: str = "2026-05-25") -> list[dict]:
    path = _outcome_log_path(tmp, date_str)
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------- outcome classification: PROCEED ----------

def test_proceed_with_progress_insert_classifies_accepted():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="PROCEED")
        _insert_node_at(conn, kind="progress",
                        created_at="2026-05-25 12:10:00")
        counts = correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
        )
        _assert(counts["rows_emitted"] == 1, counts)
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "ACCEPTED",
                f"expected ACCEPTED: {rows[0]}")
        print("PASS proceed_with_progress_insert_classifies_accepted")
    finally:
        _cleanup(tmp, conn)


def test_proceed_with_no_insert_classifies_ambiguous():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="PROCEED")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "AMBIGUOUS", rows[0])
        print("PASS proceed_with_no_insert_classifies_ambiguous")
    finally:
        _cleanup(tmp, conn)


# ---------- outcome classification: MODIFY ----------

def test_modify_with_linked_insert_classifies_accepted():
    tmp, conn = _fresh_db()
    try:
        # Cited node exists before the verdict.
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="MODIFY",
                        evidence_ids=[cited])
        # In-window insert WITH an edge to the cited node.
        new = _insert_node_at(conn, kind="progress",
                              created_at="2026-05-25 12:10:00")
        _add_edge_at(conn, src=new, dst=cited, relation="related_to",
                     created_at="2026-05-25 12:10:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "ACCEPTED", rows[0])
        print("PASS modify_with_linked_insert_classifies_accepted")
    finally:
        _cleanup(tmp, conn)


def test_modify_with_insert_but_no_link_classifies_overridden():
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="MODIFY",
                        evidence_ids=[cited])
        # Insert with NO edge to cited.
        _insert_node_at(conn, kind="progress",
                        created_at="2026-05-25 12:10:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "OVERRIDDEN", rows[0])
        print("PASS modify_with_insert_but_no_link_classifies_overridden")
    finally:
        _cleanup(tmp, conn)


def test_modify_with_no_insert_classifies_ambiguous():
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="MODIFY",
                        evidence_ids=[cited])
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "AMBIGUOUS", rows[0])
        print("PASS modify_with_no_insert_classifies_ambiguous")
    finally:
        _cleanup(tmp, conn)


# ---------- outcome classification: DO_NOT_PROCEED ----------

def test_do_not_proceed_with_no_progress_insert_classifies_accepted():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="DO_NOT_PROCEED")
        # A non-progress insert in window doesn't count as "ignored".
        _insert_node_at(conn, kind="fact",
                        created_at="2026-05-25 12:10:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "ACCEPTED", rows[0])
        print("PASS do_not_proceed_with_no_progress_insert_classifies_accepted")
    finally:
        _cleanup(tmp, conn)


def test_do_not_proceed_with_progress_insert_classifies_overridden():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="DO_NOT_PROCEED")
        _insert_node_at(conn, kind="progress",
                        created_at="2026-05-25 12:10:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "OVERRIDDEN", rows[0])
        print("PASS do_not_proceed_with_progress_insert_classifies_overridden")
    finally:
        _cleanup(tmp, conn)


# ---------- outcome classification: NEEDS_HUMAN_JUDGMENT ----------

def test_needs_human_judgment_with_activity_classifies_accepted():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="NEEDS_HUMAN_JUDGMENT")
        _insert_node_at(conn, kind="fact",
                        created_at="2026-05-25 12:10:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "ACCEPTED", rows[0])
        print("PASS needs_human_judgment_with_activity_classifies_accepted")
    finally:
        _cleanup(tmp, conn)


def test_needs_human_judgment_with_no_activity_classifies_unresolved():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="NEEDS_HUMAN_JUDGMENT")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "UNRESOLVED", rows[0])
        print("PASS needs_human_judgment_with_no_activity_classifies_unresolved")
    finally:
        _cleanup(tmp, conn)


# ---------- row-emission gating ----------

def test_skipped_gate_row_emits_no_outcome():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, skipped=True)
        counts = correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
        )
        _assert(counts["rows_skipped_skipped_verdict"] == 1, counts)
        _assert(counts["rows_emitted"] == 0, counts)
        _assert(_read_outcome_rows(tmp) == [], "no outcome row should exist")
        print("PASS skipped_gate_row_emits_no_outcome")
    finally:
        _cleanup(tmp, conn)


def test_gate_row_without_session_id_emits_no_outcome():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, session_id=None)
        counts = correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
        )
        _assert(counts["rows_skipped_no_session_id"] == 1, counts)
        _assert(counts["rows_emitted"] == 0, counts)
        _assert(_read_outcome_rows(tmp) == [], "no outcome row should exist")
        print("PASS gate_row_without_session_id_emits_no_outcome")
    finally:
        _cleanup(tmp, conn)


# ---------- cited_ids_touched (Gap D signal) ----------

def test_cited_ids_touched_independent_of_outcome():
    """cited_ids_touched counts via session_retrievals, separate from
    outcome classification. AMBIGUOUS outcome can coexist with a positive
    cited_touch count."""
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        # MODIFY with no inserts → AMBIGUOUS outcome
        _write_gate_row(tmp, recommendation="MODIFY",
                        evidence_ids=[cited])
        # But the cited node WAS touched in this session after t0.
        _bump_retrieval(conn, node_id=cited,
                        last_injected_at="2026-05-25 12:15:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["outcome_category"] == "AMBIGUOUS",
                f"expected AMBIGUOUS outcome: {rows[0]}")
        _assert(rows[0]["cited_ids_total"] == 1, rows[0])
        _assert(rows[0]["cited_ids_touched"] == 1,
                f"expected cited_touched=1 independently of outcome: {rows[0]}")
        print("PASS cited_ids_touched_independent_of_outcome")
    finally:
        _cleanup(tmp, conn)


# ---------- correction signals (id=1159) ----------

def test_correction_of_cited_node_sets_cited_corrected():
    """A cited node corrected in-window → cited_ids_corrected=1 and
    followup_count_corrections=1. The reward-attribution signal."""
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="PROCEED", evidence_ids=[cited])
        _write_correction_row(tmp, date_str="2026-05-25", hour=12, minute=15,
                              bad_node_id=cited)
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["followup_count_corrections"] == 1, rows[0])
        _assert(rows[0]["cited_ids_corrected"] == 1,
                f"cited node was corrected → expected 1: {rows[0]}")
        print("PASS correction_of_cited_node_sets_cited_corrected")
    finally:
        _cleanup(tmp, conn)


def test_correction_of_noncited_node_counts_but_not_cited_corrected():
    """A correction of a node NOT in the verdict's cited set still counts in
    followup_count_corrections but contributes 0 to cited_ids_corrected."""
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="PROCEED", evidence_ids=[cited])
        _write_correction_row(tmp, date_str="2026-05-25", hour=12, minute=15,
                              bad_node_id=999999)  # not cited
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["followup_count_corrections"] == 1, rows[0])
        _assert(rows[0]["cited_ids_corrected"] == 0,
                f"non-cited correction → cited_corrected should be 0: {rows[0]}")
        print("PASS correction_of_noncited_node_counts_but_not_cited_corrected")
    finally:
        _cleanup(tmp, conn)


def test_correction_outside_window_or_session_not_counted():
    """Corrections in a different session, or after the window closes, are
    not attributed to the verdict."""
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="PROCEED", evidence_ids=[cited])
        # Different session — must not count.
        _write_correction_row(tmp, date_str="2026-05-25", hour=12, minute=15,
                              bad_node_id=cited, session_id="some-other-sid")
        # Same session but well past the 30-min default window (t0=12:00).
        _write_correction_row(tmp, date_str="2026-05-25", hour=14, minute=0,
                              bad_node_id=cited)
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["followup_count_corrections"] == 0,
                f"out-of-session/out-of-window corrections must not count: {rows[0]}")
        _assert(rows[0]["cited_ids_corrected"] == 0, rows[0])
        print("PASS correction_outside_window_or_session_not_counted")
    finally:
        _cleanup(tmp, conn)


def test_no_corrections_yields_zero_signals():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="PROCEED")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        _assert(rows[0]["followup_count_corrections"] == 0, rows[0])
        _assert(rows[0]["cited_ids_corrected"] == 0, rows[0])
        print("PASS no_corrections_yields_zero_signals")
    finally:
        _cleanup(tmp, conn)


# ---------- window truncation ----------

def test_window_truncated_by_next_gate_in_session():
    """Two gates 10 minutes apart in the same session — the first's
    window must end at the second's ts, not at t0+1800s."""
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, hour=12, minute=0, query_hash="first00000a")
        _write_gate_row(tmp, hour=12, minute=10, query_hash="second0000b")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = sorted(_read_outcome_rows(tmp), key=lambda r: r["gate_ts"])
        _assert(len(rows) == 2, rows)
        _assert(rows[0]["window_seconds"] == 600,
                f"first window should be 600s (truncated by next gate): "
                f"{rows[0]['window_seconds']}")
        _assert(rows[1]["window_seconds"] == 1800,
                f"second window should be default 1800s: "
                f"{rows[1]['window_seconds']}")
        print("PASS window_truncated_by_next_gate_in_session")
    finally:
        _cleanup(tmp, conn)


# ---------- multi-day handling ----------

def test_correlator_walks_multi_day_range():
    """Two gate rows on different days are both correlated when the
    range covers both."""
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, date_str="2026-05-25", query_hash="day25xxxx111")
        _write_gate_row(tmp, date_str="2026-05-26", query_hash="day26xxxx222")
        counts = correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 26),
        )
        _assert(counts["rows_emitted"] == 2, counts)
        # Each day's outcome file should have its own row.
        d25 = _read_outcome_rows(tmp, "2026-05-25")
        d26 = _read_outcome_rows(tmp, "2026-05-26")
        _assert(len(d25) == 1 and len(d26) == 1,
                f"expected one row per day: d25={d25}, d26={d26}")
        print("PASS correlator_walks_multi_day_range")
    finally:
        _cleanup(tmp, conn)


# ---------- idempotency ----------

def test_correlator_is_idempotent_at_same_version():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="PROCEED")
        first = correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
        )
        _assert(first["rows_emitted"] == 1, first)
        second = correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
        )
        _assert(second["rows_emitted"] == 0, second)
        _assert(second["rows_skipped_dedup"] == 1, second)
        # Still only one outcome row.
        rows = _read_outcome_rows(tmp)
        _assert(len(rows) == 1, f"expected 1 row after re-run, got {len(rows)}")
        print("PASS correlator_is_idempotent_at_same_version")
    finally:
        _cleanup(tmp, conn)


def test_different_correlator_version_emits_fresh_row():
    tmp, conn = _fresh_db()
    try:
        _write_gate_row(tmp, recommendation="PROCEED")
        correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
            correlator_version="0.1.0",
        )
        correlator.correlate(
            tmp, date(2026, 5, 25), date(2026, 5, 25),
            correlator_version="0.2.0",
        )
        rows = _read_outcome_rows(tmp)
        _assert(len(rows) == 2, f"expected 2 rows under different versions: {rows}")
        versions = sorted(r["correlator_version"] for r in rows)
        _assert(versions == ["0.1.0", "0.2.0"], versions)
        print("PASS different_correlator_version_emits_fresh_row")
    finally:
        _cleanup(tmp, conn)


# ---------- privacy invariant ----------

def test_emitted_rows_contain_no_forbidden_fields():
    tmp, conn = _fresh_db()
    try:
        cited = _insert_node_at(conn, kind="fact",
                                title="Secret Cited Title",
                                body="Secret Cited Body",
                                created_at="2026-05-20 09:00:00",
                                session_id="other-session")
        _write_gate_row(tmp, recommendation="MODIFY",
                        evidence_ids=[cited])
        _insert_node_at(conn, kind="progress",
                        title="Secret New Title",
                        body="Secret New Body",
                        created_at="2026-05-25 12:10:00")
        correlator.correlate(tmp, date(2026, 5, 25), date(2026, 5, 25))
        rows = _read_outcome_rows(tmp)
        forbidden = {
            "title", "body", "node_title", "src_title", "dst_title",
            "query_text", "query_excerpt", "raw_request",
            "description", "reason",
        }
        for r in rows:
            leaks = set(r.keys()) & forbidden
            _assert(not leaks, f"forbidden fields leaked: {leaks} in {r}")
            # Make sure no value contains the planted secret strings.
            blob = json.dumps(r, default=str)
            for needle in ("Secret Cited Title", "Secret Cited Body",
                           "Secret New Title", "Secret New Body"):
                _assert(needle not in blob,
                        f"secret string {needle!r} leaked: {blob}")
        print("PASS emitted_rows_contain_no_forbidden_fields")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_proceed_with_progress_insert_classifies_accepted()
    test_proceed_with_no_insert_classifies_ambiguous()
    test_modify_with_linked_insert_classifies_accepted()
    test_modify_with_insert_but_no_link_classifies_overridden()
    test_modify_with_no_insert_classifies_ambiguous()
    test_do_not_proceed_with_no_progress_insert_classifies_accepted()
    test_do_not_proceed_with_progress_insert_classifies_overridden()
    test_needs_human_judgment_with_activity_classifies_accepted()
    test_needs_human_judgment_with_no_activity_classifies_unresolved()
    test_skipped_gate_row_emits_no_outcome()
    test_gate_row_without_session_id_emits_no_outcome()
    test_cited_ids_touched_independent_of_outcome()
    test_correction_of_cited_node_sets_cited_corrected()
    test_correction_of_noncited_node_counts_but_not_cited_corrected()
    test_correction_outside_window_or_session_not_counted()
    test_no_corrections_yields_zero_signals()
    test_window_truncated_by_next_gate_in_session()
    test_correlator_walks_multi_day_range()
    test_correlator_is_idempotent_at_same_version()
    test_different_correlator_version_emits_fresh_row()
    test_emitted_rows_contain_no_forbidden_fields()
    print("\nAll correlator tests pass.")
