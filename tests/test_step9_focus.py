"""Step 9 §4.3 focus mechanics — auto-bump, decay, get_focus, pin, prune, CLI.

Exercises:
- bump_focus inserts new row, increments existing
- decay reduces effective score over time but bump_focus rehydrates
- non-workstream node ids are silently ignored
- bump_focus does NOT auto-evict (lazy: storage keeps all touched workstreams)
- prune_focus keeps top-N + all pinned, deletes the rest
- pinned rows survive prune even with low scores
- get_focus returns workstream fields, limit=N, sorted pinned-first then score-desc
- set_focus boosts above default delta
- pin / unpin / drop semantics
- bump_focus_for_nodes resolves leaf -> workstream and dedupes
- kb_focus_cli list / set / pin / unpin / drop / prune end-to-end
- SessionStart brief reads focus when populated, falls back when empty
"""
from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "hooks"))

import db  # noqa: E402
import kb_focus_cli  # noqa: E402
import session_start as ss  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_step9_focus_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _backdate_focus(conn, workstream_id: int, hours_ago: float) -> None:
    """Move set_at backwards so decay tests don't have to sleep."""
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    conn.execute(
        "UPDATE focus SET set_at = ? WHERE workstream_id = ?",
        (ts.strftime("%Y-%m-%d %H:%M:%S"), workstream_id),
    )
    conn.commit()


# ---------- bump_focus core ----------

