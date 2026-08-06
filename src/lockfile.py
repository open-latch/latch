"""Shared project-level filesystem lock + wait-for-compaction helper.

Originally lived inline in `compactor.py` (`_project_lock`). Promoted to its
own module so MCP write tools (`kb_insert`, `kb_update`, `kb_link`,
`kb_unlink`) can also consult it: when a compaction is in flight, writes
wait up to `WRITE_LOCK_TIMEOUT_S` for it to finish before proceeding,
rather than racing against the compactor's read-extract-write window.

Design:
- Lock file is `<project_dir>/compactor.lock` (unchanged path).
- Acquire is atomic via `os.O_CREAT | os.O_EXCL` — Windows and POSIX both
  honor it without needing fcntl/msvcrt. A held lock whose PID is provably
  dead is evicted at acquire time (same liveness rule as
  `wait_for_compaction`), so a crashed compactor can't block future
  compactions until someone hand-deletes the file.
- Lock body is `<pid>\\n<acquired_at_iso_utc>\\n<owner_token>`. Legacy
  single-line PID files (pre-2026-05-27) parse as PID-only with empty
  timestamp; readers tolerate that.
- `wait_for_compaction(project_path, timeout_s=60)` polls until the lock
  file disappears OR the writing PID is confirmed dead (stale, unlink),
  OR `timeout_s` elapses (raises `CompactionInProgressError`). We do not
  steal a live lock — only one that names a dead PID.
- `writer_lock(project_path, timeout_s=60)` retries the same atomic acquire
  and, once successful, holds `compactor.lock` for the writer's whole context.
  Use it for multi-commit batches where wait-then-write would leave a race.

Why not block on a stdlib threading.Lock or a SQLite advisory lock instead:
the compactor runs in a separate Python process (spawned by the bash
wrapper), so an in-process lock can't see it. SQLite advisory locks don't
exist in stdlib sqlite3. A filesystem sentinel is the lowest-coupling
primitive that both processes can observe.
"""
from __future__ import annotations

import contextlib
import errno
import hmac
import os
import secrets
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paths
import project_config

LOCK_FILENAME = "compactor.lock"
CLEANUP_LOCK_SUFFIX = ".cleanup"

# Default ceiling for MCP write tools waiting on an in-flight compaction.
# Matches the embedding warm-up lock-acquire timeout (id=401) so a single
# pathological compaction can't stall the session arbitrarily.
WRITE_LOCK_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 0.1

# Same-thread lifecycle helpers may compose code that already owns the project
# writer lock.  Track ownership by the actual normalized lock path (and PID so
# forked children never inherit authority) while preserving exclusion across
# threads and processes.
_WRITER_LOCK_STATE = threading.local()
_ACCESS_LOCK_STATE = threading.local()


def _ownership_key(lock_file: Path) -> tuple[int, str]:
    return (
        os.getpid(),
        os.path.normcase(str(lock_file.resolve())),
    )


def _owned_depths() -> dict[tuple[int, str], int]:
    held = getattr(_WRITER_LOCK_STATE, "depths", None)
    if held is None:
        held = {}
        _WRITER_LOCK_STATE.depths = held
    return held


def _access_holds() -> dict[tuple[int, str], dict[str, object]]:
    held = getattr(_ACCESS_LOCK_STATE, "holds", None)
    if held is None:
        held = {}
        _ACCESS_LOCK_STATE.holds = held
    return held


class CompactionInProgressError(RuntimeError):
    """Raised by `wait_for_compaction` when the timeout elapses with the lock
    still held by a live PID."""


