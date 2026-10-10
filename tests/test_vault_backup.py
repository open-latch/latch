from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.store import db  # noqa: E402
from latch.store import paths  # noqa: E402
from latch.store import vault_backup  # noqa: E402
from latch.store import vault_identity  # noqa: E402


UTC = timezone.utc


def _seed(scope: Path) -> None:
    conn = db.connect(str(scope))
    try:
        conn.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','protected','survives','canonical')"
        )
        conn.commit()
    finally:
        conn.close()


def test_snapshot_is_external_atomic_verified_and_restorable(tmp_path):
    _seed(tmp_path)
    created = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    receipt = vault_backup.create_snapshot(str(tmp_path), reason="test", now=created)
    database = Path(receipt["database"])
    manifest_path = Path(receipt["manifest"])
    live = paths.project_dir(str(tmp_path))
    test_root = paths.validated_test_root()
    assert database.is_relative_to(test_root / "backups")
    assert not database.is_relative_to(live)
    assert database.name.endswith("--daily--test.db")
    assert not list(database.parent.glob("*.tmp"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert hashlib.sha256(database.read_bytes()).hexdigest() == manifest["sha256"]
    assert manifest["integrity_check"] == "ok"
    assert manifest["protected_until"] == (created + timedelta(days=30)).isoformat()
    restored = vault_backup.verify_restore(manifest_path, work_root=tmp_path / "drills")
    assert restored["ok"] is True
    assert restored["nodes"] == 1
    assert list((tmp_path / "drills").iterdir()) == []


def test_daily_and_six_hour_retention_cannot_be_pruned_early(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    daily = vault_backup.create_snapshot(str(tmp_path), reason="cadence", now=start)
    frequent = vault_backup.create_snapshot(
        str(tmp_path), reason="cadence", now=start + timedelta(hours=6)
    )
    assert daily["tier"] == "daily"
    assert frequent["tier"] == "six-hour"
    assert daily["protected_until"] == (start + timedelta(days=30)).isoformat()
    assert frequent["protected_until"] == (
        start + timedelta(hours=6, days=5)
    ).isoformat()

    before = {
        Path(daily["database"]): Path(daily["database"]).read_bytes(),
        Path(frequent["database"]): Path(frequent["database"]).read_bytes(),
    }
    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=4, hours=23)
    )
    assert result["deleted"] == 0
    assert all(path.read_bytes() == body for path, body in before.items())


def test_prune_deletes_only_expired_verified_pair_and_keeps_newest(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    old_daily = vault_backup.create_snapshot(str(tmp_path), reason="old", now=start)
    old_frequent = vault_backup.create_snapshot(
        str(tmp_path), reason="old", now=start + timedelta(hours=6)
    )
    newest = vault_backup.create_snapshot(
        str(tmp_path), reason="new", now=start + timedelta(days=1)
    )
    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )
    assert result["deleted"] == 2
    assert not Path(old_daily["database"]).exists()
    assert not Path(old_frequent["database"]).exists()
    assert Path(newest["database"]).exists()


def test_corrupt_snapshot_is_never_pruned(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    corrupt = vault_backup.create_snapshot(str(tmp_path), reason="corrupt", now=start)
    vault_backup.create_snapshot(
        str(tmp_path), reason="new", now=start + timedelta(days=1)
    )
    database = Path(corrupt["database"])
    database.chmod(0o600)
    database.write_bytes(database.read_bytes() + b"tampered")
    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )
    assert result["skipped"] == 1
    assert database.exists()


def test_invalid_manifest_timestamp_does_not_block_other_expired_pruning(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    invalid = vault_backup.create_snapshot(str(tmp_path), reason="invalid", now=start)
    expired = vault_backup.create_snapshot(
        str(tmp_path), reason="expired", now=start + timedelta(hours=6)
    )
    newest = vault_backup.create_snapshot(
        str(tmp_path), reason="newest", now=start + timedelta(days=1)
    )
    manifest_path = Path(invalid["manifest"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["created_at"] = "not-a-timestamp"
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )

    assert result == {"deleted": 1, "protected": 1, "skipped": 1}
    assert Path(invalid["database"]).exists()
    assert not Path(expired["database"]).exists()
    assert Path(newest["database"]).exists()


class _WindowsCPython311Os:
    """The real ``os`` module as vault_backup sees it on Windows CPython 3.11.

    Windows has no ``O_NOFOLLOW``, and before 3.13 it also has no
    ``os.fchmod`` and no ``follow_symlinks`` support in ``os.chmod``.
    """

    name = "nt"
    supports_follow_symlinks = os.supports_follow_symlinks - {os.chmod}

    def __getattr__(self, attribute: str):
        if attribute in {"fchmod", "lchmod", "O_NOFOLLOW"}:
            raise AttributeError(attribute)
        return getattr(os, attribute)


class _WindowsCPython313Os:
    """The real ``os`` module as vault_backup sees it on Windows CPython 3.13+.

    3.13 adds ``os.fchmod`` and ``follow_symlinks`` for ``os.chmod``.  Its
    fchmod sets the attribute through the handle, which needs
    FILE_WRITE_ATTRIBUTES, so an ``O_RDONLY`` descriptor is access denied.
    """

    name = "nt"

    def __init__(self) -> None:
        self.supports_follow_symlinks = os.supports_follow_symlinks | {self.chmod}

    def fchmod(self, descriptor, mode):
        raise PermissionError(13, "Access is denied")

    def chmod(self, path, mode, *, follow_symlinks=True):
        # Without following, Windows sets a symlink's own attribute.
        if follow_symlinks or not os.path.islink(path):
            os.chmod(path, mode)

    def __getattr__(self, attribute: str):
        if attribute == "O_NOFOLLOW":
            raise AttributeError(attribute)
        return getattr(os, attribute)


def _simulate_windows(monkeypatch, windows_os) -> None:
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        # DeleteFileW refuses a file that still has the read-only attribute.
        if os.path.lexists(self) and not self.lstat().st_mode & stat.S_IWRITE:
            raise PermissionError(13, "Access is denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(vault_backup, "os", windows_os)
    monkeypatch.setattr(Path, "unlink", unlink)


@pytest.mark.parametrize(
    "windows_os",
    [_WindowsCPython311Os, _WindowsCPython313Os],
    ids=["cpython311", "cpython313"],
)
def test_windows_prune_clears_read_only_snapshots(tmp_path, monkeypatch, windows_os):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    old_daily = vault_backup.create_snapshot(str(tmp_path), reason="old", now=start)
    old_frequent = vault_backup.create_snapshot(
        str(tmp_path), reason="old", now=start + timedelta(hours=6)
    )
    newest = vault_backup.create_snapshot(
        str(tmp_path), reason="new", now=start + timedelta(days=1)
    )
    _simulate_windows(monkeypatch, windows_os())

    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )

    assert result == {"deleted": 2, "protected": 1, "skipped": 0}
    for receipt in (old_daily, old_frequent):
        assert not Path(receipt["database"]).exists()
        assert not Path(receipt["manifest"]).exists()
    assert Path(newest["database"]).exists()
    assert Path(newest["manifest"]).exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows refuses to rename a file that prune holds open, so this "
    "swap cannot be staged there",
)
@pytest.mark.parametrize("swap_in", ["file", "symlink"])
def test_windows_prune_refuses_a_snapshot_swapped_before_the_by_name_chmod(
    tmp_path, monkeypatch, swap_in
):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    raced = vault_backup.create_snapshot(str(tmp_path), reason="raced", now=start)
    vault_backup.create_snapshot(
        str(tmp_path), reason="newer", now=start + timedelta(days=1)
    )
    database = Path(raced["database"])
    manifest = Path(raced["manifest"])
    aside = database.with_name(database.name + ".aside")
    victim = tmp_path / "read-only-swap-target.txt"
    victim.write_bytes(b"must survive")
    victim.chmod(stat.S_IREAD)
    if swap_in == "symlink":
        probe = tmp_path / "symlink-probe"
        try:
            probe.symlink_to(victim)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
        probe.unlink()
    chmods: list[Path] = []

    class _SwapBeforeChmod(_WindowsCPython311Os):
        def chmod(self, path, mode, **kwargs):
            # Swap the validated snapshot out between prune's identity check
            # and its by-name chmod.
            chmods.append(Path(path))
            if Path(path) == database and not os.path.lexists(aside):
                os.replace(database, aside)
                if swap_in == "symlink":
                    database.symlink_to(victim)
                else:
                    database.write_bytes(b"swapped in")
                    database.chmod(stat.S_IREAD)
            os.chmod(path, mode, **kwargs)

    _simulate_windows(monkeypatch, _SwapBeforeChmod())

    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )

    assert chmods == [database]
    assert result == {"deleted": 0, "protected": 1, "skipped": 1}
    assert os.path.lexists(database)
    assert aside.exists()
    assert manifest.exists()
    # The by-name chmod may clear the swapped-in target's read-only bit, but
    # the identity recheck refuses the pair, so nothing is deleted.
    assert victim.read_bytes() == b"must survive"
    if swap_in == "file":
        assert database.read_bytes() == b"swapped in"


def test_orphan_daily_manifest_cannot_suppress_required_daily_snapshot(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    orphan = vault_backup.create_snapshot(
        str(tmp_path), reason="orphan", now=start
    )
    manifest_path = Path(orphan["manifest"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["snapshot"] = "missing.db"
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    replacement = vault_backup.create_snapshot(
        str(tmp_path), reason="replacement", now=start + timedelta(hours=1)
    )

    assert replacement["tier"] == "daily"
    assert Path(replacement["database"]).is_file()


def test_shortened_manifest_deadline_never_allows_early_pruning(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    shortened = vault_backup.create_snapshot(
        str(tmp_path), reason="shortened", now=start
    )
    vault_backup.create_snapshot(
        str(tmp_path), reason="newer", now=start + timedelta(days=1)
    )
    manifest_path = Path(shortened["manifest"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["protected_until"] = (start + timedelta(days=1)).isoformat()
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=2)
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert Path(shortened["database"]).is_file()


def test_prune_rejects_snapshot_path_traversal_without_touching_victim(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    malicious = vault_backup.create_snapshot(
        str(tmp_path), reason="traversal", now=start
    )
    vault_backup.create_snapshot(
        str(tmp_path), reason="newer", now=start + timedelta(days=1)
    )
    victim = tmp_path / "outside-snapshot-victim.txt"
    victim.write_bytes(b"must survive")
    victim.chmod(0o640)
    before_mode = victim.stat().st_mode & 0o777

    manifest_path = Path(malicious["manifest"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["snapshot"] = os.path.relpath(victim, manifest_path.parent)
    payload["sha256"] = hashlib.sha256(victim.read_bytes()).hexdigest()
    payload["bytes"] = victim.stat().st_size
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert victim.read_bytes() == b"must survive"
    assert victim.stat().st_mode & 0o777 == before_mode


def test_prune_rejects_symlinked_snapshot_without_touching_target(tmp_path):
    _seed(tmp_path)
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    malicious = vault_backup.create_snapshot(
        str(tmp_path), reason="symlink", now=start
    )
    vault_backup.create_snapshot(
        str(tmp_path), reason="newer", now=start + timedelta(days=1)
    )
    victim = tmp_path / "symlink-target.txt"
    victim.write_bytes(b"must survive")
    victim.chmod(0o640)
    before_mode = victim.stat().st_mode & 0o777
    database = Path(malicious["database"])
    link = database.with_name(database.name + ".candidate-link")
    try:
        link.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    database.chmod(0o600)
    database.unlink()
    link.rename(database)

    manifest_path = Path(malicious["manifest"])
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["sha256"] = hashlib.sha256(victim.read_bytes()).hexdigest()
    payload["bytes"] = victim.stat().st_size
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    result = vault_backup.prune_expired(
        str(tmp_path), now=start + timedelta(days=40)
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert victim.read_bytes() == b"must survive"
    assert victim.stat().st_mode & 0o777 == before_mode


def test_test_runtime_ignores_durability_root_spoof(tmp_path, monkeypatch):
    unsafe = tmp_path / "would-be-test-backups"
    monkeypatch.setenv(vault_backup.DURABILITY_ROOT_ENV, str(unsafe))
    _seed(tmp_path)
    receipt = vault_backup.create_snapshot(str(tmp_path), reason="spoof")
    assert Path(receipt["database"]).is_relative_to(
        paths.validated_test_root() / "backups"
    )
    assert not unsafe.exists()


def test_production_classified_legacy_copy_still_backs_up_inside_test_root(tmp_path):
    vault = paths.project_dir(str(tmp_path))
    vault.mkdir(parents=True)
    legacy = sqlite3.connect(vault / "kb.db")
    try:
        legacy.executescript((ROOT / "src" / "latch" / "store" / "schema.sql").read_text(encoding="utf-8"))
        legacy.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','legacy production copy','safe','canonical')"
        )
        legacy.commit()
    finally:
        legacy.close()
    conn = db.connect(str(tmp_path))
    try:
        assert conn._kb_vault_identity.classification == vault_identity.CLASS_PRODUCTION
    finally:
        conn.close()
    receipt = vault_backup.create_snapshot(str(tmp_path), reason="legacy-copy")
    assert Path(receipt["database"]).is_relative_to(
        paths.validated_test_root() / "backups"
    )


def test_real_restore_recreates_missing_production_registry(tmp_path, monkeypatch):
    vault = paths.project_dir(str(tmp_path / "source"))
    vault.mkdir(parents=True)
    legacy = sqlite3.connect(vault / "kb.db")
    try:
        legacy.executescript((ROOT / "src" / "latch" / "store" / "schema.sql").read_text(encoding="utf-8"))
        legacy.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision','recoverable','survives restore','canonical')"
        )
        legacy.commit()
    finally:
        legacy.close()
    conn = db.connect(str(tmp_path / "source"))
    try:
        identity = conn._kb_vault_identity
    finally:
        conn.close()
    assert identity.classification == vault_identity.CLASS_PRODUCTION
    snapshot = vault_backup.create_snapshot(str(tmp_path / "source"), reason="recovery")

    registry = (
        paths.validated_test_root()
        / "production-registry-shadow"
        / f"{identity.vault_uuid}.json"
    )
    registry.chmod(0o600)
    registry.unlink()
    target = paths.validated_test_root() / "vaults" / "restored-production"

    restored = vault_backup.restore_snapshot(
        Path(snapshot["manifest"]), target_vault=target
    )

    assert restored["ok"] is True
    assert restored["nodes"] == 1
    assert Path(restored["registry"]).is_file()
    monkeypatch.setenv("LATCH_KB_DIR", str(target))
    reopened = db.connect("ignored-after-restore")
    try:
        assert reopened._kb_vault_identity == identity
        assert reopened.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
    finally:
        reopened.close()
    with pytest.raises(vault_identity.VaultSafetyError, match="already exists"):
        vault_identity.register_restored_production_vault(target)
