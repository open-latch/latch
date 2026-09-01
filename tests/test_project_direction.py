from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: F401,E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.store import artifacts  # noqa: E402
from latch.retrieval import authority  # noqa: E402
from latch.store import db  # noqa: E402
from latch.retrieval import project_direction as pd  # noqa: E402
from latch.store import priorities as priority_store  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_project_direction_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_project_direction_assembles_workstream_spine():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(
            conn,
            kind="workstream",
            title="Seed first-wow path",
            body=(
                "Objective: make install-time seed catch undeniable.\n"
                "Next action: run the applied seed catch demo."
            ),
            status="canonical",
        )
        foundational = db.insert_node(
            conn,
            kind="decision",
            title="Keep seed proof local-first",
            body="Seed proof must stay local-first and preview-first.",
            status="canonical",
        )
        local_decision = db.insert_node(
            conn,
            kind="decision",
            title="Use catch-demo after apply",
            body="Repeat the catch-demo once staging evidence exists.",
            status="canonical",
            workstream_id=ws,
        )
        backlog = db.insert_node(
            conn,
            kind="open_question",
            title="Confirm catch-demo in a throwaway project",
            body="Dogfood the first-wow path.",
            status="staging",
            workstream_id=ws,
        )
        progress = db.insert_node(
            conn,
            kind="progress",
            title="Seed receipt shipped",
            body="Receipt shipped.",
            status="canonical",
            workstream_id=ws,
        )
        db.add_edge(conn, foundational, ws, "constrains")
        artifacts.link_node_artifacts(
            conn,
            progress,
            [{"repo": "/repo/latch", "path": "src/seed.py"}],
        )
        db.set_focus(conn, ws)
        overall_priority = priority_store.add_priority(
            conn,
            "Keep catch-up visibly grounded",
        )["id"]
        scoped_priority = priority_store.add_priority(
            conn,
            "Prefer the seed proof path",
            workstream_id=ws,
        )["id"]

        report = pd.assemble_project_direction(conn, limit=3)
        _assert(report["label"] == "Latch project direction", report)
        _assert(report["mode"] == "expanded", report)
        _assert(report["must_display_to_user"] is True, report)
        _assert(report["used"]["workstreams"] == 1, report["used"])
        item = report["workstreams"][0]
        _assert(item["id"] == ws, item)
        _assert(item["objective"] == "make install-time seed catch undeniable.", item)
        _assert(item["next_action"] == "run the applied seed catch demo.", item)
        _assert(item["next_action_source"] == "declared", item)
        _assert(item["next_action_node_id"] == ws, item)
        _assert(item["priorities"][0]["id"] == scoped_priority, item)
        _assert(report["overall_priorities"][0]["id"] == overall_priority, report)
        _assert(report["used"]["priorities"] == 2, report["used"])
        _assert(report["foregrounded_item"]["id"] == ws, report)
        _assert(report["foregrounded_item"]["reason"] == "declared", report)
        decision_titles = {d["title"]: d for d in item["governing_decisions"]}
        _assert(decision_titles["Keep seed proof local-first"]["authority_tier"]
                == "foundational", decision_titles)
        _assert(decision_titles["Use catch-demo after apply"]["authority_tier"]
                == "lane-local", decision_titles)
        _assert(item["backlog_items"][0]["id"] == backlog, item["backlog_items"])
        _assert(item["recent_progress"][0]["id"] == progress, item["recent_progress"])
        _assert(item["artifacts"][0]["repo"] == "/repo/latch", item["artifacts"])
        _assert(item["artifacts"][0]["path"] == "src/seed.py", item["artifacts"])
        _assert(report["used"]["unanchored_items"] == 0, report["used"])

        text = pd.format_text(report)
        _assert("Latch Project Direction" in text, text)
        _assert("Governing decisions:" in text, text)
        _assert("foundational" in text, text)
        encoded = json.dumps(report)
        _assert("Confirm catch-demo" in encoded, encoded)
        print("PASS project_direction_assembles_workstream_spine")
    finally:
        _cleanup(tmp, conn)


