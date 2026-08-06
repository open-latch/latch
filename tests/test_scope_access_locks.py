"""Canonical scope leases quiesce aliases and reject queued stale work."""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest

import db
import lockfile
import paths
import project_config
import project_mode


@pytest.fixture
def private_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[Path, Path, Path]:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "access-scope-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)

    first = tmp_path / "checkout-a"
    second = tmp_path / "checkout-b"
    first.mkdir()
    second.mkdir()
    vault = test_root / "vaults" / f"scope-access-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(first, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(first, kb_dir=vault)
    shutil.copytree(first / ".latch", second / ".latch")
    project_config.authorize_scope(second, kb_dir=vault)
    return first, second, vault


def test_aliases_and_descendants_share_one_access_lock(
    private_aliases: tuple[Path, Path, Path],
) -> None:
    first, second, _vault = private_aliases
    descendant = second / "nested"
    descendant.mkdir()

    paths_seen = {
        project_config.access_lock_path(project_config.resolve(path))
        for path in (first, second, descendant)
    }
    assert len(paths_seen) == 1


def test_unlatch_waits_for_open_connection_across_aliases(
    private_aliases: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second, _vault = private_aliases
    monkeypatch.setattr(project_mode, "_disable_instructions", lambda roots: [])
    connection = db.connect(str(first))
    finished = threading.Event()
    errors: list[BaseException] = []

    def transition() -> None:
        try:
            project_mode.apply_unlatch(str(second))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=transition)
    worker.start()
    assert not finished.wait(timeout=0.2)
    connection.close()
    assert finished.wait(timeout=3)
    worker.join(timeout=3)

    assert not errors
    assert project_config.resolve(first).state == project_config.MODE_UNLATCHED
    assert project_config.resolve(second).state == project_config.MODE_UNLATCHED


def test_direct_mode_mutation_waits_for_open_connection_across_aliases(
    private_aliases: tuple[Path, Path, Path],
) -> None:
    first, second, _vault = private_aliases
    connection = db.connect(str(first))
    finished = threading.Event()
    errors: list[BaseException] = []

    def transition() -> None:
        try:
            project_config.set_scope_mode(
                second,
                project_config.MODE_UNLATCHED,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=transition)
    worker.start()
    assert not finished.wait(timeout=0.2)
    connection.close()
    assert finished.wait(timeout=3)
    worker.join(timeout=3)

    assert not errors
    assert project_config.resolve(first).state == project_config.MODE_UNLATCHED
    assert project_config.resolve(second).state == project_config.MODE_UNLATCHED


def test_direct_repin_waits_for_open_connection_across_aliases(
    private_aliases: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    first, second, old_vault = private_aliases
    test_root = paths.validated_test_root()
    assert test_root is not None
    replacement = test_root / "vaults" / f"direct-repin-{tmp_path.name}"
    replacement.mkdir(parents=True)
    connection = db.connect(str(first))
    finished = threading.Event()
    errors: list[BaseException] = []

    def repin() -> None:
        try:
            project_config.repin_private_scope(second, replacement)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=repin)
    worker.start()
    assert not finished.wait(timeout=0.2)
    assert project_config.resolve(first).kb_dir == old_vault
    connection.close()
    assert finished.wait(timeout=3)
    worker.join(timeout=3)

    assert not errors
    assert project_config.resolve(first).kb_dir == replacement
    assert project_config.resolve(second).kb_dir == replacement


def test_direct_scope_creation_drains_inherited_scope_access(
    private_aliases: tuple[Path, Path, Path],
) -> None:
    first, _second, _vault = private_aliases
    child = first / "new-private-boundary"
    child.mkdir()
    connection = db.connect(str(child))
    finished = threading.Event()
    errors: list[BaseException] = []

    def create_boundary() -> None:
        try:
            project_config.create_scope(
                child,
                policy=project_config.POLICY_PRIVATE,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=create_boundary)
    worker.start()
    assert not finished.wait(timeout=0.2)
    connection.close()
    assert finished.wait(timeout=3)
    worker.join(timeout=3)

    assert not errors
    assert project_config.resolve(child).state == project_config.MODE_LOCKED
    assert project_config.resolve(first).state == project_config.MODE_LATCHED


