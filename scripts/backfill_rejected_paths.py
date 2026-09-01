"""One-time V2 backfill: recover typed `rejected_path` rows from existing decisions.

Roadmap item **V2** (decision id=3948). The typed table and its accessors live in
`src/schema.sql` and `src/db.py`; this script is the record of the one-time
migration that populated it from decision bodies written before the type existed.

**This has already been applied** to the project vault (201 rows, all
`source='backfill'`). The script is committed so the operation is reviewable and
repeatable, not so it runs again. Nothing installs, schedules, or imports it —
it is not wired into `install.sh`, the daemon, or the test suite. `apply` is a
dry run unless `--write` is passed, and the underlying insert is
`INSERT OR IGNORE` on `UNIQUE(node_id, option)`, so a re-run is a no-op rather
than a duplicate.

## The two phases, and the judgment between them

    python scripts/backfill_rejected_paths.py candidates
    python scripts/backfill_rejected_paths.py apply <manifest.json>          # dry run
    python scripts/backfill_rejected_paths.py apply <manifest.json> --write  # commits

Both phases open the vault through the engine's own resolution, so a pinned
install is targeted the usual way — `LATCH_KB_DIR=/path/to/vault` — and `--cwd`
selects a per-project vault in unpinned (legacy) mode.

`candidates` is deterministic and reproducible: a deliberately wide regex net
over every `kind='decision'` node, minus the self-reference class. It answers
"which nodes are worth reading", not "which nodes are rejections".

Between the two phases sits a step this file does **not** reproduce: reading each
candidate body and deciding, against the pre-registered rubric in
`docs/v2_rejection_rubric.md`, whether it records a genuine rejection — and if so
extracting the five fields. That is judgment, not a regex, and the rubric was
fixed before any count was produced precisely so it could be audited afterward.
The manifest is the output of that pass.

`apply` takes that manifest and does the mechanical half. The manifest is not
committed: it holds verbatim decision text from a private vault, and this
repository is public.

## Manifest format

A JSON list of objects, one per rejected option:

    [
      {
        "node_id": 2394,
        "option": "…the alternative that was available and not taken…",
        "reason": "…why it was rejected…",
        "ratifier": "founder (verbatim)",
        "decided_at": "2026-07-20",
        "scope_predicate": "distribution: repository choice",
        "source": "backfill"
      }
    ]

`option` and `reason` are required and must be non-blank — an unexplained
rejection cannot support a revival check. Any other key is ignored, including
`confidence`: the classification pass tagged rows clean/soft, but `rejected_path`
deliberately has no `confidence` column. A backfilled row is distinguished from a
declared one by `source='backfill'` and nothing else.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.store import db  # noqa: E402

# The wide net, per the rubric's recall note: the retired detector matched only
# "rejected" | "discarded" | "ruled", which misses a rejection phrased "we are
# not going to do X because Y". Widening here is intentional — precision is the
# judgment pass's job, recall is this one's.
CANDIDATE_PATTERNS = [
    r"\breject(?:ed|s|ing)?\b", r"\bdiscard(?:ed|s|ing)?\b", r"\bruled out\b",
    r"\bnot going to\b", r"\bdecided against\b",
    r"\bconsidered and (?:rejected|dropped|declined)\b", r"\bwe will not\b",
    r"\bdo not\b.{0,40}\b(?:build|ship|use|adopt|add|pursue)\b",
    r"\bnever\b.{0,30}\b(?:build|ship|use|adopt|per-seat|own)\b",
    r"\brules? out\b", r"\bdeclined\b", r"\bkilled\b", r"\bdead end\b",
    r"\bwrong (?:trade|move|call)\b", r"\bexplicitly not\b", r"\binstead of\b",
    r"\brather than\b", r"\bnot recommended\b", r"\babandon(?:ed|ing)?\b",
]

# Disqualifier D1. Latch's own roadmap nodes describing this feature match every
# pattern above on self-reference alone; the rubric records this as the dominant
# false-positive class. Stripped before the net is re-applied, so a node that
# *only* matched on self-reference drops out.
SELF_REF = [
    r"typed\s+`?rejected`?", r"rejected-path", r"ratified/rejected",
    r"rejected/superseded", r"≥20 genuine rejections",
    r"backfill \d+ existing decisions", r"reject-with-reason",
    r"rejected-with-reason",
]

FIELDS = ("option", "reason", "ratifier", "decided_at", "scope_predicate", "source")


def find_candidates(conn: sqlite3.Connection) -> tuple[list[int], int]:
    """Node ids worth reading, and the decision denominator they came from."""
    rows = conn.execute(
        "SELECT id, body FROM nodes WHERE kind='decision'"
    ).fetchall()
    net = [re.compile(p, re.I) for p in CANDIDATE_PATTERNS]
    self_ref = [re.compile(p, re.I) for p in SELF_REF]

    survivors = []
    for row in rows:
        text = row["body"] or ""
        if not any(p.search(text) for p in net):
            continue
        stripped = text
        for p in self_ref:
            stripped = p.sub(" ", stripped)
        if any(p.search(stripped) for p in net):
            survivors.append(int(row["id"]))
    return sorted(set(survivors)), len(rows)


def load_manifest(path: Path) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(entries).__name__}")
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"{path}[{i}]: expected an object")
        if not isinstance(entry.get("node_id"), int):
            raise ValueError(f"{path}[{i}]: node_id must be an int")
        for required in ("option", "reason"):
            value = entry.get(required)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path}[{i}]: {required} must be a non-blank string")
    return entries


def apply_manifest(
    conn: sqlite3.Connection, entries: list[dict], *, write: bool
) -> tuple[int, int, int]:
    """Insert each entry. Returns (inserted, already_present, missing_node)."""
    inserted = skipped = missing = 0
    for entry in entries:
        node_id = entry["node_id"]
        exists = conn.execute(
            "SELECT 1 FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if exists is None:
            print(f"  skip node {node_id}: no such node", file=sys.stderr)
            missing += 1
            continue
        # Only the six declared fields reach the table; `confidence` and any
        # other manifest bookkeeping is dropped here by construction.
        kwargs = {k: entry[k] for k in FIELDS if k in entry and k != "source"}
        row_id = db.insert_rejected_path_nc(
            conn, node_id, source=entry.get("source", "backfill"), **kwargs
        )
        if row_id is None:
            skipped += 1
        else:
            inserted += 1
    if write:
        conn.commit()
    else:
        conn.rollback()
    return inserted, skipped, missing


def cmd_candidates(args: argparse.Namespace) -> int:
    conn = db.connect_readonly(args.cwd)
    try:
        survivors, total = find_candidates(conn)
    finally:
        conn.close()
    print(f"decisions scanned : {total}")
    print(f"candidates        : {len(survivors)}")
    if args.out:
        Path(args.out).write_text(json.dumps(survivors), encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(" ".join(str(i) for i in survivors))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    entries = load_manifest(Path(args.manifest))
    conn = db.connect(args.cwd)
    try:
        inserted, skipped, missing = apply_manifest(conn, entries, write=args.write)
    finally:
        conn.close()
    mode = "committed" if args.write else "DRY RUN — rolled back, pass --write to commit"
    print(f"manifest rows     : {len(entries)}")
    print(f"inserted          : {inserted}")
    print(f"already present   : {skipped}")
    print(f"unknown node      : {missing}")
    print(mode)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--cwd",
        default=None,
        help="project whose vault to open (default: the configured/pinned vault)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_cand = sub.add_parser("candidates", help="print decision ids worth reading")
    p_cand.add_argument("--out", help="write the id list to this JSON file")
    p_cand.set_defaults(func=cmd_candidates)

    p_apply = sub.add_parser("apply", help="insert rejected_path rows from a manifest")
    p_apply.add_argument("manifest", help="path to the extraction manifest JSON")
    p_apply.add_argument(
        "--write",
        action="store_true",
        help="commit the rows (without this the transaction is rolled back)",
    )
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
