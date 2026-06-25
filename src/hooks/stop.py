"""Stop hook: increments turn counter and auto-compacts every 5 user exchanges.

Runs after every assistant turn. Stays cheap: a single SQLite read/write,
then optionally spawn a detached compactor subprocess.
"""
from __future__ import annotations

import sys

from _common import (
    log, project_cwd, read_hook_input, session_id, spawn_compactor_detached,
    transcript_path,
)

import db
from paths import is_in_compact, is_write_disabled

COMPACT_EVERY_N_TURNS = 5


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
            turn = db.increment_turn(conn, sid)
            sess = db.get_session(conn, sid)
            last = sess["last_compact_turn"] if sess else 0
            should_compact = (turn - last) >= COMPACT_EVERY_N_TURNS
        finally:
            conn.close()
    except Exception as e:
        log(f"stop hook db error: {e}")
        return 0

    if should_compact:
        log(f"auto-compact: session={sid} turn={turn}")
        spawn_compactor_detached(sid, cwd, tpath, final=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
