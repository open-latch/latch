"""Acceptance regressions for unattended heal's ratified-node immunity."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import drift  # noqa: E402
import heal  # noqa: E402


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
            "check_and_record",
            lambda *_args, **_kwargs: (True, {}),
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
