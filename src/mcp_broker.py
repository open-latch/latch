"""Lightweight discovery and single-flight startup for the latch MCP daemon.

Only Python's standard library plus ``paths`` is imported here.  Keeping this
module slim is an architectural requirement: every host-created stdio process
imports it, while FastMCP, NumPy, ONNX Runtime, and the tokenizer live once in
the shared daemon.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO

import paths


PROTOCOL_VERSION = 1
DISCOVERY_FILE = "mcp-daemon.json"
START_LOCK_FILE = "mcp-daemon.start.lock"
OWNER_FENCE_FILE = "mcp-daemon.owner.lock"
EMBED_DISCOVERY_FILE = "embed.sock.json"
LOG_FILE = "mcp-daemon.log"
PROXY_LEASE_DIR = "mcp-proxies"
RUNTIME_REGISTRY_DIR = "mcp-runtimes"
LIFECYCLE_STREAM = "mcp_lifecycle"
DEFAULT_START_TIMEOUT_S = 30.0
DEFAULT_CONNECT_TIMEOUT_S = 2.0
DEFAULT_PROXY_CAP = 32
DEFAULT_PROXY_RETIRE_IDLE_S = 5 * 60.0
DEFAULT_PROXY_HEARTBEAT_S = 30.0
DEFAULT_PROXY_STALE_S = 5 * 60.0
START_FAILURE_MAX_AGE_S = 60.0
START_REASONS = frozenset({
    "proxy_start",
    "proxy_connect",
    "daemon_reconnect",
    "connection_retry",
    "prompt_hook",
})


class BrokerError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_key() -> str:
    """Content-fingerprint protocol-sensitive code and model compatibility.

    A changed key causes a blue/green owner transition: new proxies start a new
    daemon while already-connected proxies can finish against the old one.  The
    old owner then leaves on its idle timeout.  This avoids killing unrelated
    sessions during an upgrade.  Source and tokenizer contents are small enough
    to hash on every lightweight process start.  The 90 MB model is represented
    by its size plus the versioned tokenizer/config contents so proxy startup
    does not read gigabytes when many host contexts start together; replacing a
    model with a same-size incompatible artifact requires a protocol bump.
    """
    root = Path(__file__).resolve().parent
    content_files = (
        root / "mcp_broker.py",
        root / "mcp_proxy.py",
        root / "mcp_daemon.py",
        root / "mcp_server.py",
        root / "mcp_runtime.py",
        paths.KB_ROOT / "vendor" / "config.json",
        paths.KB_ROOT / "vendor" / "tokenizer.json",
        paths.KB_ROOT / "vendor" / "tokenizer_config.json",
        paths.KB_ROOT / "vendor" / "vocab.txt",
    )
    h = hashlib.sha256(f"protocol={PROTOCOL_VERSION}".encode())
    for path in content_files:
        h.update(f"\0{path.name}\0".encode())
        try:
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    h.update(chunk)
        except OSError:
            h.update(b"missing")
    model = paths.KB_ROOT / "vendor" / "model.onnx"
    try:
        model_size = model.stat().st_size
    except OSError:
        model_size = -1
    h.update(f"\0model.onnx:size={model_size}".encode())
    return h.hexdigest()[:20]


RUNTIME_KEY = _runtime_key()


def runtime_dir() -> Path:
    return paths.ensure_project_dir()


def runtime_key_dir(runtime_key: str | None = None) -> Path:
    """Return the private registry directory for one protocol runtime.

    Discovery and election must be keyed together.  A vault-wide discovery
    file paired with a vault-wide lock lets v1 -> v2 -> v1 starts overwrite
    each other and elect two owners for the same runtime.
    """
    key = runtime_key or RUNTIME_KEY
    if not key or len(key) > 64 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in key
    ):
        raise ValueError("invalid MCP runtime key")
    path = runtime_dir() / "runtime" / RUNTIME_REGISTRY_DIR / key
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def discovery_path(runtime_key: str | None = None) -> Path:
    return runtime_key_dir(runtime_key) / DISCOVERY_FILE


def start_lock_path(runtime_key: str | None = None) -> Path:
    return runtime_key_dir(runtime_key) / START_LOCK_FILE


def owner_fence_path(runtime_key: str | None = None) -> Path:
    return runtime_key_dir(runtime_key) / OWNER_FENCE_FILE


def embed_discovery_path(runtime_key: str | None = None) -> Path:
    return runtime_key_dir(runtime_key) / EMBED_DISCOVERY_FILE


def proxy_lease_dir(runtime_key: str | None = None) -> Path:
    """Lease pool for one runtime fingerprint within the pinned vault."""
    path = runtime_key_dir(runtime_key) / PROXY_LEASE_DIR
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.environ.get(name, str(default))))
    except ValueError:
        return default


def proxy_policy() -> dict[str, int | float]:
    return {
        "cap": _env_int("LATCH_MCP_PROXY_CAP", DEFAULT_PROXY_CAP),
        "retire_idle_s": _env_float(
            "LATCH_MCP_PROXY_RETIRE_IDLE_SEC", DEFAULT_PROXY_RETIRE_IDLE_S
        ),
        "heartbeat_s": _env_float(
            "LATCH_MCP_PROXY_HEARTBEAT_SEC", DEFAULT_PROXY_HEARTBEAT_S
        ),
        "stale_s": _env_float(
            "LATCH_MCP_PROXY_STALE_SEC", DEFAULT_PROXY_STALE_S
        ),
    }


def write_proxy_lease(connection_id: str, payload: dict[str, Any]) -> Path:
    path = proxy_lease_dir() / f"{connection_id}.json"
    _atomic_json(path, payload)
    return path


def remove_proxy_lease(connection_id: str, *, reason: str = "closed") -> None:
    try:
        (proxy_lease_dir() / f"{connection_id}.json").unlink()
        emit_lifecycle("proxy_closed", connection_id=connection_id, reason=reason)
    except OSError:
        pass


def proxy_lease_state(
    *, policy: dict[str, int | float] | None = None
) -> dict[str, Any]:
    """Return live capacity rows plus non-destructive stale diagnostics.

    A live process may renew its lease after this scan reads an old heartbeat.
    Never unlink that live process's path: its next atomic write repairs the
    lease.  Dead-process rows are safe to remove because connection ids are
    process-unique.
    """
    rows: list[dict[str, Any]] = []
    stale_count = 0
    max_stale_age_s = 0.0
    policy = proxy_policy() if policy is None else policy
    stale_s = float(policy["stale_s"])
    now = time.time()
    for path in proxy_lease_dir().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("runtime_key") != RUNTIME_KEY:
            continue
        pid = payload.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        heartbeat = payload.get("heartbeat_epoch")
        heartbeat_age = (
            max(0.0, now - float(heartbeat))
            if isinstance(heartbeat, (int, float))
            else float("inf")
        )
        if heartbeat_age > stale_s:
            stale_count += 1
            observed = heartbeat if isinstance(heartbeat, (int, float)) else payload.get(
                "started_epoch"
            )
            age = now - float(observed) if isinstance(observed, (int, float)) else stale_s
            max_stale_age_s = max(max_stale_age_s, age)
            continue
        rows.append(payload)
    rows.sort(
        key=lambda row: (
            float(row.get("last_activity_epoch") or 0.0),
            float(row.get("started_epoch") or 0.0),
        ),
        reverse=True,
    )
    return {
        "live": rows,
        "stale_count": stale_count,
        "max_stale_age_s": round(max_stale_age_s, 3),
    }


def proxy_inventory() -> list[dict[str, Any]]:
    return list(proxy_lease_state()["live"])


def _lifecycle_path(day: datetime | None = None) -> Path:
    day = day or datetime.now(timezone.utc)
    return runtime_dir() / f"{LIFECYCLE_STREAM}-{day:%Y-%m-%d}.log"


def emit_lifecycle(event: str, **fields: Any) -> None:
    """Best-effort, transition-only lifecycle telemetry.

    Rows deliberately exclude prompt text, tool arguments, tokens, and full
    session inventories.  Logging can never break the MCP path.
    """
    try:
        row = {
            "ts": _utc_now(),
            "event": event,
            "runtime_key": RUNTIME_KEY,
            "process_pid": os.getpid(),
            **fields,
        }
        path = _lifecycle_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str, sort_keys=True) + "\n")
    except Exception:
        pass


def lifecycle_summary(
    *,
    hours: int = 24,
    lease_state: dict[str, Any] | None = None,
    policy: dict[str, int | float] | None = None,
) -> dict[str, Any]:
    """Return bounded operational signals for status/doctor surfaces."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    counts: Counter[str] = Counter()
    warnings: list[dict[str, Any]] = []
    proxy_high_water = 0
    max_over_cap_duration_s = 0.0
    max_cold_start_duration_ms = 0.0
    max_peak_connections = 0
    latest_daemon_start: dict[str, Any] | None = None
    latest_daemon_start_ts: datetime | None = None
    warning_events = {
        "daemon_failed", "daemon_start_failed", "daemon_owner_conflict",
        "daemon_upgrade_incompatible",
        "proxy_over_cap", "proxy_retired", "legacy_fallback",
        "prompt_retrieval_degraded", "daemon_reconnect_failed",
        "daemon_disconnect_unknown_outcome",
    }
    for offset in range(0, max(2, hours // 24 + 2)):
        path = _lifecycle_path(datetime.now(timezone.utc) - timedelta(days=offset))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError):
                continue
            if ts < cutoff:
                continue
            if row.get("runtime_key") != RUNTIME_KEY:
                continue
            event = str(row.get("event") or "unknown")
            counts[event] += 1
            live_leases = row.get("live_leases")
            if isinstance(live_leases, int):
                proxy_high_water = max(proxy_high_water, live_leases)
            duration = row.get("over_cap_duration_s")
            if isinstance(duration, (int, float)):
                max_over_cap_duration_s = max(max_over_cap_duration_s, float(duration))
            cold_start = row.get("cold_start_duration_ms")
            if isinstance(cold_start, (int, float)):
                max_cold_start_duration_ms = max(
                    max_cold_start_duration_ms, float(cold_start)
                )
            peak_connections = row.get("peak_connections")
            if isinstance(peak_connections, int):
                max_peak_connections = max(max_peak_connections, peak_connections)
            if event == "daemon_started" and (
                latest_daemon_start_ts is None or ts > latest_daemon_start_ts
            ):
                latest_daemon_start_ts = ts
                latest_daemon_start = {
                    key: row.get(key)
                    for key in ("ts", "reason", "cold_start_duration_ms", "pid")
                    if row.get(key) is not None
                }
            if event in warning_events:
                warnings.append({
                    key: row.get(key)
                    for key in (
                        "ts", "event", "reason", "pid", "live_leases", "cap",
                        "over_cap_duration_s",
                    )
                    if row.get(key) is not None
                })
    warnings.sort(key=lambda row: str(row.get("ts") or ""))
    policy = proxy_policy() if policy is None else policy
    lease_state = (
        proxy_lease_state(policy=policy) if lease_state is None else lease_state
    )
    inventory = list(lease_state.get("live") or [])
    cap = int(policy["cap"])
    proxy_high_water = max(proxy_high_water, len(inventory))
    currently_over_cap = cap > 0 and len(inventory) > cap
    current_over_cap_duration_s = 0.0
    if currently_over_cap:
        observed = [
            float(row["over_cap_since_epoch"])
            for row in inventory
            if isinstance(row.get("over_cap_since_epoch"), (int, float))
        ]
        if observed:
            current_over_cap_duration_s = max(0.0, time.time() - min(observed))

    return {
        "window_hours": max(1, hours),
        "counts": dict(sorted(counts.items())),
        "proxy_high_water": proxy_high_water,
        "max_over_cap_duration_s": round(max_over_cap_duration_s, 3),
        "max_cold_start_duration_ms": round(max_cold_start_duration_ms, 3),
        "max_peak_connections": max_peak_connections,
        "latest_daemon_start": latest_daemon_start,
        "current_live_leases": len(inventory),
        "current_stale_leases": int(lease_state.get("stale_count") or 0),
        "max_stale_lease_age_s": float(lease_state.get("max_stale_age_s") or 0.0),
        "lease_scope": "runtime_key",
        "currently_over_cap": currently_over_cap,
        "current_over_cap_duration_s": round(current_over_cap_duration_s, 3),
        "over_cap_duration_is_lower_bound": currently_over_cap,
        "warning_count": sum(counts[event] for event in warning_events),
        "recent_warnings": warnings[-10:],
    }


def _atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_discovery(runtime_key: str | None = None) -> dict[str, Any] | None:
    key = runtime_key or RUNTIME_KEY
    try:
        payload = json.loads(discovery_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("runtime_key") != key:
        return None
    error = payload.get("error")
    if isinstance(error, str):
        try:
            age = time.time() - float(payload["created_epoch"])
        except (KeyError, TypeError, ValueError):
            return None
        if 0 <= age <= START_FAILURE_MAX_AGE_S:
            return payload
        try:
            discovery_path(key).unlink()
        except OSError:
            pass
        return None
    if payload.get("protocol") != PROTOCOL_VERSION:
        return None
    if payload.get("host") != "127.0.0.1":
        return None
    if not isinstance(payload.get("port"), int):
        return None
    if not isinstance(payload.get("token"), str) or not payload["token"]:
        return None
    if not isinstance(payload.get("pid"), int):
        return None
    return payload


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # On Windows os.kill(pid, 0) is not a harmless existence probe: Python
        # maps kill through Win32 termination/control-event semantics and it can
        # interrupt the process being inspected. Query the process handle
        # directly instead, using only the standard library.
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            handle = open_process(0x1000, False, pid)
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
            finally:
                close_handle(handle)
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _send_prelude(sock: socket.socket, metadata: dict[str, Any], *, op: str) -> None:
    payload = dict(metadata)
    payload.update({"op": op, "protocol": PROTOCOL_VERSION})
    sock.sendall(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")


def probe_discovery(
    payload: dict[str, Any], timeout: float = DEFAULT_CONNECT_TIMEOUT_S
) -> bool:
    try:
        with socket.create_connection(
            (payload["host"], int(payload["port"])), timeout=timeout
        ) as sock:
            sock.settimeout(timeout)
            _send_prelude(
                sock,
                {
                    "token": payload["token"],
                    "runtime_key": payload["runtime_key"],
                    "proxy_pid": os.getpid(),
                },
                op="probe",
            )
            line = sock.makefile("rb").readline(4096)
            response = json.loads(line.decode("utf-8"))
            return bool(response.get("ok") and response.get("pid") == payload["pid"])
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return False


def publish_start_failure(runtime_key: str, message: str) -> Path:
    """Publish a short-lived actionable startup failure for a retained proxy."""
    path = discovery_path(runtime_key)
    _atomic_json(path, {
        "runtime_key": runtime_key,
        "created_epoch": time.time(),
        "error": message,
    })
    return path


def _checked_discovery() -> dict[str, Any] | None:
    payload = read_discovery()
    if payload is not None and isinstance(payload.get("error"), str):
        raise BrokerError(payload["error"])
    return payload


def acquire_owner_fence() -> BinaryIO | None:
    """Acquire the process-lifetime fence for this vault/runtime key.

    The OS releases the lock if a warming daemon or its broker dies, unlike a
    PID file that can be stolen while the original daemon is still starting.
    """
    path = owner_fence_path()
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except (OSError, BlockingIOError):
        handle.close()
        return None


def _read_lock() -> dict[str, Any] | None:
    try:
        payload = json.loads(start_lock_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _acquire_start_lock() -> bool:
    path = start_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "runtime_key": RUNTIME_KEY,
        "created_at": _utc_now(),
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        lock = _read_lock() or {}
        owner = lock.get("pid")
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0.0
        if not isinstance(owner, int) or not _pid_alive(owner) or age > 2 * _start_timeout():
            try:
                path.unlink()
            except OSError:
                return False
            return _acquire_start_lock()
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True)
        f.write("\n")
    return True


def _release_start_lock() -> None:
    lock = _read_lock() or {}
    if lock.get("pid") != os.getpid():
        return
    try:
        start_lock_path().unlink()
    except OSError:
        pass


def _start_timeout() -> float:
    raw = os.environ.get("LATCH_MCP_DAEMON_START_TIMEOUT_SEC")
    try:
        return max(1.0, float(raw)) if raw is not None else DEFAULT_START_TIMEOUT_S
    except ValueError:
        return DEFAULT_START_TIMEOUT_S


def _start_reason(value: str) -> str:
    return value if value in START_REASONS else "unknown"


def _spawn_daemon(project_cwd: str, *, start_reason: str) -> int:
    daemon_py = Path(__file__).resolve().parent / "mcp_daemon.py"
    env = os.environ.copy()
    env["LATCH_KB_DIR"] = str(runtime_dir())
    env["LATCH_MCP_DAEMON_PROCESS"] = "1"
    env["LATCH_MCP_RUNTIME_KEY"] = RUNTIME_KEY
    env["LATCH_MCP_PROTOCOL_VERSION"] = str(PROTOCOL_VERSION)
    env["LATCH_MCP_INITIAL_PROJECT_CWD"] = project_cwd
    env["LATCH_MCP_START_REASON"] = _start_reason(start_reason)
    env["LATCH_MCP_START_REQUEST_EPOCH"] = str(time.time())

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "cwd": str(paths.KB_ROOT),
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        env["LATCH_MCP_DAEMONIZE"] = "1"

    log_path = runtime_dir() / LOG_FILE
    with log_path.open("ab", buffering=0) as log:
        kwargs["stderr"] = log
        process = subprocess.Popen([sys.executable, str(daemon_py)], **kwargs)
    if os.name != "nt":
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)
            raise BrokerError("latch MCP daemon bootstrap did not detach")
    emit_lifecycle(
        "daemon_spawned",
        bootstrap_pid=process.pid,
        reason=_start_reason(start_reason),
    )
    return process.pid


def ensure_daemon(
    project_cwd: str, *, start_reason: str = "proxy_connect"
) -> dict[str, Any]:
    payload = _checked_discovery()
    if payload is not None and probe_discovery(payload):
        return payload

    deadline = time.monotonic() + _start_timeout()
    acquired = _acquire_start_lock()
    if acquired:
        try:
            payload = _checked_discovery()
            if payload is None or not probe_discovery(payload):
                _spawn_daemon(project_cwd, start_reason=start_reason)
            while time.monotonic() < deadline:
                payload = _checked_discovery()
                if payload is not None and probe_discovery(payload):
                    return payload
                time.sleep(0.05)
        finally:
            _release_start_lock()
    else:
        while time.monotonic() < deadline:
            payload = _checked_discovery()
            if payload is not None and probe_discovery(payload):
                return payload
            time.sleep(0.05)

    raise BrokerError(
        f"shared latch MCP daemon did not become ready within {_start_timeout():.1f}s"
    )


def request_daemon_start(project_cwd: str) -> bool:
    """Request a detached single-flight startup without blocking a hook."""
    payload = read_discovery()
    if payload is not None and probe_discovery(payload, timeout=0.02):
        return False
    env = os.environ.copy()
    env["LATCH_KB_DIR"] = str(runtime_dir())
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(paths.KB_ROOT),
        "env": env,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--ensure-daemon",
                project_cwd,
                "prompt_hook",
            ],
            **kwargs,
        )
        emit_lifecycle("daemon_wake_requested")
        return True
    except OSError as exc:
        emit_lifecycle("daemon_start_failed", reason=str(exc))
        return False


