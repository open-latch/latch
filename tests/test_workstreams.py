from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import db  # noqa: E402
import feeders  # noqa: E402
import lifecycle_receipts  # noqa: E402
import priorities  # noqa: E402
import rolling  # noqa: E402
import workstreams  # noqa: E402


@pytest.fixture
def kb(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    conn = db.connect(str(project))
    monkeypatch.setattr(
        workstreams, "_similar_workstreams",
        lambda _conn, _text, *, embedding=None: (embedding, []),
    )
    monkeypatch.setattr(workstreams, "_auto_plan_is_current", lambda *_a, **_k: True)
    try:
        yield str(project), conn
    finally:
        conn.close()


def _node(conn, kind, title, *, status="staging", workstream_id=None):
    return db.insert_node(
        conn, kind=kind, title=title, body=title, status=status,
        workstream_id=workstream_id,
    )


def _open(
    project, conn, *, op_key="open:one", title="Ship lifecycle", members=(),
    candidate_key=None,
):
    return workstreams.open_workstream(
        conn,
        title=title,
        objective="Ship deterministic lifecycle automation",
        done_when="OPEN and CLOSE round-trip atomically",
        scope_boundary="Lifecycle state only",
        next_step="Exercise the focused tests",
        member_ids=members,
        op_key=op_key,
        recurrence={"session_count": 2, "session_ids": ["s1", "s2"], "since": "2026-07-01"},
        project_path=project,
        candidate_key=candidate_key,
    )


def _seed_post_open_sessions(
    conn,
    *,
    opened_at: str,
    count: int,
    contact_workstream_id: int | None = None,
):
    opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    ambient = _node(conn, "fact", f"ambient evidence {opened_at}")
    session_ids = []
    for index in range(count):
        session_id = f"post-open-{ambient}-{index}"
        started = opened + timedelta(hours=2 + index * 2)
        stamp = started.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO sessions(id,project_path,started_at) VALUES(?, '/p', ?)",
            (session_id, stamp),
        )
        conn.commit()
        db.record_retrieval_events(
            conn,
            source="prompt",
            session_id=session_id,
            turn=1,
            items=[(ambient, 0.5)],
            ts=stamp,
        )
        session_ids.append(session_id)
    if contact_workstream_id is not None and session_ids:
        contact_ts = opened + timedelta(hours=2 + (count - 1) * 2, minutes=10)
        db.record_retrieval_events(
            conn,
            source="tool",
            session_id=session_ids[-1],
            turn=2,
            items=[(int(contact_workstream_id), 1.0)],
            ts=contact_ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
    return session_ids


def test_resolve_active_closed_merged_and_cycle(kb):
    _, conn = kb
    active = _node(conn, "workstream", "active")
    closed = _node(conn, "workstream", "closed", status="stale")
    merged = _node(conn, "workstream", "merged", status="stale")
    db.add_edge(conn, merged, active, "merged_into")

    assert workstreams.resolve_active(conn, active)["state"] == "active"
    assert workstreams.resolve_active(conn, closed)["active_id"] is None
    resolved = workstreams.resolve_active(conn, merged)
    assert resolved["state"] == "merged"
    assert resolved["active_id"] == active
    assert resolved["path"] == [merged, active]

    a = _node(conn, "workstream", "cycle-a", status="stale")
    b = _node(conn, "workstream", "cycle-b", status="stale")
    db.add_edge(conn, a, b, "merged_into")
    db.add_edge(conn, b, a, "merged_into")
    assert workstreams.resolve_active(conn, a)["state"] == "cycle"


def test_open_is_atomic_audited_and_idempotent(kb):
    project, conn = kb
    member = _node(conn, "idea", "member")
    for i, score in enumerate((10.0, 7.0, 4.0), start=1):
        wid = _node(conn, "workstream", f"existing {i}")
        db.set_focus_row_nc(
            conn, wid, score=score, set_at=db._now(), set_by="test", pinned=False,
        )
    db.recompute_focus_ranks_nc(conn)
    conn.commit()

    result = _open(
        project, conn, members=[member], candidate_key="detector:open:candidate",
    )
    wid = result["workstream_id"]
    node = db.get_node(conn, wid)
    assert node["status"] == "staging"
    for heading in ("Objective:", "Done when:", "Scope boundary:", "Next step:"):
        assert heading in node["body"]
    assert db.get_node(conn, member)["workstream_id"] == wid
    focus = db.get_focus_row(conn, wid)
    assert focus["pinned"] == 0
    assert focus["score"] < 4.0
    ledger = db.get_workstream_op(conn, "open:one")
    assert ledger["state"] == "applied"
    assert ledger["candidate_key"] == "detector:open:candidate"
    assert ledger["payload"]["assigned_member_ids"] == [member]
    assert result["receipt"] == (
        'latch opened workstream "Ship lifecycle" — recurred across 2 sessions '
        "since 2026-07-01; Done when: OPEN and CLOSE round-trip atomically."
    )

    retry = _open(
        project, conn, members=[member], candidate_key="detector:open:candidate",
    )
    assert retry["idempotent"] is True
    assert retry["workstream_id"] == wid
    assert conn.execute(
        "SELECT COUNT(*) n FROM nodes WHERE kind = 'workstream' AND title = 'Ship lifecycle'"
    ).fetchone()["n"] == 1

    with pytest.raises(workstreams.WorkstreamConflictError):
        _open(project, conn, op_key="open:one", title="Different request")


def test_automatic_open_requires_cross_session_recurrence(kb):
    project, conn = kb
    with pytest.raises(workstreams.WorkstreamValidationError):
        workstreams.open_workstream(
            conn,
            title="one-shot", objective="x", done_when="y", scope_boundary="z",
            next_step="n", op_key="auto:one", origin="auto",
            recurrence={"session_count": 2, "session_ids": ["same"]},
            project_path=project,
        )
    assert conn.execute("SELECT COUNT(*) n FROM workstream_ops").fetchone()["n"] == 0


def test_automatic_open_probation_starts_after_historical_recurrence(kb):
    project, conn = kb
    sessions = [f"historical-{index}" for index in range(12)]
    result = workstreams.open_workstream(
        conn,
        title="Historically recurring lane",
        objective="Start a fresh trial",
        done_when="Post-open evidence arrives",
        scope_boundary="Automatic OPEN",
        next_step="Observe the next sessions",
        op_key="open:historical-recurrence",
        candidate_key="candidate:historical-recurrence",
        origin="auto",
        recurrence={"session_count": len(sessions), "session_ids": sessions},
        probation={
            "active": False,
            "opened_at": "2000-01-01 00:00:00",
            "eligible_session_target": 1,
        },
        project_path=project,
    )
    probation = result["payload"]["probation"]
    assert probation["active"] is True
    assert probation["eligible_session_count"] == 0
    assert probation["eligible_session_target"] == 10
    assert probation["opened_at"] != "2000-01-01 00:00:00"
    assert probation["recurrence"]["session_count"] == 12


def test_open_and_adopt_reject_priority_members(kb):
    project, conn = kb
    lane = _node(conn, "workstream", "Priority-safe lane")
    priority = _node(conn, "priority", "Scoped priority", workstream_id=lane)
    with pytest.raises(workstreams.WorkstreamValidationError, match="invalid OPEN member"):
        workstreams.open_workstream(
            conn,
            title="Invalid priority lane",
            objective="Never reparent priority",
            done_when="Rejected",
            scope_boundary="Priority scope",
            next_step="Stop",
            member_ids=[priority],
            op_key="open:priority-member",
            recurrence={"session_count": 2, "session_ids": ["s1", "s2"]},
            project_path=project,
        )
    with pytest.raises(workstreams.WorkstreamValidationError, match="invalid ADOPT node"):
        workstreams.adopt_nodes(
            conn,
            lane,
            [priority],
            op_key="adopt:priority-member",
            project_path=project,
        )
    assert db.get_node(conn, priority)["workstream_id"] == lane


def test_automatic_open_accepts_verified_shared_target_without_sessions(kb):
    project, conn = kb
    members = [
        _node(conn, "idea", "shared-target member a"),
        _node(conn, "progress", "shared-target member b"),
    ]
    target = _node(conn, "decision", "shared decision", status="canonical")
    for member in members:
        db.add_edge(conn, member, target, "advances")
    result = workstreams.open_workstream(
        conn,
        title="Structurally recurring lane",
        objective="Follow the shared decision",
        done_when="Both strands land",
        scope_boundary="Only the linked strands",
        next_step="Advance the decision",
        member_ids=members,
        op_key="open:shared-target",
        candidate_key="candidate:shared-target",
        origin="auto",
        recurrence={
            "session_count": 0,
            "session_ids": [],
            "shared_target_ids": [target],
            "shared_target_validated": True,
        },
        project_path=project,
    )
    assert result["state"] == "applied"
    assert result["payload"]["request"]["recurrence"]["session_count"] == 0
    assert result["payload"]["request"]["recurrence"]["shared_target_ids"] == [target]
    assert "recurred across 0 sessions" in result["receipt"]


def test_automatic_open_shared_target_ignores_stale_members(kb):
    project, conn = kb
    active_member = _node(conn, "idea", "active shared-target member")
    stale_member = _node(
        conn, "progress", "stale shared-target member", status="stale",
    )
    target = _node(conn, "decision", "shared decision", status="canonical")
    db.add_edge(conn, active_member, target, "advances")
    db.add_edge(conn, stale_member, target, "advances")

    with pytest.raises(
        workstreams.WorkstreamValidationError,
        match="automatic OPEN requires two-session recurrence or a verified shared target",
    ):
        workstreams.open_workstream(
            conn,
            title="Stale structural recurrence",
            objective="Ignore stale evidence",
            done_when="Only live strands qualify",
            scope_boundary="Shared-target OPEN",
            next_step="Wait for another active strand",
            member_ids=[active_member, stale_member],
            op_key="open:stale-shared-target",
            candidate_key="candidate:stale-shared-target",
            origin="auto",
            recurrence={
                "session_count": 0,
                "session_ids": [],
                "shared_target_ids": [target],
                "shared_target_validated": True,
            },
            project_path=project,
        )
    assert db.get_workstream_op(conn, "open:stale-shared-target") is None


def test_automatic_open_fails_closed_when_similarity_comparison_is_incomplete(kb):
    project, conn = kb
    _node(conn, "workstream", "Unembedded active lane")
    result = workstreams.open_workstream(
        conn,
        title="Cannot compare safely",
        objective="Avoid duplicate lanes",
        done_when="Comparison succeeds",
        scope_boundary="Automatic OPEN",
        next_step="Wait for embeddings",
        op_key="open:similarity-incomplete",
        candidate_key="candidate:similarity-incomplete",
        origin="auto",
        recurrence={"session_count": 2, "session_ids": ["s1", "s2"]},
        project_path=project,
    )
    assert result["state"] == "failed"
    assert result["payload"]["governor"] == "similarity_comparison_incomplete"
    assert conn.execute(
        "SELECT 1 FROM nodes WHERE kind='workstream' AND title='Cannot compare safely'"
    ).fetchone() is None


def test_automatic_open_rechecks_current_plan_inside_transaction(kb, monkeypatch):
    project, conn = kb
    monkeypatch.setattr(workstreams, "_auto_plan_is_current", lambda *_a, **_k: False)
    result = workstreams.open_workstream(
        conn,
        title="Stale automatic plan",
        objective="Must not apply",
        done_when="Never",
        scope_boundary="Stale candidate",
        next_step="Re-derive",
        op_key="open:stale-plan",
        candidate_key="candidate:stale-plan",
        origin="auto",
        recurrence={"session_count": 2, "session_ids": ["s1", "s2"]},
        project_path=project,
    )
    assert result["state"] == "failed"
    assert result["payload"]["governor"] == "auto_plan_stale"
    assert conn.execute(
        "SELECT 1 FROM nodes WHERE kind='workstream' AND title='Stale automatic plan'"
    ).fetchone() is None


def test_open_similarity_watch_pair_uses_neighbor_not_branch_parent(kb, monkeypatch):
    project, conn = kb
    parent = _node(conn, "workstream", "Parent lane")
    neighbor = _node(conn, "workstream", "Similar neighbor")
    monkeypatch.setattr(
        workstreams,
        "_similar_workstreams",
        lambda _conn, _text, *, embedding=None: (
            embedding, [{"id": neighbor, "title": "Similar neighbor", "similarity": 0.75}],
        ),
    )
    result = workstreams.open_workstream(
        conn,
        title="Allowed adjacent lane", objective="x", done_when="y",
        scope_boundary="z", next_step="n", op_key="open:watch",
        branched_from=parent, similarity_override=True,
        recurrence={"session_count": 2, "session_ids": ["a", "b"]},
        project_path=project,
    )
    assert result["payload"]["watch_pair"] == [result["workstream_id"], neighbor]
    assert result["payload"]["watch_pair"][1] != parent
    assert result["payload"]["probation"]["eligible_session_target"] == 10
    assert result["payload"]["probation"]["eligible_session_count"] == 2


def test_auto_open_below_ambiguity_band_watches_nearest_without_merging(kb, monkeypatch):
    project, conn = kb
    neighbor = _node(conn, "workstream", "Low-similarity neighbor")
    valid_embedding = b"\0" * (384 * 4)
    conn.execute("UPDATE nodes SET embedding=? WHERE id=?", (valid_embedding, neighbor))
    conn.commit()
    monkeypatch.setattr(
        workstreams,
        "_similar_workstreams",
        lambda _conn, _text, *, embedding=None: (
            valid_embedding,
            [{"id": neighbor, "title": "Low-similarity neighbor", "similarity": 0.65}],
        ),
    )
    result = workstreams.open_workstream(
        conn,
        title="Auto probation lane", objective="x", done_when="y",
        scope_boundary="z", next_step="n", op_key="open:auto-watch",
        origin="auto",
        recurrence={
            "session_count": 2, "session_ids": ["auto-a", "auto-b"],
            "since": "2026-07-01",
        },
        project_path=project,
    )
    assert result["payload"]["watch_pair"] == [result["workstream_id"], neighbor]
    assert result["payload"]["watch_similarity"] == pytest.approx(0.65)
    assert conn.execute(
        "SELECT 1 FROM edges WHERE relation = 'merged_into' "
        "AND src IN (?, ?) AND dst IN (?, ?)",
        (result["workstream_id"], neighbor, result["workstream_id"], neighbor),
    ).fetchone() is None


def test_open_recomputes_recent_lane_cap_and_manual_force_is_audited(kb):
    project, conn = kb
    for index in range(workstreams.RECENT_ACTIVE_LANE_CAP):
        lane = _node(conn, "workstream", f"recent lane {index}")
        _node(conn, "fact", f"recent member {index}", workstream_id=lane)
    blocked = _open(
        project, conn, op_key="open:cap-blocked", title="Cap blocked lane",
    )
    assert blocked["state"] == "failed" and blocked["error_code"] == "blocked"
    assert blocked["payload"]["governor"] == "recent_active_lane_cap"
    assert conn.execute(
        "SELECT 1 FROM nodes WHERE kind='workstream' AND title='Cap blocked lane'"
    ).fetchone() is None

    forced = workstreams.open_workstream(
        conn,
        title="Forced lane", objective="x", done_when="y", scope_boundary="z",
        next_step="n", op_key="open:cap-forced", force=True,
        recurrence={"session_count": 2, "session_ids": ["a", "b"]},
        project_path=project,
    )
    assert forced["state"] == "applied" and forced["forced"] is True
    assert forced["payload"]["governor_forced"] is True
    assert db.get_workstream_op(conn, "open:cap-forced")["forced"] == 1

    with pytest.raises(workstreams.WorkstreamValidationError):
        workstreams.open_workstream(
            conn,
            title="Auto force forbidden", objective="x", done_when="y",
            scope_boundary="z", next_step="n", op_key="open:auto-force",
            origin="auto", force=True,
            recurrence={"session_count": 2, "session_ids": ["a", "b"]},
            project_path=project,
        )


def test_open_does_not_steal_member_from_active_lane(kb):
    project, conn = kb
    owner = _node(conn, "workstream", "Existing owner")
    member = _node(conn, "idea", "owned member", workstream_id=owner)
    with pytest.raises(workstreams.WorkstreamConflictError, match="explicit repoint"):
        _open(
            project, conn, op_key="open:foreign-member", title="Would steal",
            members=[member],
        )
    assert db.get_node(conn, member)["workstream_id"] == owner
    assert conn.execute(
        "SELECT 1 FROM nodes WHERE kind='workstream' AND title='Would steal'"
    ).fetchone() is None


def test_close_moot_repoint_focus_priority_and_reopen(kb):
    project, conn = kb
    opened = _open(project, conn)
    wid = opened["workstream_id"]
    target = _node(conn, "workstream", "Target lane")
    member = _node(conn, "idea", "moot member", workstream_id=wid)
    edge_feeder = _node(conn, "fact", "repoint me")
    old_edge = db.add_edge_nc(conn, edge_feeder, wid, "advances", created_by="test")
    conn.commit()
    priority = priorities.add_priority(
        conn, "Keep closure exact", workstream_id=wid, rank=1,
    )["id"]
    floating_priority = priorities.add_priority(
        conn, "Preserve floating order", workstream_id=wid,
    )["id"]

    preflight = workstreams.close_preflight(conn, wid)
    assert {row["id"] for row in preflight["feeders"]} == {member, edge_feeder}
    result = workstreams.close_workstream(
        conn,
        wid,
        outcome="completed",
        reason="all acceptance checks passed",
        dispositions={member: "moot", edge_feeder: {"action": "repoint", "target": target}},
        op_key="close:one",
        preflight_token=preflight["token"],
        project_path=project,
    )
    assert result["workstream_id"] == wid
    assert db.get_node(conn, wid)["status"] == "stale"
    focus = db.get_focus_row(conn, wid)
    assert focus["score"] == 0.0 and focus["pinned"] == 0
    assert db.get_node(conn, priority)["status"] == "stale"
    assert db.get_node(conn, floating_priority)["status"] == "stale"
    snapshots = {item["id"]: item for item in result["payload"]["priority_snapshots"]}
    assert snapshots[priority]["status"] == "canonical"
    assert snapshots[priority]["workstream_id"] == wid
    assert snapshots[priority]["rank"] == 1
    assert snapshots[priority]["retired_at"] is None
    assert snapshots[floating_priority]["rank"] is None
    assert snapshots[floating_priority]["retired_at"] is None
    assert conn.execute("SELECT status FROM edges WHERE id = ?", (old_edge,)).fetchone()["status"] == "tombstoned"
    replacement = conn.execute(
        "SELECT id, created_by FROM edges WHERE src = ? AND dst = ? AND relation = 'advances'",
        (edge_feeder, target),
    ).fetchone()
    assert replacement is not None and replacement["created_by"] == "lifecycle:close"
    moot = conn.execute(
        "SELECT id FROM edges WHERE src = ? AND dst = ? AND relation = 'resolves'",
        (wid, member),
    ).fetchone()
    assert moot is not None
    assert set(result["payload"]["feeder_disposition_edge_ids"]) >= {
        int(old_edge), int(replacement["id"]), int(moot["id"]),
    }
    assert "<!--latch:epilogue:close:one-->" in db.get_node(conn, wid)["body"]
    assert feeders.open_feeder_snapshot(conn, wid) == []

    reopened = workstreams.reopen_workstream(
        conn, wid, reason="follow-up acceptance gap", op_key="reopen:one",
        project_path=project,
    )
    assert reopened["state"] == "applied"
    assert db.get_node(conn, wid)["status"] == "staging"
    assert "<!--latch:epilogue:close:one-->" in db.get_node(conn, wid)["body"]
    assert "Reopened: follow-up acceptance gap." in db.get_node(conn, wid)["body"]


def test_close_stale_preflight_and_mutation_failure_are_non_partial(kb):
    project, conn = kb
    wid = _open(project, conn, op_key="open:stale", title="Stale close")["workstream_id"]
    feeder = _node(conn, "idea", "must disposition", workstream_id=wid)

    stale = workstreams.close_workstream(
        conn, wid, outcome="completed", reason="done", dispositions={feeder: "moot"},
        op_key="close:stale", preflight_token="not-the-snapshot", project_path=project,
    )
    assert stale["state"] == "failed" and stale["error_code"] == "preflight_stale"
    assert db.get_node(conn, wid)["status"] != "stale"
    assert conn.execute(
        "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND relation = 'resolves'",
        (wid, feeder),
    ).fetchone() is None

    second = _node(conn, "fact", "bad repoint")
    db.add_edge(conn, second, wid, "advances")
    with pytest.raises(workstreams.WorkstreamValidationError):
        workstreams.close_workstream(
            conn, wid, outcome="completed", reason="done",
            dispositions={feeder: "moot", second: {"action": "repoint", "target": 999999}},
            op_key="close:rollback", project_path=project,
        )
    assert db.get_node(conn, wid)["status"] != "stale"
    assert conn.execute(
        "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND relation = 'resolves'",
        (wid, feeder),
    ).fetchone() is None
    assert db.get_workstream_op(conn, "close:rollback") is None


def test_close_captures_implicit_preflight_after_writer_lock(kb, monkeypatch):
    project, conn = kb
    workstream_id = _node(conn, "workstream", "Raced close lane")
    feeder = _node(conn, "fact", "late feeder")
    original_writer_lock = workstreams.lockfile.writer_lock

    @contextmanager
    def mutate_before_acquire(project_path, *args, **kwargs):
        db.add_edge(conn, feeder, workstream_id, "advances")
        with original_writer_lock(project_path, *args, **kwargs):
            yield

    monkeypatch.setattr(workstreams.lockfile, "writer_lock", mutate_before_acquire)
    result = workstreams.close_workstream(
        conn,
        workstream_id,
        outcome="completed",
        reason="Attempted close",
        op_key="close:locked-preflight",
        project_path=project,
    )
    assert result["state"] == "failed"
    assert result["error_code"] == "open_feeders"
    assert db.get_node(conn, workstream_id)["status"] != "stale"


@pytest.mark.parametrize("priority_status", ["canonical", "staging"])
def test_close_fails_closed_on_legacy_priority_without_order_metadata(
    kb, priority_status,
):
    project, conn = kb
    workstream_id = _open(
        project, conn, op_key="open:malformed-close", title="Malformed close lane",
    )["workstream_id"]
    malformed = _node(
        conn, "priority", "Legacy priority without ordering",
        status=priority_status, workstream_id=workstream_id,
    )
    assert conn.execute(
        "SELECT 1 FROM priority_order WHERE node_id = ?", (malformed,),
    ).fetchone() is None

    message = rf"priority nodes missing priority_order metadata: \[{malformed}\]"
    with pytest.raises(workstreams.WorkstreamValidationError, match=message):
        workstreams.close_preflight(conn, workstream_id)
    with pytest.raises(workstreams.WorkstreamValidationError, match=message):
        workstreams.close_workstream(
            conn, workstream_id, outcome="completed", reason="would omit priority",
            op_key="close:malformed-priority", project_path=project,
        )

    assert db.get_node(conn, workstream_id)["status"] != "stale"
    assert db.get_node(conn, malformed)["status"] == priority_status
    assert db.get_workstream_op(conn, "close:malformed-priority") is None


def test_force_close_records_unhandled_feeders(kb):
    project, conn = kb
    wid = _open(project, conn, op_key="open:force", title="Force close")["workstream_id"]
    feeder = _node(conn, "idea", "left open", workstream_id=wid)
    result = workstreams.close_workstream(
        conn, wid, outcome="abandoned", reason="explicit override", dispositions={},
        op_key="close:force", force=True, project_path=project,
    )
    assert result["forced"] is True
    assert result["payload"]["unhandled_feeder_ids"] == [feeder]


def test_auto_probation_abandonment_releases_mixed_kind_open_members(kb, monkeypatch):
    project, conn = kb
    opened_at = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    idea = _node(conn, "idea", "probation idea")
    fact = _node(conn, "fact", "probation fact")
    progress = _node(conn, "progress", "probation progress")
    monkeypatch.setattr(db, "_now", lambda: opened_at)
    opened = workstreams.open_workstream(
        conn,
        title="Auto probation lane",
        objective="Validate the lane",
        done_when="The probation window has evidence",
        scope_boundary="Only the automatic trial",
        next_step="Observe contacts",
        member_ids=[idea, fact, progress],
        op_key="open:auto-probation-mixed",
        origin="auto",
        recurrence={
            "session_count": 2,
            "session_ids": ["probation-a", "probation-b"],
            "since": "2026-07-01",
        },
        probation={"active": False, "opened_at": "forged", "eligible_session_target": 2},
        project_path=project,
    )
    workstream_id = opened["workstream_id"]
    assert opened["payload"]["probation"] == {
        "active": True,
        "opened_at": opened_at,
        "eligible_session_target": 10,
        "eligible_session_count": 0,
        "recurrence": opened["payload"]["request"]["recurrence"],
    }
    _seed_post_open_sessions(conn, opened_at=opened_at, count=10)

    normal = workstreams.close_preflight(conn, workstream_id)
    assert {item["id"] for item in normal["feeders"]} == {idea}
    preflight = workstreams.close_preflight(
        conn, workstream_id, origin="auto", outcome="abandoned",
    )
    assert {item["id"] for item in preflight["feeders"]} == {
        idea, fact, progress,
    }
    assert all(item.get("release_only") for item in preflight["feeders"])

    result = workstreams.close_workstream(
        conn,
        workstream_id,
        outcome="abandoned",
        reason="No contacts during automatic probation",
        dispositions={node_id: "release" for node_id in (idea, fact, progress)},
        op_key="close:auto-probation-mixed",
        origin="auto",
        preflight_token=preflight["token"],
        project_path=project,
    )
    assert result["state"] == "applied"
    assert result["payload"]["released_member_ids"] == sorted(
        [idea, fact, progress]
    )
    assert all(
        db.get_node(conn, node_id)["workstream_id"] is None
        for node_id in (idea, fact, progress)
    )


def test_auto_probation_assigned_members_are_release_only(kb, monkeypatch):
    project, conn = kb
    opened_at = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    fact = _node(conn, "fact", "release-only fact")
    monkeypatch.setattr(db, "_now", lambda: opened_at)
    opened = workstreams.open_workstream(
        conn,
        title="Release-only probation lane",
        objective="Validate release rail",
        done_when="The close rail is safe",
        scope_boundary="Automatic probation",
        next_step="Run close preflight",
        member_ids=[fact],
        op_key="open:auto-probation-release-only",
        origin="auto",
        recurrence={"session_count": 2, "session_ids": ["a", "b"]},
        probation={"active": False, "opened_at": "forged", "eligible_session_target": 2},
        project_path=project,
    )
    _seed_post_open_sessions(conn, opened_at=opened_at, count=10)
    with pytest.raises(workstreams.WorkstreamValidationError, match="only release"):
        workstreams.close_workstream(
            conn,
            opened["workstream_id"],
            outcome="abandoned",
            reason="No contacts",
            dispositions={fact: "moot"},
            op_key="close:auto-probation-not-release",
            origin="auto",
            project_path=project,
        )
    assert db.get_node(conn, fact)["workstream_id"] == opened["workstream_id"]

    _seed_post_open_sessions(
        conn,
        opened_at=opened_at,
        count=1,
        contact_workstream_id=opened["workstream_id"],
    )
    state = workstreams._auto_open_probation_state(conn, opened["workstream_id"])
    assert state["graduated"] is True
    assert state["release_ready"] is False
    with pytest.raises(workstreams.WorkstreamValidationError, match="non-open feeder"):
        workstreams.close_workstream(
            conn,
            opened["workstream_id"],
            outcome="abandoned",
            reason="No longer eligible",
            dispositions={fact: "release"},
            op_key="close:auto-probation-graduated",
            origin="auto",
            candidate_key="candidate:graduated",
            project_path=project,
        )


def test_adopt_batch_relations_and_auto_guard(kb):
    project, conn = kb
    wid = _node(conn, "workstream", "Adoption lane")
    idea = _node(conn, "idea", "idea")
    fact = _node(conn, "fact", "fact")
    result = workstreams.adopt_nodes(
        conn, wid, [idea, fact], op_key="adopt:one",
        relations={idea: "motivates", fact: "depends_on"},
        evidence={"forward_looking": True, "trigger": "declared_intent"},
        project_path=project,
    )
    assert result["payload"]["assigned_member_ids"] == [idea, fact]
    assert {db.get_node(conn, idea)["workstream_id"], db.get_node(conn, fact)["workstream_id"]} == {wid}
    assert {
        row["relation"] for row in conn.execute(
            "SELECT relation FROM edges WHERE src IN (?, ?) AND dst = ?", (idea, fact, wid),
        ).fetchall()
    } == {"motivates", "depends_on"}
    retry = workstreams.adopt_nodes(
        conn, wid, [idea, fact], op_key="adopt:one",
        relations={idea: "motivates", fact: "depends_on"},
        evidence={"forward_looking": True, "trigger": "declared_intent"},
        project_path=project,
    )
    assert retry["idempotent"] is True

    orphan = _node(conn, "idea", "orphan")
    with pytest.raises(workstreams.WorkstreamValidationError):
        workstreams.adopt_nodes(
            conn, wid, [orphan], op_key="adopt:auto", origin="auto",
            evidence={"forward_looking": True, "trigger": "declared_intent"},
            project_path=project,
        )
    assert db.get_node(conn, orphan)["workstream_id"] is None


def test_adopt_captures_implicit_preflight_after_writer_lock(kb, monkeypatch):
    project, conn = kb
    wid = _node(conn, "workstream", "Locked adoption lane")
    idea = _node(conn, "idea", "raced idea")
    original_writer_lock = workstreams.lockfile.writer_lock

    @contextmanager
    def mutate_before_acquire(project_path, *args, **kwargs):
        conn.execute(
            "UPDATE nodes SET updated_at = '2040-01-01 00:00:00' WHERE id = ?",
            (idea,),
        )
        conn.commit()
        with original_writer_lock(project_path, *args, **kwargs):
            yield

    monkeypatch.setattr(workstreams.lockfile, "writer_lock", mutate_before_acquire)
    result = workstreams.adopt_nodes(
        conn,
        wid,
        [idea],
        op_key="adopt:locked-preflight",
        project_path=project,
    )
    assert result["state"] == "applied"
    assert db.get_node(conn, idea)["workstream_id"] == wid


def test_applied_receipts_surface_once(kb):
    project, conn = kb
    result = _open(project, conn, op_key="open:receipt", title="Receipt lane")
    pending = lifecycle_receipts.pending_receipts(conn)
    assert any(item["op_key"] == "open:receipt" for item in pending)
    surfaced = lifecycle_receipts.surface_pending(conn, session_id="surface")
    assert result["receipt"] in surfaced
    assert lifecycle_receipts.surface_pending(conn, session_id="surface") == []


def _merge_fixture(project, conn):
    source = _node(conn, "workstream", "Source lane")
    absorber = _node(conn, "workstream", "Absorber lane")
    member_a = _node(conn, "idea", "source member", workstream_id=source)
    member_b = _node(conn, "fact", "source fact", workstream_id=source)
    feeder = _node(conn, "fact", "portable feeder")
    portable_edge = db.add_edge_nc(conn, feeder, source, "advances", created_by="test")
    unknown_node = _node(conn, "fact", "unknown relation")
    unknown_edge = db.add_edge_nc(conn, unknown_node, source, "related_to", created_by="test")
    db.set_focus_row_nc(
        conn, source, score=5.0, set_at=db._now(), set_by="test", pinned=True,
    )
    db.set_focus_row_nc(
        conn, absorber, score=3.0, set_at=db._now(), set_by="test", pinned=False,
    )
    db.recompute_focus_ranks_nc(conn)
    conn.commit()
    return {
        "source": source, "absorber": absorber, "members": [member_a, member_b],
        "feeder": feeder, "portable_edge": portable_edge,
        "unknown_node": unknown_node, "unknown_edge": unknown_edge,
    }


def test_merge_unmerge_full_round_trip(kb, monkeypatch):
    project, conn = kb
    monkeypatch.setattr(priorities, "MAX_ACTIVE", 2)
    lane = _merge_fixture(project, conn)
    source, absorber = lane["source"], lane["absorber"]
    absorber_priority = priorities.add_priority(
        conn, "absorber existing", workstream_id=absorber,
    )["id"]
    source_p1 = priorities.add_priority(conn, "source p1", workstream_id=source)["id"]
    source_p2 = priorities.add_priority(conn, "source p2", workstream_id=source)["id"]
    source_priority_order = [
        item["id"] for item in priorities.list_priorities(conn, workstream_id=source)
    ]
    for index, node_id in enumerate([source, absorber, *lane["members"]], start=1):
        conn.execute(
            "UPDATE nodes SET updated_at = ?, updated_by = ? WHERE id = ?",
            (f"2001-01-0{index} 00:00:00", f"pre-merge-{index}", node_id),
        )
    conn.commit()
    node_metadata = {
        node_id: {
            "updated_at": db.get_node(conn, node_id)["updated_at"],
            "updated_by": db.get_node(conn, node_id)["updated_by"],
        }
        for node_id in [source, absorber, *lane["members"]]
    }
    source_focus = db.get_focus_row(conn, source)
    absorber_focus = db.get_focus_row(conn, absorber)
    source_body = db.get_node(conn, source)["body"]
    absorber_body = db.get_node(conn, absorber)["body"]

    preflight = workstreams.merge_preflight(conn, source, absorber)
    assert [row["id"] for row in preflight["unknown_inbound_edges"]] == [lane["unknown_edge"]]
    merged = workstreams.merge_workstreams(
        conn,
        source,
        absorber,
        op_key="merge:one",
        dispositions={lane["unknown_edge"]: "rehome"},
        evidence={"coactive_sessions": 3, "window_sessions": 5},
        preflight_token=preflight["token"],
        project_path=project,
    )
    assert merged["state"] == "applied"
    assert workstreams.resolve_active(conn, source)["active_id"] == absorber
    assert db.get_node(conn, source)["status"] == "stale"
    assert db.get_node(conn, source)["body"] == source_body
    assert all(db.get_node(conn, node_id)["workstream_id"] == absorber for node_id in lane["members"])
    assert conn.execute(
        "SELECT status FROM edges WHERE id = ?", (lane["portable_edge"],),
    ).fetchone()["status"] == "tombstoned"
    assert conn.execute(
        "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND relation = 'advances' AND status = 'active'",
        (lane["feeder"], absorber),
    ).fetchone() is not None
    assert conn.execute(
        "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND relation = 'supersedes'",
        (absorber, source),
    ).fetchone() is None
    assert db.get_focus_row(conn, source) is None
    merged_focus = db.get_focus_row(conn, absorber)
    assert merged_focus["pinned"] == 1 and merged_focus["score"] > 4.9
    assert db.get_node(conn, source_p1)["status"] == "stale"
    assert db.get_node(conn, source_p2)["status"] == "stale"
    assert len(merged["payload"]["readded_priority_ids"]) == 1
    assert len(merged["payload"]["overflow_retired_priority_ids"]) == 1
    copied = merged["payload"]["readded_priority_ids"][0]
    assert priorities.list_priorities(conn, workstream_id=absorber)[-1]["id"] == copied
    assert db.get_node(conn, absorber)["body"].count("<!--latch:op:merge:one-->") == 1
    assert f"receipt #{merged['operation_id']}" in merged["receipt"]
    assert Path(merged["payload"]["backup_path"]).exists()

    reversed_result = workstreams.unmerge_workstreams(
        conn, "merge:one", op_key="unmerge:one", project_path=project,
    )
    assert reversed_result["state"] == "applied"
    assert workstreams.resolve_active(conn, source)["state"] == "active"
    assert db.get_node(conn, source)["body"] == source_body
    assert db.get_node(conn, absorber)["body"] == absorber_body
    assert all(db.get_node(conn, node_id)["workstream_id"] == source for node_id in lane["members"])
    assert conn.execute(
        "SELECT status FROM edges WHERE id = ?", (lane["portable_edge"],),
    ).fetchone()["status"] == "active"
    for edge_id in merged["payload"]["rehomed_edge_ids"]:
        assert conn.execute(
            "SELECT status FROM edges WHERE id = ?", (edge_id,),
        ).fetchone()["status"] == "tombstoned"
    assert db.get_focus_row(conn, source) == source_focus
    assert db.get_focus_row(conn, absorber) == absorber_focus
    assert [
        item["id"] for item in priorities.list_priorities(conn, workstream_id=source)
    ] == source_priority_order
    assert db.get_node(conn, source_p1)["status"] == "canonical"
    assert db.get_node(conn, source_p2)["status"] == "canonical"
    assert db.get_node(conn, copied) is None
    assert conn.execute(
        "SELECT 1 FROM priority_order WHERE node_id = ?", (copied,),
    ).fetchone() is None
    assert db.get_node(conn, absorber_priority)["status"] == "canonical"
    assert {
        node_id: {
            "updated_at": db.get_node(conn, node_id)["updated_at"],
            "updated_by": db.get_node(conn, node_id)["updated_by"],
        }
        for node_id in [source, absorber, *lane["members"]]
    } == node_metadata


def test_merge_captures_implicit_preflight_after_writer_lock(kb, monkeypatch):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    raced_member = lane["members"][0]
    original_writer_lock = workstreams.lockfile.writer_lock

    @contextmanager
    def mutate_before_acquire(project_path, *args, **kwargs):
        conn.execute(
            "UPDATE nodes SET status = 'canonical', "
            "updated_at = '2040-01-01 00:00:00' WHERE id = ?",
            (raced_member,),
        )
        conn.commit()
        with original_writer_lock(project_path, *args, **kwargs):
            yield

    monkeypatch.setattr(workstreams.lockfile, "writer_lock", mutate_before_acquire)
    result = workstreams.merge_workstreams(
        conn,
        lane["source"],
        lane["absorber"],
        op_key="merge:locked-preflight",
        dispositions={lane["unknown_edge"]: "preserve"},
        project_path=project,
    )
    assert result["state"] == "applied"
    assert db.get_node(conn, raced_member)["workstream_id"] == lane["absorber"]


def test_merge_unknown_relations_require_disposition_and_auto_cannot_force(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    blocked = workstreams.merge_workstreams(
        conn, lane["source"], lane["absorber"], op_key="merge:blocked",
        project_path=project,
    )
    assert blocked["state"] == "failed" and blocked["error_code"] == "blocked"
    assert db.get_node(conn, lane["source"])["status"] != "stale"
    assert all(
        db.get_node(conn, node_id)["workstream_id"] == lane["source"]
        for node_id in lane["members"]
    )
    with pytest.raises(workstreams.WorkstreamValidationError):
        workstreams.merge_workstreams(
            conn, lane["source"], lane["absorber"], op_key="merge:auto-force",
            origin="auto", force=True, project_path=project,
        )


@pytest.mark.parametrize("priority_status", ["canonical", "staging"])
@pytest.mark.parametrize("malformed_side", ["source", "absorber"])
def test_merge_fails_closed_on_legacy_priority_without_order_metadata(
    kb, malformed_side, priority_status,
):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    malformed = _node(
        conn, "priority", f"Malformed {malformed_side} priority",
        status=priority_status, workstream_id=lane[malformed_side],
    )
    assert conn.execute(
        "SELECT 1 FROM priority_order WHERE node_id = ?", (malformed,),
    ).fetchone() is None

    message = rf"priority nodes missing priority_order metadata: \[{malformed}\]"
    with pytest.raises(workstreams.WorkstreamValidationError, match=message):
        workstreams.merge_preflight(conn, lane["source"], lane["absorber"])
    with pytest.raises(workstreams.WorkstreamValidationError, match=message):
        workstreams.merge_workstreams(
            conn, lane["source"], lane["absorber"],
            op_key=f"merge:malformed-{malformed_side}", project_path=project,
        )

    assert db.get_node(conn, lane["source"])["status"] != "stale"
    assert all(
        db.get_node(conn, node_id)["workstream_id"] == lane["source"]
        for node_id in lane["members"]
    )
    assert db.get_node(conn, malformed)["status"] == priority_status
    assert db.get_workstream_op(conn, f"merge:malformed-{malformed_side}") is None


def test_merge_failure_injection_rolls_back_every_write(kb, monkeypatch):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    source, absorber = lane["source"], lane["absorber"]

    def fail(label):
        if label == "merge_after_edges":
            raise RuntimeError("injected")

    monkeypatch.setattr(workstreams, "_failure_point", fail)
    with pytest.raises(RuntimeError, match="injected"):
        workstreams.merge_workstreams(
            conn, source, absorber, op_key="merge:injected",
            dispositions={lane["unknown_edge"]: "rehome"}, project_path=project,
        )
    assert db.get_node(conn, source)["status"] != "stale"
    assert all(db.get_node(conn, node_id)["workstream_id"] == source for node_id in lane["members"])
    assert conn.execute(
        "SELECT status FROM edges WHERE id = ?", (lane["portable_edge"],),
    ).fetchone()["status"] == "active"
    assert conn.execute(
        "SELECT 1 FROM edges WHERE src = ? AND dst = ? AND relation = 'merged_into' AND status = 'active'",
        (source, absorber),
    ).fetchone() is None
    assert db.get_workstream_op(conn, "merge:injected") is None


def test_unmerge_failure_injection_rolls_back_replay(kb, monkeypatch):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    merged = workstreams.merge_workstreams(
        conn, lane["source"], lane["absorber"], op_key="merge:reverse-injected",
        dispositions={lane["unknown_edge"]: "rehome"}, project_path=project,
    )

    def fail(label):
        if label == "unmerge_after_edges":
            raise RuntimeError("reverse injected")

    monkeypatch.setattr(workstreams, "_failure_point", fail)
    with pytest.raises(RuntimeError, match="reverse injected"):
        workstreams.unmerge_workstreams(
            conn, "merge:reverse-injected", op_key="unmerge:injected",
            project_path=project,
        )
    assert db.get_node(conn, lane["source"])["status"] == "stale"
    assert all(
        db.get_node(conn, node_id)["workstream_id"] == lane["absorber"]
        for node_id in lane["members"]
    )
    assert conn.execute(
        "SELECT status FROM edges WHERE id = ?", (merged["payload"]["merge_edge_id"],),
    ).fetchone()["status"] == "active"
    assert db.get_workstream_op(conn, "unmerge:injected") is None


def test_unmerge_fails_closed_on_drift(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    merged = workstreams.merge_workstreams(
        conn, lane["source"], lane["absorber"], op_key="merge:drift",
        dispositions={lane["unknown_edge"]: "preserve"}, project_path=project,
    )
    moved = merged["payload"]["repointed_member_ids"][0]
    db.set_node_workstream(conn, [moved], None)
    result = workstreams.unmerge_workstreams(
        conn, "merge:drift", op_key="unmerge:drift", project_path=project,
    )
    assert result["state"] == "failed" and result["error_code"] == "preflight_stale"
    assert db.get_node(conn, lane["source"])["status"] == "stale"
    assert db.get_node(conn, moved)["workstream_id"] is None
    assert conn.execute(
        "SELECT status FROM edges WHERE id = ?", (merged["payload"]["merge_edge_id"],),
    ).fetchone()["status"] == "active"


def test_unmerge_ignores_informational_focus_rank_drift(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    merged = workstreams.merge_workstreams(
        conn,
        lane["source"],
        lane["absorber"],
        op_key="merge:focus-rank",
        dispositions={lane["unknown_edge"]: "preserve"},
        project_path=project,
    )
    conn.execute(
        "UPDATE focus SET rank = rank + 100 WHERE workstream_id = ?",
        (lane["absorber"],),
    )
    conn.commit()

    result = workstreams.unmerge_workstreams(
        conn,
        "merge:focus-rank",
        op_key="unmerge:focus-rank",
        project_path=project,
    )
    assert result["state"] == "applied"
    assert db.get_node(conn, lane["source"])["status"] != "stale"
    assert conn.execute(
        "SELECT status FROM edges WHERE id = ?",
        (merged["payload"]["merge_edge_id"],),
    ).fetchone()["status"] == "tombstoned"


def test_unmerge_fails_closed_when_moved_member_metadata_drifted(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    workstreams.merge_workstreams(
        conn,
        lane["source"],
        lane["absorber"],
        op_key="merge:member-metadata",
        dispositions={lane["unknown_edge"]: "preserve"},
        project_path=project,
    )
    moved = lane["members"][0]
    conn.execute(
        "UPDATE nodes SET updated_at = '2040-01-01 00:00:00', "
        "updated_by = 'later-editor' WHERE id = ?",
        (moved,),
    )
    conn.commit()

    result = workstreams.unmerge_workstreams(
        conn,
        "merge:member-metadata",
        op_key="unmerge:member-metadata",
        project_path=project,
    )
    assert result["state"] == "failed"
    assert f"member_metadata:{moved}" in result["drift"]
    assert db.get_node(conn, moved)["workstream_id"] == lane["absorber"]


def test_unmerge_preserves_edited_merge_created_priority(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    priorities.add_priority(conn, "source priority", workstream_id=lane["source"])
    merged = workstreams.merge_workstreams(
        conn,
        lane["source"],
        lane["absorber"],
        op_key="merge:edited-copy",
        dispositions={lane["unknown_edge"]: "preserve"},
        project_path=project,
    )
    copied = merged["payload"]["readded_priority_ids"][0]
    db.update_node(conn, copied, body="post-merge user edit")
    result = workstreams.unmerge_workstreams(
        conn,
        "merge:edited-copy",
        op_key="unmerge:edited-copy",
        project_path=project,
    )
    assert result["state"] == "failed"
    assert f"copied_priority:{copied}" in result["drift"]
    assert db.get_node(conn, copied)["body"] == "post-merge user edit"


def test_unmerge_preserves_referenced_merge_created_priority(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    priorities.add_priority(conn, "source priority", workstream_id=lane["source"])
    merged = workstreams.merge_workstreams(
        conn,
        lane["source"],
        lane["absorber"],
        op_key="merge:referenced-copy",
        dispositions={lane["unknown_edge"]: "preserve"},
        project_path=project,
    )
    copied = merged["payload"]["readded_priority_ids"][0]
    evidence = _node(conn, "fact", "copy evidence")
    db.add_edge(conn, copied, evidence, "related_to")
    result = workstreams.unmerge_workstreams(
        conn,
        "merge:referenced-copy",
        op_key="unmerge:referenced-copy",
        project_path=project,
    )
    assert result["state"] == "failed"
    assert f"copied_priority_reference:{copied}:edge" in result["drift"]
    assert db.get_node(conn, copied) is not None


def test_unmerge_appends_correction_if_merge_line_was_evicted(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    merged = workstreams.merge_workstreams(
        conn, lane["source"], lane["absorber"], op_key="merge:evicted",
        dispositions={lane["unknown_edge"]: "preserve"}, project_path=project,
    )
    body = db.get_node(conn, lane["absorber"])["body"]
    for i in range(4):
        body = rolling.apply(body, f"later entry {i}", date=f"2026-07-{10 + i}")
    assert "<!--latch:op:merge:evicted-->" not in body
    db.update_node(conn, lane["absorber"], body=body)

    result = workstreams.unmerge_workstreams(
        conn, "merge:evicted", op_key="unmerge:evicted", project_path=project,
    )
    assert result["payload"]["rolling_line_removed"] is False
    restored = db.get_node(conn, lane["absorber"])["body"]
    assert "Correction: merge of workstream" in restored
    assert "<!--latch:op:unmerge:evicted-->" in restored


def test_integrity_reconciler_repairs_close_status_drift_with_backup(kb):
    project, conn = kb
    opened = _open(project, conn, op_key="open:integrity-close")
    workstream_id = opened["workstream_id"]
    closed = workstreams.close_workstream(
        conn,
        workstream_id,
        outcome="completed",
        reason="Acceptance criteria passed",
        op_key="close:integrity",
        project_path=project,
    )
    assert closed["state"] == "applied"
    db.update_node(conn, workstream_id, status="canonical")

    report = workstreams.reconcile_lifecycle_integrity(
        conn, project_path=project, emit_log=False,
    )
    assert report["ok"] is True
    assert report["repaired_status_ids"] == [workstream_id]
    assert report["repaired_edge_ids"] == []
    assert report["repair_count"] == 1
    assert Path(report["backup_path"]).exists()
    assert db.get_node(conn, workstream_id)["status"] == "stale"

    clean = workstreams.reconcile_lifecycle_integrity(
        conn, project_path=project, emit_log=False,
    )
    assert clean["repair_count"] == 0
    assert clean["backup_path"] is None


def test_integrity_reconciler_repairs_unmerge_status_and_edge_drift(kb):
    project, conn = kb
    lane = _merge_fixture(project, conn)
    merged = workstreams.merge_workstreams(
        conn,
        lane["source"],
        lane["absorber"],
        op_key="merge:integrity-unmerge",
        dispositions={lane["unknown_edge"]: "preserve"},
        project_path=project,
    )
    workstreams.unmerge_workstreams(
        conn,
        "merge:integrity-unmerge",
        op_key="unmerge:integrity",
        project_path=project,
    )
    merge_edge_id = merged["payload"]["merge_edge_id"]
    db.update_node(conn, lane["source"], status="stale")
    conn.execute(
        "UPDATE edges SET status='active' WHERE id=?", (merge_edge_id,),
    )
    conn.commit()

    report = workstreams.reconcile_lifecycle_integrity(
        conn, project_path=project, emit_log=False,
    )
    assert report["ok"] is True
    assert report["repaired_status_ids"] == [lane["source"]]
    assert report["repaired_edge_ids"] == [merge_edge_id]
    assert report["repair_count"] == 2
    assert Path(report["backup_path"]).exists()
    assert db.get_node(conn, lane["source"])["status"] == "staging"
    edge = conn.execute(
        "SELECT status FROM edges WHERE id=?", (merge_edge_id,),
    ).fetchone()
    assert edge["status"] == "tombstoned"


def test_integrity_reconciler_leaves_legacy_lane_unmanaged_without_receipts(kb):
    project, conn = kb
    legacy_id = _node(conn, "workstream", "Legacy unmanaged lane")
    before = db.get_node(conn, legacy_id)
    before_ops = conn.execute("SELECT COUNT(*) n FROM workstream_ops").fetchone()["n"]
    before_events = conn.execute(
        "SELECT COUNT(*) n FROM workstream_op_events"
    ).fetchone()["n"]

    report = workstreams.reconcile_lifecycle_integrity(
        conn, project_path=project, emit_log=False,
    )
    assert report["ok"] is True
    assert report["legacy_unmanaged_ids"] == [legacy_id]
    assert report["legacy_unmanaged_count"] == 1
    assert report["repair_count"] == 0
    assert report["backup_path"] is None
    assert report["synthetic_receipts_created"] == 0
    assert db.get_node(conn, legacy_id) == before
    assert conn.execute("SELECT COUNT(*) n FROM workstream_ops").fetchone()["n"] == before_ops
    assert conn.execute(
        "SELECT COUNT(*) n FROM workstream_op_events"
    ).fetchone()["n"] == before_events
