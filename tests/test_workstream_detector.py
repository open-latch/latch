from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import workstream_detector as detector  # noqa: E402


NOW = "2026-07-22 12:00:00"


def _base_inputs() -> dict:
    return {
        "now": NOW,
        "eligible_sessions": [
            {"session_id": f"S{i}", "started_at": f"2026-07-{16 + i:02d} 12:00:00"}
            for i in range(1, 6)
        ],
        "contacts": {},
        "lanes": {},
        "tier2": {},
        "tier3": {},
        "orphan_groups": [],
        "orphan_events": {},
        "orphan_count": 0,
        "observation_start": "2026-06-01 00:00:00",
        "recent_active_lane_count": 0,
        "recent_active_lane_cap": 12,
        "probation_lanes": {},
    }


def _lane(lane_id: int, **overrides) -> dict:
    row = {
        "id": lane_id,
        "title": f"Lane {lane_id}",
        "status": "canonical",
        "created_at": "2026-05-01 00:00:00",
        "updated_at": "2026-07-01 00:00:00",
        "pinned": False,
        "open_feeder_count": 0,
        "active_priority_count": 0,
        "last_contact_at": None,
        "member_activity_at": None,
        "active_member_count": 0,
        "member_ids": [],
        "unknown_inbound_edge_ids": [],
        "done_when": None,
        "forward_member_ids": [],
        "resolved_forward_member_ids": [],
        "resolution_edge_ids": [],
        "resolution_evidence": [],
    }
    row.update(overrides)
    return row


def _open_group(*, member_ids=(10, 11, 12, 13), kinds=None) -> dict:
    ids = list(member_ids)
    if kinds is None:
        kinds = ["idea", "progress", "decision", "open_question"][:len(ids)]
    dates = [
        "2026-07-17 00:00:00",
        "2026-07-18 00:00:00",
        "2026-07-19 00:00:00",
        "2026-07-19 01:00:00",
        "2026-07-20 00:00:00",
        "2026-07-21 00:00:00",
    ]
    return {
        "tree_id": 90,
        "tree_derived_at": "2026-07-22 00:00:00",
        "tree_time_source": "lifecycle_log",
        "member_ids": ids,
        "member_kinds": list(kinds),
        "member_created_at": dates[:len(ids)],
        "member_content_hashes": [None] * len(ids),
        "member_embeddings": {},
        "member_artifacts": {},
        "fact_linked_to_nonfact_ids": [],
        "target_member_ids": {},
    }


def _contacted_open_inputs(group: dict) -> dict:
    inputs = _base_inputs()
    inputs["orphan_count"] = len(group["member_ids"])
    inputs["orphan_groups"] = [group]
    inputs["orphan_events"] = {
        group["member_ids"][0]: [
            {"session_id": "S1", "ts": "2026-07-18 00:00:00", "source": "prompt"},
            {"session_id": "S2", "ts": "2026-07-20 00:00:00", "source": "write"},
        ]
    }
    return inputs


def test_merge_requires_tier1_threshold_and_tier2_corroboration():
    inputs = _base_inputs()
    inputs["lanes"] = {1: _lane(1), 2: _lane(2)}
    inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4", "S5"],
    }
    snapshot = detector.derive_shadow_snapshot(inputs)
    assert [row for row in snapshot["candidates"] if row["op"] == "MERGE"] == []

    inputs["tier2"] = {
        "1:2": {"shared_target_ids": [70, 71], "cross_path_sessions": []}
    }
    snapshot = detector.derive_shadow_snapshot(inputs)
    merge = next(row for row in snapshot["candidates"] if row["op"] == "MERGE")
    assert merge["signal"]["co_contact_sessions"] == 4
    assert merge["signal"]["jaccard"] == 0.8
    assert merge["signal"]["tier2_inputs"] == ["shared_targets"]
    assert merge["signal"]["candidate_payload_binding"] \
        == detector.lifecycle_signals.make_candidate_payload_binding(
            "MERGE", merge["signal"],
        )
    assert merge["candidate_key"] == detector.lifecycle_signals.candidate_evidence_key(
        merge["signal"]["base_candidate_key"],
        merge["signal"]["candidate_payload_binding"],
    )


def test_merge_direction_and_apply_request_fail_closed_on_ambiguity():
    inputs = _base_inputs()
    inputs["lanes"] = {
        1: _lane(1, active_member_count=1),
        2: _lane(2, active_member_count=3),
    }
    inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4"],
    }
    inputs["tier2"] = {
        "1:2": {"shared_target_ids": [70, 71], "cross_path_sessions": []}
    }
    candidate = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "MERGE"
    )
    assert candidate["signal"]["direction"] == {
        "source_workstream_id": 1,
        "absorber_workstream_id": 2,
        "basis": "active_member_count",
        "unambiguous": True,
    }
    assert candidate["signal"]["apply_request"]["dispositions"] == {}

    inputs["lanes"][1]["active_member_count"] = 3
    inputs["lanes"][2]["active_member_count"] = 1
    reversed_direction = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "MERGE"
    )
    assert reversed_direction["signal"]["base_candidate_key"] \
        == candidate["signal"]["base_candidate_key"]
    assert reversed_direction["candidate_key"] != candidate["candidate_key"]
    inputs["lanes"][1]["active_member_count"] = 1
    inputs["lanes"][2]["active_member_count"] = 3

    inputs["lanes"][1]["unknown_inbound_edge_ids"] = [501]
    unsafe = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "MERGE"
    )
    assert unsafe["signal"]["ambiguity_reason"] == "unknown_inbound_relations"
    assert "apply_request" not in unsafe["signal"]

    inputs["lanes"][1]["unknown_inbound_edge_ids"] = []
    inputs["lanes"][1]["active_member_count"] = 3
    tied = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "MERGE"
    )
    assert tied["signal"]["ambiguity_reason"] == "merge_direction_tied"
    assert "apply_request" not in tied["signal"]


