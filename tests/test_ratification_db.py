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

import db  # noqa: E402
import heal  # noqa: E402
import paths  # noqa: E402
import schema_version  # noqa: E402
import versioning  # noqa: E402


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
