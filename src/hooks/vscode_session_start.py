#!/usr/bin/env python3
"""VS Code/Copilot SessionStart hook: AGENTS.md re-sync + brief.

This is intentionally thinner than Claude Code's lifecycle hook and separate
from Codex's hook. VS Code's hook support is still preview, and VS Code can
also discover Claude Code hook files. The VS Code adapter should only surface
read-side context at session start and keep AGENTS.md fresh; it should not
spawn transcript compaction or write Codex session markers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure src/ and src/hooks/ are importable when VS Code launches this script
# directly from .github/hooks/latch.json.
SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import hook_field, log, read_hook_input, transcript_path  # noqa: E402

import budget  # noqa: E402
import db  # noqa: E402
from paths import is_disabled, is_in_compact  # noqa: E402
from session_start import _build_briefing  # noqa: E402


def vscode_project_cwd(payload: dict) -> str:
    return hook_field(
        payload,
        "cwd",
        "workingDirectory",
        "workspaceRoot",
        "workspaceFolder",
        "workdir",
        default=os.getcwd(),
    )


def vscode_session_id(payload: dict) -> str | None:
    return hook_field(payload, "session_id", "sessionId", "id")


def main() -> int:
    if is_disabled() or is_in_compact():
        return 0

    payload = read_hook_input()
    cwd = vscode_project_cwd(payload)
    sid = vscode_session_id(payload)
    tpath = transcript_path(payload)

    surfaced_ids: list[int] = []
    try:
        conn = db.connect(cwd)
        try:
            if sid:
                db.upsert_session(conn, sid, cwd, tpath)
            orphan_count = len(db.orphaned_sessions(conn, cwd))
        finally:
            conn.close()
    except Exception as e:
        log(f"vscode_session_start db error: {e}")
        orphan_count = 0

    try:
        budget_line = budget.brief_line(cwd)
    except Exception as e:
        log(f"vscode_session_start budget brief_line failed: {e}")
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
                    source="vscode_session_start",
                )
            finally:
                conn.close()
        except Exception as e:
            log(f"vscode_session_start record_retrievals failed: {e}")

    if briefing:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": briefing,
            }
        }))

    return 0


def _auto_sync_agents_md(cwd: str) -> str | None:
    """Re-sync this project's AGENTS.md managed region when already wired."""
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
