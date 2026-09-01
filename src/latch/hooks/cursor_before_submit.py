#!/usr/bin/env python3
"""Cursor beforeSubmitPrompt hook: invalidate the prior gate receipt."""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import json
import sys
from pathlib import Path

from latch.hosts import cursor_gate_state  # noqa: E402
from latch.hooks._common import log, read_hook_input  # noqa: E402
from latch.store.paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402


def current_session_context(sid: str | None) -> str | None:
    if not sid:
        return None
    return (
        f"Latch Cursor current session id: `{sid}`. Managed current-session "
        "workflows must pass this exact id to their wrapper; never reuse another "
        "chat's id or scan Cursor history."
    )


def main() -> int:
    payload = read_hook_input()
    try:
        cwd = cursor_gate_state.project_cwd(payload)
        sid = cursor_gate_state.session_id(payload, cwd)
        if is_disabled() or is_in_compact() or is_unlatched_mode():
            # Clear any earlier authorization so toggling latch back on cannot
            # revive a receipt from before the disabled/unlatched prompt.
            cursor_gate_state.reset_session(cwd, sid)
            print("{}")
            return 0
        prompt = cursor_gate_state.prompt_text(payload)
        cursor_gate_state.begin_prompt(cwd, sid, prompt)
        response = {"continue": True}
        context = current_session_context(sid)
        if context:
            response["additional_context"] = context
        print(json.dumps(response))
    except Exception as e:
        log(f"cursor_before_submit failed: {e}")
        # Non-zero plus failClosed=true prevents a stale prior receipt from
        # carrying into a prompt we could not fingerprint.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
