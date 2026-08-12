"""Machine scope-mode receipt guards.

Enabling project scopes is one-way. A durable activation witness backs the
mode receipt, so a receipt that says global Shared after the explicit flip (a
partial control-root restore or file-level sync) fails closed instead of
silently reopening install-wide global access — exactly like a missing
receipt does beside project-scope state.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import paths
import project_config


@pytest.fixture
def guard_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "machine-policy-guards" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    return home


def _directory(path: Path) -> Path:
    path.mkdir(parents=True)
    return path.resolve()


def _private_scope(root: Path, kb: Path) -> project_config.ResolvedScope:
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    return project_config.authorize_scope(root, kb_dir=kb)


def _overwrite_policy_with_shared() -> None:
    """Simulate an out-of-band file replacement, not a supported transition."""
    path = project_config.machine_policy_path()
    path.unlink()
    project_config.atomic_json(
        path,
        {"format": 1, "policy": project_config.MACHINE_POLICY_SHARED},
    )


def test_explicit_activation_leaves_a_one_way_witness(guard_env: Path) -> None:
    assert project_config._explicit_activation_path().is_file()
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_EXPLICIT
    )


def test_stale_shared_policy_after_activation_fails_closed(
    guard_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    _private_scope(root, kb)
    _overwrite_policy_with_shared()

    with pytest.raises(
        project_config.ProjectConfigError,
        match="says global Shared after project scopes were activated",
    ):
        project_config.read_machine_policy()
    with pytest.raises(
        project_config.ProjectConfigError,
        match="says global Shared after project scopes were activated",
    ):
        project_config.resolve(root)
    # The stale receipt must not reopen global access for unscoped locations.
    outside = _directory(tmp_path / "unscoped-location")
    with pytest.raises(project_config.ProjectConfigError, match="global Shared"):
        project_config.resolve(outside)


def test_stale_shared_policy_is_repairable_only_toward_explicit(
    guard_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    initial = _private_scope(root, kb)
    _overwrite_policy_with_shared()

    # Ratifying the stale Shared receipt is refused ...
    with pytest.raises(
        project_config.ProjectConfigError,
        match="after project scopes were activated",
    ):
        project_config.write_machine_policy(project_config.MACHINE_POLICY_SHARED)
    # ... while upgrading it back to explicit is the documented repair.
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_EXPLICIT
    )
    repaired = project_config.resolve(root)
    assert repaired.state == project_config.MODE_LATCHED
    assert repaired.scope_id == initial.scope_id
    assert repaired.kb_dir == kb


def test_missing_policy_with_activation_witness_fails_closed(
    guard_env: Path, tmp_path: Path,
) -> None:
    # Even before any scope is created, the flip alone makes a missing
    # receipt unrecoverable through silent Shared fallback.
    project_config.machine_policy_path().unlink()
    with pytest.raises(project_config.ProjectConfigError, match="interrupted"):
        project_config.read_machine_policy()


def test_interrupted_activation_completes_by_rerunning_latch(
    guard_env: Path, tmp_path: Path,
) -> None:
    import project_mode

    # Torn state: witness durable, receipt write interrupted.
    project_config.machine_policy_path().unlink()
    with pytest.raises(
        project_config.ProjectConfigError, match="complete the activation"
    ):
        project_config.read_machine_policy()

    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    rc = project_mode.apply_latch(
        root,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(kb),
        enable_project_scopes=True,
    )

    assert rc == 0
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_EXPLICIT
    )
    assert project_config.resolve(root).state == project_config.MODE_LATCHED


def test_shared_policy_without_activation_still_reads_shared(
    guard_env: Path, tmp_path: Path,
) -> None:
    # No project scopes were ever activated: an ordinary Shared receipt in a
    # fresh control plane keeps working (untouched-install compatibility).
    project_config.machine_policy_path().unlink()
    project_config._explicit_activation_path().unlink()
    project_config.write_machine_policy(project_config.MACHINE_POLICY_SHARED)
    kb = _directory(tmp_path / "global-kb")
    (guard_env / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(kb)}) + "\n", encoding="utf-8"
    )
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_SHARED
    )
    project = _directory(tmp_path / "ordinary-project")
    target = project_config.resolve(project)
    assert target.state == project_config.MODE_LATCHED
    assert target.kb_dir == kb
