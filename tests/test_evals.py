from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import _isolation  # noqa: F401,E402
import evals  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_default_wedge_fixture_passes():
    cases = evals.load_cases([evals.DEFAULT_FIXTURE])
    result = evals.run_cases(cases)
    _assert(result["ok"] is True, json.dumps(result, indent=2))
    _assert(result["summary"]["cases"] >= 7, result["summary"])
    _assert(
        result["summary"]["required_retrieval_rate"] == 1.0,
        result["summary"],
    )
    _assert("latch_full" in result["modes"], result["modes"])
    _assert("active_seed_graph" in result["modes"], result["modes"])
    _assert("stale_search" in result["modes"], result["modes"])
    _assert("memory_like" in result["modes"], result["modes"])
    _assert(
        result["modes"]["latch_full"]["passed"]
        > result["modes"]["memory_like"]["passed"],
        result["modes"],
    )
    _assert(result["summary"]["latch_only_wins"] >= 1, result["summary"])
    comparison = result["summary"]["comparisons"]["latch_full_vs_memory_like"]
    _assert(comparison["primary_only_wins"] >= 1, comparison)
    _assert(comparison["net_wins"] >= 1, comparison)
    print("PASS default_wedge_fixture_passes")


def test_markdown_report_names_memory_trap():
    cases = evals.load_cases([evals.DEFAULT_FIXTURE])
    result = evals.run_cases(cases)
    report = evals.render_markdown(result)
    _assert("Latch Wedge Benchmark" in report, report)
    _assert("not a memory benchmark" in report, report)
    _assert("visible gate receipts" in report, report)
    _assert("Memory trap:" in report, report)
    _assert("Memory-like baseline:" in report, report)
    _assert("Latch-only wins vs memory-like baseline" in report, report)
    _assert("## Comparisons" in report, report)
    _assert("active_seed_graph" in report, report)
    _assert("Source note:" in report, report)
    print("PASS markdown_report_names_memory_trap")


def test_single_mode_runs_without_baseline():
    cases = evals.load_cases([evals.DEFAULT_FIXTURE])
    result = evals.run_cases(cases, modes=["latch_full"])
    _assert(result["ok"] is True, result)
    _assert(list(result["modes"].keys()) == ["latch_full"], result["modes"])
    _assert(result["summary"]["latch_only_wins"] == 0, result["summary"])
    print("PASS single_mode_runs_without_baseline")


def test_missing_required_ref_fails_case():
    fixture = Path(tempfile.mkdtemp(prefix="latch-eval-test-")) / "fixture.jsonl"
    fixture.write_text(
        json.dumps({
            "id": "missing-ref",
            "suite": "test",
            "kind": "rejected_path_enforcement",
            "query": "revive the unseeded rejected path",
            "nodes": [{
                "ref": "decision",
                "kind": "decision",
                "title": "Keep the current plan",
                "body": "Decision: keep the current plan.",
                "status": "canonical",
            }, {
                "ref": "noise",
                "kind": "decision",
                "title": "Unrelated implementation note",
                "body": "This note talks about unrelated implementation work.",
                "status": "canonical",
            }],
            "expect": {
                "must_retrieve": ["noise"],
                "supporting_phrases": [],
            },
        }) + "\n",
        encoding="utf-8",
    )
    cases = evals.load_cases([fixture])
    result = evals.run_cases(cases, seed_top_k=1)
    _assert(result["ok"] is False, result)
    _assert(result["summary"]["failed"] == 1, result["summary"])
    _assert(result["cases"][0]["missing_refs"] == ["noise"], result["cases"][0])
    print("PASS missing_required_ref_fails_case")


if __name__ == "__main__":
    test_default_wedge_fixture_passes()
    test_markdown_report_names_memory_trap()
    test_single_mode_runs_without_baseline()
    test_missing_required_ref_fails_case()
    print("\nAll eval tests pass.")
