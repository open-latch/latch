"""Acceptance regressions for unattended heal's ratified-node immunity."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latch.store import db  # noqa: E402
from latch.gate import predicate_policy  # noqa: E402
from latch.pipeline import drift  # noqa: E402
from latch.pipeline import heal  # noqa: E402


def _embedding() -> bytes:
    return np.ones(db.VEC_DIM, dtype=np.float32).tobytes()


def _days_ago(days: int) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).strftime("%Y-%m-%d %H:%M:%S")


def _decision(conn, title: str) -> int:
    return db.insert_node(
        conn,
        kind="decision",
        title=title,
        body=f"Contradictory ruling fixture: {title}.",
        embedding=_embedding(),
    )


def _ratify(conn, node_id: int) -> None:
    db.insert_ratification_nc(
        conn,
        node_id,
        ratifier="session:founder",
        action="ratify",
        source="latch_update",
    )
    db.update_node(conn, node_id, status="canonical")


def _set_resolution_inputs(
    conn,
    node_id: int,
    *,
    updated_at: str,
    ref_count: int = 0,
) -> None:
    conn.execute(
        "UPDATE nodes SET updated_at = ?, ref_count = ? WHERE id = ?",
        (updated_at, ref_count, node_id),
    )
    conn.commit()


def _force_pair(
    monkeypatch: pytest.MonkeyPatch,
    left_id: int,
    right_id: int,
    *,
    similarity: float = 0.99,
) -> None:
    def fake_find(conn, _vec, *, exclude_id=None, **_kwargs):
        if exclude_id == left_id:
            other_id = right_id
        elif exclude_id == right_id:
            other_id = left_id
        else:
            return []
        return [{**db.get_node(conn, other_id), "similarity": similarity}]

    monkeypatch.setattr(heal, "find_near_duplicates", fake_find)


def _quiet_nightly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heal.paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(heal.paths, "is_disabled", lambda: False)
    monkeypatch.setattr(heal.log_utils, "maintain_log_retention", lambda _path: {})
    monkeypatch.setattr(heal.correlator, "correlate", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(drift, "sweep", lambda *_args, **_kwargs: {})


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    def record(event_type: str, row: dict, **_kwargs) -> None:
        events.append((event_type, row))

    monkeypatch.setattr(heal.log_utils, "emit_event", record)
    return events


def _referral_ids(conn, left_id: int, right_id: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT q.id
        FROM nodes q
        JOIN edges left_edge
          ON left_edge.src = q.id
         AND left_edge.dst = ?
         AND left_edge.relation = 'related_to'
         AND left_edge.status = 'active'
        JOIN edges right_edge
          ON right_edge.src = q.id
         AND right_edge.dst = ?
         AND right_edge.relation = 'related_to'
         AND right_edge.status = 'active'
        WHERE q.kind = 'open_question'
          AND q.status = 'staging'
        ORDER BY q.id
        """,
        (left_id, right_id),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _supersedes_edges(conn, left_id: int, right_id: int) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT src, dst FROM edges "
        "WHERE relation = 'supersedes' AND status = 'active' "
        "AND ((src = ? AND dst = ?) OR (src = ? AND dst = ?))",
        (left_id, right_id, right_id, left_id),
    ).fetchall()
    return [(int(row["src"]), int(row["dst"])) for row in rows]


def _referral_events(
    events: list[tuple[str, dict]], left_id: int, right_id: int
) -> list[dict]:
    pair = sorted((left_id, right_id))
    return [
        row
        for event_type, row in events
        if event_type == "heal_human_referral"
        and [row.get("node_a_id"), row.get("node_b_id")] == pair
    ]


def _reconciled_by_edges(
    conn, left_id: int, right_id: int
) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT src, dst FROM edges "
        "WHERE relation = 'reconciled_by' AND status = 'active' "
        "AND ((src = ? AND dst = ?) OR (src = ? AND dst = ?))",
        (left_id, right_id, right_id, left_id),
    ).fetchall()
    return [(int(row["src"]), int(row["dst"])) for row in rows]


