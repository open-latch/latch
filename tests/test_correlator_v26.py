"""Integrated v2.6 boundary and receipt-generation regressions.

The row shapes mirror the structural gate/rollout records used by PR #73; no
prompt text or private path is part of an asserted output.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys

import pytest


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from latch.proof import correlator  # noqa: E402
from latch.proof import correlator_cli  # noqa: E402
from latch.store import db  # noqa: E402
from latch.evals import outcome_measurement  # noqa: E402
from latch.store import paths  # noqa: E402


DAY = date(2026, 7, 30)
DAY_TEXT = DAY.isoformat()
CLAUDE_FIXTURE = (
    Path(__file__).parent
    / "fixtures/outcome_measurement/claude-transcript-2026-07-22.sanitized.jsonl"
)


def _gate_row(
    project: Path,
    *,
    ts: str,
    call_id: str,
    query_hash: str,
    session_id: str | None = None,
    skipped: bool = False,
) -> dict:
    return {
        "ts": ts,
        "project": paths.sanitize_cwd(str(project)),
        "session_id": session_id,
        "event_type": "gate",
        "gate_call_id": call_id,
        "query_hash": query_hash,
        "recommendation": None if skipped else "PROCEED",
        "skipped": skipped,
        "error": "disabled" if skipped else None,
        "evidence_ids": [],
        "decision_chain": [],
    }


def _write_rows(project: Path, rows: list[dict]) -> None:
    conn = db.connect(str(project))
    conn.close()
    target = paths.project_dir(str(project)) / f"gate-{DAY_TEXT}.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _outcomes(project: Path) -> list[dict]:
    target = paths.project_dir(str(project)) / f"gate_outcome-{DAY_TEXT}.log"
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text().splitlines() if line]


def _install_claude_session(
    project: Path,
    *,
    session_id: str,
    calls: list[tuple[str, str, dict]],
) -> Path:
    """Mutate only values in the sanitized corpus-derived Claude shape."""
    seed = [json.loads(line) for line in CLAUDE_FIXTURE.read_text().splitlines()]
    rows: list[dict] = []
    for index, (tool_id, ts, result) in enumerate(calls):
        tool_use = json.loads(json.dumps(seed[0]))
        tool_result = json.loads(json.dumps(seed[1]))
        tool_use.update({"timestamp": ts, "cwd": str(project), "sessionId": session_id})
        tool_result.update({"timestamp": ts, "cwd": str(project), "sessionId": session_id})
        use_block = tool_use["message"]["content"][0]
        use_block.update({"id": tool_id, "name": "mcp__latch__latch_gate"})
        result_block = tool_result["message"]["content"][0]
        result_block["tool_use_id"] = tool_id
        result_block["content"] = [{"type": "text", "text": json.dumps(result)}]
        rows.extend((tool_use, tool_result))
    transcript = project / f"claude-{session_id}.jsonl"
    transcript.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    conn = db.connect(str(project))
    try:
        db.upsert_session(
            conn, session_id, str(project), transcript_path=str(transcript),
        )
    finally:
        conn.close()
    return transcript


def _install_index(
    monkeypatch: pytest.MonkeyPatch,
    *,
    by_call: dict[str, dict] | None = None,
    by_session: dict[str, list[dict]] | None = None,
) -> None:
    by_call = by_call or {}
    normalized_sessions = {
        session_id: [
            {
                **call,
                "project_check": "match",
                "source_order": (0, index),
            }
            for index, call in enumerate(calls)
        ]
        for session_id, calls in (by_session or {}).items()
    }
    index = {
        "by_nonce": {},
        "by_session": normalized_sessions,
        "candidate_completeness": {"complete": True},
    }
    monkeypatch.setattr(
        correlator.codex_attribution,
        "build_index",
        lambda *_args, **_kwargs: index,
    )
    monkeypatch.setattr(
        correlator.codex_attribution,
        "attribute",
        lambda row, *_args, **_kwargs: by_call.get(row.get("gate_call_id")),
    )
    monkeypatch.setattr(correlator, "_count_file_touches", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        correlator,
        "_host_session_stream",
        lambda *_a, **_k: {
            "transcript_path": None,
            "calls": [],
            "by_nonce": {},
            "complete": True,
            "loss_markers": [],
        },
    )


def _hit(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "transcript_path": "/nonexistent/sanitized-rollout.jsonl",
        "source": "codex_transcript_nonce",
        "project_check": "match",
        "source_order": (0, 0),
        "host_observation": {
            "nonce": "synthetic",
            "ts": None,
            "session_id": session_id,
        },
    }


def test_host_supplied_candidate_conflict_uses_full_s2_semantics() -> None:
    base = outcome_measurement.Observation(
        source=outcome_measurement.SOURCE_HOST,
        file="sanitized-rollout.jsonl",
        byte_offset=10,
        nonce="aaaaaaaaaaaa",
        ts=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        session_id="session-a",
        adapter="claude",
        verdict="PROCEED",
        verdict_id_lists={"evidence_ids": [4164]},
    )
    changed = replace(
        base,
        byte_offset=20,
        verdict="MODIFY",
        verdict_id_lists={"evidence_ids": [4179]},
    )
    proof = {
        "version": "project-proof-v1",
        "key_epoch": "epoch-1",
        "fingerprint": "a" * 64,
    }
    candidates = [
        {
            "session_id": "session-a",
            "ts": row.ts,
            "project_proof": proof,
            "host_observation": correlator._host_observation_payload(row),
        }
        for row in (base, changed)
    ]
    reasons = correlator.codex_attribution._candidate_set_conflicts(candidates)
    assert "nonidentical_nonce_candidate" in reasons


def test_same_project_unknown_at_391s_does_not_truncate_or_censor_recovered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
    )
    unknown = _gate_row(
        tmp_path,
        ts="2026-07-30T12:06:31.000Z",
        call_id="bbbbbbbbbbbb",
        query_hash="hash-b",
    )
    _write_rows(tmp_path, [first, unknown])
    _install_index(
        monkeypatch,
        by_call={first["gate_call_id"]: _hit("session-a")},
        by_session={"session-a": [first]},
    )

    counts = correlator.correlate(str(tmp_path), DAY, DAY)
    rows = _outcomes(tmp_path)
    assert counts["rows_unknown_session_ignored_for_boundary"] == 1
    assert len(rows) == 1
    assert rows[0]["window_seconds"] == 1800
    assert rows[0]["window_boundary_uncertain"] is False
    assert rows[0]["outcome_category"] != "CENSORED"


def test_recovered_attribution_forwards_and_persists_scoped_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
    )
    _write_rows(tmp_path, [gate])
    _install_index(
        monkeypatch,
        by_session={"session-a": [gate]},
    )
    scoped_receipt = correlator.codex_attribution._with_receipt_hash({
        "version": "codex-rollout-full-v3",
        "complete": True,
        "measurement_window": {
            "start": "2026-07-30T12:00:00.000Z",
            "end_inclusive": "2026-07-30T12:15:00.000Z",
            "window_seconds": 900,
        },
        "waived_defects": [{
            "defect_id": "D000001",
            "kind": "missing_tool_results",
            "reason": "proven_disjoint_interval",
        }],
    })
    observed_windows: list[int | None] = []

    def scoped_attribute(row, _index, **kwargs):
        observed_windows.append(kwargs.get("window_seconds"))
        return {
            **_hit("session-a"),
            "candidate_completeness": scoped_receipt,
        }

    monkeypatch.setattr(
        correlator.codex_attribution,
        "attribute",
        scoped_attribute,
    )

    correlator.correlate(str(tmp_path), DAY, DAY, window_seconds=900)
    rows = _outcomes(tmp_path)
    assert observed_windows == [900]
    assert len(rows) == 1
    assert rows[0]["candidate_index_complete"] is True
    assert rows[0]["candidate_completeness_receipt"] == scoped_receipt
    assert correlator.codex_attribution._receipt_hash_matches(
        rows[0]["candidate_completeness_receipt"]
    )


@pytest.mark.parametrize("invalid_window", [-1, True, 1.5, 10**100])
def test_correlator_rejects_invalid_window_before_identity_branching(
    tmp_path: Path,
    invalid_window,
) -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        correlator.correlate(
            str(tmp_path),
            DAY,
            DAY,
            window_seconds=invalid_window,
        )


@pytest.mark.parametrize("invalid_window", ["-1", str(10**100)])
def test_correlator_cli_rejects_invalid_window_as_argv_error(
    tmp_path: Path,
    invalid_window: str,
) -> None:
    assert correlator_cli.main([
        "kb-correlate",
        "--project", str(tmp_path),
        "--start", DAY_TEXT,
        "--end", DAY_TEXT,
        "--window", invalid_window,
    ]) == 2


def test_skipped_recovered_call_is_non_emitting_exact_session_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
    )
    skipped = _gate_row(
        tmp_path,
        ts="2026-07-30T12:10:00.000Z",
        call_id="bbbbbbbbbbbb",
        query_hash="hash-b",
        skipped=True,
    )
    _write_rows(tmp_path, [first, skipped])
    _install_index(
        monkeypatch,
        by_call={
            first["gate_call_id"]: _hit("session-a"),
            skipped["gate_call_id"]: _hit("session-a"),
        },
        by_session={"session-a": [first, skipped]},
    )

    counts = correlator.correlate(str(tmp_path), DAY, DAY)
    rows = _outcomes(tmp_path)
    assert len(rows) == 1
    assert rows[0]["gate_call_id"] == first["gate_call_id"]
    assert rows[0]["window_seconds"] == 600
    assert counts["rows_skipped_skipped_verdict"] == 1
    assert counts["rows_skipped_boundary_capable"] == 1
    assert counts["rows_skipped_attributed_from_transcript"] == 1


def test_unmatched_transcript_call_bounds_recovered_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
    )
    unmatched = {
        "ts": "2026-07-30T12:05:00.000Z",
        "gate_call_id": "cccccccccccc",
    }
    _write_rows(tmp_path, [first])
    _install_index(
        monkeypatch,
        by_call={first["gate_call_id"]: _hit("session-a")},
        by_session={"session-a": [first, unmatched]},
    )

    correlator.correlate(str(tmp_path), DAY, DAY)
    rows = _outcomes(tmp_path)
    assert len(rows) == 1
    assert rows[0]["window_seconds"] == 300
    assert rows[0]["boundary_source"] == "same_session_s1_s2_union"


def test_host_window_stays_clean_across_foreign_and_unknown_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
        session_id="host-session",
    )
    foreign = _gate_row(
        tmp_path,
        ts="2026-07-30T12:06:31.000Z",
        call_id="bbbbbbbbbbbb",
        query_hash="hash-b",
        session_id="foreign-session",
    )
    unknown = _gate_row(
        tmp_path,
        ts="2026-07-30T12:07:00.000Z",
        call_id="cccccccccccc",
        query_hash="hash-c",
    )
    _write_rows(tmp_path, [host, foreign, unknown])
    _install_index(monkeypatch)

    correlator.correlate(str(tmp_path), DAY, DAY)
    rows = {row["gate_call_id"]: row for row in _outcomes(tmp_path)}
    assert rows[host["gate_call_id"]]["window_seconds"] == 1800
    assert rows[host["gate_call_id"]]["window_boundary_uncertain"] is False


def test_claude_s2_nonce_join_carries_and_compares_tool_result_metadata(
    tmp_path: Path,
) -> None:
    session_id = "claude-session-a"
    conn = db.connect(str(tmp_path))
    try:
        context = correlator.project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch=correlator.PROJECT_KEY_EPOCH_DEFAULT,
        )
        proof = context.prove(str(tmp_path))
    finally:
        conn.close()
    result = {
        "gate_call_id": "aaaaaaaaaaaa",
        "skipped": False,
        "measurement_protocol_version": "outcome-v2.6.0",
        "runtime_version": "runtime-pinned",
        "attestation": "runtime-pinned",
        "runtime_attestation": "runtime-pinned",
        "key_epoch": correlator.PROJECT_KEY_EPOCH_DEFAULT,
        "project_proof": proof,
    }
    _install_claude_session(
        tmp_path,
        session_id=session_id,
        calls=[("toolu_gate_a", "2026-07-30T12:00:00.000Z", result)],
    )
    gate = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
        session_id=session_id,
    )
    gate.update({
        "measurement_protocol_version": "outcome-v2.6.0",
        "runtime_version": "runtime-pinned",
        "attestation": "runtime-pinned",
        "runtime_attestation": "runtime-pinned",
        "key_epoch": correlator.PROJECT_KEY_EPOCH_DEFAULT,
        "project_proof": proof,
    })
    _write_rows(tmp_path, [gate])

    correlator.correlate(
        str(tmp_path), DAY, DAY, pinned_runtime_version="runtime-pinned",
    )
    row = _outcomes(tmp_path)[0]
    assert row["diagnostic_disposition"] == "confirmatory"
    assert row["candidate_index_complete"] is True
    assert "measurement_protocol_version" not in row
    assert "receipt_identity" not in row


def test_mixed_s1_s2_key_epoch_is_loss_never_conflict(tmp_path: Path) -> None:
    session_id = "claude-session-rotated"
    conn = db.connect(str(tmp_path))
    try:
        current = correlator.project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch=correlator.PROJECT_KEY_EPOCH_DEFAULT,
        )
        rotated = correlator.project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch="outcome-v2.6-key-rotated",
        )
        host_proof = current.prove(str(tmp_path))
        gate_proof = rotated.prove(str(tmp_path))
    finally:
        conn.close()
    result = {
        "gate_call_id": "aaaaaaaaaaaa",
        "skipped": False,
        "measurement_protocol_version": "outcome-v2.6.0",
        "runtime_version": "runtime-pinned",
        "attestation": "runtime-pinned",
        "runtime_attestation": "runtime-pinned",
        "key_epoch": correlator.PROJECT_KEY_EPOCH_DEFAULT,
        "project_proof": host_proof,
    }
    _install_claude_session(
        tmp_path,
        session_id=session_id,
        calls=[("toolu_gate_a", "2026-07-30T12:00:00.000Z", result)],
    )
    gate = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
        session_id=session_id,
    )
    gate.update({
        "measurement_protocol_version": "outcome-v2.6.0",
        "runtime_version": "runtime-pinned",
        "attestation": "runtime-pinned",
        "runtime_attestation": "runtime-pinned",
        "key_epoch": "outcome-v2.6-key-rotated",
        "project_proof": gate_proof,
    })
    _write_rows(tmp_path, [gate])

    correlator.correlate(
        str(tmp_path), DAY, DAY, pinned_runtime_version="runtime-pinned",
    )
    row = _outcomes(tmp_path)[0]
    assert row["diagnostic_disposition"] == "loss_signal"
    assert row["diagnostic_loss_reasons"] == ["key_epoch_mismatch"]


def test_same_timestamp_later_claude_stream_coordinate_bounds_window(
    tmp_path: Path,
) -> None:
    session_id = "claude-session-tie"
    base = {"skipped": False}
    _install_claude_session(
        tmp_path,
        session_id=session_id,
        calls=[
            ("toolu_gate_a", "2026-07-30T12:00:00.000Z", {
                **base, "gate_call_id": "aaaaaaaaaaaa",
            }),
            ("toolu_gate_b", "2026-07-30T12:00:00.000Z", {
                **base, "gate_call_id": "bbbbbbbbbbbb",
            }),
        ],
    )
    gate = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
        session_id=session_id,
    )
    _write_rows(tmp_path, [gate])
    correlator.correlate(str(tmp_path), DAY, DAY)
    assert _outcomes(tmp_path)[0]["window_seconds"] == 0


def test_exact_session_gate_only_nonce_censors_preceding_window(
    tmp_path: Path,
) -> None:
    session_id = "claude-session-gate-only"
    _install_claude_session(
        tmp_path,
        session_id=session_id,
        calls=[("toolu_gate_a", "2026-07-30T12:00:00.000Z", {
            "gate_call_id": "aaaaaaaaaaaa", "skipped": False,
        })],
    )
    first = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
        session_id=session_id,
    )
    gate_only = _gate_row(
        tmp_path,
        ts="2026-07-30T12:06:31.000Z",
        call_id="bbbbbbbbbbbb",
        query_hash="hash-b",
        session_id=session_id,
    )
    _write_rows(tmp_path, [first, gate_only])
    counts = correlator.correlate(str(tmp_path), DAY, DAY)
    rows = {row["gate_call_id"]: row for row in _outcomes(tmp_path)}
    assert counts["rows_gate_only_boundary_loss"] == 1
    assert rows["aaaaaaaaaaaa"]["window_seconds"] == 391
    assert rows["aaaaaaaaaaaa"]["outcome_category"] == "CENSORED"
    assert rows["aaaaaaaaaaaa"]["censor_reason"] == "boundary_uncertain"


def test_legacy_correlator_dedups_by_implementation_not_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _gate_row(
        tmp_path,
        ts="2026-07-30T12:00:00.000Z",
        call_id="aaaaaaaaaaaa",
        query_hash="hash-a",
        session_id="host-session",
    )
    _write_rows(tmp_path, [row])
    _install_index(monkeypatch)

    first = correlator.correlate(
        str(tmp_path), DAY, DAY,
        measurement_protocol_version="outcome-v2.5.0",
    )
    second = correlator.correlate(
        str(tmp_path), DAY, DAY,
        measurement_protocol_version="outcome-v2.6.0",
    )
    duplicate = correlator.correlate(
        str(tmp_path), DAY, DAY,
        measurement_protocol_version="outcome-v2.6.0",
    )

    assert first["rows_emitted"] == 1
    assert second["rows_emitted"] == 0
    assert duplicate["rows_emitted"] == 0
    rows = _outcomes(tmp_path)
    assert len(rows) == 1
    assert "measurement_protocol_version" not in rows[0]
    assert "receipt_identity" not in rows[0]
    assert "measurement_eligible" not in rows[0]
