"""Tests for the V1 outcome-instrumentation slice (KB id=3948 phase V item V1,
scope ratified in id=3985).

Three changes are pinned here:

1. ``_log_invocation`` persists the remaining verdict id-lists
   (``abandoned_paths`` / ``active_constraints`` / ``current_direction``)
   alongside ``decision_chain``, and still writes no verdict prose. The
   id-lists-only reading is the one compatible with id=3915, which excludes
   summary and decision text by name.
2. The correlator grounds a MODIFY outcome in files edited inside the window
   (the "shipped diff" signal). MODIFY with no KB write but with shipped code
   reads OVERRIDDEN; MODIFY with neither stays AMBIGUOUS by founder ruling.
3. ``gate_report`` publishes a coverage block whose denominator is ALL gate
   rows, exposes the unlabelable count next to the rate, and never
   double-counts across correlator versions.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import artifacts    # noqa: E402
import correlator   # noqa: E402
import db           # noqa: E402
import gate         # noqa: E402
import gate_report  # noqa: E402
import paths        # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh():
    tmp = tempfile.mkdtemp(prefix="kb_v1_instr_test_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


VERDICT = {
    "recommendation": "MODIFY",
    "summary": "SECRET_SUMMARY_PROSE about the abandoned path.",
    "decision_chain": [10, 11],
    "abandoned_paths": [99, 98],
    "active_constraints": [11],
    "current_direction": [10],
    "risk_if_proceed": "SECRET_RISK_PROSE",
    "better_next_action": "SECRET_ACTION_PROSE",
    "evidence_nodes": [10, 11],
    "load_bearing_claims": [],
    "uncovered_claims": [],
    "prompt_chars": 100,
    "backend": "claude",
}


def _log_one(tmp, verdict):
    gate._log_invocation(
        request="a request",
        verdict=verdict,
        evidence=[],
        chain_assembly={"seeds": [], "evidence_node_ids": []},
        elapsed_ms=1.0,
        project_path=tmp,
        session_id="sid-1",
        gate_call_id="call-1",
    )
    rows = []
    for f in sorted(paths.project_dir(tmp).glob("gate-*.log")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


# ---------- 1. verdict id-lists reach the durable log ----------

def test_gate_log_persists_all_verdict_id_lists():
    tmp, conn = _fresh()
    try:
        rows = _log_one(tmp, VERDICT)
        _assert(len(rows) == 1, f"expected one row, got {len(rows)}")
        row = rows[0]
        for field, expected in (
            ("decision_chain", [10, 11]),
            ("abandoned_paths", [99, 98]),
            ("active_constraints", [11]),
            ("current_direction", [10]),
        ):
            _assert(field in row, f"{field} missing from gate log row: {row}")
            _assert(row[field] == expected,
                    f"{field} should round-trip: {row[field]!r} != {expected!r}")
        print("PASS gate_log_persists_all_verdict_id_lists")
    finally:
        _cleanup(tmp, conn)


def test_gate_log_id_lists_are_ints_and_carry_no_prose():
    """The whole reason this is allowed under id=3915: ints, never text."""
    tmp, conn = _fresh()
    try:
        row = _log_one(tmp, VERDICT)[0]
        for field in ("abandoned_paths", "active_constraints",
                      "current_direction"):
            _assert(isinstance(row[field], list), f"{field} must be a list")
            for value in row[field]:
                _assert(isinstance(value, int) and not isinstance(value, bool),
                        f"{field} must hold plain ints, got {value!r}")
        blob = json.dumps(row, default=str)
        for needle in ("SECRET_SUMMARY_PROSE", "SECRET_RISK_PROSE",
                       "SECRET_ACTION_PROSE"):
            _assert(needle not in blob,
                    f"verdict prose {needle!r} leaked into the log: {blob}")
        for forbidden in ("summary", "risk_if_proceed", "better_next_action"):
            _assert(forbidden not in row,
                    f"{forbidden} must not be a log field: {row.keys()}")
        print("PASS gate_log_id_lists_are_ints_and_carry_no_prose")
    finally:
        _cleanup(tmp, conn)


def test_gate_log_id_lists_present_even_when_empty():
    """Absent-vs-empty must not be ambiguous: the key is always written, so
    "field in 100% of new gate logs" is checkable without guessing."""
    tmp, conn = _fresh()
    try:
        bare = dict(VERDICT)
        bare.update(abandoned_paths=[], active_constraints=[],
                    current_direction=[])
        row = _log_one(tmp, bare)[0]
        for field in ("abandoned_paths", "active_constraints",
                      "current_direction"):
            _assert(field in row, f"{field} must be present when empty: {row}")
            _assert(row[field] == [], f"{field} should be []: {row[field]!r}")
        print("PASS gate_log_id_lists_present_even_when_empty")
    finally:
        _cleanup(tmp, conn)


def test_role_bearing_id_outside_evidence_still_gets_a_row():
    """`evidence` is hydrated from evidence_nodes only. A classifier can name an
    abandoned path it did not also list as evidence; iterating evidence alone
    dropped that id silently, losing the one role the stream exists for."""
    captured: list[dict] = []

    def _fake_emit(**kwargs):
        captured.append(kwargs)

    tmp, conn = _fresh()
    orig = gate.capture_streams.emit_gate_outcome_event
    try:
        gate.capture_streams.emit_gate_outcome_event = _fake_emit
        # 99 and 98 are abandoned paths; only 10/11 are hydrated evidence.
        gate._emit_gate_outcome_event(
            project_path=tmp,
            session_id="sid-1",
            request="a request",
            gate_call_id="call-1",
            verdict=dict(VERDICT),
            exposure=[],
            evidence=[
                {"id": 10, "kind": "decision", "status": "canonical",
                 "workstream_id": None},
                {"id": 11, "kind": "fact", "status": "canonical",
                 "workstream_id": None},
            ],
        )
        _assert(captured, "expected an outcome event to be recorded")
        cited = captured[0]["cited_nodes"]
        by_id = {c["node_id"]: c for c in cited}
        for missing in (99, 98):
            _assert(missing in by_id,
                    f"role-bearing id {missing} dropped: {sorted(by_id)}")
            _assert("abandoned_path" in by_id[missing]["roles"],
                    f"id {missing} lost its role: {by_id[missing]}")
            _assert(by_id[missing]["kind"] is None,
                    "unhydrated metadata must stay null, not be guessed")
        _assert("abandoned_path" not in by_id[10]["roles"], by_id[10])
        print("PASS role_bearing_id_outside_evidence_still_gets_a_row")
    finally:
        gate.capture_streams.emit_gate_outcome_event = orig
        _cleanup(tmp, conn)


# ---------- 2. shipped-diff signal ----------

def _write_transcript(tmp: str, ts: str, file_path: str) -> str:
    """One Claude-Code-shaped transcript line containing an Edit tool_use."""
    p = Path(tmp) / "transcript.jsonl"
    p.write_text(json.dumps({
        "timestamp": ts,
        "message": {
            "content": [
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": file_path}},
            ],
        },
    }) + "\n", encoding="utf-8")
    return str(p)


def test_windowed_artifacts_keep_only_in_window_edits():
    tmp, conn = _fresh()
    try:
        repo = Path(tmp) / "repo"
        (repo / ".git").mkdir(parents=True)
        target = repo / "src" / "thing.py"
        target.parent.mkdir(parents=True)
        target.write_text("x = 1\n", encoding="utf-8")

        tpath = _write_transcript(
            tmp, "2026-05-25T12:10:00.000Z", str(target),
        )
        t0 = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
        inside = artifacts.observe_session_artifacts_in_window(
            tpath, str(repo), t0,
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
        )
        _assert(len(inside) == 1, f"in-window edit should be seen: {inside}")

        outside = artifacts.observe_session_artifacts_in_window(
            tpath, str(repo), t0,
            datetime(2026, 5, 25, 12, 5, tzinfo=timezone.utc),
        )
        _assert(outside == [], f"out-of-window edit must be skipped: {outside}")
        print("PASS windowed_artifacts_keep_only_in_window_edits")
    finally:
        _cleanup(tmp, conn)


def test_windowed_artifacts_skip_lines_without_timestamp():
    """An unparseable timestamp must not be counted as in-window — silently
    admitting it would inflate the shipped-diff signal."""
    tmp, conn = _fresh()
    try:
        repo = Path(tmp) / "repo"
        (repo / ".git").mkdir(parents=True)
        p = Path(tmp) / "t.jsonl"
        p.write_text(json.dumps({
            "message": {"content": [
                {"type": "tool_use", "name": "Write",
                 "input": {"file_path": str(repo / "a.py")}},
            ]},
        }) + "\n", encoding="utf-8")
        out = artifacts.observe_session_artifacts_in_window(
            str(p), str(repo),
            datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 26, 0, 0, tzinfo=timezone.utc),
        )
        _assert(out == [], f"timestamp-less line must be skipped: {out}")
        print("PASS windowed_artifacts_skip_lines_without_timestamp")
    finally:
        _cleanup(tmp, conn)


def test_failed_edit_does_not_count_as_shipped_code():
    """PR #71 review P1. An Edit whose tool_result is_error=true moved no code;
    counting it manufactures evidence that the ruling was ignored."""
    tmp, conn = _fresh()
    try:
        repo = Path(tmp) / "repo"
        (repo / ".git").mkdir(parents=True)
        p = Path(tmp) / "t.jsonl"
        p.write_text("\n".join([
            json.dumps({
                "timestamp": "2026-05-25T12:10:00.000Z",
                "message": {"content": [
                    {"type": "tool_use", "id": "toolu_bad", "name": "Edit",
                     "input": {"file_path": str(repo / "failed.py")}},
                ]},
            }),
            json.dumps({
                "timestamp": "2026-05-25T12:10:01.000Z",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "toolu_bad",
                     "is_error": True, "content": "String not found"},
                ]},
            }),
            json.dumps({
                "timestamp": "2026-05-25T12:11:00.000Z",
                "message": {"content": [
                    {"type": "tool_use", "id": "toolu_ok", "name": "Write",
                     "input": {"file_path": str(repo / "shipped.py")}},
                ]},
            }),
        ]) + "\n", encoding="utf-8")
        out = artifacts.observe_session_artifacts_in_window(
            str(p), str(repo),
            datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
        )
        rels = {row["path"] for row in out}
        _assert(rels == {"shipped.py"},
                f"only the successful edit should count, got {rels}")
        print("PASS failed_edit_does_not_count_as_shipped_code")
    finally:
        _cleanup(tmp, conn)


def test_codex_apply_patch_edits_are_seen():
    """PR #71 review P2. Codex ships edits as an apply_patch envelope, not a
    Claude-shaped tool_use. Missing it blinded the signal on most gate traffic."""
    tmp, conn = _fresh()
    try:
        repo = Path(tmp) / "repo"
        (repo / ".git").mkdir(parents=True)
        patch = (
            "*** Begin Patch\n"
            f"*** Update File: {repo / 'src' / 'mod.py'}\n"
            "@@\n-old\n+new\n"
            f"*** Add File: {repo / 'src' / 'added.py'}\n"
            "+fresh\n"
            "*** End Patch\n"
        )
        p = Path(tmp) / "codex.jsonl"
        p.write_text(json.dumps({
            "timestamp": "2026-05-25T12:10:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_ok",
                "name": "apply_patch",
                "input": patch,
            },
        }) + "\n", encoding="utf-8")
        out = artifacts.observe_session_artifacts_in_window(
            str(p), str(repo),
            datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
        )
        rels = {row["path"] for row in out}
        _assert(rels == {"src/mod.py", "src/added.py"},
                f"both patched files should be seen, got {rels}")
        print("PASS codex_apply_patch_edits_are_seen")
    finally:
        _cleanup(tmp, conn)


def test_failed_codex_apply_patch_does_not_count():
    tmp, conn = _fresh()
    try:
        repo = Path(tmp) / "repo"
        (repo / ".git").mkdir(parents=True)
        p = Path(tmp) / "codex.jsonl"
        p.write_text(json.dumps({
            "timestamp": "2026-05-25T12:10:00.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "failed",
                "call_id": "call_bad",
                "name": "apply_patch",
                "input": ("*** Begin Patch\n"
                          f"*** Update File: {repo / 'nope.py'}\n"),
            },
        }) + "\n", encoding="utf-8")
        out = artifacts.observe_session_artifacts_in_window(
            str(p), str(repo),
            datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
        )
        _assert(out == [], f"a failed patch must not count: {out}")
        print("PASS failed_codex_apply_patch_does_not_count")
    finally:
        _cleanup(tmp, conn)


def test_dedup_prefers_gate_call_id_over_hash_and_timestamp():
    """PR #71 review P1. Two distinct calls can share (query_hash, ts) — a
    MODIFY and its retry — so the nonce must be the join key when present."""
    same_hash_ts = {"gate_query_hash": "h", "gate_ts": "t",
                    "correlator_version": "0.3.0"}
    a = correlator._dedup_key({**same_hash_ts, "gate_call_id": "call-a"})
    b = correlator._dedup_key({**same_hash_ts, "gate_call_id": "call-b"})
    _assert(a != b, f"distinct nonces must not collapse: {a} == {b}")
    legacy_x = correlator._dedup_key(same_hash_ts)
    legacy_y = correlator._dedup_key(dict(same_hash_ts))
    _assert(legacy_x == legacy_y and legacy_x is not None,
            "pre-nonce rows still dedup on hash+ts")
    _assert(a != legacy_x, "nonce and legacy keys must not alias")
    _assert(correlator._dedup_key({"gate_call_id": "c"}) is None,
            "a row with no version cannot be keyed")
    print("PASS dedup_prefers_gate_call_id_over_hash_and_timestamp")


def test_modify_with_shipped_code_and_no_kb_write_is_overridden():
    tmp, conn = _fresh()
    try:
        outcome = correlator._classify(
            conn, {"recommendation": "MODIFY", "evidence_ids": []},
            "sid-x",
            datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
            file_touches=3,
        )
        _assert(outcome == "OVERRIDDEN",
                f"code shipped with no KB write is OVERRIDDEN, got {outcome}")
        print("PASS modify_with_shipped_code_and_no_kb_write_is_overridden")
    finally:
        _cleanup(tmp, conn)


def test_modify_with_no_activity_at_all_stays_ambiguous():
    """Founder ruling id=3985: no evidence stays AMBIGUOUS. Relabelling it
    UNRESOLVED would satisfy the <=20% bar by renaming, not by inferring."""
    tmp, conn = _fresh()
    try:
        outcome = correlator._classify(
            conn, {"recommendation": "MODIFY", "evidence_ids": []},
            "sid-x",
            datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
            file_touches=0,
        )
        _assert(outcome == "AMBIGUOUS",
                f"no evidence must stay AMBIGUOUS, got {outcome}")
        print("PASS modify_with_no_activity_at_all_stays_ambiguous")
    finally:
        _cleanup(tmp, conn)


def test_file_touches_is_zero_when_session_has_no_transcript():
    """Failure isolation: a missing transcript yields 0, never an exception."""
    tmp, conn = _fresh()
    try:
        count = correlator._count_file_touches(
            conn, "nonexistent-session", tmp,
            datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 25, 12, 30, tzinfo=timezone.utc),
        )
        _assert(count == 0, f"missing session must yield 0, got {count}")
        print("PASS file_touches_is_zero_when_session_has_no_transcript")
    finally:
        _cleanup(tmp, conn)


def test_correlator_version_bumped_past_prior_release():
    _assert(correlator.CORRELATOR_VERSION_DEFAULT == "0.3.0",
            f"version should be 0.3.0, got "
            f"{correlator.CORRELATOR_VERSION_DEFAULT}")
    print("PASS correlator_version_bumped_past_prior_release")


# ---------- 3. coverage metric ----------

def test_coverage_denominator_is_all_gate_rows():
    gates = [
        {"session_id": "a", "ts": "2026-05-25T12:00:00.000Z"},
        {"session_id": "b", "ts": "2026-05-25T12:01:00.000Z"},
        {"session_id": None, "ts": "2026-05-25T12:02:00.000Z"},
        {"session_id": None, "ts": "2026-05-25T12:03:00.000Z"},
    ]
    outcomes = [
        {"outcome_category": "ACCEPTED"}, {"outcome_category": "AMBIGUOUS"},
    ]
    cov = gate_report._coverage(gates, outcomes)
    _assert(cov["gate_rows"] == 4, cov)
    _assert(cov["labeled_rows"] == 2, cov)
    _assert(cov["unlabelable_no_session_id"] == 2, cov)
    _assert(cov["labelable_rows"] == 2, cov)
    _assert(cov["coverage_pct"] == 50.0, cov)
    _assert(cov["coverage_pct_of_labelable"] == 100.0, cov)
    _assert(cov["ambiguous_rows"] == 1, cov)
    _assert(cov["ambiguous_pct"] == 50.0, cov)
    print("PASS coverage_denominator_is_all_gate_rows")


def test_labelable_excludes_rows_the_correlator_refuses():
    """PR #71 review P2. Skipped verdicts and unparseable timestamps are
    refused outright, so counting them understates performance on real rows."""
    gates = [
        {"session_id": "a", "ts": "2026-05-25T12:00:00.000Z"},
        {"session_id": "b", "ts": "2026-05-25T12:01:00.000Z"},
        {"session_id": None, "ts": "2026-05-25T12:02:00.000Z"},
        {"session_id": "c", "ts": "2026-05-25T12:03:00.000Z", "skipped": True},
        {"session_id": "d", "ts": "not-a-timestamp"},
    ]
    outcomes = [{"outcome_category": "ACCEPTED"},
                {"outcome_category": "OVERRIDDEN"}]
    cov = gate_report._coverage(gates, outcomes)
    _assert(cov["gate_rows"] == 5, cov)
    _assert(cov["unlabelable_no_session_id"] == 1, cov)
    _assert(cov["unlabelable_skipped_verdict"] == 1, cov)
    _assert(cov["unlabelable_unparseable_ts"] == 1, cov)
    _assert(cov["labelable_rows"] == 2, cov)
    _assert(cov["coverage_pct_of_labelable"] == 100.0, cov)
    _assert(cov["coverage_pct"] == 40.0, cov)
    print("PASS labelable_excludes_rows_the_correlator_refuses")


def test_text_report_renders_the_coverage_block():
    """PR #71 review P2. A metric only visible under --json is invisible."""
    report = {
        "used": {"gate_rows": 4, "gate_outcome_rows": 1},
        "window": {"start": "2026-05-19", "end": "2026-05-25", "days": 7},
        "why_it_matters": "structural receipt",
        "verdict_counts": {"MODIFY": 4},
        "outcome_counts": {"AMBIGUOUS": 1},
        "outcome_by_verdict_counts": {},
        "adversary_delta_counts": {},
        "human_action_counts": {},
        "claim_signals": {"load_bearing_claims": 0, "uncovered_claims": 0,
                          "evidence_type_counts": {}, "gap_type_counts": {}},
        "top_evidence_nodes": [],
        "top_decision_chain_nodes": [],
        "priority_evidence": [],
        "coverage": {
            "gate_rows": 4, "labeled_rows": 1,
            "unlabelable_no_session_id": 2,
            "unlabelable_skipped_verdict": 1,
            "unlabelable_unparseable_ts": 0,
            "labelable_rows": 1, "coverage_pct": 25.0,
            "coverage_pct_of_labelable": 100.0,
            "ambiguous_rows": 1, "ambiguous_pct": 100.0,
        },
    }
    text = gate_report.format_text(report)
    for needle in ("Outcome Coverage", "25.0", "no session id: 2",
                   "skipped verdict: 1", "AMBIGUOUS"):
        _assert(needle in text, f"{needle!r} missing from report:\n{text}")
    _assert("unparseable timestamp" not in text,
            "a zero bucket should not be listed")
    print("PASS text_report_renders_the_coverage_block")