def _grant_llm_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        heal.budget,
        "reserve_available",
        lambda *_args, requested, **_kwargs: (
            requested,
            {
                "date": heal.budget.current_date(),
                "approved_dates": [],
            },
        ),
    )


def _stub_nightly_decision(
    monkeypatch: pytest.MonkeyPatch, decision: str
) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    def arbitrate(older, newer, _similarity, **_kwargs):
        calls.append((int(older["id"]), int(newer["id"])))
        return {"decision": decision, "reason": "fixture verdict"}

    monkeypatch.setattr(heal, "_arbitrate_nightly", arbitrate)
    return calls


_TIER_SIMILARITY = {"high": 0.99, "low": 0.60}


def test_ratified_referral_failure_leaves_pair_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified = _decision(conn, "Founder-ratified atomic referral target")
        challenger = _decision(conn, "Contradiction awaiting atomic referral")
        _ratify(conn, ratified)
        events = _capture_events(monkeypatch)

        original_add_edge_nc = db.add_edge_nc
        calls = 0

        def fail_during_referral(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("injected referral-link failure")
            return original_add_edge_nc(*args, **kwargs)

        monkeypatch.setattr(heal.db, "add_edge_nc", fail_during_referral)
        with pytest.raises(
            sqlite3.OperationalError, match="injected referral-link failure"
        ):
            heal._route_ratified_pair_to_human(
                conn,
                ratified,
                challenger,
                trigger="recency",
                project_path=str(tmp_path),
            )

        assert not heal.edge_exists_between(conn, ratified, challenger)
        assert _referral_ids(conn, ratified, challenger) == []
        assert _referral_events(events, ratified, challenger) == []

        monkeypatch.setattr(heal.db, "add_edge_nc", original_add_edge_nc)
        referral_id, created = heal._route_ratified_pair_to_human(
            conn,
            ratified,
            challenger,
            trigger="recency",
            project_path=str(tmp_path),
        )

        assert created is True
        assert _referral_ids(conn, ratified, challenger) == [referral_id]
        assert heal.edge_exists_between(conn, ratified, challenger)
        assert len(_referral_events(events, ratified, challenger)) == 1
    finally:
        conn.close()


def test_ratified_loser_never_picked_by_recency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified = _decision(conn, "Founder-ratified older ruling")
        challenger = _decision(conn, "Fresh contradictory note")
        _ratify(conn, ratified)
        _set_resolution_inputs(conn, ratified, updated_at=_days_ago(90))
        _set_resolution_inputs(conn, challenger, updated_at=_days_ago(1))
        _force_pair(monkeypatch, ratified, challenger)
        _quiet_nightly(monkeypatch)

        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=False,
            integrity=False,
        )

        assert db.get_node(conn, ratified)["status"] == "canonical"
        assert db.get_node(conn, challenger)["status"] == "staging"
        assert _supersedes_edges(conn, ratified, challenger) == []
        assert len(_referral_ids(conn, ratified, challenger)) == 1
    finally:
        conn.close()


def test_ratified_loser_never_picked_by_ref_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified = _decision(conn, "Founder-ratified lower-ref ruling")
        challenger = _decision(conn, "Higher-ref contradictory note")
        _ratify(conn, ratified)
        shared_timestamp = _days_ago(5)
        _set_resolution_inputs(
            conn,
            ratified,
            updated_at=shared_timestamp,
            ref_count=heal.REF_COUNT_MIN_BOTH,
        )
        _set_resolution_inputs(
            conn,
            challenger,
            updated_at=shared_timestamp,
            ref_count=int(
                heal.REF_COUNT_MIN_BOTH * heal.REF_COUNT_RATIO_THRESHOLD
            ),
        )
        assert db.get_node(conn, ratified)["ref_count"] >= 1
        assert (
            db.get_node(conn, challenger)["ref_count"]
            / db.get_node(conn, ratified)["ref_count"]
        ) >= 3
        _force_pair(monkeypatch, ratified, challenger)
        _quiet_nightly(monkeypatch)

        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=False,
            integrity=False,
        )

        assert db.get_node(conn, ratified)["status"] == "canonical"
        assert db.get_node(conn, challenger)["status"] == "staging"
        assert _supersedes_edges(conn, ratified, challenger) == []
        assert len(_referral_ids(conn, ratified, challenger)) == 1
    finally:
        conn.close()


