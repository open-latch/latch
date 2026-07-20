import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_readme_states_tier_tradeoffs_scope_and_evidence_boundary() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "Full — best protection" in normalized
    assert "same-topic injection" in normalized
    assert "topic-similarity check on each eligible prompt" in normalized
    assert "install-wide" in normalized
    assert "Intensity controls hook-added briefs and prompt context" in normalized
    assert "same gate check and configuration run when invoked" in normalized
    assert "does **not** promise identical evidence, catches, or outcomes" in normalized
    assert "not a retrieval-quality benchmark" in normalized
    assert "observed developer savings" in normalized
    assert "proof that the agent noticed or used the reference" in normalized
    assert "bash bin/latch_intensity_eval.sh" in normalized

    receipt = json.loads(
        (ROOT / "benchmarks" / "results" / "intensity_v1_receipt.json")
        .read_text(encoding="utf-8")
    )
    tiers = receipt["tiers"]
    for tier in ("quiet", "standard", "full"):
        row = tiers[tier]
        ratio = (
            f"{row['opportunities_with_expected_reference']}/"
            f"{row['labeled_reference_opportunities']}"
        )
        assert ratio in normalized
    chars = [f"{tiers[tier]['prompt_context_chars']:,}" for tier in (
        "quiet", "standard", "full",
    )]
    assert f"`{chars[0]}`, `{chars[1]}`, and `{chars[2]}` characters" in normalized


def test_contract_reference_keeps_intensity_out_of_gate_logic() -> None:
    text = (ROOT / "docs" / "agent-contract-reference.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())
    assert "shared contract obligations are the same" in normalized
    assert "managed contract remains static at all tiers" in normalized
    assert "live Latch read before each response" in normalized
    assert "controls hook-added briefs and prompt context" in normalized
    assert "Tier telemetry is observational" in normalized
    assert "When a supported prompt hook injects `## KB hits`" in normalized
