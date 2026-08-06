"""Database leases complete the exact scope identity handshake."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

import db
import lockfile
import paths
import project_config


@pytest.fixture
def explicit_scope_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "db-scope-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    return test_root


def _new_private_scope(
    test_root: Path,
    tmp_path: Path,
    *,
    name: str,
) -> tuple[Path, Path, project_config.ResolvedScope]:
    root = tmp_path / name
    root.mkdir()
    vault = test_root / "vaults" / f"{name}-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    target = project_config.authorize_scope(root, kb_dir=vault)
    assert target.vault_uuid is None
    return root, vault, target


def _mature_private_scope(
    test_root: Path,
    tmp_path: Path,
    *,
    name: str,
) -> tuple[Path, Path, project_config.ResolvedScope]:
    root, vault, _initial = _new_private_scope(
        test_root,
        tmp_path,
        name=name,
    )
    connection = db.connect(str(root))
    connection.close()
    target = project_config.resolve(root)
    assert target.state == project_config.MODE_LATCHED
    assert target.vault_uuid is not None
    return root, vault, target


def _identity_rows(vault: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(str(vault / "kb.db"))
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='vault_identity'"
        ).fetchone()
        if table is None:
            return []
        return connection.execute(
            "SELECT vault_uuid, classification, created_at, "
            "registry_fingerprint FROM vault_identity ORDER BY slot"
        ).fetchall()
    finally:
        connection.close()


def _vault_file_snapshot(vault: Path) -> dict[str, bytes]:
    return {
        entry.name: entry.read_bytes()
        for entry in sorted(vault.iterdir())
        if entry.is_file() and not entry.is_symlink()
    }


def _forbid_writer_lock(*_args, **_kwargs):
    raise AssertionError("initialized current-schema open took the writer sentinel")


def _swap_database_only_while_opening(
    monkeypatch: pytest.MonkeyPatch,
    selected_db: Path,
    foreign_db: Path,
) -> dict[str, bool]:
    """Open the foreign inode, then restore both names before revalidation."""
    original_connect = db.sqlite3.connect
    parked_expected = selected_db.with_name("kb.db.expected")
    swapped = {"value": False}

    def swapped_connect(database, *args, **kwargs):
        candidate = str(database)
        opens_selected = (
            candidate == str(selected_db)
            or candidate.startswith(selected_db.as_uri() + "?")
        )
        if (
            not swapped["value"]
            and kwargs.get("factory") is db._Connection
            and opens_selected
        ):
            selected_db.rename(parked_expected)
            foreign_db.rename(selected_db)
            try:
                connection = original_connect(database, *args, **kwargs)
                try:
                    selected_db.rename(foreign_db)
                except OSError as exc:
                    # Windows may forbid renaming an open SQLite file, so this
                    # specific post-open pathname restoration race cannot be
                    # staged there. Close and restore both real vaults first.
                    connection.close()
                    selected_db.rename(foreign_db)
                    parked_expected.rename(selected_db)
                    pytest.skip(
                        f"platform cannot stage an open-file rename race: {exc}"
                    )
                parked_expected.rename(selected_db)
            except BaseException:
                if selected_db.exists() and not foreign_db.exists():
                    selected_db.rename(foreign_db)
                if parked_expected.exists() and not selected_db.exists():
                    parked_expected.rename(selected_db)
                raise
            swapped["value"] = True
            return connection
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", swapped_connect)
    return swapped


def test_initialized_current_schema_open_skips_writer_but_validates_registry(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _vault, target = _mature_private_scope(
        explicit_scope_env,
        tmp_path,
        name="current-schema-fast-path",
    )
    monkeypatch.setattr(db.lockfile, "writer_lock", _forbid_writer_lock)

    connection = db.connect(str(root))
    try:
        assert connection._kb_vault_identity.vault_uuid == target.vault_uuid
    finally:
        connection.close()

    missing_registry = tmp_path / "missing-fast-path-registry.json"
    monkeypatch.setattr(
        db.vault_identity,
        "_registry_path",
        lambda _identity: missing_registry,
    )
    with pytest.raises(
        db.vault_identity.VaultSafetyError,
        match="registry missing or unreadable",
    ):
        db.connect(str(root))


def _seed_legacy_nodes_table(vault: Path) -> None:
    """Make the vault pre-existing without giving it an immutable identity."""
    connection = sqlite3.connect(str(vault / "kb.db"))
    try:
        schema = Path(db.SCHEMA_PATH).read_text(encoding="utf-8")
        _identity_prefix, nodes_and_after = schema.split(
            "CREATE TABLE IF NOT EXISTS nodes (",
            maxsplit=1,
        )
        connection.executescript(
            "PRAGMA journal_mode = WAL;\n"
            "PRAGMA foreign_keys = ON;\n"
            "CREATE TABLE IF NOT EXISTS nodes ("
            + nodes_and_after
        )
    finally:
        connection.close()


def _interrupt_before_identity_row(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    vault: Path,
    target: project_config.ResolvedScope,
) -> tuple[project_config.ResolvedScope, Path]:
    original_ensure = db.vault_identity.ensure_identity

    def crash_before_identity(*args, **kwargs):
        raise RuntimeError("simulated crash before identity row")

    monkeypatch.setattr(
        db.vault_identity,
        "ensure_identity",
        crash_before_identity,
    )
    with pytest.raises(RuntimeError, match="before identity row"):
        db.connect(str(root))
    monkeypatch.setattr(db.vault_identity, "ensure_identity", original_ensure)

    # Model a process death after the identity schema reached durable storage
    # but before its one immutable row was committed. Some legacy migration
    # paths may roll the DDL back with the failed connection, so persist only
    # that zero-row table explicitly; the production recovery must do the rest.
    connection = sqlite3.connect(str(vault / "kb.db"))
    try:
        db.vault_identity._ensure_schema(connection)
        connection.commit()
    finally:
        connection.close()

    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert (
        pending.reason_code
        == project_config.LOCK_VAULT_IDENTITY_INITIALIZING
    )
    assert pending.target_revision == target.target_revision
    assert _identity_rows(vault) == []
    receipt = db._initialization_receipt_path(target, vault)
    assert receipt.is_file()
    return pending, receipt


def _interrupt_before_private_binding(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    vault: Path,
    target: project_config.ResolvedScope,
) -> tuple[project_config.ResolvedScope, Path]:
    original_finalize = project_config.finalize_scope_vault_identity

    def crash_before_binding(*args, **kwargs):
        raise RuntimeError("simulated crash before private binding finalization")

    monkeypatch.setattr(
        project_config,
        "finalize_scope_vault_identity",
        crash_before_binding,
    )
    with pytest.raises(RuntimeError, match="private binding finalization"):
        db.connect(str(root))
    monkeypatch.setattr(
        project_config,
        "finalize_scope_vault_identity",
        original_finalize,
    )

    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert pending.reason_code == project_config.LOCK_VAULT_IDENTITY_PENDING
    receipt = db._initialization_receipt_path(target, vault)
    assert receipt.is_file()
    return pending, receipt


def test_first_private_db_open_finalizes_identity_and_reopens(
    explicit_scope_env: Path, tmp_path: Path,
) -> None:
    root = tmp_path / "client"
    root.mkdir()
    vault = explicit_scope_env / "vaults" / f"private-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    initial = project_config.authorize_scope(root, kb_dir=vault)
    assert initial.vault_uuid is None

    first = db.connect(str(root))
    identity = first._kb_vault_identity
    with lockfile.writer_lock(str(root)):
        pass
    first.close()

    finalized = project_config.resolve(root)
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.revision == initial.revision
    assert finalized.vault_uuid == identity.vault_uuid

    second = db.connect(str(root))
    try:
        assert second._kb_vault_identity.vault_uuid == identity.vault_uuid
    finally:
        second.close()


def test_new_vault_records_absence_before_exclusive_database_creation(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="receipt-before-create",
    )
    database = vault / "kb.db"
    receipt_path = db._initialization_receipt_path(initial, vault)
    original_create = db._create_new_database_file
    original_sqlite_connect = db.sqlite3.connect
    observed = {"prepared": False, "exact_before_sqlite": False}

    def inspect_before_create(path: Path) -> str:
        assert path == database
        assert not database.exists()
        receipt = db._read_initialization_receipt(initial, vault)
        assert receipt is not None
        assert receipt["phase"] == db._INITIALIZATION_PHASE_PREPARED
        assert receipt["new_vault"] is True
        assert receipt["db_fingerprint"] is None
        observed["prepared"] = True
        return original_create(path)

    monkeypatch.setattr(db, "_create_new_database_file", inspect_before_create)

    def inspect_sqlite_open(database_value, *args, **kwargs):
        if kwargs.get("factory") is db._Connection:
            assert database_value == database.as_uri() + "?mode=rw"
            assert kwargs.get("uri") is True
            assert db._load_initialization_receipt(initial, vault) is True
            observed["exact_before_sqlite"] = True
        return original_sqlite_connect(database_value, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", inspect_sqlite_open)
    connection = db.connect(str(root))
    connection.close()

    assert observed["prepared"]
    assert observed["exact_before_sqlite"]
    assert database.is_file()
    assert not receipt_path.exists()


def test_prepared_receipt_with_no_database_resumes_exclusive_creation(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="crash-before-create",
    )
    original_create = db._create_new_database_file

    def crash_before_create(_path: Path) -> None:
        raise RuntimeError("simulated crash before exclusive database creation")

    monkeypatch.setattr(db, "_create_new_database_file", crash_before_create)
    with pytest.raises(RuntimeError, match="before exclusive database creation"):
        db.connect(str(root))

    receipt = db._read_initialization_receipt(initial, vault)
    assert receipt is not None
    assert receipt["phase"] == db._INITIALIZATION_PHASE_PREPARED
    assert not (vault / "kb.db").exists()

    monkeypatch.setattr(db, "_create_new_database_file", original_create)
    connection = db.connect(str(root))
    connection.close()
    assert project_config.resolve(root).vault_uuid is not None


def test_crash_after_database_creation_without_exact_receipt_never_adopts_foreign_db(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="crash-after-create",
    )
    receipt_path = db._initialization_receipt_path(initial, vault)
    binding_path = project_config.scope_binding_path(str(initial.scope_id))
    binding_before = binding_path.read_bytes()
    original_atomic_json = project_config.atomic_json

    def crash_before_exact_receipt(path, payload, *, mode=0o600):
        if (
            Path(path) == receipt_path
            and payload.get("phase") == db._INITIALIZATION_PHASE_EXACT
        ):
            raise RuntimeError("simulated crash before exact database receipt")
        return original_atomic_json(path, payload, mode=mode)

    monkeypatch.setattr(project_config, "atomic_json", crash_before_exact_receipt)
    with pytest.raises(RuntimeError, match="before exact database receipt"):
        db.connect(str(root))
    monkeypatch.setattr(project_config, "atomic_json", original_atomic_json)

    database = vault / "kb.db"
    assert database.is_file()
    receipt = db._read_initialization_receipt(initial, vault)
    assert receipt is not None
    assert receipt["phase"] == db._INITIALIZATION_PHASE_PREPARED

    # Model an unrelated process populating the unproven inode after the crash.
    foreign = sqlite3.connect(str(database))
    foreign.execute("CREATE TABLE canary(secret TEXT NOT NULL)")
    foreign.execute("INSERT INTO canary VALUES('must survive')")
    foreign.commit()
    foreign.close()
    foreign_before = database.read_bytes()

    with pytest.raises(
        project_config.ProjectConfigError,
        match="before recording the exact database origin",
    ):
        db.connect(str(root))

    assert database.read_bytes() == foreign_before
    check = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert check.execute("SELECT secret FROM canary").fetchall() == [
            ("must survive",)
        ]
        tables = {
            str(row[0])
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "nodes" not in tables
        assert "vault_identity" not in tables
    finally:
        check.close()
    assert binding_path.read_bytes() == binding_before
    assert receipt_path.is_file()
    assert project_config.resolve(root).vault_uuid is None


def test_database_replacement_before_exact_receipt_cannot_gain_new_vault_authority(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="replace-before-exact-receipt",
    )
    database = vault / "kb.db"
    parked_owned = vault / "owned-empty.db"
    foreign_path = vault / "foreign.db"
    foreign = sqlite3.connect(str(foreign_path))
    foreign.execute("CREATE TABLE canary(secret TEXT NOT NULL)")
    foreign.execute("INSERT INTO canary VALUES('foreign inode')")
    foreign.commit()
    foreign.close()
    foreign_before = foreign_path.read_bytes()
    receipt_path = db._initialization_receipt_path(initial, vault)
    original_atomic_json = project_config.atomic_json
    swapped = {"value": False}

    def swap_before_exact_receipt(path, payload, *, mode=0o600):
        if (
            not swapped["value"]
            and Path(path) == receipt_path
            and payload.get("phase") == db._INITIALIZATION_PHASE_EXACT
        ):
            database.rename(parked_owned)
            foreign_path.rename(database)
            swapped["value"] = True
        return original_atomic_json(path, payload, mode=mode)

    monkeypatch.setattr(project_config, "atomic_json", swap_before_exact_receipt)
    with pytest.raises(
        project_config.ProjectConfigError,
        match="receipt does not match the exact target",
    ):
        db.connect(str(root))

    assert swapped["value"]
    assert parked_owned.read_bytes() == b""
    assert database.read_bytes() == foreign_before
    check = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert check.execute("SELECT secret FROM canary").fetchall() == [
            ("foreign inode",)
        ]
        assert check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone() is None
    finally:
        check.close()
    assert receipt_path.is_file()
    assert project_config.resolve(root).vault_uuid is None


def test_preexisting_foreign_sqlite_without_nodes_fails_closed_unchanged(
    explicit_scope_env: Path,
    tmp_path: Path,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="foreign-sqlite",
    )
    database = vault / "kb.db"
    foreign = sqlite3.connect(str(database))
    foreign.execute("CREATE TABLE canary(secret TEXT NOT NULL)")
    foreign.execute("INSERT INTO canary VALUES('foreign data')")
    foreign.commit()
    foreign.close()
    database_before = database.read_bytes()
    binding_path = project_config.scope_binding_path(str(initial.scope_id))
    binding_before = binding_path.read_bytes()

    with pytest.raises(
        project_config.ProjectConfigError,
        match="not a recognizable legacy Latch KB",
    ):
        db.connect(str(root))

    assert database.read_bytes() == database_before
    check = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        assert check.execute("SELECT secret FROM canary").fetchall() == [
            ("foreign data",)
        ]
        tables = {
            str(row[0])
            for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == {"canary"}
    finally:
        check.close()
    assert binding_path.read_bytes() == binding_before
    assert not db._initialization_receipt_path(initial, vault).exists()
    assert project_config.resolve(root).vault_uuid is None


def test_orphan_sqlite_sidecar_cannot_seed_a_new_vault(
    explicit_scope_env: Path,
    tmp_path: Path,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="orphan-sidecar",
    )
    sidecar = vault / "kb.db-wal"
    sidecar.write_bytes(b"foreign wal canary")

    with pytest.raises(
        project_config.ProjectConfigError,
        match="contains SQLite sidecars",
    ):
        db.connect(str(root))

    assert sidecar.read_bytes() == b"foreign wal canary"
    assert not (vault / "kb.db").exists()
    assert not db._initialization_receipt_path(initial, vault).exists()


def test_scoped_legacy_latch_adoption_stops_before_mutation_when_backup_fails(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="legacy-backup-required",
    )
    _seed_legacy_nodes_table(vault)
    database_before = (vault / "kb.db").read_bytes()

    import vault_backup

    def fail_backup(*_args, **_kwargs):
        raise RuntimeError("simulated pre-migration backup failure")

    monkeypatch.setattr(vault_backup, "create_pre_migration_snapshot", fail_backup)
    with pytest.raises(RuntimeError, match="pre-migration backup failure"):
        db.connect(str(root))

    assert (vault / "kb.db").read_bytes() == database_before
    assert _identity_rows(vault) == []
    assert project_config.resolve(root).vault_uuid is None
    assert db._load_initialization_receipt(initial, vault) is False


def test_private_identity_row_crash_recovers_only_the_exact_bound_vault(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="private-row-crash",
    )
    project_config.record_session_binding(root, "private-recovery-task")
    monkeypatch.setenv("CODEX_THREAD_ID", "private-recovery-task")
    pending, receipt = _interrupt_before_private_binding(
        monkeypatch,
        root,
        vault,
        initial,
    )

    rows_before = _identity_rows(vault)
    assert len(rows_before) == 1
    assert pending.target_revision == initial.target_revision

    recovered = db.connect(str(root))
    try:
        assert recovered._kb_vault_identity.vault_uuid == rows_before[0][0]
    finally:
        recovered.close()

    finalized = project_config.resolve(root)
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.revision == initial.revision
    assert finalized.vault_uuid == rows_before[0][0]
    assert _identity_rows(vault) == rows_before
    assert not receipt.exists()


def test_compatibility_first_identity_adoption_keeps_same_session_valid(
    compatibility_scope_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy-project"
    root.mkdir()
    vault = compatibility_scope_env["vault"]
    initial = project_config.resolve(root)
    assert initial.source == project_config.SOURCE_COMPATIBILITY
    assert initial.vault_uuid is None
    project_config.record_session_binding(root, "legacy-task")
    monkeypatch.setenv("CODEX_THREAD_ID", "legacy-task")

    first = db.connect(str(root))
    identity = first._kb_vault_identity
    first.close()

    finalized = project_config.resolve(root)
    assert finalized.revision == initial.revision
    assert finalized.vault_uuid == identity.vault_uuid
    assert project_config.current_session_revision(root, "legacy-task") == (
        initial.revision
    )
    second = db.connect(str(root))
    second.close()


def test_compatibility_identity_row_crash_recovers_exact_global_vault(
    compatibility_scope_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "legacy-row-crash"
    root.mkdir()
    vault = compatibility_scope_env["vault"]
    initial = project_config.resolve(root)
    original_finalize = project_config.finalize_compatibility_vault_identity

    def crash_before_binding(*args, **kwargs):
        raise RuntimeError(
            "simulated crash before compatibility binding finalization"
        )

    monkeypatch.setattr(
        project_config,
        "finalize_compatibility_vault_identity",
        crash_before_binding,
    )
    with pytest.raises(RuntimeError, match="compatibility binding finalization"):
        db.connect(str(root))
    monkeypatch.setattr(
        project_config,
        "finalize_compatibility_vault_identity",
        original_finalize,
    )

    rows_before = _identity_rows(vault)
    assert len(rows_before) == 1
    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert pending.reason_code == project_config.LOCK_VAULT_IDENTITY_PENDING
    receipt = db._initialization_receipt_path(initial, vault)
    assert receipt.is_file()

    recovered = db.connect(str(root))
    try:
        assert recovered._kb_vault_identity.vault_uuid == rows_before[0][0]
    finally:
        recovered.close()

    finalized = project_config.resolve(root)
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.revision == initial.revision
    assert finalized.vault_uuid == rows_before[0][0]
    assert _identity_rows(vault) == rows_before
    assert not receipt.exists()


def test_compatibility_agent_without_session_receipt_cannot_open_kb(
    compatibility_scope_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "unattributed-legacy-project"
    root.mkdir()
    monkeypatch.setenv("CODEX_THREAD_ID", "missing-receipt")

    with pytest.raises(db.ProjectTargetChangedError, match="older project KB"):
        db.connect(str(root))

    assert not (compatibility_scope_env["vault"] / "kb.db").exists()


@pytest.mark.parametrize(
    ("legacy_database", "expected_classification"),
    [
        (False, db.vault_identity.CLASS_TEST),
        (True, db.vault_identity.CLASS_PRODUCTION),
    ],
)
def test_zero_row_recovery_preserves_original_vault_classification(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy_database: bool,
    expected_classification: str,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name=f"zero-row-{'legacy' if legacy_database else 'new'}",
    )
    if legacy_database:
        _seed_legacy_nodes_table(vault)

    pending, receipt = _interrupt_before_identity_row(
        monkeypatch,
        root,
        vault,
        initial,
    )
    assert pending.target_fingerprint == initial.target_fingerprint
    assert db._load_initialization_receipt(initial, vault) is not legacy_database

    recovered = db.connect(str(root))
    try:
        identity = recovered._kb_vault_identity
        assert identity.classification == expected_classification
    finally:
        recovered.close()

    finalized = project_config.resolve(root)
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.revision == initial.revision
    assert finalized.vault_uuid == identity.vault_uuid
    assert _identity_rows(vault)[0][1] == expected_classification
    assert not receipt.exists()


def test_compatibility_zero_row_receipt_recovers_exact_global_vault(
    compatibility_scope_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "compatibility-zero-row"
    root.mkdir()
    vault = compatibility_scope_env["vault"]
    initial = project_config.resolve(root)

    _pending, receipt = _interrupt_before_identity_row(
        monkeypatch,
        root,
        vault,
        initial,
    )
    recovered = db.connect(str(root))
    try:
        identity = recovered._kb_vault_identity
        assert identity.classification == db.vault_identity.CLASS_TEST
    finally:
        recovered.close()

    finalized = project_config.resolve(root)
    assert finalized.state == project_config.MODE_LATCHED
    assert finalized.revision == initial.revision
    assert finalized.vault_uuid == identity.vault_uuid
    assert not receipt.exists()


@pytest.mark.parametrize("receipt_kind", ["missing", "stale"])
def test_agent_without_current_receipt_cannot_recover_or_mutate_zero_row_vault(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_kind: str,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name=f"agent-{receipt_kind}-receipt",
    )
    _pending, recovery_receipt = _interrupt_before_identity_row(
        monkeypatch,
        root,
        vault,
        initial,
    )
    binding_path = project_config.scope_binding_path(str(initial.scope_id))
    binding_before = binding_path.read_bytes()
    session_id = f"{receipt_kind}-recovery-task"
    if receipt_kind == "stale":
        stale_root, _stale_vault, _stale_target = _new_private_scope(
            explicit_scope_env,
            tmp_path,
            name="different-client",
        )
        project_config.record_session_binding(stale_root, session_id)
    monkeypatch.setenv("CODEX_THREAD_ID", session_id)

    with pytest.raises(db.ProjectTargetChangedError, match="older project KB"):
        db.connect(str(root))

    assert binding_path.read_bytes() == binding_before
    assert _identity_rows(vault) == []
    assert recovery_receipt.is_file()
    still_pending = project_config.resolve(root)
    assert still_pending.state == project_config.MODE_LOCKED
    assert (
        still_pending.reason_code
        == project_config.LOCK_VAULT_IDENTITY_INITIALIZING
    )


def test_zero_row_without_recovery_receipt_remains_locked_and_unmodified(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="missing-recovery-receipt",
    )
    _pending, receipt = _interrupt_before_identity_row(
        monkeypatch,
        root,
        vault,
        initial,
    )
    binding_path = project_config.scope_binding_path(str(initial.scope_id))
    binding_before = binding_path.read_bytes()
    receipt.unlink()

    with pytest.raises(
        db.ProjectTargetChangedError,
        match="no exact recovery receipt",
    ):
        db.connect(str(root))

    assert binding_path.read_bytes() == binding_before
    assert _identity_rows(vault) == []
    still_pending = project_config.resolve(root)
    assert still_pending.state == project_config.MODE_LOCKED
    assert (
        still_pending.reason_code
        == project_config.LOCK_VAULT_IDENTITY_INITIALIZING
    )


def test_recovery_refuses_replaced_private_directory_fingerprint(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="replaced-recovery-vault",
    )
    pending, receipt = _interrupt_before_identity_row(
        monkeypatch,
        root,
        vault,
        initial,
    )
    parked = vault.with_name(vault.name + "-parked")
    vault.rename(parked)
    vault.mkdir()

    with pytest.raises(
        lockfile.ProjectTargetChangedError,
        match="directory identity changed",
    ):
        db._recover_interrupted_identity(str(root), pending)
    with pytest.raises(db.ProjectTargetChangedError, match="project is locked"):
        db.connect(str(root))

    assert not (vault / "kb.db").exists()
    assert _identity_rows(parked) == []
    assert receipt.is_file()


def test_recovery_refuses_compatibility_pin_change(
    compatibility_scope_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "compatibility-pin-change"
    root.mkdir()
    vault = compatibility_scope_env["vault"]
    initial = project_config.resolve(root)
    original_finalize = project_config.finalize_compatibility_vault_identity

    def crash_before_binding(*args, **kwargs):
        raise RuntimeError(
            "simulated crash before compatibility binding finalization"
        )

    monkeypatch.setattr(
        project_config,
        "finalize_compatibility_vault_identity",
        crash_before_binding,
    )
    with pytest.raises(RuntimeError, match="compatibility binding finalization"):
        db.connect(str(root))
    monkeypatch.setattr(
        project_config,
        "finalize_compatibility_vault_identity",
        original_finalize,
    )
    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert pending.reason_code == project_config.LOCK_VAULT_IDENTITY_PENDING
    receipt = db._initialization_receipt_path(initial, vault)
    assert receipt.is_file()
    rows_before = _identity_rows(vault)
    binding_path = project_config.compatibility_binding_path()
    binding_before = binding_path.read_bytes()
    test_root = paths.validated_test_root()
    assert test_root is not None
    replacement = (
        test_root / "vaults" / f"replacement-global-{tmp_path.name}"
    )
    replacement.mkdir(parents=True)
    monkeypatch.setenv("LATCH_KB_DIR", str(replacement))

    with pytest.raises(
        lockfile.ProjectTargetChangedError,
        match="scope changed",
    ):
        db._recover_interrupted_identity(str(root), pending)
    with pytest.raises(db.ProjectTargetChangedError, match="project is locked"):
        db.connect(str(root))

    assert binding_path.read_bytes() == binding_before
    assert _identity_rows(vault) == rows_before
    assert not (replacement / "kb.db").exists()
    assert receipt.is_file()


def test_writable_open_rejects_foreign_db_inode_restored_before_revalidation(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_root, selected_vault, selected_target = _mature_private_scope(
        explicit_scope_env,
        tmp_path,
        name="selected-writable-inode",
    )
    _foreign_root, foreign_vault, foreign_target = _mature_private_scope(
        explicit_scope_env,
        tmp_path,
        name="foreign-writable-inode",
    )
    assert selected_target.vault_uuid != foreign_target.vault_uuid
    selected_before = _vault_file_snapshot(selected_vault)
    foreign_before = _vault_file_snapshot(foreign_vault)
    swapped = _swap_database_only_while_opening(
        monkeypatch,
        selected_vault / "kb.db",
        foreign_vault / "kb.db",
    )
    monkeypatch.setattr(db.lockfile, "writer_lock", _forbid_writer_lock)

    with pytest.raises(
        db.ProjectTargetChangedError,
        match="opened SQLite vault does not match",
    ):
        db.connect(str(selected_root))

    assert swapped["value"]
    assert _vault_file_snapshot(selected_vault) == selected_before
    assert _vault_file_snapshot(foreign_vault) == foreign_before
    assert project_config.resolve(selected_root).vault_uuid == (
        selected_target.vault_uuid
    )


@pytest.mark.parametrize(
    "connector_name",
    ["open_existing_readonly", "connect_readonly"],
)
def test_readonly_open_rejects_foreign_db_inode_restored_before_revalidation(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connector_name: str,
) -> None:
    selected_root, selected_vault, selected_target = _mature_private_scope(
        explicit_scope_env,
        tmp_path,
        name=f"selected-{connector_name}-inode",
    )
    _foreign_root, foreign_vault, foreign_target = _mature_private_scope(
        explicit_scope_env,
        tmp_path,
        name=f"foreign-{connector_name}-inode",
    )
    assert selected_target.vault_uuid != foreign_target.vault_uuid
    selected_before = _vault_file_snapshot(selected_vault)
    foreign_before = _vault_file_snapshot(foreign_vault)
    swapped = _swap_database_only_while_opening(
        monkeypatch,
        selected_vault / "kb.db",
        foreign_vault / "kb.db",
    )

    connector = getattr(db, connector_name)
    with pytest.raises(
        db.ProjectTargetChangedError,
        match="opened SQLite vault does not match",
    ):
        connector(str(selected_root))

    assert swapped["value"]
    assert _vault_file_snapshot(selected_vault) == selected_before
    assert _vault_file_snapshot(foreign_vault) == foreign_before
    assert project_config.resolve(selected_root).vault_uuid == (
        selected_target.vault_uuid
    )


@pytest.mark.parametrize(
    "connector_name",
    ["open_existing_readonly", "connect_readonly"],
)
@pytest.mark.parametrize("interruption", ["initializing", "pending"])
def test_readonly_locked_identity_state_never_recovers_or_mutates(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connector_name: str,
    interruption: str,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name=f"readonly-{connector_name}-{interruption}",
    )
    if interruption == "initializing":
        pending, receipt = _interrupt_before_identity_row(
            monkeypatch,
            root,
            vault,
            initial,
        )
    else:
        pending, receipt = _interrupt_before_private_binding(
            monkeypatch,
            root,
            vault,
            initial,
        )
    binding_path = project_config.scope_binding_path(str(initial.scope_id))
    binding_before = binding_path.read_bytes()
    receipt_before = receipt.read_bytes()
    rows_before = _identity_rows(vault)
    vault_before = _vault_file_snapshot(vault)

    connector = getattr(db, connector_name)
    with pytest.raises(db.ProjectTargetChangedError, match="project is locked"):
        connector(str(root))

    after = project_config.resolve(root)
    assert after.state == project_config.MODE_LOCKED
    assert after.reason_code == pending.reason_code
    assert after.revision == pending.revision
    assert binding_path.read_bytes() == binding_before
    assert receipt.read_bytes() == receipt_before
    assert _identity_rows(vault) == rows_before
    assert _vault_file_snapshot(vault) == vault_before


def test_recovery_receipt_rejects_same_directory_db_file_replacement(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, vault, initial = _new_private_scope(
        explicit_scope_env,
        tmp_path,
        name="receipt-db-replacement",
    )
    _pending, receipt = _interrupt_before_identity_row(
        monkeypatch,
        root,
        vault,
        initial,
    )
    binding_path = project_config.scope_binding_path(str(initial.scope_id))
    binding_before = binding_path.read_bytes()
    receipt_before = receipt.read_bytes()
    database = vault / "kb.db"
    original_fingerprint = db._database_file_fingerprint(database)
    parked = vault / "kb.db.original"
    database.rename(parked)
    database.write_bytes(parked.read_bytes())
    assert db._database_file_fingerprint(database) != original_fingerprint
    replacement_before = database.read_bytes()

    with pytest.raises(
        project_config.ProjectConfigError,
        match="receipt does not match the exact target",
    ):
        db.connect(str(root))

    assert binding_path.read_bytes() == binding_before
    assert receipt.read_bytes() == receipt_before
    assert database.read_bytes() == replacement_before
    assert _identity_rows(vault) == []
    assert project_config.resolve(root).reason_code == (
        project_config.LOCK_VAULT_IDENTITY_INITIALIZING
    )


def test_concurrent_first_open_serializes_vault_initialization(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "concurrent-client"
    root.mkdir()
    vault = explicit_scope_env / "vaults" / f"concurrent-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(root, kb_dir=vault)

    original_connect = db.sqlite3.connect
    counter_lock = threading.Lock()
    start = threading.Barrier(2)
    active_opens = 0
    max_active_opens = 0
    identities: list[str] = []
    errors: list[BaseException] = []

    def slow_connect(*args, **kwargs):
        nonlocal active_opens, max_active_opens
        if kwargs.get("factory") is not db._Connection:
            return original_connect(*args, **kwargs)
        with counter_lock:
            active_opens += 1
            max_active_opens = max(max_active_opens, active_opens)
        try:
            time.sleep(0.05)
            return original_connect(*args, **kwargs)
        finally:
            with counter_lock:
                active_opens -= 1

    monkeypatch.setattr(db.sqlite3, "connect", slow_connect)

    def open_vault() -> None:
        try:
            start.wait(timeout=3)
            connection = db.connect(str(root))
            try:
                identities.append(connection._kb_vault_identity.vault_uuid)
            finally:
                connection.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    workers = [threading.Thread(target=open_vault) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert not errors
    assert len(set(identities)) == 1
    assert max_active_opens == 1


@pytest.mark.parametrize(
    "migration_kind",
    ["schema-version", "latch-version", "schema-shape"],
)
def test_identified_migration_waits_for_writer_sentinel(
    explicit_scope_env: Path,
    tmp_path: Path,
    migration_kind: str,
) -> None:
    root, vault, _target = _mature_private_scope(
        explicit_scope_env,
        tmp_path,
        name=f"serialized-{migration_kind}-migration",
    )
    raw = sqlite3.connect(vault / "kb.db")
    try:
        if migration_kind == "schema-version":
            raw.execute(
                "UPDATE latch_meta SET value='2' WHERE key='kb_schema_version'"
            )
        elif migration_kind == "latch-version":
            raw.execute(
                "UPDATE latch_meta SET value='prior-release' WHERE key=?",
                (db._CONNECTION_SETUP_KEY,),
            )
        else:
            raw.execute("DROP TABLE seed_source_import")
        raw.commit()
    finally:
        raw.close()

    ready = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def hold_writer() -> None:
        try:
            with lockfile.writer_lock(str(root)):
                ready.set()
                assert release.wait(timeout=5)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
            ready.set()

    def migrate() -> None:
        try:
            connection = db.connect(str(root))
            connection.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            finished.set()

    holder = threading.Thread(target=hold_writer)
    holder.start()
    assert ready.wait(timeout=3)
    worker = threading.Thread(target=migrate)
    worker.start()
    assert not finished.wait(timeout=0.2)
    release.set()
    holder.join(timeout=5)
    worker.join(timeout=5)

    assert not holder.is_alive()
    assert not worker.is_alive()
    assert not errors
    check = sqlite3.connect(vault / "kb.db")
    try:
        assert db.schema_version.read(check) == db.schema_version.KB_SCHEMA_VERSION
        setup = check.execute(
            "SELECT value FROM latch_meta WHERE key=?",
            (db._CONNECTION_SETUP_KEY,),
        ).fetchone()
        assert setup is not None
        assert setup[0] == db.schema_version.LATCH_VERSION
        if migration_kind == "schema-shape":
            repaired = check.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='seed_source_import'"
            ).fetchone()
            assert repaired is not None
    finally:
        check.close()


def test_second_first_open_waits_for_identity_row_commit(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "identity-window-client"
    root.mkdir()
    vault = explicit_scope_env / "vaults" / f"identity-window-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(root, kb_dir=vault)

    original_ensure = db.vault_identity.ensure_identity
    identity_window = threading.Event()
    finish_first = threading.Event()
    call_lock = threading.Lock()
    first_call = True
    identities: list[str] = []
    errors: list[BaseException] = []

    def paused_ensure(*args, **kwargs):
        nonlocal first_call
        with call_lock:
            pause = first_call
            first_call = False
        if pause:
            identity_window.set()
            assert finish_first.wait(timeout=5)
        return original_ensure(*args, **kwargs)

    monkeypatch.setattr(db.vault_identity, "ensure_identity", paused_ensure)

    def open_vault() -> None:
        try:
            connection = db.connect(str(root))
            try:
                identities.append(connection._kb_vault_identity.vault_uuid)
            finally:
                connection.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=open_vault)
    first.start()
    assert identity_window.wait(timeout=5)
    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert (
        pending.reason_code
        == project_config.LOCK_VAULT_IDENTITY_INITIALIZING
    )

    second = threading.Thread(target=open_vault)
    second.start()
    time.sleep(0.1)
    assert second.is_alive()
    finish_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert len(identities) == 2
    assert len(set(identities)) == 1


def test_second_first_open_waits_for_scope_identity_finalization(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "finalization-window-client"
    root.mkdir()
    vault = explicit_scope_env / "vaults" / f"finalization-window-{tmp_path.name}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(root, kb_dir=vault)

    original_finalize = project_config.finalize_scope_vault_identity
    identity_committed = threading.Event()
    finish_finalization = threading.Event()
    identities: list[str] = []
    errors: list[BaseException] = []

    def paused_finalize(*args, **kwargs):
        identity_committed.set()
        assert finish_finalization.wait(timeout=5)
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(
        project_config,
        "finalize_scope_vault_identity",
        paused_finalize,
    )

    def open_vault() -> None:
        try:
            connection = db.connect(str(root))
            try:
                identities.append(connection._kb_vault_identity.vault_uuid)
            finally:
                connection.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=open_vault)
    first.start()
    assert identity_committed.wait(timeout=5)
    pending = project_config.resolve(root)
    assert pending.state == project_config.MODE_LOCKED
    assert pending.reason_code == project_config.LOCK_VAULT_IDENTITY_PENDING

    second = threading.Thread(target=open_vault)
    second.start()
    time.sleep(0.05)
    assert second.is_alive(), "second open bypassed the active initializer"
    finish_finalization.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert not errors
    assert len(identities) == 2
    assert len(set(identities)) == 1


def test_connection_rejects_vault_directory_swap_after_validation(
    explicit_scope_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots: list[Path] = []
    vaults: list[Path] = []
    for name in ("selected", "foreign"):
        root = tmp_path / name
        root.mkdir()
        vault = explicit_scope_env / "vaults" / f"swap-{name}-{tmp_path.name}"
        vault.mkdir(parents=True)
        project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
        project_config.authorize_scope(root, kb_dir=vault)
        connection = db.connect(str(root))
        connection.close()
        roots.append(root)
        vaults.append(vault)

    selected_root, _foreign_root = roots
    selected_vault, foreign_vault = vaults
    parked_vault = selected_vault.with_name(selected_vault.name + "-parked")
    original_connect = db.sqlite3.connect
    swapped = False

    def swap_before_open(*args, **kwargs):
        nonlocal swapped
        if kwargs.get("factory") is db._Connection and not swapped:
            swapped = True
            selected_vault.rename(parked_vault)
            try:
                selected_vault.symlink_to(foreign_vault, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - platform capability
                parked_vault.rename(selected_vault)
                pytest.skip(f"directory symlink unavailable: {exc}")
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", swap_before_open)
    try:
        with pytest.raises(
            project_config.ProjectConfigError,
            match="bound KB.*(unsafe|identity changed)",
        ):
            db.connect(str(selected_root))
    finally:
        if selected_vault.is_symlink():
            selected_vault.unlink()
        if parked_vault.exists():
            parked_vault.rename(selected_vault)
        (selected_vault / "compactor.lock").unlink(missing_ok=True)

    assert swapped
