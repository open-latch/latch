"""Golden contract and public-tree privacy tests for the A2 consumer seam."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import subprocess
import sys


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_CONSUMER = _ROOT / "bin" / "predicate_consumer.py"


def _module(name: str):
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module(f"latch.gate.{name}")
    finally:
        sys.path.remove(str(_SRC))


@dataclass(frozen=True)
class SyntheticProjection:
    engine: str
    policy_domain_id: str
    binding_rows: tuple[dict[str, object], ...]
    advisory_rows: tuple[dict[str, object], ...]
    reason_counts: dict[str, int]
    freshness_token: str


def _projection() -> SyntheticProjection:
    return SyntheticProjection(
        engine="predicate-policy-projection-v1",
        policy_domain_id="synthetic-project-golden",
        binding_rows=(
            {
                "rejected_path_id": 41,
                "node_id": 741,
                "option": "synthetic rejected option 41",
                "reason": "synthetic rejection reason 41",
                "ratifier": "synthetic-founder",
                "decided_at": "2026-08-27T00:00:00Z",
                "scope_predicate": "file:src/synthetic_widget.py",
                "source": "declared",
                "policy_domain_id": "synthetic-project-golden",
                "owner_kind": "decision",
                "owner_status": "canonical",
                "owner_updated_at": "2026-08-27T00:00:00Z",
                "latest_ratification_id": 941,
                "latest_ratification_action": "ratify",
                "latest_ratification_ratifier": "synthetic-founder",
                "latest_ratification_decided_at": "2026-08-27T00:00:00Z",
                "latest_ratification_source": "declared",
                "superseder_ids": (),
                "reconciler_ids": (),
                "authority_basis": "synthetic-ratified-declared",
                "classification": "binding",
                "reason_codes": (),
            },
        ),
        advisory_rows=(),
        reason_counts={},
        freshness_token="synthetic-projection-golden",
    )


def _action(project_root: Path) -> dict[str, object]:
    return {
        "policy_domain_id": "synthetic-project-golden",
        "project_root": str(project_root),
        "cwd": str(project_root),
        "tool_name": "synthetic.write",
        "proposed_file_paths": ["src/synthetic_widget.py"],
        "diff_paths": [],
        "staged_paths": [],
        "import_names": [],
        "api_names": [],
        "evidence_complete": True,
        "evidence_provenance": ["synthetic-test"],
    }


def test_reference_consumer_matches_library_and_golden_contract(tmp_path):
    consumer = _module("predicate_consumer")
    snapshot = _module("predicate_snapshot")
    token_path = tmp_path / "source-generation.txt"
    token_path.write_text("generation-golden\n", encoding="utf-8")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()

    document = snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-project-golden",
        projector=_projection,
        freshness_token_path=token_path,
    )
    result = consumer.evaluate_policy(target, _action(project_root))

    assert set(result.verdict) == {"engine", "decision", "llm_calls", "matches"}
    assert result.verdict == {
        "engine": "predicate-v1",
        "decision": "block",
        "llm_calls": 0,
        "matches": [
            {
                "rejected_path_id": 41,
                "node_id": 741,
                "option": "synthetic rejected option 41",
                "predicate": "file:src/synthetic_widget.py",
                "reason": "synthetic rejection reason 41",
                "source": "declared",
            }
        ],
    }
    assert result.receipt == {
        "contract": "predicate-policy-receipt-v1",
        "engine": "predicate-v1",
        "decision": "block",
        "llm_calls": 0,
        "policy_domain_id": "synthetic-project-golden",
        "policy_digest": document["digest"],
        "freshness_token": "synthetic-projection-golden",
        "binding_rows": 1,
        "binding_compiled": 1,
        "advisory_rows": 0,
        "uncompilable_rows": 0,
        "matched_rejected_path_ids": [41],
        "matched_node_ids": [741],
        "reason_codes": [],
        "advisory_reason_counts": {},
    }

    proc = subprocess.run(
        [sys.executable, "-I", str(_CONSUMER), str(target)],
        input=json.dumps(_action(project_root)),
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(proc.stdout) == result.receipt
    assert proc.stderr == ""
    serialized_receipt = proc.stdout
    for private_text in (
        "synthetic rejected option",
        "synthetic rejection reason",
        "file:src/synthetic_widget.py",
        str(project_root),
    ):
        assert private_text not in serialized_receipt


def test_public_tree_contains_no_policy_artifact_vault_text_or_private_path():
    public_surfaces = (
        _SRC / "latch" / "gate" / "predicate_snapshot.py",
        _SRC / "latch" / "gate" / "predicate_consumer.py",
        _CONSUMER,
        _ROOT / "docs" / "predicate_verdict_v1.md",
        _ROOT / "docs" / "predicate_policy_snapshot_v1.md",
    )
    forbidden_absolute_prefix = "/" + "Users" + "/"
    forbidden_vault_text = "REAL" + "_VAULT_REJECTION_TEXT"
    for path in public_surfaces:
        text = path.read_text(encoding="utf-8")
        assert forbidden_absolute_prefix not in text, path
        assert forbidden_vault_text not in text, path

    artifacts = [
        path
        for path in _ROOT.rglob("*")
        if path.is_file()
        and (
            path.name.endswith(".predicate-policy.snapshot.json")
            or path.name == "policy.snapshot.json"
        )
        and ".git" not in path.parts
    ]
    assert artifacts == []
