"""Direct Codex subprocesses must prove their project binding."""
from __future__ import annotations

import subprocess

import pytest

import budget
import db
import paths
import project_config
import seed


def _bound_project(tmp_path):
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    vaults = paths.validated_test_root() / "vaults"
    kb_a = vaults / f"codex-cli-{tmp_path.name}-a"
    kb_b = vaults / f"codex-cli-{tmp_path.name}-b"
    for kb_dir in (kb_a, kb_b):
        kb_dir.mkdir(parents=True)
        project_config.mark_kb_target(kb_dir)
    binding = project_config.write_binding(
        project,
        mode=project_config.MODE_LATCHED,
        kb_dir=kb_a,
    )
    return project, kb_a, kb_b, binding


def test_codex_home_without_task_id_fails_before_bound_kb_access(
    tmp_path, monkeypatch,
):
    project, kb_a, _kb_b, _binding = _bound_project(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    with pytest.raises(db.ProjectTargetChangedError, match="cannot verify"):
        db.connect(str(project))
    with pytest.raises(seed.SeedBindingChangedError, match="cannot verify"):
        seed.snapshot_seed_binding(str(project))
    with pytest.raises(budget.BudgetCliBindingError, match="cannot verify"):
        budget._run_cli_command(
            "status", str(project), env={"CODEX_HOME": str(tmp_path)}
        )

    assert not (kb_a / "kb.db").exists()
    assert not (kb_a / "budget.json").exists()


def test_codex_backend_environment_without_task_id_fails_closed(
    tmp_path, monkeypatch,
):
    project, _kb_a, _kb_b, _binding = _bound_project(tmp_path)
    monkeypatch.setenv("LATCH_MAINTENANCE_BACKEND", "codex")

    with pytest.raises(db.ProjectTargetChangedError, match="cannot verify"):
        db.connect(str(project))
    with pytest.raises(seed.SeedBindingChangedError, match="cannot verify"):
        seed.snapshot_seed_binding(str(project))
    with pytest.raises(budget.BudgetCliBindingError, match="cannot verify"):
        budget._run_cli_command(
            "status", str(project), env={"LATCH_GATE_BACKEND": "codex"}
        )


def test_stale_codex_task_id_cannot_access_replacement_kb(
    tmp_path, monkeypatch,
):
    project, _kb_a, kb_b, binding_a = _bound_project(tmp_path)
    task_id = "old-codex-task"
    assert project_config.record_session_binding(project, task_id) == binding_a.revision
    project_config.write_binding(
        project,
        mode=project_config.MODE_LATCHED,
        kb_dir=kb_b,
    )
    monkeypatch.setenv("CODEX_THREAD_ID", task_id)

    with pytest.raises(db.ProjectTargetChangedError, match="older project KB"):
        db.connect(str(project))
    with pytest.raises(seed.SeedBindingChangedError, match="older project KB"):
        seed.snapshot_seed_binding(str(project))
    with pytest.raises(budget.BudgetCliBindingError, match="older or different"):
        budget._run_cli_command(
            "status", str(project), session_id=task_id, env={}
        )

    assert not (kb_b / "kb.db").exists()
    assert not (kb_b / "budget.json").exists()


def test_global_shared_codex_context_uses_persisted_revision(tmp_path):
    project = tmp_path / "legacy-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)

    snapshot = seed.snapshot_seed_binding(str(project))
    target = project_config.require_latched(project)
    assert target.source == project_config.SOURCE_GLOBAL
    assert snapshot.revision == target.revision
    assert snapshot.kb_dir == target.kb_dir
    # Global Shared preserves the existing KB for manual invocations;
    # an actual Codex context still needs its task receipt just like Private.
    result = budget._run_cli_command("status", str(project), env={})
    assert result["approved_today"] is False


def test_bound_manual_non_agent_access_remains_compatible(tmp_path):
    project, kb_a, _kb_b, binding = _bound_project(tmp_path)

    snapshot = seed.snapshot_seed_binding(str(project))
    assert snapshot.revision == binding.revision
    result = budget._run_cli_command("status", str(project), env={})

    assert result["approved_today"] is False
    assert not (kb_a / "kb.db").exists()
