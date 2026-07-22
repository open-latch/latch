from __future__ import annotations

import copy
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import lifecycle_signals  # noqa: E402
import workstream_automation as automation  # noqa: E402
import workstream_detector  # noqa: E402
import workstreams  # noqa: E402


NOW = "2026-07-22 12:00:00"


def _connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    path = tmp_path / "kb.db"
    monkeypatch.setattr(db, "db_path", lambda _cwd=None: path)
    monkeypatch.setattr(
        db,
        "ensure_project_dir",
        lambda _cwd=None: path.parent.mkdir(parents=True, exist_ok=True),
    )
    return db.connect(str(tmp_path))


def _lane(conn: sqlite3.Connection, *, pinned: bool = False) -> int:
    lane = db.insert_node(
        conn,
        kind="workstream",
        title="Lane",
        body="Objective: ship\nDone when: shipped\nScope boundary: repo\nNext step: test",
        status="canonical",
    )
    if pinned:
        db.pin_focus(conn, lane)
    return lane


def _fake_module(calls: list[dict], *, merge=True, unmerge=True):
    def opened(conn, *, force=False, **kwargs):
        calls.append({"op": "OPEN", "force": force, **kwargs})
        return {"ok": True, "state": "applied", "op_key": kwargs["op_key"]}

    def closed(conn, *, force=False, **kwargs):
        calls.append({"op": "CLOSE", "force": force, **kwargs})
        return {"ok": True, "state": "applied", "op_key": kwargs["op_key"]}

    def merged(conn, *, force=False, **kwargs):
        calls.append({"op": "MERGE", "force": force, **kwargs})
        return {"ok": True, "state": "applied", "op_key": kwargs["op_key"]}

    def adopted(conn, **kwargs):
        calls.append({"op": "ADOPT", **kwargs})
        return {"ok": True, "state": "applied", "op_key": kwargs["op_key"]}

    attrs = {
        "open_workstream": opened,
        "close_workstream": closed,
        "close_preflight": workstreams.close_preflight,
        "merge_preflight": workstreams.merge_preflight,
        "priorities": workstreams.priorities,
        "adopt_nodes": adopted,
    }
    if merge:
        attrs["merge_workstreams"] = merged
    if unmerge:
        attrs["unmerge_workstream"] = lambda *_args, **_kwargs: {"ok": True}
    return SimpleNamespace(**attrs)


def _seed_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_key: str,
    op: str,
    signal: dict,
) -> None:
    for index in (1, 2):
        db.record_workstream_derivation(
            conn,
            derivation_key=f"d{index}-{candidate_key[-8:]}",
            substrate_version=workstream_detector.SUBSTRATE_VERSION,
            window_start=f"2026-07-{19 + index:02d} 00:00:00",
            window_end=f"2026-07-{20 + index:02d} 00:00:00",
            candidates=[{
                "candidate_key": candidate_key,
                "op": op,
                "signal": signal,
            }],
        )


def _record_candidate_once(
    conn: sqlite3.Connection,
    *,
    derivation_key: str,
    candidate_key: str,
    op: str,
    signal: dict,
) -> None:
    db.record_workstream_derivation(
        conn,
        derivation_key=derivation_key,
        substrate_version=workstream_detector.SUBSTRATE_VERSION,
        window_start="2026-07-20 00:00:00",
        window_end="2026-07-21 00:00:00",
        candidates=[{
            "candidate_key": candidate_key,
            "op": op,
            "signal": signal,
        }],
    )


def _accepted_proposal(
    conn: sqlite3.Connection,
    *,
    candidate_key: str,
    member_ids: list[int],
    event_key: str = "proposal-accepted",
    force_field: bool = False,
    recurrence: dict | None = None,
    record_sessions: bool = True,
) -> dict:
    latest = db.latest_workstream_derivation(conn)
    payload = {
        "proposal_key": "proposal:one",
        "candidate_key": candidate_key,
        "title": "Automated lane",
        "objective": "ship it",
        "done_when": "tests pass",
        "scope_boundary": "this repo",
        "next_step": "implement",
        "member_ids": member_ids,
        "recurrence": recurrence or {
            "session_ids": ["S1", "S2"],
            "session_count": 2,
            "since": "2026-07-01",
        },
        "proposal_validated": True,
        "proposal_source": "compactor",
    }
    if force_field:
        payload["force"] = False
    if record_sessions and member_ids:
        for session_id in payload["recurrence"].get("session_ids", []):
            db.record_retrieval_events(
                conn,
                source="tool",
                session_id=str(session_id),
                turn=1,
                items=[(member_ids[0], None)],
                ts="2026-07-20 12:00:00",
            )
    return db.append_workstream_op_event(
        conn,
        event_key=event_key,
        candidate_key=candidate_key,
        event_type="proposal_accepted",
        payload=payload,
        derivation_key=latest["derivation_key"],
        session_id="S2",
        require_latest_candidate=True,
    )


def _exercise_unmerge(conn: sqlite3.Connection, *, op_key: str = "manual-unmerge") -> None:
    db.begin_workstream_op(
        conn,
        op_key=op_key,
        op="UNMERGE",
        origin="manual",
        payload={"merge_op_key": "prior-merge"},
    )
    db.finish_workstream_op(conn, op_key, state="applied")


def _record_open_origin(
    conn: sqlite3.Connection,
    workstream_id: int,
    *,
    op_key: str,
    origin: str,
) -> None:
    db.begin_workstream_op(
        conn,
        op_key=op_key,
        op="OPEN",
        origin=origin,
        dst_workstream_id=workstream_id,
        payload={
            "assigned_member_ids": [],
            "probation": {},
            "watch_pair": [],
        },
    )
    db.finish_workstream_op(conn, op_key, state="applied")


