"""Tests for src/codex_attribution.py — recovering gate-call session attribution
from Codex's own rollout transcripts.

Context: Codex-hosted gate calls carry no session_id (KB id=4018), so they are
unlabelable and every measured number silently becomes single-host. The rollout
records the gate call itself, so a content join recovers the real thread id.

The tests that matter most here are the ones asserting the join DECLINES. An
ambiguous match must resolve to nothing rather than to a guess: confident wrong
attribution is worse than honest absence (canonical id=1716, Cursor precedent
id=1493 -> id=1525).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import codex_attribution  # noqa: E402
import correlator         # noqa: E402
import db                 # noqa: E402
import gate               # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


THREAD_A = "019fb2f6-1388-7980-8e6d-5e9f4c96f1e2"
THREAD_B = "019fae1c-f3ae-77f3-b8c1-1a9b7348bb36"


def _make_home(threads: dict[str, list[tuple[str, str, str | None]]]) -> str:
    """Build a throwaway CODEX_HOME. `threads` maps thread id ->
    [(iso_ts, request, gate_call_id_or_None), ...]."""
    tmp = tempfile.mkdtemp(prefix="codex_attr_test_")
    day = Path(tmp) / "sessions" / "2026" / "07" / "30"
    day.mkdir(parents=True)
    for thread, calls in threads.items():
        p = day / f"rollout-2026-07-30T05-18-19-{thread}.jsonl"
        lines = [json.dumps({
            "timestamp": "2026-07-30T05:18:19.000Z",
            "type": "session_meta",
            "payload": {"id": thread, "cwd": "/repo"},
        })]
        for i, (ts, request, nonce) in enumerate(calls):
            call_id = f"call_{thread[:6]}_{i}"
            lines.append(json.dumps({
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "latch_gate",
                    "call_id": call_id,
                    "arguments": json.dumps({"request": request}),
                },
            }))
            output = {"request": request, "gate_status": "OK"}
            if nonce:
                output["gate_call_id"] = nonce
            lines.append(json.dumps({
                "timestamp": ts,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": [{"type": "input_text",
                                "text": json.dumps(output)}],
                },
            }))
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp


def _gate_row(request: str, ts: str, nonce: str | None = None) -> dict:
    row = {
        "query_hash": codex_attribution.query_hash(request),
        "ts": ts,
        "session_id": None,
    }
    if nonce:
        row["gate_call_id"] = nonce
    return row


# ---------- the join key agrees with the gate's own ----------

def test_query_hash_matches_the_gate_implementation():
    """Both sides of the join must hash identically or nothing ever matches."""
    for request in ("add a redis queue", "  padded  ", "", "unicode — dash"):
        _assert(codex_attribution.query_hash(request) == gate._query_hash(request),
                f"hash disagreement for {request!r}")
    print("PASS query_hash_matches_the_gate_implementation")


# ---------- exact nonce join ----------

def test_nonce_join_is_exact_and_beats_an_ambiguous_hash():
    """Two threads issue the SAME request text, so the hash is ambiguous — but
    one carries the nonce, which identifies it outright."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "same request", "nonce-aaa")],
        THREAD_B: [("2026-07-30T05:20:05.000Z", "same request", "nonce-bbb")],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("same request", "2026-07-30T05:20:00.000Z", "nonce-bbb"), idx)
        _assert(hit is not None, "nonce should resolve despite hash ambiguity")
        _assert(hit["session_id"] == THREAD_B, hit)
        _assert(hit["source"] == "codex_transcript_nonce", hit)
        _assert(hit["transcript_path"].endswith(".jsonl"), hit)
        print("PASS nonce_join_is_exact_and_beats_an_ambiguous_hash")
    finally:
        shutil.rmtree(home, ignore_errors=True)


# ---------- hash join, and the cases it must refuse ----------

