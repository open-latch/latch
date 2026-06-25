"""Self-triggering maintenance — replaces the external OS scheduler.

The nightly maintenance pass (backup + heal + weekly decay/tree + prune) used
to be driven by a Windows scheduled task calling a bash wrapper. That bolted
latch to Windows + git-bash and broke on Mac / managed machines / laptops
(see KB id=1173, docs/claude_kb/selfheal_trigger_v1.md).

This module makes maintenance self-triggering off the Claude Code session
lifecycle instead:

  * `maybe_trigger(project_path)` is called once from the MCP server startup
    path (mcp_server.py __main__). It is cheap and never raises: a cadence
    check + a detached background spawn if anything is due.
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
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import budget  # noqa: E402  (imported for symmetry / future use; heal gates internally)
import lockfile  # noqa: E402
import maintenance  # noqa: E402
import paths  # noqa: E402

# ---- cadence (hours). Defaults preserve the old schtask cadence. ----
BACKUP_INTERVAL_H = 12     # local kb.db.bak rotation
HEAL_INTERVAL_H = 48       # was every-2-days day-of-year parity (~48h)
WEEKLY_INTERVAL_H = 168    # decay + tree, weekly

STATE_FILENAME = "maintenance_state.json"
SPAWN_LOG_FILENAME = "selfheal_spawn.log"
SPAWN_LOG_MAX_BYTES = 1_000_000  # truncate the detached-child stdout log past this

BACKUPS_KEPT = 3           # kb.db.bak.* retained, newest by mtime
LEGACY_LOG_MAX_AGE_DAYS = 3

# Reentrancy guard env var. Set on the detached maintenance child so that any
# `claude -p` it spawns (heal/tree arbitration) inherits it and its MCP server
# refuses to re-trigger maintenance. Mirrors compactor's CLAUDE_KB_IN_COMPACT.
IN_MAINTENANCE_ENV = "CLAUDE_KB_IN_MAINTENANCE"

# CREATE_NO_WINDOW: the detached maintenance child has no console, so any
# child it launches (git.exe, the claude.cmd shim inside heal/tree) would
# otherwise allocate its own console window. 0 on POSIX (no-op).
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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
    )


# ---------------- trigger (runs on the MCP startup path) ----------------

def maybe_trigger(project_path: str | None) -> None:
    """Cheap, never-raises. Spawn a detached maintenance pass iff something is
    due. Called once from mcp_server.py __main__ before mcp.run()."""
    try:
        if paths.is_disabled():
            return
        # Reentrancy: do not trigger from inside a maintenance/compaction child
        # (its own claude -p arbitration must not recurse into more maintenance).
        if os.environ.get(IN_MAINTENANCE_ENV) or paths.is_in_compact():
            return
        state = _load_state(project_path)
        if not _any_due(state, datetime.now(timezone.utc)):
            return
        spawn_detached(project_path)
    except Exception as e:
        # Never let a maintenance trigger break MCP startup.
        sys.stderr.write(f"[latch] selfheal.maybe_trigger error: {e}\n")


def spawn_detached(project_path: str | None) -> None:
    """Launch `selfheal.py <project_path>` as a detached background process
    that outlives this MCP server. Cross-platform detach (id=1071 audit)."""
    proj_dir = paths.ensure_project_dir(project_path)
    log_path = proj_dir / SPAWN_LOG_FILENAME
    _rotate_spawn_log(log_path)

    env = os.environ.copy()
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
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}

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

        # Snapshot before any mutating op, even if the backup cadence alone
        # wasn't due (matches the old wrapper's "backup before any op").
        if backup_due or heal_due or weekly_due:
            if _backup_db(project_path):
                _prune_backups(project_path)
                state["last_backup_at"] = now.isoformat()
                ran.append("backup")

        if heal_due:
            try:
                maintenance.run_nightly_heal(project_path)  # budget-gated internally
                state["last_heal_at"] = now.isoformat()
                ran.append("heal")
            except Exception as e:
                _log(f"heal failed for {project_path}: {e}")

        if weekly_due:
            try:
                maintenance.run_weekly_maintenance(project_path)
                maintenance.run_tree_rebuild(project_path)
                state["last_weekly_at"] = now.isoformat()
                ran.append("weekly")
            except Exception as e:
                _log(f"weekly/tree failed for {project_path}: {e}")

        _prune_legacy_logs(project_path)

        if ran and os.environ.get("CLAUDE_KB_GIT_SNAPSHOT") == "1":
            _git_snapshot(project_path)

        _save_state(project_path, state)

    _log(f"pass complete for {project_path}: ran={ran}")
    return {"ok": True, "ran": ran}


def _backup_db(project_path: str | None) -> bool:
    """Copy kb.db -> kb.db.bak.<ts>. Returns False (no-op) if no kb.db yet."""
    proj_dir = paths.project_dir(project_path)
    src = proj_dir / "kb.db"
    if not src.exists():
        _log(f"no kb.db at {src} — skipping backup")
        return False
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = proj_dir / f"kb.db.bak.{ts}"
    try:
        shutil.copy2(src, dst)
        return True
    except OSError as e:
        _log(f"backup copy failed: {e}")
        return False


def _prune_backups(project_path: str | None, keep: int = BACKUPS_KEPT) -> None:
    """Keep the `keep` newest kb.db.bak.* by mtime; delete the rest."""
    proj_dir = paths.project_dir(project_path)
    baks = sorted(
        proj_dir.glob("kb.db.bak.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in baks[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


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
