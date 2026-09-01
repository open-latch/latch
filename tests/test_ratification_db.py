"""DB-level regressions for V3 ratification-bound judgment authority.

These tests pin the transition invariant from Latch nodes 5011/5143: judgment
kinds may become canonical only after a structured ratification row exists,
while legacy canonical state remains valid without synthetic backfill.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.store import db  # noqa: E402
from latch.pipeline import heal  # noqa: E402
from latch.store import paths  # noqa: E402
from latch.store import schema_version  # noqa: E402
from latch.install import versioning  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(str(tmp_path))
    try:
        yield connection
    finally:
        connection.close()


def _node(conn, kind: str = "decision", *, status: str = "staging") -> int:
    return db.insert_node(
        conn,
        kind=kind,
        title=f"{kind} under review",
        body="authority is pending",
        status=status,
    )


def _required_error_type():
    # The fallback makes the pre-repair failure say "DID NOT RAISE" instead of
    # failing while merely looking up the not-yet-implemented exception.
    return getattr(db, "RatificationRequiredError", RuntimeError)


def _insert_ratification(conn, node_id: int, **kwargs) -> int:
    row_id = db.insert_ratification_nc(conn, node_id, **kwargs)
    conn.commit()
    return row_id


def test_authority_kind_constants_are_the_ratified_narrow_mapping():
    assert getattr(db, "JUDGMENT_KINDS", None) == frozenset(
        {"decision", "preference"}
    )
    assert getattr(db, "EVIDENCE_PROMOTION_KINDS", None) == frozenset(
        {"fact", "progress"}
    )


def test_ratification_table_is_additive_without_schema_version_bump(conn):
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ratification'"
    ).fetchone()
    assert table is not None
    assert schema_version.read(conn) == versioning.KB_SCHEMA_VERSION
    assert schema_version.ensure_supported(conn) == versioning.KB_SCHEMA_VERSION


def test_ratification_round_trips_closed_structural_metadata(conn):
    node_id = _node(conn)
    row_id = _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        decided_at="2026-08-10 12:34:56",
        action="ratify",
        scope="node",
        source="capture_decision",
    )

    row = db.ratification_for_node(conn, node_id)
    assert row == {
        "id": row_id,
        "node_id": node_id,
        "ratifier": "session:founder",
        "decided_at": "2026-08-10 12:34:56",
        "action": "ratify",
        "scope": "node",
        "source": "capture_decision",
    }
    assert db.list_ratifications(conn, action="ratify") == [row]
    assert db.list_ratifications(conn, source="latch_update") == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ratifier", "   "),
        ("action", "approve"),
        ("scope", "project"),
        ("source", "maintenance"),
    ],
)
def test_ratification_metadata_is_closed_and_ratifier_is_identified(
    conn, field, value
):
    node_id = _node(conn)
    kwargs = {
        "ratifier": "session:founder",
        "action": "ratify",
        "scope": "node",
        "source": "latch_update",
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        _insert_ratification(conn, node_id, **kwargs)


@pytest.mark.parametrize("kind", ["decision", "preference"])
def test_g1_no_judgment_transition_to_canonical_without_ratification(conn, kind):
    node_id = _node(conn, kind)
    with pytest.raises(_required_error_type()):
        db.update_node(conn, node_id, status="canonical")
    assert db.get_node(conn, node_id)["status"] == "staging"


def test_reject_row_does_not_authorize_canonical_transition(conn):
    node_id = _node(conn)
    _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        action="reject",
        source="capture_decision",
    )
    with pytest.raises(_required_error_type()):
        db.update_node(conn, node_id, status="canonical")
    assert db.get_node(conn, node_id)["status"] == "staging"


def test_latest_ratification_action_governs_and_history_remains_auditable(conn):
    node_id = _node(conn)
    reject_id = _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        decided_at="2026-08-10 12:00:00",
        action="reject",
        source="capture_decision",
    )
    with pytest.raises(_required_error_type()):
        db.update_node(conn, node_id, status="canonical")

    ratify_id = _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        decided_at="2026-08-10 12:01:00",
        action="ratify",
        source="latch_update",
    )
    db.update_node(conn, node_id, status="canonical")
    db.update_node(conn, node_id, status="staging")

    final_reject_id = _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        decided_at="2026-08-10 12:02:00",
        action="reject",
        source="capture_decision",
    )

    latest = db.ratification_for_node(conn, node_id)
    assert latest is not None
    assert latest["id"] == final_reject_id
    assert latest["action"] == "reject"
    history = db.list_ratifications(conn)
    assert [row["id"] for row in history] == [
        reject_id,
        ratify_id,
        final_reject_id,
    ]
    assert [row["action"] for row in history] == [
        "reject",
        "ratify",
        "reject",
    ]
    with pytest.raises(_required_error_type()):
        db.update_node(conn, node_id, status="canonical")
    assert db.get_node(conn, node_id)["status"] == "staging"


@pytest.mark.parametrize("source", ["capture_decision", "latch_update"])
def test_ratify_row_authorizes_judgment_transition(conn, source):
    node_id = _node(conn, "preference")
    _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        action="ratify",
        source=source,
    )
    db.update_node(conn, node_id, status="canonical")
    assert db.get_node(conn, node_id)["status"] == "canonical"


def test_unique_ratification_shape_migrates_without_version_bump(tmp_path):
    project = tmp_path / "unique-ratification"
    connection = db.connect(str(project))
    node_id = _node(connection)
    schema_before = schema_version.read(connection)
    connection.execute("DROP TABLE ratification")
    connection.execute(
        """
        CREATE TABLE ratification (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id    INTEGER NOT NULL UNIQUE
                       REFERENCES nodes(id) ON DELETE CASCADE,
            ratifier   TEXT    NOT NULL CHECK (length(trim(ratifier)) > 0),
            decided_at TEXT    NOT NULL DEFAULT (datetime('now')),
            action     TEXT    NOT NULL CHECK (action IN ('ratify', 'reject')),
            scope      TEXT    NOT NULL DEFAULT 'node'
                               CHECK (scope IN ('node')),
            source     TEXT    NOT NULL
                               CHECK (source IN ('capture_decision', 'latch_update'))
        )
        """
    )
    original_id = connection.execute(
        """
        INSERT INTO ratification
            (node_id, ratifier, decided_at, action, scope, source)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            node_id,
            "session:founder",
            "2026-08-10 12:00:00",
            "reject",
            "node",
            "capture_decision",
        ),
    ).lastrowid
    connection.commit()
    connection.close()

    reopened = db.connect(str(project))
    try:
        assert schema_version.read(reopened) == schema_before
        original = db.ratification_for_node(reopened, node_id)
        assert original is not None
        assert original["id"] == original_id
        assert original["action"] == "reject"

        ratify_id = _insert_ratification(
            reopened,
            node_id,
            ratifier="session:founder",
            action="ratify",
            source="latch_update",
        )
        assert ratify_id > original_id
        assert [row["action"] for row in db.list_ratifications(reopened)] == [
            "reject",
            "ratify",
        ]
    finally:
        reopened.close()

    reopened_again = db.connect(str(project))
    try:
        assert schema_version.read(reopened_again) == schema_before
        assert [
            row["action"] for row in db.list_ratifications(reopened_again)
        ] == ["reject", "ratify"]
    finally:
        reopened_again.close()


