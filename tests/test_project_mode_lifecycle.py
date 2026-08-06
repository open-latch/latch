"""Canonical product lifecycle tests for the public latch/unlatch scope flow."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import paths
import project_config
import project_mode


_REAL_DISABLE_INSTRUCTIONS = project_mode._disable_instructions
_REAL_ENABLE_INSTRUCTIONS = project_mode._enable_instructions


@pytest.fixture
def lifecycle_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "project-mode-lifecycle" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    for name in (
        "CLAUDE_KB_HOME",
        "LATCH_KB_DIR",
        "CLAUDE_KB_DIR",
        "LATCH_UNLATCHED",
        "LATCH_DISABLE",
        "CLAUDE_KB_DISABLE",
        "LATCH_DISABLE_WRITE",
        "CLAUDE_KB_DISABLE_WRITE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(project_mode, "_disable_instructions", lambda _roots: [])
    monkeypatch.setattr(project_mode, "_enable_instructions", lambda _roots: [])
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    return home


def _directory(path: Path) -> Path:
    path.mkdir(parents=True)
    return path.resolve()


def _pin_global(home: Path, tmp_path: Path) -> Path:
    kb = _directory(tmp_path / "global-kb")
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(kb)}) + "\n",
        encoding="utf-8",
    )
    return kb


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_status_is_read_only_and_commands_require_exact_names_and_confirmation(
    lifecycle_env: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _directory(tmp_path / "plain-client")
    before_root = _file_snapshot(root)
    before_control = _file_snapshot(project_config.control_root())

    assert project_mode.main([
        "status", "--project", str(root), "--json",
    ]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == project_config.MODE_LOCKED
    assert payload["selected_root"] == str(root)
    assert payload["effective_root"] == str(root)
    assert payload["policy"] is None
    assert payload["kb_dir"] is None
    assert _file_snapshot(root) == before_root
    assert _file_snapshot(project_config.control_root()) == before_control

    assert project_mode.main([
        "latch", "--project", str(root), "--confirm", "yes", "--private",
        "--new-kb",
    ]) == 2
    assert project_mode.main([
        "unlatch", "--project", str(root), "--confirm", "yes",
    ]) == 2
    assert project_config.resolve(root).state == project_config.MODE_LOCKED
    assert not (root / project_config.PORTABLE_DIR_NAME).exists()

    with pytest.raises(SystemExit):
        project_mode.parse_args(["latch", "--project", str(root), "--shared"])
    for alias in ("on", "off", "set"):
        with pytest.raises(SystemExit):
            project_mode.parse_args([alias])
    assert project_mode.parse_args([
        "latch", "--project", str(root), "--confirm", "latch", "--shared",
    ]).command == "latch"
    assert project_mode.parse_args([
        "unlatch", "--project", str(root), "--confirm", "unlatch",
    ]).command == "unlatch"


def test_plain_non_git_root_requires_shared_or_private_choice(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    root = _directory(tmp_path / "not-a-repository")
    assert not (root / ".git").exists()

    assert project_mode.main([
        "latch", "--project", str(root), "--confirm", "latch",
    ]) == 2
    assert project_config.resolve(root).state == project_config.MODE_LOCKED

    assert project_mode.main([
        "latch", "--project", str(root), "--confirm", "latch", "--shared",
    ]) == 0
    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LATCHED
    assert target.policy == project_config.POLICY_SHARED
    assert target.project_root == root
    assert target.kb_dir == global_kb
    assert (root / ".latch" / "scope.json").is_file()
    assert not (root / ".git").exists()


def test_nearest_scope_inheritance_and_private_child_leave_sibling_shared(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    outer = _directory(tmp_path / "consulting")
    child = _directory(outer / "client-a")
    nested = _directory(child / "service")
    sibling = _directory(outer / "client-b")
    private_kb = _directory(tmp_path / "client-a-vault")
    project_mode.apply_latch(outer, policy=project_config.POLICY_SHARED)

    inherited = project_mode.status_payload(nested)
    assert inherited["inherited"] is True
    assert inherited["effective_root"] == str(outer)
    assert inherited["policy"] == project_config.POLICY_SHARED
    assert inherited["kb_dir"] == str(global_kb)

    project_mode.apply_latch(
        child,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(private_kb),
    )
    private = project_config.resolve(nested)
    other = project_config.resolve(sibling)
    assert private.project_root == child
    assert private.policy == project_config.POLICY_PRIVATE
    assert private.kb_dir == private_kb
    assert other.project_root == outer
    assert other.policy == project_config.POLICY_SHARED
    assert other.kb_dir == global_kb


def test_downstream_unlatch_is_only_an_off_boundary_and_relatch_restores_parent(
    lifecycle_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "paused-child")
    nested = _directory(child / "nested")
    sibling = _directory(outer / "active-sibling")
    parent = project_mode.apply_latch(outer, policy=project_config.POLICY_SHARED)
    assert parent == 0
    parent_target = project_config.resolve(outer)
    vault_parent = project_mode._private_vault_parent()
    vaults_before = set(vault_parent.iterdir()) if vault_parent.exists() else set()
    originals = {
        child / "CLAUDE.md": b"client claude rules\n",
        child / "AGENTS.md": b"client agent rules\n",
    }
    for path, body in originals.items():
        path.write_bytes(body)
    monkeypatch.setattr(
        project_mode,
        "_disable_instructions",
        _REAL_DISABLE_INSTRUCTIONS,
    )
    monkeypatch.setattr(
        project_mode,
        "_enable_instructions",
        _REAL_ENABLE_INSTRUCTIONS,
    )

    project_mode.apply_unlatch(child)
    off = project_config.resolve(nested)
    assert off.state == project_config.MODE_UNLATCHED
    assert off.project_root == child
    assert off.scope_id == parent_target.scope_id
    assert off.remembered_kb_dir == global_kb
    assert all(path.read_bytes() != body for path, body in originals.items())
    assert project_config.resolve(sibling).state == project_config.MODE_LATCHED
    assert project_config.resolve(outer).state == project_config.MODE_LATCHED
    assert not (child / ".latch" / "scope.json").exists()
    vaults_after = set(vault_parent.iterdir()) if vault_parent.exists() else set()
    assert vaults_after == vaults_before

    project_mode.apply_latch(child)
    resumed = project_config.resolve(nested)
    assert resumed.state == project_config.MODE_LATCHED
    assert resumed.project_root == outer
    assert resumed.scope_id == parent_target.scope_id
    assert resumed.kb_dir == global_kb
    for path, body in originals.items():
        assert path.read_bytes() == body
    assert not project_config.local_binding_path(child).exists()


def test_changed_parent_off_boundary_can_be_replaced_with_private_scope(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    _pin_global(lifecycle_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "client")
    private_kb = _directory(tmp_path / "client-vault")
    project_mode.apply_latch(outer, policy=project_config.POLICY_SHARED)
    project_mode.apply_unlatch(child)
    project_config.set_scope_mode(outer, project_config.MODE_UNLATCHED)
    project_config.set_scope_mode(outer, project_config.MODE_LATCHED)

    with pytest.raises(project_config.ProjectConfigError, match="remembered parent"):
        project_mode.apply_latch(child)

    project_mode.apply_latch(
        child,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(private_kb),
    )
    target = project_config.resolve(child)
    assert target.state == project_config.MODE_LATCHED
    assert target.project_root == child
    assert target.policy == project_config.POLICY_PRIVATE
    assert target.kb_dir == private_kb


def test_interrupted_off_replacement_retries_same_explicit_choice(
    lifecycle_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_global(lifecycle_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "client")
    project_mode.apply_latch(outer, policy=project_config.POLICY_SHARED)
    project_mode.apply_unlatch(child)
    original_unlink = project_config.durable_unlink

    def interrupt(_path: Path) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(project_config, "durable_unlink", interrupt)
    with pytest.raises(OSError, match="simulated interruption"):
        project_mode.apply_latch(child, policy=project_config.POLICY_SHARED)
    interrupted = project_config.resolve(child)
    assert interrupted.state == project_config.MODE_LOCKED
    assert (
        interrupted.reason_code
        == project_config.LOCK_INTERRUPTED_OFF_REPLACEMENT
    )
    scope_id = json.loads(
        (child / ".latch" / "scope.json").read_text(encoding="utf-8")
    )["scope_id"]

    monkeypatch.setattr(project_config, "durable_unlink", original_unlink)
    project_mode.apply_latch(child, policy=project_config.POLICY_SHARED)
    target = project_config.resolve(child)
    assert target.state == project_config.MODE_LATCHED
    assert target.project_root == child
    assert target.policy == project_config.POLICY_SHARED
    assert target.scope_id == scope_id


def test_project_scopes_require_one_explicit_opt_in_and_then_fail_closed(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    project_config.machine_policy_path().unlink()
    project_config.write_machine_policy(project_config.MACHINE_POLICY_SHARED)
    root = _directory(tmp_path / "private-client")
    sibling = _directory(tmp_path / "unselected-client")
    private_kb = _directory(tmp_path / "private-vault")

    before = project_config.resolve(root)
    assert before.source == project_config.SOURCE_GLOBAL
    assert before.kb_dir == global_kb

    with pytest.raises(project_config.ProjectConfigError, match="explicitly enable"):
        project_mode.apply_latch(
            root,
            policy=project_config.POLICY_PRIVATE,
            kb_dir=str(private_kb),
        )
    assert project_config.read_machine_policy() == project_config.MACHINE_POLICY_SHARED
    assert project_config.resolve(root).source == project_config.SOURCE_GLOBAL

    project_mode.apply_latch(
        root,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(private_kb),
        enable_project_scopes=True,
    )
    target = project_config.resolve(root)
    assert project_config.read_machine_policy() == project_config.MACHINE_POLICY_EXPLICIT
    assert target.state == project_config.MODE_LATCHED
    assert target.policy == project_config.POLICY_PRIVATE
    assert target.kb_dir == private_kb
    assert project_config.resolve(sibling).state == project_config.MODE_LOCKED


def test_private_scope_selects_existing_vault_without_mutating_it(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "existing-vault")
    (kb / "canary.bin").write_bytes(b"existing\x00vault")
    before = _file_snapshot(kb)

    assert project_mode.main([
        "latch", "--project", str(root), "--confirm", "latch", "--private",
        "--kb-dir", str(kb),
    ]) == 0
    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LATCHED
    assert target.policy == project_config.POLICY_PRIVATE
    assert target.kb_dir == kb
    assert _file_snapshot(kb) == before
    assert not (kb / "kb.db").exists()
    assert not (kb / project_config.KB_TARGET_MARKER_FILE_NAME).exists()


def test_private_new_kb_is_empty_and_does_not_change_global_scope(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    (global_kb / "global-canary").write_text("unchanged\n", encoding="utf-8")
    shared_root = _directory(tmp_path / "shared")
    private_root = _directory(tmp_path / "private")
    project_mode.apply_latch(shared_root, policy=project_config.POLICY_SHARED)
    shared_before = project_config.resolve(shared_root)
    pin_before = (lifecycle_env / "kb_location.json").read_bytes()

    assert project_mode.main([
        "latch", "--project", str(private_root), "--confirm", "latch",
        "--private", "--new-kb",
    ]) == 0
    private = project_config.resolve(private_root)
    shared_after = project_config.resolve(shared_root)
    assert private.policy == project_config.POLICY_PRIVATE
    assert private.kb_dir is not None and private.kb_dir != global_kb
    assert list(private.kb_dir.iterdir()) == []
    assert shared_after.kb_dir == shared_before.kb_dir == global_kb
    assert shared_after.scope_id == shared_before.scope_id
    assert (lifecycle_env / "kb_location.json").read_bytes() == pin_before
    assert (global_kb / "global-canary").read_text(encoding="utf-8") == "unchanged\n"


def test_unlatch_and_latch_restore_exact_private_scope_and_kb(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    nested = _directory(root / "src")
    kb = _directory(tmp_path / "private-vault")
    (kb / "canary").write_text("unchanged\n", encoding="utf-8")
    project_mode.apply_latch(
        root,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(kb),
    )
    before = project_config.resolve(root)
    marker_before = (root / ".latch" / "scope.json").read_bytes()

    assert project_mode.main([
        "unlatch", "--project", str(root), "--confirm", "unlatch",
    ]) == 0
    off = project_config.resolve(nested)
    assert off.state == project_config.MODE_UNLATCHED
    assert off.policy == project_config.POLICY_PRIVATE
    assert off.scope_id == before.scope_id
    assert off.kb_dir is None
    assert off.remembered_kb_dir == kb

    assert project_mode.main([
        "latch", "--project", str(root), "--confirm", "latch",
    ]) == 0
    after = project_config.resolve(nested)
    assert after.state == project_config.MODE_LATCHED
    assert after.project_root == root
    assert after.policy == before.policy == project_config.POLICY_PRIVATE
    assert after.scope_id == before.scope_id
    assert after.kb_dir == before.kb_dir == kb
    assert (root / ".latch" / "scope.json").read_bytes() == marker_before
    assert (kb / "canary").read_text(encoding="utf-8") == "unchanged\n"


def test_private_scope_cannot_transition_or_fall_back_to_global(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "private-vault")
    project_mode.apply_latch(
        root,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(kb),
    )
    before = project_config.resolve(root)

    assert project_mode.main([
        "latch", "--project", str(root), "--confirm", "latch", "--shared",
    ]) == 2
    after = project_config.resolve(root)
    assert after.state == project_config.MODE_LATCHED
    assert after.policy == project_config.POLICY_PRIVATE
    assert after.scope_id == before.scope_id
    assert after.kb_dir == kb
    assert after.kb_dir != global_kb


def test_copied_private_marker_is_locked_until_explicitly_authorized(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    original = _directory(tmp_path / "checkout-a")
    clone = _directory(tmp_path / "checkout-b")
    kb = _directory(tmp_path / "private-vault")
    project_mode.apply_latch(
        original,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(kb),
    )
    expected = project_config.resolve(original)
    shutil.copytree(original / ".latch", clone / ".latch")

    copied = project_mode.status_payload(clone)
    assert copied["state"] == project_config.MODE_LOCKED
    assert copied["effective_root"] == str(clone)
    assert copied["scope_id"] == expected.scope_id
    assert copied["kb_dir"] is None
    assert "not authorized" in str(copied["reason"])

    project_mode.apply_latch(
        clone,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(kb),
    )
    authorized = project_config.resolve(clone)
    assert authorized.state == project_config.MODE_LATCHED
    assert authorized.scope_id == expected.scope_id
    assert authorized.revision == expected.revision
    assert authorized.kb_dir == kb


def test_tampered_private_marker_locks_without_global_fallback_and_shows_target(
    lifecycle_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_global(lifecycle_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    client = _directory(outer / "client")
    kb = _directory(tmp_path / "private-vault")
    project_mode.apply_latch(outer, policy=project_config.POLICY_SHARED)
    project_mode.apply_latch(
        client,
        policy=project_config.POLICY_PRIVATE,
        kb_dir=str(kb),
    )
    marker = client / ".latch" / "scope.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["policy"] = project_config.POLICY_SHARED
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    locked = project_mode.status_payload(client)
    assert locked["state"] == project_config.MODE_LOCKED
    assert locked["effective_root"] == str(client)
    assert locked["policy"] == project_config.POLICY_PRIVATE
    assert locked["kb_dir"] == str(kb)
    assert locked["kb_dir"] != str(global_kb)
    assert "does not match" in str(locked["reason"])
    assert project_config.resolve(client / "nested").state == project_config.MODE_LOCKED
