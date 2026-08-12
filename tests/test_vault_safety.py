from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import install_engine  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402
import project_config  # noqa: E402
import uninstall_engine  # noqa: E402
import vault_identity  # noqa: E402


def _new_test_vault(scope: Path) -> tuple[Path, vault_identity.VaultIdentity]:
    scope.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(scope))
    try:
        identity = conn._kb_vault_identity
    finally:
        conn.close()
    return paths.project_dir(str(scope)), identity


def test_new_vault_is_test_and_identity_is_sqlite_immutable(tmp_path):
    vault, identity = _new_test_vault(tmp_path)
    assert identity.classification == vault_identity.CLASS_TEST
    conn = sqlite3.connect(vault / "kb.db")
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE vault_identity SET classification='production'")
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("DELETE FROM vault_identity")
    finally:
        conn.close()


def test_unidentified_existing_database_is_adopted_as_production(tmp_path):
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    test_root = paths.validated_test_root()
    assert test_root is not None
    vault = test_root / "vaults" / f"legacy-adoption-{tmp_path.name}"
    vault.mkdir(parents=True)
    legacy = sqlite3.connect(vault / "kb.db")
    try:
        legacy.executescript((ROOT / "src" / "schema.sql").read_text(encoding="utf-8"))
        legacy.execute("DROP TABLE vault_identity")
        legacy.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','legacy','must survive','canonical')"
        )
        legacy.commit()
    finally:
        legacy.close()

    project_config.create_scope(
        tmp_path,
        policy=project_config.POLICY_PRIVATE,
    )
    project_config.authorize_scope(tmp_path, kb_dir=vault)

    conn = db.connect(str(tmp_path))
    try:
        identity = conn._kb_vault_identity
        assert identity.classification == vault_identity.CLASS_PRODUCTION
    finally:
        conn.close()

    with pytest.raises(vault_identity.ProductionVaultDeletionRefused):
        vault_identity.safe_delete_test_vault(
            vault,
            expected_uuid=identity.vault_uuid,
            capability=os.environ[paths.TEST_CAPABILITY_ENV],
        )
    assert (vault / "kb.db").is_file()


def test_test_delete_requires_capability_uuid_registry_and_containment(tmp_path):
    vault, identity = _new_test_vault(tmp_path / "good")
    capability = os.environ[paths.TEST_CAPABILITY_ENV]

    with pytest.raises(vault_identity.VaultSafetyError, match="capability"):
        vault_identity.safe_delete_test_vault(
            vault, expected_uuid=identity.vault_uuid, capability="wrong"
        )
    with pytest.raises(vault_identity.VaultSafetyError, match="UUID"):
        vault_identity.safe_delete_test_vault(
            vault, expected_uuid="00000000-0000-0000-0000-000000000000",
            capability=capability,
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(vault_identity.VaultSafetyError, match="outside"):
        vault_identity.safe_delete_test_vault(
            outside, expected_uuid=identity.vault_uuid, capability=capability
        )

    vault_identity.safe_delete_test_vault(
        vault, expected_uuid=identity.vault_uuid, capability=capability
    )
    assert not vault.exists()


def test_symlink_and_missing_registry_fail_closed(tmp_path):
    vault, identity = _new_test_vault(tmp_path / "symlink")
    alias = vault.parent / "vault-alias"
    try:
        alias.symlink_to(vault, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    with pytest.raises(vault_identity.VaultSafetyError, match="symlinked"):
        vault_identity.safe_delete_test_vault(
            alias,
            expected_uuid=identity.vault_uuid,
            capability=os.environ[paths.TEST_CAPABILITY_ENV],
        )
    assert vault.exists()

    registry = paths.validated_test_root() / "registry" / f"{identity.vault_uuid}.json"
    registry.chmod(0o600)
    registry.unlink()
    with pytest.raises(vault_identity.VaultSafetyError, match="registry"):
        db.connect(str(tmp_path / "symlink"))
    assert vault.exists()


def test_partial_or_forged_test_environment_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.TEST_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(paths.TEST_CAPABILITY_ENV, raising=False)
    with pytest.raises(
        (paths.UnsafeTestExecutionError, project_config.ProjectConfigError),
        match="incomplete",
    ):
        paths.project_dir("scope")

    (tmp_path / paths.TEST_SENTINEL).write_text(
        json.dumps({"format": 1, "capability_sha256": "0" * 64}),
        encoding="utf-8",
    )
    monkeypatch.setenv(paths.TEST_CAPABILITY_ENV, "forged")
    with pytest.raises(
        (paths.UnsafeTestExecutionError, project_config.ProjectConfigError),
        match="does not match",
    ):
        paths.project_dir("scope")


def test_direct_test_runner_refuses_before_pinned_vault_cleanup(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    sentinel = production / "DO_NOT_DELETE"
    sentinel.write_text("critical", encoding="utf-8")
    env = os.environ.copy()
    env.pop(paths.TEST_ROOT_ENV, None)
    env.pop(paths.TEST_CAPABILITY_ENV, None)
    env["LATCH_KB_DIR"] = str(production)
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "test_seed.py")],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "LOCKED" in output or "direct test execution is not allowed" in output
    assert sentinel.read_text(encoding="utf-8") == "critical"
    assert not (production / "kb.db").exists()


def test_subprocess_inherits_authenticated_test_root(tmp_path):
    child_scope = tmp_path / "child-scope"
    child_scope.mkdir()
    code = (
        f"import json,paths; p=paths.project_dir({str(child_scope)!r}).resolve(); "
        "print(json.dumps({'path':str(p),'root':str(paths.validated_test_root())}))"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert Path(payload["path"]).is_relative_to(Path(payload["root"]) / "vaults")


def test_install_refuses_source_checkout_production_target(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(install_engine, "KB_HOME", source)
    monkeypatch.setattr(install_engine, "KB_LOCATION_PATH", source / "kb_location.json")
    monkeypatch.setattr(install_engine, "PROJECTS_DIR", source / "projects")
    level, message = install_engine.pin_kb_dir(str(source / "store"), dry_run=True)
    assert level == "FAIL"
    assert "inside the source checkout" in message


def test_identity_layer_refuses_new_production_vault_inside_source(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.TEST_ROOT_ENV, raising=False)
    monkeypatch.delenv(paths.TEST_CAPABILITY_ENV, raising=False)
    source = tmp_path / "source"
    vault = source / "store"
    vault.mkdir(parents=True)
    monkeypatch.setattr(paths, "KB_ROOT", source)
    conn = sqlite3.connect(vault / "kb.db")
    try:
        with pytest.raises(vault_identity.VaultSafetyError, match="source checkout"):
            vault_identity.ensure_identity(conn, vault, new_vault=True)
    finally:
        conn.close()


def test_uninstall_purge_never_deletes_kb_data(tmp_path, monkeypatch):
    source = tmp_path / "source"
    vault = source / "projects" / "critical"
    vault.mkdir(parents=True)
    marker = vault / "kb.db"
    marker.write_bytes(b"critical")
    monkeypatch.setattr(uninstall_engine, "KB_HOME", source)
    changes = uninstall_engine.purge_data(dry_run=False)
    assert marker.read_bytes() == b"critical"
    assert any("no data-deletion path" in change for change in changes)