class ProjectTargetChangedError(RuntimeError):
    """A queued writer's project became unlatched or selected another KB."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _validate_advisory_file(path: Path, fd: int) -> None:
    opened = os.fstat(fd)
    current = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise project_config.ProjectConfigError(f"unsafe lock file: {path}")


def _open_advisory_file(path: Path) -> int:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise project_config.ProjectConfigError(f"unsafe lock file: {path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        _validate_advisory_file(path, fd)
    except Exception:
        os.close(fd)
        raise
    return fd


def append_project_log(
    project_path: str | os.PathLike | None,
    filename: str,
    line: str,
    *,
    expected_revision: str | None = None,
) -> None:
    """Append one line inside the exact KB selected for ``project_path``.

    Log files can contain project paths and backend errors, so they follow the
    same project-access lease as database work instead of sharing an
    install-level file.
    """
    if Path(filename).name != filename:
        raise ValueError("project log filename must be a basename")
    project = str(project_path or os.getcwd())
    with project_access_lock(project) as locked_kb:
        if expected_revision is not None:
            if project_config.resolve(project).revision != expected_revision:
                raise ProjectTargetChangedError(
                    "stale_session",
                    "project binding changed before log append",
                )
        prepared_kb = paths.ensure_project_dir(project)
        if os.path.normcase(str(prepared_kb)) != os.path.normcase(str(locked_kb)):
            raise ProjectTargetChangedError(
                "target_changed",
                "project KB changed before log append",
            )
        log_path = locked_kb / filename
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(log_path, flags, 0o600)
        try:
            _validate_advisory_file(log_path, fd)
        except Exception:
            os.close(fd)
            raise
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)


def _advisory_lock(fd: int, *, exclusive: bool) -> None:
    if sys.platform == "win32":
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        mode = msvcrt.LK_NBLCK if exclusive else msvcrt.LK_NBRLCK
        while True:
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, mode, 1)
                break
            except OSError as exc:
                if not _windows_lock_contention(exc):
                    raise
                time.sleep(POLL_INTERVAL_S)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _windows_lock_contention(exc: OSError) -> bool:
    """Distinguish a busy byte range from a permanent Windows lock error."""
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
        exc, "winerror", None
    ) in {32, 33, 36}


def _advisory_unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _access_lock_path(project_path: str) -> Path:
    """Return the canonical scope lock outside every selected KB."""
    return project_config.access_lock_path(project_config.resolve(project_path))


def _target_snapshot(target: project_config.ResolvedScope) -> tuple[object, ...]:
    """Immutable fields whose change invalidates an acquired access lease."""
    # ``project_root`` is caller context, not target identity: global Shared
    # callers intentionally share one exact vault from unrelated roots.
    return (
        target.state,
        target.policy,
        target.scope_id,
        target.target_revision,
        target.revision,
        target.kb_dir,
        target.remembered_kb_dir,
        target.target_fingerprint,
        target.source,
        target.lock_key,
        target.reason_code,
    )


def _active_target_directory(
    project_path: str,
    target: project_config.ResolvedScope,
) -> Path:
    if target.state == project_config.MODE_UNLATCHED:
        raise ProjectTargetChangedError(
            "unlatched", f"project is unlatched: {project_path}"
        )
    if target.state != project_config.MODE_LATCHED or target.kb_dir is None:
        raise ProjectTargetChangedError(
            "locked",
            f"project is locked: {project_path}: {target.reason or 'no safe KB target'}",
        )
    selected = project_config.validated_bound_kb_dir(target)
    if selected is None:
        raise ProjectTargetChangedError(
            "locked", f"project has no safe KB target: {project_path}"
        )
    return selected


def _release_access_hold(
    holds: dict[tuple[int, str], dict[str, object]],
    key: tuple[int, str],
    record: dict[str, object],
) -> None:
    """Release a shared process hold only when its last context exits."""
    record["depth"] = int(record["depth"]) - 1
    if int(record["depth"]) > 0:
        return
    if holds.get(key) is record:
        holds.pop(key, None)
    fd = int(record["fd"])
    try:
        _advisory_unlock(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def project_access_lock(
    project_path: str,
    *,
    kb_dir: str | None = None,
    exclusive: bool = False,
    resolved_target: project_config.ResolvedScope | None = None,
):
    """Hold canonical scope access, or exclusively quiesce a transition.

    Aliases and descendants of one scope share this advisory file.  The full
    target snapshot is revalidated after waiting, so an operation can never
    continue on a binding that changed while it was queued.
    """
    before = resolved_target or project_config.resolve(project_path)
    before_snapshot = _target_snapshot(before)
    canonical_kb: Path | None = None
    if not exclusive:
        if paths.unlatch_scope(project_path, resolved=before) is not None:
            raise ProjectTargetChangedError(
                "unlatched", f"project is unlatched: {project_path}"
            )
        canonical_kb = _active_target_directory(project_path, before)
    requested_kb = (
        project_config.validated_kb_path(kb_dir)
        if kb_dir is not None
        else canonical_kb
    )
    if (
        not exclusive
        and requested_kb is not None
        and canonical_kb is not None
        and os.path.normcase(str(requested_kb))
        != os.path.normcase(str(canonical_kb))
    ):
        raise ProjectTargetChangedError(
            "target_changed",
            f"explicit KB {requested_kb} is not selected by {project_path}",
        )
    locked_kb = requested_kb
    access_file = project_config.access_lock_path(before)
    key = _ownership_key(access_file)
    holds = _access_holds()
    existing = holds.get(key)
    if existing is not None:
        if exclusive and existing["mode"] != "exclusive":
            raise RuntimeError("cannot upgrade a shared project access lock")
        if existing.get("snapshot") != before_snapshot:
            raise ProjectTargetChangedError(
                "target_changed",
                f"project scope changed inside a nested access lease: {project_path}",
            )
        existing["depth"] = int(existing["depth"]) + 1
        try:
            yield locked_kb
        finally:
            _release_access_hold(holds, key, existing)
        return

    fd = _open_advisory_file(access_file)
    locked = False
    registered = False
    try:
        _advisory_lock(fd, exclusive=exclusive)
        locked = True
        _validate_advisory_file(access_file, fd)
        after = project_config.resolve(project_path)
        if _target_snapshot(after) != before_snapshot:
            raise ProjectTargetChangedError(
                "target_changed",
                f"project scope changed while waiting: {project_path}",
            )
        if not exclusive:
            if paths.unlatch_scope(project_path, resolved=after) is not None:
                raise ProjectTargetChangedError(
                    "unlatched",
                    f"project became unlatched while waiting: {project_path}",
                )
            current_kb = _active_target_directory(project_path, after)
            if (
                locked_kb is None
                or os.path.normcase(str(current_kb))
                != os.path.normcase(str(canonical_kb))
            ):
                raise ProjectTargetChangedError(
                    "target_changed",
                    f"project KB changed while waiting: {canonical_kb} -> {current_kb}",
                )
        record = {
            "depth": 1,
            "fd": fd,
            "mode": "exclusive" if exclusive else "shared",
            "snapshot": before_snapshot,
        }
        holds[key] = record
        registered = True
        try:
            yield locked_kb
        finally:
            _release_access_hold(holds, key, record)
    except BaseException:
        if not registered:
            try:
                if locked:
                    _advisory_unlock(fd)
            finally:
                os.close(fd)
        raise


@contextlib.contextmanager
def scope_mutation_lock(project_path: str):
    """Quiesce one canonical scope before its public authority changes.

    A product transition may already own this exact lock exclusively and may
    intentionally evolve the target snapshot across several public mutation
    calls.  Refresh that same canonical hold only after the guarded mutation;
    ordinary access leases keep strict stale-target rejection.
    """
    with project_access_lock(project_path, exclusive=True):
        leased = project_config.resolve(project_path)
        access_file = project_config.access_lock_path(leased)
        key = _ownership_key(access_file)
        record = _access_holds().get(key)
        if (
            record is None
            or record.get("mode") != "exclusive"
            or record.get("snapshot") != _target_snapshot(leased)
        ):
            raise ProjectTargetChangedError(
                "target_changed",
                f"project scope changed after acquiring mutation authority: {project_path}",
            )
        try:
            yield
        finally:
            # Preserve a product wrapper's already-held exclusive lease across
            # intentional create->authorize or mode transition sequences.  A
            # changed canonical key remains a separate lock and is never folded
            # into the old hold.
            try:
                current = project_config.resolve(project_path)
                current_file = project_config.access_lock_path(current)
            except Exception:
                current = None
                current_file = None
            if (
                current is not None
                and current_file == access_file
                and _access_holds().get(key) is record
                and record.get("mode") == "exclusive"
            ):
                record["snapshot"] = _target_snapshot(current)


def _lock_path(project_path: str, *, kb_dir: str | None = None) -> Path:
    directory = (
        project_config.validated_kb_path(kb_dir)
        if kb_dir is not None
        else paths.project_dir(project_path)
    )
    return directory / LOCK_FILENAME


def _read_lock(lock_file: Path) -> tuple[int | None, str | None]:
    """Return (pid, acquired_at_iso) parsed from the lock file, or (None, None)
    if the file is missing or unreadable. Tolerates legacy single-line PID
    files and any post-parse garbage."""
    if lock_file.is_symlink() or (lock_file.exists() and not lock_file.is_file()):
        return None, None
    try:
        text = lock_file.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None, None
    lines = text.splitlines()
    pid: int | None = None
    if lines:
        try:
            pid = int(lines[0].strip())
        except ValueError:
            pid = None
    acquired_at = lines[1].strip() if len(lines) > 1 else None
    return pid, acquired_at


def _read_lock_token(lock_file: Path) -> str | None:
    if lock_file.is_symlink() or (lock_file.exists() and not lock_file.is_file()):
        return None
    try:
        lines = lock_file.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return None
    return lines[2].strip() if len(lines) > 2 and lines[2].strip() else None


@contextlib.contextmanager
def _cleanup_mutex(lock_file: Path):
    """Serialize stale eviction/release with an OS-released advisory lock."""
    mutex_file = lock_file.with_name(lock_file.name + CLEANUP_LOCK_SUFFIX)
    fd = _open_advisory_file(mutex_file)
    try:
        _advisory_lock(fd, exclusive=True)
        try:
            _validate_advisory_file(mutex_file, fd)
            yield
        finally:
            _advisory_unlock(fd)
    finally:
        os.close(fd)


def _release_owned_lock(lock_file: Path, token: str) -> None:
    """Remove only the sentinel created by this acquisition."""
    try:
        with _cleanup_mutex(lock_file):
            if _read_lock_token(lock_file) != token:
                return
            try:
                lock_file.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _prepare_writable_kb(directory: Path) -> Path:
    """Prepare a resolved write target without allowing a final symlink."""
    if directory.is_symlink():
        raise project_config.ProjectConfigError(
            f"project KB directory must not be a symlink: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise project_config.ProjectConfigError(
            f"project KB directory is missing or unsafe: {directory}"
        )
    return directory


def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness check using only stdlib.

    Windows: OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION (0x1000).
    POSIX: os.kill(pid, 0) — signal 0 just probes permission/existence.

    Returns True if the PID exists. Errs on the side of True when uncertain
    (permission errors, weird OSes) — `wait_for_compaction` should never
    steal a lock it can't prove is dead."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                # ERROR_INVALID_PARAMETER (87) = pid doesn't exist.
                # ERROR_ACCESS_DENIED (5)    = pid exists but we can't query.
                err = kernel32.GetLastError()
                if err == 87:
                    return False
                return True
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True


@contextlib.contextmanager
def _compactor_sentinel(project_path: str, *, kb_dir: str):
    """Atomic acquire-or-skip lock for the compactor. Yields True if acquired,
    False if already held by a live PID.

    A lock whose recorded PID is provably dead (crashed/killed compactor) is
    evicted and acquisition retried once — same liveness rule as
    `wait_for_compaction`. We never steal a lock we can't prove is dead:
    a live PID or an unparseable body yields False. The compactor uses this;
    writers use `wait_for_compaction` instead."""
    locked_kb = project_config.validated_kb_path(kb_dir)
    lock_file = locked_kb / LOCK_FILENAME
    lock_key = _ownership_key(lock_file)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    fd: int | None = None
    with _cleanup_mutex(lock_file):
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid, _ = _read_lock(lock_file)
            if pid is not None and not _pid_alive(pid):
                try:
                    lock_file.unlink()
                except OSError:
                    pass
                else:
                    try:
                        fd = os.open(
                            str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                        )
                    except FileExistsError:
                        # A non-cooperating process replaced the sentinel.
                        pass
        if fd is not None:
            created_stat = os.fstat(fd)
            created_identity = (created_stat.st_dev, created_stat.st_ino)
            try:
                payload = (
                    f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n{token}"
                ).encode("utf-8")
                offset = 0
                while offset < len(payload):
                    written = os.write(fd, payload[offset:])
                    if written <= 0:
                        raise OSError("compactor lock write made no progress")
                    offset += written
            except Exception:
                try:
                    current = lock_file.lstat()
                    if (current.st_dev, current.st_ino) == created_identity:
                        lock_file.unlink()
                except OSError:
                    pass
                raise
            finally:
                os.close(fd)
    if fd is None:
        yield False
        return
    held = _owned_depths()
    held[lock_key] = 1
    try:
        yield True
    finally:
        held.pop(lock_key, None)
        _release_owned_lock(lock_file, token)


@contextlib.contextmanager
def compactor_lock(project_path: str, *, kb_dir: str | None = None):
    """Acquire shared project access plus the exclusive writer sentinel."""
    access = project_access_lock(project_path, kb_dir=kb_dir)
    try:
        locked_kb = access.__enter__()
    except ProjectTargetChangedError:
        yield False
        return
    try:
        assert locked_kb is not None
        if kb_dir is None:
            locked_kb = _prepare_writable_kb(locked_kb)
        with _compactor_sentinel(
            project_path, kb_dir=str(locked_kb)
        ) as acquired:
            if acquired and kb_dir is None:
                current = project_config.resolve(project_path)
                if paths.unlatch_scope(
                    project_path, resolved=current
                ) is not None:
                    yield False
                    return
                current_kb = _active_target_directory(project_path, current)
                if os.path.normcase(str(current_kb)) != os.path.normcase(
                    str(locked_kb)
                ):
                    yield False
                    return
            yield acquired
    finally:
        access.__exit__(*sys.exc_info())


def wait_for_compaction(
    project_path: str,
    timeout_s: float = WRITE_LOCK_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    *,
    kb_dir: str | None = None,
) -> None:
    """Block until any in-flight compaction releases its lock.

    Returns immediately when no lock exists. Detects stale locks left by a
    crashed compactor by checking PID liveness and unlinks them. Raises
    `CompactionInProgressError` after `timeout_s` seconds if the lock is
    still held by a live PID.

    Never steals a live lock — a compactor that legitimately takes >60s
    must surface as a timeout to the caller; we do not corrupt the
    compactor's read-extract-write window."""
    lock_file = _lock_path(project_path, kb_dir=kb_dir)
    if not lock_file.exists():
        return
    deadline = time.monotonic() + timeout_s
    while True:
        if not lock_file.exists():
            return
        pid, _ = _read_lock(lock_file)
        if pid is not None and not _pid_alive(pid):
            cleaned = False
            with _cleanup_mutex(lock_file):
                current_pid, _ = _read_lock(lock_file)
                if current_pid is not None and not _pid_alive(current_pid):
                    try:
                        lock_file.unlink()
                    except OSError:
                        pass
                    else:
                        cleaned = True
                elif current_pid is None and not lock_file.exists():
                    cleaned = True
            if cleaned:
                return
        if time.monotonic() >= deadline:
            raise CompactionInProgressError(
                f"compaction lock at {lock_file} still held after {timeout_s}s"
            )
        time.sleep(poll_interval_s)


