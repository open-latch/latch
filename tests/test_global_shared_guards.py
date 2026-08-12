"""Global Shared mode guards that lost coverage when the compatibility-binding
suite was deleted (563786d).

Two protections for the default mode every non-consultant runs:

* conflicting global env aliases must fail closed rather than pick a vault;
* in-place vault substitution at the pinned path is currently ADOPTED —
  a characterization of the accepted v1 residual (shared mode stores no
  expected vault UUID), so a behavior change here is loud, not silent.
"""
from __future__ import annotations

import shutil

import pytest

import db
import paths
import project_config


def test_conflicting_global_environment_aliases_lock_shared_scope(
    compatibility_scope_env: dict, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = compatibility_scope_env["vault"]
    replacement = vault.parent / f"{vault.name}-environment-conflict"
    replacement.mkdir()
    project = tmp_path / "env-conflict-project"
    project.mkdir()
    monkeypatch.setenv("LATCH_KB_DIR", str(vault))
    monkeypatch.setenv("CLAUDE_KB_DIR", str(replacement))

    target = project_config.resolve(project)

    assert target.state == project_config.MODE_LOCKED
    assert target.kb_dir is None
    assert target.reason_code == project_config.LOCK_GLOBAL_PIN_CHANGED


def _pin(home, vault) -> None:
    import json

    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(vault)}) + "\n",
        encoding="utf-8",
    )
    refresh = getattr(paths, "refresh_pinned_dir", None)
    if refresh is not None:
        refresh()


def test_global_continuity_epoch_rotates_shared_revisions_only_when_present(
    compatibility_scope_env: dict, tmp_path,
) -> None:
    """Absent epoch file preserves existing installs' revisions; every bump
    permanently rotates the install-wide Shared revision (and with it every
    session receipt recorded before an OFF/ON cycle)."""
    project = tmp_path / "epoch-project"
    project.mkdir()
    assert not project_config.global_continuity_epoch_path().exists()
    before = project_config.resolve(project)
    assert before.state == project_config.MODE_LATCHED
    assert project_config.resolve(project).revision == before.revision

    session_revision = project_config.record_session_binding(project, "pre-cycle")
    assert session_revision == before.revision

    project_config.bump_global_continuity_epoch()
    first_bump = project_config.resolve(project)
    assert first_bump.state == project_config.MODE_LATCHED
    assert first_bump.kb_dir == before.kb_dir
    assert first_bump.revision != before.revision
    assert project_config.current_session_revision(project, "pre-cycle") is None

    project_config.bump_global_continuity_epoch()
    second_bump = project_config.resolve(project)
    assert second_bump.revision != first_bump.revision


def test_inplace_vault_substitution_is_adopted_in_shared_mode(
    compatibility_scope_env: dict, tmp_path,
) -> None:
    """Characterization, not an endorsement: replacing the shared KB's kb.db
    in place (same directory, same pin) is silently adopted because shared
    mode compares the live vault UUID against nothing stored. Accepted v1
    residual — machine-registered Private vaults ARE refused by the
    reservation check; only never-registered vaults can be substituted. If
    this test starts failing because substitution now fails closed, that is
    an intentional upgrade: replace this test with the fail-closed assertion.
    """
    home = compatibility_scope_env["home"]
    vault_a = compatibility_scope_env["vault"]
    root = tmp_path / "daily-project"
    root.mkdir()

    first = db.connect(str(root))
    identity_a = first._kb_vault_identity
    first.close()
    before = project_config.resolve(root)
    assert before.state == project_config.MODE_LATCHED
    assert before.vault_uuid == identity_a.vault_uuid

    vault_b = vault_a.parent / f"{vault_a.name}-replacement"
    vault_b.mkdir()
    _pin(home, vault_b)
    second = db.connect(str(root))
    identity_b = second._kb_vault_identity
    second.close()
    assert identity_b.vault_uuid != identity_a.vault_uuid
    _pin(home, vault_a)

    shutil.copyfile(vault_b / "kb.db", vault_a / "kb.db")

    after = project_config.resolve(root)
    assert after.state == project_config.MODE_LATCHED
    assert after.revision == before.revision
    assert after.vault_uuid == identity_b.vault_uuid