@pytest.mark.parametrize(
    ("active", "retained", "expected_source", "expected_basis"),
    [
        (True, True, 2, "probation_watch_pair"),
        (False, True, 1, "active_member_count"),
        (True, False, 1, "active_member_count"),
    ],
)
def test_merge_direction_uses_only_current_probation_watch_pairs(
    active, retained, expected_source, expected_basis,
):
    inputs = _base_inputs()
    inputs["lanes"] = {
        1: _lane(1, active_member_count=1),
        2: _lane(2, active_member_count=3),
    }
    inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4"],
    }
    inputs["tier2"] = {
        "1:2": {"shared_target_ids": [70, 71], "cross_path_sessions": []},
    }
    inputs["probation_lanes"] = {
        2: {
            "active": active,
            "window_within_retention": retained,
            "watch_pair": [2, 1],
        },
    }

    candidate = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "MERGE"
    )

    assert candidate["signal"]["direction"]["source_workstream_id"] \
        == expected_source
    assert candidate["signal"]["direction"]["basis"] == expected_basis


def test_heal_signals_are_bounded_tier2_and_expose_qualifying_source(monkeypatch):
    rows = [
        {
            "event": "cross_lane_duplicate",
            "node_a": index,
            "node_b": 100 + index,
            "ws_a": 2,
            "ws_b": 1,
            "ts": "2026-07-20 00:00:00",
            "reason": "must never enter detector evidence",
        }
        for index in range(25)
    ]
    rows.append({
        "event": "cross_lane_duplicate", "node_a": 999, "node_b": 1000,
        "ws_a": None, "ws_b": 1, "ts": "2026-07-20 00:00:00",
    })
    monkeypatch.setattr(detector.log_utils, "read_log_range", lambda *_a, **_k: rows)
    projected = detector._latest_heal_pair_signals(
        "/project", now=detector._parse_ts(NOW),
    )
    assert len(projected[(1, 2)]) == detector.HEAL_SIGNALS_PER_PAIR_LIMIT
    assert set(projected[(1, 2)][0]) == {
        "event", "node_a", "node_b", "workstream_ids",
    }

    inputs = _base_inputs()
    inputs["lanes"] = {
        1: _lane(1, active_member_count=1),
        2: _lane(2, active_member_count=2),
    }
    inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4"],
    }
    inputs["tier2"] = {"1:2": {"heal_signals": projected[(1, 2)]}}
    merge = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "MERGE"
    )
    assert merge["signal"]["tier2_qualified_by"] == ["cross_lane_duplicate"]
    assert len(merge["signal"]["heal_signals"]) \
        == detector.HEAL_SIGNALS_PER_PAIR_LIMIT


def test_weak_tier3_annotations_never_replace_tier1_or_tier2():
    inputs = _base_inputs()
    inputs["lanes"] = {1: _lane(1), 2: _lane(2)}
    inputs["tier3"] = {
        "1:2": {
            "qualification_role": "annotation_only",
            "artifact_overlap_count": 8,
            "tree_co_membership": True,
            "centroid_cosine": 0.999,
            "centroid_fisher_z": 3.8,
        }
    }
    inputs["contacts"] = {
        1: ["S1", "S2", "S3"],
        2: ["S1", "S2", "S3"],
    }
    inputs["tier2"] = {"1:2": {"shared_target_ids": [70, 71]}}
    below_tier1 = detector.derive_shadow_snapshot(inputs)
    assert not any(row["op"] == "MERGE" for row in below_tier1["candidates"])
    assert below_tier1["tier3_annotations"]["1:2"]["qualification_role"] \
        == "annotation_only"

    inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4"],
    }
    inputs["tier2"] = {}
    no_tier2 = detector.derive_shadow_snapshot(inputs)
    assert not any(row["op"] == "MERGE" for row in no_tier2["candidates"])


