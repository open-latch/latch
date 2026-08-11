"""Phase 1 structural outcome-event substrate tests."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import multiprocessing
import os
import sqlite3
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import capture_streams  # noqa: E402
import gate             # noqa: E402
import log_utils        # noqa: E402
import mcp_broker       # noqa: E402
import paths            # noqa: E402


_HEADER = {"ts", "project", "session_id", "event_type"}
_FORBIDDEN_KEYS = {
    "title",
    "body",
    "query_text",
    "query_excerpt",
    "raw_request",
    "request_text",
    "prompt",
    "claim",
    "description",
    "reason",
    "summary",
    "decision_text",
}


@pytest.fixture
def local_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    vault = tmp_path / "vault"
    monkeypatch.setattr(paths, "project_dir", lambda _cwd=None: vault)
    monkeypatch.delenv("LATCH_OUTCOME_EVENTS", raising=False)
    capture_streams._OUTCOME_SETTINGS_CACHE.clear()
    return vault


def _rows(vault: Path, stream: str = capture_streams.OUTCOME_STREAM) -> list[dict]:
    path = vault / f"{stream}-{log_utils._today_utc_date()}.log"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _settings(vault: Path, data: object) -> Path:
    vault.mkdir(parents=True, exist_ok=True)
    path = vault / paths.VAULT_RUNTIME_SETTINGS_FILENAME
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_policy_is_default_on_with_explicit_process_and_vault_opt_out(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert capture_streams.outcome_events_enabled("project") is True

    monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "0")
    assert capture_streams.outcome_events_enabled("project") is False
    monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "1")
    assert capture_streams.outcome_events_enabled("project") is True
    monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "true")
    assert capture_streams.outcome_events_enabled("project") is False

    monkeypatch.delenv("LATCH_OUTCOME_EVENTS")
    _settings(local_vault, {"outcome_events": False})
    assert capture_streams.outcome_events_enabled("project") is False
    monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "1")
    assert capture_streams.outcome_events_enabled("project") is True
    monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "0")
    _settings(local_vault, {"outcome_events": True})
    assert capture_streams.outcome_events_enabled("project") is False
    monkeypatch.delenv("LATCH_OUTCOME_EVENTS")
    _settings(local_vault, {"outcome_events": True})
    assert capture_streams.outcome_events_enabled("project") is True
    _settings(local_vault, {"unrelated": "preserved"})
    assert capture_streams.outcome_events_enabled("project") is True


def test_policy_changes_are_seen_without_reimport(
    local_vault: Path,
) -> None:
    settings = _settings(local_vault, {"outcome_events": False})
    assert capture_streams.outcome_events_enabled("project") is False

    settings.write_text('{"outcome_events": true}', encoding="utf-8")
    previous_ns = settings.stat().st_mtime_ns
    os.utime(settings, ns=(previous_ns + 1_000_000, previous_ns + 1_000_000))
    assert capture_streams.outcome_events_enabled("project") is True


@pytest.mark.parametrize("data", ["not-json", "[]", '{"outcome_events": "false"}'])
def test_invalid_vault_policy_fails_closed_without_output(
    local_vault: Path,
    capsys: pytest.CaptureFixture[str],
    data: str,
) -> None:
    local_vault.mkdir(parents=True, exist_ok=True)
    (local_vault / paths.VAULT_RUNTIME_SETTINGS_FILENAME).write_text(
        data,
        encoding="utf-8",
    )
    assert capture_streams.outcome_events_enabled("project") is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_unreadable_vault_policy_fails_closed_without_output(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings(local_vault, {"outcome_events": True})
    original_read_text = Path.read_text

    def fail_for_settings(self: Path, *args, **kwargs):
        if self == settings:
            raise PermissionError("denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_settings)
    assert capture_streams.outcome_events_enabled("project") is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.skipif(os.name == "nt", reason="symlink policy differs on Windows")
def test_symlinked_vault_policy_fails_closed(local_vault: Path) -> None:
    local_vault.mkdir(parents=True, exist_ok=True)
    target = local_vault / "policy-target.json"
    target.write_text('{"outcome_events": true}', encoding="utf-8")
    (local_vault / paths.VAULT_RUNTIME_SETTINGS_FILENAME).symlink_to(target)
    assert capture_streams.outcome_events_enabled("project") is False


def test_capture_emits_neutral_action_and_link_rows(local_vault: Path) -> None:
    capture_streams.emit_decision_event(
        node_ids=[41, 42],
        confidence_tier="explicit_user",
        provenance="gate_question",
        was_confirmed=True,
        human_action="approve",
        query_hash="abcdef123456",
        project_path="project",
        session_id="session-1",
    )

    decision_rows = _rows(local_vault, capture_streams.DECISION_STREAM)
    assert len(decision_rows) == 1
    assert decision_rows[0]["node_ids"] == [41, 42]

    rows = _rows(local_vault)
    assert [row["row_kind"] for row in rows] == [
        "capture_action",
        "decision_capture_link",
    ]
    assert all(row["event_type"] == "outcome_event" for row in rows)
    assert all(row["events_version"] == "1" for row in rows)
    assert rows[0]["human_action"] == "approve"
    assert rows[0]["decision_node_ids"] == [41, 42]
    assert rows[1]["decision_node_ids"] == [41, 42]
    assert rows[0]["query_hash"] == rows[1]["query_hash"] == "abcdef123456"
    assert set(rows[0]) == _HEADER | {
        "events_version",
        "row_kind",
        "query_hash",
        "human_action",
        "confidence_tier",
        "provenance",
        "was_confirmed",
        "decision_node_ids",
    }
    assert set(rows[1]) == _HEADER | {
        "events_version",
        "row_kind",
        "decision_node_ids",
        "query_hash",
        "provenance",
    }


def test_capture_normalizes_unanchored_hashes_and_empty_signals(
    local_vault: Path,
) -> None:
    empty_hash = hashlib.sha1(b"").hexdigest()[:12]
    capture_streams.emit_decision_event(
        node_ids=[7],
        confidence_tier="agent_confirmed",
        provenance="inline_capture",
        was_confirmed=True,
        query_hash=empty_hash,
        project_path="project",
    )
    assert _rows(local_vault)[0]["query_hash"] is None

    before = len(_rows(local_vault))
    capture_streams.emit_decision_event(
        node_ids=[],
        confidence_tier="explicit_user",
        provenance="gate_question",
        was_confirmed=True,
        human_action="approve",
        query_hash="abcdef123456",
        project_path="project",
    )
    assert len(_rows(local_vault)) == before + 1
    assert _rows(local_vault)[-1]["row_kind"] == "capture_action"
    assert _rows(local_vault)[-1]["decision_node_ids"] == []

    before = len(_rows(local_vault))
    capture_streams.emit_decision_event(
        node_ids=[],
        confidence_tier="agent_inferred",
        provenance="inline_capture",
        was_confirmed=False,
        query_hash="not-a-hash",
        project_path="project",
    )
    assert len(_rows(local_vault)) == before


def test_whitespace_only_request_hash_is_unanchored(local_vault: Path) -> None:
    empty_hash = hashlib.sha1(b"").hexdigest()[:12]
    assert gate._query_hash(" \t\r\n") == empty_hash
    capture_streams.emit_decision_event(
        node_ids=[8],
        confidence_tier="explicit_user",
        provenance="gate_question",
        was_confirmed=True,
        human_action="approve",
        query_hash=gate._query_hash(" \t\r\n"),
        project_path="project",
    )
    assert all(row["query_hash"] is None for row in _rows(local_vault))


def test_process_opt_out_creates_no_outcome_file(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "0")
    capture_streams.emit_decision_event(
        node_ids=[1],
        confidence_tier="explicit_user",
        provenance="gate_question",
        was_confirmed=True,
        human_action="modify",
        query_hash="abcdef123456",
        project_path="project",
    )
    assert len(_rows(local_vault, capture_streams.DECISION_STREAM)) == 1
    assert _rows(local_vault) == []


def test_capture_outcome_failure_cannot_suppress_decision_log(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(**_kwargs):
        raise OSError("outcome writer failed")

    monkeypatch.setattr(capture_streams, "_emit_capture_outcomes", fail)
    capture_streams.emit_decision_event(
        node_ids=[1],
        confidence_tier="explicit_user",
        provenance="gate_question",
        was_confirmed=True,
        human_action="approve",
        query_hash="abcdef123456",
        project_path="project",
    )
    assert len(_rows(local_vault, capture_streams.DECISION_STREAM)) == 1
    assert _rows(local_vault) == []


def test_gate_writer_allowlists_schema_and_content(local_vault: Path) -> None:
    capture_streams.emit_gate_outcome_event(
        gate_call_id="012345abcdef",
        query_hash="abcdef123456",
        verdict="PROCEED",
        skipped=False,
        timed_out=False,
        error_present=False,
        backend="codex",
        adversary={
            "verdict_delta": "MODIFY",
            "counter_node_id": 9,
            "n_forks": 2,
            "summary": "DO_NOT_SERIALIZE",
        },
        assembled_nodes=[
            {
                "node_id": 9,
                "source": "hybrid",
                "position": 0,
                "cited": True,
                "title": "DO_NOT_SERIALIZE",
            },
            {
                "node_id": 10,
                "source": "DO_NOT_SERIALIZE",
                "position": 1,
                "cited": True,
            },
        ],
        cited_nodes=[
            {
                "node_id": 9,
                "kind": "decision",
                "status_at_citation": "canonical",
                "workstream_id_at_event": 3,
                "authority_tier_at_citation": "lane-local",
                "via_relation": None,
                "roles": ["decision_chain", "not_a_role"],
                "classifier_load_bearing": True,
                "body": "DO_NOT_SERIALIZE",
            },
            {
                "node_id": 10,
                "kind": "DO_NOT_SERIALIZE",
                "status_at_citation": "DO_NOT_SERIALIZE",
                "workstream_id_at_event": "DO_NOT_SERIALIZE",
                "authority_tier_at_citation": "DO_NOT_SERIALIZE",
                "via_relation": "DO_NOT_SERIALIZE",
                "roles": ["DO_NOT_SERIALIZE"],
                "classifier_load_bearing": False,
            },
        ],
        uncovered_claim_count=0,
        project_path="project",
        session_id=None,
    )
    row = _rows(local_vault)[0]
    assert set(row) == _HEADER | {
        "events_version",
        "row_kind",
        "gate_call_id",
        "query_hash",
        "verdict",
        "skipped",
        "timed_out",
        "error_present",
        "backend",
        "adversary",
        "assembled_nodes",
        "cited_nodes",
        "uncovered_claim_count",
    }
    assert row["row_kind"] == "gate_verdict"
    assert row["adversary"] == {
        "verdict_delta": "MODIFY",
        "counter_node_id": 9,
        "n_forks": 2,
    }
    assert row["cited_nodes"][0]["roles"] == ["decision_chain"]
    assert len(row["assembled_nodes"]) == 1
    assert row["cited_nodes"][1] == {
        "node_id": 10,
        "kind": None,
        "status_at_citation": None,
        "workstream_id_at_event": None,
        "authority_tier_at_citation": None,
        "via_relation": None,
        "roles": [],
        "classifier_load_bearing": False,
    }
    serialized = json.dumps(row)
    assert "DO_NOT_SERIALIZE" not in serialized
    assert not (_FORBIDDEN_KEYS & set(row))


def _synthetic_assembly() -> dict:
    seed = {
        "id": 1,
        "kind": "fact",
        "title": "seed title",
        "body_excerpt": "seed body",
        "status": "canonical",
        "workstream_id": 10,
        "source": "hybrid",
        "score": 1.0,
        "authority_tier": "lane-local",
    }
    graph = {
        "id": 2,
        "kind": "decision",
        "title": "graph title",
        "body_excerpt": "graph body",
        "status": "staging",
        "workstream_id": 10,
        "via_relation": "motivates",
        "direction": "out",
        "hop": 1,
        "path": [2],
        "authority_tier": "lane-local",
    }
    focus = {
        "id": 3,
        "kind": "fact",
        "title": "focus title",
        "body_excerpt": "focus body",
        "status": "staging",
        "workstream_id": 10,
        "via_relation": "related_to",
        "direction": "out",
        "hop": 1,
        "path": [3],
        "focus_derived": True,
    }
    return {
        "query": "raw request",
        "seeds": [seed],
        "chains": [{
            "seed_id": 1,
            "lane_group_id": 10,
            "evidence": [graph, focus],
        }],
        "evidence_node_ids": [2, 3],
        "lane_groups": [{"id": 10, "title": "lane title"}],
        "priorities": [
            {"id": 90, "title": "overall priority", "workstream_id": None},
            {
                "id": 91,
                "title": "scoped priority",
                "workstream_id": 10,
                "workstream_title": "lane title",
            },
        ],
    }


def _synthetic_verdict() -> dict:
    return {
        "recommendation": "PROCEED",
        "summary": "never log this",
        "decision_chain": [90, 10, 1, 2],
        "abandoned_paths": [],
        "active_constraints": [2],
        "current_direction": [1],
        "risk_if_proceed": "",
        "better_next_action": "",
        "evidence_nodes": [90, 10, 1, 2],
        "load_bearing_claims": [{
            "claim": "never log this claim",
            "evidence_type": "kb_node",
            "evidence_ref": 2,
            "gap_type": None,
        }],
        "uncovered_claims": [{"claim": "never log", "gap_type": "unknowable"}],
        "error": None,
        "backend": "codex",
        "adversary": {
            "verdict_delta": "MODIFY",
            "counter_node_id": 2,
            "design_decision_questions": [{"question": "never log"}],
        },
    }


def test_gate_snapshot_matches_prompt_order_and_event_time_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(capture_streams, "emit_gate_outcome_event", capture)
    assembly = _synthetic_assembly()
    exposure: list[dict] = []
    gate.build_classifier_prompt(assembly, exposure=exposure)
    gate._emit_gate_outcome_event(
        project_path="project",
        session_id="session",
        request="raw request",
        gate_call_id="012345abcdef",
        verdict=_synthetic_verdict(),
        exposure=exposure,
        evidence=[
            {
                "id": 90,
                "kind": "priority",
                "title": "never log",
                "status": "canonical",
                "workstream_id": None,
            },
            {
                "id": 10,
                "kind": "workstream",
                "title": "never log",
                "status": "canonical",
                "workstream_id": None,
            },
            {
                "id": 1,
                "kind": "fact",
                "title": "never log",
                "status": "canonical",
                "workstream_id": 10,
            },
            {
                "id": 2,
                "kind": "decision",
                "title": "never log",
                "status": "staging",
                "workstream_id": 10,
            },
        ],
    )

    assert captured["assembled_nodes"] == [
        {"node_id": 90, "source": "priority", "position": 0, "cited": True},
        {"node_id": 10, "source": "lane", "position": 1, "cited": True},
        {"node_id": 91, "source": "priority", "position": 2, "cited": False},
        {"node_id": 10, "source": "lane", "position": 3, "cited": True},
        {"node_id": 1, "source": "hybrid", "position": 4, "cited": True},
        {"node_id": 2, "source": "graph", "position": 5, "cited": True},
        {"node_id": 3, "source": "focus", "position": 6, "cited": False},
    ]
    cited = {row["node_id"]: row for row in captured["cited_nodes"]}
    assert cited[90]["kind"] == "priority"
    assert cited[10]["kind"] == "workstream"
    assert cited[1]["roles"] == ["decision_chain", "current_direction"]
    assert cited[2]["roles"] == ["decision_chain", "active_constraint"]
    assert cited[2]["classifier_load_bearing"] is True
    assert cited[2]["status_at_citation"] == "staging"
    assert cited[2]["authority_tier_at_citation"] == "lane-local"
    assert cited[2]["via_relation"] == "motivates"
    assert captured["adversary"] == {
        "verdict_delta": "MODIFY",
        "counter_node_id": 2,
        "n_forks": 1,
    }
    assert captured["uncovered_claim_count"] == 1


def test_gate_opt_out_bypasses_exposure_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_emit(**_kwargs):
        raise AssertionError("disabled outcome events must not reach the writer")

    monkeypatch.setattr(capture_streams, "emit_gate_outcome_event", must_not_emit)
    gate._emit_gate_outcome_event(
        project_path="project",
        session_id=None,
        request="request",
        gate_call_id="012345abcdef",
        verdict=_synthetic_verdict(),
        exposure=None,
        evidence=[],
    )


def _empty_verdict(**updates) -> dict:
    verdict = {
        "recommendation": "PROCEED",
        "summary": "stub",
        "decision_chain": [],
        "abandoned_paths": [],
        "active_constraints": [],
        "current_direction": [],
        "risk_if_proceed": "",
        "better_next_action": "",
        "evidence_nodes": [],
        "load_bearing_claims": [],
        "uncovered_claims": [],
        "error": None,
        "backend": "codex",
    }
    verdict.update(updates)
    return verdict


def _stub_gate_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    assembly = {
        "query": "",
        "seeds": [],
        "chains": [],
        "evidence_node_ids": [],
        "lane_groups": [],
        "priorities": [],
    }
    verdict = _empty_verdict()

    def assemble(_conn, request, **_kwargs):
        result = copy.deepcopy(assembly)
        result["query"] = request
        return result

    monkeypatch.setattr(paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(gate, "assemble_gate", assemble)
    monkeypatch.setattr(gate, "_record_gate_contacts", lambda *_a, **_k: None)
    monkeypatch.setattr(gate, "classify_gate", lambda *_a, **_k: copy.deepcopy(verdict))
    monkeypatch.setattr(gate, "_should_fire_adversary", lambda _verdict: False)
    monkeypatch.setattr(gate, "_budget_count_snapshot", lambda _path: 0)
    monkeypatch.setattr(gate, "LOG_RAW_QUERY", False)


def test_run_gate_result_invariance_and_exact_gate_join(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_gate_runtime(monkeypatch)
    observed_exposure: list[list[dict] | None] = []

    def classify(*_args, **kwargs):
        observed_exposure.append(kwargs.get("outcome_exposure"))
        return _empty_verdict()

    monkeypatch.setattr(gate, "classify_gate", classify)
    conn = sqlite3.connect(":memory:")
    try:
        monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "0")
        disabled = gate.run_gate(
            conn,
            "private request text",
            project_path="project",
            session_id="session",
        )
        monkeypatch.setenv("LATCH_OUTCOME_EVENTS", "1")
        enabled = gate.run_gate(
            conn,
            "private request text",
            project_path="project",
            session_id="session",
        )
    finally:
        conn.close()

    # The invariant under test is that the outcome-events flag does not change
    # gate behavior. `gate_call_id` is a fresh per-call nonce that is now
    # returned to the caller (so hosts recording tool results capture it and an
    # offline pass can attribute the call exactly), so it necessarily differs
    # between any two calls — flag or no flag. Compare everything else, and
    # assert the nonce's presence separately rather than weakening the check.
    assert enabled.keys() == disabled.keys()
    assert {k: v for k, v in enabled.items() if k != "gate_call_id"} == {
        k: v for k, v in disabled.items() if k != "gate_call_id"
    }
    assert enabled["gate_call_id"] != disabled["gate_call_id"]
    assert len(enabled["gate_call_id"]) == 12
    gate_rows = _rows(local_vault, gate.LOG_STREAM)
    outcome_rows = _rows(local_vault)
    assert len(gate_rows) == 2
    assert len(outcome_rows) == 1
    assert outcome_rows[0]["gate_call_id"] == gate_rows[1]["gate_call_id"]
    assert gate_rows[0]["gate_call_id"] != gate_rows[1]["gate_call_id"]
    assert len(gate_rows[1]["gate_call_id"]) == 12
    assert "private request text" not in json.dumps(outcome_rows[0])
    assert observed_exposure[0] is None
    assert observed_exposure[1] == []


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"recommendation": None, "skipped": True, "error": "disabled"},
            {"skipped": True, "timed_out": False, "error_present": True},
        ),
        (
            {"recommendation": None, "timed_out": True, "error": "timeout"},
            {"skipped": False, "timed_out": True, "error_present": True},
        ),
        (
            {"recommendation": None, "error": "malformed backend output"},
            {"skipped": False, "timed_out": False, "error_present": True},
        ),
    ],
)
def test_run_gate_records_degraded_outcomes_without_error_text(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict,
    expected: dict,
) -> None:
    _stub_gate_runtime(monkeypatch)
    monkeypatch.setattr(
        gate,
        "classify_gate",
        lambda *_a, **_k: _empty_verdict(**updates),
    )
    conn = sqlite3.connect(":memory:")
    try:
        result = gate.run_gate(conn, "request", project_path="project")
    finally:
        conn.close()
    row = _rows(local_vault)[0]
    assert result["verdict"]["error"] == updates["error"]
    assert {key: row[key] for key in expected} == expected
    assert updates["error"] not in json.dumps(row)


def test_run_gate_records_post_adversary_structure(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_gate_runtime(monkeypatch)
    monkeypatch.setattr(gate, "_should_fire_adversary", lambda _verdict: True)
    monkeypatch.setattr(
        gate.profiles,
        "active_adversary_mode",
        lambda _conn: "counter_node",
    )
    monkeypatch.setattr(
        gate,
        "adversary_classify",
        lambda *_a, **_k: {
            "verdict_delta": "MODIFY",
            "counter_node_id": 12,
            "design_decision_questions": [
                {"question": "never serialize this"}
            ],
            "error": None,
            "backend": "codex",
        },
    )
    monkeypatch.setattr(gate, "_log_adversary", lambda **_kwargs: None)
    conn = sqlite3.connect(":memory:")
    try:
        result = gate.run_gate(conn, "request", project_path="project")
    finally:
        conn.close()
    assert result["verdict"]["adversary"]["verdict_delta"] == "MODIFY"
    row = _rows(local_vault)[0]
    assert row["adversary"] == {
        "verdict_delta": "MODIFY",
        "counter_node_id": 12,
        "n_forks": 1,
    }
    assert "never serialize this" not in json.dumps(row)


def test_outcome_failure_and_unlatched_mode_do_not_change_gate(
    local_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_gate_runtime(monkeypatch)

    def fail(**_kwargs):
        raise OSError("outcome writer failed")

    monkeypatch.setattr(capture_streams, "emit_gate_outcome_event", fail)
    conn = sqlite3.connect(":memory:")
    try:
        result = gate.run_gate(conn, "request", project_path="project")
        assert result["verdict"]["recommendation"] == "PROCEED"
        assert len(_rows(local_vault, gate.LOG_STREAM)) == 1

        monkeypatch.setattr(paths, "is_unlatched_mode", lambda: True)
        before = copy.deepcopy(result)
        unlatched = gate.run_gate(conn, "request", project_path="project")
        assert unlatched["verdict"]["reason"] == "unlatched"
        assert len(_rows(local_vault, gate.LOG_STREAM)) == 1
        assert _rows(local_vault) == []
        assert before["verdict"]["recommendation"] == "PROCEED"
    finally:
        conn.close()


def test_outcome_stream_name_is_retention_compatible() -> None:
    match = log_utils._DAILY_LOG_RE.match("outcome_event-2026-07-28.log")
    assert match is not None
    assert match.group("stream") == "outcome_event"


def _concurrent_outcome_writer(worker_id: int, rows_per_worker: int) -> None:
    assembled = [
        {
            "node_id": node_id,
            "source": "graph",
            "position": node_id,
            "cited": False,
        }
        for node_id in range(60)
    ]
    for row_id in range(rows_per_worker):
        capture_streams.emit_gate_outcome_event(
            gate_call_id=f"{worker_id:04x}{row_id:08x}",
            query_hash="abcdef123456",
            verdict="PROCEED",
            skipped=False,
            timed_out=False,
            error_present=False,
            backend="codex",
            adversary=None,
            assembled_nodes=assembled,
            cited_nodes=[],
            uncovered_claim_count=0,
            project_path="project",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX append smoke")
def test_concurrent_process_rows_remain_valid_json(
    local_vault: Path,
) -> None:
    context = multiprocessing.get_context("fork")
    workers = [
        context.Process(target=_concurrent_outcome_writer, args=(worker_id, 8))
        for worker_id in range(4)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    rows = _rows(local_vault)
    assert len(rows) == 32
    assert len({row["gate_call_id"] for row in rows}) == 32


@pytest.mark.parametrize(
    "filename",
    [
        "gate.py",
        "capture_streams.py",
        "outcome_measurement.py",
        "outcome_measurement_runner.py",
        "outcome_evidence.py",
        "artifacts.py",
        "project_proof.py",
        "paths.py",
        "db.py",
        "vault_identity.py",
        "log_utils.py",
    ],
)
def test_outcome_runtime_modules_change_the_shared_runtime_key(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
) -> None:
    baseline = mcp_broker._runtime_key()
    target = Path(mcp_broker.__file__).resolve().parent / filename
    original_open = Path.open

    def changed_open(self: Path, *args, **kwargs):
        handle = original_open(self, *args, **kwargs)
        if self.resolve() != target:
            return handle
        data = handle.read()
        handle.close()
        if isinstance(data, bytes):
            return io.BytesIO(data + b"\n# runtime-key-test\n")
        return io.StringIO(data + "\n# runtime-key-test\n")

    monkeypatch.setattr(Path, "open", changed_open)
    assert mcp_broker._runtime_key() != baseline