def test_direct_off_boundary_waits_for_inherited_scope_access(
    private_aliases: tuple[Path, Path, Path],
) -> None:
    first, _second, _vault = private_aliases
    child = first / "new-off-boundary"
    child.mkdir()
    connection = db.connect(str(child))
    finished = threading.Event()
    errors: list[BaseException] = []

    def create_boundary() -> None:
        try:
            project_config.create_off_boundary(child)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=create_boundary)
    worker.start()
    assert not finished.wait(timeout=0.2)
    connection.close()
    assert finished.wait(timeout=3)
    worker.join(timeout=3)

    assert not errors
    assert project_config.resolve(child).state == project_config.MODE_UNLATCHED
    assert project_config.resolve(first).state == project_config.MODE_LATCHED


def test_direct_shared_to_private_waits_for_global_scope_access(
    private_aliases: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    _first, _second, _vault = private_aliases
    test_root = paths.validated_test_root()
    assert test_root is not None
    home = tmp_path / "latch-home"
    shared_vault = test_root / "vaults" / f"direct-shared-{tmp_path.name}"
    private_vault = test_root / "vaults" / f"direct-private-{tmp_path.name}"
    shared_vault.mkdir(parents=True)
    private_vault.mkdir(parents=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(shared_vault)}) + "\n",
        encoding="utf-8",
    )
    shared_root = tmp_path / "shared-client"
    shared_root.mkdir()
    project_config.create_scope(
        shared_root,
        policy=project_config.POLICY_SHARED,
    )
    project_config.authorize_scope(shared_root)
    connection = db.connect(str(shared_root))
    finished = threading.Event()
    errors: list[BaseException] = []

    def convert() -> None:
        try:
            project_config.convert_shared_scope_to_private(
                shared_root,
                private_vault,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=convert)
    worker.start()
    assert not finished.wait(timeout=0.2)
    assert project_config.resolve(shared_root).policy == project_config.POLICY_SHARED
    connection.close()
    assert finished.wait(timeout=3)
    worker.join(timeout=3)

    assert not errors
    converted = project_config.resolve(shared_root)
    assert converted.policy == project_config.POLICY_PRIVATE
    assert converted.kb_dir == private_vault


