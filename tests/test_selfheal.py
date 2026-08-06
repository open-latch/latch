"""Unit tests for selfheal.py — self-triggering maintenance (KB id=1173).

Pure-Python, no live Claude / no scheduler / no real fork — runs on any OS.

Covers:
- cadence math (_due): never-stamped / elapsed / within-interval.
- state round-trip: missing file = all-due; atomic save; corrupt JSON tolerated.
- maybe_trigger guards: kill switch, reentrancy env, not-due => no spawn.
- single-flight: run_selfheal skips when the compactor lock is held.
- op stamping: only ops that ran advance their stamp; a raising op does not.
- backup + prune: protected external snapshots survive count-based pruning.
- spawn argv + per-OS detach flags.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import lockfile  # noqa: E402
import mcp_runtime  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402
import selfheal  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_project() -> str:
    return tempfile.mkdtemp(prefix="kb_selfheal_test_")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _reset_trigger() -> None:
    selfheal._TRIGGER_CHECKED.clear()
    selfheal._TRIGGER_FAILURE_SIGNATURE.clear()


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
        "last_workstream_shadow_at": _iso(now - timedelta(hours=1)),
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


def test_operational_log_is_project_vault_local():
    project_a = _fresh_project()
    project_b = _fresh_project()
    try:
        test_root = paths.validated_test_root()
        _assert(test_root is not None, "pytest must provide an authenticated root")
        vault_a = test_root / "vaults" / f"selfheal-log-{Path(project_a).name}"
        vault_b = test_root / "vaults" / f"selfheal-log-{Path(project_b).name}"
        vault_a.mkdir(parents=True)
        vault_b.mkdir(parents=True)
        project_config.create_scope(
            project_a,
            policy=project_config.POLICY_PRIVATE,
        )
        project_config.authorize_scope(project_a, kb_dir=vault_a)
        project_config.create_scope(
            project_b,
            policy=project_config.POLICY_PRIVATE,
        )
        project_config.authorize_scope(project_b, kb_dir=vault_b)
        selfheal._log(project_a, "only-a")
        selfheal._log(project_b, "only-b")
        log_a = paths.project_dir(project_a) / "maintenance.log"
        log_b = paths.project_dir(project_b) / "maintenance.log"
        body_a = log_a.read_text(encoding="utf-8")
        body_b = log_b.read_text(encoding="utf-8")
        _assert("only-a" in body_a and "only-b" not in body_a, body_a)
        _assert("only-b" in body_b and "only-a" not in body_b, body_b)
    finally:
        shutil.rmtree(project_a, ignore_errors=True)
        shutil.rmtree(project_b, ignore_errors=True)


# ---------------- maybe_trigger guards ----------------

def test_maybe_trigger_kill_switch():
    proj = _fresh_project()
    calls = []
    orig_spawn = selfheal.spawn_detached
    orig_disabled = paths.is_disabled
    try:
        _reset_trigger()
        selfheal.spawn_detached = lambda p, **_kwargs: calls.append(p)
        paths.is_disabled = lambda *_args: True
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
        _reset_trigger()
        selfheal.spawn_detached = lambda p, **_kwargs: calls.append(p)
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
        _reset_trigger()
        now = datetime.now(timezone.utc)
        selfheal._save_state(proj, {
            "last_backup_at": _iso(now),
            "last_heal_at": _iso(now),
            "last_weekly_at": _iso(now),
            "last_workstream_shadow_at": _iso(now),
        })
        selfheal.spawn_detached = lambda p, **_kwargs: calls.append(p)
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
        _reset_trigger()
        # No state file => all due.
        selfheal.spawn_detached = lambda p, **_kwargs: calls.append(p)
        selfheal.maybe_trigger(proj)
        _assert(calls == [proj], f"due => exactly one spawn, got {calls}")
        print("PASS maybe_trigger_due_spawns")
    finally:
        selfheal.spawn_detached = orig_spawn
        shutil.rmtree(proj, ignore_errors=True)


def test_maybe_trigger_tracks_each_project_target_independently(monkeypatch):
    project_a = _fresh_project()
    project_b = _fresh_project()
    calls: list[str] = []
    try:
        _reset_trigger()
        monkeypatch.setattr(selfheal, "spawn_detached", calls.append)
        selfheal.maybe_trigger(project_a)
        selfheal.maybe_trigger(project_b)
        _assert(calls == [project_a, project_b], calls)
        _assert(len(selfheal._TRIGGER_CHECKED) == 2, selfheal._TRIGGER_CHECKED)
    finally:
        _reset_trigger()
        shutil.rmtree(project_a, ignore_errors=True)
        shutil.rmtree(project_b, ignore_errors=True)


def test_stale_expected_target_stops_before_target_sidecar_reads(monkeypatch):
    project = _fresh_project()
    try:
        _reset_trigger()
        monkeypatch.setattr(
            selfheal,
            "_trigger_blocked",
            lambda *_args: (_ for _ in ()).throw(
                AssertionError("stale trigger inspected replacement target")
            ),
        )
        selfheal.maybe_trigger(
            project,
            expected_binding_revision="stale-session",
            expected_kb_dir=None,
        )
        _assert(not selfheal._TRIGGER_CHECKED, selfheal._TRIGGER_CHECKED)
    finally:
        _reset_trigger()
        shutil.rmtree(project, ignore_errors=True)


def test_maybe_trigger_retries_only_after_vault_policy_changes(
    monkeypatch,
):
    proj = _fresh_project()
    calls: list[str] = []
    policy = (
        paths.project_dir(proj)
        / paths.VAULT_RUNTIME_SETTINGS_FILENAME
    )

    def fail_then_succeed(project_path):
        calls.append(project_path)
        if len(calls) == 1:
            raise ValueError("missing vault runner")

    try:
        _reset_trigger()
        monkeypatch.setattr(selfheal, "spawn_detached", fail_then_succeed)
        selfheal.maybe_trigger(proj)
        selfheal.maybe_trigger(proj)
        _assert(calls == [proj], f"unchanged broken policy retried: {calls}")

        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text("{}\n", encoding="utf-8")
        selfheal.maybe_trigger(proj)
        _assert(calls == [proj, proj], f"repaired policy did not retry: {calls}")
        _assert(bool(selfheal._TRIGGER_CHECKED), "successful retry not consumed")
    finally:
        _reset_trigger()
        shutil.rmtree(proj, ignore_errors=True)


def test_ineligible_connections_do_not_consume_trigger_check():
    proj = _fresh_project()
    calls = []
    orig_spawn = selfheal.spawn_detached
    base = {
        "connection_id": "guarded",
        "project_cwd": proj,
        "session_id": None,
        "session_source": "test",
        "proxy_pid": 123,
        "proxy_started_at": "now",
        "runtime_key": "test",
        "gate_backend": "codex",
        "maintenance_backend": "codex",
    }
    try:
        _reset_trigger()
        selfheal.spawn_detached = lambda p, **kwargs: calls.append((p, kwargs))
        for guarded in (
            {"in_compact": True},
            {"disabled": True},
            {"unlatched": True},
            {"in_maintenance": True},
        ):
            context = mcp_runtime.ConnectionContext(**base, **guarded)
            with mcp_runtime.bind_connection(context):
                selfheal.maybe_trigger(proj)
            _assert(
                not selfheal._TRIGGER_CHECKED,
                f"{guarded} consumed the maintenance trigger",
            )

        eligible = mcp_runtime.ConnectionContext(**base)
        with mcp_runtime.bind_connection(eligible):
            selfheal.maybe_trigger(proj)
            selfheal.maybe_trigger(proj)
        _assert(
            calls == [(proj, {})],
            f"eligible connection should trigger exactly once: {calls}",
        )
    finally:
        selfheal.spawn_detached = orig_spawn
        _reset_trigger()
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


def test_heal_due_forces_backup_when_backup_timer_is_fresh():
    proj = _fresh_project()
    calls = []
    orig_backup = selfheal._backup_db
    orig_prune = selfheal._prune_backups
    orig_heal = selfheal.maintenance.run_nightly_heal
    orig_weekly = selfheal.maintenance.run_weekly_maintenance
    orig_tree = selfheal.maintenance.run_tree_rebuild
    try:
        _seed_db(proj)
        now = datetime.now(timezone.utc)
        selfheal._save_state(proj, {
            "last_backup_at": _iso(now - timedelta(hours=1)),  # fresh 6h cadence
            "last_heal_at": _iso(now - timedelta(hours=49)),  # due 48h heal
            "last_weekly_at": _iso(now - timedelta(hours=1)),
            "last_workstream_shadow_at": _iso(now - timedelta(hours=1)),
        })
        selfheal._backup_db = lambda p: calls.append(("backup", p)) or True
        selfheal._prune_backups = lambda p: calls.append(("prune", p))
        selfheal.maintenance.run_nightly_heal = (
            lambda p, **k: calls.append(("heal", p))
        )
        selfheal.maintenance.run_weekly_maintenance = (
            lambda p, **k: (_ for _ in ()).throw(
                AssertionError("weekly should NOT run")
            )
        )
        selfheal.maintenance.run_tree_rebuild = lambda p, **k: None

        result = selfheal.run_selfheal(proj)

        _assert(result["ran"] == ["backup", "heal"], result)
        _assert(calls == [("backup", proj), ("prune", proj), ("heal", proj)], calls)
        state = selfheal._load_state(proj)
        _assert(
            selfheal._parse(state["last_backup_at"]) > now,
            "48h heal must refresh the backup stamp even when the 6h timer is fresh",
        )
        _assert(
            selfheal._parse(state["last_heal_at"]) > now,
            "successful heal should refresh the heal stamp",
        )
        print("PASS heal_due_forces_backup_when_backup_timer_is_fresh")
    finally:
        selfheal._backup_db = orig_backup
        selfheal._prune_backups = orig_prune
        selfheal.maintenance.run_nightly_heal = orig_heal
        selfheal.maintenance.run_weekly_maintenance = orig_weekly
        selfheal.maintenance.run_tree_rebuild = orig_tree
        shutil.rmtree(proj, ignore_errors=True)


def test_failed_forced_backup_blocks_due_mutations_without_advancing_stamps():
    proj = _fresh_project()
    calls = []
    orig_backup = selfheal._backup_db
    orig_prune = selfheal._prune_backups
    orig_heal = selfheal.maintenance.run_nightly_heal
    orig_weekly = selfheal.maintenance.run_weekly_maintenance
    orig_tree = selfheal.maintenance.run_tree_rebuild
    try:
        _seed_db(proj)
        now = datetime.now(timezone.utc)
        initial = {
            "last_backup_at": _iso(now - timedelta(hours=1)),
            "last_heal_at": _iso(now - timedelta(hours=49)),
            "last_weekly_at": _iso(now - timedelta(hours=169)),
            "last_workstream_shadow_at": _iso(now - timedelta(hours=25)),
        }
        selfheal._save_state(proj, initial)

        def fail_backup(path):
            calls.append(("backup", path))
            raise selfheal.BackupCreationError("simulated snapshot failure")

        selfheal._backup_db = fail_backup
        selfheal._prune_backups = lambda p: calls.append(("prune", p))
        selfheal.maintenance.run_nightly_heal = lambda p, **k: calls.append(("heal", p))
        selfheal.maintenance.run_weekly_maintenance = (
            lambda p, **k: calls.append(("weekly", p))
        )
        selfheal.maintenance.run_tree_rebuild = lambda p, **k: calls.append(("tree", p))

        result = selfheal.run_selfheal(proj)

        _assert(result == {
            "ok": False,
            "reason": "backup_failed",
            "ran": [],
            "blocked": ["heal", "weekly", "workstream_shadow"],
        }, result)
        _assert(calls == [("backup", proj)], calls)
        _assert(selfheal._load_state(proj) == initial, "failed backup must advance no stamps")
        print("PASS failed_forced_backup_blocks_heal_and_weekly_without_advancing_stamps")
    finally:
        selfheal._backup_db = orig_backup
        selfheal._prune_backups = orig_prune
        selfheal.maintenance.run_nightly_heal = orig_heal
        selfheal.maintenance.run_weekly_maintenance = orig_weekly
        selfheal.maintenance.run_tree_rebuild = orig_tree
        shutil.rmtree(proj, ignore_errors=True)


def test_backup_snapshot_exception_is_not_downgraded_to_no_database():
    proj = _fresh_project()
    original = selfheal.vault_backup.create_snapshot
    try:
        _seed_db(proj)
        selfheal.vault_backup.create_snapshot = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("storage unavailable"))
        )
        try:
            selfheal._backup_db(proj)
        except selfheal.BackupCreationError:
            pass
        else:
            raise AssertionError("snapshot failure must raise BackupCreationError")
        print("PASS backup_snapshot_exception_is_not_downgraded_to_no_database")
    finally:
        selfheal.vault_backup.create_snapshot = original
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


def test_workstream_governance_runs_after_shared_lock_release():
    proj = _fresh_project()
    orig_shadow = selfheal.maintenance.run_workstream_shadow
    orig_governed = selfheal.maintenance.run_workstream_governed
    calls = []
    try:
        _seed_db(proj)
        now = datetime.now(timezone.utc)
        selfheal._save_state(proj, {
            "last_backup_at": _iso(now),
            "last_heal_at": _iso(now),
            "last_weekly_at": _iso(now),
            "last_workstream_shadow_at": _iso(now - timedelta(hours=25)),
        })

        def _shadow(_project, *, already_locked=False):
            _assert(already_locked is True, "selfheal must declare lock ownership")
            calls.append(("shadow", lockfile._lock_path(proj).exists()))
            return {"ok": True}

        def _governed(_project):
            calls.append(("governed", lockfile._lock_path(proj).exists()))
            return {
                "ok": True,
                "applied": [],
                "failed": [],
                "suggestion_count": 0,
            }

        selfheal.maintenance.run_workstream_shadow = _shadow
        selfheal.maintenance.run_workstream_governed = _governed
        result = selfheal.run_selfheal(proj)

        _assert(calls == [("shadow", True), ("governed", False)], calls)
        _assert("workstream_shadow" in result["ran"], result)
        _assert("workstream_automation" in result["ran"], result)
        print("PASS workstream_governance_runs_after_shared_lock_release")
    finally:
        selfheal.maintenance.run_workstream_shadow = orig_shadow
        selfheal.maintenance.run_workstream_governed = orig_governed
        shutil.rmtree(proj, ignore_errors=True)


# ---------------- backup + prune ----------------

def test_backup_creates_and_prunes():
    proj = _fresh_project()
    try:
        _seed_db(proj)
        backup_root = paths.validated_test_root() / "backups"
        before = set(backup_root.rglob("*.json")) if backup_root.exists() else set()
        for _ in range(5):
            _assert(selfheal._backup_db(proj) is True, "backup should succeed")
        selfheal._prune_backups(proj)
        manifests = set(backup_root.rglob("*.json")) - before
        _assert(len(manifests) == 5, f"all protected snapshots must survive: {manifests}")
        _assert(not list(paths.project_dir(proj).glob("kb.db.bak.*")),
                "legacy in-vault backups must not be created")
        print(f"PASS backup_creates_and_prunes (protected {len(manifests)})")
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
    orig_runner = selfheal.paths.configured_maintenance_runner

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    try:
        selfheal.subprocess.Popen = _FakePopen
        selfheal.paths.configured_maintenance_runner = lambda **_kwargs: (
            "codex",
            sys.executable,
            str(Path(proj).parent),
            os.pathsep.join(("/vault/bin", "/usr/bin")),
        )
        context = mcp_runtime.ConnectionContext(
            connection_id="codex-maintenance",
            project_cwd=proj,
            session_id=None,
            session_source="test",
            proxy_pid=123,
            proxy_started_at="now",
            runtime_key="test",
            gate_backend="codex",
            maintenance_backend="codex",
        )
        private = mcp_runtime.validate_child_environment({
            "PATH": os.environ.get("PATH", ""),
            "OPENAI_API_KEY": "codex-secret",
            "ANTHROPIC_API_KEY": "claude-secret",
        })
        with mcp_runtime.bind_connection(
            context, child_environment=private
        ):
            selfheal.spawn_detached(proj)
        args = captured["args"]
        kw = captured["kwargs"]
        _assert(args[0] == sys.executable, f"argv[0] should be python: {args}")
        _assert(args[1].endswith("selfheal.py"), f"argv[1] should be selfheal.py: {args}")
        _assert(args[2] == proj, f"argv[2] should be project path: {args}")
        _assert(
            args[3] == project_config.resolve(proj).revision,
            f"missing binding snapshot: {args}",
        )
        _assert(args[4] == str(paths.project_dir(proj)), f"wrong KB snapshot: {args}")
        _assert(kw["env"].get(selfheal.IN_MAINTENANCE_ENV) == "1",
                "child env must carry the reentrancy guard")
        _assert(kw["env"].get("LATCH_MAINTENANCE_BACKEND") == "codex",
                "child env must carry the vault maintenance backend")
        _assert(kw["env"].get("CODEX_BIN") == sys.executable,
                "child env must carry the vault maintenance executable")
        _assert(
            kw["env"].get("PATH")
            == os.pathsep.join(("/vault/bin", "/usr/bin")),
            "child env inherited the daemon owner's PATH",
        )
        _assert("OPENAI_API_KEY" not in kw["env"],
                "connection credential crossed the autonomous boundary")
        _assert("ANTHROPIC_API_KEY" not in kw["env"],
                "connection credential crossed the autonomous boundary")
        if sys.platform == "win32":
            _assert("creationflags" in kw and kw["creationflags"] == (0x8 | 0x200),
                    f"win detach flags wrong: {kw.get('creationflags')}")
        else:
            _assert(kw.get("start_new_session") is True, "posix detach flag missing")
        print("PASS spawn_builds_correct_command")
    finally:
        selfheal.subprocess.Popen = orig_popen
        selfheal.paths.configured_maintenance_runner = orig_runner
        shutil.rmtree(proj, ignore_errors=True)


def test_detached_child_refuses_project_repin_before_touching_new_kb(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="kb_selfheal_repin_"))
    project = root / "project"
    project.mkdir()
    (project / ".git").mkdir()
    kb_a = paths.validated_test_root() / "vaults" / f"selfheal-a-{root.name}"
    kb_b = paths.validated_test_root() / "vaults" / f"selfheal-b-{root.name}"
    kb_a.mkdir(parents=True)
    kb_b.mkdir(parents=True)
    captured = {}

    class _FakePopen:
        def __init__(self, args, **_kwargs):
            captured["args"] = list(args)

    try:
        project_config.mark_kb_target(kb_a)
        project_config.mark_kb_target(kb_b)
        project_config.write_binding(
            project, mode=project_config.MODE_LATCHED, kb_dir=kb_a,
        )
        monkeypatch.setattr(selfheal.subprocess, "Popen", _FakePopen)
        selfheal.spawn_detached(str(project))
        args = captured["args"]
        project_config.repin_private_scope(project, kb_b)
        before = {path.name for path in kb_b.iterdir()}

        result = selfheal.run_selfheal(
            args[2],
            expected_binding_revision=args[3],
            expected_kb_dir=args[4],
        )

        _assert(result == {"ok": False, "reason": "target_changed"}, result)
        _assert({path.name for path in kb_b.iterdir()} == before,
                "stale detached maintenance touched the newly pinned KB")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(kb_a, ignore_errors=True)
        shutil.rmtree(kb_b, ignore_errors=True)


def test_stale_daemon_connection_cannot_spawn_maintenance_in_repinned_kb(
    monkeypatch,
):
    root = Path(tempfile.mkdtemp(prefix="kb_selfheal_stale_connection_"))
    project = root / "project"
    project.mkdir()
    (project / ".git").mkdir()
    kb_a = paths.validated_test_root() / "vaults" / f"selfheal-conn-a-{root.name}"
    kb_b = paths.validated_test_root() / "vaults" / f"selfheal-conn-b-{root.name}"
    kb_a.mkdir(parents=True)
    kb_b.mkdir(parents=True)
    popen_calls = []
    try:
        project_config.mark_kb_target(kb_a)
        project_config.mark_kb_target(kb_b)
        binding_a = project_config.write_binding(
            project, mode=project_config.MODE_LATCHED, kb_dir=kb_a,
        )
        context = mcp_runtime.ConnectionContext(
            connection_id="stale-maintenance",
            project_cwd=str(project),
            session_id="old-task",
            session_source="test",
            proxy_pid=123,
            proxy_started_at="now",
            runtime_key="test",
            project_binding_revision=binding_a.revision,
            project_kb_dir=str(kb_a),
        )
        project_config.write_binding(
            project, mode=project_config.MODE_LATCHED, kb_dir=kb_b,
        )
        before = {path.name for path in kb_b.iterdir()}
        monkeypatch.setattr(
            selfheal.subprocess, "Popen",
            lambda *args, **kwargs: popen_calls.append((args, kwargs)),
        )
        _reset_trigger()
        with mcp_runtime.bind_connection(context):
            selfheal.maybe_trigger(str(project))

        _assert(popen_calls == [], "stale daemon connection spawned maintenance")
        _assert({path.name for path in kb_b.iterdir()} == before,
                "stale daemon connection touched the newly pinned KB")
    finally:
        _reset_trigger()
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(kb_a, ignore_errors=True)
        shutil.rmtree(kb_b, ignore_errors=True)


def test_windows_shared_spawn_preserves_broker_owned_site_packages(
    monkeypatch, tmp_path
):
    proj = _fresh_project()
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    captured = {}

    class _FakePopen:
        def __init__(self, _args, **kwargs):
            captured.update(kwargs)

    context = mcp_runtime.ConnectionContext(
        connection_id="windows-maintenance",
        project_cwd=proj,
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="codex",
        maintenance_backend="codex",
    )
    private = mcp_runtime.validate_child_environment({
        "PATH": r"C:\client\bin",
        "OPENAI_API_KEY": "codex-secret",
    })
    try:
        monkeypatch.setattr(
            selfheal,
            "sys",
            SimpleNamespace(platform="win32", executable=sys.executable),
        )
        monkeypatch.setattr(selfheal.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(
            selfheal.paths,
            "configured_maintenance_runner",
            lambda **_kwargs: (
                "codex",
                sys.executable,
                str(tmp_path),
                os.pathsep.join(("/vault/bin", "/usr/bin")),
            ),
        )
        monkeypatch.setenv("PATH", "/first-spawner/poison")
        monkeypatch.setenv("PYTHONPATH", "/caller/poison")
        monkeypatch.setenv(
            mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV,
            str(site_packages),
        )
        with mcp_runtime.bind_connection(
            context, child_environment=private
        ):
            selfheal.spawn_detached(proj)
        _assert(captured["env"]["PYTHONPATH"] == str(site_packages), captured)
        _assert("/caller/poison" not in captured["env"]["PYTHONPATH"], captured)
        _assert(captured["env"]["USERPROFILE"] == str(tmp_path), captured)
        _assert(
            captured["env"]["PATH"]
            == os.pathsep.join(("/vault/bin", "/usr/bin")),
            captured,
        )
        _assert("OPENAI_API_KEY" not in captured["env"], captured)
    finally:
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
    test_ineligible_connections_do_not_consume_trigger_check()
    test_run_selfheal_skips_when_locked()
    test_only_run_ops_advance_stamps()
    test_raising_op_does_not_advance_its_stamp()
    test_workstream_governance_runs_after_shared_lock_release()
    test_backup_creates_and_prunes()
    test_backup_noop_without_db()
    test_spawn_builds_correct_command()
    print("\nAll selfheal tests pass.")
