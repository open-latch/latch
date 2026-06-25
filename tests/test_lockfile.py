"""Unit tests for lockfile.py — the shared compactor lock + write-side
wait_for_compaction helper.

Covers:
- no-lock: wait_for_compaction returns immediately.
- live-PID lock: wait_for_compaction times out (we don't steal a live lock).
- stale-PID lock: wait_for_compaction unlinks the lock and returns.
- legacy single-line PID file: parsed correctly.
- compactor_lock: acquire-or-skip semantics preserved (the existing
  compactor.py contract).
- lock contents include PID + timestamp on the new code path.

We pick a clearly-dead PID by writing a number well above the typical OS
range. On Windows, OpenProcess returns 0/ERROR_INVALID_PARAMETER for
pids that don't exist; on POSIX, os.kill(pid, 0) raises ProcessLookupError.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import lockfile  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


# A PID that is essentially guaranteed not to be in use.
# Windows max PID is ~4 million in practice (depends on session); POSIX
# generally well under 4 million. 9_999_991 is safely outside both.
DEAD_PID = 9_999_991


def _fresh_project() -> str:
    return tempfile.mkdtemp(prefix="kb_lockfile_test_")


def test_no_lock_returns_immediately():
    proj = _fresh_project()
    try:
        t0 = time.monotonic()
        lockfile.wait_for_compaction(proj, timeout_s=5.0)
        elapsed = time.monotonic() - t0
        _assert(elapsed < 0.5, f"expected near-instant return, got {elapsed:.3f}s")
        print(f"PASS no_lock_returns_immediately ({elapsed*1000:.1f}ms)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_stale_pid_lock_is_stolen():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(f"{DEAD_PID}\n2026-01-01T00:00:00+00:00",
                             encoding="utf-8")
        t0 = time.monotonic()
        lockfile.wait_for_compaction(proj, timeout_s=5.0)
        elapsed = time.monotonic() - t0
        _assert(not lock_file.exists(),
                f"stale lock not unlinked: still at {lock_file}")
        _assert(elapsed < 0.5,
                f"expected fast stale-PID detection, got {elapsed:.3f}s")
        print(f"PASS stale_pid_lock_is_stolen ({elapsed*1000:.1f}ms)")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_live_pid_lock_times_out():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Our own PID is definitely alive — wait_for_compaction must NOT
        # steal it.
        lock_file.write_text(f"{os.getpid()}\n2026-01-01T00:00:00+00:00",
                             encoding="utf-8")
        t0 = time.monotonic()
        raised = False
        try:
            lockfile.wait_for_compaction(proj, timeout_s=0.5,
                                         poll_interval_s=0.05)
        except lockfile.CompactionInProgressError:
            raised = True
        elapsed = time.monotonic() - t0
        _assert(raised, "expected CompactionInProgressError")
        _assert(lock_file.exists(),
                "live-PID lock must NOT be unlinked on timeout")
        _assert(0.4 <= elapsed < 2.0,
                f"timeout ~0.5s expected, got {elapsed:.3f}s")
        print(f"PASS live_pid_lock_times_out ({elapsed*1000:.1f}ms)")
    finally:
        # Clean up — we left a live-PID lock behind.
        try:
            lockfile._lock_path(proj).unlink()
        except OSError:
            pass
        shutil.rmtree(proj, ignore_errors=True)


def test_legacy_single_line_pid_file_parses():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Pre-2026-05-27 format: just the PID, no timestamp line.
        lock_file.write_text(str(DEAD_PID), encoding="utf-8")
        pid, ts = lockfile._read_lock(lock_file)
        _assert(pid == DEAD_PID, f"expected {DEAD_PID}, got {pid}")
        _assert(ts is None, f"expected None timestamp, got {ts!r}")
        # And wait_for_compaction should still detect the stale PID
        # and unlink.
        lockfile.wait_for_compaction(proj, timeout_s=2.0)
        _assert(not lock_file.exists(),
                "legacy stale PID lock should be cleaned up")
        print("PASS legacy_single_line_pid_file_parses")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_compactor_lock_acquire_or_skip():
    proj = _fresh_project()
    try:
        # First acquire succeeds, second yields False, then the first
        # releases and the file is gone.
        with lockfile.compactor_lock(proj) as a1:
            _assert(a1 is True, "first acquire should succeed")
            with lockfile.compactor_lock(proj) as a2:
                _assert(a2 is False,
                        "second acquire while held should yield False")
            _assert(lockfile._lock_path(proj).exists(),
                    "outer lock should still be held after inner fails")
        _assert(not lockfile._lock_path(proj).exists(),
                "lock should be released on context exit")
        print("PASS compactor_lock_acquire_or_skip")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_compactor_lock_writes_pid_and_timestamp():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired is True, "acquire failed")
            pid, ts = lockfile._read_lock(lock_file)
            _assert(pid == os.getpid(),
                    f"expected own pid {os.getpid()}, got {pid}")
            _assert(ts is not None and "T" in ts,
                    f"expected ISO timestamp, got {ts!r}")
        print(f"PASS compactor_lock_writes_pid_and_timestamp (pid={pid})")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_compactor_lock_evicts_stale_dead_pid_lock():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Orphaned lock from a crashed compactor (dead PID).
        lock_file.write_text(f"{DEAD_PID}\n2026-01-01T00:00:00+00:00",
                             encoding="utf-8")
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired is True,
                    "acquire should evict a dead-PID lock and succeed")
            pid, _ = lockfile._read_lock(lock_file)
            _assert(pid == os.getpid(),
                    f"lock should now hold our pid, got {pid}")
        _assert(not lock_file.exists(),
                "lock should be released on context exit")
        print("PASS compactor_lock_evicts_stale_dead_pid_lock")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_compactor_lock_evicts_legacy_stale_lock():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Legacy single-line PID format, dead holder.
        lock_file.write_text(str(DEAD_PID), encoding="utf-8")
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired is True,
                    "acquire should evict a legacy-format dead-PID lock")
        print("PASS compactor_lock_evicts_legacy_stale_lock")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_compactor_lock_does_not_steal_live_lock():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        # Our own PID is alive — must NOT be stolen.
        lock_file.write_text(f"{os.getpid()}\n2026-01-01T00:00:00+00:00",
                             encoding="utf-8")
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired is False,
                    "acquire must yield False on a live-PID lock")
        _assert(lock_file.exists(),
                "live-PID lock must NOT be unlinked")
        print("PASS compactor_lock_does_not_steal_live_lock")
    finally:
        try:
            lockfile._lock_path(proj).unlink()
        except OSError:
            pass
        shutil.rmtree(proj, ignore_errors=True)


def test_compactor_lock_does_not_steal_unparseable_lock():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text("not-a-pid\ngarbage", encoding="utf-8")
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired is False,
                    "acquire must yield False when the lock body is "
                    "unparseable — we can't prove the holder is dead")
        _assert(lock_file.exists(),
                "unparseable lock must NOT be unlinked")
        print("PASS compactor_lock_does_not_steal_unparseable_lock")
    finally:
        try:
            lockfile._lock_path(proj).unlink()
        except OSError:
            pass
        shutil.rmtree(proj, ignore_errors=True)


def test_pid_alive_for_self_and_dead_pid():
    _assert(lockfile._pid_alive(os.getpid()) is True,
            "own pid should be alive")
    _assert(lockfile._pid_alive(DEAD_PID) is False,
            f"pid {DEAD_PID} should be reported dead")
    _assert(lockfile._pid_alive(0) is False,
            "pid 0 should be reported not-alive")
    _assert(lockfile._pid_alive(-1) is False,
            "negative pid should be reported not-alive")
    print("PASS pid_alive_for_self_and_dead_pid")


def test_wait_releases_when_lock_disappears_mid_poll():
    """Simulate the in-flight case where the compactor finishes while a
    writer is polling. We create a live-PID lock, then in a thread unlink
    it after a short delay, and assert wait_for_compaction returns
    cleanly within the timeout."""
    import threading

    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(f"{os.getpid()}\n2026-01-01T00:00:00+00:00",
                             encoding="utf-8")

        def _release_after():
            time.sleep(0.3)
            try:
                lock_file.unlink()
            except OSError:
                pass

        threading.Thread(target=_release_after, daemon=True).start()
        t0 = time.monotonic()
        lockfile.wait_for_compaction(proj, timeout_s=5.0,
                                     poll_interval_s=0.05)
        elapsed = time.monotonic() - t0
        _assert(0.2 <= elapsed < 2.0,
                f"expected ~0.3s wait, got {elapsed:.3f}s")
        print(f"PASS wait_releases_when_lock_disappears_mid_poll "
              f"({elapsed*1000:.1f}ms)")
    finally:
        try:
            lockfile._lock_path(proj).unlink()
        except OSError:
            pass
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    test_no_lock_returns_immediately()
    test_stale_pid_lock_is_stolen()
    test_live_pid_lock_times_out()
    test_legacy_single_line_pid_file_parses()
    test_compactor_lock_acquire_or_skip()
    test_compactor_lock_writes_pid_and_timestamp()
    test_compactor_lock_evicts_stale_dead_pid_lock()
    test_compactor_lock_evicts_legacy_stale_lock()
    test_compactor_lock_does_not_steal_live_lock()
    test_compactor_lock_does_not_steal_unparseable_lock()
    test_pid_alive_for_self_and_dead_pid()
    test_wait_releases_when_lock_disappears_mid_poll()
    print("\nAll lockfile tests pass.")
