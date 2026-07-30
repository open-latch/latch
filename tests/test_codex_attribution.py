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


def _make_home(threads: dict[str, list[tuple[str, str, str | None]]],
               cwd: str = "/repo") -> str:
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
            "payload": {"id": thread, "cwd": cwd},
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
        THREAD_A: [("2026-07-30T05:20:00.000Z", "same request", "aaaaaaaaaaaa")],
        THREAD_B: [("2026-07-30T05:20:05.000Z", "same request", "bbbbbbbbbbbb")],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("same request", "2026-07-30T05:20:00.000Z", "bbbbbbbbbbbb"), idx)
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


# ---------- PR #73 review regressions ----------

def test_nonce_parses_out_of_a_host_wrapped_output():
    """PR #73 review P1. Real Codex outputs are wrapped ("Script completed /
    Wall time / Output:"), so decoding the whole string as JSON returns None and
    exact nonce attribution silently never fires."""
    wrapped = ('Script completed\nWall time 0.2 seconds\nOutput:\n'
               '{"request":"x","gate_call_id":"e28e30e9888f","gate_status":"OK"}')
    got = codex_attribution._gate_call_id_in_output(
        [{"type": "input_text", "text": wrapped}])
    _assert(got == "e28e30e9888f", f"wrapped output must yield the nonce: {got}")
    _assert(codex_attribution._gate_call_id_in_output(
        "the gate_call_id field was missing") is None,
        "prose mentioning the key is not a value")
    _assert(codex_attribution._gate_call_id_in_output(
        '{"gate_call_id":"NOTHEXVALUE"}') is None,
        "a wrong-shaped value must be rejected on both paths")
    print("PASS nonce_parses_out_of_a_host_wrapped_output")


def test_attribution_is_scoped_to_the_project():
    """PR #73 review P1. CODEX_HOME is machine-wide, so an identical request in
    another repo is otherwise a valid hash match."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "shared request", None)],
    }, cwd="/other/repo")
    try:
        idx = codex_attribution.build_index(Path(home))
        row = _gate_row("shared request", "2026-07-30T05:20:01.000Z")
        _assert(codex_attribution.attribute(row, idx, project="-my-repo") is None,
                "a rollout from another project must not match")
        _assert(codex_attribution.attribute(row, idx) is not None,
                "unscoped callers keep the previous behavior")
        print("PASS attribution_is_scoped_to_the_project")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_recovered_gates_truncate_at_the_next_gate_in_their_thread():
    """PR #73 review P1. The next-gate map was built before attribution, so two
    consecutive recovered gates in one thread each got a full 30-minute window
    and the earlier verdict absorbed the later one's activity."""
    rows = [
        {"ts": "2026-07-30T05:00:00.000Z", "session_id": None,
         "query_hash": "h1"},
        {"ts": "2026-07-30T05:10:00.000Z", "session_id": None,
         "query_hash": "h2"},
    ]
    resolved = {
        0: {"session_id": THREAD_A, "session_source": "codex_transcript_hash",
            "transcript_path": None},
        1: {"session_id": THREAD_A, "session_source": "codex_transcript_hash",
            "transcript_path": None},
    }
    without = correlator._build_next_in_session_map(rows)
    _assert(without == {}, f"pre-fix behavior: no truncation at all: {without}")
    with_resolved = correlator._build_next_in_session_map(rows, resolved)
    _assert(with_resolved.get(0) is not None,
            f"first recovered gate must truncate at the second: {with_resolved}")
    _assert(with_resolved.get(1) is None,
            "last gate in the thread has no successor")
    print("PASS recovered_gates_truncate_at_the_next_gate_in_their_thread")


