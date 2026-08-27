"""Self-triggering maintenance — replaces the external OS scheduler.

The nightly maintenance pass (backup + heal + weekly decay/tree + prune) used
to be driven by a Windows scheduled task calling a bash wrapper. That bolted
latch to Windows + git-bash and broke on Mac / managed machines / laptops
(see KB id=1173, docs/claude_kb/selfheal_trigger_v1.md).

This module makes maintenance self-triggering off the Claude Code session
lifecycle instead:

  * `maybe_trigger(project_path)` is called once from legacy MCP startup, or
    by the first eligible authenticated shared-daemon connection. It is cheap
    and never raises: a cadence check + detached spawn if anything is due.
  * the detached child runs `run_selfheal(project_path)`, which holds the
    shared compactor lock for the whole pass (single-flight + write-gating)
    and runs each op only when its elapsed-time cadence is due.

No OS scheduler, no admin, no stored credentials — works on any OS/IDE/managed
machine wherever the MCP server already runs.

Cadence lives in `<project_dir>/maintenance_state.json` (elapsed-time since
last run, not wall-clock), mirroring the budget.json pattern.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import budget  # noqa: E402  (imported for symmetry / future use; heal gates internally)
import lockfile  # noqa: E402
import maintenance  # noqa: E402
import mcp_runtime  # noqa: E402
import paths  # noqa: E402
import vault_backup  # noqa: E402

# ---- cadence (hours). Defaults preserve the old schtask cadence. ----
BACKUP_INTERVAL_H = 6      # protected online snapshots
HEAL_INTERVAL_H = 48       # was every-2-days day-of-year parity (~48h)
WEEKLY_INTERVAL_H = 168    # decay + tree, weekly
WORKSTREAM_SHADOW_INTERVAL_H = 24

STATE_FILENAME = "maintenance_state.json"
SPAWN_LOG_FILENAME = "selfheal_spawn.log"
SPAWN_LOG_MAX_BYTES = 1_000_000  # truncate the detached-child stdout log past this

LEGACY_LOG_MAX_AGE_DAYS = 3


class BackupCreationError(RuntimeError):
    """A live vault existed but its required protected snapshot failed."""


# Reentrancy guard env var. Set on the detached maintenance child so that any
# `claude -p` it spawns (heal/tree arbitration) inherits it and its MCP server
# refuses to re-trigger maintenance. Mirrors compactor's CLAUDE_KB_IN_COMPACT.
IN_MAINTENANCE_ENV = "CLAUDE_KB_IN_MAINTENANCE"

# CREATE_NO_WINDOW: the detached maintenance child has no console, so any
# child it launches (git.exe, the claude.cmd shim inside heal/tree) would
# otherwise allocate its own console window. 0 on POSIX (no-op).
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_TRIGGER_LOCK = threading.Lock()
_TRIGGER_CHECKED = False
_TRIGGER_FAILURE_SIGNATURE: tuple[str, int | None, int | None] | None = None


# ---------------- state ----------------

def _state_path(project_path: str | None) -> Path:
    return paths.project_dir(project_path) / STATE_FILENAME


def _load_state(project_path: str | None) -> dict:
    p = _state_path(project_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        # Corrupt / unreadable state = treat as all-due (safe: a maintenance
        # pass is idempotent and conservative).
        return {}


def _save_state(project_path: str | None, state: dict) -> None:
    p = _state_path(project_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _due(state: dict, key: str, interval_h: float, now: datetime) -> bool:
    """True if `key` has never been stamped or `interval_h` hours have elapsed."""
    last = _parse(state.get(key))
    if last is None:
        return True
    return now - last >= timedelta(hours=interval_h)


def _any_due(state: dict, now: datetime) -> bool:
    return (
        _due(state, "last_backup_at", BACKUP_INTERVAL_H, now)
        or _due(state, "last_heal_at", HEAL_INTERVAL_H, now)
        or _due(state, "last_weekly_at", WEEKLY_INTERVAL_H, now)
        or _due(
            state,
            "last_workstream_shadow_at",
            WORKSTREAM_SHADOW_INTERVAL_H,
            now,
        )
    )


# ---------------- trigger (runs on the MCP startup path) ----------------

def _trigger_blocked() -> bool:
    connection = mcp_runtime.current_connection()
    return bool(
        paths.is_unlatched_mode()
        or paths.is_disabled()
        or paths.is_in_compact()
        or os.environ.get(IN_MAINTENANCE_ENV)
        or (connection is not None and connection.in_maintenance)
    )


def _runner_policy_signature(
    project_path: str | None,
) -> tuple[str, int | None, int | None]:
    path = (
        paths.project_dir(project_path)
        / paths.VAULT_RUNTIME_SETTINGS_FILENAME
    )
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(path), None, None


def maybe_trigger(project_path: str | None) -> None:
    """Cheap, never-raises. Spawn a detached maintenance pass iff something is
    due. In shared mode, the first eligible authenticated connection performs
    the check; guarded connections do not consume the process-wide attempt."""
    global _TRIGGER_CHECKED, _TRIGGER_FAILURE_SIGNATURE
    try:
        signature = _runner_policy_signature(project_path)
        if (
            _TRIGGER_CHECKED
            or _TRIGGER_FAILURE_SIGNATURE == signature
            or _trigger_blocked()
        ):
            return
        with _TRIGGER_LOCK:
            signature = _runner_policy_signature(project_path)
            if (
                _TRIGGER_CHECKED
                or _TRIGGER_FAILURE_SIGNATURE == signature
                or _trigger_blocked()
            ):
                return
            state = _load_state(project_path)
            if _any_due(state, datetime.now(timezone.utc)):
                try:
                    spawn_detached(project_path)
                except Exception:
                    # Suppress repeat noise for unchanged broken policy while
                    # allowing quickstart/config repair to retry in-place.
                    _TRIGGER_FAILURE_SIGNATURE = signature
                    raise
            _TRIGGER_FAILURE_SIGNATURE = None
            _TRIGGER_CHECKED = True
    except Exception as e:
        # Never let a maintenance trigger break MCP startup.
        sys.stderr.write(f"[latch] selfheal.maybe_trigger error: {e}\n")


def spawn_detached(project_path: str | None) -> None:
    """Launch `selfheal.py <project_path>` as a detached background process
    that outlives this MCP server. Cross-platform detach (id=1071 audit)."""
    proj_dir = paths.ensure_project_dir(project_path)
    log_path = proj_dir / SPAWN_LOG_FILENAME
    _rotate_spawn_log(log_path)

    connection = mcp_runtime.current_connection()
    env = mcp_runtime.autonomous_subprocess_environment()
    if connection is not None:
        backend, executable, maintenance_home, maintenance_path = (
            paths.configured_maintenance_runner(project_path=project_path)
        )
        env["LATCH_MAINTENANCE_BACKEND"] = backend
        env[paths.MAINTENANCE_EXECUTABLE_ENV[backend]] = executable
        if backend == "claude":
            # Model selectors are policy, not credentials. Preserve them across
            # the detached boundary while continuing to exclude private auth.
            for name in mcp_runtime.CLAUDE_MODEL_ENV_VARS:
                value = mcp_runtime.connection_env_value(name)
                if value is not None:
                    env[name] = value
        env["HOME"] = maintenance_home
        env["PATH"] = maintenance_path
        if sys.platform == "win32":
            env["USERPROFILE"] = maintenance_home
        else:
            env.pop("USERPROFILE", None)
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
    if connection is not None and sys.platform == "win32":
        raw_site_packages = os.environ.get(
            mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV
        )
        if raw_site_packages:
            site_packages = Path(raw_site_packages)
            if not site_packages.is_absolute() or not site_packages.is_dir():
                raise ValueError("invalid broker-owned Windows site-packages path")
            env["PYTHONPATH"] = str(site_packages)
    env[IN_MAINTENANCE_ENV] = "1"

    args = [sys.executable, str(Path(__file__).resolve()), str(project_path or os.getcwd())]
    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        env=env,
        close_fds=True,
    )
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    # Capture the detached child's stdout/stderr (any crash traceback) to the
    # spawn log. The parent's handle is closed right after Popen; the child
    # keeps its own inherited handle.
    with open(log_path, "a", encoding="utf-8") as log:
        subprocess.Popen(args, stdout=log, stderr=log, **kwargs)


def _rotate_spawn_log(log_path: Path) -> None:
    try:
        if log_path.exists() and log_path.stat().st_size > SPAWN_LOG_MAX_BYTES:
            log_path.unlink()
    except OSError:
        pass


# ---------------- the pass (runs in the detached child) ----------------

def run_selfheal(project_path: str | None) -> dict:
    """The maintenance pass. Single-flight via the shared compactor lock;
    each op runs only when its cadence is due. Backup always runs first when
    any mutating op will run, so heal/weekly never mutate without a snapshot."""
    if paths.is_unlatched_mode():
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}

    run_governed_after_unlock = False
    automation_result: dict | None = None
    with lockfile.compactor_lock(project_path) as acquired:
        if not acquired:
            # A compaction or another selfheal pass already holds the lock.
            _log(f"lock held for {project_path} — skipping pass")
            return {"ok": False, "reason": "locked"}

        state = _load_state(project_path)
        now = datetime.now(timezone.utc)
        ran: list[str] = []

        backup_due = _due(state, "last_backup_at", BACKUP_INTERVAL_H, now)
        heal_due = _due(state, "last_heal_at", HEAL_INTERVAL_H, now)
        weekly_due = _due(state, "last_weekly_at", WEEKLY_INTERVAL_H, now)
        workstream_shadow_due = _due(
            state,
            "last_workstream_shadow_at",
            WORKSTREAM_SHADOW_INTERVAL_H,
            now,
        )

        # Snapshot before any mutating op, even if the backup cadence alone
        # wasn't due (matches the old wrapper's "backup before any op").
        backup_failed = False
        if backup_due or heal_due or weekly_due or workstream_shadow_due:
            try:
                backup_created = _backup_db(project_path)
            except BackupCreationError:
                backup_failed = True
                backup_created = False
            if backup_created:
                _prune_backups(project_path)
                state["last_backup_at"] = now.isoformat()
                ran.append("backup")

        blocked: list[str] = []
        if heal_due and backup_failed:
            blocked.append("heal")
            _log(f"heal blocked for {project_path}: required protected backup failed")
        elif heal_due:
            try:
                maintenance.run_nightly_heal(
                    project_path, already_locked=True,
                )  # budget-gated internally
                state["last_heal_at"] = now.isoformat()
                ran.append("heal")
            except Exception as e:
                _log(f"heal failed for {project_path}: {e}")

        if weekly_due and backup_failed:
            blocked.append("weekly")
            _log(f"weekly/tree blocked for {project_path}: required protected backup failed")
        elif weekly_due:
            try:
                maintenance.run_weekly_maintenance(project_path)
                maintenance.run_tree_rebuild(project_path)
                state["last_weekly_at"] = now.isoformat()
                ran.append("weekly")
            except Exception as e:
                _log(f"weekly/tree failed for {project_path}: {e}")

        # Independent cadence: lifecycle detection must still run on days when
        # the contradiction healer is not due (or fails). It derives candidates
        # only; governed mutation is a separate trust-ladder stage.
        if workstream_shadow_due and backup_failed:
            blocked.append("workstream_shadow")
            _log(
                f"workstream shadow blocked for {project_path}: "
                "required protected backup failed"
            )
        elif workstream_shadow_due:
            try:
                maintenance.run_workstream_shadow(
                    project_path, already_locked=True,
                )
                state["last_workstream_shadow_at"] = now.isoformat()
                ran.append("workstream_shadow")
                # Lifecycle operations take this same lock via writer_lock.
                # Defer governed execution until the outer maintenance lock is
                # released instead of self-deadlocking here.
                run_governed_after_unlock = True
            except Exception as e:
                _log(f"workstream shadow failed for {project_path}: {e}")

        _prune_legacy_logs(project_path)

        if ran and os.environ.get("CLAUDE_KB_GIT_SNAPSHOT") == "1":
            _git_snapshot(project_path)

        _save_state(project_path, state)

    if run_governed_after_unlock:
        try:
            automation_result = maintenance.run_workstream_governed(project_path)
            ran.append("workstream_automation")
        except Exception as e:
            _log(f"workstream automation failed for {project_path}: {e}")

    _log(f"pass complete for {project_path}: ran={ran}")
    result = {"ok": not backup_failed, "ran": ran}
    if backup_failed:
        result.update({
            "reason": "backup_failed",
            "blocked": blocked,
        })
    if automation_result is not None:
        result["workstream_automation"] = {
            "ok": bool(automation_result.get("ok")),
            "applied_count": len(automation_result.get("applied") or []),
            "failed_count": len(automation_result.get("failed") or []),
            "suggestion_count": int(
                automation_result.get("suggestion_count") or 0
            ),
        }
    return result


def _backup_db(project_path: str | None) -> bool:
    """Create a protected online snapshot outside the live vault."""
    if not paths.db_path(project_path).exists():
        _log(f"no kb.db at {paths.db_path(project_path)} — skipping backup")
        return False
    try:
        receipt = vault_backup.create_snapshot(project_path, reason="selfheal")
        _log(f"protected backup created: {receipt['manifest']}")
        return True
    except Exception as e:
        _log(f"protected backup failed: {e}")
        raise BackupCreationError("required protected backup failed") from e


def _prune_backups(project_path: str | None, keep: int | None = None) -> None:
    """Prune only snapshots whose signed-in-code protection window expired."""
    del keep  # compatibility with older focused tests; count retention is gone.
    try:
        vault_backup.prune_expired(project_path)
    except Exception as e:
        _log(f"protected backup prune failed: {e}")


def _prune_legacy_logs(project_path: str | None) -> None:
    """Best-effort cleanup of the old bash-wrapper per-run log artifacts under
    maintenance_logs/ (selfheal no longer writes them, but a migrated install
    may still have stale ones)."""
    log_dir = paths.project_dir(project_path) / "maintenance_logs"
    if not log_dir.is_dir():
        return
    cutoff = datetime.now(timezone.utc).timestamp() - LEGACY_LOG_MAX_AGE_DAYS * 86400
    patterns = ("*_debug_*.log", "*_stderr_*.log", "*_summary_*.json", "run_*.log")
    for pat in patterns:
        for f in log_dir.glob(pat):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def _git_snapshot(project_path: str | None) -> None:
    """OPT-IN ONLY (CLAUDE_KB_GIT_SNAPSHOT=1). Best-effort, fully exception-
    wrapped: a git failure must never break the maintenance pass. Most install
    users have no git remote configured, which is exactly why this is off by
    default — see docs/claude_kb/selfheal_trigger_v1.md §2.5."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    try:
        kb_home = str(paths.KB_ROOT)
        subprocess.run(["git", "-C", kb_home, "add", "-A"],
                       capture_output=True, timeout=60, check=False,
                       creationflags=CREATE_NO_WINDOW)
        subprocess.run(["git", "-C", kb_home, "commit", "-m", f"kb snapshot {ts}"],
                       capture_output=True, timeout=60, check=False,
                       creationflags=CREATE_NO_WINDOW)
        subprocess.run(["git", "-C", kb_home, "push"],
                       capture_output=True, timeout=120, check=False,
                       creationflags=CREATE_NO_WINDOW)
        _log("git snapshot attempted (opt-in)")
    except Exception as e:
        _log(f"git snapshot failed (ignored): {e}")


def _log(msg: str) -> None:
    log_path = paths.KB_ROOT / "maintenance.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] selfheal: {msg}\n")
    except Exception:
        pass


if __name__ == "__main__":
    # Detached entry point: python selfheal.py <project_path>
    project = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    print(json.dumps(run_selfheal(project)))
