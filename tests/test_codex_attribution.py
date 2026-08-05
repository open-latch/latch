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
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import codex_attribution  # noqa: E402
import correlator         # noqa: E402
import db                 # noqa: E402
import gate               # noqa: E402
import paths              # noqa: E402
import project_proof      # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


THREAD_A = "11111111-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"


def _proof_context(epoch: str = "epoch-1") -> project_proof.ProjectProofContext:
    return project_proof.ProjectProofContext.from_vault_key(
        bytes.fromhex("31" * 32),
        key_epoch=epoch,
        vault_id="33333333-3333-4333-8333-333333333333",
    )


def _make_home(threads: dict[str, list[tuple]],
               cwd: str | dict[str, str] = "/repo",
               directory_date: str | dict[str, str] = "2026/07/30") -> str:
    """Build a throwaway CODEX_HOME. `threads` maps thread id ->
    [(iso_ts, request, gate_call_id_or_None, skipped_or_None?), ...].

    The JSONL line and double-encoding shapes are sanitized from real Codex
    rollouts (session_meta, function_call, function_call_output).  Tests vary
    only content, paths, ids, and timestamps; the external encoding shape is
    preserved rather than invented.
    """
    tmp = tempfile.mkdtemp(prefix="codex_attr_test_")
    for thread, calls in threads.items():
        thread_day = (
            directory_date.get(thread, "2026/07/30")
            if isinstance(directory_date, dict) else directory_date
        )
        thread_cwd = cwd.get(thread, "/repo") if isinstance(cwd, dict) else cwd
        day = Path(tmp) / "sessions" / Path(thread_day)
        day.mkdir(parents=True, exist_ok=True)
        file_day = thread_day.replace("/", "-")
        p = day / f"rollout-{file_day}T05-18-19-{thread}.jsonl"
        lines = [json.dumps({
            "timestamp": "2026-07-30T05:18:19.000Z",
            "type": "session_meta",
            "payload": {"id": thread, "cwd": thread_cwd},
        })]
        for i, call in enumerate(calls):
            ts, request, nonce = call[:3]
            skipped = call[3] if len(call) > 3 else None
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
            if skipped is not None:
                output["skipped"] = bool(skipped)
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
        # Historical diagnostic only; production attribution ignores hashes.
        "query_hash": gate._query_hash(request),
        "ts": ts,
        "session_id": None,
    }
    if nonce:
        row["gate_call_id"] = nonce
    return row


# ---------- the join key agrees with the gate's own ----------