@contextlib.contextmanager
def _writer_sentinel(
    project_path: str,
    locked_kb: Path,
    *,
    timeout_s: float,
    poll_interval_s: float,
):
    """Acquire only the exclusive compactor sentinel for ``locked_kb``."""
    lock_key = _ownership_key(locked_kb / LOCK_FILENAME)
    held = _owned_depths()
    depth = int(held.get(lock_key, 0))
    if depth:
        held[lock_key] = depth + 1
        try:
            yield
        finally:
            remaining = int(held.get(lock_key, 1)) - 1
            if remaining:
                held[lock_key] = remaining
            else:
                held.pop(lock_key, None)
        return

    deadline = time.monotonic() + timeout_s
    while True:
        # writer_lock already owns the project access lease. Reacquiring it
        # here duplicated resolution and could reject a concurrent first-vault
        # identity finalization even though the binding revision/path stayed
        # unchanged. This layer owns only the per-KB writer sentinel.
        with _compactor_sentinel(
            project_path, kb_dir=str(locked_kb)
        ) as acquired:
            if acquired:
                held[lock_key] = 1
                try:
                    yield
                finally:
                    held.pop(lock_key, None)
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CompactionInProgressError(
                f"writer lock at {locked_kb / LOCK_FILENAME} still held "
                f"after {timeout_s}s"
            )
        time.sleep(min(poll_interval_s, remaining))


