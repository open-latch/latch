"""Immutable KB identity and the only supported vault deletion boundary.

Every database is classified exactly once as ``production`` or ``test``.  The
classification is stored in SQLite behind UPDATE/DELETE triggers and mirrored
to an external capability registry.  A disagreement or missing registry entry
fails closed.

Production vault deletion is intentionally not implemented.  Test deletion is
available only when all four gates agree: authenticated test runtime, test DB
identity, exact vault UUID, and realpath containment under the disposable test
root's ``vaults`` directory.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import paths

CLASS_PRODUCTION = "production"
CLASS_TEST = "test"
CLASSIFICATIONS = (CLASS_PRODUCTION, CLASS_TEST)
REGISTRY_ENV = "LATCH_VAULT_REGISTRY_ROOT"
PRODUCTION_ROOT_ENV = "LATCH_PRODUCTION_DATA_ROOT"


class VaultSafetyError(RuntimeError):
    pass


class ProductionVaultDeletionRefused(VaultSafetyError):
    pass


@dataclass(frozen=True)
class VaultIdentity:
    vault_uuid: str
    classification: str
    created_at: str
    registry_fingerprint: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def platform_production_root() -> Path:
    configured = os.environ.get(PRODUCTION_ROOT_ENV)
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "Latch"
        return Path.home() / "AppData" / "Local" / "Latch"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Latch"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "latch"


def default_production_vault() -> Path:
    """New production installs live outside the source checkout."""
    return platform_production_root() / "vaults" / "default"


def platform_durability_root() -> Path:
    """Independent default root for protected production backups."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "LatchBackups"
        return Path.home() / "AppData" / "Local" / "LatchBackups"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LatchBackups"
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base else Path.home() / ".local" / "state") / "latch" / "backups"


def _classification_for_path(vault_dir: Path) -> str:
    test_root = paths.validated_test_root()
    if test_root is None:
        return CLASS_PRODUCTION
    allowed = (test_root / "vaults").resolve()
    resolved = vault_dir.resolve()
    if not _is_relative_to(resolved, allowed) or resolved == allowed:
        raise VaultSafetyError(
            f"test process attempted to resolve a vault outside its disposable root: {resolved}"
        )
    return CLASS_TEST


def _registry_root(classification: str) -> Path:
    test_root = paths.validated_test_root()
    if classification == CLASS_TEST:
        if test_root is None:
            raise VaultSafetyError("test vault identity requires an authenticated test root")
        return test_root / "registry"
    # Tests that exercise legacy-production adoption must never create records
    # in the user's real production registry.
    if test_root is not None:
        return test_root / "production-registry-shadow"
    configured = os.environ.get(REGISTRY_ENV)
    return Path(configured).expanduser() if configured else platform_production_root() / "registry"


def _registry_path(identity: VaultIdentity) -> Path:
    return _registry_root(identity.classification) / f"{identity.vault_uuid}.json"


def _registry_payload(identity: VaultIdentity) -> dict[str, object]:
    return {"format": 1, **asdict(identity)}