def test_queued_access_rejects_binding_changed_before_lock(
    private_aliases: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, _second, _vault = private_aliases
    entered_wait = threading.Event()
    original_lock = lockfile._advisory_lock
    owner_thread = threading.get_ident()
    errors: list[BaseException] = []

    def delayed_lock(fd: int, *, exclusive: bool) -> None:
        if threading.get_ident() != owner_thread:
            entered_wait.set()
        original_lock(fd, exclusive=exclusive)

    monkeypatch.setattr(lockfile, "_advisory_lock", delayed_lock)

    def access() -> None:
        try:
            with lockfile.project_access_lock(str(first)):
                raise AssertionError("stale access body must not run")
        except BaseException as exc:
            errors.append(exc)

    with lockfile.project_access_lock(str(first), exclusive=True):
        worker = threading.Thread(target=access)
        worker.start()
        assert entered_wait.wait(timeout=3)
        project_config.set_scope_mode(first, project_config.MODE_UNLATCHED)
    worker.join(timeout=3)

    assert len(errors) == 1
    assert isinstance(errors[0], lockfile.ProjectTargetChangedError)
    assert errors[0].reason == "target_changed"


def test_mutation_refreshes_outer_exclusive_lease_for_create_authorize(
    private_aliases: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    _first, _second, _vault = private_aliases
    test_root = paths.validated_test_root()
    assert test_root is not None
    root = tmp_path / "fresh-strict-root"
    vault = test_root / "vaults" / f"fresh-strict-{tmp_path.name}"
    root.mkdir()
    vault.mkdir(parents=True)

    initial_lock = project_config.access_lock_path(project_config.resolve(root))
    with lockfile.project_access_lock(str(root), exclusive=True):
        project_config.create_scope(
            root,
            policy=project_config.POLICY_PRIVATE,
        )
        locked = project_config.resolve(root)
        assert locked.state == project_config.MODE_LOCKED
        assert project_config.access_lock_path(locked) == initial_lock
        project_config.authorize_scope(root, kb_dir=vault)

    authorized = project_config.resolve(root)
    assert authorized.state == project_config.MODE_LATCHED
    assert authorized.kb_dir == vault


def test_new_scope_handoff_drains_old_access_and_stays_locked_until_authorized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "scope-handoff-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    global_kb = test_root / "vaults" / f"handoff-global-{tmp_path.name}"
    private_kb = test_root / "vaults" / f"handoff-private-{tmp_path.name}"
    global_kb.mkdir(parents=True)
    private_kb.mkdir(parents=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(global_kb)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(project_mode, "_disable_instructions", lambda _roots: [])
    monkeypatch.setattr(project_mode, "_enable_instructions", lambda _roots: [])
    project_config.write_machine_policy(
        project_config.MACHINE_POLICY_COMPATIBILITY
    )
    project_config.initialize_compatibility_binding()
    root = tmp_path / "new-client"
    root.mkdir()

    old_access = lockfile.project_access_lock(str(root))
    assert old_access.__enter__() == global_kb
    binding_written = threading.Event()
    authorize_root = threading.Event()
    root_authorized = threading.Event()
    finish_transition = threading.Event()
    errors: list[BaseException] = []
    original_write = project_config._write_root_scope_authorization

    def controlled_authorization(*args, **kwargs):
        binding_written.set()
        assert authorize_root.wait(timeout=5)
        result = original_write(*args, **kwargs)
        root_authorized.set()
        assert finish_transition.wait(timeout=5)
        return result

    monkeypatch.setattr(
        project_config,
        "_write_root_scope_authorization",
        controlled_authorization,
    )

    def create_private_scope() -> None:
        try:
            project_mode.apply_latch(
                root,
                policy=project_config.POLICY_PRIVATE,
                kb_dir=str(private_kb),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=create_private_scope)
    worker.start()
    assert not binding_written.wait(timeout=0.2)
    old_access.__exit__(None, None, None)
    assert binding_written.wait(timeout=5)

    intermediate = project_config.resolve(root)
    assert intermediate.state == project_config.MODE_LOCKED
    assert intermediate.reason_code == project_config.LOCK_UNAUTHORIZED_ROOT
    with pytest.raises(lockfile.ProjectTargetChangedError) as blocked:
        with lockfile.project_access_lock(str(root)):
            pass
    assert blocked.value.reason == "locked"

    authorize_root.set()
    assert root_authorized.wait(timeout=5)
    with lockfile.project_access_lock(str(root)) as selected:
        assert selected == private_kb

    finish_transition.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors


def test_explicit_scope_migration_drains_shared_global_access_before_policy_flip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "explicit-migration-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    global_kb = test_root / "vaults" / f"migration-global-{tmp_path.name}"
    global_kb.mkdir(parents=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(global_kb)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(project_mode, "_disable_instructions", lambda _roots: [])
    monkeypatch.setattr(project_mode, "_enable_instructions", lambda _roots: [])
    project_config.write_machine_policy(
        project_config.MACHINE_POLICY_COMPATIBILITY
    )
    project_config.initialize_compatibility_binding()
    active_root = tmp_path / "active-global-session"
    migration_root = tmp_path / "migration-root"
    active_root.mkdir()
    migration_root.mkdir()

    old_access = lockfile.project_access_lock(str(active_root))
    assert old_access.__enter__() == global_kb
    finished = threading.Event()
    errors: list[BaseException] = []

    def migrate() -> None:
        try:
            project_mode.apply_latch(
                migration_root,
                policy=project_config.POLICY_SHARED,
                require_explicit_scopes=True,
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=migrate)
    worker.start()
    assert not finished.wait(timeout=0.2)
    assert (
        project_config.read_machine_policy()
        == project_config.MACHINE_POLICY_COMPATIBILITY
    )

    old_access.__exit__(None, None, None)
    assert finished.wait(timeout=5)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_EXPLICIT
    )
    migrated = project_config.resolve(migration_root)
    assert migrated.state == project_config.MODE_LATCHED
    assert migrated.policy == project_config.POLICY_SHARED
    assert migrated.kb_dir == global_kb
