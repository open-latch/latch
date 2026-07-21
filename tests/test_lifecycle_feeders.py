"""Chunk-1 lifecycle-capture tests (KB 2299/2330).

Covers the deterministic feeder layer and its three surfacing sites:
- feeders.open_feeders: membership + intent-edge resolution, resolved/stale
  exclusions, edge-relation-wins dedupe.
- feeders.merge_feeder_rows: compactor context augmentation (role tagging,
  id dedupe, cap).
- COMPACT_PROMPT contract: temporal stance + closure duty present.
- project_direction backlog: declared-intent feeders join membership backlog.
- SessionStart brief: focus workstreams render their open feeders.
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import compactor  # noqa: E402
import db  # noqa: E402
import feeders  # noqa: E402
import project_direction  # noqa: E402
import session_start  # noqa: E402


def _mk_kb(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    conn = db.connect(str(project))
    return str(project), conn


def _ws(conn, title="Lifecycle workstream"):
    return db.insert_node(
        conn, kind="workstream", title=title, body="Objective: land chunk 1",
    )


def test_open_feeders_membership_and_edges(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    oq = db.insert_node(
        conn, kind="open_question", title="open member", body="x",
        workstream_id=ws,
    )
    db.insert_node(  # resolved open_question: canonical means answered
        conn, kind="open_question", title="resolved member", body="x",
        status="canonical", workstream_id=ws,
    )
    idea = db.insert_node(  # canonical idea: ratified, still forward-looking
        conn, kind="idea", title="ratified idea member", body="x",
        status="canonical", workstream_id=ws,
    )
    db.insert_node(
        conn, kind="fact", title="settled member", body="x", workstream_id=ws,
    )
    db.insert_node(
        conn, kind="idea", title="stale member", body="x", status="stale",
        workstream_id=ws,
    )
    feeder_fact = db.insert_node(
        conn, kind="fact", title="research feeding goal", body="x",
    )
    db.add_edge(conn, feeder_fact, ws, "advances")
    bystander = db.insert_node(conn, kind="fact", title="merely related", body="x")
    db.add_edge(conn, bystander, ws, "related_to")

    rows = feeders.open_feeders(conn, ws, limit=10)
    assert {r["id"] for r in rows} == {oq, idea, feeder_fact}
    assert [r["via"] for r in rows if r["id"] == feeder_fact] == ["advances"]
    conn.close()


def test_open_feeders_edge_relation_wins_over_membership(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    both = db.insert_node(
        conn, kind="idea", title="member and declared", body="x",
        workstream_id=ws,
    )
    db.add_edge(conn, both, ws, "motivates")
    rows = feeders.open_feeders(conn, ws, limit=10)
    assert [r["via"] for r in rows if r["id"] == both] == ["motivates"]
    conn.close()


def test_merge_feeder_rows_appends_focus_feeders(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    oq = db.insert_node(
        conn, kind="open_question", title="needs closing", body="x",
        workstream_id=ws,
    )

    related = [{"id": 999999, "kind": "fact", "title": "similarity hit"}]
    merged = feeders.merge_feeder_rows(conn, related)
    assert merged[0]["id"] == 999999
    tail = [r for r in merged if r.get("role") == "open_feeder"]
    assert {r["id"] for r in tail} == {oq}
    assert tail[0]["workstream_id"] == ws

    # A feeder already present in the sample is not appended twice.
    merged2 = feeders.merge_feeder_rows(
        conn, [{"id": oq, "kind": "open_question", "title": "needs closing"}],
    )
    assert [r["id"] for r in merged2] == [oq]
    conn.close()


def test_compact_prompt_carries_lifecycle_contract():
    for marker in (
        "Temporal stance",
        "FORWARD-LOOKING",
        "Done when:",
        "Closure duty",
        "open_feeder",
    ):
        assert marker in compactor.COMPACT_PROMPT


def test_project_direction_backlog_includes_edge_feeders(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    feeder_fact = db.insert_node(
        conn, kind="fact", title="evidence advancing goal", body="x",
    )
    db.add_edge(conn, feeder_fact, ws, "advances")

    report = project_direction.assemble_project_direction(conn)
    ws_row = next(r for r in report["workstreams"] if r["id"] == ws)
    by_id = {n["id"]: n for n in ws_row["backlog_items"]}
    assert feeder_fact in by_id
    assert by_id[feeder_fact]["relation"] == "advances"
    conn.close()


def test_session_brief_renders_open_feeders(tmp_path):
    project, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    db.insert_node(
        conn, kind="open_question", title="unresolved feeder", body="x",
        workstream_id=ws,
    )
    conn.close()

    brief = session_start._build_briefing(project)
    assert "open feeders:" in brief
    assert "unresolved feeder" in brief


def test_open_feeders_excludes_resolved_and_reopens_on_tombstone(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    resolved_oq = db.insert_node(
        conn, kind="open_question", title="answered elsewhere", body="x",
        workstream_id=ws,
    )
    superseded_idea = db.insert_node(
        conn, kind="idea", title="old direction", body="x", workstream_id=ws,
    )
    replaced_fact = db.insert_node(
        conn, kind="fact", title="abandoned research", body="x",
    )
    db.add_edge(conn, replaced_fact, ws, "advances")
    outcome = db.insert_node(conn, kind="progress", title="closed the loop", body="x")
    db.add_edge(conn, outcome, resolved_oq, "resolves")
    db.add_edge(conn, outcome, superseded_idea, "supersedes")
    db.add_edge(conn, outcome, replaced_fact, "replaces")
    still_open = db.insert_node(
        conn, kind="open_question", title="still open", body="x",
        workstream_id=ws,
    )

    # Resolution edges close feeders even though every node is still 'staging'.
    ids = {r["id"] for r in feeders.open_feeders(conn, ws, limit=0)}
    assert ids == {still_open}

    # Tombstoning the resolution edge re-opens the feeder (audit-stable inverse).
    db.tombstone_edge(conn, outcome, resolved_oq, "resolves")
    ids = {r["id"] for r in feeders.open_feeders(conn, ws, limit=0)}
    assert ids == {still_open, resolved_oq}
    conn.close()


def test_project_direction_edge_feeders_survive_member_crowding(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    feeder_fact = db.insert_node(
        conn, kind="fact", title="edge-only evidence", body="x",
    )
    db.add_edge(conn, feeder_fact, ws, "advances")
    for i in range(feeders.DEFAULT_LIMIT + 1):
        db.insert_node(
            conn, kind="idea", title=f"member idea {i}", body="x",
            workstream_id=ws,
        )

    report = project_direction.assemble_project_direction(conn)
    ws_row = next(r for r in report["workstreams"] if r["id"] == ws)
    by_id = {n["id"]: n for n in ws_row["backlog_items"]}
    assert feeder_fact in by_id
    assert by_id[feeder_fact]["relation"] == "advances"
    conn.close()


def test_project_direction_backlog_hides_resolved_members(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    resolved = db.insert_node(
        conn, kind="open_question", title="already handled", body="x",
        workstream_id=ws,
    )
    outcome = db.insert_node(conn, kind="progress", title="handled it", body="x")
    db.add_edge(conn, outcome, resolved, "resolves")
    open_member = db.insert_node(
        conn, kind="open_question", title="still pending", body="x",
        workstream_id=ws,
    )

    report = project_direction.assemble_project_direction(conn)
    ws_row = next(r for r in report["workstreams"] if r["id"] == ws)
    backlog_ids = {n["id"] for n in ws_row["backlog_items"]}
    assert open_member in backlog_ids
    assert resolved not in backlog_ids
    conn.close()


def test_project_direction_dual_member_edge_feeder_keeps_relation(tmp_path):
    _, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    dual = db.insert_node(
        conn, kind="idea", title="member with declared intent", body="x",
        workstream_id=ws,
    )
    db.add_edge(conn, dual, ws, "motivates")

    report = project_direction.assemble_project_direction(conn)
    ws_row = next(r for r in report["workstreams"] if r["id"] == ws)
    row = next(n for n in ws_row["backlog_items"] if n["id"] == dual)
    assert row["relation"] == "motivates"
    conn.close()


def test_session_brief_surfaces_feeder_ids_for_dedupe(tmp_path):
    project, conn = _mk_kb(tmp_path)
    ws = _ws(conn)
    db.set_focus(conn, ws)
    feeder_fact = db.insert_node(
        conn, kind="fact", title="declared feeder fact", body="x",
    )
    db.add_edge(conn, feeder_fact, ws, "depends_on")
    conn.close()

    surfaced: list[int] = []
    brief = session_start._build_briefing(project, surfaced_ids=surfaced)
    assert "declared feeder fact" in brief
    assert feeder_fact in surfaced