def connect_mcp(
    metadata: dict[str, Any], *, start_reason: str = "proxy_connect"
) -> tuple[socket.socket, dict[str, Any]]:
    project_cwd = str(metadata.get("project_cwd") or os.getcwd())
    payload = ensure_daemon(project_cwd, start_reason=start_reason)
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            sock = socket.create_connection(
                (payload["host"], int(payload["port"])),
                timeout=DEFAULT_CONNECT_TIMEOUT_S,
            )
            sock.settimeout(None)
            prelude = dict(metadata)
            prelude.update(
                {
                    "token": payload["token"],
                    "runtime_key": RUNTIME_KEY,
                }
            )
            _send_prelude(sock, prelude, op="mcp")
            return sock, payload
        except OSError as exc:
            last_error = exc
            payload = ensure_daemon(project_cwd, start_reason="connection_retry")
    raise BrokerError(f"could not connect to shared latch MCP daemon: {last_error}")


def publish_discovery(
    *,
    port: int,
    token: str,
    pid: int,
    started_at: str,
    runtime_key: str | None = None,
    owner_runtime_key: str | None = None,
) -> Path:
    key = runtime_key or RUNTIME_KEY
    payload = {
        "protocol": PROTOCOL_VERSION,
        "runtime_key": key,
        "owner_runtime_key": owner_runtime_key or RUNTIME_KEY,
        "host": "127.0.0.1",
        "port": int(port),
        "token": token,
        "pid": int(pid),
        "started_at": started_at,
    }
    path = discovery_path(key)
    _atomic_json(path, payload)
    return path


def remove_discovery_aliases_if_owner(*, pid: int, token: str) -> None:
    """Remove every runtime-key alias that still points to this exact owner."""
    registry = runtime_dir() / "runtime" / RUNTIME_REGISTRY_DIR
    try:
        paths_to_check = list(registry.glob(f"*/{DISCOVERY_FILE}"))
    except OSError:
        return
    for path in paths_to_check:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("pid") == pid and secrets.compare_digest(
            str(payload.get("token") or ""), token
        ):
            try:
                path.unlink()
            except OSError:
                pass


def remove_embed_discovery_if_owner(*, pid: int, token: str) -> None:
    path = embed_discovery_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if payload.get("pid") == pid and secrets.compare_digest(
        str(payload.get("token") or ""), token
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--ensure-daemon":
        try:
            ensure_daemon(sys.argv[2], start_reason=_start_reason(sys.argv[3]))
            return 0
        except Exception as exc:
            emit_lifecycle("daemon_start_failed", reason=str(exc))
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
