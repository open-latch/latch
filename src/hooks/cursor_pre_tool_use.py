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
from _common import log, read_hook_input  # noqa: E402
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402


DENY_MESSAGE = (
    "Latch blocked this mutation because the current Cursor prompt has no "
    "matching gate receipt. Call latch_gate with the user request verbatim, "
    "surface the Latch gate findings, then retry the tool."
)


def decision(payload: dict) -> dict:
    mutation, _kind = cursor_gate_state.mutation_capability(payload)
    if not mutation:
        return {}
    cwd = cursor_gate_state.project_cwd(payload)
    sid = cursor_gate_state.session_id(payload, cwd)
    operation_allowed, _operation_reason = \
        cursor_gate_state.consume_operation_authorization(cwd, sid, payload)
    if operation_allowed:
        return {}
    allowed, _reason = cursor_gate_state.mutation_authorized(cwd, sid)
    if allowed:
        # Empty output preserves Cursor's own permission/autorun behavior.
        return {}
    return {"permission": "deny", "user_message": DENY_MESSAGE}


def main() -> int:
    if is_disabled() or is_in_compact() or is_unlatched_mode():
        print("{}")
        return 0
    payload = read_hook_input()
    try:
        print(json.dumps(decision(payload)))
    except Exception as e:
        log(f"cursor_pre_tool_use failed closed: {e}")
        print(json.dumps({"permission": "deny", "user_message": DENY_MESSAGE}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
