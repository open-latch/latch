#!/usr/bin/env python3
"""Cursor SessionStart hook: silent session handoff, gate state, and wiring."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

from _common import (  # noqa: E402
    fence_inactive_session,
    log,
    read_hook_input,
    record_session_binding,
    session_start_transition,
    transcript_path,
)

import cursor_gate_state  # noqa: E402
import cursor_session  # noqa: E402
import cursor_wiring  # noqa: E402
import project_config  # noqa: E402
from project_config import ProjectConfigError  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402
from session_start import (  # noqa: E402
    _SESSION_SETUP_NOTICE,
    _build_locked_notice,
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
    payload = read_hook_input()
    cwd = cursor_project_cwd(payload)
    try:
        with session_start_transition(cwd):
            return _run_session_start(payload, cwd)
    except (OSError, ProjectConfigError) as exc:
        log(
            f"cursor_session_start transition coordination failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        emit_cursor_context(_SESSION_SETUP_NOTICE)
        return 0


def _run_session_start(payload: dict, cwd: str) -> int:
    sid = cursor_session_id(payload)
    tpath = transcript_path(payload)
    setup_degraded = False
    if not sid:
        setup_degraded = True
        log(
            "cursor_session_start missing conversation id",
            cwd,
            expected_revision="stale-session",
        )

    if is_unlatched_mode(cwd):
        fence_inactive_session(cwd, sid)
        emit_cursor_context(_build_unlatched_notice(cwd))
        return 0
    target = project_config.resolve(cwd)
    if target.state == project_config.MODE_LOCKED:
        fence_inactive_session(cwd, sid)
        emit_cursor_context(_build_locked_notice(target))
        return 0

    try:
        binding_revision = record_session_binding(cwd, sid)
    except (OSError, ProjectConfigError) as exc:
        log(
            f"cursor_session_start binding snapshot failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        emit_cursor_context(_SESSION_SETUP_NOTICE)
        return 0
    if binding_revision is None:
        log(
            "cursor_session_start could not verify a conversation id for this binding",
            cwd,
            expected_revision="stale-session",
        )
        emit_cursor_context(_SESSION_SETUP_NOTICE)
        return 0
    if is_disabled(cwd):
        return 0

    try:
        wiring_result = cursor_wiring.repair_project(cwd)
    except Exception as e:
        log(
            f"cursor project wiring check failed: {e}",
            cwd,
            expected_revision=binding_revision,
        )
        wiring_result = cursor_wiring.RepairResult(
            "error",
            "_⚠ latch could not check Cursor project wiring. This task will continue; "
            "rerun the latch Cursor installer manually._",
        )

    try:
        cursor_gate_state.initialize_session(cwd, sid)
    except Exception as e:
        setup_degraded = True
        log(
            f"cursor_session_start gate state reset failed: {e}",
            cwd,
            expected_revision=binding_revision,
        )

    # Transcript/session identity is independent of opening the database.
    # Marker failures remain visible because current-chat compact/seed
    # resolution cannot safely guess another conversation.
    if sid:
        try:
            cursor_session.write_marker(cwd, sid, transcript_path=tpath)
        except Exception as e:
            setup_degraded = True
            log(
                f"cursor_session_start marker write failed: {e}",
                cwd,
                expected_revision=binding_revision,
            )
    emit_cursor_context(_join_startup_notices(
        _SESSION_SETUP_NOTICE if setup_degraded else None,
        wiring_result.notice,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
