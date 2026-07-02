"""Internal/advanced CLI for workstream focus controls.

Usage (called by `bin/run_kb_focus.sh`):

    python kb_focus_cli.py <project_cwd> <subcommand> [args...]

Subcommands:
    list                       — print current focus rows as JSON
    set <workstream_id>        — explicit boost (FOCUS_USER_BOOST), set_by=user
    pin <workstream_id>        — pin (insert if missing); pinned rows resist pruning
    unpin <workstream_id>      — clear pinned flag
    drop <workstream_id>       — remove row (loses score history)
    prune                      — keep top-N non-pinned + all pinned, delete the rest

Always prints a single JSON object to stdout. Exit 0 unless argv is malformed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db


def _emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    return 0


def _focus_list(conn) -> list[dict]:
    rows = db.get_focus(conn, limit=10)
    out = []
    for r in rows:
        out.append({
            "workstream_id": r["workstream_id"],
            "title": r["title"],
            "score": round(float(r["score"]), 3),
            "effective_score": round(float(r["effective_score"]), 3),
            "rank": r["rank"],
            "pinned": bool(r["pinned"]),
            "set_by": r["set_by"],
            "set_at": r["set_at"],
        })
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        return _emit({"ok": False, "error": "usage: kb_focus_cli.py <cwd> <subcommand> [args]"})
    cwd = argv[1]
    sub = argv[2]
    rest = argv[3:]

    conn = db.connect(cwd)
    try:
        if sub == "list":
            return _emit({"ok": True, "focus": _focus_list(conn)})

        if sub == "prune":
            n = db.prune_focus(conn)
            return _emit({"ok": True, "action": "pruned", "deleted": n,
                          "focus": _focus_list(conn)})

        if sub in ("set", "pin", "unpin", "drop"):
            if not rest:
                return _emit({"ok": False, "error": f"{sub} requires <workstream_id>"})
            try:
                wid = int(rest[0])
            except ValueError:
                return _emit({"ok": False, "error": f"workstream_id must be int, got {rest[0]!r}"})

            row = conn.execute("SELECT id, kind, title, status FROM nodes WHERE id = ?", (wid,)).fetchone()
            if row is None:
                return _emit({"ok": False, "error": f"node {wid} not found"})
            if sub != "drop" and row["kind"] != "workstream":
                return _emit({
                    "ok": False,
                    "error": f"node {wid} is kind={row['kind']!r}, not 'workstream'",
                })

            if sub == "set":
                db.set_focus(conn, wid, set_by="user")
                action = "set"
            elif sub == "pin":
                ok = db.pin_focus(conn, wid)
                if not ok:
                    return _emit({"ok": False, "error": f"node {wid} is not a workstream"})
                action = "pinned"
            elif sub == "unpin":
                db.unpin_focus(conn, wid)
                action = "unpinned"
            else:  # drop
                db.drop_focus(conn, wid)
                action = "dropped"

            return _emit({
                "ok": True, "action": action, "workstream_id": wid,
                "title": row["title"], "focus": _focus_list(conn),
            })

        return _emit({"ok": False, "error": f"unknown subcommand {sub!r}"})
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
