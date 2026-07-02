"""Slice 3-B wiring — db marker, profiles gating/render, detection.log emit.

Covers the pieces that bolt the deterministic cite detector (test_cite_detector)
into the Stop -> UserPromptSubmit advisory-nudge flow (KB id=1436):
  * db: pending_cite_nudge migration + set/take (read-and-reset) semantics.
  * profiles: claim_backing_requires_code_trace gates ONLY mission control;
    render_cite_correction_directive shape.
  * capture_streams: detection.log structural-only row + closed-set action.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(_SRC / "hooks"))  # for the stop-hook integration test

import capture_streams  # noqa: E402
import db               # noqa: E402
import log_utils        # noqa: E402
import profiles         # noqa: E402
import stop             # noqa: E402  (src/hooks/stop.py)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_cite_")
    return tmp, db.connect(tmp)


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


DEFAULT_SID = "22222222-2222-2222-2222-222222222222"


# ---------- db marker ----------

def test_migration_adds_column():
    tmp, conn = _fresh_db()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        _assert("pending_cite_nudge" in cols, "pending_cite_nudge column missing")
        db._migrate_cite_nudge(conn)  # idempotent: second run must not raise
        print("PASS migration_adds_column")
    finally:
        _cleanup(tmp, conn)


def test_set_and_take_round_trip():
    tmp, conn = _fresh_db()
    try:
        db.upsert_session(conn, DEFAULT_SID, tmp, None)
        db.set_pending_cite_nudge(conn, DEFAULT_SID, 3)
        _assert(db.take_pending_cite_nudge(conn, DEFAULT_SID) == 3, "wrong count read")
        # consumed: second take is 0
        _assert(db.take_pending_cite_nudge(conn, DEFAULT_SID) == 0, "marker not reset")
        print("PASS set_and_take_round_trip")
    finally:
        _cleanup(tmp, conn)


def test_take_absent_session_is_zero():
    tmp, conn = _fresh_db()
    try:
        _assert(db.take_pending_cite_nudge(conn, "no-such-session") == 0,
                "absent session should be 0, not error")
        print("PASS take_absent_session_is_zero")
    finally:
        _cleanup(tmp, conn)


def test_fresh_session_defaults_zero():
    tmp, conn = _fresh_db()
    try:
        db.upsert_session(conn, DEFAULT_SID, tmp, None)
        _assert(db.take_pending_cite_nudge(conn, DEFAULT_SID) == 0,
                "new session should default to 0")
        print("PASS fresh_session_defaults_zero")
    finally:
        _cleanup(tmp, conn)


# ---------- profiles gating ----------

def test_gate_true_only_for_mission_control():
    tmp, conn = _fresh_db()
    try:
        # unbound actor -> False, and NO presets materialised (hot-path no-write)
        _assert(profiles.claim_backing_requires_code_trace(conn, "stranger") is False,
                "unbound actor must be False")
        n = conn.execute("SELECT COUNT(*) c FROM nodes WHERE kind='profile'").fetchone()["c"]
        _assert(n == 0, f"gate must not materialise presets for unbound actor (got {n})")

        profiles.bind_actor(conn, "dev-b", name="mission-control")
        _assert(profiles.claim_backing_requires_code_trace(conn, "dev-b") is True,
                "mission-control actor must be True")

        profiles.bind_actor(conn, "dev-a", name="trust-and-go")
        _assert(profiles.claim_backing_requires_code_trace(conn, "dev-a") is False,
                "trust-and-go actor must be False")
        print("PASS gate_true_only_for_mission_control")
    finally:
        _cleanup(tmp, conn)


def test_render_directive_shape():
    out = profiles.render_cite_correction_directive(2)
    _assert("file:line" in out, "directive must demand file:line")
    _assert("2" in out, "directive should mention the count")
    _assert("Cite-presence check" in out, "directive header missing")
    # count floors at 1 even if a bad 0 sneaks in (rendered as bold **1**)
    _assert("**1**" in profiles.render_cite_correction_directive(0), "count should floor at 1")
    _assert("conclusion " in profiles.render_cite_correction_directive(1), "singular for 1")
    print("PASS render_directive_shape")


# ---------- detection.log ----------

def _today():
    return datetime.now(timezone.utc).date()


def test_detection_row_structural_only():
    tmp = tempfile.mkdtemp(prefix="kb_det_")
    try:
        capture_streams.emit_detection_event(
            n_claims=4, n_flagged=2, action="nudge_queued",
            transcript_hash="deadbeef0000",
            project_path=tmp, session_id=DEFAULT_SID,
        )
        rows = list(log_utils.read_log_range("detection", _today(), _today(), tmp))
        _assert(len(rows) == 1, f"expected 1 detection row, got {len(rows)}")
        r = rows[0]
        expected = {
            "scanned", "n_claims", "n_flagged", "action", "transcript_hash",
            "ts", "project", "session_id", "event_type",
        }
        _assert(set(r.keys()) == expected,
                f"schema mismatch: {set(r.keys()) ^ expected}")
        _assert(r["n_flagged"] == 2 and r["n_claims"] == 4, "counts wrong")
        _assert(r["action"] == "nudge_queued", "action wrong")
        forbidden = {"claim", "text", "body", "prompt", "flagged", "title"}
        _assert(not (set(r.keys()) & forbidden), "forbidden content field leaked")
        print("PASS detection_row_structural_only")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- Stop-hook end-to-end (transcript -> scan -> marker + log) ----------

import json  # noqa: E402


def _write_transcript(tmp, assistant_text):
    """A minimal Claude Code-shaped JSONL transcript: one user line, one
    assistant line carrying typed text blocks."""
    tpath = Path(tmp) / "transcript.jsonl"
    rows = [
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "text", "text": "why is the fit off?"}]}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": assistant_text}]}},
    ]
    tpath.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(tpath)


def test_stop_scan_flags_for_mission_control():
    tmp, conn = _fresh_db()
    try:
        # Bind the resolved actor (db._ACTOR) to mission control in this temp db.
        profiles.bind_actor(conn, name="mission-control")
        db.upsert_session(conn, DEFAULT_SID, tmp, None)
        tpath = _write_transcript(tmp, "The clamp flag is set to false in the deployed config.")
        conn.close()  # the hook opens its own connection

        stop._cite_presence_check(DEFAULT_SID, tmp, tpath)

        conn = db.connect(tmp)
        _assert(db.take_pending_cite_nudge(conn, DEFAULT_SID) == 1,
                "mission-control uncited claim should queue a nudge")
        rows = list(log_utils.read_log_range("detection", _today(), _today(), tmp))
        _assert(len(rows) == 1 and rows[0]["action"] == "nudge_queued",
                f"detection row should record nudge_queued: {rows}")
        print("PASS stop_scan_flags_for_mission_control")
    finally:
        _cleanup(tmp, conn)


def test_stop_scan_clears_when_cited():
    tmp, conn = _fresh_db()
    try:
        profiles.bind_actor(conn, name="mission-control")
        db.upsert_session(conn, DEFAULT_SID, tmp, None)
        tpath = _write_transcript(tmp, "The clamp flag is set to false in `config.toml:42`.")
        conn.close()

        stop._cite_presence_check(DEFAULT_SID, tmp, tpath)

        conn = db.connect(tmp)
        _assert(db.take_pending_cite_nudge(conn, DEFAULT_SID) == 0,
                "a properly cited claim must not queue a nudge")
        rows = list(log_utils.read_log_range("detection", _today(), _today(), tmp))
        _assert(rows and rows[0]["action"] == "none" and rows[0]["n_claims"] == 1,
                f"detection row should be scanned/none with 1 claim: {rows}")
        print("PASS stop_scan_clears_when_cited")
    finally:
        _cleanup(tmp, conn)


def test_stop_scan_noop_for_unbound_actor():
    tmp, conn = _fresh_db()
    try:
        # No binding for db._ACTOR -> trust-and-go default -> byte-identical no-op.
        db.upsert_session(conn, DEFAULT_SID, tmp, None)
        tpath = _write_transcript(tmp, "The clamp flag is set to false everywhere.")
        conn.close()

        stop._cite_presence_check(DEFAULT_SID, tmp, tpath)

        conn = db.connect(tmp)
        _assert(db.take_pending_cite_nudge(conn, DEFAULT_SID) == 0,
                "unbound actor must not be scanned / nudged")
        rows = list(log_utils.read_log_range("detection", _today(), _today(), tmp))
        _assert(rows == [], f"unbound actor must emit NO detection rows: {rows}")
        print("PASS stop_scan_noop_for_unbound_actor")
    finally:
        _cleanup(tmp, conn)


def test_detection_closed_set():
    _assert(set(capture_streams.DETECTION_ACTIONS) == {"none", "nudge_queued"},
            "DETECTION_ACTIONS drift")
    _assert(capture_streams.DETECTION_STREAM == "detection", "stream name drift")
    print("PASS detection_closed_set")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} CITE-NUDGE-FLOW TESTS PASSED")
