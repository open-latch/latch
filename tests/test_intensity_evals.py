from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import intensity_evals  # noqa: E402


def test_frozen_intensity_envelope_has_expected_tradeoff() -> None:
    fixture = ROOT / "benchmarks" / "fixtures" / "intensity_v1.jsonl"
    result = intensity_evals.run(
        intensity_evals.load_events(fixture), fixture_path=fixture
    )
    assert result["labeled_reference_opportunities"] == 5
    assert result["relative_rebuild_risk_weight"] == 17

    quiet = result["tiers"]["quiet"]
    standard = result["tiers"]["standard"]
    full = result["tiers"]["full"]
    assert quiet["relative_risk_weight_with_expected_reference"] == 0
    assert standard["relative_risk_weight_with_expected_reference"] == 6
    assert full["relative_risk_weight_with_expected_reference"] == 17
    assert quiet["topic_similarity_checks"] == 0
    assert standard["topic_similarity_checks"] == 7
    assert full["topic_similarity_checks"] == 7
    assert standard["vector_retrieval_runs"] == 6
    assert full["vector_retrieval_runs"] == 7
    assert quiet["prompt_context_chars"] == 0
    assert 0 < standard["prompt_context_chars"] < full["prompt_context_chars"]
    assert (
        result["full_vs_standard"]
        ["additional_relative_risk_weight_with_reference"]
        == 11
    )
    assert "not measured hours" in result["claim_boundary"]


def test_json_cli_receipt_is_machine_readable(tmp_path: Path, capsys) -> None:
    fixture = ROOT / "benchmarks" / "fixtures" / "intensity_v1.jsonl"
    assert intensity_evals.main([
        "--fixture", str(fixture), "--format", "json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["suite"] == "intensity_v1"
    assert len(payload["fixture_sha256"]) == 64
    assert payload["gate_invariant"].startswith("All tiers keep the same gate")


def test_checked_in_receipt_matches_current_fixture_and_policy() -> None:
    fixture = ROOT / "benchmarks" / "fixtures" / "intensity_v1.jsonl"
    actual = intensity_evals.run(
        intensity_evals.load_events(fixture), fixture_path=fixture
    )
    receipt = json.loads(
        (ROOT / "benchmarks" / "results" / "intensity_v1_receipt.json")
        .read_text(encoding="utf-8")
    )
    assert receipt["fixture_sha256"] == actual["fixture_sha256"]
    for tier in intensity_evals.TIERS:
        expected = receipt["tiers"][tier]
        current = actual["tiers"][tier]
        assert expected == {
            "labeled_reference_opportunities": current[
                "labeled_reference_opportunities"
            ],
            "opportunities_with_expected_reference": current[
                "opportunities_with_expected_reference"
            ],
            "prompt_context_chars": current["prompt_context_chars"],
            "relative_rebuild_risk_weight": current[
                "relative_rebuild_risk_weight"
            ],
            "relative_risk_weight_with_expected_reference": current[
                "relative_risk_weight_with_expected_reference"
            ],
            "topic_similarity_checks": current["topic_similarity_checks"],
            "vector_retrieval_runs": current["vector_retrieval_runs"],
        }
