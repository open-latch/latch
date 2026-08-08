"""V4 citation-rate counter over gate.log rows (roadmap 3948 item V4, built
under handoff id=4626 item 4).

Implements docs/v4_citation_metric.md byte-for-byte in field names:

    V4 = 100 × |{eligible rows: cited_rejected_paths ≠ [] and
                 recommendation ∈ V4_CHANGED_LABELS}| ÷ |eligible rows|

    eligible := skipped == false AND error == null AND recommendation ∈
                CLASSIFIER_LABELS AND "surfaced_rejected_paths" present.

Every row lands in exactly one bucket, checked in this precedence order:
unparsable → non_gate → capability_missing → skipped → errored →
invalid_recommendation → eligible. The excluded buckets are reported so the
denominator is auditable; nothing is silently dropped (priority 4114).

Stdlib-only and deliberately independent of gate.py — a log counter must not
drag the gate runtime in. Label parity with gate.CLASSIFIER_LABELS is pinned
by tests/test_v4_citation_rate.py instead.

Usage:
    python3 src/v4_citation_rate.py <gate-log-file-or-vault-dir> ...
Directories are expanded to their gate-*.log files (the daily-log naming
log_utils uses). Output is one JSON object on stdout. The PASS judgment at
the ≥5% bar belongs to the founder at window close; the exit code is always
0 for readable input.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path
from typing import Iterable, Iterator

CLASSIFIER_LABELS = ("PROCEED", "MODIFY", "DO_NOT_PROCEED", "NEEDS_HUMAN_JUDGMENT")
# The declared changed-verdict subset (docs/v4_citation_metric.md deviation 2:
# NEEDS_HUMAN_JUDGMENT is a routing outcome, not a changed verdict).
V4_CHANGED_LABELS = ("MODIFY", "DO_NOT_PROCEED")
PASS_THRESHOLD_PCT = 5.0


def _clean_int_list(value) -> list[int]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, int) and not isinstance(x, bool)]


def _pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 2)


def compute(rows: Iterable[dict | None]) -> dict:
    """Bucket every row and compute the V4 metric. `None` entries stand for
    lines that failed to parse (iter_rows yields them so the caller's count
    of consumed lines always matches the file)."""
    counts = {
        "rows_total": 0,
        "unparsable": 0,
        "non_gate": 0,
        "capability_missing": 0,
        "skipped": 0,
        "errored": 0,
        "invalid_recommendation": 0,
        "eligible": 0,
        "citing": 0,
        "changed_verdict": 0,
        "cited_proceed": 0,
        "cited_needs_human": 0,
    }
    for row in rows:
        counts["rows_total"] += 1
        if not isinstance(row, dict):
            counts["unparsable"] += 1
            continue
        event_type = row.get("event_type")
        if event_type is not None and event_type != "gate":
            counts["non_gate"] += 1
            continue
        if "surfaced_rejected_paths" not in row:
            counts["capability_missing"] += 1
            continue
        if row.get("skipped"):
            counts["skipped"] += 1
            continue
        if row.get("error") is not None:
            counts["errored"] += 1
            continue
        recommendation = row.get("recommendation")
        if recommendation not in CLASSIFIER_LABELS:
            counts["invalid_recommendation"] += 1
            continue
        counts["eligible"] += 1
        cited = _clean_int_list(row.get("cited_rejected_paths"))
        if not cited:
            continue
        counts["citing"] += 1
        if recommendation in V4_CHANGED_LABELS:
            counts["changed_verdict"] += 1
        elif recommendation == "PROCEED":
            counts["cited_proceed"] += 1
        elif recommendation == "NEEDS_HUMAN_JUDGMENT":
            counts["cited_needs_human"] += 1
    eligible = counts["eligible"]
    counts["citing_rate_pct"] = _pct(counts["citing"], eligible)
    counts["v4_firing_rate_pct"] = _pct(counts["changed_verdict"], eligible)
    counts["pass_threshold_pct"] = PASS_THRESHOLD_PCT
    # PASS is judged on the true rate, never the 2-dp display rounding
    # (docs/v4_citation_metric.md: PASS iff V4 >= 5.0).
    counts["pass_at_5pct"] = (
        None if eligible <= 0
        else 100.0 * counts["changed_verdict"] / eligible >= PASS_THRESHOLD_PCT
    )
    return counts


def iter_rows(path: Path) -> Iterator[dict | None]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            yield None
            continue
        yield obj if isinstance(obj, dict) else None


def resolve_paths(args: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for arg in args:
        p = Path(arg)
        if p.is_dir():
            # Include retention-gzipped daily logs: log_utils gzips files
            # older than the 30-day hot window, and a ~200-call V4 window can
            # span that. Missing them would silently shrink the denominator.
            files.extend(sorted([*p.glob("gate-*.log"), *p.glob("gate-*.log.gz")]))
        else:
            files.append(p)
    return files


def summarize(paths: Iterable[Path | str]) -> dict:
    files = [Path(p) for p in paths]
    rows: list[dict | None] = []
    for f in files:
        rows.extend(iter_rows(f))
    out = compute(rows)
    out["files"] = len(files)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V4 typed-rejection citation rate over gate.log rows "
        "(docs/v4_citation_metric.md)",
    )
    parser.add_argument(
        "paths", nargs="+",
        help="gate log files, or directories containing gate-*.log",
    )
    ns = parser.parse_args(argv)
    print(json.dumps(summarize(resolve_paths(ns.paths)), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
