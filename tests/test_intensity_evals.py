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


def test_candidate_selection_matches_live_hook_policy() -> None:
    candidates = [
        {"id": 1, "kind": "fact", "score": 0.61},
        {"id": 2, "kind": "decision", "score": 0.91},
        {"id": 3, "kind": "idea", "score": 0.99},
        {"id": 4, "kind": "progress", "score": 0.59},
        {"id": 5, "kind": "fact", "score": 0.87},
        {"id": 6, "kind": "decision", "score": 0.60},
        {"id": 7, "kind": "fact", "score": 0.605},
    ]
    all_eligible = intensity_evals.prompt_hook._select_candidates(
        candidates,
        {2},
        sim_floor=0.60,
        max_inject=4,
    )
    assert [row["id"] for row in all_eligible] == [5, 1, 7, 6]

    chosen = intensity_evals.prompt_hook._select_candidates(
        candidates, {2}, sim_floor=0.60, max_inject=2,
    )
    assert [row["id"] for row in chosen] == [5, 1]


def test_checked_in_receipt_matches_current_fixture_and_policy() -> None:
    fixture = ROOT / "benchmarks" / "fixtures" / "intensity_v1.jsonl"
    actual = intensity_evals.run(
        intensity_evals.load_events(fixture), fixture_path=fixture
    )
    receipt = json.loads(
        (ROOT / "benchmarks" / "results" / "intensity_v1_receipt.json")
        .read_text(encoding="utf-8")
    )
    assert receipt == intensity_evals.portable_receipt(actual)
    assert "fixture" not in receipt
    assert all("events" not in row for row in receipt["tiers"].values())


def test_write_receipt_cli_emits_exact_portable_artifact(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    fixture = ROOT / "benchmarks" / "fixtures" / "intensity_v1.jsonl"
    destination = tmp_path / "results" / "receipt.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = intensity_evals.os.replace

    def replace(source, target) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(intensity_evals.os, "replace", replace)
    assert intensity_evals.main([
        "--fixture", str(fixture), "--write-receipt", str(destination),
    ]) == 0

    actual = intensity_evals.run(
        intensity_evals.load_events(fixture), fixture_path=fixture
    )
    rendered = destination.read_text(encoding="utf-8")
    assert json.loads(rendered) == (
        intensity_evals.portable_receipt(actual)
    )
    checked_in = (
        ROOT / "benchmarks" / "results" / "intensity_v1_receipt.json"
    )
    assert rendered == checked_in.read_text(encoding="utf-8")
    assert destination.read_bytes() == checked_in.read_bytes()
    assert str(fixture) not in rendered
    assert '"events"' not in rendered
    assert rendered.endswith("\n")
    assert replacements and replacements[-1][1] == destination
    assert replacements[-1][0].parent == destination.parent
    assert "Wrote portable receipt" in capsys.readouterr().out