def test_open_tree_group_never_qualifies_without_multi_session_tier1():
    inputs = _base_inputs()
    inputs["orphan_count"] = 4
    inputs["orphan_groups"] = [{
        "tree_id": 90,
        "tree_derived_at": "2026-07-22 00:00:00",
        "tree_time_source": "lifecycle_log",
        "member_ids": [10, 11, 12, 13],
        "member_kinds": ["idea", "fact", "progress", "decision"],
        "member_created_at": [
            "2026-07-18 00:00:00", "2026-07-19 00:00:00",
            "2026-07-20 00:00:00", "2026-07-20 01:00:00",
        ],
        "fact_linked_to_nonfact_ids": [11],
    }]
    tree_only = detector.derive_shadow_snapshot(inputs)
    assert not tree_only["open_groups"][0]["qualified"]
    assert not any(row["op"] == "OPEN" for row in tree_only["candidates"])

    inputs["orphan_events"] = {
        10: [
            {"session_id": "S1", "ts": "2026-07-18 00:00:00", "source": "prompt"},
            {"session_id": "S2", "ts": "2026-07-20 00:00:00", "source": "write"},
        ],
        11: [
            {"session_id": "S2", "ts": "2026-07-22 00:00:00", "source": "prompt"},
        ],
    }
    qualified = detector.derive_shadow_snapshot(inputs)
    opened = next(row for row in qualified["candidates"] if row["op"] == "OPEN")
    assert opened["signal"]["tier1"] == "multi_session_orphan_contact"
    assert opened["signal"]["tree_role"] == "grouping_only"


def test_stale_tree_signal_is_counted_and_never_qualifies():
    inputs = _base_inputs()
    inputs["orphan_count"] = 4
    inputs["orphan_groups"] = [{
        "tree_id": 90,
        "tree_derived_at": "2026-07-01 00:00:00",
        "tree_time_source": "lifecycle_log",
        "member_ids": [10, 11, 12, 13],
        "member_kinds": ["idea", "fact", "progress", "decision"],
        "member_created_at": ["2026-06-01 00:00:00"] * 4,
    }]
    inputs["orphan_events"] = {
        node_id: [
            {"session_id": "S1", "ts": "2026-07-18 00:00:00", "source": "prompt"},
            {"session_id": "S2", "ts": "2026-07-20 00:00:00", "source": "prompt"},
            {"session_id": "S2", "ts": "2026-07-22 00:00:00", "source": "write"},
        ]
        for node_id in (10, 11, 12, 13)
    }
    snapshot = detector.derive_shadow_snapshot(inputs)
    assert snapshot["counters"]["stale_tree_signal"] == 1
    assert snapshot["open_groups"][0]["reason"] == "stale_tree_signal"
    assert not any(row["op"] == "OPEN" for row in snapshot["candidates"])


def test_open_dedupes_content_hash_and_cosine_units_before_counting():
    group = _open_group(member_ids=(10, 11, 12, 13, 14), kinds=[
        "idea", "idea", "progress", "decision", "open_question",
    ])
    group["member_content_hashes"] = ["same", "same", "p", "d", "q"]
    inputs = _contacted_open_inputs(group)
    first = detector.derive_shadow_snapshot(inputs)
    opened = next(row for row in first["candidates"] if row["op"] == "OPEN")
    assert opened["signal"]["raw_member_count"] == 5
    assert opened["signal"]["unit_count"] == 4
    assert [10, 11] in opened["signal"]["deduped_units"]

    cosine_group = _open_group(member_ids=(20, 21, 22, 23, 24), kinds=[
        "idea", "idea", "progress", "decision", "open_question",
    ])
    cosine_group["member_embeddings"] = {
        "20": [1.0, 0.0, 0.0, 0.0],
        "21": [0.99, 0.05, 0.0, 0.0],
        "22": [0.0, 1.0, 0.0, 0.0],
        "23": [0.0, 0.0, 1.0, 0.0],
        "24": [0.0, 0.0, 0.0, 1.0],
    }
    cosine = detector.derive_shadow_snapshot(_contacted_open_inputs(cosine_group))
    candidate = next(row for row in cosine["candidates"] if row["op"] == "OPEN")
    assert candidate["signal"]["unit_count"] == 4
    assert [20, 21] in candidate["signal"]["deduped_units"]