def test_bump_focus_inserts_new_row():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.bump_focus(conn, ws)
        row = conn.execute("SELECT score, set_by, pinned FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is not None, "focus row not created")
        _assert(abs(row["score"] - db.FOCUS_DEFAULT_DELTA) < 1e-6,
                f"score={row['score']}, expected {db.FOCUS_DEFAULT_DELTA}")
        _assert(row["set_by"] == "auto", f"set_by={row['set_by']}")
        _assert(row["pinned"] == 0, "default pinned should be 0")
        print("PASS bump_focus_inserts_new_row")
    finally:
        _cleanup(tmp, conn)


def test_bump_focus_accumulates_on_existing_row():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.bump_focus(conn, ws)
        db.bump_focus(conn, ws)
        db.bump_focus(conn, ws)
        row = conn.execute("SELECT score FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        # Three bumps at delta=1, with negligible decay between them. Allow a
        # tiny floor for any measurable decay drift across the operations.
        _assert(2.95 <= row["score"] <= 3.0, f"3 bumps -> score={row['score']}, expected ~3")
        print("PASS bump_focus_accumulates_on_existing_row")
    finally:
        _cleanup(tmp, conn)


def test_bump_focus_silently_ignores_non_workstream_node():
    tmp, conn = _fresh_db()
    try:
        leaf = db.insert_node(conn, kind="fact", title="F", body="...")
        db.bump_focus(conn, leaf)
        rows = conn.execute("SELECT COUNT(*) c FROM focus").fetchone()
        _assert(rows["c"] == 0, f"leaf node id should not create focus row, got {rows['c']}")
        print("PASS bump_focus_silently_ignores_non_workstream_node")
    finally:
        _cleanup(tmp, conn)


def test_bump_focus_silently_ignores_none_and_missing():
    tmp, conn = _fresh_db()
    try:
        db.bump_focus(conn, None)
        db.bump_focus(conn, 99999)  # missing id
        rows = conn.execute("SELECT COUNT(*) c FROM focus").fetchone()
        _assert(rows["c"] == 0, "None / missing id must not write")
        print("PASS bump_focus_silently_ignores_none_and_missing")
    finally:
        _cleanup(tmp, conn)


# ---------- decay ----------

def test_decay_reduces_effective_score_over_time():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.bump_focus(conn, ws)
        _backdate_focus(conn, ws, hours_ago=10.0)
        rows = db.get_focus(conn)
        _assert(len(rows) == 1, "expected 1 row")
        eff = rows[0]["effective_score"]
        # 1.0 * 0.95^10 ~ 0.5987
        _assert(0.55 < eff < 0.65, f"decay 10h on score=1.0 -> {eff}, expected ~0.6")
        print("PASS decay_reduces_effective_score_over_time")
    finally:
        _cleanup(tmp, conn)


def test_bump_after_decay_rehydrates_score():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.bump_focus(conn, ws)
        _backdate_focus(conn, ws, hours_ago=10.0)
        # Bump again — stored score should be decayed-then-incremented and
        # set_at reset to now, so future decay starts fresh.
        db.bump_focus(conn, ws)
        row = conn.execute("SELECT score, set_at FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        # ~0.5987 + 1.0 = ~1.5987
        _assert(1.55 < row["score"] < 1.65, f"rehydrate -> stored={row['score']}, expected ~1.6")
        print("PASS bump_after_decay_rehydrates_score")
    finally:
        _cleanup(tmp, conn)


# ---------- bump_focus_for_nodes ----------

def test_bump_focus_for_nodes_resolves_leaf_to_workstream():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        leaf = db.insert_node(conn, kind="fact", title="F", body="f", workstream_id=ws)
        db.bump_focus_for_nodes(conn, [leaf])
        row = conn.execute("SELECT score FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is not None and row["score"] > 0,
                f"leaf -> workstream resolution failed: {row}")
        print("PASS bump_focus_for_nodes_resolves_leaf_to_workstream")
    finally:
        _cleanup(tmp, conn)


def test_bump_focus_for_nodes_dedupes_workstream_pile_on():
    """Five leaves under one workstream = one bump, not five."""
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        leaves = [
            db.insert_node(conn, kind="fact", title=f"F{i}", body=f"f{i}", workstream_id=ws)
            for i in range(5)
        ]
        db.bump_focus_for_nodes(conn, leaves)
        row = conn.execute("SELECT score FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(abs(row["score"] - db.FOCUS_DEFAULT_DELTA) < 0.05,
                f"5 leaves of same WS should bump once, got score={row['score']}")
        print("PASS bump_focus_for_nodes_dedupes_workstream_pile_on")
    finally:
        _cleanup(tmp, conn)


def test_bump_focus_for_nodes_workstream_self_bump():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.bump_focus_for_nodes(conn, [ws])
        row = conn.execute("SELECT score FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is not None and row["score"] > 0,
                "kb_get on a workstream should bump its own focus")
        print("PASS bump_focus_for_nodes_workstream_self_bump")
    finally:
        _cleanup(tmp, conn)


# ---------- bump does not auto-evict (lazy storage) ----------

def test_bump_focus_does_not_auto_evict():
    """A fresh workstream that doesn't yet outscore stale ones must survive
    long enough on the table to climb. Auto-evict-on-bump used to kick out
    the just-inserted row and starve mid-session focus shifts."""
    tmp, conn = _fresh_db()
    try:
        wss = [db.insert_node(conn, kind="workstream", title=f"WS{i}", body="...") for i in range(5)]
        for i, ws in enumerate(wss):
            for _ in range(i + 1):
                db.bump_focus(conn, ws)
        rows = conn.execute("SELECT workstream_id FROM focus").fetchall()
        ids = {r["workstream_id"] for r in rows}
        _assert(ids == set(wss),
                f"all 5 workstreams should remain (lazy eviction), got {ids}")
        print("PASS bump_focus_does_not_auto_evict")
    finally:
        _cleanup(tmp, conn)


# ---------- prune (explicit) ----------

def test_prune_focus_keeps_top_cap_unpinned():
    tmp, conn = _fresh_db()
    try:
        wss = [db.insert_node(conn, kind="workstream", title=f"WS{i}", body="...") for i in range(5)]
        for i, ws in enumerate(wss):
            for _ in range(i + 1):
                db.bump_focus(conn, ws)
        # Pre-prune: 5 rows. After prune: top-3 by score.
        n = db.prune_focus(conn)
        rows = conn.execute(
            "SELECT workstream_id FROM focus ORDER BY score DESC"
        ).fetchall()
        ids = {r["workstream_id"] for r in rows}
        _assert(len(ids) == db.FOCUS_CAP,
                f"prune left {len(ids)} rows, expected {db.FOCUS_CAP}")
        _assert(ids == {wss[4], wss[3], wss[2]},
                f"survivors should be top-3 by score, got {ids}")
        _assert(n == 2, f"prune deleted {n}, expected 2")
        print("PASS prune_focus_keeps_top_cap_unpinned")
    finally:
        _cleanup(tmp, conn)


def test_prune_focus_preserves_pinned_low_scores():
    tmp, conn = _fresh_db()
    try:
        wss = [db.insert_node(conn, kind="workstream", title=f"WS{i}", body="...") for i in range(5)]
        # Pin the lowest-score one (score=0) and bump everyone else.
        db.pin_focus(conn, wss[0])
        for i, ws in enumerate(wss[1:], start=1):
            for _ in range(i + 1):
                db.bump_focus(conn, ws)
        db.prune_focus(conn)
        rows = conn.execute("SELECT workstream_id, pinned FROM focus").fetchall()
        ids = {r["workstream_id"]: r["pinned"] for r in rows}
        _assert(wss[0] in ids and ids[wss[0]] == 1, f"pinned WS0 was pruned: {ids}")
        # Pinned + cap=3 auto = 4 total.
        _assert(len(ids) == db.FOCUS_CAP + 1, f"expected 4 survivors, got {ids}")
        print("PASS prune_focus_preserves_pinned_low_scores")
    finally:
        _cleanup(tmp, conn)


def test_prune_focus_idempotent_under_cap():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="solo", body="...")
        db.bump_focus(conn, ws)
        n1 = db.prune_focus(conn)
        n2 = db.prune_focus(conn)
        _assert(n1 == 0 and n2 == 0, f"under-cap prune should delete 0, got {n1},{n2}")
        print("PASS prune_focus_idempotent_under_cap")
    finally:
        _cleanup(tmp, conn)


# ---------- get_focus ----------

def test_get_focus_orders_pinned_first_then_score():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="workstream", title="A", body="...")
        b = db.insert_node(conn, kind="workstream", title="B", body="...")
        c = db.insert_node(conn, kind="workstream", title="C", body="...")
        # B has highest score. C is pinned with low score.
        db.bump_focus(conn, a)
        for _ in range(5):
            db.bump_focus(conn, b)
        db.pin_focus(conn, c)
        rows = db.get_focus(conn, limit=10)
        ids = [r["workstream_id"] for r in rows]
        _assert(ids[0] == c, f"pinned should sort first, got {ids}")
        _assert(ids.index(b) < ids.index(a), f"B should outrank A by score, got {ids}")
        print("PASS get_focus_orders_pinned_first_then_score")
    finally:
        _cleanup(tmp, conn)


def test_get_focus_skips_stale_workstreams():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.bump_focus(conn, ws)
        db.update_node(conn, ws, status="stale")
        rows = db.get_focus(conn)
        _assert(rows == [], f"stale workstream should be filtered, got {rows}")
        print("PASS get_focus_skips_stale_workstreams")
    finally:
        _cleanup(tmp, conn)


# ---------- set / pin / unpin / drop ----------

def test_set_focus_boosts_above_default_delta():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="workstream", title="A", body="...")
        b = db.insert_node(conn, kind="workstream", title="B", body="...")
        for _ in range(3):
            db.bump_focus(conn, a)  # auto bumps -> score ~3
        db.set_focus(conn, b)        # user set -> score ~5
        rows = db.get_focus(conn)
        _assert(rows[0]["workstream_id"] == b,
                f"set_focus should land at top, got {[r['workstream_id'] for r in rows]}")
        print("PASS set_focus_boosts_above_default_delta")
    finally:
        _cleanup(tmp, conn)


