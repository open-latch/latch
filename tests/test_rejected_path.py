"""Tests for the V2 typed-rejection slice (KB id=3948 phase V item V2).

V2's text: *typed `rejected` status {option, reason, ratifier, date, scope
predicate}; backfill existing decisions; retire substring detection.*

Two scope readings are pinned here, both deliberate and both departures from
the most literal reading of 3948:

1. **A rejection is a row, not a node status.** A decision that records a
   rejected alternative is usually itself canonical and adopted — id=2224
   ratifies a tool surface *and* lists its rejected alternatives. Stamping that
   node ``status='rejected'`` would destroy its authority. Rejection is a
   property of an option, so it gets its own table keyed to the recording node.

2. **No KB_SCHEMA_VERSION bump.** ``rejected_path`` is additive side state,
   following the ``_migrate_seed_import_ledgers`` precedent ("without a version
   bump"). Bumping would stamp the vault past an older installed engine and
   trip ``SchemaTooNewError`` (id=2694) for a purely additive change.

The substring test that used to select the report's rejection commentary is
retired here: measured against the pre-registered rubric it was 50% precision
and 50% recall, and its dominant false positive was Latch's own roadmap text
describing this very feature.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db            # noqa: E402
import gate_report   # noqa: E402
import schema_version  # noqa: E402
import versioning    # noqa: E402


@pytest.fixture()
def conn():
    tmp = tempfile.mkdtemp(prefix="kb_v2_rejected_test_")
    connection = db.connect(tmp)
    try:
        yield connection
    finally:
        connection.close()
        shutil.rmtree(tmp, ignore_errors=True)


def _decision(conn, title="Adopt Redis for session cache", status="canonical"):
    return db.insert_node(
        conn, kind="decision", title=title, body="body", status=status
    )


# --- schema ---------------------------------------------------------------


def test_migration_creates_table_without_schema_version_bump(conn):
    """Additive side state must not move the compatibility boundary (id=1694
    governs bumps; id=2694 is the lockout this avoids)."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='rejected_path'"
    ).fetchone()
    assert row is not None
    # The vault this migration just built must still be readable by an engine
    # pinned at the same KB_SCHEMA_VERSION the repo ships.
    assert schema_version.read(conn) <= versioning.KB_SCHEMA_VERSION
    assert schema_version.ensure_supported(conn) <= versioning.KB_SCHEMA_VERSION


def test_migration_is_idempotent(conn):
    db._migrate_rejected_path(conn)
    db._migrate_rejected_path(conn)
    assert db.count_rejected_paths(conn) == 0


# --- the five declared fields --------------------------------------------


def test_records_all_five_declared_fields(conn):
    node_id = _decision(conn)
    row_id = db.insert_rejected_path(
        conn,
        node_id,
        option="in-process LRU cache",
        reason="loses state across worker restarts",
        ratifier="founder",
        decided_at="2026-07-30",
        scope_predicate="package:cache",
    )
    assert row_id is not None
    (row,) = db.rejected_paths_for_node(conn, node_id)
    assert row["option"] == "in-process LRU cache"
    assert row["reason"] == "loses state across worker restarts"
    assert row["ratifier"] == "founder"
    assert row["decided_at"] == "2026-07-30"
    assert row["scope_predicate"] == "package:cache"
    assert row["source"] == "declared"


@pytest.mark.parametrize("field", ["option", "reason"])
def test_option_and_reason_are_required(conn, field):
    """An unexplained rejection cannot support a revival check — a row with an
    empty reason would read as authoritative while carrying nothing actionable."""
    node_id = _decision(conn)
    kwargs = {"option": "some path", "reason": "some reason"}
    kwargs[field] = "   "
    with pytest.raises(ValueError):
        db.insert_rejected_path(conn, node_id, **kwargs)


def test_source_must_be_declared_or_backfill(conn):
    node_id = _decision(conn)
    with pytest.raises(ValueError):
        db.insert_rejected_path(
            conn, node_id, option="x", reason="y", source="guessed"
        )


