"""Protected online SQLite snapshots for Latch vaults.

Production snapshots live in an independent platform data root, never beside
the live DB or inside the source checkout.  Snapshot publication is atomic and
each manifest declares a protection deadline that the pruning code has no
override for.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import paths
import vault_identity

DURABILITY_ROOT_ENV = "LATCH_DURABILITY_ROOT"
FREQUENT_RETENTION_DAYS = 5
DAILY_RETENTION_DAYS = 30
_REASON_RE = re.compile(r"[^a-z0-9_-]+")


class BackupSafetyError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _durability_root(identity: vault_identity.VaultIdentity, vault_dir: Path) -> Path:
    test_root = paths.validated_test_root()
    if test_root is not None:
        # Even a production-classified legacy copy opened during a test must
        # never write to the user's real durability root.
        root = test_root / "backups"
    elif identity.classification == vault_identity.CLASS_TEST:
        raise BackupSafetyError("test backup requires an authenticated test root")
    else:
        configured = os.environ.get(DURABILITY_ROOT_ENV)
        root = (
            Path(configured).expanduser()
            if configured
            else vault_identity.platform_durability_root()
        )
    resolved_root = root.resolve()
    resolved_vault = vault_dir.resolve()
    if resolved_root == resolved_vault or _is_relative_to(resolved_root, resolved_vault):
        raise BackupSafetyError("backup root must be outside the live vault")
    if test_root is not None:
        if not _is_relative_to(resolved_root, test_root.resolve()):
            raise BackupSafetyError("test backups must stay inside the disposable test root")
    elif identity.classification == vault_identity.CLASS_PRODUCTION:
        source_root = paths.KB_ROOT.resolve()
        if resolved_root == source_root or _is_relative_to(resolved_root, source_root):
            raise BackupSafetyError("production backups must be outside the source checkout")
    return resolved_root


def _snapshot_dir(identity: vault_identity.VaultIdentity, vault_dir: Path) -> Path:
    return _durability_root(identity, vault_dir) / identity.vault_uuid / "snapshots"


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupSafetyError(f"invalid backup manifest: {path}") from exc
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise BackupSafetyError(f"unsupported backup manifest: {path}")
    return payload


def _manifests(snapshot_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    out: list[tuple[Path, dict[str, Any]]] = []
    if not snapshot_dir.is_dir():
        return out
    for path in sorted(snapshot_dir.glob("*.json")):
        try:
            out.append((path, _read_manifest(path)))
        except BackupSafetyError:
            # Unknown/corrupt files are never pruning candidates.
            continue
    return out


def _daily_exists(snapshot_dir: Path, day: str) -> bool:
    return any(
        payload.get("tier") == "daily"
        and str(payload.get("created_at") or "").startswith(day)
        for _path, payload in _manifests(snapshot_dir)
    )


def _sqlite_receipt(db_file: Path) -> dict[str, Any]:
    conn = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        nodes = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM nodes").fetchone()
        edges = int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
        sessions = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        identity = vault_identity.read_identity(db_file)
    finally:
        conn.close()
    if integrity != "ok" or fk:
        raise BackupSafetyError(
            f"snapshot verification failed: integrity={integrity}, foreign_keys={len(fk)}"
        )
    if identity is None:
        raise BackupSafetyError("snapshot has no immutable vault identity")
    return {
        "integrity_check": integrity,
        "foreign_key_violations": 0,
        "nodes": int(nodes[0]),
        "max_node_id": int(nodes[1]),
        "edges": edges,
        "sessions": sessions,
        "vault_uuid": identity.vault_uuid,
        "classification": identity.classification,
        "identity_digest": vault_identity.identity_digest(identity),
    }


def _legacy_sqlite_receipt(db_file: Path) -> dict[str, Any]:
    """Verify an unidentified pre-migration database without adopting it."""
    conn = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        nodes = (
            conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM nodes").fetchone()
            if "nodes" in tables
            else (0, 0)
        )
        edges = (
            int(conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
            if "edges" in tables
            else 0
        )
        sessions = (
            int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            if "sessions" in tables
            else 0
        )
    finally:
        conn.close()
    if integrity != "ok" or fk:
        raise BackupSafetyError(
            f"pre-migration snapshot verification failed: "
            f"integrity={integrity}, foreign_keys={len(fk)}"
        )
    return {
        "integrity_check": integrity,
        "foreign_key_violations": 0,
        "nodes": int(nodes[0]),
        "max_node_id": int(nodes[1]),
        "edges": edges,
        "sessions": sessions,
        "classification": vault_identity.CLASS_PRODUCTION,
        "identity_state": "unidentified-existing-treated-as-production",
    }


def create_pre_migration_snapshot(
    conn: sqlite3.Connection,
    db_file: Path,
    *,
    from_version: int,
    to_version: int,
    reason: str = "schema-migration",
    now: datetime | None = None,
) -> Path:
    """Protect a legacy DB externally before any identity/schema mutation.

    An unidentified existing database is production by definition. In pytest,
    its backup is redirected to the authenticated test root so this path can be
    exercised without touching user durability storage.
    """
    created = (now or _now()).astimezone(timezone.utc)
    live = db_file.expanduser().resolve()
    test_root = paths.validated_test_root()
    existing_identity = vault_identity.read_identity(live)
    if existing_identity is not None:
        snapshot_dir = _snapshot_dir(existing_identity, live.parent)
        root = snapshot_dir.parents[1]
    elif test_root is not None:
        root = test_root / "backups" / "legacy-production"
    else:
        configured = os.environ.get(DURABILITY_ROOT_ENV)
        root = (
            Path(configured).expanduser()
            if configured
            else vault_identity.platform_durability_root()
        ) / "legacy-production"
    root = root.resolve()
    if root == live.parent or _is_relative_to(root, live.parent):
        raise BackupSafetyError("pre-migration backup root must be outside the live vault")
    if test_root is None:
        source_root = paths.KB_ROOT.resolve()
        if root == source_root or _is_relative_to(root, source_root):
            raise BackupSafetyError("production backups must be outside the source checkout")
    if existing_identity is None:
        path_key = hashlib.sha256(str(live).encode("utf-8")).hexdigest()[:24]
        snapshot_dir = root / path_key / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    cleaned_reason = _REASON_RE.sub("-", reason.lower()).strip("-") or "migration"
    stem = (
        f"{created.strftime('%Y%m%dT%H%M%S%fZ')}--daily--"
        f"{cleaned_reason}--schema-{from_version}-to-{to_version}"
    )
    final_db = snapshot_dir / f"{stem}.db"
    final_manifest = snapshot_dir / f"{stem}.json"
    temp_db = snapshot_dir / f".{stem}.db.tmp"
    temp_manifest = snapshot_dir / f".{stem}.json.tmp"
    if final_db.exists() or final_manifest.exists():
        raise BackupSafetyError("refusing to overwrite a pre-migration snapshot")
    destination = sqlite3.connect(str(temp_db))
    try:
        conn.backup(destination)
    finally:
        destination.close()
    receipt = (
        _sqlite_receipt(temp_db)
        if existing_identity is not None
        else _legacy_sqlite_receipt(temp_db)
    )
    digest = hashlib.sha256(temp_db.read_bytes()).hexdigest()
    manifest = {
        "format": 1,
        "snapshot": final_db.name,
        "sha256": digest,
        "bytes": temp_db.stat().st_size,
        "created_at": created.isoformat(),
        "protected_until": (created + timedelta(days=DAILY_RETENTION_DAYS)).isoformat(),
        "tier": "daily",
        "reason": cleaned_reason,
        "source_path_sha256": hashlib.sha256(str(live).encode("utf-8")).hexdigest(),
        "schema_from": from_version,
        "schema_to": to_version,
        **receipt,
    }
    temp_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_db, final_db)
    os.replace(temp_manifest, final_manifest)
    for path in (final_db, final_manifest):
        try:
            path.chmod(0o444)
        except OSError:
            pass
    return final_db


def create_snapshot(
    project_path: str | None,
    *,
    reason: str = "cadence",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create and verify an atomic online snapshot of the selected vault."""
    import db  # local import keeps schema_version able to use this module later

    created = (now or _now()).astimezone(timezone.utc)
    conn = db.connect(project_path)
    try:
        identity = getattr(conn, "_kb_vault_identity", None)
        if not isinstance(identity, vault_identity.VaultIdentity):
            raise BackupSafetyError("live connection has no validated vault identity")
        vault_dir = paths.project_dir(project_path).resolve()
        snapshot_dir = _snapshot_dir(identity, vault_dir)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        day = created.date().isoformat()
        daily = not _daily_exists(snapshot_dir, day)
        tier = "daily" if daily else "six-hour"
        retention = DAILY_RETENTION_DAYS if daily else FREQUENT_RETENTION_DAYS
        protected_until = created + timedelta(days=retention)
        cleaned_reason = _REASON_RE.sub("-", reason.lower()).strip("-") or "snapshot"
        stem = f"{created.strftime('%Y%m%dT%H%M%S%fZ')}--{tier}--{cleaned_reason}"
        final_db = snapshot_dir / f"{stem}.db"
        temp_db = snapshot_dir / f".{stem}.db.tmp"
        final_manifest = snapshot_dir / f"{stem}.json"
        temp_manifest = snapshot_dir / f".{stem}.json.tmp"
        if final_db.exists() or final_manifest.exists():
            raise BackupSafetyError("refusing to overwrite an existing snapshot")
        destination = sqlite3.connect(str(temp_db))
        try:
            conn.backup(destination)
        finally:
            destination.close()
        receipt = _sqlite_receipt(temp_db)
        if receipt["vault_uuid"] != identity.vault_uuid:
            raise BackupSafetyError("snapshot identity differs from live vault")
        digest = hashlib.sha256(temp_db.read_bytes()).hexdigest()
        manifest = {
            "format": 1,
            "snapshot": final_db.name,
            "sha256": digest,
            "bytes": temp_db.stat().st_size,
            "created_at": created.isoformat(),
            "protected_until": protected_until.isoformat(),
            "tier": tier,
            "reason": cleaned_reason,
            **receipt,
        }
        temp_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_db, final_db)
        os.replace(temp_manifest, final_manifest)
        for path in (final_db, final_manifest):
            try:
                path.chmod(0o444)
            except OSError:
                pass
        return {"database": str(final_db), "manifest": str(final_manifest), **manifest}
    finally:
        conn.close()