def test_coverage_reports_none_not_zero_on_empty_window():
    """"No data" must never render as "0%" — that reads as a failing bar."""
    cov = gate_report._coverage([], [])
    _assert(cov["coverage_pct"] is None, cov)
    _assert(cov["coverage_pct_of_labelable"] is None, cov)
    _assert(cov["ambiguous_pct"] is None, cov)
    _assert(cov["gate_rows"] == 0, cov)
    print("PASS coverage_reports_none_not_zero_on_empty_window")


def test_outcome_rows_deduped_to_highest_correlator_version():
    rows = [
        {"gate_query_hash": "h1", "gate_ts": "t1",
         "correlator_version": "0.2.0", "outcome_category": "AMBIGUOUS"},
        {"gate_query_hash": "h1", "gate_ts": "t1",
         "correlator_version": "0.3.0", "outcome_category": "OVERRIDDEN"},
        {"gate_query_hash": "h2", "gate_ts": "t2",
         "correlator_version": "0.2.0", "outcome_category": "ACCEPTED"},
    ]
    kept = gate_report._latest_version_only(rows)
    _assert(len(kept) == 2, f"one row per gate call expected: {kept}")
    by_hash = {r["gate_query_hash"]: r for r in kept}
    _assert(by_hash["h1"]["correlator_version"] == "0.3.0", kept)
    _assert(by_hash["h1"]["outcome_category"] == "OVERRIDDEN", kept)
    _assert(by_hash["h2"]["correlator_version"] == "0.2.0", kept)
    print("PASS outcome_rows_deduped_to_highest_correlator_version")