def test_project_direction_uses_shared_graded_authority():
    _assert(pd.AUTHORITY_RELATIONS is authority.AUTHORITY_RELATIONS, "shared relation set")
    _assert(pd._authority_tier(
        relation="constrains", decision_workstream_id=None, workstream_id=41,
    ) == authority.FOUNDATIONAL, "unscoped project constraint is foundational")
    _assert(pd._authority_tier(
        relation="replaces", decision_workstream_id=72, workstream_id=41,
    ) == authority.GOVERNING, "authority edge governs the rendered lane")
    _assert(pd._authority_tier(
        relation="related_to", decision_workstream_id=41, workstream_id=41,
    ) == authority.LANE_LOCAL, "membership is lane-local in its owning lane")


def test_project_direction_falls_back_to_recent_workstreams():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(
            conn,
            kind="workstream",
            title="Unfocused workstream",
            body="Keep this visible even without focus.",
            status="staging",
        )
        report = pd.assemble_project_direction(conn, limit=3)
        _assert(report["used"]["workstreams"] == 1, report)
        _assert(report["workstreams"][0]["id"] == ws, report["workstreams"])
        _assert(report["workstreams"][0]["focus_rank"] is None, report["workstreams"])
        print("PASS project_direction_falls_back_to_recent_workstreams")
    finally:
        _cleanup(tmp, conn)


def test_project_direction_handles_empty_kb():
    tmp, conn = _fresh_db()
    try:
        report = pd.assemble_project_direction(conn)
        _assert(report["used"]["workstreams"] == 0, report)
        _assert(report["workstreams"] == [], report["workstreams"])
        _assert(report["unanchored_evidence"] == [], report["unanchored_evidence"])
        text = pd.format_text(report)
        _assert("No active or recent workstreams found" in text, text)
        print("PASS project_direction_handles_empty_kb")
    finally:
        _cleanup(tmp, conn)


