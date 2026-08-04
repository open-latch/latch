#!/usr/bin/env python3
"""Codex SessionStart hook: silent attribution and managed-wiring repair.

Codex support intentionally does not mirror Claude Code's Stop/SessionEnd
automatic compaction. Healthy startup emits no model context.
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

import codex_session  # noqa: E402
import codex_wiring  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import (  # noqa: E402
    _SESSION_SETUP_NOTICE,
    _build_unlatched_notice,
    _build_unlatched_system_message,
    _emit_session_start_context,
    _join_startup_notices,
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
            _build_unlatched_notice(),
            system_message=_build_unlatched_system_message(),
        )
        return 0
    if is_disabled():
        return 0

    payload = read_hook_input()
    cwd = codex_project_cwd(payload)
    sid = codex_session_id(payload)
    tpath = transcript_path(payload)

    setup_degraded = not bool(sid)
    if sid:
        # Session attribution is independent of KB setup.  In particular, a
        # readable external vault may be outside this hook's writable sandbox;
        # codex_session supplies a private runtime fallback for that case.
        try:
            codex_session.write_marker(
                cwd, sid, transcript_path=tpath,
            )
        except Exception as e:
            setup_degraded = True
            log(f"codex_session_start marker write failed: {e}")
    else:
        # A missing current id must invalidate any prior workspace marker;
        # otherwise an MCP process without CODEX_THREAD_ID can inherit the
        # previous task's attribution.
        try:
            codex_session.invalidate_marker(cwd)
        except Exception as e:
            log(f"codex_session_start marker invalidation failed: {e}")

    wiring_result = _auto_repair_codex_wiring(cwd)

    notice = _join_startup_notices(
        _SESSION_SETUP_NOTICE if setup_degraded else None,
        wiring_result.notice,
    )
    if notice:
        _emit_session_start_context(notice)

    return 0


def _auto_repair_codex_wiring(cwd: str) -> codex_wiring.RepairResult:
    """Repair an older managed Codex bundle without blocking SessionStart."""
    try:
        result = codex_wiring.repair_project(cwd)
        if result.action == "synced":
            log(f"codex wiring auto-repair: refreshed managed bundle for {cwd}")
        elif result.action == "error":
            log(f"codex wiring auto-repair could not complete for {cwd}")
        return result
    except Exception as e:
        log(f"codex wiring auto-repair skipped: {e}")
        return codex_wiring.RepairResult(
            "error",
            "_⚠ Latch could not check Codex project wiring. This task will "
            "continue; rerun the Latch Codex installer manually._",
        )


if __name__ == "__main__":
    sys.exit(main())