def test_report_dedup_keeps_distinct_calls_that_share_hash_and_timestamp():
    """PR #71 re-review P1. The correlator's join was fixed to prefer the nonce
    but the report's was not, so a MODIFY and its retry — same query_hash, same
    ts — still collapsed into one report row and undercounted gate calls."""
    rows = [
        {"gate_call_id": "call-a", "gate_query_hash": "h", "gate_ts": "t",
         "correlator_version": "0.3.0", "outcome_category": "OVERRIDDEN"},
        {"gate_call_id": "call-b", "gate_query_hash": "h", "gate_ts": "t",
         "correlator_version": "0.3.0", "outcome_category": "ACCEPTED"},
    ]
    kept = gate_report._latest_version_only(rows)
    _assert(len(kept) == 2,
            f"two distinct calls must stay two rows, got {len(kept)}: {kept}")
    _assert({r["gate_call_id"] for r in kept} == {"call-a", "call-b"}, kept)
    print("PASS report_dedup_keeps_distinct_calls_that_share_hash_and_timestamp")


def test_report_dedup_still_collapses_versions_of_one_call():
    """The nonce must not defeat the version collapse it was added beside."""
    rows = [
        {"gate_call_id": "call-a", "gate_query_hash": "h", "gate_ts": "t",
         "correlator_version": "0.2.0", "outcome_category": "AMBIGUOUS"},
        {"gate_call_id": "call-a", "gate_query_hash": "h", "gate_ts": "t",
         "correlator_version": "0.3.0", "outcome_category": "OVERRIDDEN"},
    ]
    kept = gate_report._latest_version_only(rows)
    _assert(len(kept) == 1, f"one call is one row: {kept}")
    _assert(kept[0]["correlator_version"] == "0.3.0", kept)
    print("PASS report_dedup_still_collapses_versions_of_one_call")


