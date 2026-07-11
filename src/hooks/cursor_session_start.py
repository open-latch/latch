#!/usr/bin/env python3
"""Cursor SessionStart hook: session handoff, AGENTS.md sync, and KB brief."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import hook_field, log, read_hook_input, transcript_path  # noqa: E402

import budget  # noqa: E402
import cursor_session  # noqa: E402
import db  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import _build_briefing, _build_unlatched_brief  # noqa: E402


def cursor_project_cwd(payload: dict) -> str:
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]
    return hook_field(
        payload, "workspaceRoot", "cwd", "workingDirectory", "workdir",
        default=os.getcwd(),
    )


def cursor_session_id(payload: dict) -> str | None:
    return hook_field(payload, "conversation_id", "session_id", "sessionId", "id")


def emit_cursor_context(context: str) -> None:
    if context:
        print(json.dumps({"additional_context": context}))


def _auto_sync_agents_md(cwd: str) -> str | None:
    try:
        import agents_md_sync
        target = Path(cwd) / "AGENTS.md"
        action = agents_md_sync.sync(target, create=False)
        if action == "synced":
            log(f"cursor agents_md auto-sync: re-synced managed region in {target}")
        return action
    except Exception as e:
        log(f"cursor agents_md auto-sync skipped: {e}")
        return None


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

    agents_md_action = _auto_sync_agents_md(cwd)
    briefing = _build_briefing(
        cwd,
        orphan_count=orphan_count,
        budget_line=budget_line,
        surfaced_ids=surfaced_ids,
        claude_md_synced=(agents_md_action == "synced"),
        synced_doc_name="AGENTS.md",
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
