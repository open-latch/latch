from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import gate  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_unlatched_gate_findings_do_not_claim_kb_evidence():
    findings = gate.format_gate_findings(
        gate.unlatched_verdict(),
        [],
        gate_status="SKIPPED",
    )
    guidance = findings.get("display_guidance", "")
    _assert("gate was skipped" in guidance, guidance)
    _assert("currently UNLATCHED" in guidance, guidance)
    _assert("ran the gate" not in guidance, guidance)
    _assert("cited KB evidence" not in guidance, guidance)
    _assert(
        "No KB evidence was read" in findings.get("receipt", {}).get("authority", ""),
        findings,
    )
    print("PASS unlatched_gate_findings_do_not_claim_kb_evidence")


if __name__ == "__main__":
    test_unlatched_gate_findings_do_not_claim_kb_evidence()
