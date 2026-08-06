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
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import lockfile  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402


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


def test_stale_lock_eviction_has_only_one_winner():
    proj = _fresh_project()
    try:
        lock_file = lockfile._lock_path(proj)
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(
            f"{DEAD_PID}\n2026-01-01T00:00:00+00:00\nstale-token",
            encoding="utf-8",
        )
        start = threading.Barrier(3)
        winner_ready = threading.Event()
        both_decided = threading.Event()
        release_winner = threading.Event()
        outcomes: list[bool] = []
        active = 0
        max_active = 0
        state_lock = threading.Lock()

        def contend() -> None:
            nonlocal active, max_active
            start.wait(timeout=2)
            with lockfile.compactor_lock(proj) as acquired:
                with state_lock:
                    outcomes.append(acquired)
                    if acquired:
                        active += 1
                        max_active = max(max_active, active)
                        winner_ready.set()
                    if len(outcomes) == 2:
                        both_decided.set()
                if acquired:
                    release_winner.wait(timeout=2)
                    with state_lock:
                        active -= 1

        contenders = [threading.Thread(target=contend) for _ in range(2)]
        for contender in contenders:
            contender.start()
        start.wait(timeout=2)
        assert winner_ready.wait(timeout=2)
        assert both_decided.wait(timeout=2)
        release_winner.set()
        for contender in contenders:
            contender.join(timeout=2)

        assert not any(contender.is_alive() for contender in contenders)
        assert sorted(outcomes) == [False, True]
        assert max_active == 1
        assert not lock_file.exists()
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_lock_release_does_not_unlink_a_replacement_owner():
    proj = _fresh_project()
    lock_file = lockfile._lock_path(proj)
    replacement_token = "replacement-owner-token"
    try:
        with lockfile.compactor_lock(proj) as acquired:
            assert acquired is True
            lock_file.write_text(
                f"{os.getpid()}\n2026-01-01T00:00:00+00:00\n"
                f"{replacement_token}",
                encoding="utf-8",
            )

        assert lock_file.exists()
        assert lockfile._read_lock_token(lock_file) == replacement_token
    finally:
        try:
            lock_file.unlink()
        except OSError:
            pass
        shutil.rmtree(proj, ignore_errors=True)


def test_failed_lock_body_write_removes_only_its_own_sentinel(monkeypatch):
    proj = _fresh_project()
    lock_file = lockfile._lock_path(proj)
    original_write = lockfile.os.write
    failed = False

    def fail_first_write(fd: int, payload: bytes) -> int:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected lock body write failure")
        return original_write(fd, payload)

    try:
        with monkeypatch.context() as local:
            local.setattr(lockfile.os, "write", fail_first_write)
            with pytest.raises(OSError, match="injected lock body write failure"):
                with lockfile.compactor_lock(proj):
                    pass

        assert failed
        assert not lock_file.exists()
        with lockfile.compactor_lock(proj) as acquired:
            assert acquired is True
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_partial_lock_publication_is_hidden_from_contenders(monkeypatch):
    proj = _fresh_project()
    original_write = lockfile.os.write
    partial_written = threading.Event()
    finish_publication = threading.Event()
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_decided = threading.Event()
    outcomes: dict[str, bool] = {}
    shortened = False

    def short_first_payload(fd: int, payload: bytes) -> int:
        nonlocal shortened
        if not shortened and payload != b"\0":
            shortened = True
            written = original_write(fd, payload[:1])
            partial_written.set()
            assert finish_publication.wait(timeout=2)
            return written
        return original_write(fd, payload)

    def first() -> None:
        with lockfile.compactor_lock(proj) as acquired:
            outcomes["first"] = acquired
            if acquired:
                first_acquired.set()
                release_first.wait(timeout=2)

    def second() -> None:
        with lockfile.compactor_lock(proj) as acquired:
            outcomes["second"] = acquired
            second_decided.set()

    try:
        monkeypatch.setattr(lockfile.os, "write", short_first_payload)
        monkeypatch.setattr(
            lockfile,
            "_pid_alive",
            lambda pid: pid == os.getpid(),
        )
        first_thread = threading.Thread(target=first)
        first_thread.start()
        assert partial_written.wait(timeout=2)

        second_thread = threading.Thread(target=second)
        second_thread.start()
        assert not second_decided.wait(timeout=0.2)

        finish_publication.set()
        assert first_acquired.wait(timeout=2)
        assert second_decided.wait(timeout=2)
        release_first.set()
        first_thread.join(timeout=2)
        second_thread.join(timeout=2)

        assert outcomes == {"first": True, "second": False}
        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
    finally:
        finish_publication.set()
        release_first.set()
        shutil.rmtree(proj, ignore_errors=True)


def test_windows_advisory_lock_reraises_permanent_error(tmp_path, monkeypatch):
    calls = 0

    def fail_permanently(_fd: int, _mode: int, _size: int) -> None:
        nonlocal calls
        calls += 1
        raise OSError(lockfile.errno.EINVAL, "invalid handle")

    fake_msvcrt = types.SimpleNamespace(
        LK_NBLCK=1,
        LK_NBRLCK=2,
        locking=fail_permanently,
    )
    lock_path = tmp_path / "windows.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.write(fd, b"\0")
    try:
        with monkeypatch.context() as local:
            local.setattr(lockfile.sys, "platform", "win32")
            local.setitem(sys.modules, "msvcrt", fake_msvcrt)
            with pytest.raises(OSError, match="invalid handle"):
                lockfile._advisory_lock(fd, exclusive=True)
        assert calls == 1
    finally:
        os.close(fd)


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_advisory_file_rejects_links_without_touching_target(tmp_path, link_kind):
    foreign = tmp_path / "foreign-empty-file"
    foreign.write_bytes(b"")
    lock_path = tmp_path / "advisory.lock"
    try:
        if link_kind == "symlink":
            lock_path.symlink_to(foreign)
        else:
            os.link(foreign, lock_path)
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"{link_kind} creation unavailable: {exc}")

    with pytest.raises(project_config.ProjectConfigError, match="unsafe lock file"):
        lockfile._open_advisory_file(lock_path)
    assert foreign.read_bytes() == b""