def test_project_direction_compact_mode_is_bounded():
    loose_metadata = "界" * (pd.COMPACT_TEXT_CHARS * 20)
    nodes = [
        pd.DirectionNode(
            id=index,
            kind=loose_metadata,
            title="x" * (pd.COMPACT_TEXT_CHARS + 20),
            status=loose_metadata,
            authority_tier=loose_metadata,
            relation=loose_metadata,
        )
        for index in range(20)
    ]
    artifacts_rows = [
        pd.DirectionArtifact(
            repo="/repo/" + ("r" * (pd.COMPACT_TEXT_CHARS + 20)),
            path="src/" + ("p" * (pd.COMPACT_TEXT_CHARS + 20)),
            node_ids=[index],
        )
        for index in range(20)
    ]
    compact_row = pd._compact_workstream(pd.WorkstreamDirection(
        id=1,
        title="t" * (pd.COMPACT_TEXT_CHARS + 20),
        status=loose_metadata,
        objective="o" * (pd.COMPACT_TEXT_CHARS + 20),
        focus_rank=1,
        focus_score=10.0,
        governing_decisions=nodes,
        backlog_items=nodes,
        constraints=nodes,
        recent_progress=nodes,
        artifacts=artifacts_rows,
        priorities=[],
        next_action="n" * (pd.COMPACT_TEXT_CHARS + 20),
        next_action_source="declared",
        next_action_node_id=1,
        omitted={},
    ))
    _assert(
        len(compact_row["governing_decisions"]) == pd.COMPACT_DECISION_LIMIT,
        compact_row,
    )
    _assert(
        len(compact_row["backlog_items"]) == pd.COMPACT_BACKLOG_LIMIT,
        compact_row,
    )
    _assert(
        len(compact_row["constraints"]) == pd.COMPACT_CONSTRAINT_LIMIT,
        compact_row,
    )
    _assert(
        len(compact_row["recent_progress"]) == pd.COMPACT_PROGRESS_LIMIT,
        compact_row,
    )
    _assert(
        len(compact_row["artifacts"]) == pd.COMPACT_ARTIFACT_LIMIT,
        compact_row,
    )
    _assert(len(compact_row["title"]) <= pd.COMPACT_TEXT_CHARS, compact_row)
    _assert(
        len(compact_row["status"]) <= pd.COMPACT_METADATA_CHARS,
        compact_row,
    )
    _assert(len(compact_row["objective"]) <= pd.COMPACT_TEXT_CHARS, compact_row)
    _assert(len(compact_row["next_action"]) <= pd.COMPACT_TEXT_CHARS, compact_row)
    compact_node = compact_row["governing_decisions"][0]
    for key in ("kind", "status", "authority_tier", "relation"):
        _assert(
            len(compact_node[key]) <= pd.COMPACT_METADATA_CHARS,
            compact_node,
        )

    tmp, conn = _fresh_db()
    try:
        for index in range(pd.COMPACT_WORKSTREAM_LIMIT + 3):
            workstream_id = db.insert_node(
                conn,
                kind="workstream",
                title=f"Catch-up lane {index}",
                body=f"Objective: bounded lane {index}.",
                status="canonical",
            )
            db.set_focus(conn, workstream_id)
        changes_before = conn.total_changes
        report = pd.assemble_project_direction(
            conn,
            limit=999,
            member_limit=999,
            unanchored_limit=999,
            compact=True,
        )
        _assert(report["mode"] == "compact", report)
        _assert(report["read_only"] is True, report)
        _assert(
            len(report["workstreams"]) == pd.COMPACT_WORKSTREAM_LIMIT,
            report["workstreams"],
        )
        _assert(
            report["omitted"]["workstreams"] == 3,
            report["omitted"],
        )
        _assert(report["foregrounded_item"] is not None, report)
        _assert(
            report["compact_limits"]["members_scanned_per_workstream"]
            == pd.COMPACT_MEMBER_LIMIT,
            report["compact_limits"],
        )
        _assert(conn.total_changes == changes_before, "compact direction wrote state")
        print("PASS project_direction_compact_mode_is_bounded")
    finally:
        _cleanup(tmp, conn)


def test_project_direction_labels_inferred_action_and_foregrounds_source():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(
            conn,
            kind="workstream",
            title="Inference lane",
            body="Objective: finish the inferred lane.",
            status="canonical",
        )
        question = db.insert_node(
            conn,
            kind="open_question",
            title="Choose the final adapter",
            body="Which adapter should ship?",
            status="staging",
            workstream_id=ws,
        )
        db.set_focus(conn, ws)

        report = pd.assemble_project_direction(conn, compact=True)
        row = report["workstreams"][0]
        _assert(row["next_action_source"] == "inferred_from_backlog", row)
        _assert(row["next_action_node_id"] == question, row)
        _assert(report["foregrounded_item"]["id"] == question, report)
        _assert(
            report["foregrounded_item"]["reason"] == "inferred_from_backlog",
            report,
        )
        _assert("Next action (inferred_from_backlog" in pd.format_text(report), report)
    finally:
        _cleanup(tmp, conn)


def test_compact_direction_counts_and_fills_nonfocused_workstreams():
    tmp, conn = _fresh_db()
    try:
        workstream_ids = [
            db.insert_node(
                conn,
                kind="workstream",
                title=f"Lane {index}",
                body=f"Objective: keep lane {index} visible.",
                status="canonical",
            )
            for index in range(5)
        ]
        focused = workstream_ids[0]
        db.set_focus(conn, focused)

        report = pd.assemble_project_direction(conn, compact=True)

        rendered_ids = [row["id"] for row in report["workstreams"]]
        _assert(rendered_ids[0] == focused, rendered_ids)
        _assert(len(rendered_ids) == pd.COMPACT_WORKSTREAM_LIMIT, rendered_ids)
        _assert(
            report["omitted"]["workstreams"]
            == len(workstream_ids) - pd.COMPACT_WORKSTREAM_LIMIT,
            report["omitted"],
        )
    finally:
        _cleanup(tmp, conn)