def _attest(conn: sqlite3.Connection, candidate_key: str, *sessions: str) -> None:
    latest = db.latest_workstream_derivation(conn)
    for index, session_id in enumerate(sessions):
        db.append_workstream_op_event(
            conn,
            event_key=f"attest-{candidate_key[-8:]}-{index}",
            candidate_key=candidate_key,
            event_type="attestation",
            verdict="agree",
            session_id=session_id,
            derivation_key=latest["derivation_key"],
            require_latest_candidate=True,
        )


def _open_signal(*, sessions=("S1", "S2"), tier1=True, with_request=True) -> dict:
    signal = {
        "qualified": True,
        "tier1_present": tier1,
        "proposal": {"validated": True, "source": "compactor"},
    }
    if with_request:
        signal["apply_request"] = {
            "title": "Automated lane",
            "objective": "ship it",
            "done_when": "tests pass",
            "scope_boundary": "this repo",
            "next_step": "implement",
            "member_ids": [],
            "recurrence": {
                "session_ids": list(sessions),
                "session_count": len(sessions),
                "since": "2026-07-01",
            },
            "proposal_validated": True,
            "proposal_source": "compactor",
        }
    return signal


def _merge_signal(left: int, right: int, *, with_direction: bool = True) -> dict:
    signal = {
        "qualified": True,
        "left": left,
        "right": right,
        "left_sessions": 4,
        "right_sessions": 5,
        "co_contact_sessions": 4,
        "union_sessions": 5,
        "jaccard": 0.8,
        "tier1": "co_contact",
        "tier2_inputs": ["shared_targets"],
        "shared_target_ids": [101, 102],
    }
    if with_direction:
        signal["direction"] = {
            "source_workstream_id": left,
            "absorber_workstream_id": right,
            "basis": "contact_sessions",
            "unambiguous": True,
        }
        signal["apply_request"] = {
            "source_workstream_id": left,
            "absorber_workstream_id": right,
            "dispositions": {},
            "evidence": {"trigger": "co_contact"},
            "priority_policy_complete": True,
        }
    return signal


def _synthetic_merge_receipt_payload() -> dict:
    """Shape-complete receipt for regret-linkage tests; no state is replayed."""
    return {
        "repointed_member_ids": [],
        "prior_memberships": {},
        "rehomed_edge_ids": [],
        "tombstoned_edge_ids": [],
        "edge_rehomes": [],
        "retired_priority_ids": [],
        "readded_priority_ids": [],
        "overflow_retired_priority_ids": [],
        "priority_map": [],
        "priority_snapshots": [],
        "created_priority_snapshots": [],
        "src_focus": None,
        "dst_focus": None,
        "post_focus": None,
        "rolling_line": "merged",
        "rolling_op_key": "synthetic",
        "absorber_body_before": "",
        "absorber_body_before_hash": "before",
        "absorber_body_after_hash": "after",
        "source_body_hash": "source",
        "source_title": "source",
        "source_prior_status": "staging",
        "merge_edge_id": 1,
    }


@pytest.mark.parametrize(
    ("signal", "reason"),
    [
        (_open_signal(sessions=("S1",)), "open_proposal_or_recurrence_incomplete"),
        (_open_signal(tier1=False), "open_proposal_or_recurrence_incomplete"),
        (_open_signal(with_request=False), "missing_apply_request"),
    ],
)
def test_one_session_tree_only_and_missing_request_never_mutate(
    tmp_path, monkeypatch, signal, reason,
):
    conn = _connect(tmp_path, monkeypatch)
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b", "c", "d"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    calls: list[dict] = []
    result = automation.run_governed(
        conn,
        now=NOW,
        receipts_live=True,
        workstreams_module=_fake_module(calls),
    )
    assert calls == []
    assert result["applied"] == []
    assert reason in result["plans"][0]["reason_codes"]
    assert conn.execute("SELECT COUNT(*) FROM workstream_ops").fetchone()[0] == 0
    conn.close()


def _completed_close_signal(lane: int) -> dict:
    return {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "completed",
        "apply_request": {
            "workstream_id": lane,
            "outcome": "completed",
            "reason": "done when satisfied",
            "dispositions": {},
        },
    }


def test_pinned_lane_never_applies_even_with_quorum(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn, pinned=True)
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=_completed_close_signal(lane))
    _attest(conn, key, "S1", "S2")
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    assert calls == []
    assert "pinned_target" in result["plans"][0]["reason_codes"]
    assert db.get_node(conn, lane)["status"] == "canonical"
    conn.close()


def test_receipt_channel_and_attestation_quorum_are_hard_gates(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=_completed_close_signal(lane))
    _attest(conn, key, "S1")
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=False, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert calls == []
    assert "receipt_channel_unavailable" in plan["reason_codes"]
    assert "close_policy_not_satisfied" in plan["reason_codes"]
    conn.close()


def test_recent_target_activity_defers_an_otherwise_attested_close(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=_completed_close_signal(lane))
    _attest(conn, key, "S1", "S2")
    db.record_retrieval_events(
        conn,
        source="tool",
        session_id="active",
        turn=2,
        items=[(lane, None)],
        ts="2026-07-22 11:50:00",
    )
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert calls == []
    assert "session_not_quiescent" in plan["reason_codes"]
    conn.close()


