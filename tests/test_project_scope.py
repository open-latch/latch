"""SCM-independent scope identity, authorization, and inheritance tests."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path

import pytest

import paths
import project_config


@pytest.fixture
def scope_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "scope-tests" / tmp_path.name
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


def _pin_shared(home: Path, tmp_path: Path) -> Path:
    shared = _directory(tmp_path / "shared-kb")
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(shared)}) + "\n", encoding="utf-8"
    )
    return shared


def _private_scope(root: Path, kb: Path) -> project_config.ResolvedScope:
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    return project_config.authorize_scope(root, kb_dir=kb)


def _shared_scope(root: Path) -> project_config.ResolvedScope:
    project_config.create_scope(root, policy=project_config.POLICY_SHARED)
    return project_config.authorize_scope(root)


def _write_vault_uuid(kb: Path, vault_uuid: str | None = None) -> str:
    chosen = vault_uuid or str(uuid.uuid4())
    connection = sqlite3.connect(kb / "kb.db")
    try:
        connection.execute(
            "CREATE TABLE vault_identity (slot INTEGER PRIMARY KEY, vault_uuid TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO vault_identity (slot, vault_uuid) VALUES (1, ?)",
            (chosen,),
        )
        connection.commit()
    finally:
        connection.close()
    return chosen


def test_portable_marker_contains_policy_not_machine_path(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "private-vault")

    target = _private_scope(root, kb)
    payload = json.loads((root / ".latch" / "scope.json").read_text(encoding="utf-8"))

    assert payload == {
        "format": 1,
        "policy": "private",
        "scope_id": target.scope_id,
    }
    assert str(kb) not in json.dumps(payload)
    assert target.kb_dir == kb
    assert not (kb / project_config.KB_TARGET_MARKER_FILE_NAME).exists()


def test_non_git_descendants_inherit_nearest_explicit_root(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "plain-folder")
    nested = _directory(root / "one" / "two")
    kb = _directory(tmp_path / "vault")

    expected = _private_scope(root, kb)
    actual = project_config.resolve(nested)

    assert actual.state == project_config.MODE_LATCHED
    assert actual.project_root == root
    assert actual.scope_id == expected.scope_id
    assert actual.kb_dir == kb


def test_copied_or_fresh_clone_marker_is_locked_until_authorized(
    scope_env: Path, tmp_path: Path,
) -> None:
    original = _directory(tmp_path / "original")
    clone = _directory(tmp_path / "clone")
    kb = _directory(tmp_path / "vault")
    original_target = _private_scope(original, kb)
    shutil.copytree(original / ".latch", clone / ".latch")

    locked = project_config.resolve(clone)
    assert locked.state == project_config.MODE_LOCKED
    assert locked.scope_id == original_target.scope_id
    assert "not authorized" in (locked.reason or "")

    alias = project_config.authorize_scope(clone, kb_dir=kb)
    assert alias.state == project_config.MODE_LATCHED
    assert alias.scope_id == original_target.scope_id
    assert alias.revision == original_target.revision


def test_all_authorized_aliases_share_one_mode_and_target(
    scope_env: Path, tmp_path: Path,
) -> None:
    first = _directory(tmp_path / "checkout-a")
    second = _directory(tmp_path / "checkout-b")
    kb_a = _directory(tmp_path / "vault-a")
    kb_b = _directory(tmp_path / "vault-b")
    initial = _private_scope(first, kb_a)
    shutil.copytree(first / ".latch", second / ".latch")
    project_config.authorize_scope(second, kb_dir=kb_a)

    project_config.set_scope_mode(first, project_config.MODE_UNLATCHED)
    assert project_config.resolve(first).state == project_config.MODE_UNLATCHED
    assert project_config.resolve(second).state == project_config.MODE_UNLATCHED

    project_config.repin_private_scope(second, kb_b)
    project_config.set_scope_mode(second, project_config.MODE_LATCHED)
    first_after = project_config.resolve(first)
    second_after = project_config.resolve(second)
    assert first_after.kb_dir == second_after.kb_dir == kb_b
    assert first_after.revision == second_after.revision
    assert first_after.revision != initial.revision


def test_deleted_private_marker_stays_locked_below_shared_parent(
    scope_env: Path, tmp_path: Path,
) -> None:
    shared_kb = _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "consulting")
    client = _directory(outer / "client")
    private_kb = _directory(tmp_path / "client-vault")
    _shared_scope(outer)
    _private_scope(client, private_kb)

    (client / ".latch" / "scope.json").unlink()
    target = project_config.resolve(client)

    assert target.state == project_config.MODE_LOCKED
    assert target.project_root == client
    assert target.kb_dir is None
    assert target.remembered_kb_dir == private_kb
    assert target.remembered_kb_dir != shared_kb
    assert "missing" in (target.reason or "")


def test_tampered_marker_is_locked_not_reinterpreted(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    _private_scope(root, kb)
    marker = root / ".latch" / "scope.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["policy"] = "shared"
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LOCKED
    assert "does not match" in (target.reason or "")


def test_private_below_shared_is_allowed_but_shared_below_private_is_not(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "outer")
    private = _directory(outer / "private")
    forbidden = _directory(private / "shared")
    private_kb = _directory(tmp_path / "private-kb")
    _shared_scope(outer)
    _private_scope(private, private_kb)

    with pytest.raises(project_config.ProjectConfigError, match="Shared scope cannot"):
        project_config.create_scope(forbidden, policy=project_config.POLICY_SHARED)


def test_untouched_install_defaults_to_global_shared_without_policy_file(
    scope_env: Path, tmp_path: Path,
) -> None:
    global_kb = _pin_shared(scope_env, tmp_path)
    project = _directory(tmp_path / "old-project")

    # Existing Shared users do not need a migration record. The persisted pin
    # remains the single authority until project mode is explicitly enabled.
    project_config.machine_policy_path().unlink()
    target = project_config.resolve(project)
    assert target.state == project_config.MODE_LATCHED
    assert target.source == project_config.SOURCE_GLOBAL
    assert target.kb_dir == global_kb


def test_global_shared_mode_ignores_project_scope_files(
    compatibility_scope_env: dict[str, Path],
    tmp_path: Path,
) -> None:
    project = _directory(tmp_path / "ordinary-shared-project")
    marker = project / ".latch" / "scope.json"
    marker.parent.mkdir()
    marker.write_text("{}\n", encoding="utf-8")

    target = project_config.resolve(project)

    assert target.state == project_config.MODE_LATCHED
    assert target.source == project_config.SOURCE_GLOBAL
    assert target.kb_dir == compatibility_scope_env["vault"]
    assert project_config.discover(project) is None


def test_missing_policy_fails_closed_after_project_state_exists(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    root = _directory(tmp_path / "client")
    _shared_scope(root)

    project_config.machine_policy_path().unlink()
    with pytest.raises(project_config.ProjectConfigError, match="policy.*missing|mode.*missing"):
        project_config.resolve(root)


def test_explicit_policy_locks_unknown_and_missing_paths(
    scope_env: Path, tmp_path: Path,
) -> None:
    unknown = _directory(tmp_path / "unknown")
    missing = unknown / "not-created" / "yet"

    assert project_config.resolve(unknown).state == project_config.MODE_LOCKED
    missing_target = project_config.resolve(missing)
    assert missing_target.state == project_config.MODE_LOCKED
    assert missing_target.project_root == missing.resolve(strict=False)


def test_authorizing_existing_kb_is_read_only(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "existing-kb")
    canary = kb / "canary.bin"
    canary.write_bytes(b"unchanged\x00contents")
    before = {path.name: path.read_bytes() for path in kb.iterdir()}
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)

    project_config.authorize_scope(root, kb_dir=kb)

    after = {path.name: path.read_bytes() for path in kb.iterdir()}
    assert after == before


def test_recreated_kb_directory_fails_continuity_check(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    _private_scope(root, kb)
    kb.rmdir()
    kb.mkdir()

    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LOCKED
    assert "identity changed" in (target.reason or "")


def test_downstream_unlatch_is_off_boundary_and_never_a_vault(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "client-work")
    sibling = _directory(outer / "other-work")
    shared = _shared_scope(outer)

    off = project_config.create_off_boundary(child)

    assert off.state == project_config.MODE_UNLATCHED
    assert off.project_root == child
    assert off.scope_id == shared.scope_id
    assert not (child / ".latch" / "scope.json").exists()
    assert project_config.resolve(sibling).state == project_config.MODE_LATCHED
    resumed = project_config.remove_off_boundary(child)
    assert resumed.state == project_config.MODE_LATCHED
    assert resumed.scope_id == shared.scope_id


def test_downstream_relatch_permanently_stales_pre_off_task(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "client-work")
    _shared_scope(outer)
    before = project_config.resolve(child)
    project_config.record_session_binding(child, "pre-off-task")

    project_config.create_off_boundary(child)
    after = project_config.remove_off_boundary(child)

    # The product returns to the exact inherited scope and KB, but the local
    # The continuity epoch prevents pre-cycle work from reviving.
    assert after.target_revision == before.target_revision
    assert after.revision != before.revision
    assert after.kb_dir == before.kb_dir
    assert project_config.current_session_revision(child, "pre-off-task") is None
    assert project_config.record_session_binding(child, "fresh-task") == after.revision


def test_off_boundary_refuses_resume_after_parent_target_changes(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "child")
    _shared_scope(outer)
    project_config.create_off_boundary(child)
    project_config.set_scope_mode(outer, project_config.MODE_UNLATCHED)
    project_config.set_scope_mode(outer, project_config.MODE_LATCHED)

    with pytest.raises(project_config.ProjectConfigError, match="remembered parent"):
        project_config.remove_off_boundary(child)
    assert project_config.resolve(child).state == project_config.MODE_UNLATCHED


def test_off_boundary_keeps_last_known_target_visible_after_parent_repin(
    scope_env: Path, tmp_path: Path,
) -> None:
    original = _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "shared-root")
    child = _directory(outer / "child")
    _shared_scope(outer)
    off = project_config.create_off_boundary(child)
    assert off.remembered_kb_dir == original

    replacement = _directory(tmp_path / "replacement-global")
    (scope_env / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(replacement)}) + "\n", encoding="utf-8"
    )
    project_config.reauthorize_shared_scope(outer)

    after = project_config.resolve(child)
    assert after.state == project_config.MODE_UNLATCHED
    assert after.kb_dir is None
    assert after.remembered_kb_dir == original
    assert after.remembered_kb_dir != replacement
    assert "remembered parent target changed" in (after.reason or "")


def test_session_receipt_cannot_follow_unlatch_or_repin(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    first_kb = _directory(tmp_path / "vault-one")
    second_kb = _directory(tmp_path / "vault-two")
    initial = _private_scope(root, first_kb)
    assert project_config.record_session_binding(root, "task-1") == initial.revision

    project_config.set_scope_mode(root, project_config.MODE_UNLATCHED)
    assert project_config.current_session_revision(root, "task-1") is None
    project_config.repin_private_scope(root, second_kb)
    project_config.set_scope_mode(root, project_config.MODE_LATCHED)
    assert project_config.current_session_revision(root, "task-1") is None
    with pytest.raises(project_config.ProjectConfigError, match="older Latch scope"):
        project_config.record_session_binding(root, "task-1")


def test_shared_scope_locks_if_global_pin_changes(
    scope_env: Path, tmp_path: Path,
) -> None:
    first = _pin_shared(scope_env, tmp_path)
    root = _directory(tmp_path / "shared-root")
    target = _shared_scope(root)
    assert target.kb_dir == first
    second = _directory(tmp_path / "other-global")
    (scope_env / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(second)}) + "\n", encoding="utf-8"
    )

    changed = project_config.resolve(root)
    assert changed.state == project_config.MODE_LOCKED
    assert "global KB pin changed" in (changed.reason or "")


def test_private_scope_cannot_select_global_kb(
    scope_env: Path, tmp_path: Path,
) -> None:
    shared = _pin_shared(scope_env, tmp_path)
    root = _directory(tmp_path / "client")
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)

    with pytest.raises(project_config.ProjectConfigError, match="cannot bind"):
        project_config.authorize_scope(root, kb_dir=shared)


def test_transition_lock_is_non_git_and_fails_fast_when_busy(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "plain-folder")

    with project_config.transition_lock(root):
        with pytest.raises(project_config.ProjectTransitionBusyError):
            with project_config.transition_lock(root):
                raise AssertionError("unreachable")


def test_scope_control_override_cannot_escape_authenticated_test_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(tmp_path / "outside"))
    with pytest.raises(project_config.ProjectConfigError, match="authenticated test root"):
        project_config.control_root()


def test_copied_marker_cannot_mutate_the_central_scope(
    scope_env: Path, tmp_path: Path,
) -> None:
    original = _directory(tmp_path / "original")
    clone = _directory(tmp_path / "clone")
    first_kb = _directory(tmp_path / "first-kb")
    second_kb = _directory(tmp_path / "second-kb")
    expected = _private_scope(original, first_kb)
    shutil.copytree(original / ".latch", clone / ".latch")

    for mutation in (
        lambda: project_config.set_scope_mode(
            clone, project_config.MODE_UNLATCHED
        ),
        lambda: project_config.repin_private_scope(clone, second_kb),
        lambda: project_config.create_off_boundary(clone),
    ):
        with pytest.raises(project_config.ProjectConfigError):
            mutation()

    after = project_config.resolve(original)
    assert after.state == project_config.MODE_LATCHED
    assert after.scope_id == expected.scope_id
    assert after.kb_dir == first_kb


def test_malformed_private_marker_still_blocks_nested_shared_scope(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    outer = _directory(tmp_path / "consulting")
    client = _directory(outer / "client")
    nested = _directory(client / "nested")
    private_kb = _directory(tmp_path / "client-vault")
    _shared_scope(outer)
    _private_scope(client, private_kb)
    (client / ".latch" / "scope.json").write_text("{not-json", encoding="utf-8")

    locked = project_config.resolve(client)
    assert locked.state == project_config.MODE_LOCKED
    assert locked.policy == project_config.POLICY_PRIVATE
    assert locked.remembered_kb_dir == private_kb
    with pytest.raises(project_config.ProjectConfigError, match="Shared scope cannot"):
        project_config.create_scope(nested, policy=project_config.POLICY_SHARED)


def test_locked_policy_uses_strictest_remembered_authority(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    _private_scope(root, kb)
    authorization_path = project_config.local_binding_path(root)
    authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
    authorization["policy"] = project_config.POLICY_SHARED
    authorization["marker_fingerprint"] = project_config._marker_fingerprint(
        authorization["scope_id"], project_config.POLICY_SHARED
    )
    authorization_path.write_text(
        json.dumps(authorization) + "\n", encoding="utf-8"
    )

    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LOCKED
    assert target.policy == project_config.POLICY_PRIVATE


def test_locked_authorized_alias_keeps_the_canonical_scope_lock_key(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    initial = _private_scope(root, kb)
    marker = root / ".latch" / "scope.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["policy"] = project_config.POLICY_SHARED
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LOCKED
    assert target.lock_key == initial.lock_key


def test_new_vault_identity_requires_exact_revision_finalization(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "empty-vault")
    initial = _private_scope(root, kb)
    vault_uuid = _write_vault_uuid(kb)

    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert pending.reason_code == project_config.LOCK_VAULT_IDENTITY_PENDING
    with pytest.raises(project_config.ProjectConfigError, match="binding changed"):
        project_config.finalize_scope_vault_identity(
            root,
            expected_revision="0" * 32,
            vault_uuid=vault_uuid,
        )

    finalized = project_config.finalize_scope_vault_identity(
        root,
        expected_revision=initial.revision,
        vault_uuid=vault_uuid,
    )
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.revision == initial.revision
    binding = project_config._load_scope_binding(finalized.scope_id)
    assert binding.vault_uuid == vault_uuid


def test_missing_bound_vault_identity_locks_before_reinitialization(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "client")
    kb = _directory(tmp_path / "vault")
    _write_vault_uuid(kb)
    _private_scope(root, kb)
    (kb / "kb.db").unlink()

    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LOCKED
    assert "identity is missing" in (target.reason or "")


def test_copied_vault_uuid_cannot_belong_to_two_private_scopes(
    scope_env: Path, tmp_path: Path,
) -> None:
    first_root = _directory(tmp_path / "first-client")
    second_root = _directory(tmp_path / "second-client")
    first_kb = _directory(tmp_path / "first-vault")
    vault_uuid = _write_vault_uuid(first_kb)
    _private_scope(first_root, first_kb)
    second_kb = tmp_path / "restored-vault"
    shutil.copytree(first_kb, second_kb)
    assert project_config._read_vault_uuid(second_kb) == vault_uuid
    project_config.create_scope(
        second_root, policy=project_config.POLICY_PRIVATE
    )

    with pytest.raises(project_config.ProjectConfigError, match="reserved"):
        project_config.authorize_scope(second_root, kb_dir=second_kb)


def test_authorizing_alias_of_unlatched_scope_preserves_then_reenables_mode(
    scope_env: Path, tmp_path: Path,
) -> None:
    first = _directory(tmp_path / "first")
    second = _directory(tmp_path / "second")
    kb = _directory(tmp_path / "vault")
    _private_scope(first, kb)
    project_config.set_scope_mode(first, project_config.MODE_UNLATCHED)
    shutil.copytree(first / ".latch", second / ".latch")

    alias = project_config.authorize_scope(second, kb_dir=kb)
    assert alias.state == project_config.MODE_UNLATCHED
    project_config.set_scope_mode(second, project_config.MODE_LATCHED)
    assert project_config.resolve(first).state == project_config.MODE_LATCHED
    assert project_config.resolve(second).state == project_config.MODE_LATCHED


def test_global_pin_cannot_be_redirected_to_a_private_vault(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    private_root = _directory(tmp_path / "private-client")
    private_kb = _directory(tmp_path / "private-vault")
    _private_scope(private_root, private_kb)
    legacy_root = _directory(tmp_path / "legacy-root")
    # Returning to global mode is intentionally unsupported after project
    # scopes exist, so inspect the global resolver directly through a fresh
    # Shared-mode control plane containing the copied Private reservation.
    project_config.machine_policy_path().unlink()
    project_config.atomic_json(
        project_config.machine_policy_path(),
        {"format": 1, "policy": project_config.MACHINE_POLICY_SHARED},
    )
    (scope_env / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(private_kb)}) + "\n", encoding="utf-8"
    )

    target = project_config.resolve(legacy_root)
    assert target.state == project_config.MODE_LOCKED
    assert "collides with Private scope" in (target.reason or "")


def test_global_shared_mode_without_a_pin_is_locked(
    scope_env: Path, tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "legacy-root")
    project_config.atomic_json(
        project_config.machine_policy_path(),
        {"format": 1, "policy": project_config.MACHINE_POLICY_SHARED},
    )

    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LOCKED
    assert target.kb_dir is None
    assert "not pinned" in (target.reason or "")


def test_shared_scope_can_be_explicitly_reauthorized_after_global_repin(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    root = _directory(tmp_path / "shared-root")
    _shared_scope(root)
    replacement = _directory(tmp_path / "replacement-global")
    (scope_env / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(replacement)}) + "\n", encoding="utf-8"
    )

    locked = project_config.resolve(root)
    assert locked.reason_code == project_config.LOCK_GLOBAL_PIN_CHANGED
    repaired = project_config.reauthorize_shared_scope(root)
    assert repaired.state == project_config.MODE_LATCHED
    assert repaired.policy == project_config.POLICY_SHARED
    assert repaired.kb_dir == replacement


def test_shared_to_private_updates_every_authorized_alias(
    scope_env: Path, tmp_path: Path,
) -> None:
    _pin_shared(scope_env, tmp_path)
    first = _directory(tmp_path / "first")
    second = _directory(tmp_path / "second")
    private_kb = _directory(tmp_path / "private-vault")
    initial = _shared_scope(first)
    shutil.copytree(first / ".latch", second / ".latch")
    project_config.authorize_scope(second)

    converted = project_config.convert_shared_scope_to_private(first, private_kb)

    assert converted.scope_id == initial.scope_id
    for alias in (first, second):
        target = project_config.resolve(alias)
        assert target.state == project_config.MODE_LATCHED
        assert target.policy == project_config.POLICY_PRIVATE
        assert target.kb_dir == private_kb
        marker = json.loads(
            (alias / ".latch" / "scope.json").read_text(encoding="utf-8")
        )
        assert marker["policy"] == project_config.POLICY_PRIVATE


def test_shared_to_private_transition_is_resumable_after_marker_write_failure(
    scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_shared(scope_env, tmp_path)
    root = _directory(tmp_path / "shared-root")
    private_kb = _directory(tmp_path / "private-vault")
    _shared_scope(root)
    marker_path = root / ".latch" / "scope.json"
    original_atomic_json = project_config.atomic_json
    failed = False

    def fail_once(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> None:
        nonlocal failed
        if path == marker_path and not failed:
            failed = True
            raise OSError("simulated interruption")
        original_atomic_json(path, payload, mode=mode)

    monkeypatch.setattr(project_config, "atomic_json", fail_once)
    with pytest.raises(OSError, match="simulated interruption"):
        project_config.convert_shared_scope_to_private(root, private_kb)
    assert project_config.resolve(root).state == project_config.MODE_LOCKED

    repaired = project_config.convert_shared_scope_to_private(root, private_kb)
    assert repaired.state == project_config.MODE_LATCHED
    assert repaired.policy == project_config.POLICY_PRIVATE


def test_private_target_rejects_broad_and_project_overlapping_directories(
    scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _directory(tmp_path / "project")
    child = _directory(root / "child")
    faux_home = _directory(tmp_path / "user-home")
    monkeypatch.setenv("HOME", str(faux_home))
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)

    for unsafe in (
        Path(root.anchor),
        faux_home,
        root,
        child,
        tmp_path,
        project_config.control_root(),
        scope_env,
    ):
        with pytest.raises(project_config.ProjectConfigError, match="must not overlap|stay outside"):
            project_config.authorize_scope(root, kb_dir=unsafe)


def test_scope_registry_lock_rejects_busy_symlink_and_hardlink(
    scope_env: Path, tmp_path: Path,
) -> None:
    with project_config.scope_registry_lock():
        with pytest.raises(project_config.ProjectTransitionBusyError):
            with project_config.scope_registry_lock():
                raise AssertionError("unreachable")

    lock_path = project_config.control_root() / project_config.LOCKS_DIR_NAME / "scope-registry.lock"
    lock_path.unlink()
    canary = tmp_path / "canary"
    canary.write_text("keep", encoding="utf-8")
    lock_path.symlink_to(canary)
    with pytest.raises(project_config.ProjectConfigError, match="regular file"):
        with project_config.scope_registry_lock():
            raise AssertionError("unreachable")
    lock_path.unlink()

    try:
        os.link(canary, lock_path)
    except OSError:
        pytest.skip("hard links are unavailable on this filesystem")
    with pytest.raises(project_config.ProjectConfigError, match="single-link"):
        with project_config.scope_registry_lock():
            raise AssertionError("unreachable")
    assert canary.read_text(encoding="utf-8") == "keep"