def test_report_and_correlator_agree_on_call_identity():
    """Writer and reader must define "one gate call" the same way, or the report
    silently disagrees with the log it is reporting on."""
    row = {"gate_call_id": "call-a", "gate_query_hash": "h", "gate_ts": "t",
           "correlator_version": "0.3.0"}
    legacy = {"gate_query_hash": "h", "gate_ts": "t",
              "correlator_version": "0.3.0"}
    for candidate in (row, legacy):
        report_key = gate_report._gate_call_identity(candidate)
        corr_key = correlator._dedup_key(candidate)
        _assert(report_key is not None and corr_key is not None, candidate)
        # The correlator appends the version; the report deliberately does not,
        # because collapsing versions is its whole job. The call-identity prefix
        # must match exactly.
        _assert(corr_key[:len(report_key)] == report_key,
                f"identity prefixes disagree: {corr_key} vs {report_key}")
    print("PASS report_and_correlator_agree_on_call_identity")


def test_version_dedup_order_independent():
    """The newer row must win whichever order the daily files are read in."""
    newer = {"gate_query_hash": "h", "gate_ts": "t",
             "correlator_version": "0.3.0"}
    older = {"gate_query_hash": "h", "gate_ts": "t",
             "correlator_version": "0.2.0"}
    for rows in ([newer, older], [older, newer]):
        kept = gate_report._latest_version_only(rows)
        _assert(len(kept) == 1, kept)
        _assert(kept[0]["correlator_version"] == "0.3.0", kept)
    print("PASS version_dedup_order_independent")


