"""SessionEnd hook: final compact + promote summary to canonical."""
from __future__ import annotations

import json
import sys

from _common import (
    STALE_SESSION_MESSAGE,
    clear_session_binding,
    current_session_revision,
    log,
    project_cwd,
    read_hook_input,
    session_id,
    spawn_compactor_detached,
    transcript_path,
)

import lockfile
from paths import UNLATCHED_MESSAGE, is_in_compact, is_unlatched_mode, is_write_disabled

db = None
_RUNTIME_LOADED = False


def _load_runtime() -> None:
    global db, _RUNTIME_LOADED
    if _RUNTIME_LOADED:
        return
    import db as _db

    db = _db
    _RUNTIME_LOADED = True


def main() -> int:
    payload = read_hook_input()
    cwd = project_cwd(payload)
    # is_write_disabled() implies is_disabled(); covers both kill-switches.
    if is_unlatched_mode(cwd):
        _print_unlatched_context("SessionEnd")
        return 0
    if is_write_disabled(cwd) or is_in_compact():
        return 0
    _load_runtime()
    sid = session_id(payload)
    if not sid:
        return 0
    try:
        binding_revision = current_session_revision(cwd, sid)
    except Exception as e:
        log(
            f"session_end binding error: {e}",
            cwd,
            expected_revision="stale-session",
        )
        return 0
    if binding_revision is None:
        log(
            f"session_end skipped stale session: {STALE_SESSION_MESSAGE}",
            cwd,
            expected_revision="stale-session",
        )
        return 0
    tpath = transcript_path(payload)

    try:
        with lockfile.project_access_lock(cwd) as locked_kb:
            if current_session_revision(cwd, sid) != binding_revision:
                log(
                    f"session_end skipped stale session: {STALE_SESSION_MESSAGE}",
                    cwd,
                    expected_revision=binding_revision,
                )
                return 0
            conn = db.connect(
                cwd,
                expected_binding_revision=binding_revision,
                expected_kb_dir=str(locked_kb),
            )
            try:
                db.upsert_session(conn, sid, cwd, tpath)
                sess = db.get_session(conn, sid)
                if sess and sess.get("ended_at"):
                    return 0  # already finalized by an earlier end/final compact
            finally:
                conn.close()
    except Exception as e:
        log(
            f"session_end db error: {e}",
            cwd,
            expected_revision=binding_revision,
        )
        return 0

    log(
        f"session_end: session={sid}",
        cwd,
        expected_revision=binding_revision,
    )
    spawn_compactor_detached(
        sid,
        cwd,
        tpath,
        final=True,
        binding_revision=binding_revision,
        expected_kb_dir=str(locked_kb),
    )
    try:
        clear_session_binding(
            cwd,
            sid,
            expected_revision=binding_revision,
        )
    except Exception as e:
        log(
            f"session_end binding cleanup failed: {e}",
            cwd,
            expected_revision=binding_revision,
        )
    return 0


def _print_unlatched_context(event: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": UNLATCHED_MESSAGE,
        }
    }))


if __name__ == "__main__":
    sys.exit(main())
