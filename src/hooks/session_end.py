"""SessionEnd hook: final compact + promote summary to canonical."""
from __future__ import annotations

import json
import sys

from _common import (
    log, project_cwd, read_hook_input, session_id,
    spawn_compactor_detached, transcript_path,
)

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
    # The payload project is authoritative even when the hook executable was
    # started from an unrelated directory.
    payload = read_hook_input()
    cwd = project_cwd(payload)
    # is_write_disabled() implies is_disabled(); covers both kill-switches.
    if is_unlatched_mode():
        _print_unlatched_context("SessionEnd")
        return 0
    if is_write_disabled() or is_in_compact():
        return 0
    _load_runtime()
    sid = session_id(payload)
    if not sid:
        return 0
    tpath = transcript_path(payload)

    try:
        conn = db.connect(cwd)
        try:
            db.upsert_session(conn, sid, cwd, tpath)
            sess = db.get_session(conn, sid)
            if sess and sess.get("ended_at"):
                return 0  # already finalized (e.g. SessionStart reconciled it)
        finally:
            conn.close()
    except Exception as e:
        log(f"session_end db error: {e}")
        return 0

    log(f"session_end: session={sid}")
    spawn_compactor_detached(sid, cwd, tpath, final=True)
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
