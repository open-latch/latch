"""Regression contract for the exact existing-install compatibility binding.

Compatibility is a migration allowance, not ambient authority.  The installer
must bind the exact already-pinned global KB into machine-local control state;
runtime resolution may not recreate that authority from ``kb_location.json``.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

import db
import install_engine
import paths
import project_config


@pytest.fixture
def compatibility_binding_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Path]:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "compatibility-binding-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    vault = test_root / "vaults" / "compatibility-binding" / tmp_path.name
    vault.mkdir(parents=True)
    project = tmp_path / "legacy-project"
    project.mkdir()
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(vault)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.delenv("LATCH_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    return {
        "control": control,
        "home": home,
        "project": project,
        "vault": vault,
    }


def _pin(home: Path, vault: Path) -> None:
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(vault)}) + "\n",
        encoding="utf-8",
    )


def _file_snapshot(root: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(root)): (
            path.read_bytes(),
            path.stat().st_mode,
            path.stat().st_mtime_ns,
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _migrate_existing_install() -> None:
    level, message = install_engine.configure_scope_policy(
        existing_pin_before_install=True,
        dry_run=False,
    )
    assert level == "OK", message
    level, message = install_engine.configure_compatibility_binding(
        dry_run=False,
    )
    assert level == "OK", message


def _adopt_first_identity(
    project: Path,
    *,
    session_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[project_config.ResolvedScope, str]:
    initial = project_config.resolve(project)
    assert initial.state == project_config.MODE_LATCHED
    assert initial.source == project_config.SOURCE_COMPATIBILITY
    assert initial.vault_uuid is None
    assert project_config.record_session_binding(project, session_id) == initial.revision
    monkeypatch.setenv("CODEX_THREAD_ID", session_id)
    connection = db.connect(str(project))
    try:
        vault_uuid = connection._kb_vault_identity.vault_uuid
    finally:
        connection.close()
    return initial, vault_uuid


def test_compatibility_policy_without_persisted_binding_is_locked(
    compatibility_binding_env: dict[str, Path],
) -> None:
    project_config.write_machine_policy(
        project_config.MACHINE_POLICY_COMPATIBILITY
    )
    assert not project_config.compatibility_binding_path().exists()

    target = project_config.resolve(compatibility_binding_env["project"])

    assert target.state == project_config.MODE_LOCKED
    assert target.kb_dir is None
    assert target.source == project_config.SOURCE_COMPATIBILITY
    assert "compatibility" in (target.reason or "").lower()
    assert "binding" in (target.reason or "").lower()


def test_installer_migration_persists_exact_binding_without_mutating_kb(
    compatibility_binding_env: dict[str, Path],
) -> None:
    vault = compatibility_binding_env["vault"]
    canary = vault / "existing-knowledge.bin"
    canary.write_bytes(b"existing customer knowledge\x00must stay byte exact")
    existing_uuid = str(uuid.uuid4())
    connection = sqlite3.connect(vault / "kb.db")
    try:
        connection.execute(
            "CREATE TABLE vault_identity "
            "(slot INTEGER PRIMARY KEY, vault_uuid TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO vault_identity (slot, vault_uuid) VALUES (1, ?)",
            (existing_uuid,),
        )
        connection.commit()
    finally:
        connection.close()
    before = _file_snapshot(vault)

    _migrate_existing_install()

    assert _file_snapshot(vault) == before
    binding_path = project_config.compatibility_binding_path()
    assert binding_path.is_file()
    assert not binding_path.is_relative_to(vault)
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    assert set(payload) == {
        "format",
        "kb_dir",
        "kb_fingerprint",
        "vault_uuid",
        "revision",
    }
    assert payload["format"] == project_config.FORMAT_VERSION
    assert Path(payload["kb_dir"]) == vault.resolve()
    assert payload["vault_uuid"] == existing_uuid
    assert len(payload["kb_fingerprint"]) == 64
    assert len(payload["revision"]) == 32

    target = project_config.resolve(compatibility_binding_env["project"])
    assert target.state == project_config.MODE_LATCHED
    assert target.kb_dir == vault.resolve()
    assert target.target_fingerprint == payload["kb_fingerprint"]
    assert project_config._load_compatibility_binding().revision == payload["revision"]
    assert len(target.target_revision) == 32


def test_first_uuid_adoption_preserves_target_effective_revision_and_session(
    compatibility_binding_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _migrate_existing_install()
    project = compatibility_binding_env["project"]
    initial, vault_uuid = _adopt_first_identity(
        project,
        session_id="compat-first-open",
        monkeypatch=monkeypatch,
    )

    finalized = project_config.resolve(project)
    binding = project_config._load_compatibility_binding()
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.vault_uuid == vault_uuid
    assert binding.vault_uuid == vault_uuid
    assert finalized.target_revision == initial.target_revision
    assert finalized.revision == initial.revision
    assert project_config.current_session_revision(
        project,
        "compat-first-open",
    ) == initial.revision

    reopened = db.connect(str(project))
    try:
        assert reopened._kb_vault_identity.vault_uuid == vault_uuid
    finally:
        reopened.close()


@pytest.mark.parametrize("replacement", ["missing", "different"])
def test_bound_compatibility_uuid_missing_or_different_in_same_directory_locks(
    compatibility_binding_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    _migrate_existing_install()
    project = compatibility_binding_env["project"]
    initial, original_uuid = _adopt_first_identity(
        project,
        session_id=f"compat-{replacement}",
        monkeypatch=monkeypatch,
    )
    vault = compatibility_binding_env["vault"]
    original_directory = vault.stat()

    connection = sqlite3.connect(vault / "kb.db")
    try:
        if replacement == "missing":
            connection.execute("DROP TABLE vault_identity")
        connection.commit()
    finally:
        connection.close()
    if replacement == "different":
        replacement_db = vault / "replacement.db"
        connection = sqlite3.connect(replacement_db)
        try:
            connection.execute(
                "CREATE TABLE vault_identity "
                "(slot INTEGER PRIMARY KEY, vault_uuid TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO vault_identity (slot, vault_uuid) VALUES (1, ?)",
                (str(uuid.uuid4()),),
            )
            connection.commit()
        finally:
            connection.close()
        replacement_db.replace(vault / "kb.db")

    current_directory = vault.stat()
    assert (current_directory.st_dev, current_directory.st_ino) == (
        original_directory.st_dev,
        original_directory.st_ino,
    )
    target = project_config.resolve(project)
    assert target.state == project_config.MODE_LOCKED
    assert target.kb_dir is None
    assert target.target_revision == initial.target_revision
    assert target.vault_uuid == original_uuid
    assert "identity" in (target.reason or "").lower()
    assert project_config.current_session_revision(
        project,
        f"compat-{replacement}",
    ) is None


def test_global_pin_change_locks_until_explicit_compatibility_reauthorization(
    compatibility_binding_env: dict[str, Path],
) -> None:
    _migrate_existing_install()
    home = compatibility_binding_env["home"]
    project = compatibility_binding_env["project"]
    initial = project_config.resolve(project)
    project_config.record_session_binding(project, "pre-repin-task")
    replacement = compatibility_binding_env["vault"].parent / (
        compatibility_binding_env["vault"].name + "-replacement"
    )
    replacement.mkdir()
    _pin(home, replacement)

    locked = project_config.resolve(project)
    assert locked.state == project_config.MODE_LOCKED
    assert locked.kb_dir is None
    assert locked.remembered_kb_dir == initial.kb_dir
    assert "pin changed" in (locked.reason or "").lower()

    project_config.reauthorize_compatibility_binding()
    repaired = project_config.resolve(project)
    assert repaired.state == project_config.MODE_LATCHED
    assert repaired.kb_dir == replacement.resolve()
    assert repaired.target_revision != initial.target_revision
    assert repaired.revision != initial.revision
    assert project_config.current_session_revision(
        project,
        "pre-repin-task",
    ) is None


def test_conflicting_global_environment_aliases_lock_compatibility_scope(
    compatibility_binding_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _migrate_existing_install()
    vault = compatibility_binding_env["vault"]
    replacement = vault.parent / f"{vault.name}-environment-conflict"
    replacement.mkdir()
    monkeypatch.setenv("LATCH_KB_DIR", str(vault))
    monkeypatch.setenv("CLAUDE_KB_DIR", str(replacement))

    target = project_config.resolve(compatibility_binding_env["project"])

    assert target.state == project_config.MODE_LOCKED
    assert target.kb_dir is None
    assert target.reason_code == project_config.LOCK_GLOBAL_PIN_CHANGED
    assert "select different global KBs" in (target.reason or "")


def test_first_global_uuid_finalizes_compatibility_and_every_shared_binding(
    compatibility_binding_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _migrate_existing_install()
    legacy = compatibility_binding_env["project"]
    first = tmp_path / "shared-first"
    second = tmp_path / "shared-second"
    first.mkdir()
    second.mkdir()
    first_marker = project_config.create_scope(
        first,
        policy=project_config.POLICY_SHARED,
    )
    second_marker = project_config.create_scope(
        second,
        policy=project_config.POLICY_SHARED,
    )
    first_initial = project_config.authorize_scope(first)
    second_initial = project_config.authorize_scope(second)
    legacy_initial = project_config.resolve(legacy)
    assert first_initial.vault_uuid is None
    assert second_initial.vault_uuid is None
    assert legacy_initial.vault_uuid is None

    for root, session_id in (
        (legacy, "legacy-shared-task"),
        (first, "first-shared-task"),
        (second, "second-shared-task"),
    ):
        project_config.record_session_binding(root, session_id)

    monkeypatch.setenv("CODEX_THREAD_ID", "first-shared-task")
    connection = db.connect(str(first))
    try:
        vault_uuid = connection._kb_vault_identity.vault_uuid
    finally:
        connection.close()

    first_final = project_config.resolve(first)
    second_final = project_config.resolve(second)
    legacy_final = project_config.resolve(legacy)
    for initial, final in (
        (first_initial, first_final),
        (second_initial, second_final),
        (legacy_initial, legacy_final),
    ):
        assert final.state == project_config.MODE_LATCHED
        assert final.vault_uuid == vault_uuid
        assert final.target_revision == initial.target_revision
        assert final.revision == initial.revision

    assert project_config._load_scope_binding(
        first_marker.scope_id
    ).vault_uuid == vault_uuid
    assert project_config._load_scope_binding(
        second_marker.scope_id
    ).vault_uuid == vault_uuid
    assert project_config._load_compatibility_binding().vault_uuid == vault_uuid
    assert project_config.current_session_revision(
        legacy,
        "legacy-shared-task",
    ) == legacy_initial.revision
    assert project_config.current_session_revision(
        first,
        "first-shared-task",
    ) == first_initial.revision
    assert project_config.current_session_revision(
        second,
        "second-shared-task",
    ) == second_initial.revision

    monkeypatch.setenv("CODEX_THREAD_ID", "second-shared-task")
    reopened = db.connect(str(second))
    try:
        assert reopened._kb_vault_identity.vault_uuid == vault_uuid
    finally:
        reopened.close()