def test_merge_requires_both_operations_exercised_unmerge_and_priority_policy(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    left, right = _lane(conn), _lane(conn)
    key = lifecycle_signals.make_candidate_key("MERGE", [left, right])
    signal = {
        "qualified": True,
        "left": left,
        "right": right,
        "apply_request": {
            "src_workstream_id": left,
            "dst_workstream_id": right,
            "reason": "sustained overlap",
            "priority_policy_complete": False,
        },
    }
    _seed_candidate(conn, candidate_key=key, op="MERGE", signal=signal)
    _attest(conn, key, "S1", "S2")
    calls: list[dict] = []
    plan = automation.plan_actions(
        conn,
        now=NOW,
        receipts_live=True,
        workstreams_module=_fake_module(calls, unmerge=False),
    )[0]
    assert not plan["eligible"]
    assert "merge_reversibility_rails_incomplete" in plan["reason_codes"]
    assert {
        key: plan["policy_evidence"][key]
        for key in (
            "merge_present", "unmerge_present", "unmerge_exercised",
            "priority_policy_complete",
        )
    } == {
        "merge_present": True,
        "unmerge_present": False,
        "unmerge_exercised": False,
        "priority_policy_complete": False,
    }
    conn.close()


def test_complete_open_is_eligible_and_executor_never_passes_force(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b", "c", "d"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=_open_signal())
    calls: list[dict] = []
    module = _fake_module(calls)
    first_plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=module,
    )[0]
    second_plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=module,
    )[0]
    assert first_plan["eligible"]
    assert first_plan["op_key"] == second_plan["op_key"]
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=module,
    )
    assert len(result["applied"]) == 1
    assert calls[0]["op"] == "OPEN"
    assert calls[0]["force"] is False
    assert calls[0]["origin"] == "auto"
    conn.close()


def test_probation_abandonment_requires_active_auto_open_and_release_only(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    db.begin_workstream_op(
        conn,
        op_key="auto-open",
        op="OPEN",
        origin="auto",
        dst_workstream_id=lane,
        payload={
            "assigned_member_ids": [],
            "watch_pair": None,
            "probation": {
                "active": True,
                "opened_at": "2026-07-01 00:00:00",
                "until": "2026-07-30 00:00:00",
            },
        },
    )
    db.finish_workstream_op(conn, "auto-open", state="applied")
    feeder = db.insert_node(
        conn, kind="idea", title="probation feeder", body="pending",
        status="staging", workstream_id=lane,
    )
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "abandoned",
        "reason": "auto_open_probation_no_contacts",
        "opening_op_key": "auto-open",
        "probation_ready": True,
        "auto_apply_eligible": True,
        "eligible_session_count": 10,
        "eligible_session_target": 10,
        "apply_request": {
            "workstream_id": lane,
            "outcome": "abandoned",
            "reason": "no probation contacts",
            "dispositions": {str(feeder): {"action": "release"}},
        },
    }
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=signal)
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    assert result["plans"][0]["eligible"]
    assert result["plans"][0]["policy_evidence"]["probation_abandonment"]
    assert calls[0]["force"] is False
    conn.close()


def test_probation_close_uses_auto_preflight_and_production_release_service(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    assigned = [
        db.insert_node(
            conn, kind=kind, title=f"assigned {kind}", body="pending",
            status="staging", workstream_id=lane,
        )
        for kind in ("fact", "progress")
    ]
    db.begin_workstream_op(
        conn,
        op_key="auto-open-mixed",
        op="OPEN",
        origin="auto",
        dst_workstream_id=lane,
        payload={
            "assigned_member_ids": assigned,
            "watch_pair": None,
            "probation": {
                "active": True,
                "opened_at": "2026-07-01 00:00:00",
                "eligible_session_target": 10,
            },
        },
    )
    db.finish_workstream_op(conn, "auto-open-mixed", state="applied")
    unrelated = db.insert_node(
        conn, kind="fact", title="unrelated contact", body="other work",
        status="canonical",
    )
    for index in range(10):
        db.record_retrieval_events(
            conn,
            source="tool",
            session_id=f"eligible-{index}",
            turn=1,
            items=[(unrelated, None)],
            ts=f"2026-07-{2 + index:02d} 12:00:00",
        )
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "abandoned",
        "reason": "auto_open_probation_no_contacts",
        "opening_op_key": "auto-open-mixed",
        "probation_ready": True,
        "auto_apply_eligible": True,
        "eligible_session_count": 10,
        "eligible_session_target": 10,
        "apply_request": {
            "workstream_id": lane,
            "outcome": "abandoned",
            "reason": "zero contacts across 10 eligible sessions",
            "dispositions": {
                str(node_id): {"action": "release"} for node_id in assigned
            },
        },
    }
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=signal)
    monkeypatch.setattr(
        workstreams,
        "_backup_before_mutation",
        lambda *_args, **_kwargs: None,
    )
    result = automation.run_governed(
        conn,
        now=NOW,
        receipts_live=True,
        project_path=str(tmp_path),
        workstreams_module=workstreams,
    )
    assert len(result["applied"]) == 1
    assert result["failed"] == []
    assert db.get_node(conn, lane)["status"] == "stale"
    assert all(db.get_node(conn, node_id)["workstream_id"] is None for node_id in assigned)
    operation = db.get_workstream_op(conn, result["applied"][0]["op_key"])
    assert operation["candidate_key"] == key
    assert operation["forced"] == 0
    conn.close()


def test_expired_probation_cannot_remain_automatically_active(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    feeder = db.insert_node(
        conn, kind="idea", title="old feeder", body="pending",
        status="staging", workstream_id=lane,
    )
    db.begin_workstream_op(
        conn,
        op_key="auto-open-expired",
        op="OPEN",
        origin="auto",
        dst_workstream_id=lane,
        payload={
            "assigned_member_ids": [feeder],
            "watch_pair": None,
            "probation": {"active": True, "opened_at": "2026-01-01 00:00:00"},
        },
    )
    db.finish_workstream_op(conn, "auto-open-expired", state="applied")
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "abandoned",
        "reason": "auto_open_probation_no_contacts",
        "opening_op_key": "auto-open-expired",
        "probation_ready": True,
        "auto_apply_eligible": True,
        "eligible_session_count": 10,
        "eligible_session_target": 10,
        "apply_request": {
            "workstream_id": lane,
            "outcome": "abandoned",
            "reason": "stale signal",
            "dispositions": {str(feeder): {"action": "release"}},
        },
    }
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=signal)
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert not plan["eligible"]
    assert not plan["policy_evidence"]["probation_abandonment"]
    assert "close_policy_not_satisfied" in plan["reason_codes"]
    assert calls == []
    conn.close()


