#!/usr/bin/env python3
"""Codex SessionStart hook: AGENTS.md re-sync + brief, no auto-compaction.

Codex support intentionally does not mirror Claude Code's Stop/SessionEnd
automatic compaction right now. This hook re-syncs an already-wired AGENTS.md
managed region, builds the start-of-session KB brief, and records retrievals
for dedupe when a session id is available.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure src/ and src/hooks/ are importable when Codex launches this script
# directly from hooks.json.
SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import hook_field, log, read_hook_input, transcript_path  # noqa: E402

import budget  # noqa: E402
import codex_session  # noqa: E402
import db  # noqa: E402
from paths import is_disabled, is_in_compact  # noqa: E402
from session_start import _build_briefing  # noqa: E402


def codex_project_cwd(payload: dict) -> str:
    return hook_field(
        payload,
        "cwd",
        "workingDirectory",
        "workspaceRoot",
        "workdir",
        default=os.getcwd(),
    )


def codex_session_id(payload: dict) -> str | None:
    return (
        hook_field(payload, "session_id", "sessionId", "thread_id", "threadId", "id")
        or os.environ.get("CODEX_THREAD_ID")
    )


def main() -> int:
    if is_disabled() or is_in_compact():
        return 0

    payload = read_hook_input()
    cwd = codex_project_cwd(payload)
    sid = codex_session_id(payload)
    tpath = transcript_path(payload)

    surfaced_ids: list[int] = []
    try:
        conn = db.connect(cwd)
        try:
            if sid:
                db.upsert_session(conn, sid, cwd, tpath)
                try:
                    codex_session.write_marker(cwd, sid, transcript_path=tpath)
                except Exception as e:
                    log(f"codex_session_start marker write failed: {e}")
            orphan_count = len(db.orphaned_sessions(conn, cwd))
        finally:
            conn.close()
    except Exception as e:
        log(f"codex_session_start db error: {e}")
        orphan_count = 0

    try:
        budget_line = budget.brief_line(cwd)
    except Exception as e:
        log(f"codex_session_start budget brief_line failed: {e}")
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
                    source="codex_session_start",
                )
            finally:
                conn.close()
        except Exception as e:
            log(f"codex_session_start record_retrievals failed: {e}")

    if briefing:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": briefing,
            }
        }))

    return 0


def _auto_sync_agents_md(cwd: str) -> str | None:
    """Re-sync this project's AGENTS.md managed region when already wired.

    Mirrors Claude's CLAUDE.md hot-path behavior: ``create=False`` means a
    fresh or unmanaged project is never auto-wired, but an existing managed
    region is kept current after latch upgrades. Wrapped so sync failures never
    break Codex session startup.
    """
    try:
        import agents_md_sync
        target = Path(cwd) / "AGENTS.md"
        action = agents_md_sync.sync(target, create=False)
        if action == "synced":
            log(f"agents_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)")
        return action
    except Exception as e:
        log(f"agents_md auto-sync skipped: {e}")
        return None


if __name__ == "__main__":
    sys.exit(main())
