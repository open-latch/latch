from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import feeders  # noqa: E402
import heal  # noqa: E402
import mcp_server  # noqa: E402
import priorities  # noqa: E402
import seed  # noqa: E402
import workstreams  # noqa: E402


@pytest.fixture
def boundary_kb(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    conn = db.connect(str(project))
    monkeypatch.setattr(mcp_server, "_conn", lambda: db.connect(str(project)))
    monkeypatch.setattr(mcp_server, "PROJECT_CWD", str(project))
    try:
        yield str(project), conn
    finally:
        conn.close()


def _lane(conn, title, *, status="staging"):
    return db.insert_node(
        conn, kind="workstream", title=title, body=title, status=status,
    )


def _merged_pair(conn):
    source = _lane(conn, "Merged source", status="stale")
    active = _lane(conn, "Active absorber")
    db.add_edge(conn, source, active, "merged_into")
    return source, active


def test_mcp_insert_and_update_redirect_merged_membership(boundary_kb, monkeypatch):
    _, conn = boundary_kb
    merged, active = _merged_pair(conn)

    def insert_without_heal(db_conn, *, kind, title, body, status, workstream_id, **_kwargs):
        return {
            "id": db.insert_node(
                db_conn, kind=kind, title=title, body=body, status=status,
                workstream_id=workstream_id,
            )
        }

    monkeypatch.setattr(heal, "insert_with_heal", insert_without_heal)
    inserted = mcp_server.kb_insert(
        "fact", "redirected insert", "body", workstream_id=merged,
    )
    assert inserted["workstream_resolution"]["resolved_workstream_id"] == active
    assert db.get_node(conn, inserted["id"])["workstream_id"] == active

    orphan = db.insert_node(conn, kind="fact", title="move me", body="body")
    updated = mcp_server.kb_update(orphan, workstream_id=merged)
    assert updated["workstream_resolution"]["state"] == "merged"
    assert db.get_node(conn, orphan)["workstream_id"] == active


def test_mcp_membership_rejects_closed_or_missing_lane_without_write(
    boundary_kb, monkeypatch,
):
    _, conn = boundary_kb
    closed = _lane(conn, "Closed", status="stale")
    calls = []
    monkeypatch.setattr(heal, "insert_with_heal", lambda *_a, **_k: calls.append(True))
    rejected = mcp_server.kb_insert("fact", "no", "write", workstream_id=closed)
    assert rejected["ok"] is False
    assert rejected["workstream_resolution"]["state"] == "closed"
    assert calls == []

    orphan = db.insert_node(conn, kind="fact", title="unchanged", body="old")
    rejected_update = mcp_server.kb_update(orphan, title="new", workstream_id=999999)
    assert rejected_update["ok"] is False
    assert db.get_node(conn, orphan)["title"] == "unchanged"


def test_mcp_update_allows_ordinary_edits_in_closed_lane(boundary_kb):
    _, conn = boundary_kb
    closed = _lane(conn, "Closed history", status="stale")
    member = db.insert_node(
        conn,
        kind="decision",
        title="Historical decision",
        body="Original wording",
        status="canonical",
        workstream_id=closed,
    )

    explicit = mcp_server.kb_update(
        member,
        title="Must not write",
        workstream_id=closed,
    )
    assert explicit["ok"] is False
    assert "closed (stale)" in explicit["error"]
    assert db.get_node(conn, member)["title"] == "Historical decision"

    result = mcp_server.kb_update(
        member,
        title="Corrected historical decision",
        body="Corrected wording",
        status="staging",
    )

    assert result["ok"] is True
    assert "workstream_resolution" not in result
    updated = db.get_node(conn, member)
    assert updated["title"] == "Corrected historical decision"
    assert updated["body"] == "Corrected wording"
    assert updated["status"] == "staging"
    assert updated["workstream_id"] == closed


def test_mcp_update_does_not_reparent_merged_membership_unless_requested(
    boundary_kb,
):
    _, conn = boundary_kb
    merged, active = _merged_pair(conn)
    member = db.insert_node(
        conn,
        kind="fact",
        title="Historical member",
        body="Before correction",
        status="canonical",
        workstream_id=merged,
    )

    corrected = mcp_server.kb_update(member, body="After correction")

    assert corrected["ok"] is True
    assert "workstream_resolution" not in corrected
    assert db.get_node(conn, member)["workstream_id"] == merged

    moved = mcp_server.kb_update(member, workstream_id=merged)
    assert moved["ok"] is True
    assert moved["workstream_resolution"]["resolved_workstream_id"] == active
    assert db.get_node(conn, member)["workstream_id"] == active


def test_mcp_generic_insert_cannot_bypass_priority_ordering(
    boundary_kb, monkeypatch,
):
    _, conn = boundary_kb
    calls = []
    monkeypatch.setattr(heal, "insert_with_heal", lambda *_a, **_k: calls.append(True))
    before = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'priority'",
    ).fetchone()[0]

    result = mcp_server.kb_insert(
        "priority", "Bypass ordering", "must not be inserted",
    )

    assert result["ok"] is False
    assert "latch_priority_add" in result["error"]
    assert calls == []
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind = 'priority'",
    ).fetchone()[0] == before