def test_latest_accepted_open_proposal_supplies_read_only_apply_request(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    members = [
        db.insert_node(
            conn, kind="idea", title=f"member {index}", body="forward",
            status="staging",
        )
        for index in range(4)
    ]
    key = lifecycle_signals.make_candidate_key(
        "OPEN", [], [f"unit-{value}" for value in members],
    )
    signal = {
        "qualified": True,
        "member_ids": members,
        "tier1_present": True,
        "tier1": "multi_session_orphan_contact",
    }
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    event = _accepted_proposal(conn, candidate_key=key, member_ids=members)
    calls: list[dict] = []
    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )[0]
    assert plan["eligible"]
    assert plan["apply_request"]["member_ids"] == members
    assert plan["apply_request"]["objective"] == "ship it"
    assert "force" not in plan["apply_request"]
    assert plan["policy_evidence"]["proposal_event_id"] == event["id"]
    assert calls == []
    assert conn.execute("SELECT COUNT(*) FROM workstream_ops").fetchone()[0] == 0
    conn.close()


def test_accepted_open_proposal_rechecks_contamination_free_sessions(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    member = db.insert_node(
        conn, kind="idea", title="member", body="forward", status="staging",
    )
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["session-proof"])
    signal = {
        "qualified": True,
        "member_ids": [member],
        "tier1_present": True,
        "tier1": "multi_session_orphan_contact",
    }
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    for session_id in ("S1", "S2"):
        db.record_retrieval_events(
            conn,
            source="prompt",
            session_id=session_id,
            turn=0,
            items=[(member, None)],
            ts="2026-07-20 12:00:00",
        )
    _accepted_proposal(
        conn,
        candidate_key=key,
        member_ids=[member],
        record_sessions=False,
    )

    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module([]),
    )[0]

    assert not plan["eligible"]
    assert plan["apply_request"] == {}
    assert plan["policy_evidence"]["proposal_event_status"] == "recurrence_incomplete"
    assert "open_proposal_event_not_accepted" in plan["reason_codes"]
    conn.close()


def test_rejected_malformed_and_stale_open_proposals_never_qualify(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    member = db.insert_node(
        conn, kind="idea", title="member", body="forward", status="staging",
    )
    stale_key = lifecycle_signals.make_candidate_key("OPEN", [], ["stale"])
    current_key = lifecycle_signals.make_candidate_key("OPEN", [], ["current"])
    signal = {
        "qualified": True,
        "member_ids": [member],
        "tier1_present": True,
        "tier1": "multi_session_orphan_contact",
    }
    _record_candidate_once(
        conn, derivation_key="stale-d1", candidate_key=stale_key,
        op="OPEN", signal=signal,
    )
    _accepted_proposal(
        conn, candidate_key=stale_key, member_ids=[member], event_key="stale-accepted",
    )
    _record_candidate_once(
        conn, derivation_key="current-d1", candidate_key=current_key,
        op="OPEN", signal=signal,
    )
    _record_candidate_once(
        conn, derivation_key="current-d2", candidate_key=current_key,
        op="OPEN", signal=signal,
    )
    latest = db.latest_workstream_derivation(conn)
    db.append_workstream_op_event(
        conn,
        event_key="current-rejected",
        candidate_key=current_key,
        event_type="proposal_rejected",
        payload={"reasons": ["insufficient_recurrence"]},
        derivation_key=latest["derivation_key"],
        session_id="S3",
        require_latest_candidate=True,
    )
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert not plan["eligible"]
    assert plan["apply_request"] == {}
    assert "open_proposal_event_not_accepted" in plan["reason_codes"]
    assert calls == []
    conn.close()


def test_accepted_open_proposal_with_force_field_fails_closed(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    member = db.insert_node(
        conn, kind="idea", title="member", body="forward", status="staging",
    )
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["force"])
    signal = {
        "qualified": True,
        "member_ids": [member],
        "tier1_present": True,
    }
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    _accepted_proposal(
        conn, candidate_key=key, member_ids=[member], force_field=True,
    )
    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module([]),
    )[0]
    assert not plan["eligible"]
    assert plan["apply_request"] == {}
    assert plan["policy_evidence"]["proposal_event_status"] == "malformed"
    conn.close()


def test_newer_rejected_proposal_supersedes_older_acceptance(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    member = db.insert_node(
        conn, kind="idea", title="member", body="forward", status="staging",
    )
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["superseded"])
    signal = {
        "qualified": True,
        "member_ids": [member],
        "tier1_present": True,
    }
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    _accepted_proposal(conn, candidate_key=key, member_ids=[member])
    latest = db.latest_workstream_derivation(conn)
    db.append_workstream_op_event(
        conn,
        event_key="newer-proposal-rejected",
        candidate_key=key,
        event_type="proposal_rejected",
        payload={"reasons": ["charter_drift"]},
        derivation_key=latest["derivation_key"],
        session_id="S3",
        require_latest_candidate=True,
    )
    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module([]),
    )[0]
    assert not plan["eligible"]
    assert plan["apply_request"] == {}
    assert plan["policy_evidence"]["proposal_event_status"] == "proposal_rejected"
    conn.close()


