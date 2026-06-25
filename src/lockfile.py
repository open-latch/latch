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
- Lock body is `<pid>\\n<acquired_at_iso_utc>` — two lines. Legacy
  single-line PID files (pre-2026-05-27) parse as PID-only with empty
  timestamp; readers tolerate that.
- `wait_for_compaction(project_path, timeout_s=60)` polls until the lock
  file disappears OR the writing PID is confirmed dead (stale, unlink),
  OR `timeout_s` elapses (raises `CompactionInProgressError`). We do not
  steal a live lock — only one that names a dead PID.

Why not block on a stdlib threading.Lock or a SQLite advisory lock instead:
the compactor runs in a separate Python process (spawned by the bash
wrapper), so an in-process lock can't see it. SQLite advisory locks don't
exist in stdlib sqlite3. A filesystem sentinel is the lowest-coupling
primitive that both processes can observe.
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paths

LOCK_FILENAME = "compactor.lock"

# Default ceiling for MCP write tools waiting on an in-flight compaction.
# Matches the embedding warm-up lock-acquire timeout (id=401) so a single
# pathological compaction can't stall the session arbitrarily.
WRITE_LOCK_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 0.1


class CompactionInProgressError(RuntimeError):
    """Raised by `wait_for_compaction` when the timeout elapses with the lock
    still held by a live PID."""


def _lock_path(project_path: str) -> Path:
    return paths.project_dir(project_path) / LOCK_FILENAME


def _read_lock(lock_file: Path) -> tuple[int | None, str | None]:
    """Return (pid, acquired_at_iso) parsed from the lock file, or (None, None)
    if the file is missing or unreadable. Tolerates legacy single-line PID
    files and any post-parse garbage."""
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
def compactor_lock(project_path: str):
    """Atomic acquire-or-skip lock for the compactor. Yields True if acquired,
    False if already held by a live PID.

    A lock whose recorded PID is provably dead (crashed/killed compactor) is
    evicted and acquisition retried once — same liveness rule as
    `wait_for_compaction`. We never steal a lock we can't prove is dead:
    a live PID or an unparseable body yields False. The compactor uses this;
    writers use `wait_for_compaction` instead."""
    lock_file = _lock_path(project_path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pid, _ = _read_lock(lock_file)
        if pid is None or _pid_alive(pid):
            yield False
            return
        try:
            lock_file.unlink()
        except OSError:
            pass
        try:
            fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Lost the post-eviction re-acquire race to another process.
            yield False
            return
    try:
        payload = f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}"
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        yield True
    finally:
        try:
            lock_file.unlink()
        except OSError:
            pass


def wait_for_compaction(
    project_path: str,
    timeout_s: float = WRITE_LOCK_TIMEOUT_S,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> None:
    """Block until any in-flight compaction releases its lock.

    Returns immediately when no lock exists. Detects stale locks left by a
    crashed compactor by checking PID liveness and unlinks them. Raises
    `CompactionInProgressError` after `timeout_s` seconds if the lock is
    still held by a live PID.

    Never steals a live lock — a compactor that legitimately takes >60s
    must surface as a timeout to the caller; we do not corrupt the
    compactor's read-extract-write window."""
    lock_file = _lock_path(project_path)
    if not lock_file.exists():
        return
    deadline = time.monotonic() + timeout_s
    while True:
        if not lock_file.exists():
            return
        pid, _ = _read_lock(lock_file)
        if pid is not None and not _pid_alive(pid):
            try:
                lock_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return
        if time.monotonic() >= deadline:
            raise CompactionInProgressError(
                f"compaction lock at {lock_file} still held after {timeout_s}s"
            )
        time.sleep(poll_interval_s)