def test_ratified_winner_also_suppressed_routes_to_referral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        challenger = _decision(conn, "Older contradictory note")
        ratified = _decision(conn, "Fresh founder-ratified ruling")
        _ratify(conn, ratified)
        _set_resolution_inputs(conn, challenger, updated_at=_days_ago(90))
        _set_resolution_inputs(conn, ratified, updated_at=_days_ago(1))
        _force_pair(monkeypatch, ratified, challenger)
        _quiet_nightly(monkeypatch)
        events = _capture_events(monkeypatch)

        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=False,
            integrity=False,
        )

        assert db.get_node(conn, ratified)["status"] == "canonical"
        assert db.get_node(conn, challenger)["status"] == "staging"
        assert _supersedes_edges(conn, ratified, challenger) == []
        assert len(_referral_ids(conn, ratified, challenger)) == 1
        referral_events = _referral_events(events, ratified, challenger)
        assert len(referral_events) == 1
        assert referral_events[0]["trigger"] == "recency"
    finally:
        conn.close()


def test_llm_supersede_against_ratified_routes_to_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified = _decision(conn, "Founder-ratified LLM target")
        challenger = _decision(conn, "LLM-selected replacement")
        _ratify(conn, ratified)
        shared_timestamp = _days_ago(5)
        _set_resolution_inputs(conn, ratified, updated_at=shared_timestamp)
        _set_resolution_inputs(conn, challenger, updated_at=shared_timestamp)
        _force_pair(monkeypatch, ratified, challenger)
        _quiet_nightly(monkeypatch)
        monkeypatch.setattr(
            heal.budget,
            "reserve_available",
            lambda *_args, requested, **_kwargs: (
                requested,
                {
                    "date": heal.budget.current_date(),
                    "approved_dates": [],
                },
            ),
        )
        monkeypatch.setattr(
            heal,
            "_arbitrate_nightly",
            lambda *_args, **_kwargs: {
                "decision": "supersede_b",
                "reason": "newer contradicts older",
            },
        )
        events = _capture_events(monkeypatch)

        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )
        referrals_after_first = _referral_ids(conn, ratified, challenger)
        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )

        assert db.get_node(conn, ratified)["status"] == "canonical"
        assert db.get_node(conn, challenger)["status"] == "staging"
        assert _supersedes_edges(conn, ratified, challenger) == []
        assert len(referrals_after_first) == 1
        assert _referral_ids(conn, ratified, challenger) == referrals_after_first
        referral_events = _referral_events(events, ratified, challenger)
        assert len(referral_events) == 1
        assert referral_events[0]["trigger"] == "llm"
    finally:
        conn.close()


def test_insert_with_heal_refers_ratified_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified = _decision(conn, "Founder-ratified insert match")
        _ratify(conn, ratified)
        monkeypatch.setattr(
            heal.embeddings,
            "embed",
            lambda _text: np.ones(db.VEC_DIM, dtype=np.float32),
        )

        def matched_candidate(candidate_conn, _vec, **_kwargs):
            return [
                {**db.get_node(candidate_conn, ratified), "similarity": 0.99}
            ]

        monkeypatch.setattr(heal, "find_near_duplicates", matched_candidate)
        monkeypatch.setattr(
            heal,
            "arbitrate",
            lambda *_args, **_kwargs: {
                "decision": "supersede",
                "reason": "new insert replaces match",
            },
        )
        _quiet_nightly(monkeypatch)
        events = _capture_events(monkeypatch)

        result = heal.insert_with_heal(
            conn,
            kind="decision",
            title="Inserted contradictory decision",
            body="A new unattended decision contradicts the ratified match.",
            project_path=None,
            use_llm=True,
        )
        inserted = result["id"]
        referrals_after_insert = _referral_ids(conn, ratified, inserted)

        _force_pair(monkeypatch, ratified, inserted)
        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )

        assert result["heal"] == "keep_both"
        assert db.get_node(conn, ratified)["status"] == "canonical"
        assert db.get_node(conn, inserted)["status"] == "staging"
        assert _supersedes_edges(conn, ratified, inserted) == []
        assert len(referrals_after_insert) == 1
        assert _referral_ids(conn, ratified, inserted) == referrals_after_insert
        referral_events = _referral_events(events, ratified, inserted)
        assert len(referral_events) == 1
        assert referral_events[0]["trigger"] == "llm"
    finally:
        conn.close()


