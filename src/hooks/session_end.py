"""SessionEnd hook: final compact + promote summary to canonical."""
from __future__ import annotations

import sys

from _common import (
    log, project_cwd, read_hook_input, session_id,
    spawn_compactor_detached, transcript_path,
)

import db
from paths import is_in_compact, is_write_disabled


def main() -> int:
    # is_write_disabled() implies is_disabled(); covers both kill-switches.
    if is_write_disabled() or is_in_compact():
        return 0
    payload = read_hook_input()
    sid = session_id(payload)
    if not sid:
        return 0
    cwd = project_cwd(payload)
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


if __name__ == "__main__":
    sys.exit(main())
