#!/usr/bin/env python3
"""Cursor preToolUse hook: deny mutation until the current prompt is gated."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import cursor_gate_state  # noqa: E402
import cursor_session  # noqa: E402
import lockfile  # noqa: E402
from _common import (  # noqa: E402
    STALE_SESSION_MESSAGE,
    current_session_revision,
    log,
    read_hook_input,
    transcript_path,
)
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402


DENY_MESSAGE = (
    "Latch blocked this mutation because the current Cursor prompt has no "
    "matching gate receipt. Call latch_gate with the user request verbatim, "
    "surface the Latch gate findings, then retry the tool."
)
OPERATION_DENY_MESSAGE = (
    "Latch blocked this managed operation because its one-shot receipt was "
    "missing, already consumed, or did not match the exact tool and arguments. "
    "Rerun the documented preview/prepare workflow or correct the invocation; "
    "a general latch_gate receipt cannot override this narrow lane."
)
STALE_DENY_MESSAGE = STALE_SESSION_MESSAGE


def decision(payload: dict) -> dict:
    cwd = cursor_gate_state.project_cwd(payload)
    sid = cursor_gate_state.session_id(payload, cwd)
    try:
        binding_revision = current_session_revision(cwd, sid)
    except Exception as exc:
        log(
            f"cursor_pre_tool_use binding verification failed: {exc}",
            cwd,
            expected_revision="stale-session",
        )
        return {"permission": "deny", "user_message": STALE_DENY_MESSAGE}
    if binding_revision is None:
        log(
            f"cursor_pre_tool_use skipped stale session: {STALE_SESSION_MESSAGE}",
            cwd,
            expected_revision="stale-session",
        )
        return {"permission": "deny", "user_message": STALE_DENY_MESSAGE}
    try:
        with lockfile.project_access_lock(cwd):
            if current_session_revision(cwd, sid) != binding_revision:
                log(
                    "cursor_pre_tool_use skipped stale session: "
                    + STALE_SESSION_MESSAGE,
                    cwd,
                    expected_revision=binding_revision,
                )
                return {"permission": "deny", "user_message": STALE_DENY_MESSAGE}
            return _current_session_decision(payload, cwd, sid)
    except lockfile.ProjectTargetChangedError:
        log(
            f"cursor_pre_tool_use skipped stale session: {STALE_SESSION_MESSAGE}",
            cwd,
            expected_revision=binding_revision,
        )
        return {"permission": "deny", "user_message": STALE_DENY_MESSAGE}


def _current_session_decision(payload: dict, cwd: str, sid: str | None) -> dict:
    tpath = transcript_path(payload)
    if sid and tpath:
        cursor_session.refresh_transcript_path(cwd, sid, tpath)
    mutation, _kind = cursor_gate_state.mutation_capability(payload)
    if not mutation:
        return {}
    operation_intended, _intent_reason = \
        cursor_gate_state.managed_operation_intended(cwd, sid)
    operation_allowed, _operation_reason = \
        cursor_gate_state.consume_operation_authorization(cwd, sid, payload)
    if operation_allowed:
        return {}
    if operation_intended:
        return {"permission": "deny", "user_message": OPERATION_DENY_MESSAGE}
    allowed, _reason = cursor_gate_state.mutation_authorized(cwd, sid)
    if allowed:
        # Empty output preserves Cursor's own permission/autorun behavior.
        return {}
    return {"permission": "deny", "user_message": DENY_MESSAGE}


def main() -> int:
    payload = read_hook_input()
    try:
        cwd = cursor_gate_state.project_cwd(payload)
        if is_disabled(cwd) or is_in_compact() or is_unlatched_mode(cwd):
            print("{}")
            return 0
        print(json.dumps(decision(payload)))
    except Exception as e:
        print(json.dumps({"permission": "deny", "user_message": DENY_MESSAGE}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