def test_version_dedup_keeps_unkeyed_rows():
    rows = [{"outcome_category": "ACCEPTED"}]
    _assert(len(gate_report._latest_version_only(rows)) == 1,
            "a row without a join key must not be silently dropped")
    print("PASS version_dedup_keeps_unkeyed_rows")


if __name__ == "__main__":
    test_gate_log_persists_all_verdict_id_lists()
    test_gate_log_id_lists_are_ints_and_carry_no_prose()
    test_gate_log_id_lists_present_even_when_empty()
    test_role_bearing_id_outside_evidence_still_gets_a_row()
    test_windowed_artifacts_keep_only_in_window_edits()
    test_windowed_artifacts_skip_lines_without_timestamp()
    test_modify_with_shipped_code_and_no_kb_write_is_overridden()
    test_modify_with_no_activity_at_all_stays_ambiguous()
    test_file_touches_is_zero_when_session_has_no_transcript()
    test_correlator_version_bumped_past_prior_release()
    test_failed_edit_does_not_count_as_shipped_code()
    test_codex_apply_patch_edits_are_seen()
    test_failed_codex_apply_patch_does_not_count()
    test_dedup_prefers_gate_call_id_over_hash_and_timestamp()
    test_labelable_excludes_rows_the_correlator_refuses()
    test_text_report_renders_the_coverage_block()
    test_coverage_denominator_is_all_gate_rows()
    test_coverage_reports_none_not_zero_on_empty_window()
    test_outcome_rows_deduped_to_highest_correlator_version()
    test_report_dedup_keeps_distinct_calls_that_share_hash_and_timestamp()
    test_report_dedup_still_collapses_versions_of_one_call()
    test_report_and_correlator_agree_on_call_identity()
    test_version_dedup_order_independent()
    test_version_dedup_keeps_unkeyed_rows()
    print("ALL V1 INSTRUMENTATION TESTS PASSED")