@contextlib.contextmanager
def writer_lock(
    project_path: str,
    timeout_s: float = WRITE_LOCK_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    *,
    kb_dir: str | None = None,
    resolved_target: project_config.ResolvedScope | None = None,
):
    """Hold shared target access plus the exclusive writer sentinel.

    Shared target access lets a mode transition quiesce every open KB
    connection without serializing ordinary readers. The writer sentinel keeps
    compaction and multi-commit writes mutually exclusive. Both layers are
    reentrant for composed same-thread operations.
    """
    if timeout_s < 0:
        raise ValueError("timeout_s must be non-negative")
    if poll_interval_s <= 0:
        raise ValueError("poll_interval_s must be positive")
    with project_access_lock(
        project_path,
        kb_dir=kb_dir,
        resolved_target=resolved_target,
    ) as locked_kb:
        assert locked_kb is not None
        if kb_dir is None:
            locked_kb = _prepare_writable_kb(locked_kb)
        with _writer_sentinel(
            project_path,
            locked_kb,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        ):
            if kb_dir is None:
                current = project_config.resolve(project_path)
                if paths.unlatch_scope(
                    project_path, resolved=current
                ) is not None:
                    raise ProjectTargetChangedError(
                        "unlatched", f"project became unlatched: {project_path}"
                    )
                current_kb = _active_target_directory(project_path, current)
                if os.path.normcase(str(current_kb)) != os.path.normcase(
                    str(locked_kb)
                ):
                    raise ProjectTargetChangedError(
                        "target_changed",
                        f"project KB changed while waiting: {locked_kb} -> {current_kb}",
                    )
            yield


