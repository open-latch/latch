"""Aggregate-only coverage report for rejected-path predicate compilation."""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence

from latch.gate import predicate


_WRITE_PROBE = """
INSERT INTO rejected_path
    (id, node_id, option, reason, scope_predicate, source)
VALUES
    (-9223372036854775808, -1, '__predicate_coverage_probe__',
     '__predicate_coverage_probe__', NULL, 'declared')
"""


def open_read_only(vault_path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite vault through URI ``mode=ro`` only."""
    path = Path(vault_path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError(f"vault path is not a file: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # Defense in depth.  mode=ro is the authority boundary; query_only makes a
    # future connection-mode regression fail closed as well.
    conn.execute("PRAGMA query_only = ON")
    return conn


def verify_read_only(conn: sqlite3.Connection) -> bool:
    """Attempt an INSERT and accept only SQLite's READONLY failure."""
    try:
        conn.execute(_WRITE_PROBE)
    except sqlite3.OperationalError as exc:
        conn.rollback()
        error_code = getattr(exc, "sqlite_errorcode", None)
        if error_code is not None and error_code & 0xFF != sqlite3.SQLITE_READONLY:
            raise RuntimeError("write probe failed for a non-read-only reason") from exc
        if error_code is None and "readonly" not in str(exc).lower().replace("-", ""):
            raise RuntimeError("write probe did not prove a read-only connection") from exc
        return True
    except Exception:
        conn.rollback()
        raise
    else:
        # This rollback prevents persistence even if both read-only defenses
        # were accidentally removed.  Success is still a hard failure.
        conn.rollback()
        raise RuntimeError("write probe unexpectedly succeeded")


def coverage_report(vault_path: str | Path) -> dict[str, object]:
    """Compile every rejected_path row and return counts only.

    The SELECT intentionally excludes option and reason.  scope_predicate is
    used transiently by the compiler but only its type bucket is emitted.
    """
    conn = open_read_only(vault_path)
    try:
        read_only_verified = verify_read_only(conn)
        rows = conn.execute(
            "SELECT id, node_id, scope_predicate, source "
            "FROM rejected_path ORDER BY id"
        )
        prefix_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        compiled = 0
        uncompilable = 0
        total = 0
        for row in rows:
            total += 1
            prefix_counts[predicate.coverage_prefix(row["scope_predicate"])] += 1
            source = row["source"]
            source_counts[source if source in {"declared", "backfill"} else "<other>"] += 1
            check = predicate.compile_predicate(
                {
                    "id": row["id"],
                    "node_id": row["node_id"],
                    "option": "",
                    "reason": "",
                    "scope_predicate": row["scope_predicate"],
                    "source": source,
                }
            )
            if check.compilable:
                compiled += 1
            else:
                uncompilable += 1
    finally:
        conn.close()

    if compiled + uncompilable != total:
        raise RuntimeError("predicate accounting invariant failed")
    if sum(prefix_counts.values()) != total or sum(source_counts.values()) != total:
        raise RuntimeError("aggregate accounting invariant failed")

    return {
        "engine": predicate.ENGINE,
        "total": total,
        "per_prefix": dict(sorted(prefix_counts.items())),
        "source_counts": dict(sorted(source_counts.items())),
        "compiled": compiled,
        "uncompilable": uncompilable,
        "compiled_fraction": compiled / total if total else 0.0,
        "read_only_verified": read_only_verified,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report aggregate rejected-path predicate compilability"
    )
    parser.add_argument("vault", help="path to an existing SQLite vault")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    json.dump(coverage_report(args.vault), sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