def test_coverage_never_counts_a_recovered_row_as_unlabelable():
    """PR #73 review P1. Recovered rows stayed in the unlabelable bucket, so the
    labelable denominator could drop below the labeled count and print an
    impossible rate."""
    import gate_report
    gates = [
        {"session_id": None, "ts": "2026-07-30T05:00:00.000Z",
         "query_hash": "h1", "gate_call_id": "aaaaaaaaaaaa"},
        {"session_id": None, "ts": "2026-07-30T05:10:00.000Z",
         "query_hash": "h2", "gate_call_id": "bbbbbbbbbbbb"},
    ]
    outcomes = [
        {"outcome_category": "ACCEPTED", "gate_call_id": "aaaaaaaaaaaa",
         "gate_query_hash": "h1", "gate_ts": "2026-07-30T05:00:00.000Z",
         "session_source": "codex_transcript_hash"},
        {"outcome_category": "AMBIGUOUS", "gate_call_id": "bbbbbbbbbbbb",
         "gate_query_hash": "h2", "gate_ts": "2026-07-30T05:10:00.000Z",
         "session_source": "codex_transcript_hash"},
    ]
    cov = gate_report._coverage(gates, outcomes)
    _assert(cov["unlabelable_no_session_id"] == 0,
            f"labeled rows are not unlabelable: {cov}")
    _assert(cov["labelable_rows"] == 2, cov)
    _assert(cov["coverage_pct_of_labelable"] == 100.0,
            f"rate must not exceed 100%: {cov}")
    _assert(cov["coverage_pct"] == 100.0, cov)
    print("PASS coverage_never_counts_a_recovered_row_as_unlabelable")


def test_correlator_version_bumped_for_the_schema_change():
    """PR #73 review P2. session_source is a new field; without a bump, existing
    0.3.0 rows dedup and never gain it, and reports read them as UNKNOWN."""
    _assert(correlator.CORRELATOR_VERSION_DEFAULT == "0.4.0",
            f"expected 0.4.0, got {correlator.CORRELATOR_VERSION_DEFAULT}")
    print("PASS correlator_version_bumped_for_the_schema_change")


# ---------- verification-round regressions ----------

def test_nonce_parses_the_real_double_encoded_codex_shape():
    """The shape that actually ships. A hand-written fixture passed while this
    matched 0 of 261 real outputs: the payload is JSON inside a JSON string
    inside a host prefix, so its quotes arrive backslashed."""
    inner = json.dumps({"request": "x", "gate_call_id": "e28e30e9888f",
                        "gate_status": "OK"})
    real = "Wall time: 22.2 seconds\nOutput:\n" + json.dumps(
        [{"type": "text", "text": inner}])
    got = codex_attribution._gate_call_id_in_output(real)
    _assert(got == "e28e30e9888f", f"real shape must yield the nonce: {got!r}")
    print("PASS nonce_parses_the_real_double_encoded_codex_shape")


def test_ambiguous_nonce_never_claims_an_exact_match():
    """A replayed or resumed thread can record one call_id twice. Last-write-wins
    would hand back whichever rollout was scanned last and stamp it
    `codex_transcript_nonce` — a confident wrong label. The nonce must decline;
    falling through to the hash is fine when the hash is itself unique, because
    that is independent evidence."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "req one", "cccccccccccc")],
        THREAD_B: [("2026-07-30T05:21:00.000Z", "req two", "cccccccccccc")],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("req one", "2026-07-30T05:20:01.000Z", "cccccccccccc"), idx)
        _assert(hit is None or hit["source"] != "codex_transcript_nonce",
                f"an ambiguous nonce must not be labeled exact: {hit}")
        if hit:
            _assert(hit["session_id"] == THREAD_A,
                    f"the hash fallback must still be right: {hit}")
        print("PASS ambiguous_nonce_never_claims_an_exact_match")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_ambiguous_nonce_and_ambiguous_hash_decline_entirely():
    """Both signals ambiguous: nothing left to be confident about."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "same text", "cccccccccccc")],
        THREAD_B: [("2026-07-30T05:20:30.000Z", "same text", "cccccccccccc")],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        hit = codex_attribution.attribute(
            _gate_row("same text", "2026-07-30T05:20:10.000Z", "cccccccccccc"), idx)
        _assert(hit is None, f"both signals ambiguous must decline: {hit}")
        print("PASS ambiguous_nonce_and_ambiguous_hash_decline_entirely")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_matching_project_still_attributes():
    """Positive case for the scope check. Without it, deleting the scoping
    entirely leaves the suite green — the other tests only prove it rejects."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "scoped request", None)],
    }, cwd="/Users/someone/myrepo")
    try:
        import paths as _paths
        idx = codex_attribution.build_index(Path(home))
        proj = _paths.sanitize_cwd("/Users/someone/myrepo")
        hit = codex_attribution.attribute(
            _gate_row("scoped request", "2026-07-30T05:20:02.000Z"), idx,
            project=proj)
        _assert(hit is not None,
                f"a matching project must still attribute (project={proj})")
        _assert(hit["session_id"] == THREAD_A, hit)
        print("PASS matching_project_still_attributes")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_declined_rows_bound_a_recovered_window():
    """A session-less row attribution declined on still belongs to SOME thread,
    so it must bound a recovered row's window. Without this a DO_NOT_PROCEED was
    reported OVERRIDDEN by the next gate's writes."""
    rows = [
        {"ts": "2026-07-30T05:00:00.000Z", "session_id": None,
         "query_hash": "h1", "project": "-p"},
        {"ts": "2026-07-30T05:10:00.000Z", "session_id": None,
         "query_hash": "h2", "project": "-p"},
    ]
    resolved = {0: {"session_id": THREAD_A,
                    "session_source": "codex_transcript_hash",
                    "transcript_path": None}}
    undetermined = [("-p", datetime(2026, 7, 30, 5, 10, tzinfo=timezone.utc))]
    without = correlator._build_next_in_session_map(rows, resolved)
    _assert(without.get(0) is None, f"pre-fix: no boundary: {without}")
    with_undet = correlator._build_next_in_session_map(
        rows, resolved, undetermined)
    _assert(with_undet.get(0) is not None,
            f"declined row must bound the recovered window: {with_undet}")
    print("PASS declined_rows_bound_a_recovered_window")