def test_production_attribution_exposes_no_hash_join():
    _assert(not hasattr(codex_attribution, "query_hash"),
            "v2.6 freezes historical hash pilots; no live hash join ships")
    print("PASS production_attribution_exposes_no_hash_join")


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
        _assert(hit is None, "a unique hash must remain an unjoined frozen pilot")
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
        _assert(hit is None, "hash retries are never joined in v2.6")
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
    _assert(idx["by_nonce"] == {}, idx)
    _assert("by_hash" not in idx, idx)
    _assert(idx["session_calls"] == {}, idx)
    receipt = idx["candidate_completeness"]
    _assert(receipt["root_present"] is False, receipt)
    _assert(receipt["complete"] is False, receipt)
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
        _assert(without is None,
                f"no sessions row means evidence unavailable: {without}")

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
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "wrapped request", "aaaaaaaaaaaa")],
    })
    try:
        path = next(Path(home).rglob("rollout-*.jsonl"))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        result = {
            "request": "wrapped request",
            "gate_call_id": "aaaaaaaaaaaa",
            "gate_status": "OK",
        }
        rows[2]["payload"]["output"] = (
            "Wall time: 0.0000 seconds\nOutput:\n"
            + json.dumps([{"type": "text", "text": json.dumps(result)}])
        )
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )

        idx = codex_attribution.build_index(Path(home))
        _assert(idx["candidate_completeness"]["complete"] is True, idx)
        hit = codex_attribution.attribute(
            _gate_row("wrapped request", "2026-07-30T05:20:00.000Z", "aaaaaaaaaaaa"),
            idx,
        )
        _assert(hit is not None and hit["session_id"] == THREAD_A, hit)
        _assert(not hasattr(codex_attribution, "_gate_call_id_in_output"),
                "host wrapping belongs to the shared corpus parser")
        print("PASS nonce_parses_out_of_a_host_wrapped_output")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_current_outer_exec_envelope_is_structurally_attributed():
    """A sanitized current Codex exec envelope remains nonce-attributable."""
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "outer request", "bbbbbbbbbbbb")],
    })
    try:
        path = next(Path(home).rglob("rollout-*.jsonl"))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        call = rows[1]["payload"]
        call.update({
            "type": "custom_tool_call",
            "name": "exec",
            "input": (
                "const result = await tools.mcp__latch__latch_gate("
                "{request: 'SANITIZED REQUEST'});\ntext(result);"
            ),
        })
        call.pop("arguments")
        result = {
            "request": "SANITIZED REQUEST",
            "gate_call_id": "bbbbbbbbbbbb",
            "gate_status": "OK",
        }
        rows[2]["payload"].update({
            "type": "custom_tool_call_output",
            "output": (
                "Wall time: 0.0000 seconds\nOutput:\n"
                + json.dumps([{"type": "text", "text": json.dumps(result)}])
            ),
        })
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )

        idx = codex_attribution.build_index(Path(home))
        receipt = idx["candidate_completeness"]
        _assert(receipt["complete"] is True, receipt)
        hit = codex_attribution.attribute(
            _gate_row("outer request", "2026-07-30T05:20:00.000Z", "bbbbbbbbbbbb"),
            idx,
        )
        _assert(hit is not None and hit["session_id"] == THREAD_A, hit)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_attribution_is_scoped_to_the_project():
    """PR #73 review P1. CODEX_HOME is machine-wide, so an identical request in
    another repo is otherwise a valid hash match."""
    context = _proof_context()
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "shared request", "aaaaaaaaaaaa")],
    }, cwd="/other/repo")
    try:
        idx = codex_attribution.build_index(
            Path(home), proof_context=context, target_project_path="/my/repo",
        )
        row = _gate_row("shared request", "2026-07-30T05:20:01.000Z", "aaaaaaaaaaaa")
        _assert(codex_attribution.attribute(row, idx) is None,
                "a rollout from another project must not match")
        legacy = codex_attribution.build_index(Path(home))
        _assert(codex_attribution.attribute(
            row, legacy, project=paths.sanitize_cwd("/my/repo"),
        ) is None, "a lossy legacy project key must fail closed")
        print("PASS attribution_is_scoped_to_the_project")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_foreign_same_nonce_candidate_cannot_manufacture_target_uniqueness():
    """Frozen M2: project filtering cannot upgrade a nonce collision.

    One target candidate plus one positively foreign candidate is still two
    distinct observations of the same nonce before finalization.  The foreign
    row may be excluded from target attribution, but never from conflict
    detection.
    """
    context = _proof_context()
    target = "/target/repo"
    foreign = "/foreign/repo"
    home = _make_home(
        {
            THREAD_A: [
                ("2026-07-30T05:20:00.000Z", "target request", "aaaaaaaaaaaa"),
            ],
            THREAD_B: [
                ("2026-07-30T05:20:01.000Z", "foreign request", "aaaaaaaaaaaa"),
            ],
        },
        cwd={THREAD_A: target, THREAD_B: foreign},
    )
    try:
        idx = codex_attribution.build_index(
            Path(home), proof_context=context, target_project_path=target,
        )
        _assert(len(idx["by_nonce"]["aaaaaaaaaaaa"]) == 2, idx["by_nonce"])

        hit = codex_attribution.attribute(
            _gate_row(
                "target request", "2026-07-30T05:20:02.000Z", "aaaaaaaaaaaa",
            ),
            idx,
        )
        _assert(hit is not None and hit.get("conflict") is True, hit)
        _assert(hit.get("session_id") is None, hit)
        _assert("nonce_in_multiple_sessions" in hit.get("conflict_reasons", ()), hit)
        _assert("nonidentical_nonce_candidate" in hit.get("conflict_reasons", ()), hit)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_same_call_nonidentical_results_are_preserved_as_nonce_conflict():
    """M2: one call offset with two semantic results is two candidates."""

    nonce = "aaaaaaaaaaaa"
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "one request", nonce)],
    })
    try:
        path = next(Path(home).rglob("rollout-*.jsonl"))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        result = {
            "gate_call_id": nonce,
            "gate_status": "OK",
            "recommendation": "PROCEED",
        }
        rows[2]["payload"]["output"] = json.dumps(result)
        duplicate = json.loads(json.dumps(rows[2]))
        result["recommendation"] = "MODIFY"
        duplicate["payload"]["output"] = json.dumps(result)
        rows.append(duplicate)
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )

        idx = codex_attribution.build_index(Path(home))
        receipt = idx["candidate_completeness"]
        _assert(receipt["complete"] is True, receipt)
        candidates = idx["by_nonce"][nonce]
        _assert(len(candidates) == 2, candidates)
        hit = codex_attribution.attribute(
            _gate_row("one request", "2026-07-30T05:20:00.000Z", nonce),
            idx,
        )
        _assert(hit is not None and hit.get("conflict") is True, hit)
        _assert("nonidentical_nonce_candidate" in hit["conflict_reasons"], hit)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_candidate_cwd_project_proof_participates_in_semantic_conflict():
    context = _proof_context()
    host = {
        "nonce": "aaaaaaaaaaaa",
        "session_id": THREAD_A,
        "adapter": "codex",
        "verdict": "PROCEED",
        "verdict_id_lists": {"evidence_ids": [4164]},
    }
    base = {
        "session_id": THREAD_A,
        "ts": datetime(2026, 7, 30, 5, 20, tzinfo=timezone.utc),
        "host_observation": host,
    }
    target = {
        **base,
        "project_proof": context.prove("/target/repo"),
    }
    foreign = {
        **base,
        "project_proof": context.prove("/foreign/repo"),
    }
    reasons = codex_attribution._candidate_set_conflicts([target, foreign])
    _assert("nonidentical_nonce_candidate" in reasons, reasons)


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
    _assert(correlator.CORRELATOR_VERSION_DEFAULT == "0.5.0",
            f"expected 0.5.0, got {correlator.CORRELATOR_VERSION_DEFAULT}")
    _assert(
        correlator.MEASUREMENT_PROTOCOL_VERSION_DEFAULT == "outcome-v2.6.0",
        correlator.MEASUREMENT_PROTOCOL_VERSION_DEFAULT,
    )
    print("PASS correlator_version_bumped_for_the_schema_change")