@pytest.mark.parametrize(
    "change",
    [
        {"title": "renamed"},
        {"body": "replacement body"},
        {"status": "stale"},
        {"workstream_id": 999999},
    ],
)
def test_mcp_generic_update_cannot_mutate_workstream_lifecycle_fields(
    boundary_kb, change,
):
    _, conn = boundary_kb
    lane = _lane(conn, "Machine-owned lane", status="canonical")
    before = db.get_node(conn, lane)

    result = mcp_server.kb_update(lane, **change)

    assert result["ok"] is False
    assert "machine-owned" in result["error"]
    after = db.get_node(conn, lane)
    assert after["title"] == before["title"]
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert after["workstream_id"] == before["workstream_id"]


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Changed directive"},
        {"body": "Changed note"},
        {"status": "stale"},
        {"workstream_id": "lane"},
    ],
)
def test_mcp_generic_update_cannot_mutate_priority_state(boundary_kb, change):
    _, conn = boundary_kb
    lane = _lane(conn, "Priority target", status="canonical")
    priority = db.insert_node(
        conn, kind="priority", title="Keep scope", body="Keep scope",
    )
    before = db.get_node(conn, priority)
    resolved_change = dict(change)
    if resolved_change.get("workstream_id") == "lane":
        resolved_change["workstream_id"] = lane

    result = mcp_server.kb_update(priority, **resolved_change)

    assert result["ok"] is False
    assert "priority membership and state" in result["error"]
    after = db.get_node(conn, priority)
    assert after["title"] == before["title"]
    assert after["body"] == before["body"]
    assert after["status"] == before["status"]
    assert after["workstream_id"] == before["workstream_id"]


@pytest.mark.parametrize(
    "relation",
    ["merged_into", "closed_in_favor_of", "branched_from"],
)
def test_mcp_link_tools_reserve_lifecycle_owned_relations(boundary_kb, relation):
    _, conn = boundary_kb
    source = _lane(conn, f"{relation} source", status="stale")
    target = _lane(conn, f"{relation} target", status="canonical")

    rejected_link = mcp_server.kb_link(source, target, relation)
    assert rejected_link["ok"] is False
    assert "machine-owned" in rejected_link["error"]
    assert conn.execute(
        "SELECT COUNT(*) FROM edges WHERE src=? AND dst=? AND relation=?",
        (source, target, relation),
    ).fetchone()[0] == 0

    db.add_edge_nc(conn, source, target, relation, created_by="lifecycle:test")
    conn.commit()
    rejected_unlink = mcp_server.kb_unlink(source, target, relation)
    assert rejected_unlink["ok"] is False
    assert "machine-owned" in rejected_unlink["error"]
    assert conn.execute(
        "SELECT status FROM edges WHERE src=? AND dst=? AND relation=?",
        (source, target, relation),
    ).fetchone()["status"] == "active"


def test_kb_get_redirects_merged_identity_with_receipt(boundary_kb):
    _, conn = boundary_kb
    merged, active = _merged_pair(conn)
    result = mcp_server.kb_get(merged, include_neighbors=False)
    assert result["id"] == active
    assert result["workstream_resolution"] == {
        "requested_workstream_id": merged,
        "resolved_workstream_id": active,
        "state": "merged",
        "path": [merged, active],
        "receipt": f"latch redirected merged workstream {merged} to active workstream {active}.",
        "merge_evidence": [],
    }
    historical = mcp_server.kb_get(
        merged, include_neighbors=False, resolve_workstream=False,
    )
    assert historical["id"] == merged and historical["status"] == "stale"


