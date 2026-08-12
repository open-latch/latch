#!/usr/bin/env python3
"""Cursor beforeSubmitPrompt hook: invalidate the prior gate receipt."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "hooks"))

import cursor_gate_state  # noqa: E402
import lockfile  # noqa: E402
from _common import (  # noqa: E402
    STALE_SESSION_MESSAGE,
    current_session_revision,
    log,
    read_hook_input,
)
from paths import is_disabled, is_in_compact, is_unlatched_mode  # noqa: E402


def current_session_context(sid: str | None) -> str | None:
    if not sid:
        return None
    return (
        f"Latch Cursor current session id: `{sid}`. Managed current-session "
        "workflows must pass this exact id to their wrapper; never reuse another "
        "chat's id or scan Cursor history."
    )


def _reject_stale_session(
    cwd: str,
    *,
    expected_revision: str = "stale-session",
) -> int:
    log(
        f"cursor_before_submit skipped stale session: {STALE_SESSION_MESSAGE}",
        cwd,
        expected_revision=expected_revision,
    )
    print(json.dumps({
        "continue": False,
        "additional_context": STALE_SESSION_MESSAGE,
    }))
    return 1


def main() -> int:
    payload = read_hook_input()
    cwd: str | None = None
    binding_revision: str | None = None
    try:
        cwd = cursor_gate_state.project_cwd(payload)
        sid = cursor_gate_state.session_id(payload, cwd)
        if is_unlatched_mode(cwd):
            print("{}")
            return 0
        binding_revision = current_session_revision(cwd, sid)
        if binding_revision is None:
            return _reject_stale_session(cwd)
        with lockfile.project_access_lock(cwd):
            if current_session_revision(cwd, sid) != binding_revision:
                return _reject_stale_session(
                    cwd,
                    expected_revision=binding_revision,
                )
            if is_disabled(cwd) or is_in_compact():
                # Clear valid-session authorization so re-enabling Latch cannot
                # revive a receipt from before the disabled prompt.
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
    except lockfile.ProjectTargetChangedError:
        assert cwd is not None
        return _reject_stale_session(
            cwd,
            expected_revision=binding_revision or "stale-session",
        )
    except Exception as e:
        if cwd is not None and binding_revision is not None:
            log(
                f"cursor_before_submit failed: {e}",
                cwd,
                expected_revision=binding_revision,
            )
        # Non-zero plus failClosed=true prevents a stale prior receipt from
        # carrying into a prompt we could not fingerprint.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