def test_project_direction_compact_report_has_hard_byte_ceiling():
    huge = "界" * 1_000
    node = {
        "id": 1,
        "kind": huge,
        "title": huge,
        "status": huge,
        "authority_tier": huge,
        "relation": huge,
    }
    workstream = {
        "id": 1,
        "title": huge,
        "status": huge,
        "objective": huge,
        "focus_rank": 1,
        "focus_score": 1.0,
        "governing_decisions": [dict(node) for _ in range(4)],
        "backlog_items": [dict(node) for _ in range(4)],
        "constraints": [dict(node) for _ in range(4)],
        "recent_progress": [dict(node) for _ in range(4)],
        "artifacts": [],
        "priorities": [
            {
                "id": index,
                "title": huge,
                "status": "canonical",
                "effective_rank": index,
                "locked": False,
                "workstream_id": 1,
                "scope": "workstream",
            }
            for index in range(4)
        ],
        "next_action": huge,
        "next_action_source": "inferred_from_backlog",
        "next_action_node_id": 1,
        "omitted": {},
    }
    report = {
        "label": "Latch project direction",
        "source": "project_direction",
        "mode": "compact",
        "read_only": True,
        "must_display_to_user": True,
        "summary": huge,
        "why_it_matters": huge,
        "used": {},
        "workstreams": [
            json.loads(json.dumps(workstream))
            for _ in range(2)
        ],
        "overall_priorities": [
            {
                "id": index,
                "title": huge,
                "status": "canonical",
                "effective_rank": index,
                "locked": False,
                "workstream_id": None,
                "scope": "overall",
            }
            for index in range(4)
        ],
        "foregrounded_item": {
            "id": 1,
            "kind": "decision",
            "title": huge,
            "status": "canonical",
            "workstream_id": 1,
            "reason": "inferred_from_backlog",
        },
        "unanchored_evidence": [{"reason": huge} for _ in range(4)],
        "lifecycle_receipts": [{"receipt": huge} for _ in range(4)],
        "omitted": {},
        "compact_limits": {"max_bytes": pd.COMPACT_REPORT_MAX_BYTES},
    }

    bounded = pd.enforce_compact_report_bytes(report, max_bytes=5_000)

    _assert(bounded["compact_truncated"] is True, bounded)
    _assert(
        len(json.dumps(bounded, default=str).encode("utf-8"))
        <= 5_000,
        "compact report exceeded its serialized-byte ceiling",
    )
    _assert(bounded["omitted"]["workstreams"] == 1, bounded["omitted"])
    _assert(bounded["omitted"]["overall_priorities"] == 4, bounded["omitted"])
    _assert(bounded["omitted"]["unanchored_evidence"] == 4, bounded["omitted"])
    _assert(bounded["omitted"]["lifecycle_receipts"] == 4, bounded["omitted"])
    kept = bounded["workstreams"][0]
    for key in (
        "governing_decisions",
        "backlog_items",
        "constraints",
        "recent_progress",
        "priorities",
    ):
        _assert(kept["omitted"][key] == 4, (key, kept["omitted"]))
    _assert(bounded["foregrounded_item"]["id"] == 1, bounded)


def test_project_direction_surfaces_unanchored_recent_evidence():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(
            conn,
            kind="workstream",
            title="Seed report proof loop",
            body=(
                "Objective: make install-time seed report proof loops visible.\n"
                "Next action: polish the seed report proof loop."
            ),
            status="staging",
        )
        unanchored = db.insert_node(
            conn,
            kind="progress",
            title="Seed report proof loop dogfood found missing copy",
            body="The seed report proof loop needs clearer post-apply copy.",
            status="staging",
        )
        db.set_focus(conn, ws)

        report = pd.assemble_project_direction(conn, limit=1)
        _assert(report["used"]["unanchored_items"] == 1, report["used"])
        item = report["unanchored_evidence"][0]
        _assert(item["id"] == unanchored, item)
        _assert(item["suggested_workstream_id"] == ws, item)
        _assert("Shares anchor terms" in item["reason"], item)
        text = pd.format_text(report)
        _assert("Unanchored Recent Evidence" in text, text)
        _assert("automatic backfill" in text, text)
        print("PASS project_direction_surfaces_unanchored_recent_evidence")
    finally:
        _cleanup(tmp, conn)


