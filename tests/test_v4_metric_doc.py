"""Item-1 regression for the V4 build (KB id=4626): the operationalized
metric definition must exist and must name the exact gate.log fields it is
evaluated over — no prose judgment required to compute it (id=3948 V4,
narrowest-reading mandate).

The definition lives in docs/v4_citation_metric.md; src/v4_citation_rate.py
(item 4) implements it. This test pins the doc to the exact field and label
names so the definition cannot silently drift into something the counter does
not compute.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

DOC = ROOT / "docs" / "v4_citation_metric.md"

# Every token the definition must name verbatim: the two new gate.log id-list
# fields, the eligibility fields, and the changed-verdict label set.
REQUIRED_TOKENS = (
    "cited_rejected_paths",
    "surfaced_rejected_paths",
    "recommendation",
    '"MODIFY"',
    '"DO_NOT_PROCEED"',
    '"NEEDS_HUMAN_JUDGMENT"',
    "skipped",
    "error",
    "rejected_path.id",
    "PASS",
    "5.0",
)


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_metric_doc_exists_and_names_exact_fields():
    _assert(DOC.exists(), f"missing metric definition doc: {DOC}")
    text = DOC.read_text(encoding="utf-8")
    for token in REQUIRED_TOKENS:
        _assert(token in text, f"metric doc does not name required token {token!r}")
    # The narrowest-reading guard: the doc must define the changed-verdict
    # subset WITHOUT relying on better_next_action content (prose is never
    # persisted, id=3915 / id=3985) — it may mention the field only to record
    # the deviation from 3948's literal rubric.
    _assert(
        "better_next_action is never persisted" in text,
        "metric doc must state the prose-persistence ground for the narrowed rubric",
    )
    print("PASS test_metric_doc_exists_and_names_exact_fields")


def test_metric_doc_declares_window_rescope_deviation():
    """Codex review round 1 (2026-08-08) item-1 finding: the doc re-scopes
    3948's window denominator from "next 200 gate calls" to the first ~200
    ELIGIBLE rows, but did not list that as a declared deviation. Every
    deviation from the literal rubric must sit in the declared-deviations
    section (the 4611 pattern) — pin it there."""
    text = DOC.read_text(encoding="utf-8")
    _assert(
        "## Declared deviations" in text,
        "metric doc must keep a declared-deviations section",
    )
    deviations = text.split("## Declared deviations", 1)[1]
    _assert(
        "next 200 gate calls" in deviations,
        "the window/denominator re-scope must be DECLARED as a deviation, "
        "naming the literal 3948 window it replaces",
    )
    _assert(
        "eligible rows" in deviations,
        "the deviation must name the replacement denominator (eligible rows)",
    )
    print("PASS test_metric_doc_declares_window_rescope_deviation")


if __name__ == "__main__":
    test_metric_doc_exists_and_names_exact_fields()
    test_metric_doc_declares_window_rescope_deviation()