def test_hash_join_attributes_a_unique_match():
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "unique request", None)],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("unique request", "2026-07-30T05:20:03.000Z"), idx)
        _assert(hit is not None, "a unique hash match should attribute")
        _assert(hit["session_id"] == THREAD_A, hit)
        _assert(hit["source"] == "codex_transcript_hash", hit)
        print("PASS hash_join_attributes_a_unique_match")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_hash_join_declines_when_two_threads_share_the_request():
    """The load-bearing refusal. Guessing here would corrupt the measurement
    with confident wrong attribution."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "same request", None)],
        THREAD_B: [("2026-07-30T05:20:04.000Z", "same request", None)],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("same request", "2026-07-30T05:20:02.000Z"), idx)
        _assert(hit is None, f"ambiguous thread match must decline, got {hit}")
        print("PASS hash_join_declines_when_two_threads_share_the_request")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_hash_join_still_attributes_one_thread_repeating_itself():
    """A retry within one thread is NOT ambiguous about which thread it was —
    the MODIFY-then-retry flow must still be attributable."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "retried request", None),
                   ("2026-07-30T05:22:00.000Z", "retried request", None)],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("retried request", "2026-07-30T05:22:01.000Z"), idx)
        _assert(hit is not None, "same-thread repetition should still attribute")
        _assert(hit["session_id"] == THREAD_A, hit)
        print("PASS hash_join_still_attributes_one_thread_repeating_itself")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_hash_join_declines_outside_the_time_tolerance():
    """Same text on a different day is a different call."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "old request", None)],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("old request", "2026-07-30T23:59:00.000Z"), idx)
        _assert(hit is None, f"out-of-window match must decline, got {hit}")
        print("PASS hash_join_declines_outside_the_time_tolerance")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_declines_when_no_rollout_records_the_call():
    home = _make_home({THREAD_A: []})
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("never recorded", "2026-07-30T05:20:00.000Z"), idx)
        _assert(hit is None, f"unrecorded call must decline, got {hit}")
        print("PASS declines_when_no_rollout_records_the_call")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_declines_on_unparseable_gate_timestamp():
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "a request", None)],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("a request", "not-a-timestamp"), idx)
        _assert(hit is None, f"unparseable ts must decline, got {hit}")
        print("PASS declines_on_unparseable_gate_timestamp")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_missing_codex_home_yields_an_empty_index():
    idx = codex_attribution.build_index(Path("/nonexistent-codex-home"))
    _assert(idx == {"by_nonce": {}, "by_hash": {}}, idx)
    print("PASS missing_codex_home_yields_an_empty_index")


# ---------- the nonce has to reach the transcript at all ----------

def test_gate_returns_gate_call_id_so_future_rows_join_exactly():
    """Without this the nonce never reaches the host's transcript and every
    recovery is stuck on the weaker hash join."""
    tmp = tempfile.mkdtemp(prefix="codex_attr_gate_")
    conn = db.connect(tmp)
    try:
        out = gate.run_gate(conn, "a request", project_path=tmp,
                            session_id="sid-1", use_llm=False)
        _assert("gate_call_id" in out, f"gate must return the nonce: {out.keys()}")
        _assert(isinstance(out["gate_call_id"], str) and out["gate_call_id"],
                f"nonce should be a non-empty string: {out['gate_call_id']!r}")
        print("PASS gate_returns_gate_call_id_so_future_rows_join_exactly")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- recovered attribution must reach the shipped-diff signal ----------

def test_file_touches_accepts_a_supplied_transcript_path():
    """Most Codex threads have no `sessions` row, so a DB lookup would find
    nothing. Attribution already knows the transcript; it must be usable."""
    tmp = tempfile.mkdtemp(prefix="codex_attr_touch_")
    conn = db.connect(tmp)
    try:
        repo = Path(tmp) / "repo"
        (repo / ".git").mkdir(parents=True)
        t = Path(tmp) / "rollout.jsonl"
        t.write_text(json.dumps({
            "timestamp": "2026-07-30T05:20:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "c1",
                "name": "apply_patch",
                "input": f"*** Begin Patch\n*** Update File: {repo / 'x.py'}\n@@\n-a\n+b\n",
            },
        }) + "\n", encoding="utf-8")

        t0 = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)
        t_end = datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc)

        without = correlator._count_file_touches(
            conn, "thread-with-no-sessions-row", str(repo), t0, t_end)
        _assert(without == 0, f"no sessions row means no signal today: {without}")

        with_path = correlator._count_file_touches(
            conn, "thread-with-no-sessions-row", str(repo), t0, t_end,
            transcript_path=str(t))
        _assert(with_path == 1,
                f"supplied transcript should yield the edit: {with_path}")
        print("PASS file_touches_accepts_a_supplied_transcript_path")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_coverage_splits_labeled_rows_by_identity_source():
    """A blended coverage number hides that some identity was recovered rather
    than host-supplied. The split must be reportable."""
    import gate_report
    gates = [
        {"session_id": "a", "ts": "2026-07-30T05:00:00.000Z"},
        {"session_id": None, "ts": "2026-07-30T05:01:00.000Z"},
        {"session_id": None, "ts": "2026-07-30T05:02:00.000Z"},
    ]
    outcomes = [
        {"outcome_category": "ACCEPTED", "session_source": "host_supplied"},
        {"outcome_category": "OVERRIDDEN",
         "session_source": "codex_transcript_hash"},
    ]
    cov = gate_report._coverage(gates, outcomes)
    split = cov["labeled_by_session_source"]
    _assert(split.get("host_supplied") == 1, split)
    _assert(split.get("codex_transcript_hash") == 1, split)
    lines: list[str] = []
    gate_report._append_coverage(lines, cov)
    text = "\n".join(lines)
    _assert("host-supplied" in text and "codex_transcript_hash" in text,
            f"identity split must render: {text}")
    print("PASS coverage_splits_labeled_rows_by_identity_source")


if __name__ == "__main__":
    test_query_hash_matches_the_gate_implementation()
    test_nonce_join_is_exact_and_beats_an_ambiguous_hash()
    test_hash_join_attributes_a_unique_match()
    test_hash_join_declines_when_two_threads_share_the_request()
    test_hash_join_still_attributes_one_thread_repeating_itself()
    test_hash_join_declines_outside_the_time_tolerance()
    test_declines_when_no_rollout_records_the_call()
    test_declines_on_unparseable_gate_timestamp()
    test_missing_codex_home_yields_an_empty_index()
    test_gate_returns_gate_call_id_so_future_rows_join_exactly()
    test_file_touches_accepts_a_supplied_transcript_path()
    test_coverage_splits_labeled_rows_by_identity_source()
    print("ALL CODEX ATTRIBUTION TESTS PASSED")