def test_project_direction_reads_recent_lifecycle_receipts_without_claiming():
    tmp, conn = _fresh_db()
    previous_receipts_live = pd.lifecycle_receipts.RECEIPTS_CHANNEL_LIVE
    try:
        pd.lifecycle_receipts.RECEIPTS_CHANNEL_LIVE = True
        ws = db.insert_node(
            conn,
            kind="workstream",
            title="Receipt lane",
            body="Objective: surface a receipt.\nDone when: visible.",
            status="canonical",
        )
        receipt = (
            'latch opened workstream "Receipt lane" — recurred across 2 '
            "sessions since 2026-07-01; Done when: visible."
        )
        db.begin_workstream_op(
            conn,
            op_key="direction-open-receipt",
            op="OPEN",
            origin="auto",
            candidate_key="candidate:direction-open",
            dst_workstream_id=ws,
            payload={
                "request": {
                    "title": "Receipt lane",
                    "done_when": "visible",
                    "recurrence": {"session_count": 2, "since": "2026-07-01"},
                },
                "title": "Receipt lane",
                "receipt": receipt,
                "assigned_member_ids": [],
                "watch_pair": None,
                "probation": {},
            },
        )
        db.finish_workstream_op(conn, "direction-open-receipt", state="applied")

        changes_before = conn.total_changes
        first = pd.assemble_project_direction(conn)
        _assert(first["used"]["lifecycle_receipts"] == 1, first)
        _assert(receipt in pd.format_text(first), first)
        _assert(conn.total_changes == changes_before, "read-only report committed writes")
        _assert(
            conn.execute(
                "SELECT COUNT(*) FROM workstream_op_events "
                "WHERE event_type='receipt_surfaced'"
            ).fetchone()[0] == 0,
            "project direction claimed the foreground receipt",
        )
        second = pd.assemble_project_direction(conn)
        _assert(second["used"]["lifecycle_receipts"] == 1, second)
        _assert(receipt in pd.format_text(second), second)
        _assert(conn.total_changes == changes_before, "repeat report committed writes")

        pd.lifecycle_receipts.RECEIPTS_CHANNEL_LIVE = False
        silenced = pd.assemble_project_direction(conn)
        _assert(silenced["used"]["lifecycle_receipts"] == 0, silenced)
        _assert(receipt not in pd.format_text(silenced), silenced)
        _assert(conn.total_changes == changes_before, "silenced report committed writes")
    finally:
        pd.lifecycle_receipts.RECEIPTS_CHANNEL_LIVE = previous_receipts_live
        _cleanup(tmp, conn)


def test_project_direction_cli_json_output():
    tmp, conn = _fresh_db()
    try:
        ws = db.insert_node(
            conn,
            kind="workstream",
            title="CLI workstream",
            body="Objective: prove CLI direction output.",
            status="canonical",
        )
        conn.close()
        conn = None
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = pd.main(["--project", tmp, "--format", "json"])
        _assert(rc == 0, rc)
        payload = json.loads(stdout.getvalue())
        _assert(payload["workstreams"][0]["id"] == ws, payload)
        _assert(payload["workstreams"][0]["objective"] == "prove CLI direction output.", payload)
        print("PASS project_direction_cli_json_output")
    finally:
        if conn is not None:
            _cleanup(tmp, conn)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_project_direction_assembles_workstream_spine()
    test_project_direction_uses_shared_graded_authority()
    test_project_direction_falls_back_to_recent_workstreams()
    test_project_direction_handles_empty_kb()
    test_project_direction_compact_mode_is_bounded()
    test_project_direction_surfaces_unanchored_recent_evidence()
    test_project_direction_cli_json_output()
    print("\nAll project direction tests pass.")
