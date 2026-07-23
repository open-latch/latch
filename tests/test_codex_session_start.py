"""Unit tests for the Codex SessionStart hook shim."""
from __future__ import annotations

import os
import json
import sqlite3
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import agents_md_sync as ams  # noqa: E402
import codex_session_start as css  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import lifecycle_receipts  # noqa: E402
import schema_version  # noqa: E402
import session_start  # noqa: E402
import versioning  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_codex_payload_helpers():
    old = os.environ.get("CODEX_THREAD_ID")
    try:
        os.environ["CODEX_THREAD_ID"] = "env-thread"
        payload = {"workspaceRoot": "/repo", "threadId": "payload-thread"}
        _assert(css.codex_project_cwd(payload) == "/repo", payload)
        _assert(css.codex_session_id(payload) == "payload-thread", payload)
        _assert(css.codex_session_id({}) == "env-thread", "env fallback should work")
    finally:
        if old is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = old
    print("PASS codex_payload_helpers")


def test_auto_sync_agents_md_repairs_existing_managed_region():
    tmp = Path(tempfile.mkdtemp(prefix="codex_agents_sync_"))
    try:
        target = tmp / "AGENTS.md"
        ams.sync(target)
        target.write_text(
            target.read_text(encoding="utf-8").replace(
                f"latch-wiring-version: {versioning.WIRING_VERSION}",
                f"latch-wiring-version: {versioning.WIRING_VERSION - 1}",
            ).replace("Latch Contract", "Latch X"),
            encoding="utf-8",
        )
        _assert(ams.evaluate(target) == ams.DRIFT, "tampered region -> DRIFT")
        action = css._auto_sync_agents_md(str(tmp))
        _assert(action == "synced", f"expected synced, got {action!r}")
        _assert(ams.evaluate(target) == ams.OK, "AGENTS.md should be repaired")
        _assert((tmp / "AGENTS.md.latchbak").is_file(), "backup should be written")
        print("PASS auto_sync_agents_md_repairs_existing_managed_region")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_auto_sync_agents_md_does_not_first_wire_absent_file():
    tmp = Path(tempfile.mkdtemp(prefix="codex_agents_absent_"))
    try:
        target = tmp / "AGENTS.md"
        action = css._auto_sync_agents_md(str(tmp))
        _assert(action == "skipped", f"expected skipped, got {action!r}")
        _assert(not target.exists(), "auto-sync must not create AGENTS.md")
        print("PASS auto_sync_agents_md_does_not_first_wire_absent_file")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_brief_uses_agents_md_resync_notice_name():
    tmp = Path(tempfile.mkdtemp(prefix="codex_agents_notice_"))
    try:
        brief = session_start._build_briefing(
            str(tmp),
            claude_md_synced=True,
            synced_doc_name="AGENTS.md",
        )
        _assert("latch AGENTS.md was re-synced" in brief,
                f"AGENTS.md notice missing: {brief!r}")
        _assert("AGENTS.md.latchbak" in brief,
                f"AGENTS.md backup pointer missing: {brief!r}")
        _assert("CLAUDE.md.latchbak" not in brief,
                f"notice should not mention CLAUDE.md backup: {brief!r}")
        print("PASS brief_uses_agents_md_resync_notice_name")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_emits_full_brief_when_session_write_is_readonly(monkeypatch, capsys):
    tmp = Path(tempfile.mkdtemp(prefix="codex_readonly_start_"))
    conn = db.connect(str(tmp))
    try:
        vector = embeddings.embed("Readonly dogfood\n\nstartup context survives")
        db.insert_node(
            conn,
            kind="workstream",
            title="Readonly dogfood",
            body="Objective: startup context survives",
            status="canonical",
            embedding=embeddings.to_blob(vector),
        )
    finally:
        conn.close()

    retrieval_calls = []
    original_upsert = db.upsert_session
    try:
        monkeypatch.setattr(css, "is_in_compact", lambda: False)
        monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
        monkeypatch.setattr(css, "is_disabled", lambda: False)
        monkeypatch.setattr(
            css,
            "read_hook_input",
            lambda: {"cwd": str(tmp), "threadId": "readonly-thread"},
        )
        monkeypatch.setattr(css, "transcript_path", lambda _payload: None)
        monkeypatch.setattr(css, "_auto_sync_agents_md", lambda _cwd: None)
        monkeypatch.setattr(css.budget, "brief_line", lambda _cwd: None)

        def denied_upsert(*_args, **_kwargs):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(db, "upsert_session", denied_upsert)
        monkeypatch.setattr(
            db,
            "record_retrievals",
            lambda *_args, **_kwargs: retrieval_calls.append(True),
        )

        assert css.main() == 0
        output = json.loads(capsys.readouterr().out)
        brief = output["hookSpecificOutput"]["additionalContext"]
        _assert("_Full:" in brief, brief)
        _assert("Readonly dogfood" in brief, brief)
        _assert("loaded core KB context read-only" in brief, brief)
        _assert(retrieval_calls == [], retrieval_calls)
    finally:
        db.upsert_session = original_upsert
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_treats_marker_fallback_as_marker_only_when_db_is_writable(
    monkeypatch, capsys,
):
    tmp = Path(tempfile.mkdtemp(prefix="codex_marker_fallback_start_"))
    conn = db.connect(str(tmp))
    try:
        vector = embeddings.embed("Marker fallback lane\n\nDB writes still run")
        db.insert_node(
            conn,
            kind="workstream",
            title="Marker fallback lane",
            body="Objective: marker failure stays independent from DB writability",
            status="canonical",
            embedding=embeddings.to_blob(vector),
        )
    finally:
        conn.close()

    fallback = tmp / "fallback-marker.json"
    try:
        monkeypatch.setattr(css, "is_in_compact", lambda: False)
        monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
        monkeypatch.setattr(css, "is_disabled", lambda: False)
        monkeypatch.setattr(
            css,
            "read_hook_input",
            lambda: {"cwd": str(tmp), "threadId": "marker-only-thread"},
        )
        monkeypatch.setattr(css, "transcript_path", lambda _payload: None)
        monkeypatch.setattr(css, "_auto_sync_agents_md", lambda _cwd: None)
        monkeypatch.setattr(css.budget, "brief_line", lambda _cwd: None)
        monkeypatch.setattr(
            css.codex_session,
            "write_marker",
            lambda *_args, **_kwargs: fallback,
        )

        assert css.main() == 0
        output = json.loads(capsys.readouterr().out)
        brief = output["hookSpecificOutput"]["additionalContext"]
        _assert("Marker fallback lane" in brief, brief)
        _assert("loaded core KB context read-only" not in brief, brief)

        check = db.connect(str(tmp))
        try:
            _assert(
                db.get_session(check, "marker-only-thread") is not None,
                "marker-only fallback must not suppress session registration",
            )
            retrieval_count = check.execute(
                "SELECT COUNT(*) AS n FROM session_retrievals "
                "WHERE session_id = ?",
                ("marker-only-thread",),
            ).fetchone()["n"]
            _assert(
                retrieval_count > 0,
                "marker-only fallback must not suppress retrieval dedupe writes",
            )
        finally:
            check.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_preserves_brief_when_retrieval_is_first_readonly_signal(
    monkeypatch, capsys,
):
    tmp = Path(tempfile.mkdtemp(prefix="codex_retrieval_readonly_start_"))
    conn = db.connect(str(tmp))
    try:
        vector = embeddings.embed("Existing session lane\n\nretrieval detects readonly")
        wid = db.insert_node(
            conn,
            kind="workstream",
            title="Existing session lane",
            body="Objective: retrieval denial corrects the startup banner",
            status="canonical",
            embedding=embeddings.to_blob(vector),
        )
        receipt = lifecycle_receipts.opened(
            "Existing session lane", 2, "2026-07-01", "startup remains honest",
        )
        db.begin_workstream_op(
            conn,
            op_key="open-codex-late-readonly",
            op="OPEN",
            origin="auto",
            candidate_key="open:codex-late-readonly",
            dst_workstream_id=wid,
            payload={
                "request": {
                    "title": "Existing session lane",
                    "done_when": "startup remains honest",
                    "recurrence": {
                        "session_count": 2,
                        "since": "2026-07-01",
                    },
                },
                "title": "Existing session lane",
                "receipt": receipt,
                "assigned_member_ids": [],
                "watch_pair": None,
                "probation": {},
            },
        )
        db.finish_workstream_op(
            conn, "open-codex-late-readonly", state="applied",
        )
        db.upsert_session(conn, "existing-thread", str(tmp), None)
    finally:
        conn.close()

    retrieval_calls = []
    try:
        monkeypatch.setattr(css, "is_in_compact", lambda: False)
        monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
        monkeypatch.setattr(css, "is_disabled", lambda: False)
        monkeypatch.setattr(
            css,
            "read_hook_input",
            lambda: {"cwd": str(tmp), "threadId": "existing-thread"},
        )
        monkeypatch.setattr(css, "transcript_path", lambda _payload: None)
        monkeypatch.setattr(css, "_auto_sync_agents_md", lambda _cwd: None)
        monkeypatch.setattr(css.budget, "brief_line", lambda _cwd: None)
        monkeypatch.setattr(
            css.codex_session,
            "write_marker",
            lambda *_args, **_kwargs: tmp / "primary-marker.json",
        )

        def denied_retrieval(*_args, **_kwargs):
            retrieval_calls.append(True)
            raise sqlite3.OperationalError("attempt to write a readonly database")

        monkeypatch.setattr(db, "record_retrievals", denied_retrieval)

        assert css.main() == 0
        output = json.loads(capsys.readouterr().out)
        brief = output["hookSpecificOutput"]["additionalContext"]
        _assert("Existing session lane" in brief, brief)
        _assert("loaded core KB context read-only" in brief, brief)
        _assert(brief.count(receipt) == 1, brief)
        _assert(retrieval_calls == [True], retrieval_calls)

        check = db.connect(str(tmp))
        try:
            _assert(
                lifecycle_receipts.pending_receipts(check) == [],
                "the emitted lifecycle receipt should remain durably claimed",
            )
        finally:
            check.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_reports_nonreadonly_retrieval_write_failure(monkeypatch, capsys):
    tmp = Path(tempfile.mkdtemp(prefix="codex_retrieval_warning_start_"))
    conn = db.connect(str(tmp))
    try:
        vector = embeddings.embed("Retrieval warning lane\n\nmetadata failure is visible")
        db.insert_node(
            conn,
            kind="workstream",
            title="Retrieval warning lane",
            body="Objective: non-read-only metadata failures remain visible",
            status="canonical",
            embedding=embeddings.to_blob(vector),
        )
    finally:
        conn.close()

    try:
        monkeypatch.setattr(css, "is_in_compact", lambda: False)
        monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
        monkeypatch.setattr(css, "is_disabled", lambda: False)
        monkeypatch.setattr(
            css,
            "read_hook_input",
            lambda: {"cwd": str(tmp), "threadId": "warning-thread"},
        )
        monkeypatch.setattr(css, "transcript_path", lambda _payload: None)
        monkeypatch.setattr(css, "_auto_sync_agents_md", lambda _cwd: None)
        monkeypatch.setattr(css.budget, "brief_line", lambda _cwd: None)
        monkeypatch.setattr(
            css.codex_session,
            "write_marker",
            lambda *_args, **_kwargs: tmp / "primary-marker.json",
        )

        def broken_retrieval(*_args, **_kwargs):
            raise sqlite3.IntegrityError("synthetic retrieval metadata failure")

        monkeypatch.setattr(db, "record_retrievals", broken_retrieval)

        assert css.main() == 0
        output = json.loads(capsys.readouterr().out)
        brief = output["hookSpecificOutput"]["additionalContext"]
        _assert("Retrieval warning lane" in brief, brief)
        _assert("some SessionStart metadata could not be updated" in brief, brief)
        _assert("loaded core KB context read-only" not in brief, brief)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_emits_unavailable_stub_when_all_kb_opens_fail(monkeypatch, capsys):
    def denied_write(*_args, **_kwargs):
        raise PermissionError("sensitive writable path")

    def migration_required(*_args, **_kwargs):
        raise schema_version.SchemaMigrationRequiredError(
            "sensitive migration detail",
        )

    monkeypatch.setattr(css, "is_in_compact", lambda: False)
    monkeypatch.setattr(css, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(css, "is_disabled", lambda: False)
    monkeypatch.setattr(css, "read_hook_input", lambda: {"cwd": "/unavailable"})
    monkeypatch.setattr(css, "codex_session_id", lambda _payload: None)
    monkeypatch.setattr(css, "transcript_path", lambda _payload: None)
    monkeypatch.setattr(css, "_auto_sync_agents_md", lambda _cwd: None)
    monkeypatch.setattr(css.budget, "brief_line", lambda _cwd: None)
    monkeypatch.setattr(db, "connect", denied_write)
    monkeypatch.setattr(db, "connect_readonly", migration_required)

    assert css.main() == 0
    output = json.loads(capsys.readouterr().out)
    brief = output["hookSpecificOutput"]["additionalContext"]
    _assert("# latch — session brief unavailable" in brief, brief)
    _assert("requires a schema migration" in brief, brief)
    _assert("sensitive" not in brief, brief)


if __name__ == "__main__":
    test_auto_sync_agents_md_does_not_first_wire_absent_file()
    test_auto_sync_agents_md_repairs_existing_managed_region()
    test_brief_uses_agents_md_resync_notice_name()
    test_codex_payload_helpers()
    print("\nAll codex_session_start tests pass.")