def test_verified_shared_target_open_applies_without_invented_sessions(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    members = [
        db.insert_node(
            conn, kind="idea", title=f"shared member {index}", body="forward",
            status="staging",
        )
        for index in range(2)
    ]
    target = db.insert_node(
        conn, kind="decision", title="shared decision", body="decided",
        status="canonical",
    )
    for member in members:
        db.add_edge(conn, member, target, "advances")
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["shared-a", "shared-b"])
    signal = {
        "qualified": True,
        "member_ids": members,
        "shared_target_ids": [target],
    }
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    _accepted_proposal(
        conn,
        candidate_key=key,
        member_ids=members,
        recurrence={
            "session_ids": [],
            "session_count": 0,
            "shared_target_ids": [target],
        },
    )
    monkeypatch.setattr(
        workstreams,
        "_similar_workstreams",
        lambda _conn, _text, *, embedding=None: (embedding, []),
    )
    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=workstreams,
    )[0]
    assert plan["eligible"]
    assert plan["apply_request"]["recurrence"]["session_ids"] == []
    assert plan["apply_request"]["recurrence"]["shared_target_validated"] is True
    result = automation.run_governed(
        conn,
        now=NOW,
        receipts_live=True,
        project_path=str(tmp_path),
        workstreams_module=workstreams,
    )
    assert len(result["applied"]) == 1
    assert result["failed"] == []
    operation = db.get_workstream_op(conn, result["applied"][0]["op_key"])
    assert operation["candidate_key"] == key
    assert operation["payload"]["request"]["recurrence"]["session_ids"] == []
    assert operation["payload"]["request"]["recurrence"]["shared_target_ids"] == [target]
    conn.close()


def test_verified_shared_target_ignores_stale_members(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    members = [
        db.insert_node(
            conn, kind="idea", title=f"member {index}", body="forward",
            status="staging",
        )
        for index in range(2)
    ]
    target = db.insert_node(
        conn, kind="decision", title="target", body="decided", status="canonical",
    )
    for member in members:
        db.add_edge(conn, member, target, "advances")
    db.update_node(conn, members[1], status="stale")
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE dst=? AND status='active'", (target,),
    ).fetchone()[0] == 2
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["stale-a", "stale-b"])
    _seed_candidate(
        conn,
        candidate_key=key,
        op="OPEN",
        signal={
            "qualified": True,
            "member_ids": members,
            "shared_target_ids": [target],
        },
    )
    _accepted_proposal(
        conn,
        candidate_key=key,
        member_ids=members,
        recurrence={
            "session_ids": [],
            "session_count": 0,
            "shared_target_ids": [target],
        },
    )

    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module([]),
    )[0]

    assert not plan["eligible"]
    assert plan["apply_request"] == {}
    assert plan["policy_evidence"]["proposal_event_status"] == "recurrence_incomplete"
    conn.close()


def test_cap_pressure_open_stays_shadow_despite_accepted_proposal(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    member = db.insert_node(
        conn, kind="idea", title="member", body="forward", status="staging",
    )
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["cap"])
    signal = {
        "qualified": False,
        "cap_pressure": True,
        "member_ids": [member],
        "tier1_present": True,
    }
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=signal)
    _accepted_proposal(conn, candidate_key=key, member_ids=[member])
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    plan = result["plans"][0]
    assert not plan["eligible"]
    assert {"candidate_not_qualified", "cap_pressure"}.issubset(plan["reason_codes"])
    assert calls == []
    conn.close()


def test_attestation_quorum_persists_while_candidate_key_persists(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = _completed_close_signal(lane)
    _record_candidate_once(
        conn, derivation_key="attested-d1", candidate_key=key,
        op="CLOSE", signal=signal,
    )
    _attest(conn, key, "S1", "S2")
    _record_candidate_once(
        conn, derivation_key="attested-d2", candidate_key=key,
        op="CLOSE", signal=signal,
    )
    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module([]),
    )[0]
    assert plan["attestation"]["quorum"]
    assert plan["attestation"]["agree_sessions"] == ["S1", "S2"]
    assert len(plan["attestation"]["derivation_ids"]) == 1
    conn.close()


