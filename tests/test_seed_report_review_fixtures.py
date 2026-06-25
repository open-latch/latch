from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

import seed_report_review_fixtures as fixtures  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_seed_report_review_fixtures_generate_review_artifacts():
    out_dir = Path(tempfile.mkdtemp(prefix="latch-seed-review-test-"))
    shutil.rmtree(out_dir)
    try:
        rc = fixtures.main(["--out-dir", str(out_dir), "--llm", "no"])
        _assert(rc == 0, f"runner should exit 0, got {rc}")

        manifest = json.loads((out_dir / "expected_cases.json").read_text(encoding="utf-8"))
        report = json.loads((out_dir / "seed-report.json").read_text(encoding="utf-8"))
        text = (out_dir / "seed-report.txt").read_text(encoding="utf-8")

        _assert(len(manifest) == 6, f"expected six review cases, got {len(manifest)}")
        _assert(any(case["case_id"] == "negative-user-changed-mind" for case in manifest),
                f"missing changed-mind negative case: {manifest}")
        _assert(any(case["source"] == "claude" for case in manifest)
                and any(case["source"] == "codex" for case in manifest),
                f"fixtures should cover Claude and Codex: {manifest}")
        _assert(report["source"] == "both", f"runner should exercise combined source mode: {report}")
        _assert(report["source_counts"]["claude"] == 3 and report["source_counts"]["codex"] == 3,
                f"expected balanced source counts: {report['source_counts']}")
        _assert("Seed report:" in text and "Agent alignment check" in text,
                f"text report should be reviewable: {text}")
        _assert("Direction and priorities:" in text and "Agent behavior:" in text,
                f"alignment check should include synthesis and contradiction subsections: {text}")
        _assert("confidence=" not in text and "Confidence:" not in text,
                f"text report should not expose numeric confidence scores: {text}")
        _assert("confidence" not in json.dumps(report),
                f"json report should not expose confidence fields: {report}")
        _assert("injected context" not in text and "Always avoid Redis" not in text,
                f"injected KB-like context should not become seed evidence: {text}")
        _assert((out_dir / "README.txt").exists(), "runner should write review instructions")
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    print("PASS seed_report_review_fixtures_generate_review_artifacts")


if __name__ == "__main__":
    test_seed_report_review_fixtures_generate_review_artifacts()
    print("\nAll seed report review fixture tests pass.")