def test_advisory_file_detects_path_swap_after_open(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign-empty-file"
    foreign.write_bytes(b"")
    lock_path = tmp_path / "advisory.lock"
    original_open = lockfile.os.open

    def swap_after_open(path, flags, mode=0o777):
        fd = original_open(path, flags, mode)
        if Path(path) == lock_path:
            lock_path.unlink()
            os.link(foreign, lock_path)
        return fd

    monkeypatch.setattr(lockfile.os, "open", swap_after_open)
    with pytest.raises(project_config.ProjectConfigError, match="unsafe lock file"):
        lockfile._open_advisory_file(lock_path)
    assert foreign.read_bytes() == b""


def test_project_access_detects_path_swap_after_lock(tmp_path, monkeypatch):
    project = str(tmp_path / "project")
    Path(project).mkdir()
    selected_kb = str(paths.project_dir(project))
    lock_path = lockfile._access_lock_path(project)
    foreign = tmp_path / "foreign-after-lock"
    foreign.write_bytes(b"")
    original_lock = lockfile._advisory_lock
    swapped = False

    def swap_after_lock(fd, *, exclusive):
        nonlocal swapped
        original_lock(fd, exclusive=exclusive)
        if not swapped:
            swapped = True
            lock_path.unlink()
            os.link(foreign, lock_path)

    monkeypatch.setattr(lockfile, "_advisory_lock", swap_after_lock)
    with pytest.raises(project_config.ProjectConfigError, match="unsafe lock file"):
        with lockfile.project_access_lock(project, kb_dir=selected_kb):
            pass
    assert foreign.read_bytes() == b""


def test_project_access_lock_survives_non_lifo_nested_exit():
    proj = str(Path(_fresh_project()).resolve())
    selected_kb = str(paths.project_dir(proj))
    outer = lockfile.project_access_lock(proj, kb_dir=selected_kb)
    inner = lockfile.project_access_lock(proj, kb_dir=selected_kb)
    transition_entered = threading.Event()
    try:
        outer.__enter__()
        inner.__enter__()
        outer.__exit__(None, None, None)

        def transition() -> None:
            with lockfile.project_access_lock(proj, exclusive=True):
                transition_entered.set()

        waiter = threading.Thread(target=transition)
        waiter.start()
        assert not transition_entered.wait(timeout=0.2)
        inner.__exit__(None, None, None)
        assert transition_entered.wait(timeout=2)
        waiter.join(timeout=2)
        assert not waiter.is_alive()
    finally:
        if not transition_entered.is_set():
            try:
                inner.__exit__(None, None, None)
            except Exception:
                pass
        shutil.rmtree(proj, ignore_errors=True)


def test_project_access_lock_preserves_body_exception():
    proj = str(Path(_fresh_project()).resolve())
    selected_kb = str(paths.project_dir(proj))

    class SentinelError(RuntimeError):
        pass

    try:
        with pytest.raises(SentinelError, match="original body failure"):
            with lockfile.project_access_lock(proj, kb_dir=selected_kb):
                raise SentinelError("original body failure")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_writer_lock_is_reentrant_for_same_thread_and_project():
    proj = _fresh_project()
    try:
        lock_path = lockfile._lock_path(proj)
        with lockfile.writer_lock(proj, timeout_s=0.2, poll_interval_s=0.01):
            _assert(lock_path.exists(), "outer writer lock was not acquired")
            before = lock_path.read_text(encoding="utf-8")
            with lockfile.writer_lock(proj, timeout_s=0.0, poll_interval_s=0.01):
                _assert(lock_path.exists(), "nested writer lock released the outer lock")
                _assert(
                    lock_path.read_text(encoding="utf-8") == before,
                    "nested writer lock replaced the outer lock file",
                )
            _assert(lock_path.exists(), "nested exit released the outer writer lock")
        _assert(not lock_path.exists(), "outer exit did not release the writer lock")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_writer_lock_reenters_same_thread_compactor_ownership():
    proj = _fresh_project()
    try:
        lock_path = lockfile._lock_path(proj)
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired, "outer compactor lock was not acquired")
            before = lock_path.read_text(encoding="utf-8")
            with lockfile.writer_lock(proj, timeout_s=0.0, poll_interval_s=0.01):
                _assert(lock_path.exists(), "nested writer released compactor lock")
                _assert(
                    lock_path.read_text(encoding="utf-8") == before,
                    "nested writer replaced the compactor lock file",
                )
            _assert(lock_path.exists(), "nested writer exit released compactor lock")
        _assert(not lock_path.exists(), "compactor exit did not release shared lock")
    finally:
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
    test_writer_lock_is_reentrant_for_same_thread_and_project()
    test_writer_lock_reenters_same_thread_compactor_ownership()
    test_pid_alive_for_self_and_dead_pid()
    test_wait_releases_when_lock_disappears_mid_poll()
    print("\nAll lockfile tests pass.")