def test_detector_shaped_merge_is_preflighted_and_reachable(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    left, right = _lane(conn), _lane(conn)
    key = lifecycle_signals.make_candidate_key("MERGE", [left, right])
    _seed_candidate(
        conn, candidate_key=key, op="MERGE", signal=_merge_signal(left, right),
    )
    _attest(conn, key, "S1", "S2")
    _exercise_unmerge(conn)
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    plan = result["plans"][0]
    assert plan["eligible"]
    assert plan["apply_request"]["source_workstream_id"] == left
    assert plan["apply_request"]["absorber_workstream_id"] == right
    assert plan["apply_request"]["preflight_token"]
    assert calls[0]["op"] == "MERGE"
    assert calls[0]["force"] is False
    assert calls[0]["candidate_key"] == key
    conn.close()


def test_active_probation_watch_pair_synthesizes_merge_back(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    watched, opened = _lane(conn), _lane(conn)
    _record_open_origin(
        conn, watched, op_key="auto-open-absorber", origin="auto",
    )
    db.begin_workstream_op(
        conn,
        op_key="auto-open-watch",
        op="OPEN",
        origin="auto",
        dst_workstream_id=opened,
        payload={
            "assigned_member_ids": [],
            "watch_pair": [opened, watched],
            "probation": {"active": True, "opened_at": "2026-07-01 00:00:00"},
        },
    )
    db.finish_workstream_op(conn, "auto-open-watch", state="applied")
    key = lifecycle_signals.make_candidate_key("MERGE", [watched, opened])
    _seed_candidate(
        conn,
        candidate_key=key,
        op="MERGE",
        signal=_merge_signal(watched, opened, with_direction=False),
    )
    _exercise_unmerge(conn)
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    plan = result["plans"][0]
    assert plan["eligible"]
    assert plan["apply_request"]["source_workstream_id"] == opened
    assert plan["apply_request"]["absorber_workstream_id"] == watched
    assert plan["policy_evidence"]["probation_merge_back"]["eligible"]
    assert plan["policy_evidence"]["probation_self_revert"]
    assert not plan["attestation"]["quorum"]
    assert calls[0]["source_workstream_id"] == opened
    assert calls[0]["absorber_workstream_id"] == watched
    conn.close()


def test_probation_watch_pair_human_absorber_requires_attestation(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    watched, opened = _lane(conn), _lane(conn)
    _record_open_origin(
        conn, watched, op_key="human-open-absorber", origin="manual",
    )
    db.begin_workstream_op(
        conn,
        op_key="auto-open-human-watch",
        op="OPEN",
        origin="auto",
        dst_workstream_id=opened,
        payload={
            "assigned_member_ids": [],
            "watch_pair": [opened, watched],
            "probation": {"active": True, "opened_at": "2026-07-01 00:00:00"},
        },
    )
    db.finish_workstream_op(conn, "auto-open-human-watch", state="applied")
    key = lifecycle_signals.make_candidate_key("MERGE", [watched, opened])
    _seed_candidate(
        conn,
        candidate_key=key,
        op="MERGE",
        signal=_merge_signal(watched, opened, with_direction=False),
    )
    _exercise_unmerge(conn)
    calls: list[dict] = []

    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]

    assert not plan["eligible"]
    probation = plan["policy_evidence"]["probation_merge_back"]
    assert probation["eligible"]
    assert probation["absorber_origin"] == "manual"
    assert not probation["attestation_carveout_eligible"]
    assert not plan["policy_evidence"]["probation_self_revert"]
    assert "attestation_quorum_missing" in plan["reason_codes"]
    assert calls == []

    _attest(conn, key, "S1", "S2")
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert plan["eligible"]
    assert calls[0]["source_workstream_id"] == opened
    assert calls[0]["absorber_workstream_id"] == watched
    conn.close()


def test_contacted_probation_watch_pair_does_not_self_revert_after_target(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    watched, opened = _lane(conn), _lane(conn)
    unrelated = db.insert_node(
        conn,
        kind="fact",
        title="Unrelated contact",
        body="Other work",
        status="canonical",
    )
    db.begin_workstream_op(
        conn,
        op_key="auto-open-graduated-watch",
        op="OPEN",
        origin="auto",
        dst_workstream_id=opened,
        payload={
            "assigned_member_ids": [],
            "watch_pair": [opened, watched],
            "probation": {
                "active": True,
                "opened_at": "2026-07-01 00:00:00",
                "eligible_session_target": 2,
            },
        },
    )
    db.finish_workstream_op(conn, "auto-open-graduated-watch", state="applied")
    db.record_retrieval_events(
        conn,
        source="tool",
        session_id="post-open-1",
        turn=1,
        items=[(opened, None)],
        ts="2026-07-02 12:00:00",
    )
    db.record_retrieval_events(
        conn,
        source="tool",
        session_id="post-open-2",
        turn=1,
        items=[(unrelated, None)],
        ts="2026-07-03 12:00:00",
    )

    key = lifecycle_signals.make_candidate_key("MERGE", [watched, opened])
    _seed_candidate(
        conn,
        candidate_key=key,
        op="MERGE",
        signal=_merge_signal(watched, opened, with_direction=False),
    )
    _exercise_unmerge(conn)
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]

    probation = plan["policy_evidence"]["probation_merge_back"]
    assert not probation["eligible"]
    assert not plan["policy_evidence"]["probation_self_revert"]
    assert "attestation_quorum_missing" in plan["reason_codes"]
    assert "merge_direction_missing" in plan["reason_codes"]
    assert calls == []
    conn.close()


@pytest.mark.parametrize("inactive,unrelated", [(True, False), (False, True)])
def test_inactive_or_unrelated_probation_pair_stays_suggestion(
    tmp_path, monkeypatch, inactive, unrelated,
):
    conn = _connect(tmp_path, monkeypatch)
    watched, opened, other = _lane(conn), _lane(conn), _lane(conn)
    db.begin_workstream_op(
        conn,
        op_key="auto-open-watch",
        op="OPEN",
        origin="auto",
        dst_workstream_id=opened,
        payload={
            "assigned_member_ids": [],
            "watch_pair": [opened, watched],
            "probation": {
                "active": not inactive,
                "opened_at": "2026-07-01 00:00:00",
            },
        },
    )
    db.finish_workstream_op(conn, "auto-open-watch", state="applied")
    pair = (opened, other) if unrelated else (opened, watched)
    key = lifecycle_signals.make_candidate_key("MERGE", pair)
    _seed_candidate(
        conn,
        candidate_key=key,
        op="MERGE",
        signal=_merge_signal(*pair, with_direction=False),
    )
    _attest(conn, key, "S1", "S2")
    _exercise_unmerge(conn)
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert not plan["eligible"]
    assert "merge_direction_missing" in plan["reason_codes"]
    assert calls == []
    conn.close()


def test_structurally_completed_close_is_preflighted_and_reachable(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    forward = db.insert_node(
        conn, kind="progress", title="completed milestone", body="done",
        status="canonical", workstream_id=lane,
    )
    resolver = db.insert_node(
        conn, kind="fact", title="completion proof", body="verified",
        status="canonical",
    )
    edge_id = db.add_edge_nc(
        conn, resolver, forward, "resolves", created_by="test:completion",
    )
    conn.commit()
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "completed",
        "completion_evidence": {
            "done_when": "shipped",
            "forward_member_ids": [forward],
            "resolved_forward_member_ids": [forward],
            "resolution_edge_ids": [edge_id],
            "resolution_density": 1.0,
        },
        "apply_request": {
            "workstream_id": lane,
            "outcome": "completed",
            "reason": "Done-when satisfied with complete resolution coverage",
            "dispositions": {},
        },
    }
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=signal)
    _attest(conn, key, "S1", "S2")
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    plan = result["plans"][0]
    assert plan["eligible"]
    assert plan["policy_evidence"]["completion"]["structural_completion"]
    assert plan["apply_request"]["preflight_token"]
    assert calls[0]["op"] == "CLOSE"
    assert calls[0]["force"] is False
    assert calls[0]["candidate_key"] == key
    conn.close()


@pytest.mark.parametrize("late_change", ["disagree", "pin", "activity"])
def test_in_transaction_current_plan_guard_rejects_late_policy_changes(
    tmp_path, monkeypatch, late_change,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    forward = db.insert_node(
        conn, kind="progress", title="completed milestone", body="done",
        status="canonical", workstream_id=lane,
    )
    resolver = db.insert_node(
        conn, kind="fact", title="completion proof", body="verified",
        status="canonical",
    )
    edge_id = db.add_edge_nc(
        conn, resolver, forward, "resolves", created_by="test:completion",
    )
    conn.commit()
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "completed",
        "completion_evidence": {
            "done_when": "shipped",
            "forward_member_ids": [forward],
            "resolved_forward_member_ids": [forward],
            "resolution_edge_ids": [edge_id],
            "resolution_density": 1.0,
        },
        "apply_request": {
            "workstream_id": lane,
            "outcome": "completed",
            "reason": "Done-when satisfied with complete resolution coverage",
            "dispositions": {},
        },
    }
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=signal)
    _attest(conn, key, "S1", "S2")
    module = _fake_module([])
    initial = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=module,
    )[0]
    assert initial["eligible"]

    if late_change == "disagree":
        latest = db.latest_workstream_derivation(conn)
        db.append_workstream_op_event(
            conn,
            event_key="late-disagreement",
            candidate_key=key,
            event_type="attestation",
            verdict="disagree",
            payload={},
            derivation_key=latest["derivation_key"],
            session_id="S3",
            require_latest_candidate=True,
        )
    elif late_change == "pin":
        db.pin_focus(conn, lane)
    else:
        db.record_retrieval_events(
            conn,
            source="tool",
            session_id="late-contact",
            turn=1,
            items=[(lane, None)],
            ts="2026-07-22 11:59:00",
        )
    before_ops = conn.execute("SELECT COUNT(*) FROM workstream_ops").fetchone()[0]
    assert not automation.auto_plan_is_current(
        conn,
        candidate_key=key,
        op_key=initial["op_key"],
        op="CLOSE",
        now=NOW,
        receipts_live=True,
        workstreams_module=module,
    )
    assert conn.execute("SELECT COUNT(*) FROM workstream_ops").fetchone()[0] == before_ops
    conn.close()


