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

import artifacts  # noqa: E402
import db  # noqa: E402
import project_direction as pd  # noqa: E402


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

        report = pd.assemble_project_direction(conn, limit=3)
        _assert(report["label"] == "Latch project direction", report)
        _assert(report["must_display_to_user"] is True, report)
        _assert(report["used"]["workstreams"] == 1, report["used"])
        item = report["workstreams"][0]
        _assert(item["id"] == ws, item)
        _assert(item["objective"] == "make install-time seed catch undeniable.", item)
        _assert(item["next_action"] == "run the applied seed catch demo.", item)
        decision_titles = {d["title"]: d for d in item["governing_decisions"]}
        _assert(decision_titles["Keep seed proof local-first"]["authority_tier"]
                == "foundational_project_decision", decision_titles)
        _assert(decision_titles["Use catch-demo after apply"]["authority_tier"]
                == "local_implementation_decision", decision_titles)
        _assert(item["backlog_items"][0]["id"] == backlog, item["backlog_items"])
        _assert(item["recent_progress"][0]["id"] == progress, item["recent_progress"])
        _assert(item["artifacts"][0]["repo"] == "/repo/latch", item["artifacts"])
        _assert(item["artifacts"][0]["path"] == "src/seed.py", item["artifacts"])
        _assert(report["used"]["unanchored_items"] == 0, report["used"])

        text = pd.format_text(report)
        _assert("Latch Project Direction" in text, text)
        _assert("Governing decisions:" in text, text)
        _assert("foundational_project_decision" in text, text)
        encoded = json.dumps(report)
        _assert("Confirm catch-demo" in encoded, encoded)
        print("PASS project_direction_assembles_workstream_spine")
    finally:
        _cleanup(tmp, conn)


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
    test_project_direction_falls_back_to_recent_workstreams()
    test_project_direction_handles_empty_kb()
    test_project_direction_surfaces_unanchored_recent_evidence()
    test_project_direction_cli_json_output()
    print("\nAll project direction tests pass.")
