"""Unit tests for the Codex SessionStart hook shim."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import agents_md_sync as ams  # noqa: E402
import codex_session_start as css  # noqa: E402
import session_start  # noqa: E402


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
            target.read_text(encoding="utf-8").replace("KB usage", "KB X"),
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


if __name__ == "__main__":
    test_auto_sync_agents_md_does_not_first_wire_absent_file()
    test_auto_sync_agents_md_repairs_existing_managed_region()
    test_brief_uses_agents_md_resync_notice_name()
    test_codex_payload_helpers()
    print("\nAll codex_session_start tests pass.")