def test_declined_row_in_another_project_does_not_bound():
    rows = [{"ts": "2026-07-30T05:00:00.000Z", "session_id": None,
             "query_hash": "h1", "project": "-p"}]
    resolved = {0: {"session_id": THREAD_A,
                    "session_source": "codex_transcript_hash",
                    "transcript_path": None}}
    other = [("-other", datetime(2026, 7, 30, 5, 10, tzinfo=timezone.utc))]
    got = correlator._build_next_in_session_map(rows, resolved, other)
    _assert(got.get(0) is None, f"cross-project row must not bound: {got}")
    print("PASS declined_row_in_another_project_does_not_bound")


def test_coverage_buckets_are_disjoint():
    """Regression I introduced while fixing coverage: a row that is BOTH
    skipped and session-less was subtracted twice, driving labelable negative
    and rendering rates above 100% in the honesty block itself."""
    import gate_report
    gates = [{"session_id": None, "skipped": True,
              "ts": f"2026-07-30T05:0{i}:00.000Z", "query_hash": f"h{i}"}
             for i in range(5)]
    cov = gate_report._coverage(gates, [])
    buckets = (cov["unlabelable_no_session_id"]
               + cov["unlabelable_skipped_verdict"]
               + cov["unlabelable_unparseable_ts"])
    _assert(buckets + cov["labelable_rows"] == cov["gate_rows"],
            f"buckets must partition the rows exactly: {cov}")
    _assert(cov["labelable_rows"] >= 0, f"labelable cannot go negative: {cov}")
    _assert(cov["coverage_pct"] == 0.0, cov)
    print("PASS coverage_buckets_are_disjoint")


def test_unknown_provenance_is_not_reported_as_recovered():
    """A vault where recovery never ran reported itself as 100% recovered,
    because rows written before session_source existed render as UNKNOWN."""
    import gate_report
    gates = [{"session_id": "a", "ts": "2026-07-30T05:00:00.000Z"}]
    outcomes = [{"outcome_category": "ACCEPTED"}]  # legacy row, no session_source
    cov = gate_report._coverage(gates, outcomes)
    lines: list[str] = []
    gate_report._append_coverage(lines, cov)
    text = "\n".join(lines)
    _assert("unknown provenance" in text,
            f"legacy rows must read as unknown, not recovered: {text}")
    _assert("recovered from transcript" not in text,
            f"nothing was recovered here: {text}")
    print("PASS unknown_provenance_is_not_reported_as_recovered")


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
    test_nonce_parses_out_of_a_host_wrapped_output()
    test_attribution_is_scoped_to_the_project()
    test_recovered_gates_truncate_at_the_next_gate_in_their_thread()
    test_coverage_never_counts_a_recovered_row_as_unlabelable()
    test_correlator_version_bumped_for_the_schema_change()
    test_nonce_parses_the_real_double_encoded_codex_shape()
    test_ambiguous_nonce_never_claims_an_exact_match()
    test_ambiguous_nonce_and_ambiguous_hash_decline_entirely()
    test_matching_project_still_attributes()
    test_declined_rows_bound_a_recovered_window()
    test_declined_row_in_another_project_does_not_bound()
    test_coverage_buckets_are_disjoint()
    test_unknown_provenance_is_not_reported_as_recovered()
    print("ALL CODEX ATTRIBUTION TESTS PASSED")
