"""Codex session handoff helpers.

Codex exposes the current thread id to hooks, but not necessarily to MCP server
children. The SessionStart hook writes a small per-project marker and the MCP
server can read it lazily when it is running under the Codex adapter.
"""
from __future__ import annotations

import json
import hashlib
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import paths


MARKER_FILE = "codex_session.json"
MARKER_DIR = "codex_sessions"
FALLBACK_DIR_PREFIX = "latch-codex-session-"


def _current_uid() -> int | None:
    """Numeric OS owner identity, or None where ownership cannot be proven."""
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        return None
    try:
        return int(getuid())
    except (OSError, TypeError, ValueError):
        return None


def _canonical_path(value: str | os.PathLike) -> str:
    source = Path(value).expanduser()
    try:
        return str(source.resolve())
    except OSError:
        return os.path.abspath(str(source))


def _workspace_key(project_path: str | os.PathLike | None = None) -> str:
    project = _canonical_project(project_path)
    return hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]


def marker_path(project_path: str | os.PathLike | None = None) -> Path:
    """Per-workspace rendezvous path, even when the KB itself is pinned.

    A pinned vault intentionally ignores cwd for database selection.  Session
    handoff is different: collapsing all workspaces onto one marker causes the
    newest Codex task to misattribute every other live MCP connection.  Keep
    markers under the pinned runtime directory but key them by canonical cwd.
    """
    return (
        paths.project_dir(project_path)
        / "runtime"
        / MARKER_DIR
        / _workspace_key(project_path)
        / MARKER_FILE
    )


def _fallback_scope_key(project_path: str | os.PathLike | None = None) -> str:
    """Stable, non-identifying key for this OS user + install + selected vault."""
    uid = _current_uid()
    user_identity = (
        f"uid:{uid}"
        if uid is not None
        else f"ownership-unavailable:{_canonical_path(Path.home())}"
    )
    identity = "\0".join(
        (
            user_identity,
            _canonical_path(paths.KB_ROOT),
            _canonical_path(paths.project_dir(project_path)),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _fallback_marker_path(
    project_path: str | os.PathLike | None = None,
) -> Path:
    """User-scoped temp rendezvous used only when the primary marker cannot write.

    Both the install/vault identity and workspace identity are hashed.  A moved
    install, a different selected vault, or a different workspace therefore
    cannot inherit another marker.
    """
    root = Path(tempfile.gettempdir()) / (
        FALLBACK_DIR_PREFIX + _fallback_scope_key(project_path)
    )
    return root / MARKER_DIR / _workspace_key(project_path) / MARKER_FILE


def _legacy_marker_path(project_path: str | os.PathLike | None = None) -> Path:
    return paths.project_dir(project_path) / MARKER_FILE


def _canonical_project(project_path: str | os.PathLike | None = None) -> str:
    source = Path(project_path or os.getcwd()).expanduser()
    try:
        return str(source.resolve())
    except OSError:
        return os.path.abspath(str(source))


def _owned_path(path: Path, *, directory: bool) -> bool:
    """Accept only non-symlink paths provably owned by the current OS user."""
    uid = _current_uid()
    if uid is None:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            return False
    elif not stat.S_ISREG(info.st_mode):
        return False
    if info.st_uid != uid:
        return False
    return True


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    if not _owned_path(path, directory=True):
        raise PermissionError(f"unsafe Codex marker fallback directory: {path}")
    try:
        path.chmod(0o700)
    except OSError:
        pass  # Ownership was already verified; mode hardening is best effort.


def _prepare_fallback_parent(path: Path) -> None:
    scope_root = path.parents[2]
    marker_root = path.parents[1]
    workspace_root = path.parent
    _ensure_private_directory(scope_root)
    _ensure_private_directory(marker_root)
    _ensure_private_directory(workspace_root)


def _write_primary_marker(path: Path, text: str) -> None:
    """Keep the canonical vault-local marker as the first write destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _write_fallback_marker(path: Path, text: str) -> None:
    """Atomically write a user-private marker without following file symlinks."""
    if _current_uid() is None:
        raise PermissionError(
            "secure Codex marker fallback is unavailable: "
            "this platform cannot prove temporary-path ownership"
        )
    _prepare_fallback_parent(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{MARKER_FILE}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp = Path(tmp_name)
    try:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
        os.replace(tmp, path)
        if not _owned_path(path, directory=False):
            raise PermissionError(f"unsafe Codex marker fallback file: {path}")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _fallback_marker_is_safe(path: Path) -> bool:
    return (
        _owned_path(path.parents[2], directory=True)
        and _owned_path(path.parents[1], directory=True)
        and _owned_path(path.parent, directory=True)
        and _owned_path(path, directory=False)
    )


def _marker_updated_at(payload: dict) -> datetime | None:
    """Parse an aware marker timestamp and normalize it to UTC."""
    value = payload.get("updated_at")
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def write_marker(
    project_path: str | os.PathLike | None,
    session_id: str,
    *,
    transcript_path: str | None = None,
) -> Path:
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    payload = {
        "session_id": sid,
        "transcript_path": transcript_path,
        "project_path": _canonical_project(project_path),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "codex_session_start",
    }
    return _persist_marker(project_path, payload)


def invalidate_marker(
    project_path: str | os.PathLike | None,
) -> Path:
    """Write a newer tombstone so an old task marker cannot be inherited."""
    payload = {
        "project_path": _canonical_project(project_path),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "codex_session_start",
        "invalidated": True,
    }
    return _persist_marker(project_path, payload)


def _persist_marker(
    project_path: str | os.PathLike | None,
    payload: dict,
) -> Path:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    primary = marker_path(project_path)
    try:
        _write_primary_marker(primary, text)
        return primary
    except OSError:
        fallback = _fallback_marker_path(project_path)
        _write_fallback_marker(fallback, text)
        return fallback


def read_marker(project_path: str | os.PathLike | None = None) -> dict | None:
    expected_project = _canonical_project(project_path)
    fallback = _fallback_marker_path(project_path)
    candidates = (
        (0, marker_path(project_path), False),
        (1, fallback, True),
        (2, _legacy_marker_path(project_path), False),
    )
    newest: tuple[datetime, int, dict] | None = None
    for path_priority, path, private_fallback in candidates:
        if private_fallback and not _fallback_marker_is_safe(path):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        marker_project = payload.get("project_path")
        if not isinstance(marker_project, str):
            continue
        if _canonical_project(marker_project) != expected_project:
            continue
        updated_at = _marker_updated_at(payload)
        if updated_at is None:
            continue
        candidate = (updated_at, -path_priority, payload)
        if newest is None or candidate[:2] > newest[:2]:
            newest = candidate
    if newest is None:
        return None
    payload = newest[2]
    if payload.get("invalidated") is True:
        return None
    marker_session = payload.get("session_id")
    if not isinstance(marker_session, str) or not marker_session.strip():
        return None
    return payload


def read_session_id(project_path: str | os.PathLike | None = None) -> str | None:
    payload = read_marker(project_path)
    if not payload:
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str):
        return None
    sid = sid.strip()
    return sid or None