def _write_registry_exclusive(
    identity: VaultIdentity,
    vault_dir: Path,
    *,
    allow_existing: bool = True,
) -> Path:
    root = _registry_root(identity.classification).resolve()
    resolved_vault = vault_dir.resolve()
    if root == resolved_vault or _is_relative_to(root, resolved_vault):
        raise VaultSafetyError("vault registry must be outside the vault it protects")
    if identity.classification == CLASS_PRODUCTION:
        source_root = paths.KB_ROOT.resolve()
        if root == source_root or _is_relative_to(root, source_root):
            raise VaultSafetyError("production vault registry must be outside the source checkout")
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{identity.vault_uuid}.json"
    body = json.dumps(_registry_payload(identity), indent=2, sort_keys=True) + "\n"
    try:
        with destination.open("x", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            destination.chmod(0o444)
        except OSError:
            pass
    except FileExistsError:
        if not allow_existing:
            raise VaultSafetyError(
                "registry record already exists; refusing possible duplicate-vault recovery"
            )
        _validate_registry(identity)
    return destination


def _validate_registry(identity: VaultIdentity) -> Path:
    path = _registry_path(identity)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VaultSafetyError(
            f"vault registry missing or unreadable for {identity.vault_uuid}; refusing access"
        ) from exc
    expected = _registry_payload(identity)
    for key, value in expected.items():
        observed = payload.get(key)
        if isinstance(value, str):
            if not hmac.compare_digest(str(observed or ""), value):
                raise VaultSafetyError(
                    f"vault registry mismatch for {identity.vault_uuid}: {key}"
                )
        elif observed != value:
            raise VaultSafetyError(
                f"vault registry mismatch for {identity.vault_uuid}: {key}"
            )
    return path


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vault_identity (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            vault_uuid TEXT NOT NULL UNIQUE,
            classification TEXT NOT NULL CHECK (classification IN ('production', 'test')),
            created_at TEXT NOT NULL,
            registry_fingerprint TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS vault_identity_no_update
        BEFORE UPDATE ON vault_identity BEGIN
            SELECT RAISE(ABORT, 'vault identity is immutable');
        END;
        CREATE TRIGGER IF NOT EXISTS vault_identity_no_delete
        BEFORE DELETE ON vault_identity BEGIN
            SELECT RAISE(ABORT, 'vault identity is immutable');
        END;
        """
    )


def _row_identity(row: sqlite3.Row | tuple[object, ...]) -> VaultIdentity:
    return VaultIdentity(
        vault_uuid=str(row[0]),
        classification=str(row[1]),
        created_at=str(row[2]),
        registry_fingerprint=str(row[3]),
    )


def ensure_identity(
    conn: sqlite3.Connection,
    vault_dir: Path,
    *,
    new_vault: bool,
) -> VaultIdentity:
    """Create identity once, otherwise validate DB and external registry.

    Only a database proven new by the caller may inherit the authenticated
    test classification. Every unidentified pre-existing database is adopted
    as production, including a legacy DB opened under a test sandbox.
    """
    _ensure_schema(conn)
    rows = conn.execute(
        "SELECT vault_uuid, classification, created_at, registry_fingerprint "
        "FROM vault_identity ORDER BY slot"
    ).fetchall()
    if len(rows) > 1:
        raise VaultSafetyError("vault contains multiple identity rows")
    if rows:
        identity = _row_identity(rows[0])
        if identity.classification not in CLASSIFICATIONS:
            raise VaultSafetyError("vault has an invalid classification")
        # Production identity may exist at a legacy location while it is being
        # migrated. Test identity is valid only in an authenticated disposable
        # root, so it can never be replayed in normal operation.
        if (
            identity.classification == CLASS_TEST
            and _classification_for_path(vault_dir) != CLASS_TEST
        ):
            raise VaultSafetyError(
                f"vault classification/path mismatch: {identity.classification} at {vault_dir}"
            )
        _validate_registry(identity)
        return identity

    classification = (
        _classification_for_path(vault_dir) if new_vault else CLASS_PRODUCTION
    )
    if classification == CLASS_PRODUCTION and new_vault:
        source_root = paths.KB_ROOT.resolve()
        resolved_vault = vault_dir.resolve()
        if resolved_vault == source_root or _is_relative_to(resolved_vault, source_root):
            raise VaultSafetyError(
                f"refusing to create a production vault inside the source checkout: "
                f"{resolved_vault}"
            )
    identity = VaultIdentity(
        vault_uuid=str(uuid.uuid4()),
        classification=classification,
        created_at=_utc_now(),
        registry_fingerprint=secrets.token_hex(32),
    )
    _write_registry_exclusive(identity, vault_dir)
    try:
        conn.execute(
            "INSERT INTO vault_identity "
            "(slot, vault_uuid, classification, created_at, registry_fingerprint) "
            "VALUES (1, ?, ?, ?, ?)",
            (
                identity.vault_uuid,
                identity.classification,
                identity.created_at,
                identity.registry_fingerprint,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return identity


def read_identity(db_file: Path) -> VaultIdentity | None:
    """Read identity without creating or adopting anything."""
    if not db_file.is_file():
        return None
    conn = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vault_identity'"
        ).fetchone()
        if table is None:
            return None
        row = conn.execute(
            "SELECT vault_uuid, classification, created_at, registry_fingerprint "
            "FROM vault_identity WHERE slot=1"
        ).fetchone()
        return _row_identity(row) if row is not None else None
    finally:
        conn.close()


def register_restored_production_vault(vault_dir: Path) -> Path:
    """Rebuild one missing production registry record from a restored DB.

    This is deliberately narrower than normal identity creation: it accepts
    only an existing, integrity-clean production database with canonical
    immutable identity fields, refuses symlinked/source-checkout targets, and
    never overwrites or repairs an existing registry record.
    """
    lexical = Path(vault_dir).expanduser().absolute()
    resolved = Path(vault_dir).expanduser().resolve(strict=True)
    if lexical != resolved:
        raise VaultSafetyError("refusing registry recovery through a symlinked vault path")
    if not resolved.is_dir():
        raise VaultSafetyError("restored production vault path is not a directory")
    if paths.validated_test_root() is None:
        source_root = paths.KB_ROOT.resolve()
        if resolved == source_root or _is_relative_to(resolved, source_root):
            raise VaultSafetyError(
                "refusing restored production vault inside the source checkout"
            )

    db_file = resolved / "kb.db"
    identity = read_identity(db_file)
    if identity is None:
        raise VaultSafetyError("restored database has no immutable vault identity")
    if identity.classification != CLASS_PRODUCTION:
        raise VaultSafetyError("registry recovery is supported only for production vaults")
    try:
        parsed_uuid = str(uuid.UUID(identity.vault_uuid))
    except ValueError as exc:
        raise VaultSafetyError("restored vault UUID is invalid") from exc
    if not hmac.compare_digest(parsed_uuid, identity.vault_uuid):
        raise VaultSafetyError("restored vault UUID is not canonical")
    try:
        created_at = datetime.fromisoformat(identity.created_at)
    except ValueError as exc:
        raise VaultSafetyError("restored vault creation timestamp is invalid") from exc
    if created_at.tzinfo is None:
        raise VaultSafetyError("restored vault creation timestamp must include a timezone")
    if re.fullmatch(r"[0-9a-f]{64}", identity.registry_fingerprint) is None:
        raise VaultSafetyError("restored vault registry fingerprint is invalid")

    conn = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    if integrity != "ok" or foreign_keys:
        raise VaultSafetyError(
            f"refusing corrupt restored vault: integrity={integrity}, "
            f"foreign_keys={len(foreign_keys)}"
        )

    registry = _registry_path(identity)
    if registry.exists():
        raise VaultSafetyError(
            "registry record already exists; refusing possible duplicate-vault recovery"
        )
    return _write_registry_exclusive(identity, resolved, allow_existing=False)


def safe_delete_test_vault(
    vault_dir: Path,
    *,
    expected_uuid: str,
    capability: str,
) -> None:
    """Delete a test vault only after all independent gates pass.

    There is deliberately no production override and no force option.
    """
    test_root = paths.validated_test_root()
    if test_root is None:
        raise VaultSafetyError("test vault deletion requires an authenticated test runtime")
    env_capability = os.environ.get(paths.TEST_CAPABILITY_ENV) or ""
    if not capability or not hmac.compare_digest(capability, env_capability):
        raise VaultSafetyError("test vault deletion capability mismatch")
    lexical = Path(vault_dir).absolute()
    resolved = Path(vault_dir).resolve(strict=True)
    if lexical != resolved:
        raise VaultSafetyError("refusing vault deletion through a symlinked path")
    allowed = (test_root / "vaults").resolve()
    if resolved == allowed or not _is_relative_to(resolved, allowed):
        raise VaultSafetyError("test vault deletion target is outside the disposable vault root")
    identity = read_identity(resolved / "kb.db")
    if identity is None:
        raise VaultSafetyError("refusing to delete an unidentified vault")
    if identity.classification != CLASS_TEST:
        raise ProductionVaultDeletionRefused(
            f"production vault deletion is permanently refused: {identity.vault_uuid}"
        )
    if not hmac.compare_digest(identity.vault_uuid, expected_uuid):
        raise VaultSafetyError("test vault UUID does not match deletion request")
    _validate_registry(identity)
    shutil.rmtree(resolved)


def identity_digest(identity: VaultIdentity) -> str:
    body = json.dumps(_registry_payload(identity), sort_keys=True).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
