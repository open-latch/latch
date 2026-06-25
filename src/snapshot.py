"""Dump a project's kb.db to a text SQL file for git-friendly backup.

Usage:
    python src/snapshot.py <project_dir_name>      # one project
    python src/snapshot.py --all                   # every projects/<name>/ with a kb.db

Output: projects/<name>/kb.dump.sql

Restore:
    sqlite3 projects/<name>/kb.db < projects/<name>/kb.dump.sql
    python src/migrate_v2.py <project_path>     # repopulates vec_nodes from embeddings

The dump preserves nodes/edges/sessions data and the schema (including CREATE
VIRTUAL TABLE statements + triggers). It drops INSERTs targeting virtual-table
shadow storage; FTS5 repopulates via triggers on the nodes INSERTs, and
vec_nodes is rebuilt by migrate_v2.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SKIP_TABLE_PREFIXES = ("nodes_fts", "vec_nodes")


def _is_skipped_insert(line: str) -> bool:
    if not line.startswith("INSERT INTO"):
        return False
    for prefix in SKIP_TABLE_PREFIXES:
        if f'INSERT INTO "{prefix}"' in line or f"INSERT INTO {prefix}" in line:
            return True
    return False


def _load_vec_extension(conn: sqlite3.Connection) -> None:
    """iterdump() introspects every table including vec_nodes, which requires
    the vec0 module loaded or PRAGMA table_info fails."""
    try:
        import sqlite_vec  # type: ignore

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass


def dump_db(db_path: Path, out_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    _load_vec_extension(conn)
    try:
        kept = 0
        with out_path.open("w", encoding="utf-8", newline="\n") as f:
            for line in conn.iterdump():
                if _is_skipped_insert(line):
                    continue
                f.write(line + "\n")
                kept += 1
        return kept
    finally:
        conn.close()


def _projects_root() -> Path:
    return Path(__file__).resolve().parent.parent / "projects"


def _dump_one(project_name: str) -> None:
    proj_dir = _projects_root() / project_name
    db = proj_dir / "kb.db"
    if not db.exists():
        print(f"skip {project_name}: no kb.db", file=sys.stderr)
        return
    out = proj_dir / "kb.dump.sql"
    n = dump_db(db, out)
    size_kb = out.stat().st_size / 1024
    print(f"{project_name}: {n} statements, {size_kb:.1f} KB -> {out.name}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    arg = argv[1]
    root = _projects_root()
    if arg == "--all":
        for p in sorted(root.iterdir()):
            if p.is_dir() and (p / "kb.db").exists():
                _dump_one(p.name)
    else:
        _dump_one(arg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
