"""Adversarial scope-isolation regressions for the project foundation.

This file deliberately exercises failure and interruption boundaries that are
easy to miss in happy-path lifecycle coverage.  The assertions describe the
fail-closed product contract; a failure here is an implementation finding, not
a reason to weaken the test.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from queue import Empty

import pytest

import paths
import project_config


@pytest.fixture
def scope_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "scope-adversarial" / tmp_path.name
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


def _pin_shared(home: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    selected = target.resolve()
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(selected)}) + "\n",
        encoding="utf-8",
    )
    return selected


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
            "CREATE TABLE vault_identity "
            "(slot INTEGER PRIMARY KEY, vault_uuid TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO vault_identity (slot, vault_uuid) VALUES (1, ?)",
            (chosen,),
        )
        connection.commit()
    finally:
        connection.close()
    return chosen


def _install_link(source: Path, destination: Path, kind: str) -> None:
    destination.unlink(missing_ok=True)
    try:
        if kind == "symlink":
            destination.symlink_to(source)
        elif kind == "hardlink":
            os.link(source, destination)
        else:  # pragma: no cover - test parametrization controls this
            raise AssertionError(f"unknown link kind: {kind}")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"{kind}s are unavailable on this filesystem: {exc}")


def _authorize_private_worker(
    root: str,
    kb: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    """Cross-process worker; kept at module scope for spawn portability."""
    if not start.wait(15):
        results.put(("error", "Timeout", "start barrier was not released"))
        return
    try:
        target = project_config.authorize_scope(Path(root), kb_dir=Path(kb))
    except Exception as exc:  # noqa: BLE001 - the result is asserted by parent
        results.put(("error", type(exc).__name__, str(exc)))
    else:
        results.put(("ok", target.scope_id, str(target.kb_dir)))


@pytest.mark.parametrize("intermediate_kind", ["unauthorized", "malformed"])
def test_nearer_shared_marker_cannot_mask_outer_private(
    scope_env: Path,
    tmp_path: Path,
    intermediate_kind: str,
) -> None:
    _pin_shared(scope_env, tmp_path / "global-kb")
    outer = _directory(tmp_path / "outer-private")
    middle = _directory(outer / "middle")
    child = _directory(middle / "child-shared")
    private_kb = _directory(tmp_path / "private-vault")
    _private_scope(outer, private_kb)

    marker_source = _directory(tmp_path / "shared-marker-source")
    project_config.create_scope(
        marker_source,
        policy=project_config.POLICY_SHARED,
    )
    marker_payload = json.loads(
        (marker_source / ".latch" / "scope.json").read_text(encoding="utf-8")
    )
    (middle / ".latch").mkdir()
    if intermediate_kind == "malformed":
        marker_payload["unexpected"] = True
    (middle / ".latch" / "scope.json").write_text(
        json.dumps(marker_payload) + "\n",
        encoding="utf-8",
    )

    child_marker_source = _directory(tmp_path / "child-marker-source")
    project_config.create_scope(
        child_marker_source,
        policy=project_config.POLICY_SHARED,
    )
    shutil.copytree(child_marker_source / ".latch", child / ".latch")

    middle_target = project_config.resolve(middle)
    child_target = project_config.resolve(child)
    assert middle_target.state == project_config.MODE_LOCKED
    assert middle_target.policy == project_config.POLICY_PRIVATE
    assert child_target.state == project_config.MODE_LOCKED
    assert child_target.policy == project_config.POLICY_PRIVATE
    assert child_target.kb_dir is None
    with pytest.raises(project_config.ProjectConfigError, match="Private scope"):
        project_config.authorize_scope(child)


def test_private_cannot_claim_reserved_shared_target_while_pin_is_missing(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    shared_kb = _pin_shared(scope_env, tmp_path / "shared-kb")
    shared_root = _directory(tmp_path / "shared-root")
    private_root = _directory(tmp_path / "private-root")
    shared = _shared_scope(shared_root)
    project_config.create_scope(
        private_root,
        policy=project_config.POLICY_PRIVATE,
    )

    (scope_env / "kb_location.json").unlink()
    with pytest.raises(project_config.ProjectConfigError, match="reserved"):
        project_config.authorize_scope(private_root, kb_dir=shared_kb)

    _pin_shared(scope_env, shared_kb)
    restored = project_config.resolve(shared_root)
    rejected = project_config.resolve(private_root)
    assert restored.state == project_config.MODE_LATCHED
    assert restored.scope_id == shared.scope_id
    assert restored.kb_dir == shared_kb
    assert rejected.state == project_config.MODE_LOCKED
    assert rejected.kb_dir is None


@pytest.mark.parametrize("first_target", ["parent", "child"])
def test_private_targets_must_be_path_disjoint(
    scope_env: Path,
    tmp_path: Path,
    first_target: str,
) -> None:
    first_root = _directory(tmp_path / "first-project")
    second_root = _directory(tmp_path / "second-project")
    parent = _directory(tmp_path / "vault-tree")
    child = _directory(parent / "nested-vault")
    first_kb, second_kb = (
        (parent, child) if first_target == "parent" else (child, parent)
    )
    _private_scope(first_root, first_kb)
    project_config.create_scope(
        second_root,
        policy=project_config.POLICY_PRIVATE,
    )

    with pytest.raises(project_config.ProjectConfigError, match="reserved"):
        project_config.authorize_scope(second_root, kb_dir=second_kb)
    assert project_config.resolve(second_root).state == project_config.MODE_LOCKED


def test_private_target_cannot_nest_below_shared_target(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    shared_kb = _pin_shared(scope_env, tmp_path / "shared-vault")
    nested_private = _directory(shared_kb / "private-vault")
    shared_root = _directory(tmp_path / "shared-project")
    private_root = _directory(tmp_path / "private-project")
    _shared_scope(shared_root)
    project_config.create_scope(
        private_root,
        policy=project_config.POLICY_PRIVATE,
    )

    with pytest.raises(project_config.ProjectConfigError):
        project_config.authorize_scope(private_root, kb_dir=nested_private)


def test_shared_target_cannot_nest_below_private_target(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    private_root = _directory(tmp_path / "private-project")
    shared_root = _directory(tmp_path / "shared-project")
    private_kb = _directory(tmp_path / "private-vault")
    nested_shared = _directory(private_kb / "shared-vault")
    _private_scope(private_root, private_kb)
    _pin_shared(scope_env, nested_shared)
    project_config.create_scope(
        shared_root,
        policy=project_config.POLICY_SHARED,
    )

    with pytest.raises(project_config.ProjectConfigError, match="collides"):
        project_config.authorize_scope(shared_root)


@pytest.mark.parametrize("target_kind", ["private", "shared", "global"])
@pytest.mark.parametrize("root_relation", ["equal", "ancestor", "descendant"])
def test_project_root_cannot_overlap_a_later_reserved_kb_target(
    scope_env: Path,
    tmp_path: Path,
    target_kind: str,
    root_relation: str,
) -> None:
    """Reverse-order setup must be as strict as target authorization.

    Portable intent is deliberately created first. The target is reserved
    afterward, proving final local authorization rechecks the full registry
    instead of trusting the earlier marker-only decision.
    """
    container = _directory(tmp_path / "reserved-container")
    reserved = _directory(container / "reserved-kb")
    if root_relation == "equal":
        candidate_root = reserved
    elif root_relation == "ancestor":
        candidate_root = container
    else:
        candidate_root = _directory(reserved / "nested-project")

    project_config.create_scope(
        candidate_root,
        policy=project_config.POLICY_PRIVATE,
    )

    if target_kind == "private":
        owner_root = _directory(tmp_path / "private-owner")
        _private_scope(owner_root, reserved)
    elif target_kind == "shared":
        _pin_shared(scope_env, reserved)
        owner_root = _directory(tmp_path / "shared-owner")
        _shared_scope(owner_root)
    else:
        _pin_shared(scope_env, reserved)

    candidate_kb = _directory(tmp_path / "candidate-kb")
    with pytest.raises(project_config.ProjectConfigError, match="reserved KB target"):
        project_config.authorize_scope(candidate_root, kb_dir=candidate_kb)

    resolved = project_config.resolve(candidate_root)
    assert resolved.state == project_config.MODE_LOCKED
    assert resolved.kb_dir is None


def test_scope_intent_inside_reserved_kb_is_rejected_before_marker_write(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    reserved = _pin_shared(scope_env, tmp_path / "shared-kb")
    owner = _directory(tmp_path / "shared-owner")
    _shared_scope(owner)
    nested = _directory(reserved / "nested-project")

    with pytest.raises(project_config.ProjectConfigError, match="reserved KB target"):
        project_config.create_scope(nested, policy=project_config.POLICY_PRIVATE)

    assert not (nested / project_config.PORTABLE_DIR_NAME).exists()


def test_off_boundary_removal_cannot_race_parent_repin(
    scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = _directory(tmp_path / "private-project")
    child = _directory(parent / "temporarily-off")
    first_kb = _directory(tmp_path / "first-vault")
    second_kb = _directory(tmp_path / "second-vault")
    _private_scope(parent, first_kb)
    project_config.create_off_boundary(child)
    original_unlink = project_config.durable_unlink
    raced = False

    def repin_before_unlink(path: Path) -> None:
        nonlocal raced
        raced = True
        project_config.repin_private_scope(parent, second_kb)
        original_unlink(path)

    monkeypatch.setattr(project_config, "durable_unlink", repin_before_unlink)
    with pytest.raises(project_config.ProjectConfigError):
        project_config.remove_off_boundary(child)

    assert raced
    still_off = project_config.resolve(child)
    assert still_off.state == project_config.MODE_UNLATCHED
    assert still_off.kb_dir is None
    assert still_off.remembered_kb_dir == first_kb
    assert project_config.resolve(parent).kb_dir == first_kb


def test_interrupted_shared_to_private_with_new_uuid_is_repairable(
    scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin_shared(scope_env, tmp_path / "shared-kb")
    root = _directory(tmp_path / "shared-project")
    private_kb = _directory(tmp_path / "new-private-vault")
    _shared_scope(root)
    marker_path = root / ".latch" / "scope.json"
    original_atomic_json = project_config.atomic_json
    failed = False

    def interrupt_first_marker_write(
        path: Path,
        payload: dict[str, object],
        *,
        mode: int = 0o600,
    ) -> None:
        nonlocal failed
        if path == marker_path and not failed:
            failed = True
            raise OSError("simulated marker interruption")
        original_atomic_json(path, payload, mode=mode)

    monkeypatch.setattr(
        project_config,
        "atomic_json",
        interrupt_first_marker_write,
    )
    with pytest.raises(OSError, match="simulated marker interruption"):
        project_config.convert_shared_scope_to_private(root, private_kb)
    assert project_config.resolve(root).state == project_config.MODE_LOCKED

    vault_uuid = _write_vault_uuid(private_kb)
    repaired = project_config.convert_shared_scope_to_private(root, private_kb)
    binding = project_config._load_scope_binding(repaired.scope_id)
    assert repaired.state == project_config.MODE_LATCHED
    assert repaired.policy == project_config.POLICY_PRIVATE
    assert repaired.kb_dir == private_kb
    assert repaired.vault_uuid == vault_uuid
    assert binding.vault_uuid == vault_uuid


@pytest.mark.parametrize("record_kind", ["marker", "authorization", "binding"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_scope_state_links_never_grant_authority(
    scope_env: Path,
    tmp_path: Path,
    record_kind: str,
    link_kind: str,
) -> None:
    root = _directory(tmp_path / "private-project")
    kb = _directory(tmp_path / "private-vault")
    target = _private_scope(root, kb)
    paths_by_kind = {
        "marker": root / ".latch" / "scope.json",
        "authorization": project_config.local_binding_path(root),
        "binding": project_config.scope_binding_path(target.scope_id),
    }
    record = paths_by_kind[record_kind]
    canary = record.with_name(f".{record.name}.{link_kind}.canary")
    original = record.read_bytes()
    canary.write_bytes(original)
    _install_link(canary, record, link_kind)

    resolved = project_config.resolve(root)
    assert resolved.state == project_config.MODE_LOCKED
    assert resolved.kb_dir is None
    assert canary.read_bytes() == original


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_kb_database_links_lock_the_scope_without_touching_target(
    scope_env: Path,
    tmp_path: Path,
    link_kind: str,
) -> None:
    root = _directory(tmp_path / "private-project")
    kb = _directory(tmp_path / "private-vault")
    source = _directory(tmp_path / "database-source")
    _private_scope(root, kb)
    _write_vault_uuid(source)
    source_db = source / "kb.db"
    before = source_db.read_bytes()
    _install_link(source_db, kb / "kb.db", link_kind)

    resolved = project_config.resolve(root)
    assert resolved.state == project_config.MODE_LOCKED
    assert resolved.kb_dir is None
    assert "unsafe SQLite database" in (resolved.reason or "")
    assert source_db.read_bytes() == before


def test_symlinked_kb_directory_cannot_be_authorized(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    root = _directory(tmp_path / "private-project")
    real_kb = _directory(tmp_path / "real-vault")
    linked_kb = tmp_path / "linked-vault"
    _install_link(real_kb, linked_kb, "symlink")
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)

    with pytest.raises(project_config.ProjectConfigError, match="missing or unsafe"):
        project_config.authorize_scope(root, kb_dir=linked_kb)
    assert project_config.resolve(root).state == project_config.MODE_LOCKED


@pytest.mark.parametrize("git_kind", ["invalid-file", "symlink"])
def test_git_metadata_cannot_change_explicit_scope_resolution(
    scope_env: Path,
    tmp_path: Path,
    git_kind: str,
) -> None:
    root = _directory(tmp_path / "private-project")
    nested = _directory(root / "nested")
    kb = _directory(tmp_path / "private-vault")
    expected = _private_scope(root, kb)
    git_entry = root / ".git"
    if git_kind == "invalid-file":
        git_entry.write_text("not a gitdir pointer\n", encoding="utf-8")
    else:
        target = tmp_path / "git-canary"
        target.write_text("unchanged", encoding="utf-8")
        _install_link(target, git_entry, "symlink")

    resolved = project_config.resolve(nested)
    assert resolved.state == project_config.MODE_LATCHED
    assert resolved.scope_id == expected.scope_id
    assert resolved.kb_dir == kb


def test_authorized_root_missing_marker_stays_locked_in_project_mode(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_shared(scope_env, tmp_path / "global-kb")
    root = _directory(tmp_path / "private-project")
    private_kb = _directory(tmp_path / "private-vault")
    _private_scope(root, private_kb)
    (root / ".git").mkdir()
    (root / ".latch" / "scope.json").unlink()
    resolved = project_config.resolve(root)
    assert resolved.state == project_config.MODE_LOCKED
    assert resolved.kb_dir is None
    assert resolved.remembered_kb_dir == private_kb
    assert resolved.remembered_kb_dir != global_kb


def test_linked_checkout_git_metadata_does_not_create_an_implicit_boundary(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    global_kb = _pin_shared(scope_env, tmp_path / "global-kb")
    main = _directory(tmp_path / "main-checkout")
    linked = _directory(tmp_path / "linked-checkout")
    private_kb = _directory(tmp_path / "private-vault")
    common_git = _directory(main / ".git")
    linked_git = _directory(common_git / "worktrees" / "linked")
    (linked / ".git").write_text(
        f"gitdir: {linked_git}\n",
        encoding="utf-8",
    )
    _private_scope(main, private_kb)
    assert project_config.git_root(linked) == linked
    resolved = project_config.resolve(linked)
    assert resolved.state == project_config.MODE_LOCKED
    assert resolved.kb_dir is None


def test_cross_process_duplicate_private_target_has_one_winner(
    scope_env: Path,
    tmp_path: Path,
) -> None:
    first_root = _directory(tmp_path / "first-project")
    second_root = _directory(tmp_path / "second-project")
    kb = _directory(tmp_path / "single-private-vault")
    project_config.create_scope(
        first_root,
        policy=project_config.POLICY_PRIVATE,
    )
    project_config.create_scope(
        second_root,
        policy=project_config.POLICY_PRIVATE,
    )
    # Pre-create and validate the existing registry lock so this test isolates
    # binding serialization rather than first-directory creation.
    with project_config.scope_registry_lock():
        pass

    context = multiprocessing.get_context("spawn")
    try:
        start = context.Event()
        results = context.Queue()
    except (OSError, RuntimeError) as exc:
        pytest.skip(f"cross-process synchronization is unavailable: {exc}")
    processes = [
        context.Process(
            target=_authorize_private_worker,
            args=(str(root), str(kb), start, results),
        )
        for root in (first_root, second_root)
    ]
    for process in processes:
        process.start()
    start.set()
    observed: list[tuple[str, ...]] = []
    try:
        for _ in processes:
            observed.append(results.get(timeout=20))
    except Empty as exc:
        raise AssertionError("scope authorization worker did not report") from exc
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    successes = [row for row in observed if row[0] == "ok"]
    failures = [row for row in observed if row[0] == "error"]
    assert len(successes) == 1, observed
    assert len(failures) == 1, observed
    targets = [
        project_config.resolve(first_root),
        project_config.resolve(second_root),
    ]
    assert sum(target.state == project_config.MODE_LATCHED for target in targets) == 1
    assert sum(target.state == project_config.MODE_LOCKED for target in targets) == 1
    assert {target.kb_dir for target in targets} == {None, kb}