def test_g1_criterion4_ratified_survives_100_of_100(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified = _decision(conn, "G1 founder-ratified ruling")
        challenger = _decision(conn, "G1 newer contradiction")
        _ratify(conn, ratified)
        _set_resolution_inputs(conn, ratified, updated_at=_days_ago(90))
        _set_resolution_inputs(conn, challenger, updated_at=_days_ago(1))
        _force_pair(monkeypatch, ratified, challenger)
        _quiet_nightly(monkeypatch)
        events = _capture_events(monkeypatch)

        canonical_finishes = 0
        for _ in range(100):
            heal.nightly_heal(
                conn,
                project_path=str(tmp_path),
                use_llm=False,
                integrity=False,
            )
            canonical_finishes += (
                db.get_node(conn, ratified)["status"] == "canonical"
            )

        assert canonical_finishes == 100
        assert _supersedes_edges(conn, ratified, challenger) == []
        assert len(_referral_ids(conn, ratified, challenger)) == 1
        referral_events = _referral_events(events, ratified, challenger)
        assert len(referral_events) == 1
        assert referral_events[0]["trigger"] == "recency"
        assert not any(
            event_type == "reconciliation"
            and row.get("relation") == "supersedes"
            and ratified in {row.get("src_id"), row.get("dst_id")}
            for event_type, row in events
        )
    finally:
        conn.close()


def _reconcile_fixture(
    conn,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tier: str,
    ratified_side: str | None,
) -> tuple[int, int, list[tuple[int, int]], list[tuple[str, dict]]]:
    """Two judgments whose only live verdict is the LLM's reconciled_by.

    Ages differ by less than the recency window and neither node is
    referenced, so the deterministic passes stay silent on both tiers and the
    pair reaches the nightly LLM arbitrator. Returns (older, newer, llm_calls,
    events); `ratified_side` names which of the two is founder-ratified.
    """
    older = _decision(conn, "Older framing of the retention ruling")
    newer = _decision(conn, "Newer framing that narrows the ruling")
    if ratified_side == "older":
        _ratify(conn, older)
    elif ratified_side == "newer":
        _ratify(conn, newer)
    _set_resolution_inputs(conn, older, updated_at=_days_ago(10))
    _set_resolution_inputs(conn, newer, updated_at=_days_ago(5))
    _force_pair(monkeypatch, older, newer, similarity=_TIER_SIMILARITY[tier])
    _quiet_nightly(monkeypatch)
    _grant_llm_budget(monkeypatch)
    llm_calls = _stub_nightly_decision(monkeypatch, "reconciled_by")
    events = _capture_events(monkeypatch)
    return older, newer, llm_calls, events


@pytest.mark.parametrize("ratified_side", ["older", "newer"])
@pytest.mark.parametrize("tier", ["high", "low"])
def test_llm_reconciled_by_against_ratified_routes_to_human(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: str,
    ratified_side: str,
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        older, newer, llm_calls, events = _reconcile_fixture(
            conn, monkeypatch, tier=tier, ratified_side=ratified_side,
        )
        ratified = older if ratified_side == "older" else newer

        first = heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )
        referrals_after_first = _referral_ids(conn, older, newer)
        second = heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )

        assert first["by_tier"][tier] == 1
        assert llm_calls == [(older, newer)]
        assert _reconciled_by_edges(conn, older, newer) == []
        assert first["reconciled"] == 0
        assert first["human_referrals"] == 1
        assert first["kept_both"] == 1
        assert len(referrals_after_first) == 1
        referral = db.get_node(conn, referrals_after_first[0])
        assert referral["title"].startswith("Human review required")
        assert _referral_ids(conn, older, newer) == referrals_after_first
        assert second["human_referrals"] == 0
        assert second["reconciled"] == 0
        assert second["skipped_edge_exists"] == 1
        assert db.get_node(conn, ratified)["status"] == "canonical"
        assert db.reconciliation_banner(conn, older) == []
        assert db.reconciliation_banner(conn, newer) == []
        referral_events = _referral_events(events, older, newer)
        assert len(referral_events) == 1
        assert referral_events[0]["trigger"] == "llm_reconciled_by"
        assert referral_events[0]["ratified_node_ids"] == [ratified]
        assert not any(
            event_type == "reconciliation"
            and row.get("relation") == "reconciled_by"
            for event_type, row in events
        )
    finally:
        conn.close()


