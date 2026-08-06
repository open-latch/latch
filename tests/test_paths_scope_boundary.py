"""The path router must never weaken an explicit project scope."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import uuid

import pytest

import paths
import project_config


def _resolved(
    project: Path,
    *,
    state: str,
    kb_dir: Path | None,
    source: str = project_config.SOURCE_EXPLICIT,
    reason: str | None = None,
    reason_code: str | None = None,
) -> project_config.ResolvedScope:
    return project_config.ResolvedScope(
        project_root=project.resolve(),
        state=state,
        policy=project_config.POLICY_PRIVATE,
        scope_id=(
            None
            if source == project_config.SOURCE_COMPATIBILITY
            else str(uuid.uuid4())
        ),
        target_revision="1" * 32,
        revision="1" * 32,
        kb_dir=kb_dir,
        remembered_kb_dir=kb_dir,
        target_fingerprint=(
            project_config._directory_fingerprint(kb_dir)
            if kb_dir is not None
            else None
        ),
        vault_uuid=None,
        marker_path=(
            project / ".latch" / "scope.json"
            if source == project_config.SOURCE_EXPLICIT
            else None
        ),
        source=source,
        lock_key="test-scope",
        reason=reason,
        reason_code=reason_code,
    )


def _test_vault(name: str) -> Path:
    test_root = paths.validated_test_root()
    assert test_root is not None
    vault = test_root / "vaults" / name
    vault.mkdir(parents=True)
    return vault.resolve()


def _forbid_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("project_dir consulted a legacy KB fallback")

    monkeypatch.setattr(paths, "_resolve_pinned_dir", forbidden)
    monkeypatch.setattr(paths, "sanitize_cwd", forbidden)


def test_locked_scope_cannot_fall_through_to_environment_or_install_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "client"
    project.mkdir()
    ambient = _test_vault(f"ambient-{uuid.uuid4()}")
    monkeypatch.setenv("LATCH_KB_DIR", str(ambient))
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LOCKED,
            kb_dir=None,
            reason="root authorization is missing",
        ),
    )
    _forbid_fallbacks(monkeypatch)

    with pytest.raises(project_config.ProjectConfigError, match="LOCKED"):
        paths.project_dir(project)
    with pytest.raises(project_config.ProjectConfigError, match="LOCKED"):
        paths.project_dir(ambient)


def test_unlatched_scope_cannot_open_its_remembered_or_ambient_kb(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "client"
    project.mkdir()
    remembered = _test_vault(f"remembered-{uuid.uuid4()}")
    ambient = _test_vault(f"ambient-{uuid.uuid4()}")
    target = _resolved(
        project,
        state=project_config.MODE_UNLATCHED,
        kb_dir=None,
    )
    target = replace(
        target,
        remembered_kb_dir=remembered,
        target_fingerprint="3" * 64,
    )
    monkeypatch.setenv("LATCH_KB_DIR", str(ambient))
    monkeypatch.setattr(project_config, "resolve", lambda _scope: target)
    _forbid_fallbacks(monkeypatch)

    with pytest.raises(project_config.ProjectConfigError, match="UNLATCHED"):
        paths.project_dir(project)
    with pytest.raises(project_config.ProjectConfigError, match="UNLATCHED"):
        paths.db_path(project)


def test_explicit_latched_scope_selects_only_resolved_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "client"
    project.mkdir()
    selected = _test_vault(f"private-{uuid.uuid4()}")
    ambient = _test_vault(f"ambient-{uuid.uuid4()}")
    monkeypatch.setenv("LATCH_KB_DIR", str(ambient))
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LATCHED,
            kb_dir=selected,
        ),
    )
    _forbid_fallbacks(monkeypatch)

    assert paths.project_dir(project) == selected
    assert paths.db_path(project) == selected / "kb.db"


def test_compatibility_target_uses_resolvers_exact_global_pin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "legacy-project"
    project.mkdir()
    selected = _test_vault(f"global-{uuid.uuid4()}")
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LATCHED,
            kb_dir=selected,
            source=project_config.SOURCE_COMPATIBILITY,
        ),
    )
    _forbid_fallbacks(monkeypatch)

    assert paths.project_dir(project) == selected


def test_latched_without_exact_target_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "client"
    project.mkdir()
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LATCHED,
            kb_dir=None,
            reason="target disappeared",
        ),
    )
    _forbid_fallbacks(monkeypatch)

    with pytest.raises(project_config.ProjectConfigError, match="LOCKED"):
        paths.project_dir(project)


def test_authenticated_tests_reject_resolved_target_outside_vault_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "client"
    project.mkdir()
    outside = tmp_path / "outside-vault"
    outside.mkdir()
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LATCHED,
            kb_dir=outside.resolve(),
        ),
    )

    with pytest.raises(paths.UnsafeTestExecutionError, match="outside"):
        paths.project_dir(project)


def test_authenticated_unconfigured_harness_cannot_bypass_locked_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "unconfigured-test-project"
    project.mkdir()
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(paths, "KB_LOCATION_FILE", tmp_path / "missing-pin.json")
    monkeypatch.setattr(
        project_config,
        "machine_policy_path",
        lambda: tmp_path / "missing-policy.json",
    )
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LOCKED,
            kb_dir=None,
            reason="outside every authorized scope",
            reason_code=project_config.LOCK_OUTSIDE_SCOPE,
        ),
    )
    with pytest.raises(project_config.ProjectConfigError, match="LOCKED"):
        paths.project_dir(project)


def test_explicit_machine_policy_keeps_outside_scope_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project = tmp_path / "explicit-test-project"
    project.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(paths, "KB_LOCATION_FILE", tmp_path / "missing-pin.json")
    monkeypatch.setattr(project_config, "machine_policy_path", lambda: policy)
    monkeypatch.setattr(
        project_config,
        "resolve",
        lambda _scope: _resolved(
            project,
            state=project_config.MODE_LOCKED,
            kb_dir=None,
            reason="outside every authorized scope",
            reason_code=project_config.LOCK_OUTSIDE_SCOPE,
        ),
    )

    with pytest.raises(project_config.ProjectConfigError, match="LOCKED"):
        paths.project_dir(project)
