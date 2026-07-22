"""S5 compactor proposal/attestation contract."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import compactor  # noqa: E402
import db  # noqa: E402
import lifecycle_signals  # noqa: E402


def _connect(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    return db.connect(str(project))


def _node(conn, *, kind="fact", title="node", workstream_id=None):
    return db.insert_node(
        conn,
        kind=kind,
        title=title,
        body=f"body for {title}",
        workstream_id=workstream_id,
    )


def _charter():
    return (
        "Objective: keep lifecycle judgment deterministic.\n"
        "Done when: proposals are validated without mutation.\n"
        "Scope boundary: compactor events only.\n"
        "Next step: let governed automation decide later."
    )


def test_prompt_exposes_proposals_attestations_and_non_mutation_contract():
    for marker in (
        '"workstream_proposals"',
        '"attestations"',
        "merge_candidate",
        "close_candidate",
        "open_candidate",
        "Objective:",
        "Done when:",
        "Scope boundary:",
        "Next step:",
        "NEVER mutate a lane",
        "Never invent member ids",
    ):
        assert marker in compactor.COMPACT_PROMPT
    assert compactor._has_compaction_content({
        "session_summary": {"body": ""},
        "workstream_proposals": [{"proposal_key": "p"}],
    })


def test_latest_candidates_are_bounded_role_rows(tmp_path, monkeypatch):
    conn = _connect(tmp_path)
    old_merge = lifecycle_signals.make_candidate_key("MERGE", [1, 2])
    db.record_workstream_derivation(
        conn,
        derivation_key="old",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{"candidate_key": old_merge, "op": "MERGE", "signal": {}}],
    )
    open_key = lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b"])
    merge_key = lifecycle_signals.make_candidate_key("MERGE", [10, 11])
    close_key = lifecycle_signals.make_candidate_key("CLOSE", [12])
    adopt_key = lifecycle_signals.make_candidate_key("ADOPT", [13])
    db.record_workstream_derivation(
        conn,
        derivation_key="latest",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[
            {"candidate_key": open_key, "op": "OPEN",
             "signal": {"member_ids": [20, 21], "contact_sessions": ["S1", "S2"]}},
            {"candidate_key": merge_key, "op": "MERGE",
             "signal": {"left": 10, "right": 11, "co_contact_sessions": 4,
                        "jaccard": 0.8, "shared_target_ids": [30]}},
            {"candidate_key": close_key, "op": "CLOSE",
             "signal": {"workstream_id": 12, "evidence_ids": [31]}},
            {"candidate_key": adopt_key, "op": "ADOPT", "signal": {}},
        ],
    )
    rows = compactor._merge_lifecycle_candidate_rows(
        conn, [{"id": 99, "kind": "fact", "title": "existing"}],
    )
    by_role = {row.get("role"): row for row in rows if row.get("role")}
    assert set(by_role) == {"open_candidate", "merge_candidate", "close_candidate"}
    assert by_role["open_candidate"]["member_ids"] == [20, 21]
    assert by_role["merge_candidate"]["workstream_ids"] == [10, 11]
    assert by_role["merge_candidate"]["evidence_ids"] == [10, 11, 30]
    assert old_merge not in {row.get("candidate_key") for row in rows}
    assert adopt_key not in {row.get("candidate_key") for row in rows}

    monkeypatch.setattr(compactor, "COMPACTOR_ATTESTATION_CANDIDATE_LIMIT", 1)
    bounded = compactor._merge_lifecycle_candidate_rows(conn, [])
    assert len([row for row in bounded if row.get("role") in {
        "merge_candidate", "close_candidate",
    }]) == 1
    conn.close()


def test_attestations_require_latest_merge_or_close_candidate(tmp_path):
    conn = _connect(tmp_path)
    lane_a = _node(conn, kind="workstream", title="Lane A")
    lane_b = _node(conn, kind="workstream", title="Lane B")
    evidence = _node(conn, title="shared decision evidence")
    candidate = lifecycle_signals.make_candidate_key("MERGE", [lane_a, lane_b])
    db.record_workstream_derivation(
        conn,
        derivation_key="d1",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{
            "candidate_key": candidate,
            "op": "MERGE",
            "signal": {"lane_ids": [lane_a, lane_b], "evidence_ids": [evidence]},
        }],
    )
    result = compactor._apply_lifecycle_judgments(
        conn,
        "attest-session",
        {"attestations": [{
            "candidate_key": candidate,
            "verdict": "agree",
            "evidence_ids": [evidence],
        }]},
        title_to_id={},
    )
    assert result["attestations_recorded"] == 1
    row = conn.execute("SELECT * FROM workstream_op_events").fetchone()
    assert row["event_type"] == "attestation"
    assert row["verdict"] == "agree"
    assert row["session_id"] == "attest-session"
    assert json.loads(row["payload_json"])["evidence_ids"] == [evidence]

    outsider = _node(conn, title="not candidate evidence")
    for session_id, supplied_ids in (
        ("empty-evidence-session", []),
        ("invalid-evidence-session", [outsider]),
    ):
        rejected = compactor._apply_lifecycle_judgments(
            conn,
            session_id,
            {"attestations": [{
                "candidate_key": candidate,
                "verdict": "agree",
                "evidence_ids": supplied_ids,
            }]},
            title_to_id={},
        )
        assert rejected["attestations_recorded"] == 0
    assert conn.execute("SELECT COUNT(*) FROM workstream_op_events").fetchone()[0] == 1

    # Same candidate/session is idempotent; a later empty derivation makes the
    # old candidate ineligible rather than recording stale judgment.
    compactor._apply_lifecycle_judgments(
        conn,
        "attest-session",
        {"attestations": [{
            "candidate_key": candidate, "verdict": "agree", "evidence_ids": [evidence],
        }]},
        title_to_id={},
    )
    assert conn.execute("SELECT COUNT(*) FROM workstream_op_events").fetchone()[0] == 1
    db.record_workstream_derivation(
        conn,
        derivation_key="d2",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[],
    )
    stale = compactor._apply_lifecycle_judgments(
        conn,
        "another-session",
        {"attestations": [{
            "candidate_key": candidate, "verdict": "agree", "evidence_ids": [evidence],
        }]},
        title_to_id={},
    )
    assert stale["attestations_recorded"] == 0
    assert conn.execute("SELECT COUNT(*) FROM workstream_op_events").fetchone()[0] == 1
    conn.close()


def test_open_proposal_accepts_verified_recurrence_without_mutating_lanes(tmp_path):
    conn = _connect(tmp_path)
    member_a = _node(conn, title="member A")
    member_b = _node(conn, kind="progress", title="member B")
    db.record_retrieval_events(
        conn, source="prompt", session_id="S1", turn=1,
        items=[(member_a, None), (member_b, None)],
    )
    db.record_retrieval_events(
        conn, source="gate", session_id="S2", turn=2,
        items=[(member_a, None), (member_b, None)],
    )
    candidate = lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b"])
    db.record_workstream_derivation(
        conn,
        derivation_key="open-latest",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{
            "candidate_key": candidate,
            "op": "OPEN",
            "signal": {"member_ids": [member_a, member_b], "session_ids": ["S1", "S2"]},
        }],
    )
    before_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    outcome = compactor._apply_lifecycle_judgments(
        conn,
        "proposal-session",
        {"workstream_proposals": [{
            "proposal_key": "auth-cleanup",
            "candidate_key": candidate,
            "title": "Auth cleanup",
            "charter_body": _charter(),
            "seed_member_ids": [member_a, member_b],
            "member_titles": ["member A", "member B"],
            "recurrence_evidence": {"session_ids": ["S1", "S2"]},
        }]},
        title_to_id={},
    )
    assert outcome["proposals_accepted"] == 1
    assert outcome["proposals_rejected"] == 0
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before_nodes
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'workstream'",
    ).fetchone()[0] == 0
    event = conn.execute("SELECT * FROM workstream_op_events").fetchone()
    assert event["event_type"] == "proposal_accepted"
    payload = json.loads(event["payload_json"])
    assert payload["member_ids"] == [member_a, member_b]
    assert "force" not in payload
    assert "Scope boundary:" in payload["charter_body"]
    assert payload["proposal_validated"] is True
    assert payload["proposal_source"] == "compactor"
    assert payload["recurrence"]["session_count"] == 2
    assert payload["objective"] == "keep lifecycle judgment deterministic."
    conn.close()


@pytest.mark.parametrize(
    ("change", "expected_reason"),
    [
        ({"seed_member_ids": [999999]}, "unknown_member_id"),
        ({"member_titles": ["hallucinated title"]}, "unknown_or_ambiguous_member_title"),
        ({"recurrence_evidence": {"session_ids": ["S1"]}}, "insufficient_tier1_recurrence"),
        ({"force": True}, "force_not_allowed"),
        ({"charter_body": "Objective: only one line"}, "missing_charter_done_when"),
    ],
)
def test_open_proposal_rejections_are_events_only(tmp_path, change, expected_reason):
    conn = _connect(tmp_path)
    member_a = _node(conn, title="member A")
    member_b = _node(conn, title="member B")
    for session in ("S1", "S2"):
        db.record_retrieval_events(
            conn, source="prompt", session_id=session, turn=1,
            items=[(member_a, None), (member_b, None)],
        )
    candidate = lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b"])
    db.record_workstream_derivation(
        conn,
        derivation_key="open-latest",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{
            "candidate_key": candidate,
            "op": "OPEN",
            "signal": {"member_ids": [member_a, member_b]},
        }],
    )
    proposal = {
        "proposal_key": "candidate-proposal",
        "candidate_key": candidate,
        "title": "Candidate lane",
        "charter_body": _charter(),
        "seed_member_ids": [member_a, member_b],
        "member_titles": ["member A", "member B"],
        "recurrence_evidence": {"session_ids": ["S1", "S2"]},
    }
    proposal.update(change)
    before = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    outcome = compactor._apply_lifecycle_judgments(
        conn,
        "reject-session",
        {"workstream_proposals": [proposal]},
        title_to_id={},
    )
    assert outcome["proposals_rejected"] == 1
    event = conn.execute("SELECT * FROM workstream_op_events").fetchone()
    assert event["event_type"] == "proposal_rejected"
    assert expected_reason in json.loads(event["payload_json"])["reasons"]
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before
    conn.close()


def test_shared_feeder_target_can_supply_tier1_analog(tmp_path):
    conn = _connect(tmp_path)
    member_a = _node(conn, title="member A")
    member_b = _node(conn, title="member B")
    target = _node(conn, kind="decision", title="shared target")
    db.add_edge(conn, member_a, target, "advances")
    db.add_edge(conn, member_b, target, "motivates")
    candidate = lifecycle_signals.make_candidate_key("OPEN", [], ["a", "b"])
    db.record_workstream_derivation(
        conn,
        derivation_key="open-target",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{
            "candidate_key": candidate,
            "op": "OPEN",
            "signal": {"member_ids": [member_a, member_b], "shared_target_ids": [target]},
        }],
    )
    outcome = compactor._apply_lifecycle_judgments(
        conn,
        "target-session",
        {"workstream_proposals": [{
            "proposal_key": "shared-target",
            "candidate_key": candidate,
            "title": "Shared-target lane",
            "charter_body": _charter(),
            "seed_member_ids": [member_a, member_b],
            "recurrence_evidence": {"shared_target_ids": [target]},
        }]},
        title_to_id={},
    )
    assert outcome["proposals_accepted"] == 1
    conn.close()


def test_priority_nodes_are_not_exposed_or_accepted_as_proposal_members(tmp_path):
    conn = _connect(tmp_path)
    member = _node(conn, kind="idea", title="valid member")
    priority = _node(conn, kind="priority", title="standing priority")
    candidate = lifecycle_signals.make_candidate_key("OPEN", [], ["priority-boundary"])
    db.record_workstream_derivation(
        conn,
        derivation_key="priority-open",
        substrate_version=lifecycle_signals.SUBSTRATE_VERSION,
        candidates=[{
            "candidate_key": candidate,
            "op": "OPEN",
            "signal": {"member_ids": [member, priority]},
        }],
    )

    row = next(
        item for item in compactor._merge_lifecycle_candidate_rows(conn, [])
        if item.get("candidate_key") == candidate
    )
    assert row["member_ids"] == [member]
    assert priority not in row["member_ids"]

    outcome = compactor._apply_lifecycle_judgments(
        conn,
        "priority-proposal",
        {"workstream_proposals": [{
            "proposal_key": "must-reject-priority",
            "candidate_key": candidate,
            "title": "Invalid lane",
            "charter_body": _charter(),
            "seed_member_ids": [member, priority],
            "recurrence_evidence": {"session_ids": []},
        }]},
        title_to_id={},
    )
    assert outcome["proposals_rejected"] == 1
    event = conn.execute(
        "SELECT payload_json FROM workstream_op_events WHERE event_type='proposal_rejected'"
    ).fetchone()
    assert "priority_cannot_be_seed_member" in json.loads(event["payload_json"])["reasons"]
    conn.close()
