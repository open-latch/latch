from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latch.store import db  # noqa: E402
from latch.pipeline import heal  # noqa: E402
from latch.mcp import mcp_server  # noqa: E402


@pytest.fixture
def capture_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    db.connect(str(project)).close()

    monkeypatch.setattr(mcp_server, "_conn", lambda: db.connect(str(project)))
    monkeypatch.setattr(mcp_server, "_project_cwd", lambda: str(project))
    monkeypatch.setattr(mcp_server, "_project_session_id", lambda: "mcp-session")
    monkeypatch.setattr(mcp_server.paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(
        mcp_server.capture_streams,
        "emit_decision_event",
        lambda **_kwargs: None,
    )
    # Candidate selection is controlled below, so no model load is needed.
    monkeypatch.setattr(
        mcp_server.embeddings,
        "embed",
        lambda _text: [0.0] * mcp_server.embeddings.DIM,
    )
    return project


def _insert_node(project: Path, **kwargs) -> int:
    conn = db.connect(str(project))
    try:
        return db.insert_node(conn, **kwargs)
    finally:
        conn.close()


def _candidate(project: Path, node_id: int, similarity: float) -> dict:
    conn = db.connect(str(project))
    try:
        node = db.get_node(conn, node_id)
        assert node is not None
        return {**node, "similarity": similarity}
    finally:
        conn.close()


def _spy_structural_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    events: list[tuple] = []

    def _record(stream: str, payload: dict, **kwargs) -> None:
        events.append((stream, payload, kwargs))

    monkeypatch.setattr(heal.log_utils, "emit_event", _record)
    return events


def test_capture_duplicate_with_reconciliation_link_restores_baseline_streams(
    capture_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched_id = _insert_node(
        capture_vault,
        kind="decision",
        title="Existing decision",
        body="Existing decision body.",
        status="staging",
    )
    reconciliation_target = _insert_node(
        capture_vault,
        kind="fact",
        title="Constraining evidence",
        body="Evidence used by the explicit reconciliation edge.",
        status="canonical",
    )
    similarity = 0.987654321
    monkeypatch.setattr(
        heal,
        "find_near_duplicates",
        lambda *_args, **_kwargs: [
            _candidate(capture_vault, matched_id, similarity)
        ],
    )
    events = _spy_structural_events(monkeypatch)

    result = mcp_server.kb_capture_decision(
        title="Near duplicate decision",
        body="Near duplicate decision body.",
        gate_request="Capture this human decision",
        human_action="modify",
        links=[{"dst": reconciliation_target, "relation": "reconciled_by"}],
        session_id="telemetry-session",
    )

    assert [stream for stream, _payload, _kwargs in events] == [
        "reconciliation",
        "heal",
    ]
    reconciliation = events[0]
    assert set(reconciliation[1]) == {
        "src_id",
        "src_kind",
        "src_status_before",
        "dst_id",
        "dst_kind",
        "relation",
        "src_ref_count_at_event",
        "src_age_days",
        "src_session_touch_count",
        "elapsed_ms",
    }
    assert reconciliation[1]["src_id"] == result["id"]
    assert reconciliation[1]["src_kind"] == "decision"
    assert reconciliation[1]["src_status_before"] == "staging"
    assert reconciliation[1]["dst_id"] == reconciliation_target
    assert reconciliation[1]["dst_kind"] == "fact"
    assert reconciliation[1]["relation"] == "reconciled_by"
    assert reconciliation[1]["src_ref_count_at_event"] == 0
    assert reconciliation[1]["src_session_touch_count"] == 0
    assert isinstance(reconciliation[1]["src_age_days"], float)
    assert reconciliation[1]["src_age_days"] >= 0.0
    assert isinstance(reconciliation[1]["elapsed_ms"], int)
    assert reconciliation[2] == {
        "project_path": str(capture_vault),
        "session_id": "telemetry-session",
    }

    heal_event = events[1]
    assert set(heal_event[1]) == {
        "inserted_node_id",
        "inserted_kind",
        "matched_id",
        "matched_kind",
        "matched_status_before",
        "similarity",
        "arbitrator_decision",
        "elapsed_ms",
    }
    assert heal_event[1]["inserted_node_id"] == result["id"]
    assert heal_event[1]["inserted_kind"] == "decision"
    assert heal_event[1]["matched_id"] == matched_id
    assert heal_event[1]["matched_kind"] == "decision"
    assert heal_event[1]["matched_status_before"] == "staging"
    assert heal_event[1]["similarity"] == similarity
    assert heal_event[1]["arbitrator_decision"] == "keep_both"
    assert isinstance(heal_event[1]["elapsed_ms"], int)
    assert heal_event[2] == {
        "project_path": str(capture_vault),
        "session_id": "telemetry-session",
    }


def test_capture_cross_workstream_duplicate_restores_lifecycle_signal(
    capture_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left_workstream = _insert_node(
        capture_vault,
        kind="workstream",
        title="Left lane",
        body="Objective: left.",
        status="canonical",
    )
    right_workstream = _insert_node(
        capture_vault,
        kind="workstream",
        title="Right lane",
        body="Objective: right.",
        status="canonical",
    )
    matched_id = _insert_node(
        capture_vault,
        kind="decision",
        title="Existing cross-lane decision",
        body="Existing decision body.",
        status="staging",
        workstream_id=left_workstream,
    )
    similarity = 0.876543219
    monkeypatch.setattr(
        heal,
        "find_near_duplicates",
        lambda *_args, **_kwargs: [
            _candidate(capture_vault, matched_id, similarity)
        ],
    )
    events = _spy_structural_events(monkeypatch)

    result = mcp_server.kb_capture_decision(
        title="Cross-lane near duplicate",
        body="Cross-lane near duplicate body.",
        gate_request="Capture this human decision",
        human_action="modify",
        workstream_id=right_workstream,
        session_id="telemetry-session",
    )

    assert [stream for stream, _payload, _kwargs in events] == [
        "lifecycle",
        "heal",
    ]
    lifecycle = events[0]
    assert lifecycle[1] == {
        "event": "cross_lane_duplicate",
        "substrate_version": heal.lifecycle_signals.SUBSTRATE_VERSION,
        "node_a": result["id"],
        "node_b": matched_id,
        "ws_a": right_workstream,
        "ws_b": left_workstream,
        "similarity": round(similarity, 6),
    }
    assert lifecycle[2] == {
        "project_path": str(capture_vault),
        "session_id": "telemetry-session",
    }


def test_capture_candidate_search_runs_outside_the_write_transaction(
    capture_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The KNN candidate scan must run above BEGIN IMMEDIATE (5648 item 4):
    a UserPromptSubmit hook writes with a 50ms busy timeout and no writer-lock
    coverage, and the cold embed costs ~129ms — neither may sit inside the
    capture write transaction. Green today; red on a naive consolidation that
    lets the shared primitive do its own candidate scan inside the
    transaction."""
    observed: list[bool] = []

    def _spy(conn, *args, **kwargs):
        observed.append(bool(conn.in_transaction))
        return []

    monkeypatch.setattr(heal, "find_near_duplicates", _spy)

    result = mcp_server.kb_capture_decision(
        title="Candidate search stays outside the transaction",
        body="The KNN candidate scan must not run under BEGIN IMMEDIATE.",
        gate_request="Capture this human decision",
        human_action="modify",
        session_id="telemetry-session",
    )

    assert result["id"] is not None
    assert observed == [False]


def test_capture_rollback_cannot_emit_ghost_structural_events(
    capture_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched_id = _insert_node(
        capture_vault,
        kind="decision",
        title="Existing decision",
        body="Existing decision body.",
        status="staging",
    )
    reconciliation_target = _insert_node(
        capture_vault,
        kind="fact",
        title="Constraining evidence",
        body="Evidence used by the explicit reconciliation edge.",
        status="canonical",
    )
    monkeypatch.setattr(
        heal,
        "find_near_duplicates",
        lambda *_args, **_kwargs: [
            _candidate(capture_vault, matched_id, 0.9)
        ],
    )
    events = _spy_structural_events(monkeypatch)
    conn = db.connect(str(capture_vault))
    try:
        conn.executescript(
            """
            CREATE TRIGGER force_ratification_failure
            BEFORE INSERT ON ratification
            BEGIN
                SELECT RAISE(ABORT, 'forced ratification failure');
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced ratification failure"):
        mcp_server.kb_capture_decision(
            title="Capture must roll back",
            body="Ratification fails after structural payload preparation.",
            gate_request="Capture this human decision",
            human_action="approve",
            links=[{"dst": reconciliation_target, "relation": "reconciled_by"}],
            session_id="telemetry-session",
        )

    assert events == []
