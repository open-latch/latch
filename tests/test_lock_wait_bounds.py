"""Bounded advisory-lock waits and leaked-connection lease release.

A hung or leaked lock holder must surface as an actionable error, never as a
silent indefinite hang of ``latch``/``unlatch``; a garbage-collected agent
connection must not pin its scope-access lease until process exit.
"""
from __future__ import annotations

import gc
import os
import sqlite3

import pytest

import db
import lockfile


def test_advisory_lock_wait_is_bounded_with_actionable_error(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "0.3")
    lock_file = tmp_path / "scope-access.lock"
    holder = lockfile._open_advisory_file(lock_file)
    waiter = lockfile._open_advisory_file(lock_file)
    try:
        lockfile._advisory_lock(holder, exclusive=True, path=lock_file)
        with pytest.raises(lockfile.LockWaitTimeout) as excinfo:
            lockfile._advisory_lock(waiter, exclusive=True, path=lock_file)
        message = str(excinfo.value)
        assert str(lock_file) in message
        assert lockfile.LOCK_TIMEOUT_ENV in message
    finally:
        lockfile._advisory_unlock(holder)
        os.close(holder)
        os.close(waiter)


def test_shared_waiters_are_bounded_behind_an_exclusive_holder(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "0.3")
    lock_file = tmp_path / "scope-access.lock"
    holder = lockfile._open_advisory_file(lock_file)
    waiter = lockfile._open_advisory_file(lock_file)
    try:
        lockfile._advisory_lock(holder, exclusive=True, path=lock_file)
        with pytest.raises(lockfile.LockWaitTimeout):
            lockfile._advisory_lock(waiter, exclusive=False, path=lock_file)
    finally:
        lockfile._advisory_unlock(holder)
        os.close(holder)
        os.close(waiter)


def test_invalid_timeout_override_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "not-a-number")
    assert lockfile._lock_timeout_s() == lockfile.DEFAULT_LOCK_TIMEOUT_S
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "-5")
    assert lockfile._lock_timeout_s() == lockfile.DEFAULT_LOCK_TIMEOUT_S
    # A non-finite override would reintroduce the unbounded wait.
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "inf")
    assert lockfile._lock_timeout_s() == lockfile.DEFAULT_LOCK_TIMEOUT_S
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "nan")
    assert lockfile._lock_timeout_s() == lockfile.DEFAULT_LOCK_TIMEOUT_S


def test_release_owned_lock_surfaces_cleanup_mutex_timeout(
    tmp_path, monkeypatch,
):
    """A timed-out sentinel release must raise, not silently leak the
    live-PID sentinel (which would wedge later writers/compactors)."""
    monkeypatch.setenv(lockfile.LOCK_TIMEOUT_ENV, "0.3")
    lock_file = tmp_path / "compactor.lock"
    lock_file.write_text("pid\nacquired\ntoken\n", encoding="utf-8")
    mutex_file = lock_file.with_name(
        lock_file.name + lockfile.CLEANUP_LOCK_SUFFIX
    )
    holder = lockfile._open_advisory_file(mutex_file)
    try:
        lockfile._advisory_lock(holder, exclusive=True, path=mutex_file)
        with pytest.raises(lockfile.LockWaitTimeout):
            lockfile._release_owned_lock(lock_file, "token")
        assert lock_file.exists()
    finally:
        lockfile._advisory_unlock(holder)
        os.close(holder)


class _FakeLease:
    def __init__(self) -> None:
        self.exits = 0

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.exits += 1


def test_leaked_connection_releases_lease_on_gc():
    conn = sqlite3.connect(":memory:", factory=db._Connection)
    lease = _FakeLease()
    db._attach_connection_lease(conn, lease)

    del conn
    gc.collect()

    assert lease.exits == 1


def test_closed_connection_releases_lease_exactly_once():
    conn = sqlite3.connect(":memory:", factory=db._Connection)
    lease = _FakeLease()
    db._attach_connection_lease(conn, lease)

    conn.close()
    assert lease.exits == 1

    del conn
    gc.collect()
    assert lease.exits == 1
