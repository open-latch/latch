"""Slice 3-A: profile-selected adversary mode (assumption-hunter).

Covers gate.build_adversary_prompt mode selection (default byte-identical to the
shipped counter-node reviewer — the regression the Slice-3 gate verdict flagged),
profiles.active_adversary_mode resolution, and capture_streams mode tagging.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import capture_streams  # noqa: E402
import db               # noqa: E402
import gate             # noqa: E402
import log_utils        # noqa: E402
import profiles         # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_advmode_")
    return tmp, db.connect(tmp)


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


_CA = {"query": "do X", "seeds": [], "chains": [], "evidence_node_ids": []}
_V = {"recommendation": "PROCEED", "summary": "ok", "evidence_nodes": []}


# ---------- adversary prompt selection ----------

def test_default_prompt_is_counter_node():
    p = gate.build_adversary_prompt(_CA, _V)
    _assert("ATTACK THE PLAN" in p, "default should be the counter-node reviewer")
    _assert("ASSUMPTION-HUNTER" not in p, "default must not be assumption-hunter")
    print("PASS default_prompt_is_counter_node")


def test_default_equals_explicit_counter_node():
    _assert(gate.build_adversary_prompt(_CA, _V)
            == gate.build_adversary_prompt(_CA, _V, mode="counter_node"),
            "default mode must equal explicit counter_node (no regression)")
    print("PASS default_equals_explicit_counter_node")


def test_assumption_hunter_prompt_selected():
    p = gate.build_adversary_prompt(_CA, _V, mode="assumption_hunter")
    _assert("ASSUMPTION-HUNTER" in p, "assumption_hunter mode not selected")
    _assert("unverified assumption" in p.lower(), "assumption-hunter framing missing")
    print("PASS assumption_hunter_prompt_selected")


def test_unknown_mode_falls_back_to_counter_node():
    _assert(gate.build_adversary_prompt(_CA, _V, mode="bogus")
            == gate.build_adversary_prompt(_CA, _V, mode="counter_node"),
            "unknown mode must fall back to counter_node")
    print("PASS unknown_mode_falls_back_to_counter_node")


# ---------- profile -> mode resolution ----------

def test_unbound_actor_is_counter_node_no_writes():
    tmp, conn = _fresh_db()
    try:
        _assert(profiles.active_adversary_mode(conn, "dev-a") == "counter_node",
                "unbound actor must resolve to counter_node")
        n = conn.execute(
            "SELECT COUNT(*) c FROM nodes WHERE kind='profile'"
        ).fetchone()["c"]
        _assert(n == 0, f"resolution must not materialise presets for unbound (got {n})")
        print("PASS unbound_actor_is_counter_node_no_writes")
    finally:
        _cleanup(tmp, conn)


def test_mission_control_actor_is_assumption_hunter():
    tmp, conn = _fresh_db()
    try:
        profiles.bind_actor(conn, "dev-b", name="mission-control")
        _assert(profiles.active_adversary_mode(conn, "dev-b") == "assumption_hunter",
                "mission-control actor should select assumption_hunter")
        print("PASS mission_control_actor_is_assumption_hunter")
    finally:
        _cleanup(tmp, conn)


def test_trust_and_go_actor_is_counter_node():
    tmp, conn = _fresh_db()
    try:
        profiles.bind_actor(conn, "dev-a", name="trust-and-go")
        _assert(profiles.active_adversary_mode(conn, "dev-a") == "counter_node",
                "trust-and-go actor should select counter_node")
        print("PASS trust_and_go_actor_is_counter_node")
    finally:
        _cleanup(tmp, conn)


# ---------- adversary.log mode tagging (the comparison data, id=1428) ----------

def test_adversary_log_carries_mode():
    tmp = tempfile.mkdtemp(prefix="kb_advmode_log_")
    try:
        capture_streams.emit_adversary_event(
            verdict_before="PROCEED", verdict_delta="none", counter_node_id=None,
            n_forks_raised=0, latency_ms=1, query_hash="h",
            mode="assumption_hunter", project_path=tmp,
        )
        d = datetime.now(timezone.utc).date()
        rows = list(log_utils.read_log_range("adversary", d, d, tmp))
        _assert(rows and rows[0]["mode"] == "assumption_hunter",
                f"mode not logged: {rows}")
        print("PASS adversary_log_carries_mode")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} ADVERSARY-MODE TESTS PASSED")