def test_open_rejects_unlinked_facts_low_diversity_and_day_concentration():
    fact_group = _open_group(
        member_ids=(10, 11, 12, 13),
        kinds=["idea", "progress", "decision", "fact"],
    )
    fact_snapshot = detector.derive_shadow_snapshot(_contacted_open_inputs(fact_group))
    assert fact_snapshot["open_groups"][0]["unit_count"] == 3
    assert fact_snapshot["open_groups"][0]["fact_units_excluded"] == 1
    assert not any(row["op"] == "OPEN" for row in fact_snapshot["candidates"])

    contact_only_fact = _open_group(
        member_ids=(10, 11, 12, 13, 14),
        kinds=["idea", "progress", "decision", "open_question", "fact"],
    )
    contact_only_inputs = _base_inputs()
    contact_only_inputs["orphan_count"] = 5
    contact_only_inputs["orphan_groups"] = [contact_only_fact]
    contact_only_inputs["orphan_events"] = {
        14: [
            {"session_id": "S1", "ts": "2026-07-18 00:00:00", "source": "prompt"},
            {"session_id": "S2", "ts": "2026-07-20 00:00:00", "source": "write"},
        ],
    }
    contact_only_snapshot = detector.derive_shadow_snapshot(contact_only_inputs)
    assert contact_only_snapshot["open_groups"][0]["unit_count"] == 4
    assert contact_only_snapshot["open_groups"][0]["contact_session_count"] == 0
    assert not any(
        row["op"] == "OPEN" for row in contact_only_snapshot["candidates"]
    )

    eligible_contact_snapshot = detector.derive_shadow_snapshot(
        _contacted_open_inputs(contact_only_fact)
    )
    eligible_candidate = next(
        row for row in eligible_contact_snapshot["candidates"]
        if row["op"] == "OPEN"
    )
    assert eligible_candidate["signal"]["member_ids"] == [10, 11, 12, 13]

    one_kind = _open_group(kinds=["idea", "idea", "idea", "idea"])
    one_kind_snapshot = detector.derive_shadow_snapshot(_contacted_open_inputs(one_kind))
    assert not one_kind_snapshot["open_groups"][0]["diversity_guard"]
    assert not any(row["op"] == "OPEN" for row in one_kind_snapshot["candidates"])

    concentrated = _open_group(
        member_ids=(10, 11, 12, 13, 14),
        kinds=["idea", "progress", "decision", "open_question", "idea"],
    )
    concentrated["member_created_at"] = [
        "2026-07-17 00:00:00", "2026-07-17 01:00:00",
        "2026-07-17 02:00:00", "2026-07-19 00:00:00",
        "2026-07-20 00:00:00",
    ]
    concentrated_snapshot = detector.derive_shadow_snapshot(
        _contacted_open_inputs(concentrated)
    )
    assert not concentrated_snapshot["open_groups"][0]["half_day_guard"]
    assert not any(row["op"] == "OPEN" for row in concentrated_snapshot["candidates"])


def test_priority_nodes_never_enter_open_or_adopt_member_sets():
    open_group = _open_group(
        member_ids=(10, 11, 12, 13, 14),
        kinds=["idea", "progress", "decision", "open_question", "priority"],
    )
    open_inputs = _contacted_open_inputs(open_group)
    open_inputs["orphan_events"][14] = [
        {"session_id": "S3", "ts": "2026-07-21 00:00:00", "source": "prompt"},
    ]
    open_snapshot = detector.derive_shadow_snapshot(open_inputs)
    opened = next(row for row in open_snapshot["candidates"] if row["op"] == "OPEN")
    assert opened["signal"]["member_ids"] == [10, 11, 12, 13]
    assert open_snapshot["open_groups"][0]["member_ids"] == [10, 11, 12, 13]
    assert 14 not in opened["signal"]["counted_member_ids"]

    adopt_group = _open_group(
        member_ids=(20, 21, 22), kinds=["idea", "progress", "priority"],
    )
    adopt_group["lane_member_ids"] = {"1": [101]}
    adopt_group["adopt_shared_targets"] = {"1": [99]}
    adopt_group["target_member_ids"] = {"99": [20, 101]}
    adopt_inputs = _base_inputs()
    adopt_inputs["lanes"] = {1: _lane(1)}
    adopt_inputs["orphan_groups"] = [adopt_group]
    adopt_inputs["orphan_count"] = 3
    adopt_inputs["orphan_events"] = {
        20: [{"session_id": "S1", "ts": "2026-07-19 00:00:00"}],
        21: [{"session_id": "S2", "ts": "2026-07-20 00:00:00"}],
        22: [{"session_id": "S3", "ts": "2026-07-21 00:00:00"}],
    }
    adopt_snapshot = detector.derive_shadow_snapshot(adopt_inputs)
    adopted = next(row for row in adopt_snapshot["candidates"] if row["op"] == "ADOPT")
    assert adopted["signal"]["member_ids"] == [20, 21]
    assert adopted["signal"]["apply_request"]["node_ids"] == [20, 21]
    assert adopt_snapshot["adopt_groups"][0]["member_ids"] == [20, 21]


def test_shared_target_is_valid_tier1_alternative_but_tree_alone_is_not():
    group = _open_group()
    group["target_member_ids"] = {"99": [10, 11]}
    inputs = _base_inputs()
    inputs["orphan_count"] = 4
    inputs["orphan_groups"] = [group]
    snapshot = detector.derive_shadow_snapshot(inputs)
    opened = next(row for row in snapshot["candidates"] if row["op"] == "OPEN")
    assert opened["signal"]["contact_session_count"] == 0
    assert opened["signal"]["tier1_sources"] == [
        "shared_feeder_or_decision_target"
    ]


def test_recent_active_lane_cap_emits_blocked_open_with_cap_pressure():
    inputs = _contacted_open_inputs(_open_group())
    inputs["recent_active_lane_count"] = 12
    inputs["recent_active_lane_cap"] = 12
    snapshot = detector.derive_shadow_snapshot(inputs)
    blocked = next(row for row in snapshot["candidates"] if row["op"] == "OPEN")
    assert blocked["signal"]["reason"] == "cap_pressure"
    assert blocked["signal"]["cap_pressure"] is True
    assert blocked["signal"]["qualified"] is False