def test_kb_get_redirect_surfaces_immutable_merge_receipt(boundary_kb):
    project, conn = boundary_kb
    source = _lane(conn, "Receipt source")
    absorber = _lane(conn, "Receipt absorber")
    merged = workstreams.merge_workstreams(
        conn,
        source,
        absorber,
        op_key="boundary:merge:receipt",
        evidence={"coactive_sessions": 4, "window_sessions": 5},
        project_path=project,
    )

    result = mcp_server.kb_get(source, include_neighbors=False)
    resolution = result["workstream_resolution"]
    assert result["id"] == absorber
    assert resolution["receipt"] == merged["receipt"]
    assert resolution["merge_evidence"] == [{
        "source_workstream_id": source,
        "absorber_workstream_id": absorber,
        "operation_id": merged["operation_id"],
        "op_key": "boundary:merge:receipt",
        "receipt": merged["receipt"],
    }]

    write_resolution = workstreams.resolve_membership_target(conn, source)
    assert write_resolution["receipt"] == merged["receipt"]
    assert write_resolution["merge_evidence"] == resolution["merge_evidence"]


def test_kb_append_redirects_merged_lane_and_rejects_closed_identity(boundary_kb):
    project, conn = boundary_kb
    source = _lane(conn, "Append source")
    absorber = _lane(conn, "Append absorber")
    merged = workstreams.merge_workstreams(
        conn,
        source,
        absorber,
        op_key="boundary:append:merge",
        evidence={"coactive_sessions": 4, "window_sessions": 5},
        project_path=project,
    )
    source_body = db.get_node(conn, source)["body"]

    appended = mcp_server.kb_append(source, "state moved to the absorber")

    assert appended["ok"] is True
    assert appended["id"] == absorber
    assert appended["workstream_resolution"]["requested_workstream_id"] == source
    assert appended["workstream_resolution"]["resolved_workstream_id"] == absorber
    assert appended["workstream_resolution"]["receipt"] == merged["receipt"]
    assert appended["workstream_resolution"]["merge_evidence"][0]["op_key"] \
        == "boundary:append:merge"
    assert db.get_node(conn, source)["body"] == source_body
    assert "state moved to the absorber" in db.get_node(conn, absorber)["body"]

    closed = _lane(conn, "Actually closed", status="stale")
    closed_body = db.get_node(conn, closed)["body"]
    rejected = mcp_server.kb_append(closed, "must not write")
    assert rejected["ok"] is False
    assert rejected["workstream_resolution"]["state"] == "closed"
    assert db.get_node(conn, closed)["body"] == closed_body


def test_priority_focus_and_feeders_obey_lane_lifecycle(boundary_kb):
    _, conn = boundary_kb
    merged, active = _merged_pair(conn)
    closed = _lane(conn, "Closed", status="stale")
    feeder = db.insert_node(
        conn, kind="idea", title="historical feeder", body="body",
        workstream_id=merged,
    )
    assert feeder
    assert feeders.open_feeders(conn, merged) == []
    assert feeders.open_feeders(conn, closed) == []

    assert db.bump_focus_nc(conn, merged, delta=2.0) is True
    conn.commit()
    assert db.get_focus_row(conn, merged) is None
    assert db.get_focus_row(conn, active) is not None
    assert db.bump_focus_nc(conn, closed) is False
    conn.rollback()

    added = priorities.add_priority(conn, "redirect scope", workstream_id=merged)
    assert added["workstream_id"] == active
    assert added["workstream_resolution"]["state"] == "merged"
    rejected = priorities.add_priority(conn, "reject scope", workstream_id=closed)
    assert "error" in rejected and rejected["workstream_resolution"]["state"] == "closed"


def test_seed_parent_resolution_redirects_merged_identity(boundary_kb, monkeypatch):
    project, conn = boundary_kb
    merged, active = _merged_pair(conn)
    resolved, error = seed.resolve_existing_workstream_target(project, merged)
    assert error is None and resolved == active

    candidate = seed.SeedCandidate(
        kind="fact",
        title="Imported child",
        body="Imported child body",
        confidence=0.9,
        signals=["decision"],
        source_ids=[],
        source_paths=[],
        source_mtimes=[],
        source_digests=[],
        workstream_key=f"existing:{merged}",
    )

    def insert_without_heal(db_conn, *, kind, title, body, status, workstream_id, **_kwargs):
        return {
            "id": db.insert_node(
                db_conn, kind=kind, title=title, body=body, status=status,
                workstream_id=workstream_id,
            )
        }

    monkeypatch.setattr(heal, "insert_with_heal", insert_without_heal)
    applied = seed.apply_candidates(
        [candidate], project_path=project, existing_workstream_id=merged,
        workstream_scope=f"existing:{merged}",
    )
    assert applied.complete and len(applied.inserted_ids) == 1
    assert db.get_node(conn, applied.inserted_ids[0])["workstream_id"] == active
