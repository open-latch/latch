"""Tests for src/capture_streams.py — structural-only adversary.log /
decision.log emit helpers.

Scope: KB id=1343 (adversarial gate + decision-capture). Conventions:
id=1108 / id=1091 (common header, structural-only invariant, daily files).
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
import log_utils        # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp_proj():
    return tempfile.mkdtemp(prefix="kb_capture_test_")


def _today():
    return datetime.now(timezone.utc).date()


def _read(stream, tmp):
    return list(log_utils.read_log_range(stream, _today(), _today(), tmp))


# Header keys emit_event prepends to every row (id=1108 common header).
_HEADER = {"ts", "project", "session_id", "event_type"}

# Field names that would imply leaked semantic content. By construction the
# emit helpers have no param that could populate any of these — this set pins
# that guarantee against future edits.
_FORBIDDEN = {
    "title", "body", "node_title", "src_title", "dst_title",
    "query_text", "query_excerpt", "raw_request", "prompt",
    "objection", "question", "claim", "description", "reason", "summary",
}

DEFAULT_SID = "11111111-1111-1111-1111-111111111111"


def test_adversary_row_has_expected_schema():
    tmp = _tmp_proj()
    try:
        capture_streams.emit_adversary_event(
            verdict_before="PROCEED", verdict_delta="MODIFY",
            counter_node_id=856, n_forks_raised=2, latency_ms=1234,
            query_hash="abc12345def0", tokens=512, backend="codex",
            project_path=tmp, session_id=DEFAULT_SID,
        )
        rows = _read("adversary", tmp)
        _assert(len(rows) == 1, f"expected 1 row, got {len(rows)}")
        r = rows[0]
        expected = {
            "verdict_before", "verdict_delta", "counter_node_id",
            "n_forks_raised", "latency_ms", "query_hash", "tokens", "mode",
            "backend",
        } | _HEADER
        _assert(set(r.keys()) == expected,
                f"schema mismatch, symmetric diff: {set(r.keys()) ^ expected}")
        _assert(r["mode"] == "counter_node", f"default mode wrong: {r['mode']}")
        _assert(r["event_type"] == "adversary", "event_type wrong")
        _assert(r["session_id"] == DEFAULT_SID, "session_id not threaded")
        _assert(r["counter_node_id"] == 856, "counter_node_id wrong")
        _assert(r["verdict_delta"] == "MODIFY", "verdict_delta wrong")
        _assert(r["backend"] == "codex", "backend wrong")
        print("PASS adversary_row_has_expected_schema")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_decision_row_has_expected_schema():
    tmp = _tmp_proj()
    try:
        capture_streams.emit_decision_event(
            node_ids=[1343, 1279], confidence_tier="explicit_user",
            provenance="adversary_fork", was_confirmed=True,
            human_action="override", query_hash="def67890abc1",
            project_path=tmp, session_id=DEFAULT_SID,
        )
        rows = _read("decision", tmp)
        _assert(len(rows) == 1, f"expected 1 row, got {len(rows)}")
        r = rows[0]
        expected = {
            "node_ids", "confidence_tier", "provenance",
            "was_confirmed", "human_action", "query_hash",
        } | _HEADER
        _assert(set(r.keys()) == expected,
                f"schema mismatch, symmetric diff: {set(r.keys()) ^ expected}")
        _assert(r["node_ids"] == [1343, 1279], "node_ids wrong")
        _assert(r["was_confirmed"] is True, "was_confirmed wrong")
        _assert(r["provenance"] == "adversary_fork", "provenance wrong")
        _assert(r["human_action"] == "override", "human_action wrong")
        print("PASS decision_row_has_expected_schema")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_forbidden_fields_and_no_content_leak():
    tmp = _tmp_proj()
    try:
        capture_streams.emit_adversary_event(
            verdict_before="PROCEED", verdict_delta="none",
            counter_node_id=None, n_forks_raised=0, latency_ms=10,
            query_hash="abc12345def0", project_path=tmp, session_id=DEFAULT_SID,
        )
        capture_streams.emit_decision_event(
            node_ids=[1], confidence_tier="agent_inferred",
            provenance="inline_capture", was_confirmed=False,
            project_path=tmp, session_id=DEFAULT_SID,
        )
        for stream in ("adversary", "decision"):
            rows = _read(stream, tmp)
            _assert(rows, f"{stream}: no rows read back")
            for r in rows:
                leaks = set(r.keys()) & _FORBIDDEN
                _assert(not leaks,
                        f"forbidden fields leaked in {stream}: {leaks}")
        print("PASS no_forbidden_fields_and_no_content_leak")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_none_and_default_values_preserved():
    # cite-or-PROCEED: no citable counter → counter_node_id None survives the
    # JSON round-trip; query_hash / tokens default to None.
    tmp = _tmp_proj()
    try:
        capture_streams.emit_adversary_event(
            verdict_before="PROCEED", verdict_delta="none",
            counter_node_id=None, n_forks_raised=1, latency_ms=5,
            project_path=tmp, session_id=DEFAULT_SID,
        )
        r = _read("adversary", tmp)[0]
        _assert(r["counter_node_id"] is None, "None counter_node_id not preserved")
        _assert(r["query_hash"] is None, "default query_hash should be None")
        _assert(r["tokens"] is None, "default tokens should be None")
        _assert(r["backend"] is None, "default backend should be None")

        # Type-2 inferred, signal-only (no node materialized): node_ids == [].
        capture_streams.emit_decision_event(
            node_ids=[], confidence_tier="agent_inferred",
            provenance="inline_capture", was_confirmed=False,
            project_path=tmp, session_id=DEFAULT_SID,
        )
        d = _read("decision", tmp)[0]
        _assert(d["node_ids"] == [], "empty node_ids (signal-only) not preserved")
        _assert(d["was_confirmed"] is False, "was_confirmed should be False")
        _assert(d["human_action"] is None, "default human_action should be None")
        print("PASS none_and_default_values_preserved")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_closed_set_constants():
    _assert(capture_streams.VERDICT_DELTAS == ("none", "MODIFY", "DO_NOT_PROCEED"),
            "VERDICT_DELTAS drift")
    _assert(set(capture_streams.CONFIDENCE_TIERS) ==
            {"explicit_user", "agent_confirmed", "agent_inferred"},
            "CONFIDENCE_TIERS drift")
    _assert(set(capture_streams.DECISION_PROVENANCES) ==
            {"adversary_fork", "gate_question", "inline_capture"},
            "DECISION_PROVENANCES drift")
    _assert(set(capture_streams.HUMAN_ACTIONS) ==
            {"approve", "modify", "reject", "override"},
            "HUMAN_ACTIONS drift")
    _assert(set(capture_streams.ADVERSARY_MODES) ==
            {"counter_node"},
            "ADVERSARY_MODES drift")
    # Must mirror gate.CLASSIFIER_LABELS (kept local to avoid importing gate).
    _assert(capture_streams.VERDICT_LABELS ==
            ("PROCEED", "MODIFY", "DO_NOT_PROCEED", "NEEDS_HUMAN_JUDGMENT"),
            "VERDICT_LABELS drift vs gate.CLASSIFIER_LABELS")
    print("PASS closed_set_constants")


if __name__ == "__main__":
    test_adversary_row_has_expected_schema()
    test_decision_row_has_expected_schema()
    test_no_forbidden_fields_and_no_content_leak()
    test_none_and_default_values_preserved()
    test_closed_set_constants()
    print("ALL capture_streams tests passed")