def test_lane_cap_does_not_block_lane_reducing_merge_or_close():
    merge_inputs = _base_inputs()
    merge_inputs["recent_active_lane_count"] = 12
    merge_inputs["recent_active_lane_cap"] = 12
    merge_inputs["lanes"] = {
        1: _lane(1, active_member_count=1),
        2: _lane(2, active_member_count=3),
    }
    merge_inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4"],
    }
    merge_inputs["tier2"] = {
        "1:2": {"shared_target_ids": [70, 71], "cross_path_sessions": []},
    }
    merged = next(
        row for row in detector.derive_shadow_snapshot(merge_inputs)["candidates"]
        if row["op"] == "MERGE"
    )
    assert "cap_pressure" not in merged["signal"]

    close_inputs = _base_inputs()
    close_inputs["recent_active_lane_count"] = 12
    close_inputs["recent_active_lane_cap"] = 12
    close_inputs["lanes"] = {
        1: _lane(
            1,
            done_when="Release is shipped",
            forward_member_ids=[10],
            resolved_forward_member_ids=[10],
            resolution_edge_ids=[501],
            resolution_evidence=[{
                "edge_id": 501,
                "forward_member_id": 10,
                "created_by": "alice",
            }],
            resolution_density=1.0,
        ),
    }
    closed = next(
        row for row in detector.derive_shadow_snapshot(close_inputs)["candidates"]
        if row["op"] == "CLOSE"
    )
    assert closed["signal"]["qualified"] is True
    assert "cap_pressure" not in closed["signal"]


def test_mixed_tree_handoff_requires_contacts_and_shared_target_for_adopt():
    group = _open_group(member_ids=(10, 11), kinds=["idea", "progress"])
    group["lane_member_ids"] = {"1": [101]}
    group["adopt_shared_targets"] = {"1": [99]}
    group["target_member_ids"] = {"99": [10, 101]}
    group["member_embeddings"] = {
        "10": [1.0, 0.0], "11": [0.99, 0.01],
    }
    inputs = _base_inputs()
    inputs["lanes"] = {1: _lane(1)}
    inputs["orphan_groups"] = [group]
    inputs["orphan_count"] = 2

    tree_only = detector.derive_shadow_snapshot(inputs)
    assert tree_only["adopt_groups"][0]["reason"] == "tree_only_or_partial_contact"
    assert not any(
        row["op"] in {"ADOPT", "OPEN"} for row in tree_only["candidates"]
    )

    inputs["orphan_events"] = {
        10: [{"session_id": "S1", "ts": "2026-07-19 00:00:00"}],
        11: [{"session_id": "S2", "ts": "2026-07-20 00:00:00"}],
    }
    snapshot = detector.derive_shadow_snapshot(inputs)
    adopted = next(row for row in snapshot["candidates"] if row["op"] == "ADOPT")
    assert not any(row["op"] == "OPEN" for row in snapshot["candidates"])
    assert adopted["signal"]["workstream_id"] == 1
    assert adopted["signal"]["member_ids"] == [10, 11]
    assert adopted["signal"]["tier1_session_ids"] == ["S1", "S2"]
    assert adopted["signal"]["tier2_inputs"] == ["shared_target"]
    assert adopted["signal"]["apply_request"] == {
        "workstream_id": 1,
        "node_ids": [10, 11],
        "relations": {"10": "advances", "11": "advances"},
        "evidence": adopted["signal"]["apply_request"]["evidence"],
        "allow_auto_apply": True,
    }
    assert adopted["signal"]["apply_request"]["evidence"]["forward_looking"] \
        is True
    assert adopted["signal"]["candidate_payload_binding"] \
        == detector.lifecycle_signals.make_candidate_payload_binding(
            "ADOPT", adopted["signal"],
        )
    assert adopted["candidate_key"] == detector.lifecycle_signals.candidate_evidence_key(
        adopted["signal"]["base_candidate_key"],
        adopted["signal"]["candidate_payload_binding"],
    )


def test_adopt_aggregates_qualified_groups_per_lane_and_pinned_is_candidate_only():
    first = _open_group(member_ids=(10,), kinds=["idea"])
    second = _open_group(member_ids=(20,), kinds=["progress"])
    second["tree_id"] = 91
    for group, target, lane_member in ((first, 99, 101), (second, 98, 102)):
        group["lane_member_ids"] = {"1": [lane_member]}
        group["adopt_shared_targets"] = {"1": [target]}
        group["target_member_ids"] = {str(target): [group["member_ids"][0], lane_member]}
    first["member_feeder_relations"] = {"1": {"10": "motivates"}}
    inputs = _base_inputs()
    inputs["lanes"] = {1: _lane(1)}
    inputs["orphan_groups"] = [first, second]
    inputs["orphan_events"] = {
        10: [{"session_id": "S1"}, {"session_id": "S2"}],
        20: [{"session_id": "S2"}, {"session_id": "S3"}],
    }
    adopted = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "ADOPT"
    )
    assert adopted["signal"]["member_ids"] == [10, 20]
    assert adopted["signal"]["member_feeder_relations"] == {
        "10": "motivates", "20": "advances",
    }
    assert adopted["signal"]["tree_ids"] == [90, 91]
    assert len(adopted["signal"]["bounded_evidence"]) == 2

    inputs["lanes"][1]["pinned"] = True
    pinned = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "ADOPT"
    )
    assert pinned["signal"]["candidate_only"] is True
    assert pinned["signal"]["ambiguity_reason"] == "pinned_target"
    assert "apply_request" not in pinned["signal"]