def test_no_contact_human_close_never_becomes_completed_from_quorum(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    key = lifecycle_signals.make_candidate_key("CLOSE", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "outcome": "abandoned",
        "reason": "no_contamination_free_contact",
        "auto_apply_eligible": False,
        "candidate_only": True,
    }
    _seed_candidate(conn, candidate_key=key, op="CLOSE", signal=signal)
    _attest(conn, key, "S1", "S2")
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert not plan["eligible"]
    assert "completion_signal_missing" in plan["reason_codes"]
    assert "close_policy_not_satisfied" in plan["reason_codes"]
    assert calls == []
    conn.close()


def test_detector_shaped_adopt_is_reachable_without_force(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    members = [
        db.insert_node(
            conn, kind=kind, title=f"adopt {kind}", body="forward", status="staging",
        )
        for kind in ("idea", "progress")
    ]
    key = lifecycle_signals.make_candidate_key("ADOPT", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "member_ids": members,
        "tier1": "multi_session_orphan_contact",
        "tier1_session_ids": ["S1", "S2"],
        "tier2_inputs": ["shared_target"],
        "shared_target_ids": [999],
        "apply_request": {
            "workstream_id": lane,
            "node_ids": members,
            "relations": {str(node_id): "advances" for node_id in members},
            "evidence": {
                "trigger": "shared_target",
                "forward_looking": True,
                "session_ids": ["S1", "S2"],
                "shared_target_ids": [999],
            },
            "allow_auto_apply": True,
        },
    }
    _seed_candidate(conn, candidate_key=key, op="ADOPT", signal=signal)
    calls: list[dict] = []
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )
    plan = result["plans"][0]
    assert plan["eligible"]
    assert calls[0]["op"] == "ADOPT"
    assert calls[0]["candidate_key"] == key
    assert calls[0]["allow_auto_apply"] is True
    assert "force" not in calls[0]
    conn.close()


def test_cluster_or_cosine_only_adopt_never_dispatches(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    lane = _lane(conn)
    member = db.insert_node(
        conn, kind="idea", title="cluster member", body="forward", status="staging",
    )
    key = lifecycle_signals.make_candidate_key("ADOPT", [lane])
    signal = {
        "qualified": True,
        "workstream_id": lane,
        "member_ids": [member],
        "apply_request": {
            "workstream_id": lane,
            "node_ids": [member],
            "relations": {str(member): "advances"},
            "evidence": {"trigger": "cluster", "forward_looking": True},
            "allow_auto_apply": True,
        },
    }
    _seed_candidate(conn, candidate_key=key, op="ADOPT", signal=signal)
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    assert not plan["eligible"]
    assert "adopt_corroboration_incomplete" in plan["reason_codes"]
    assert plan["policy_evidence"]["cluster_or_cosine_only"]
    assert calls == []
    conn.close()


def test_trailing_regret_rate_demotes_an_otherwise_eligible_plan(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    db.begin_workstream_op(
        conn,
        op_key="auto-merge-regretted",
        op="MERGE",
        origin="auto",
        payload=_synthetic_merge_receipt_payload(),
    )
    db.finish_workstream_op(conn, "auto-merge-regretted", state="applied")
    db.begin_workstream_op(
        conn,
        op_key="manual-unmerge-regret",
        op="UNMERGE",
        origin="manual",
        candidate_key="unmerge:auto-merge-regretted",
        payload={"merge_op_key": "auto-merge-regretted"},
    )
    db.finish_workstream_op(conn, "manual-unmerge-regret", state="applied")
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["regret"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=_open_signal())
    calls: list[dict] = []
    plan = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module(calls),
    )["plans"][0]
    regret = plan["policy_evidence"]["trailing_regret"]
    assert regret["numerator"] == 1
    assert regret["denominator"] == 1
    assert regret["rate"] == 1.0
    assert "trailing_regret_rate_exceeded" in plan["reason_codes"]
    assert calls == []
    conn.close()


def test_ambiguous_regret_linkage_fails_closed(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    db.begin_workstream_op(
        conn,
        op_key="auto-merge-ambiguous",
        op="MERGE",
        origin="auto",
        payload=_synthetic_merge_receipt_payload(),
    )
    db.finish_workstream_op(conn, "auto-merge-ambiguous", state="applied")
    db.begin_workstream_op(
        conn,
        op_key="mismatched-unmerge",
        op="UNMERGE",
        origin="manual",
        candidate_key="unmerge:auto-merge-ambiguous",
        payload={"merge_op_key": "some-other-merge"},
    )
    db.finish_workstream_op(conn, "mismatched-unmerge", state="applied")
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["ambiguous"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=_open_signal())
    plan = automation.plan_actions(
        conn, now=NOW, receipts_live=True, workstreams_module=_fake_module([]),
    )[0]
    regret = plan["policy_evidence"]["trailing_regret"]
    assert regret["ambiguous_count"] == 1
    assert not regret["complete"]
    assert "trailing_regret_linkage_ambiguous" in plan["reason_codes"]
    conn.close()


def test_returned_terminal_failure_is_not_reported_as_applied(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["returned-failure"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=_open_signal())
    calls: list[dict] = []

    def failed_open(conn, **kwargs):
        calls.append(kwargs)
        return {"ok": False, "state": "failed", "error_code": "preflight_stale"}

    result = automation.run_governed(
        conn,
        now=NOW,
        receipts_live=True,
        workstreams_module=SimpleNamespace(open_workstream=failed_open),
    )
    assert len(calls) == 1
    assert result["applied"] == []
    assert result["failed"][0]["error"] == "preflight_stale"
    conn.close()


def test_raised_failure_gets_privacy_safe_terminal_ledger(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["raised-failure"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=_open_signal())
    calls: list[dict] = []

    def raised_open(conn, **kwargs):
        calls.append(kwargs)
        raise ValueError("secret exception content")

    module = SimpleNamespace(open_workstream=raised_open)
    result = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=module,
    )
    assert result["applied"] == []
    assert len(result["failed"]) == 1
    op_key = result["failed"][0]["op_key"]
    ledger = db.get_workstream_op(conn, op_key)
    assert ledger["state"] == "failed"
    assert ledger["error_code"] == "invalid_payload"
    assert ledger["candidate_key"] == key
    assert "secret exception content" not in ledger["payload_json"]
    assert ledger["payload"]["failure"] == {"error_class": "ValueError"}
    retry = automation.run_governed(
        conn, now=NOW, receipts_live=True, workstreams_module=module,
    )
    assert len(calls) == 1
    assert "operation_failed" in retry["plans"][0]["reason_codes"]
    conn.close()


@pytest.mark.parametrize(
    ("op", "plan_request", "field", "replacement"),
    [
        (
            "OPEN",
            {
                "title": "Bound lane", "objective": "ship", "done_when": "done",
                "scope_boundary": "repo", "next_step": "test", "member_ids": [1],
                "recurrence": {
                    "session_ids": ["S1", "S2"], "session_count": 2,
                    "since": "2026-07-01",
                },
            },
            "member_ids",
            [999],
        ),
        (
            "MERGE",
            {
                "source_workstream_id": 1, "absorber_workstream_id": 2,
                "dispositions": {}, "evidence": {"trigger": "co_contact"},
                "preflight_token": "merge-token",
            },
            "absorber_workstream_id",
            999,
        ),
        (
            "CLOSE",
            {
                "workstream_id": 1, "outcome": "completed", "reason": "done",
                "dispositions": {}, "preflight_token": "close-token",
            },
            "workstream_id",
            999,
        ),
        (
            "ADOPT",
            {
                "workstream_id": 1, "node_ids": [2],
                "relations": {"2": "advances"},
                "evidence": {"forward_looking": True, "trigger": "declared_intent"},
                "allow_auto_apply": True,
            },
            "node_ids",
            [999],
        ),
    ],
)
def test_current_plan_guard_binds_exact_operation_payload(
    tmp_path, monkeypatch, op, plan_request, field, replacement,
):
    conn = _connect(tmp_path, monkeypatch)
    plan = {
        "candidate_key": f"candidate:{op.lower()}",
        "op_key": f"operation:{op.lower()}",
        "op": op,
        "eligible": True,
        "apply_request": plan_request,
    }
    monkeypatch.setattr(automation, "plan_actions", lambda *_a, **_k: [plan])
    _operation, envelope = automation._prepare_operation_request(
        plan, module=workstreams, project_path=None,
    )
    assert automation.auto_plan_is_current(
        conn,
        candidate_key=plan["candidate_key"],
        op_key=plan["op_key"],
        op=op,
        operation_request=envelope,
        workstreams_module=workstreams,
    )
    substituted = copy.deepcopy(envelope)
    substituted[field] = replacement
    assert not automation.auto_plan_is_current(
        conn,
        candidate_key=plan["candidate_key"],
        op_key=plan["op_key"],
        op=op,
        operation_request=substituted,
        workstreams_module=workstreams,
    )
    conn.close()


def test_raised_failure_ledger_is_one_transaction(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    key = lifecycle_signals.make_candidate_key("OPEN", [], ["atomic-failure"])
    _seed_candidate(conn, candidate_key=key, op="OPEN", signal=_open_signal())

    def raised_open(_conn, **_kwargs):
        raise ValueError("private failure")

    def fail_terminal(*_args, **_kwargs):
        raise RuntimeError("injected terminal-write failure")

    monkeypatch.setattr(db, "finish_workstream_op_nc", fail_terminal)
    result = automation.run_governed(
        conn,
        now=NOW,
        receipts_live=True,
        project_path=str(tmp_path),
        workstreams_module=SimpleNamespace(open_workstream=raised_open),
    )
    op_key = result["failed"][0]["op_key"]
    assert db.get_workstream_op(conn, op_key) is None
    assert conn.in_transaction is False
    conn.close()
