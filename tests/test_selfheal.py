"""Unit tests for selfheal.py — self-triggering maintenance (KB id=1173).

Pure-Python, no live Claude / no scheduler / no real fork — runs on any OS.

Covers:
- cadence math (_due): never-stamped / elapsed / within-interval.
- state round-trip: missing file = all-due; atomic save; corrupt JSON tolerated.
- maybe_trigger guards: kill switch, reentrancy env, not-due => no spawn.
- single-flight: run_selfheal skips when the compactor lock is held.
- op stamping: only ops that ran advance their stamp; a raising op does not.
- backup + prune: snapshot created, newest BACKUPS_KEPT retained.
- spawn argv + per-OS detach flags.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import lockfile  # noqa: E402
import paths  # noqa: E402
import selfheal  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_project() -> str:
    return tempfile.mkdtemp(prefix="kb_selfheal_test_")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------- cadence math ----------------

def test_due_when_never_stamped():
    now = datetime.now(timezone.utc)
    _assert(selfheal._due({}, "last_heal_at", 48, now) is True,
            "missing stamp must be due")
    _assert(selfheal._due({"last_heal_at": "garbage"}, "last_heal_at", 48, now) is True,
            "unparseable stamp must be due")
    print("PASS due_when_never_stamped")


def test_due_when_interval_elapsed():
    now = datetime.now(timezone.utc)
    old = _iso(now - timedelta(hours=49))
    recent = _iso(now - timedelta(hours=47))
    _assert(selfheal._due({"last_heal_at": old}, "last_heal_at", 48, now) is True,
            "49h > 48h interval should be due")
    _assert(selfheal._due({"last_heal_at": recent}, "last_heal_at", 48, now) is False,
            "47h < 48h interval should NOT be due")
    print("PASS due_when_interval_elapsed")


def test_naive_timestamp_treated_as_utc():
    now = datetime.now(timezone.utc)
    naive = (now - timedelta(hours=49)).replace(tzinfo=None).isoformat()
    # Must not raise on aware/naive subtraction and should be due.
    _assert(selfheal._due({"last_heal_at": naive}, "last_heal_at", 48, now) is True,
            "naive stamp should parse as UTC and compute due")
    print("PASS naive_timestamp_treated_as_utc")


def test_any_due():
    now = datetime.now(timezone.utc)
    fresh = {
        "last_backup_at": _iso(now - timedelta(hours=1)),
        "last_heal_at": _iso(now - timedelta(hours=1)),
        "last_weekly_at": _iso(now - timedelta(hours=1)),
    }
    _assert(selfheal._any_due(fresh, now) is False, "all-fresh => nothing due")
    stale_backup = dict(fresh, last_backup_at=_iso(now - timedelta(hours=13)))
    _assert(selfheal._any_due(stale_backup, now) is True, "stale backup => due")
    print("PASS any_due")


# ---------------- state round-trip ----------------

def test_state_missing_is_all_due():
    proj = _fresh_project()
    try:
        _assert(selfheal._load_state(proj) == {}, "missing state => {}")
        _assert(selfheal._any_due({}, datetime.now(timezone.utc)) is True,
                "empty state => all due")
        print("PASS state_missing_is_all_due")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_state_roundtrip_and_corrupt_tolerated():
    proj = _fresh_project()
    try:
        st = {"last_heal_at": _iso(datetime.now(timezone.utc))}
        selfheal._save_state(proj, st)
        _assert(selfheal._load_state(proj) == st, "round-trip mismatch")
        # No leftover temp file.
        _assert(not selfheal._state_path(proj).with_suffix(".json.tmp").exists(),
                "temp file should have been renamed away")
        # Corrupt the file => tolerated as {}.
        selfheal._state_path(proj).write_text("{not json", encoding="utf-8")
        _assert(selfheal._load_state(proj) == {}, "corrupt JSON should load as {}")
        print("PASS state_roundtrip_and_corrupt_tolerated")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


# ---------------- maybe_trigger guards ----------------

def test_maybe_trigger_kill_switch():
    proj = _fresh_project()
    calls = []
    orig_spawn = selfheal.spawn_detached
    orig_disabled = paths.is_disabled
    try:
        selfheal.spawn_detached = lambda p: calls.append(p)
        paths.is_disabled = lambda: True
        selfheal.maybe_trigger(proj)
        _assert(calls == [], "kill switch must prevent spawn")
        print("PASS maybe_trigger_kill_switch")
    finally:
        selfheal.spawn_detached = orig_spawn
        paths.is_disabled = orig_disabled
        shutil.rmtree(proj, ignore_errors=True)


def test_maybe_trigger_reentrancy_guard():
    proj = _fresh_project()
    calls = []
    orig_spawn = selfheal.spawn_detached
    try:
        selfheal.spawn_detached = lambda p: calls.append(p)
        os.environ[selfheal.IN_MAINTENANCE_ENV] = "1"
        selfheal.maybe_trigger(proj)
        _assert(calls == [], "must not trigger from inside a maintenance child")
        print("PASS maybe_trigger_reentrancy_guard")
    finally:
        selfheal.spawn_detached = orig_spawn
        os.environ.pop(selfheal.IN_MAINTENANCE_ENV, None)
        shutil.rmtree(proj, ignore_errors=True)


def test_maybe_trigger_not_due_no_spawn():
    proj = _fresh_project()
    calls = []
    orig_spawn = selfheal.spawn_detached
    try:
        now = datetime.now(timezone.utc)
        selfheal._save_state(proj, {
            "last_backup_at": _iso(now),
            "last_heal_at": _iso(now),
            "last_weekly_at": _iso(now),
        })
        selfheal.spawn_detached = lambda p: calls.append(p)
        selfheal.maybe_trigger(proj)
        _assert(calls == [], "nothing due => no spawn")
        print("PASS maybe_trigger_not_due_no_spawn")
    finally:
        selfheal.spawn_detached = orig_spawn
        shutil.rmtree(proj, ignore_errors=True)


def test_maybe_trigger_due_spawns():
    proj = _fresh_project()
    calls = []
    orig_spawn = selfheal.spawn_detached
    try:
        # No state file => all due.
        selfheal.spawn_detached = lambda p: calls.append(p)
        selfheal.maybe_trigger(proj)
        _assert(calls == [proj], f"due => exactly one spawn, got {calls}")
        print("PASS maybe_trigger_due_spawns")
    finally:
        selfheal.spawn_detached = orig_spawn
        shutil.rmtree(proj, ignore_errors=True)


# ---------------- single-flight ----------------

def test_run_selfheal_skips_when_locked():
    proj = _fresh_project()
    heal_calls = []
    orig_heal = selfheal.maintenance.run_nightly_heal
    try:
        selfheal.maintenance.run_nightly_heal = lambda p, **k: heal_calls.append(p)
        # Hold the lock for the duration of the run_selfheal call.
        with lockfile.compactor_lock(proj) as acquired:
            _assert(acquired is True, "test setup: should hold the lock")
            result = selfheal.run_selfheal(proj)
        _assert(result.get("reason") == "locked", f"expected locked skip, got {result}")
        _assert(heal_calls == [], "no heal should run while locked")
        print("PASS run_selfheal_skips_when_locked")
    finally:
        selfheal.maintenance.run_nightly_heal = orig_heal
        shutil.rmtree(proj, ignore_errors=True)


# ---------------- op stamping ----------------

def _seed_db(proj):
    """Create a minimal kb.db so _backup_db has something to copy."""
    import db
    db.connect(proj).close()


def test_only_run_ops_advance_stamps():
    proj = _fresh_project()
    orig_heal = selfheal.maintenance.run_nightly_heal
    orig_weekly = selfheal.maintenance.run_weekly_maintenance
    orig_tree = selfheal.maintenance.run_tree_rebuild
    try:
        _seed_db(proj)
        # heal due, weekly NOT due.
        now = datetime.now(timezone.utc)
        selfheal._save_state(proj, {
            "last_weekly_at": _iso(now - timedelta(hours=1)),  # fresh => not due
        })
        selfheal.maintenance.run_nightly_heal = lambda p, **k: None
        selfheal.maintenance.run_weekly_maintenance = lambda p, **k: (_ for _ in ()).throw(
            AssertionError("weekly should NOT run"))
        selfheal.maintenance.run_tree_rebuild = lambda p, **k: None

        result = selfheal.run_selfheal(proj)
        _assert(result["ok"] is True, result)
        _assert("heal" in result["ran"], f"heal should have run: {result}")
        _assert("weekly" not in result["ran"], f"weekly should not run: {result}")
        st = selfheal._load_state(proj)
        _assert("last_heal_at" in st, "heal stamp should advance")
        _assert("last_backup_at" in st, "backup should run before heal mutates")
        print("PASS only_run_ops_advance_stamps")
    finally:
        selfheal.maintenance.run_nightly_heal = orig_heal
        selfheal.maintenance.run_weekly_maintenance = orig_weekly
        selfheal.maintenance.run_tree_rebuild = orig_tree
        shutil.rmtree(proj, ignore_errors=True)


def test_raising_op_does_not_advance_its_stamp():
    proj = _fresh_project()
    orig_heal = selfheal.maintenance.run_nightly_heal
    try:
        _seed_db(proj)
        selfheal.maintenance.run_nightly_heal = lambda p, **k: (_ for _ in ()).throw(
            RuntimeError("boom"))
        result = selfheal.run_selfheal(proj)
        _assert(result["ok"] is True, "a failing op must not crash the pass")
        _assert("heal" not in result["ran"], "failed heal must not be reported as ran")
        st = selfheal._load_state(proj)
        _assert("last_heal_at" not in st, "failed heal must not advance its stamp")
        print("PASS raising_op_does_not_advance_its_stamp")
    finally:
        selfheal.maintenance.run_nightly_heal = orig_heal
        shutil.rmtree(proj, ignore_errors=True)


# ---------------- backup + prune ----------------

def test_backup_creates_and_prunes():
    proj = _fresh_project()
    try:
        _seed_db(proj)
        # Make BACKUPS_KEPT + 2 backups, spaced so mtimes differ.
        for _ in range(selfheal.BACKUPS_KEPT + 2):
            _assert(selfheal._backup_db(proj) is True, "backup should succeed")
            time.sleep(1.05)  # second-resolution timestamp in filename
        selfheal._prune_backups(proj)
        baks = list(paths.project_dir(proj).glob("kb.db.bak.*"))
        _assert(len(baks) == selfheal.BACKUPS_KEPT,
                f"expected {selfheal.BACKUPS_KEPT} backups after prune, got {len(baks)}")
        print(f"PASS backup_creates_and_prunes (kept {len(baks)})")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def test_backup_noop_without_db():
    proj = _fresh_project()
    try:
        # No kb.db created.
        _assert(selfheal._backup_db(proj) is False, "backup must no-op without kb.db")
        print("PASS backup_noop_without_db")
    finally:
        shutil.rmtree(proj, ignore_errors=True)


# ---------------- spawn argv + flags ----------------

def test_spawn_builds_correct_command():
    proj = _fresh_project()
    captured = {}
    orig_popen = selfheal.subprocess.Popen

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    try:
        selfheal.subprocess.Popen = _FakePopen
        selfheal.spawn_detached(proj)
        args = captured["args"]
        kw = captured["kwargs"]
        _assert(args[0] == sys.executable, f"argv[0] should be python: {args}")
        _assert(args[1].endswith("selfheal.py"), f"argv[1] should be selfheal.py: {args}")
        _assert(args[2] == proj, f"argv[2] should be project path: {args}")
        _assert(kw["env"].get(selfheal.IN_MAINTENANCE_ENV) == "1",
                "child env must carry the reentrancy guard")
        if sys.platform == "win32":
            _assert("creationflags" in kw and kw["creationflags"] == (0x8 | 0x200),
                    f"win detach flags wrong: {kw.get('creationflags')}")
        else:
            _assert(kw.get("start_new_session") is True, "posix detach flag missing")
        print("PASS spawn_builds_correct_command")
    finally:
        selfheal.subprocess.Popen = orig_popen
        shutil.rmtree(proj, ignore_errors=True)


if __name__ == "__main__":
    test_due_when_never_stamped()
    test_due_when_interval_elapsed()
    test_naive_timestamp_treated_as_utc()
    test_any_due()
    test_state_missing_is_all_due()
    test_state_roundtrip_and_corrupt_tolerated()
    test_maybe_trigger_kill_switch()
    test_maybe_trigger_reentrancy_guard()
    test_maybe_trigger_not_due_no_spawn()
    test_maybe_trigger_due_spawns()
    test_run_selfheal_skips_when_locked()
    test_only_run_ops_advance_stamps()
    test_raising_op_does_not_advance_its_stamp()
    test_backup_creates_and_prunes()
    test_backup_noop_without_db()
    test_spawn_builds_correct_command()
    print("\nAll selfheal tests pass.")