def test_adopt_keeps_full_apply_structure_when_signal_evidence_is_bounded():
    groups = []
    orphan_events = {}
    for index in range(detector.WEAK_ANNOTATION_ID_LIMIT + 1):
        member = 1000 + index
        lane_member = 2000 + index
        target = 3000 + index
        group = _open_group(member_ids=(member,), kinds=["progress"])
        group["tree_id"] = 100 + index
        group["lane_member_ids"] = {"1": [lane_member]}
        group["adopt_shared_targets"] = {"1": [target]}
        group["target_member_ids"] = {str(target): [member, lane_member]}
        groups.append(group)
        orphan_events[member] = [
            {"session_id": "S1"},
            {"session_id": "S2"},
        ]

    inputs = _base_inputs()
    inputs["lanes"] = {1: _lane(1)}
    inputs["orphan_groups"] = groups
    inputs["orphan_events"] = orphan_events
    adopted = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "ADOPT"
    )

    bounded = adopted["signal"]["bounded_evidence"]
    structural = adopted["signal"]["apply_request"]["evidence"][
        "structural_evidence"
    ]
    assert len(bounded) == detector.WEAK_ANNOTATION_ID_LIMIT
    assert len(structural) == detector.WEAK_ANNOTATION_ID_LIMIT + 1
    assert {
        node_id for group in structural for node_id in group["member_ids"]
    } == set(adopted["signal"]["member_ids"])
    assert {
        target_id
        for group in structural
        for target_id in group["shared_target_ids"]
    } == set(adopted["signal"]["shared_target_ids"])


def test_close_is_conservative_about_observation_focus_feeders_and_priorities():
    inputs = _base_inputs()
    inputs["lanes"] = {
        1: _lane(1),
        2: _lane(2, pinned=True),
        3: _lane(3, open_feeder_count=1),
        4: _lane(4, active_priority_count=1),
        5: _lane(5, created_at="2026-07-20 00:00:00"),
    }
    snapshot = detector.derive_shadow_snapshot(inputs)
    closes = [row for row in snapshot["candidates"] if row["op"] == "CLOSE"]
    assert [row["signal"]["workstream_id"] for row in closes] == [1]
    assert closes[0]["signal"]["auto_apply_eligible"] is False
    assert "apply_request" not in closes[0]["signal"]

    inputs["observation_start"] = "2026-07-20 00:00:00"
    assert not any(
        row["op"] == "CLOSE"
        for row in detector.derive_shadow_snapshot(inputs)["candidates"]
    )


def test_completed_close_requires_dense_audited_resolution_and_attestation():
    inputs = _base_inputs()
    inputs["lanes"] = {
        1: _lane(
            1,
            done_when="Release is shipped",
            forward_member_ids=[10, 11],
            resolved_forward_member_ids=[10, 11],
            resolution_edge_ids=[501, 502],
            resolution_evidence=[
                {"edge_id": 501, "forward_member_id": 10, "created_by": "alice"},
                {
                    "edge_id": 502,
                    "forward_member_id": 11,
                    "created_by": "lifecycle:close",
                },
            ],
            resolution_density=1.0,
        )
    }
    completed = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "CLOSE"
    )
    assert completed["signal"]["outcome"] == "completed"
    assert completed["signal"]["attestation_required"] is True
    assert completed["signal"]["completion_evidence"]["resolution_density"] == 1.0
    assert completed["signal"]["apply_request"]["dispositions"] == {}

    inputs["lanes"][1]["resolution_evidence"] = []
    unaudited = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "CLOSE"
    )
    assert unaudited["signal"]["outcome"] == "abandoned"
    assert unaudited["signal"]["candidate_only"] is True
    assert "apply_request" not in unaudited["signal"]


