#!/usr/bin/env python3
"""Codex SessionStart hook: AGENTS.md re-sync + brief, no auto-compaction.

Codex support intentionally does not mirror Claude Code's Stop/SessionEnd
automatic compaction right now. This hook re-syncs an already-wired AGENTS.md
managed region, builds the start-of-session KB brief, and records retrievals
for dedupe when a session id is available.
"""
from __future__ import annotations

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
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import (  # noqa: E402
    _build_briefing,
    _build_unlatched_brief,
    _build_unlatched_system_message,
    _emit_session_start_context,
    _managed_doc_wiring_notice,
)


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
    if is_in_compact():
        return 0
    if is_unlatched_mode():
        _emit_session_start_context(
            _build_unlatched_brief(),
            system_message=_build_unlatched_system_message(),
        )
        return 0
    if is_disabled():
        return 0

    payload = read_hook_input()
    cwd = codex_project_cwd(payload)
    sid = codex_session_id(payload)
    tpath = transcript_path(payload)

    surfaced_ids: list[int] = []
    read_only_startup = False
    startup_write_warning = False
    if sid:
        # Session attribution is independent of KB setup.  In particular, a
        # readable external vault may be outside this hook's writable sandbox;
        # codex_session supplies a private runtime fallback for that case.
        try:
            written_marker = codex_session.write_marker(
                cwd, sid, transcript_path=tpath,
            )
            if written_marker != codex_session.marker_path(cwd):
                # The vault-local rendezvous was not writable.  An existing
                # session row can make upsert_session a read-only no-op, so the
                # marker destination is the reliable degradation signal.
                read_only_startup = True
        except Exception as e:
            startup_write_warning = True
            log(f"codex_session_start marker write failed: {e}")

    if read_only_startup:
        try:
            conn = db.connect_readonly(cwd)
            try:
                orphan_count = len(db.orphaned_sessions(conn, cwd))
            finally:
                conn.close()
        except Exception as e:
            log(f"codex_session_start read-only setup failed: {e}")
            orphan_count = 0
    else:
        try:
            conn = db.connect(cwd)
            try:
                if sid:
                    try:
                        db.upsert_session(conn, sid, cwd, tpath)
                    except Exception as e:
                        if db.is_readonly_error(e):
                            read_only_startup = True
                        else:
                            startup_write_warning = True
                        log(f"codex_session_start session upsert failed: {e}")
                orphan_count = len(db.orphaned_sessions(conn, cwd))
            finally:
                conn.close()
        except Exception as e:
            log(f"codex_session_start db error: {e}")
            orphan_count = 0
            try:
                conn = db.connect_readonly(cwd)
                try:
                    orphan_count = len(db.orphaned_sessions(conn, cwd))
                    read_only_startup = True
                finally:
                    conn.close()
            except Exception as read_error:
                log(f"codex_session_start read-only fallback failed: {read_error}")

    try:
        budget_line = budget.brief_line(cwd)
    except Exception as e:
        log(f"codex_session_start budget brief_line failed: {e}")
        budget_line = None

    agents_md_action = _auto_sync_agents_md(cwd)
    wiring_notice = _managed_doc_wiring_notice(
        agents_md_action,
        doc_name="AGENTS.md",
        manual_command=f"{SRC.parent}/bin/install_agents_md.sh --yes",
    )

    briefing = _build_briefing(
        cwd,
        orphan_count=orphan_count,
        budget_line=budget_line,
        surfaced_ids=surfaced_ids,
        claude_md_synced=(agents_md_action == "synced"),
        synced_doc_name="AGENTS.md",
        wiring_notice=wiring_notice,
        read_only=read_only_startup,
        startup_write_warning=startup_write_warning,
    )

    if sid and surfaced_ids and not read_only_startup:
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
        _emit_session_start_context(briefing)

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
        action = agents_md_sync.sync_if_outdated(target)
        if action == "synced":
            log(f"agents_md auto-sync: re-synced managed region in {target} "
                f"(backup: {target}.latchbak)")
        return action
    except Exception as e:
        log(f"agents_md auto-sync skipped: {e}")
        return "error"


if __name__ == "__main__":
    sys.exit(main())