@pytest.mark.parametrize("tier", ["high", "low"])
def test_llm_reconciled_by_on_unratified_pair_still_writes_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tier: str
) -> None:
    conn = db.connect(str(tmp_path / "vault"))
    try:
        older, newer, llm_calls, events = _reconcile_fixture(
            conn, monkeypatch, tier=tier, ratified_side=None,
        )

        result = heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )

        assert result["by_tier"][tier] == 1
        assert llm_calls == [(older, newer)]
        assert _reconciled_by_edges(conn, older, newer) == [(older, newer)]
        assert result["reconciled"] == 1
        assert result["human_referrals"] == 0
        assert result["kept_both"] == 0
        assert [
            banner["linked_id"]
            for banner in db.reconciliation_banner(conn, older)
        ] == [newer]
        assert db.get_node(conn, older)["status"] == "staging"
        assert db.get_node(conn, newer)["status"] == "staging"
        assert conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind = 'open_question'"
        ).fetchone()[0] == 0
        assert _referral_events(events, older, newer) == []
        assert [
            (row.get("src_id"), row.get("dst_id"))
            for event_type, row in events
            if event_type == "reconciliation"
            and row.get("relation") == "reconciled_by"
        ] == [(older, newer)]
    finally:
        conn.close()


def test_ratified_reconcile_referral_keeps_banner_empty_and_rows_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain = "domain:synthetic-heal-guard"
    conn = db.connect(str(tmp_path / "vault"))
    try:
        ratified, challenger, _llm_calls, _events = _reconcile_fixture(
            conn, monkeypatch, tier="low", ratified_side="older",
        )
        rejected_path_id = db.insert_rejected_path_nc(
            conn,
            ratified,
            option="synthetic revived option",
            reason="synthetic ratified rejection",
            ratifier="session:founder",
            decided_at="2026-09-01 08:00:00",
            scope_predicate="file:synthetic/heal-guard.txt",
            source="declared",
            policy_domain_id=domain,
        )
        conn.commit()
        before = predicate_policy.project_policy_domain(conn, domain)
        assert [row.rejected_path_id for row in before.binding_rows] == [
            rejected_path_id
        ]

        heal.nightly_heal(
            conn,
            project_path=str(tmp_path),
            use_llm=True,
            integrity=False,
        )

        after = predicate_policy.project_policy_domain(conn, domain)
        assert "owner_unresolved_reconciliation" not in after.reason_counts
        assert after.advisory_rows == ()
        assert [row.rejected_path_id for row in after.binding_rows] == [
            rejected_path_id
        ]
        assert after.binding_rows[0].reconciler_ids == ()
        assert db.reconciliation_banner(conn, ratified) == []
        assert len(_referral_ids(conn, ratified, challenger)) == 1
    finally:
        conn.close()