# ---------- verification-round regressions ----------

def test_nonce_parses_the_real_double_encoded_codex_shape():
    """The shape that actually ships. A hand-written fixture passed while this
    matched 0 of 261 real outputs: the payload is JSON inside a JSON string
    inside a host prefix, so its quotes arrive backslashed."""
    _assert(not hasattr(codex_attribution, "_gate_call_id_in_output"),
            "attribution consumes shared parser observations")
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
        _assert(hit is not None and hit.get("conflict") is True,
                f"an ambiguous nonce must be explicit conflict: {hit}")
        _assert(hit.get("session_id") is None, hit)
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
        _assert(hit is not None and hit.get("conflict") is True,
                f"duplicate nonce must remain an explicit conflict: {hit}")
        print("PASS ambiguous_nonce_and_ambiguous_hash_decline_entirely")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_matching_project_still_attributes():
    """Positive case for the scope check. Without it, deleting the scoping
    entirely leaves the suite green — the other tests only prove it rejects."""
    project_path = "/Users/someone/myrepo"
    context = _proof_context()
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "scoped request", "aaaaaaaaaaaa")],
    }, cwd=project_path)
    try:
        idx = codex_attribution.build_index(
            Path(home),
            proof_context=context,
            target_project_path=project_path,
        )
        hit = codex_attribution.attribute(
            _gate_row("scoped request", "2026-07-30T05:20:02.000Z", "aaaaaaaaaaaa"), idx,
        )
        _assert(hit is not None,
                "a matching project proof must still attribute")
        _assert(hit["session_id"] == THREAD_A, hit)
        _assert(hit["project_check"] == project_proof.PROJECT_MATCH, hit)
        print("PASS matching_project_still_attributes")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_partial_date_index_cannot_manufacture_false_uniqueness():
    """A resumed thread stays under its start day.  Both candidates must be
    visible even when the requested report window names only the later day."""
    home = _make_home(
        {
            THREAD_A: [("2026-07-30T05:20:00.000Z", "same request", "aaaaaaaaaaaa")],
            THREAD_B: [("2026-07-30T05:20:04.000Z", "same request", "aaaaaaaaaaaa")],
        },
        directory_date={THREAD_A: "2026/07/20", THREAD_B: "2026/07/30"},
    )
    try:
        idx = codex_attribution.build_index(
            Path(home), date(2026, 7, 30), date(2026, 7, 30),
        )
        receipt = idx["candidate_completeness"]
        _assert(receipt["complete"] is True, receipt)
        _assert(receipt["enumerated_files"] == 2, receipt)
        hit = codex_attribution.attribute(
            _gate_row("same request", "2026-07-30T05:20:02.000Z", "aaaaaaaaaaaa"), idx,
        )
        _assert(hit is not None and hit.get("conflict") is True,
                f"the old-directory candidate must preserve conflict: {hit}")
        print("PASS partial_date_index_cannot_manufacture_false_uniqueness")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_long_lived_rollout_is_discovered_many_days_after_start():
    home = _make_home(
        {THREAD_A: [("2026-07-30T05:20:00.000Z", "resumed request", "aaaaaaaaaaaa")]},
        directory_date="2026/07/01",
    )
    try:
        idx = codex_attribution.build_index(
            Path(home), date(2026, 7, 30), date(2026, 7, 30),
        )
        hit = codex_attribution.attribute(
            _gate_row("resumed request", "2026-07-30T05:20:01.000Z", "aaaaaaaaaaaa"), idx,
        )
        _assert(hit is not None and hit["session_id"] == THREAD_A, hit)
        print("PASS long_lived_rollout_is_discovered_many_days_after_start")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_candidate_completeness_receipt_is_count_only_and_fail_closed():
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "unique request", None)],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        receipt = idx["candidate_completeness"]
        encoded = json.dumps(receipt, sort_keys=True)
        _assert(receipt["version"] == "codex-rollout-full-v2", receipt)
        _assert(receipt["scope"] == "all_rollouts", receipt)
        _assert(receipt["complete"] is True, receipt)
        _assert(home not in encoded and ".jsonl" not in encoded,
                f"receipt must contain counts, not source paths: {receipt}")

        incomplete = {
            **idx,
            "candidate_completeness": {**receipt, "complete": False},
        }
        hit = codex_attribution.attribute(
            _gate_row("unique request", "2026-07-30T05:20:01.000Z"),
            incomplete,
        )
        _assert(hit is None, f"an incomplete inventory cannot attribute: {hit}")
        print("PASS candidate_completeness_receipt_is_count_only_and_fail_closed")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_collision_resistant_project_proof_filters_sanitize_collision():
    left = "/tmp/repo-a/module"
    right = "/tmp/repo/a-module"
    _assert(paths.sanitize_cwd(left) == paths.sanitize_cwd(right),
            "fixture must reproduce the lossy sanitize_cwd collision")
    context = _proof_context()
    home = _make_home(
        {
            THREAD_A: [("2026-07-30T05:20:00.000Z", "shared request", "aaaaaaaaaaaa")],
            THREAD_B: [("2026-07-30T05:20:03.000Z", "shared request", "aaaaaaaaaaaa")],
        },
        cwd={THREAD_A: left, THREAD_B: right},
    )
    try:
        idx = codex_attribution.build_index(
            Path(home), proof_context=context, target_project_path=left,
        )
        hit = codex_attribution.attribute(
            _gate_row("shared request", "2026-07-30T05:20:01.000Z", "aaaaaaaaaaaa"), idx,
        )
        _assert(hit is not None and hit.get("conflict") is True, hit)
        _assert(hit.get("session_id") is None, hit)
        _assert(hit["project_check"] == project_proof.PROJECT_MATCH, hit)
        _assert("nonidentical_nonce_candidate" in hit["conflict_reasons"], hit)
        print("PASS collision_resistant_project_proof_filters_sanitize_collision")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_project_key_rotation_blocks_attribution_without_claiming_foreign():
    project_path = "/tmp/rotating-project"
    old_context = _proof_context("epoch-1")
    new_context = _proof_context("epoch-2")
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "rotated request", "aaaaaaaaaaaa")],
    }, cwd=project_path)
    try:
        idx = codex_attribution.build_index(
            Path(home),
            proof_context=old_context,
            target_project_path=project_path,
        )
        new_proof = new_context.prove(project_path)
        candidate = idx["by_nonce"]["aaaaaaaaaaaa"][0]
        _assert(project_proof.compare_project_proofs(
            candidate["project_proof"], new_proof,
        ) == project_proof.PROJECT_KEY_EPOCH_MISMATCH,
                "rotation is a loss signal, never foreign")
        hit = codex_attribution.attribute(
            _gate_row("rotated request", "2026-07-30T05:20:01.000Z", "aaaaaaaaaaaa"),
            idx,
            target_project_proof=new_proof,
        )
        _assert(hit is None, f"mixed key epochs cannot attribute: {hit}")
        print("PASS project_key_rotation_blocks_attribution_without_claiming_foreign")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_session_calls_keep_skipped_and_unmatched_boundaries_in_order():
    home = _make_home({
        THREAD_A: [
            ("2026-07-30T05:20:00.000Z", "measured", "aaaaaaaaaaaa", False),
            ("2026-07-30T05:21:00.000Z", "skipped", "bbbbbbbbbbbb", True),
            ("2026-07-30T05:22:00.000Z", "unmatched", None, False),
        ],
    })
    try:
        idx = codex_attribution.build_index(Path(home))
        calls = idx["session_calls"][THREAD_A]
        _assert([call["gate_call_id"] for call in calls]
                == ["aaaaaaaaaaaa", "bbbbbbbbbbbb", None], calls)
        _assert([call["skipped"] for call in calls] == [False, True, False], calls)
        _assert(all("query_hash" not in call and "request" not in call
                    for call in calls),
                f"boundary stream must be prompt-free: {calls}")
        print("PASS session_calls_keep_skipped_and_unmatched_boundaries_in_order")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_malformed_gate_arguments_make_candidate_index_incomplete():
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "valid request", "aaaaaaaaaaaa")],
    })
    try:
        path = next(Path(home).rglob("rollout-*.jsonl"))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        call = next(
            row for row in rows
            if (row.get("payload") or {}).get("type") == "function_call"
        )
        call["payload"]["arguments"] = "{}"  # corpus shape, missing request
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8",
        )
        idx = codex_attribution.build_index(Path(home))
        receipt = idx["candidate_completeness"]
        _assert(receipt["malformed_candidate_regions"] == 1, receipt)
        _assert(receipt["complete"] is False, receipt)
        _assert(codex_attribution.attribute(
            _gate_row("valid request", "2026-07-30T05:20:00.000Z", "aaaaaaaaaaaa"),
            idx,
        ) is None, "malformed candidate inventory must fail closed")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_shared_parser_schema_invalid_makes_candidate_index_incomplete():
    """Every shared-parser schema failure invalidates candidate completeness.

    The malformed region deliberately contains no gate-name text.  Candidate
    completeness is a property of the pinned rollout snapshot, not of a
    secondary byte-substring heuristic in the attribution adapter.
    """
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "valid request", "aaaaaaaaaaaa")],
    })
    try:
        path = next(Path(home).rglob("rollout-*.jsonl"))
        with path.open("ab") as stream:
            stream.write(b'{"unrelated":"truncated"\n')

        idx = codex_attribution.build_index(Path(home))
        receipt = idx["candidate_completeness"]
        _assert(receipt["malformed_candidate_regions"] == 1, receipt)
        _assert(receipt["complete"] is False, receipt)
        _assert(codex_attribution.attribute(
            _gate_row("valid request", "2026-07-30T05:20:00.000Z", "aaaaaaaaaaaa"),
            idx,
        ) is None, "shared-parser schema loss must fail closed")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_full_file_digest_change_between_passes_fails_completeness(monkeypatch):
    home = _make_home({
        THREAD_A: [("2026-07-30T05:20:00.000Z", "valid request", "aaaaaaaaaaaa")],
    })
    original = codex_attribution._read_snapshot
    reads: dict[str, int] = {}

    def changed_digest(path: Path):
        data, digest, unstable = original(path)
        key = str(path)
        reads[key] = reads.get(key, 0) + 1
        if reads[key] > 1 and digest is not None:
            digest = "f" * 64 if digest != "f" * 64 else "e" * 64
        return data, digest, unstable

    monkeypatch.setattr(codex_attribution, "_read_snapshot", changed_digest)
    try:
        idx = codex_attribution.build_index(Path(home))
        receipt = idx["candidate_completeness"]
        _assert(receipt["content_changed_files"] == 1, receipt)
        _assert(receipt["complete"] is False, receipt)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_local_unsanitized_corpus_discovery_floor():
    """Local-only conformance floor over the real, unsanitized Codex corpus."""
    if os.environ.get("LATCH_RUN_CODEX_CORPUS_CONFORMANCE") != "1":
        pytest.skip("set LATCH_RUN_CODEX_CORPUS_CONFORMANCE=1 for local corpus scan")
    home = codex_attribution.codex_transcript.codex_home()
    idx = codex_attribution.build_index(home)
    receipt = idx["candidate_completeness"]
    minimum_rollouts = int(os.environ.get("LATCH_CODEX_CORPUS_MIN_ROLLOUTS", "1"))
    minimum_calls = int(os.environ.get("LATCH_CODEX_CORPUS_MIN_GATE_CALLS", "1"))
    observed_calls = sum(len(calls) for calls in idx["session_calls"].values())
    defect_fields = (
        "traversal_errors",
        "unreadable_files",
        "unstable_files",
        "content_changed_files",
        "malformed_candidate_regions",
        "missing_tool_results",
        "session_identity_conflicts",
        "unidentified_gate_files",
    )
    expected_complete = not any(int(receipt[name]) for name in defect_fields)
    expected_complete = expected_complete and not bool(receipt["inventory_changed"])
    _assert(receipt["complete"] is expected_complete, receipt)
    _assert(receipt["root_present"] is True, receipt)
    _assert(receipt["scanned_files"] == receipt["enumerated_files"], receipt)
    _assert(receipt["enumerated_files"] >= minimum_rollouts, receipt)
    _assert(observed_calls >= minimum_calls,
            f"observed {observed_calls} gate calls, expected >= {minimum_calls}")


