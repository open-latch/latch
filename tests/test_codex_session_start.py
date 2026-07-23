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


def test_main_treats_marker_fallback_as_readonly_when_session_upsert_is_noop(
    monkeypatch, capsys,
):
    tmp = Path(tempfile.mkdtemp(prefix="codex_marker_fallback_start_"))
    conn = db.connect(str(tmp))
    try:
        vector = embeddings.embed("Existing session lane\n\nmarker fallback detects it")
        db.insert_node(
            conn,
            kind="workstream",
            title="Existing session lane",
            body="Objective: marker fallback detects read-only startup",
            status="canonical",
            embedding=embeddings.to_blob(vector),
        )
        db.upsert_session(conn, "existing-thread", str(tmp), None)
    finally:
        conn.close()

    retrieval_calls = []
    primary = tmp / "primary-marker.json"
    fallback = tmp / "fallback-marker.json"
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
        monkeypatch.setattr(css.codex_session, "marker_path", lambda _cwd: primary)
        monkeypatch.setattr(
            css.codex_session,
            "write_marker",
            lambda *_args, **_kwargs: fallback,
        )
        monkeypatch.setattr(
            db,
            "record_retrievals",
            lambda *_args, **_kwargs: retrieval_calls.append(True),
        )

        assert css.main() == 0
        output = json.loads(capsys.readouterr().out)
        brief = output["hookSpecificOutput"]["additionalContext"]
        _assert("Existing session lane" in brief, brief)
        _assert("loaded core KB context read-only" in brief, brief)
        _assert(retrieval_calls == [], retrieval_calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_auto_sync_agents_md_does_not_first_wire_absent_file()
    test_auto_sync_agents_md_repairs_existing_managed_region()
    test_brief_uses_agents_md_resync_notice_name()
    test_codex_payload_helpers()
    print("\nAll codex_session_start tests pass.")
