"""Public-safety and arithmetic checks for the decision-authority packet."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PACKET = ROOT / "benchmarks" / "decision_authority_v1"


def _load_recompute_module():
    spec = importlib.util.spec_from_file_location(
        "decision_authority_recompute", PACKET / "recompute.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_decision_authority_public_packet_recomputes_exact_counts():
    result = _load_recompute_module().recompute()
    raw = result["raw"]
    amended = result["amended"]

    assert raw["attempted_cells"] == 750
    assert raw["unique_cases"] == 25
    assert raw["arms"]["B"]["e3_adherent"] == 106
    assert raw["arms"]["B"]["e3_total"] == 125
    assert raw["arms"]["D"]["e3_adherent"] == 121
    assert raw["arms"]["D"]["e4_wrong_refusal"] == 3
    assert raw["recorded_runner_verdict"] == "INDETERMINATE"

    assert amended["attempted_cells"] == 720
    assert amended["unique_cases"] == 24
    assert amended["arms"]["B"]["e3_adherent"] == 101
    assert amended["arms"]["B"]["e3_total"] == 120
    assert amended["arms"]["D"]["e3_adherent"] == 116
    assert amended["arms"]["D"]["e3_total"] == 120
    assert amended["arms"]["B"]["e4_wrong_refusal"] == 4
    assert amended["arms"]["D"]["e4_wrong_refusal"] == 0
    assert amended["adherence_lift_d_over_b"] == 15
    assert amended["case_direction_d_over_b"] == {
        "latch_higher": 10,
        "tied": 12,
        "latch_lower": 2,
    }
    assert amended["derived_reading"] == "PASS_UNDER_DISCLOSED_AMENDMENTS"


def test_decision_authority_ledger_is_structural_and_anonymized():
    ledger = (PACKET / "results.csv").read_text(encoding="utf-8")
    blocked = (
        "/Users/",
        "latch-v7-corpus",
        "mined-",
        "elicitation_argv",
        "forbidden_paths",
        "forbidden_markers",
        "answer,",
        "context_chars",
        "workspace_residue",
    )
    for token in blocked:
        assert token not in ledger


def test_decision_authority_public_ledger_checksum_is_current():
    digest = hashlib.sha256((PACKET / "results.csv").read_bytes()).hexdigest()
    checksums = (PACKET / "checksums.txt").read_text(encoding="utf-8")
    assert f"{digest}  results.csv" in checksums
