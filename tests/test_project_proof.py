"""Contract-v2.6 regressions for opaque, vault-keyed project comparison."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import paths  # noqa: E402
import project_proof  # noqa: E402


_VAULT_KEY = bytes.fromhex("7b" * 32)


def _context(epoch: str = "epoch-1") -> project_proof.ProjectProofContext:
    return project_proof.ProjectProofContext.from_vault_key(
        _VAULT_KEY,
        key_epoch=epoch,
        vault_id="44444444-4444-4444-8444-444444444444",
    )


def test_lossy_sanitize_collision_has_distinct_project_proofs(tmp_path: Path) -> None:
    """The exact PR #73 collision class must not compare equal."""
    left = tmp_path / "repo-a" / "module"
    right = tmp_path / "repo" / "a-module"
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    assert paths.sanitize_cwd(left) == paths.sanitize_cwd(right)

    context = _context()
    left_proof = context.prove(left)
    right_proof = context.prove(right)
    assert left_proof["fingerprint"] != right_proof["fingerprint"]
    assert (
        project_proof.compare_project_proofs(left_proof, right_proof)
        == project_proof.PROJECT_FOREIGN
    )


def test_canonical_aliases_compare_equal(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    project.mkdir()
    alias = project / "nested" / ".."
    context = _context()
    assert project_proof.compare_project_proofs(
        context.prove(project), context.prove(alias),
    ) == project_proof.PROJECT_MATCH

    windows_native = context.prove(r"C:\\Users\\me\\repo")
    windows_mingw = context.prove("/c/Users/me/repo")
    assert project_proof.compare_project_proofs(
        windows_native, windows_mingw,
    ) == project_proof.PROJECT_MATCH


def test_key_rotation_is_loss_not_foreign(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    old = _context("epoch-1").prove(project)
    new = _context("epoch-2").prove(project)
    assert (
        project_proof.compare_project_proofs(old, new)
        == project_proof.PROJECT_KEY_EPOCH_MISMATCH
    )


def test_same_epoch_different_vault_key_is_loss_not_foreign(tmp_path: Path) -> None:
    project = tmp_path / "workspace"
    left = project_proof.ProjectProofContext.from_vault_key(
        bytes.fromhex("11" * 32), key_epoch="epoch-1", vault_id="vault-left"
    ).prove(project)
    right = project_proof.ProjectProofContext.from_vault_key(
        bytes.fromhex("22" * 32), key_epoch="epoch-1", vault_id="vault-right"
    ).prove(project)
    assert left["key_id"] != right["key_id"]
    assert (
        project_proof.compare_project_proofs(left, right)
        == project_proof.PROJECT_KEY_EPOCH_MISMATCH
    )


def test_proof_is_versioned_and_contains_no_recoverable_path(tmp_path: Path) -> None:
    project = tmp_path / "private-customer-name" / "secret-repo"
    proof = _context().prove(project)
    encoded = json.dumps(proof, sort_keys=True)
    assert proof == {
        "version": project_proof.PROJECT_PROOF_VERSION,
        "key_epoch": "epoch-1",
        "key_id": proof["key_id"],
        "fingerprint": proof["fingerprint"],
    }
    assert len(proof["key_id"]) == 64
    assert len(proof["fingerprint"]) == 64
    assert str(project) not in encoded
    assert "private-customer-name" not in encoded
    assert "secret-repo" not in encoded


def test_vault_identity_key_material_is_not_emitted(tmp_path: Path) -> None:
    identity = SimpleNamespace(
        vault_uuid="44444444-4444-4444-8444-444444444444",
        registry_fingerprint="42" * 32,
    )
    proof = project_proof.ProjectProofContext.from_vault_identity(
        identity, key_epoch="epoch-7",
    ).prove(tmp_path)
    encoded = json.dumps(proof, sort_keys=True)
    assert identity.vault_uuid not in encoded
    assert identity.registry_fingerprint not in encoded