@contextlib.contextmanager
def identity_recovery_lock(
    project_path: str,
    target: project_config.ResolvedScope,
    *,
    timeout_s: float = WRITE_LOCK_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
):
    """Quiesce and recover only an exact interrupted first-vault target.

    LOCKED scopes cannot use the ordinary shared writer path.  This narrow
    path takes the canonical access lock exclusively, then the remembered
    exact KB writer sentinel, and revalidates the complete locked snapshot
    before yielding.
    """
    if (
        target.state != project_config.MODE_LOCKED
        or target.reason_code
        not in {
            project_config.LOCK_VAULT_IDENTITY_INITIALIZING,
            project_config.LOCK_VAULT_IDENTITY_PENDING,
        }
        or target.remembered_kb_dir is None
        or target.target_fingerprint is None
    ):
        raise ProjectTargetChangedError(
            "locked", "project is not an exact recoverable identity target"
        )
    locked_kb = project_config.validated_kb_path(target.remembered_kb_dir)
    if not hmac.compare_digest(
        project_config._directory_fingerprint(locked_kb),
        target.target_fingerprint,
    ):
        raise ProjectTargetChangedError(
            "target_changed", "remembered KB directory identity changed"
        )
    before_snapshot = _target_snapshot(target)
    with project_access_lock(
        project_path,
        exclusive=True,
        resolved_target=target,
    ):
        with _writer_sentinel(
            project_path,
            locked_kb,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
        ):
            after = project_config.resolve(project_path)
            if _target_snapshot(after) != before_snapshot:
                raise ProjectTargetChangedError(
                    "target_changed",
                    "project identity target changed while waiting for recovery",
                )
            if not hmac.compare_digest(
                project_config._directory_fingerprint(locked_kb),
                target.target_fingerprint,
            ):
                raise ProjectTargetChangedError(
                    "target_changed", "remembered KB directory identity changed"
                )
            yield locked_kb