def test_pin_focus_creates_row_when_absent():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        ok = db.pin_focus(conn, ws)
        _assert(ok, "pin_focus should succeed on a workstream")
        row = conn.execute("SELECT pinned FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is not None and row["pinned"] == 1, f"pinned not set: {row}")
        print("PASS pin_focus_creates_row_when_absent")
    finally:
        _cleanup(tmp, conn)


def test_pin_focus_rejects_non_workstream():
    tmp, conn = _fresh_db()
    try:
        leaf = db.insert_node(conn, kind="fact", title="F", body="...")
        ok = db.pin_focus(conn, leaf)
        _assert(not ok, "pin_focus must reject non-workstream nodes")
        print("PASS pin_focus_rejects_non_workstream")
    finally:
        _cleanup(tmp, conn)


def test_unpin_focus_clears_flag_keeps_row():
    """Unpin no longer auto-evicts (storage is lazy). Row stays; callers can drop or prune."""
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.pin_focus(conn, ws)
        db.unpin_focus(conn, ws)
        row = conn.execute("SELECT pinned FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is not None and row["pinned"] == 0,
                f"unpin should keep row with pinned=0, got {dict(row) if row else None}")
        print("PASS unpin_focus_clears_flag_keeps_row")
    finally:
        _cleanup(tmp, conn)


def test_drop_focus_removes_row():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        db.set_focus(conn, ws)
        db.drop_focus(conn, ws)
        row = conn.execute("SELECT * FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is None, "drop_focus should remove row")
        print("PASS drop_focus_removes_row")
    finally:
        _cleanup(tmp, conn)


# ---------- _resolve_workstream_id ----------

def test_resolve_workstream_id_for_workstream_returns_self():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        _assert(db._resolve_workstream_id(conn, ws) == ws,
                "workstream node should resolve to itself")
        print("PASS resolve_workstream_id_for_workstream_returns_self")
    finally:
        _cleanup(tmp, conn)


def test_resolve_workstream_id_for_leaf_returns_workstream_column():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS", body="...")
        leaf = db.insert_node(conn, kind="fact", title="F", body="...", workstream_id=ws)
        _assert(db._resolve_workstream_id(conn, leaf) == ws,
                "leaf with workstream_id should resolve")
        orphan = db.insert_node(conn, kind="fact", title="O", body="...")
        _assert(db._resolve_workstream_id(conn, orphan) is None,
                "orphan leaf should resolve to None")
        print("PASS resolve_workstream_id_for_leaf_returns_workstream_column")
    finally:
        _cleanup(tmp, conn)


# ---------- CLI ----------

def _run_cli(cwd, *args):
    """Capture stdout from kb_focus_cli.main and parse JSON."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kb_focus_cli.main(["kb_focus_cli.py", cwd, *args])
    out = buf.getvalue().strip()
    return rc, json.loads(out) if out else {}


def test_cli_list_returns_focus_rows():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="WS list", body="...")
        db.set_focus(conn, ws)
        conn.close()
        rc, out = _run_cli(tmp, "list")
        _assert(rc == 0 and out["ok"], f"cli list failed: {out}")
        _assert(any(f["workstream_id"] == ws for f in out["focus"]),
                f"WS missing from cli list: {out}")
        print("PASS cli_list_returns_focus_rows")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_set_pin_unpin_drop_full_cycle():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="cycle", body="...")
        conn.close()

        rc, out = _run_cli(tmp, "set", str(ws))
        _assert(out["ok"] and out["action"] == "set", f"set: {out}")

        rc, out = _run_cli(tmp, "pin", str(ws))
        _assert(out["ok"] and out["action"] == "pinned", f"pin: {out}")

        # Verify pinned=1 in DB.
        c2 = db.connect(tmp)
        row = c2.execute("SELECT pinned FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row["pinned"] == 1, f"pin didn't stick: {dict(row)}")
        c2.close()

        rc, out = _run_cli(tmp, "unpin", str(ws))
        _assert(out["ok"] and out["action"] == "unpinned", f"unpin: {out}")

        rc, out = _run_cli(tmp, "drop", str(ws))
        _assert(out["ok"] and out["action"] == "dropped", f"drop: {out}")

        c2 = db.connect(tmp)
        row = c2.execute("SELECT * FROM focus WHERE workstream_id = ?", (ws,)).fetchone()
        _assert(row is None, "drop should empty the focus table")
        c2.close()
        print("PASS cli_set_pin_unpin_drop_full_cycle")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_rejects_non_workstream_node():
    tmp, conn = _fresh_db()
    try:
        leaf = db.insert_node(conn, kind="fact", title="leaf", body="...")
        conn.close()
        rc, out = _run_cli(tmp, "set", str(leaf))
        _assert(not out["ok"] and "workstream" in out["error"],
                f"set on leaf should error, got {out}")
        print("PASS cli_rejects_non_workstream_node")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_unknown_subcommand_returns_error():
    tmp, conn = _fresh_db()
    try:
        conn.close()
        rc, out = _run_cli(tmp, "doot", "1")
        _assert(not out["ok"] and "unknown" in out["error"], f"unknown sub: {out}")
        print("PASS cli_unknown_subcommand_returns_error")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cli_prune_runs_and_reports_deleted():
    tmp, conn = _fresh_db()
    try:
        wss = [db.insert_node(conn, kind="workstream", title=f"WS{i}", body="...") for i in range(5)]
        for i, ws in enumerate(wss):
            for _ in range(i + 1):
                db.bump_focus(conn, ws)
        conn.close()
        rc, out = _run_cli(tmp, "prune")
        _assert(out["ok"] and out["action"] == "pruned" and out["deleted"] == 2,
                f"prune output unexpected: {out}")
        _assert(len(out["focus"]) == db.FOCUS_CAP,
                f"prune left wrong count: {out}")
        print("PASS cli_prune_runs_and_reports_deleted")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- SessionStart brief integration ----------

def test_session_brief_uses_focus_when_populated():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="workstream", title="aaa-not-focused",
                           body="aaa", status="canonical")
        b = db.insert_node(conn, kind="workstream", title="bbb-focused",
                           body="bbb", status="canonical")
        for _ in range(5):
            db.bump_focus(conn, b)  # b dominates focus
        conn.close()

        out = ss._build_briefing(tmp, orphan_count=0, budget_line=None,
                                 surfaced_ids=[])
        _assert("Focus (active workstreams)" in out,
                f"focused brief missing focus header: {out!r}")
        _assert("bbb-focused" in out, f"focused workstream not in brief: {out!r}")
        _assert("aaa-not-focused" not in out,
                f"non-focused WS leaked into focused brief: {out!r}")
        print("PASS session_brief_uses_focus_when_populated")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_session_brief_falls_back_to_recent_when_focus_empty():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="recent-only",
                            body="r", status="canonical")
        conn.close()
        out = ss._build_briefing(tmp, orphan_count=0, budget_line=None,
                                 surfaced_ids=[])
        _assert("Active workstreams" in out,
                f"empty-focus brief should use legacy header: {out!r}")
        _assert("recent-only" in out, f"recent WS missing from fallback: {out!r}")
        print("PASS session_brief_falls_back_to_recent_when_focus_empty")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_session_brief_marks_pinned_workstreams():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(conn, kind="workstream", title="pin-me",
                            body="...", status="canonical")
        db.pin_focus(conn, ws)
        conn.close()
        out = ss._build_briefing(tmp, orphan_count=0, budget_line=None,
                                 surfaced_ids=[])
        _assert("(pinned)" in out, f"pinned marker missing: {out!r}")
        print("PASS session_brief_marks_pinned_workstreams")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_bump_focus_inserts_new_row()
    test_bump_focus_accumulates_on_existing_row()
    test_bump_focus_silently_ignores_non_workstream_node()
    test_bump_focus_silently_ignores_none_and_missing()
    test_decay_reduces_effective_score_over_time()
    test_bump_after_decay_rehydrates_score()
    test_bump_focus_for_nodes_resolves_leaf_to_workstream()
    test_bump_focus_for_nodes_dedupes_workstream_pile_on()
    test_bump_focus_for_nodes_workstream_self_bump()
    test_bump_focus_does_not_auto_evict()
    test_prune_focus_keeps_top_cap_unpinned()
    test_prune_focus_preserves_pinned_low_scores()
    test_prune_focus_idempotent_under_cap()
    test_get_focus_orders_pinned_first_then_score()
    test_get_focus_skips_stale_workstreams()
    test_set_focus_boosts_above_default_delta()
    test_pin_focus_creates_row_when_absent()
    test_pin_focus_rejects_non_workstream()
    test_unpin_focus_clears_flag_keeps_row()
    test_drop_focus_removes_row()
    test_resolve_workstream_id_for_workstream_returns_self()
    test_resolve_workstream_id_for_leaf_returns_workstream_column()
    test_cli_list_returns_focus_rows()
    test_cli_set_pin_unpin_drop_full_cycle()
    test_cli_rejects_non_workstream_node()
    test_cli_unknown_subcommand_returns_error()
    test_cli_prune_runs_and_reports_deleted()
    test_session_brief_uses_focus_when_populated()
    test_session_brief_falls_back_to_recent_when_focus_empty()
    test_session_brief_marks_pinned_workstreams()
    print("\nAll step9 focus tests pass.")