def test_auto_open_probation_rolls_back_only_after_target_with_full_releases():
    inputs = _base_inputs()
    inputs["observation_start"] = None
    inputs["lanes"] = {
        1: _lane(1, member_ids=[10, 11], open_feeder_ids=[12]),
        2: _lane(2),
    }
    inputs["probation_lanes"] = {
        1: {
            "active": True,
            "open_op_key": "auto-open:1",
            "opened_at": "2026-07-01 00:00:00",
            "eligible_session_target": 10,
            "eligible_session_count": 9,
            "contact_session_count": 0,
            "member_ids": [10, 11],
            "open_feeder_ids": [12],
        }
    }
    before = detector.derive_shadow_snapshot(inputs)
    closes = [row for row in before["candidates"] if row["op"] == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["signal"]["opening_op_key"] == "auto-open:1"
    assert closes[0]["signal"]["candidate_only"] is True
    assert "apply_request" not in closes[0]["signal"]
    assert not any(
        row["signal"]["workstream_id"] == 2 for row in closes
    ), "human/non-auto lanes must not enter probation rollback"

    inputs["probation_lanes"][1]["eligible_session_count"] = 10
    ready = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "CLOSE"
    )
    assert ready["signal"]["qualified"] is True
    assert ready["signal"]["apply_request"]["dispositions"] == {
        "10": {"action": "release"},
        "11": {"action": "release"},
        "12": {"action": "release"},
    }

    inputs["probation_lanes"][1]["contact_session_count"] = 1
    contacted = detector.derive_shadow_snapshot(inputs)
    assert not any(row["op"] == "CLOSE" for row in contacted["candidates"])
    assert contacted["probation_diagnostics"] == []
    inputs["observation_start"] = "2026-06-01 00:00:00"
    ordinary = next(
        row for row in detector.derive_shadow_snapshot(inputs)["candidates"]
        if row["op"] == "CLOSE"
    )
    assert ordinary["signal"]["reason"] == "no_contamination_free_contact"

    inputs["observation_start"] = None
    inputs["probation_lanes"][1]["contact_session_count"] = 0
    inputs["probation_lanes"][1]["opened_at"] = "2026-03-01 00:00:00"
    expired = detector.derive_shadow_snapshot(inputs)
    assert not any(row["op"] == "CLOSE" for row in expired["candidates"])
    assert expired["probation_diagnostics"][0]["reason"] \
        == "probation_evidence_expired"
    assert expired["probation_diagnostics"][0]["window_within_retention"] is False

    inputs["probation_lanes"][1]["opened_at"] = "2026-07-01 00:00:00"
    inputs["probation_lanes"][1]["active"] = False
    inactive = detector.derive_shadow_snapshot(inputs)
    assert not any(row["op"] == "CLOSE" for row in inactive["candidates"])
    assert inactive["probation_diagnostics"] == []


def _connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    path = tmp_path / "kb.db"
    monkeypatch.setattr(db, "db_path", lambda _cwd=None: path)
    monkeypatch.setattr(
        db,
        "ensure_project_dir",
        lambda _cwd=None: path.parent.mkdir(parents=True, exist_ok=True),
    )
    return db.connect(str(tmp_path))