def _parse_timestamp(value: object, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise BackupSafetyError(f"invalid {field} timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def prune_expired(
    project_path: str | None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete only expired verified pairs; there is no force/protected override."""
    import db

    current = (now or _now()).astimezone(timezone.utc)
    conn = db.connect(project_path)
    try:
        identity = getattr(conn, "_kb_vault_identity", None)
        if not isinstance(identity, vault_identity.VaultIdentity):
            raise BackupSafetyError("live connection has no validated vault identity")
        snapshot_dir = _snapshot_dir(identity, paths.project_dir(project_path).resolve())
    finally:
        conn.close()
    candidates = _manifests(snapshot_dir)
    if not candidates:
        return {"deleted": 0, "protected": 0, "skipped": 0}
    newest = max(
        candidates,
        key=lambda item: _parse_timestamp(item[1].get("created_at"), field="created_at"),
    )[0]
    result = {"deleted": 0, "protected": 0, "skipped": 0}
    for manifest_path, manifest in candidates:
        if manifest_path == newest:
            result["protected"] += 1
            continue
        if manifest.get("vault_uuid") != identity.vault_uuid:
            result["skipped"] += 1
            continue
        protected_until = _parse_timestamp(
            manifest.get("protected_until"), field="protected_until"
        )
        if current < protected_until:
            result["protected"] += 1
            continue
        snapshot = snapshot_dir / str(manifest.get("snapshot") or "")
        if not snapshot.is_file():
            result["skipped"] += 1
            continue
        digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if digest != manifest.get("sha256"):
            result["skipped"] += 1
            continue
        for path in (snapshot, manifest_path):
            try:
                path.chmod(0o600)
            except OSError:
                pass
            path.unlink()
        result["deleted"] += 1
    return result


def verify_restore(manifest_path: Path, *, work_root: Path | None = None) -> dict[str, Any]:
    """Restore a snapshot to a new disposable directory and verify it."""
    manifest_path = manifest_path.resolve(strict=True)
    manifest = _read_manifest(manifest_path)
    snapshot = manifest_path.parent / str(manifest.get("snapshot") or "")
    if not snapshot.is_file():
        raise BackupSafetyError("snapshot referenced by manifest is missing")
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    if digest != manifest.get("sha256"):
        raise BackupSafetyError("snapshot hash differs from manifest")
    parent = work_root.resolve() if work_root else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    restore_dir = Path(tempfile.mkdtemp(prefix="latch-restore-drill-", dir=parent))
    restored = restore_dir / "kb.db"
    try:
        source = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(str(restored))
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        if manifest.get("identity_state") == "unidentified-existing-treated-as-production":
            receipt = _legacy_sqlite_receipt(restored)
        else:
            receipt = _sqlite_receipt(restored)
            if receipt["vault_uuid"] != manifest.get("vault_uuid"):
                raise BackupSafetyError("restored vault identity differs from manifest")
        if receipt["nodes"] != manifest.get("nodes"):
            raise BackupSafetyError("restored node count differs from manifest")
        return {"ok": True, "manifest": str(manifest_path), **receipt}
    finally:
        shutil.rmtree(restore_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="create and verify protected Latch backups")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--project")
    create.add_argument("--reason", default="manual")
    prune = sub.add_parser("prune")
    prune.add_argument("--project")
    restore = sub.add_parser("verify-restore")
    restore.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    if args.command == "create":
        result = create_snapshot(args.project, reason=args.reason)
    elif args.command == "prune":
        result = prune_expired(args.project)
    else:
        result = verify_restore(args.manifest)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