def test_ratification_history_does_not_change_rejected_path_semantics(conn):
    node_id = _node(conn)
    rejected_before = db.count_rejected_paths(conn)
    rejected_id = db.insert_rejected_path(
        conn,
        node_id,
        option="Redis sessions",
        reason="The operational cost is not justified.",
        ratifier="session:founder",
        decided_at="2026-08-10 12:00:00",
        scope_predicate="session storage",
        source="declared",
    )
    assert rejected_id is not None
    rejected_snapshot = db.rejected_paths_for_node(conn, node_id)

    _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        action="reject",
        source="capture_decision",
    )
    _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        action="ratify",
        source="latch_update",
    )

    duplicate = db.insert_rejected_path(
        conn,
        node_id,
        option="Redis sessions",
        reason="A duplicate remains a no-op.",
        source="declared",
    )
    assert duplicate is None
    assert db.count_rejected_paths(conn) == rejected_before + 1
    assert db.rejected_paths_for_node(conn, node_id) == rejected_snapshot


def test_legacy_canonical_judgment_is_grandfathered_without_backfill(tmp_path):
    project = tmp_path / "legacy-current"
    connection = db.connect(str(project))
    node_id = connection.execute(
        "INSERT INTO nodes(kind,title,body,status) VALUES(?,?,?,?)",
        ("decision", "legacy authority", "still authoritative", "canonical"),
    ).lastrowid
    connection.execute("DROP TABLE ratification")
    connection.commit()
    schema_before = schema_version.read(connection)
    connection.close()

    reopened = db.connect(str(project))
    try:
        assert schema_version.read(reopened) == schema_before
        assert db.get_node(reopened, node_id)["status"] == "canonical"
        assert db.ratification_for_node(reopened, node_id) is None
        assert db.list_ratifications(reopened) == []

        heal_result = heal.nightly_heal(
            reopened,
            project_path=str(project),
            use_llm=False,
            integrity=True,
            contradictions=False,
        )
        assert heal_result["ok"] is True
        assert db.get_node(reopened, node_id)["status"] == "canonical"
        assert db.ratification_for_node(reopened, node_id) is None
        assert db.count_ratifications(reopened) == 0

        # The guard governs transitions, not pre-existing state. A canonical
        # legacy row may still receive an ordinary edit without invented proof.
        db.update_node(
            reopened,
            node_id,
            body="still authoritative after ordinary maintenance",
            status="canonical",
        )
        assert db.get_node(reopened, node_id)["status"] == "canonical"
        assert db.ratification_for_node(reopened, node_id) is None
    finally:
        reopened.close()


def test_ratification_row_cascades_with_its_node(conn):
    node_id = _node(conn)
    _insert_ratification(
        conn,
        node_id,
        ratifier="session:founder",
        action="reject",
        source="capture_decision",
    )
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    conn.commit()
    assert db.ratification_for_node(conn, node_id) is None