def test_collect_inputs_probation_reads_only_applied_automatic_open(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    automatic = db.insert_node(
        conn, kind="workstream", title="Automatic", body="body", status="staging",
    )
    human = db.insert_node(
        conn, kind="workstream", title="Human", body="body", status="staging",
    )
    payload = {
        "assigned_member_ids": [],
        "watch_pair": None,
        "probation": {
            "active": True,
            "opened_at": "2026-07-01 00:00:00",
            "eligible_session_target": 13,
        },
    }
    for op_key, lane_id, origin in (
        ("open:auto", automatic, "auto"),
        ("open:human", human, "manual"),
    ):
        db.begin_workstream_op(
            conn,
            op_key=op_key,
            op="OPEN",
            origin=origin,
            dst_workstream_id=lane_id,
            payload=payload,
        )
        db.finish_workstream_op(conn, op_key, state="applied")
    inputs = detector.collect_inputs(conn, now=NOW)
    assert set(inputs["probation_lanes"]) == {automatic}
    assert inputs["probation_lanes"][automatic]["eligible_session_target"] == 13
    conn.close()


def test_collect_inputs_graduates_contacted_probation_at_target(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    graduated = db.insert_node(
        conn, kind="workstream", title="Graduated", body="body", status="staging",
    )
    zero_contact = db.insert_node(
        conn, kind="workstream", title="Zero contact", body="body", status="staging",
    )
    inactive = db.insert_node(
        conn, kind="workstream", title="Inactive receipt", body="body", status="staging",
    )
    for op_key, lane_id, active in (
        ("open:graduated", graduated, True),
        ("open:zero", zero_contact, True),
        ("open:inactive", inactive, False),
    ):
        db.begin_workstream_op(
            conn,
            op_key=op_key,
            op="OPEN",
            origin="auto",
            dst_workstream_id=lane_id,
            payload={
                "assigned_member_ids": [],
                "watch_pair": None,
                "probation": {
                    "active": active,
                    "opened_at": "2026-07-01 00:00:00",
                    "eligible_session_target": 2,
                },
            },
        )
        db.finish_workstream_op(conn, op_key, state="applied")
    for index, day in ((1, 10), (2, 11)):
        session_id = f"S{index}"
        conn.execute(
            "INSERT INTO sessions(id,project_path,started_at) VALUES(?,?,?)",
            (session_id, "/p", f"2026-07-{day:02d} 00:00:00"),
        )
        conn.commit()
        db.record_retrieval_events(
            conn,
            source="prompt",
            session_id=session_id,
            turn=1,
            items=[(graduated, 0.9)],
            ts=f"2026-07-{day:02d} 01:00:00",
        )

    inputs = detector.collect_inputs(conn, now=NOW)
    assert inputs["contacts"][graduated] == ["S1", "S2"]
    assert set(inputs["probation_lanes"]) == {zero_contact}
    assert inputs["probation_lanes"][zero_contact]["active"] is True
    assert inputs["probation_lanes"][zero_contact]["eligible_session_count"] == 2
    assert inputs["probation_lanes"][zero_contact]["contact_session_count"] == 0
    assert inputs["probation_lanes"][zero_contact]["target_sessions_observable"] is True
    conn.close()


def test_persisted_shadow_snapshot_changes_only_derivation_ledger(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    inputs = _base_inputs()
    inputs["lanes"] = {1: _lane(1), 2: _lane(2)}
    inputs["contacts"] = {
        1: ["S1", "S2", "S3", "S4"],
        2: ["S1", "S2", "S3", "S4"],
    }
    inputs["tier2"] = {
        "1:2": {"shared_target_ids": [70, 71], "cross_path_sessions": []}
    }
    snapshot = detector.derive_shadow_snapshot(inputs)
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("nodes", "edges", "focus")
    }
    receipt = detector.persist_shadow_snapshot(conn, snapshot)
    assert receipt["created"] is True
    assert detector.persist_shadow_snapshot(conn, snapshot)["created"] is False
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("nodes", "edges", "focus")
    }
    assert after == before
    assert db.latest_workstream_derivation(conn)["substrate_version"] \
        == detector.SUBSTRATE_VERSION
    conn.close()


def test_prequential_helpers_require_persistence_and_zero_disagreement():
    key = "candidate"
    state = detector.prequential_state(
        key, [{key}, {key}, {key}], verdicts=["agree", "agree"],
    )
    assert state["persistence"] == 1.0
    assert state["consecutive_present"] == 3
    assert detector.graduation_eligible(state)
    contradicted = detector.prequential_state(
        key, [{key}, {key}], verdicts=["disagree"],
    )
    assert not detector.graduation_eligible(contradicted)


def test_prequential_state_resets_at_substrate_version_boundary(tmp_path, monkeypatch):
    conn = _connect(tmp_path, monkeypatch)
    key = detector.lifecycle_signals.make_candidate_key("MERGE", [1, 2])
    db.record_workstream_derivation(
        conn,
        derivation_key="old",
        substrate_version="old-substrate",
        candidates=[{"candidate_key": key, "op": "MERGE", "signal": {}}],
    )
    db.append_workstream_op_event(
        conn,
        event_key="old-disagree",
        candidate_key=key,
        event_type="attestation",
        verdict="disagree",
        session_id="old-session",
        derivation_key="old",
        require_latest_candidate=True,
    )
    for index in (1, 2):
        db.record_workstream_derivation(
            conn,
            derivation_key=f"new-{index}",
            substrate_version=detector.SUBSTRATE_VERSION,
            candidates=[{"candidate_key": key, "op": "MERGE", "signal": {}}],
        )
    state = detector.load_prequential_state(conn, key)
    assert state["windows"] == 2
    assert state["consecutive_present"] == 2
    assert state["disagree"] == 0
    assert state["substrate_version"] == detector.SUBSTRATE_VERSION
    conn.close()


def test_collect_inputs_uses_event_time_lanes_and_current_tree_structure(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    lane = db.insert_node(
        conn, kind="workstream", title="Lane", body="Done when: shipped", status="canonical",
    )
    summary = db.insert_node(
        conn, kind="summary", title="Cluster", body="summary", status="canonical",
    )
    member_ids = [
        db.insert_node(conn, kind=kind, title=f"N{i}", body="body", status="canonical")
        for i, kind in enumerate(("idea", "fact", "progress", "decision"))
    ]
    conn.execute(
        f"UPDATE nodes SET parent_id=?, updated_at=? WHERE id IN "
        f"({','.join('?' for _ in member_ids)})",
        [summary, NOW, *member_ids],
    )
    conn.execute("UPDATE nodes SET updated_at=? WHERE id=?", (NOW, summary))
    conn.execute(
        "INSERT INTO sessions(id,project_path,started_at) VALUES('S1','/p','2026-07-20 00:00:00')"
    )
    conn.commit()
    db.record_retrieval_events(
        conn,
        source="prompt",
        session_id="S1",
        turn=2,
        items=[(lane, 0.9), (member_ids[0], 0.8)],
        ts="2026-07-20 01:00:00",
    )
    inputs = detector.collect_inputs(conn, now=NOW)
    assert inputs["contacts"][lane] == ["S1"]
    assert inputs["orphan_events"][member_ids[0]][0]["session_id"] == "S1"
    assert inputs["orphan_groups"][0]["member_ids"] == member_ids
    conn.close()


def test_collect_inputs_excludes_priority_nodes_from_tree_and_orphan_counts(
    tmp_path, monkeypatch,
):
    conn = _connect(tmp_path, monkeypatch)
    summary = db.insert_node(
        conn, kind="summary", title="Cluster", body="summary", status="canonical",
    )
    idea = db.insert_node(
        conn, kind="idea", title="Candidate member", body="body", status="canonical",
    )
    priority = db.insert_node(
        conn, kind="priority", title="Standing policy", body="body", status="canonical",
    )
    conn.execute(
        "UPDATE nodes SET parent_id=?, updated_at=? WHERE id IN (?,?)",
        (summary, NOW, idea, priority),
    )
    conn.execute("UPDATE nodes SET updated_at=? WHERE id=?", (NOW, summary))
    conn.commit()

    inputs = detector.collect_inputs(conn, now=NOW)

    assert inputs["orphan_count"] == 1
    assert inputs["orphan_groups"][0]["member_ids"] == [idea]
    assert priority not in inputs["orphan_groups"][0]["member_ids"]
    conn.close()
