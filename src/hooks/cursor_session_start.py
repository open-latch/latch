#!/usr/bin/env python3
"""Cursor SessionStart hook: session handoff, AGENTS.md sync, and KB brief."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import log, read_hook_input, transcript_path  # noqa: E402

import budget  # noqa: E402
import cursor_gate_state  # noqa: E402
import cursor_session  # noqa: E402
import cursor_wiring  # noqa: E402
import db  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import _build_briefing, _build_unlatched_brief  # noqa: E402


def cursor_project_cwd(payload: dict) -> str:
    return cursor_gate_state.project_cwd(payload)


def cursor_session_id(payload: dict) -> str | None:
    return cursor_gate_state.session_id(payload, cursor_project_cwd(payload))


def emit_cursor_context(context: str) -> None:
    if context:
        print(json.dumps({"additional_context": context}))


def main() -> int:
    if is_in_compact() or is_disabled():
        return 0
    if is_unlatched_mode():
        emit_cursor_context(_build_unlatched_brief())
        return 0

    payload = read_hook_input()
    cwd = cursor_project_cwd(payload)
    sid = cursor_session_id(payload)
    tpath = transcript_path(payload)
    surfaced_ids: list[int] = []

    try:
        wiring_result = cursor_wiring.repair_project(cwd)
    except Exception as e:
        log(f"cursor project wiring check failed: {e}")
        wiring_result = cursor_wiring.RepairResult(
            "error",
            "_⚠ latch could not check Cursor project wiring. This task will continue; "
            "rerun the latch Cursor installer manually._",
        )

    try:
        cursor_gate_state.reset_session(cwd, sid)
    except Exception as e:
        log(f"cursor_session_start gate state reset failed: {e}")

    try:
        conn = db.connect(cwd)
        try:
            if sid:
                db.upsert_session(conn, sid, cwd, tpath)
                try:
                    cursor_session.write_marker(cwd, sid, transcript_path=tpath)
                except Exception as e:
                    log(f"cursor_session_start marker write failed: {e}")
            orphan_count = len(db.orphaned_sessions(conn, cwd))
        finally:
            conn.close()
    except Exception as e:
        log(f"cursor_session_start db error: {e}")
        orphan_count = 0

    try:
        budget_line = budget.brief_line(cwd)
    except Exception as e:
        log(f"cursor_session_start budget brief_line failed: {e}")
        budget_line = None

    briefing = _build_briefing(
        cwd,
        orphan_count=orphan_count,
        budget_line=budget_line,
        surfaced_ids=surfaced_ids,
        claude_md_synced=False,
        synced_doc_name="AGENTS.md",
        wiring_notice=wiring_result.notice,
    )

    if sid and surfaced_ids:
        try:
            conn = db.connect(cwd)
            try:
                db.record_retrievals(
                    conn,
                    session_id=sid,
                    turn=0,
                    items=[(nid, None) for nid in surfaced_ids],
                    source="cursor_session_start",
                )
            finally:
                conn.close()
        except Exception as e:
            log(f"cursor_session_start record_retrievals failed: {e}")

    if sid:
        briefing = (
            f"{briefing}\n\nLatch Cursor session id: `{sid}`. "
            "Managed current-session workflows must pass this exact id to their wrapper."
        )
    emit_cursor_context(briefing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
