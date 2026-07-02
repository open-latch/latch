from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import seed_report_evals  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_seed_report_eval_passes_default_bundle():
    result = seed_report_evals.run_seed_report_eval()
    _assert(result["ok"] is True, json.dumps(result, indent=2))
    summary = result["summary"]
    _assert(summary["checks"] >= 6, summary)
    _assert(summary["source_counts"]["claude"] == 1, summary)
    _assert(summary["source_counts"]["codex"] == 1, summary)
    check_ids = {row["id"] for row in result["checks"] if row["passed"]}
    for required in {
        "internal_workstream_handoff",
        "next_step_followup",
        "redis_rejected_path",
        "agent_revived_rejected_path",
        "low_confidence_agent_mistake_filtered",
    }:
        _assert(required in check_ids, f"missing passing check {required}: {result['checks']}")
    report = seed_report_evals.render_markdown(result)
    _assert("Seed Report Eval" in report, report)
    _assert("ongoing_workstream" in report, report)
    _assert("continuity notes" in report, report)
    _assert("ongoing workstreams" not in report, report)
    _assert("agent alignment check" in report, report)
    print("PASS seed_report_eval_passes_default_bundle")


def test_seed_report_eval_cli_writes_json():
    out = Path(tempfile.mkdtemp(prefix="seed-report-eval-json-")) / "report.json"
    rc = seed_report_evals.main(["--format", "json", "--output", str(out)])
    _assert(rc == 0, f"expected success rc, got {rc}")
    payload = json.loads(out.read_text(encoding="utf-8"))
    _assert(payload["ok"] is True, payload)
    _assert(payload["summary"]["synthetic_llm_candidate_count"] == 1, payload["summary"])
    print("PASS seed_report_eval_cli_writes_json")


if __name__ == "__main__":
    test_seed_report_eval_passes_default_bundle()
    test_seed_report_eval_cli_writes_json()
    print("\nAll seed report eval tests pass.")