def test_backfill_source_is_distinguishable_from_declared(conn):
    """A recovered row must never carry the authority of a declared one."""
    node_id = _decision(conn)
    db.insert_rejected_path(
        conn, node_id, option="rewrite history", reason="breaks clones",
        source="backfill",
    )
    assert len(db.list_rejected_paths(conn, source="backfill")) == 1
    assert db.list_rejected_paths(conn, source="declared") == []


def test_reinserting_same_option_is_a_noop_not_an_error(conn):
    """Backfill must be safely re-runnable."""
    node_id = _decision(conn)
    first = db.insert_rejected_path(conn, node_id, option="opt", reason="r")
    second = db.insert_rejected_path(conn, node_id, option="opt", reason="r")
    assert first is not None
    assert second is None
    assert db.count_rejected_paths(conn) == 1


def test_rows_are_removed_with_their_node(conn):
    node_id = _decision(conn)
    db.insert_rejected_path(conn, node_id, option="opt", reason="r")
    conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    conn.commit()
    assert db.count_rejected_paths(conn) == 0


# --- the status decision --------------------------------------------------


def test_recording_a_rejection_does_not_alter_node_status(conn):
    """The load-bearing scope decision. ~12 sites treat `status != 'stale'` as
    live (gate.py:469,525,1079 among them), so a `rejected` status value would
    read as an ACTIVE decision inside the gate's own retrieval — and would also
    strip authority from canonical decisions that merely *record* a rejection."""
    node_id = _decision(conn, status="canonical")
    db.insert_rejected_path(
        conn, node_id, option="NoSQL migration", reason="audit joins required"
    )
    assert db.get_node(conn, node_id)["status"] == "canonical"


def test_rejections_on_stale_nodes_still_count(conn):
    """A rejection later superseded was still genuine when made — the rubric
    takes the count over all statuses."""
    node_id = _decision(conn, status="stale")
    db.insert_rejected_path(conn, node_id, option="opt", reason="r")
    assert len(db.list_rejected_paths(conn)) == 1
    assert db.list_rejected_paths(conn, include_stale_nodes=False) == []


# --- retiring the substring test -----------------------------------------


def test_commentary_fires_on_typed_row_not_on_title_keyword(conn):
    node_id = _decision(conn, title="Adopt Redis, no keyword here")
    db.insert_rejected_path(conn, node_id, option="in-process LRU", reason="r")
    nodes = gate_report._node_map(conn, [node_id])
    assert nodes[node_id]["rejected_path_count"] == 1
    assert "typed rejected path" in gate_report._node_commentary(nodes[node_id])


def test_commentary_no_longer_fires_on_latch_describing_itself(conn):
    """The dominant false positive: Latch's own roadmap text. Nodes 3948, 3950,
    3952, 3938, 3939, 3931 and 3926 all tripped the retired keyword test."""
    node_id = _decision(
        conn,
        title="Ratified roadmap: typed `rejected` status, retire substring detection",
    )
    nodes = gate_report._node_map(conn, [node_id])
    assert nodes[node_id]["rejected_path_count"] == 0
    assert "typed rejected path" not in gate_report._node_commentary(nodes[node_id])


def test_commentary_catches_rejection_phrased_without_the_keyword(conn):
    """The retired test missed 8 of 16 confirmed rejections — ids 1157, 1680,
    1917, 2257, 2354, 2476, 3226, 3876 — all phrased "X instead of Y"."""
    node_id = _decision(conn, title="Use per-project wiring self-repair, not registry")
    db.insert_rejected_path(
        conn, node_id, option="global registry of wired repos",
        reason="drift risk lives in project-local wiring",
    )
    nodes = gate_report._node_map(conn, [node_id])
    assert "typed rejected path" in gate_report._node_commentary(nodes[node_id])


def test_report_degrades_to_zero_when_table_absent(conn):
    """A report run against a vault an older engine created must still work."""
    node_id = _decision(conn)
    conn.execute("DROP TABLE rejected_path")
    conn.commit()
    nodes = gate_report._node_map(conn, [node_id])
    assert nodes[node_id]["rejected_path_count"] == 0