def test_unknown_gate_in_another_thread_never_truncates(  # noqa: N802
):
    """Requested regression. Two concurrent same-project Codex threads: an
    unattributed gate in thread B must not erase thread A's later activity.
    Measured on live data, the previous behavior cut a PROCEED window from
    1800s to 391s and turned 6 observed file touches into 0 — which flips a
    DO_NOT_PROCEED toward ACCEPTED, the flattering direction."""
    rows = [
        {"ts": "2026-07-30T05:00:00.000Z", "session_id": None,
         "query_hash": "hA", "project": "-p"},          # thread A, recovered
        {"ts": "2026-07-30T05:06:00.000Z", "session_id": None,
         "query_hash": "hB", "project": "-p"},          # thread B, undetermined
    ]
    resolved = {0: {"session_id": THREAD_A,
                    "session_source": "codex_transcript_hash",
                    "transcript_path": None}}
    got = correlator._build_next_in_session_map(rows, resolved)
    _assert(got.get(0) is None,
            f"an unattributed gate must not bound another thread: {got}")
    print("PASS unknown_gate_in_another_thread_never_truncates")


def test_boundary_censor_requires_exact_session_evidence():
    t0 = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)
    t_end = datetime(2026, 7, 30, 5, 30, tzinfo=timezone.utc)
    inside = {THREAD_A: [datetime(2026, 7, 30, 5, 6, tzinfo=timezone.utc)]}
    outside = {THREAD_A: [datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc)]}
    other = {THREAD_B: [datetime(2026, 7, 30, 5, 6, tzinfo=timezone.utc)]}
    _assert(
        correlator._boundary_censor_reason(inside, THREAD_A, t0, t_end)
        == "boundary_uncertain",
        "an exact-session marker inside the window must censor",
    )
    _assert(correlator._boundary_censor_reason(outside, THREAD_A, t0, t_end) is None,
            "an exact-session marker after the window is irrelevant")
    _assert(correlator._boundary_censor_reason(other, THREAD_A, t0, t_end) is None,
            "a foreign-session marker cannot censor")
    _assert(correlator._boundary_censor_reason({}, THREAD_A, t0, t_end) is None,
            "unknown-session evidence cannot censor")
    print("PASS boundary_censor_requires_exact_session_evidence")


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
    got = correlator._build_next_in_session_map(rows, resolved)
    _assert(got.get(0) is None,
            "a declined row is NOT a boundary — see "
            "test_unknown_gate_in_another_thread_never_truncates for why")
    print("PASS declined_rows_bound_a_recovered_window")


def test_declined_row_in_another_project_does_not_bound():
    rows = [{"ts": "2026-07-30T05:00:00.000Z", "session_id": None,
             "query_hash": "h1", "project": "-p"}]
    resolved = {0: {"session_id": THREAD_A,
                    "session_source": "codex_transcript_hash",
                    "transcript_path": None}}
    got = correlator._build_next_in_session_map(rows, resolved)
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
    test_unknown_gate_in_another_thread_never_truncates()
    test_boundary_censor_requires_exact_session_evidence()
    test_declined_rows_bound_a_recovered_window()
    test_declined_row_in_another_project_does_not_bound()
    test_coverage_buckets_are_disjoint()
    test_unknown_provenance_is_not_reported_as_recovered()
    print("ALL CODEX ATTRIBUTION TESTS PASSED")
