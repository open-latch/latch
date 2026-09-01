#!/usr/bin/env python3
"""Cursor SessionStart hook: silent session handoff, gate state, and wiring."""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import json
import sys
from pathlib import Path

from latch.hooks._common import log, read_hook_input, transcript_path  # noqa: E402

from latch.hosts import cursor_gate_state  # noqa: E402
from latch.hosts import cursor_session  # noqa: E402
from latch.hosts import cursor_wiring  # noqa: E402
from latch.store.paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from latch.hooks.session_start import (  # noqa: E402
    _SESSION_SETUP_NOTICE,
    _build_unlatched_notice,
    _join_startup_notices,
)


def cursor_project_cwd(payload: dict) -> str:
    return cursor_gate_state.project_cwd(payload)


def cursor_session_id(payload: dict) -> str | None:
    return cursor_gate_state.session_id(payload, cursor_project_cwd(payload))


def emit_cursor_context(context: str) -> None:
    if context:
        print(json.dumps({"additional_context": context}))


def main() -> int:
    if is_in_compact():
        return 0
    if is_unlatched_mode():
        emit_cursor_context(_build_unlatched_notice())
        return 0
    if is_disabled():
        return 0

    payload = read_hook_input()
    cwd = cursor_project_cwd(payload)
    sid = cursor_session_id(payload)
    tpath = transcript_path(payload)
    setup_degraded = False
    if not sid:
        setup_degraded = True
        log("cursor_session_start missing conversation id")

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
        cursor_gate_state.initialize_session(cwd, sid)
    except Exception as e:
        setup_degraded = True
        log(f"cursor_session_start gate state reset failed: {e}")

    # Transcript/session identity is independent of opening the database.
    # Marker failures remain visible because current-chat compact/seed
    # resolution cannot safely guess another conversation.
    if sid:
        try:
            cursor_session.write_marker(cwd, sid, transcript_path=tpath)
        except Exception as e:
            setup_degraded = True
            log(f"cursor_session_start marker write failed: {e}")

    emit_cursor_context(_join_startup_notices(
        _SESSION_SETUP_NOTICE if setup_degraded else None,
        wiring_result.notice,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
