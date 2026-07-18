"""Lightweight discovery and single-flight startup for the latch MCP daemon.

Only Python's standard library plus ``paths`` is imported here.  Keeping this
module slim is an architectural requirement: every host-created stdio process
imports it, while FastMCP, NumPy, ONNX Runtime, and the tokenizer live once in
the shared daemon.
"""
from __future__ import annotations

import hashlib
import json
import math
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

import mcp_runtime
import paths


PROTOCOL_VERSION = 1
PROXY_CAPABILITY_EPOCH = 3
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
DEFAULT_PROXY_CAP = mcp_runtime.DEFAULT_PROXY_CAP
DEFAULT_PROXY_RETIRE_IDLE_S = mcp_runtime.DEFAULT_PROXY_RETIRE_IDLE_S
DEFAULT_PROXY_HEARTBEAT_S = mcp_runtime.DEFAULT_PROXY_HEARTBEAT_S
DEFAULT_PROXY_STALE_S = mcp_runtime.DEFAULT_PROXY_STALE_S
START_FAILURE_MAX_AGE_S = 60.0
START_REASONS = frozenset({
    "proxy_start",
    "proxy_connect",
    "daemon_reconnect",
    "connection_retry",
    "prompt_hook",
})
WINDOWS_CREATE_NO_WINDOW = 0x08000000
WINDOWS_DETACHED_PROCESS = 0x00000008
WINDOWS_CREATE_NEW_PROCESS_GROUP = 0x00000200
DAEMON_OS_ENV_VARS = mcp_runtime.PROCESS_OS_ENV_VARS
DAEMON_OWNER_ENV_VARS: tuple[str, ...] = ()
DAEMON_HELPER_ENV_VARS = (
    "LATCH_MCP_DAEMON_START_TIMEOUT_SEC",
)


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


def legacy_discovery_path() -> Path:
    """Discovery location used before the runtime-key registry existed."""
    return runtime_dir() / DISCOVERY_FILE


def legacy_proxy_lease_dir() -> Path:
    """Lease location used by pre-registry proxies such as fa162bd."""
    return runtime_dir() / "runtime" / PROXY_LEASE_DIR


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value) if math.isfinite(value) else default


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


def write_proxy_lease(
    connection_id: str,
    payload: dict[str, Any],
    *,
    runtime_key: str | None = None,
) -> Path:
    path = proxy_lease_dir(runtime_key) / f"{connection_id}.json"
    _atomic_json(path, payload)
    return path


def remove_proxy_lease(
    connection_id: str,
    *,
    runtime_key: str | None = None,
    reason: str = "closed",
) -> None:
    try:
        (proxy_lease_dir(runtime_key) / f"{connection_id}.json").unlink()
        emit_lifecycle(
            "proxy_closed",
            connection_id=connection_id,
            reason=reason,
            runtime_key=runtime_key or RUNTIME_KEY,
        )
    except OSError:
        pass


def _registry_lease_sources(
    owner_runtime_key: str,
) -> tuple[list[tuple[Path, str, str]], set[str], set[str], set[str]]:
    """Classify every registry lease directory in one filesystem pass.

    Discovery associates a key with a live owner, but lease observation cannot
    depend on that association: retained proxies keep heartbeating after their
    owner exits and before they reconnect. PID liveness is the same bounded
    identity signal used for leases, so no network probe enters heartbeat or
    retirement paths.
    """
    sources: list[tuple[Path, str, str]] = []
    alias_keys = {owner_runtime_key}
    unassociated_keys: set[str] = set()
    other_owner_keys: set[str] = set()
    registry = runtime_dir() / "runtime" / RUNTIME_REGISTRY_DIR
    try:
        key_dirs = [path for path in registry.iterdir() if path.is_dir()]
    except OSError:
        key_dirs = []
    for key_dir in key_dirs:
        key = key_dir.name
        discovery: dict[str, Any] = {}
        try:
            value = json.loads(
                (key_dir / DISCOVERY_FILE).read_text(encoding="utf-8")
            )
            if isinstance(value, dict):
                discovery = value
        except (OSError, ValueError):
            pass
        pid = discovery.get("pid")
        discovery_live = isinstance(pid, int) and _pid_alive(pid)
        declared_owner = discovery.get("owner_runtime_key")
        if not isinstance(declared_owner, str) and discovery.get("runtime_key") == key:
            declared_owner = key
        if key == owner_runtime_key:
            lease_class = "current_owner"
        elif discovery_live and declared_owner == owner_runtime_key:
            lease_class = "current_alias"
            alias_keys.add(key)
        elif discovery_live and isinstance(declared_owner, str):
            lease_class = "other_live_owner"
            other_owner_keys.add(declared_owner)
        else:
            lease_class = "unassociated"
        lease_dir = key_dir / PROXY_LEASE_DIR
        if lease_dir.exists():
            lease_paths = list(lease_dir.glob("*.json"))
            if lease_paths and lease_class == "unassociated":
                unassociated_keys.add(key)
            sources.extend((path, key, lease_class) for path in lease_paths)
    legacy_dir = legacy_proxy_lease_dir()
    if legacy_dir.exists():
        sources.extend(
            (path, "legacy_pre_registry", "pre_registry")
            for path in legacy_dir.glob("*.json")
        )
    return sources, alias_keys, unassociated_keys, other_owner_keys


def proxy_lease_state(
    *,
    policy: dict[str, int | float] | None = None,
    owner_runtime_key: str | None = None,
) -> dict[str, Any]:
    """Return one owner-scoped capacity pool plus legacy alias diagnostics.

    A live process may renew its lease after this scan reads an old heartbeat.
    Never unlink that live process's path: its next atomic write repairs the
    lease. Dead-process rows are safe to remove because connection ids are
    process-unique. Capability-epoch leases are deduplicated across aliases so
    a crash between write-new/remove-old migration cannot consume two slots.
    """
    owner_runtime_key = owner_runtime_key or RUNTIME_KEY
    capable_by_id: dict[str, dict[str, Any]] = {}
    capable_class_by_id: dict[str, str] = {}
    legacy_by_id: dict[str, dict[str, Any]] = {}
    other_owner_by_id: dict[str, dict[str, Any]] = {}
    stale_count = 0
    max_stale_age_s = 0.0
    dead_removed = 0
    policy = proxy_policy() if policy is None else policy
    stale_s = float(policy["stale_s"])
    now = time.time()
    sources, alias_keys, unassociated_keys, other_owner_keys = (
        _registry_lease_sources(owner_runtime_key)
    )
    for path, _source_key, lease_class in sources:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        pid = payload.get("pid")
        if not isinstance(pid, int) or not _pid_alive(pid):
            try:
                path.unlink()
                dead_removed += 1
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
        connection_id = payload.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            continue
        row = dict(payload)
        epoch = payload.get("proxy_capability_epoch")
        capable = isinstance(epoch, int) and epoch >= PROXY_CAPABILITY_EPOCH
        if lease_class == "other_live_owner":
            target = other_owner_by_id
        else:
            target = capable_by_id if capable else legacy_by_id
        previous = target.get(connection_id)
        if previous is None or float(row.get("heartbeat_epoch") or 0.0) > float(
            previous.get("heartbeat_epoch") or 0.0
        ):
            target[connection_id] = row
            if target is capable_by_id:
                capable_class_by_id[connection_id] = lease_class
    def sorted_rows(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            rows.values(),
            key=lambda row: (
                float(row.get("last_activity_epoch") or 0.0),
                float(row.get("started_epoch") or 0.0),
            ),
            reverse=True,
        )

    rows = sorted_rows(capable_by_id)
    legacy_rows = sorted_rows(legacy_by_id)
    other_owner_rows = sorted_rows(other_owner_by_id)
    observed_rows = sorted_rows({
        **legacy_by_id,
        **other_owner_by_id,
        **capable_by_id,
    })
    unassociated_capable = [
        row
        for connection_id, row in capable_by_id.items()
        if capable_class_by_id.get(connection_id) == "unassociated"
    ]
    return {
        "live": rows,
        "legacy_incompatible": legacy_rows,
        "unassociated_capable": unassociated_capable,
        "other_live_owner": other_owner_rows,
        "observed_live": observed_rows,
        "stale_count": stale_count,
        "max_stale_age_s": round(max_stale_age_s, 3),
        "dead_removed": dead_removed,
        "owner_runtime_key": owner_runtime_key,
        "alias_runtime_keys": sorted(alias_keys),
        "pool_runtime_keys": sorted(alias_keys | unassociated_keys),
        "unassociated_runtime_keys": sorted(unassociated_keys),
        "other_owner_runtime_keys": sorted(other_owner_keys),
    }


def proxy_inventory(*, owner_runtime_key: str | None = None) -> list[dict[str, Any]]:
    return list(proxy_lease_state(owner_runtime_key=owner_runtime_key)["live"])


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
    policy = proxy_policy() if policy is None else policy
    lease_state = (
        proxy_lease_state(policy=policy) if lease_state is None else lease_state
    )
    visible_runtime_keys = set(lease_state.get("pool_runtime_keys") or [])
    visible_runtime_keys.add(RUNTIME_KEY)
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
        "proxy_upgrade_fresh_task_required",
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
            if row.get("runtime_key") not in visible_runtime_keys:
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
        "legacy_incompatible_leases": len(
            lease_state.get("legacy_incompatible") or []
        ),
        "unassociated_capable_leases": len(
            lease_state.get("unassociated_capable") or []
        ),
        "other_live_owner_leases": len(
            lease_state.get("other_live_owner") or []
        ),
        "observed_live_leases": len(lease_state.get("observed_live") or inventory),
        "lease_scope": "owner_runtime_key",
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


def _windows_creation_flags() -> int:
    # Windows documents CREATE_NO_WINDOW as ignored when DETACHED_PROCESS is
    # also set. Keep both intentionally: DETACHED_PROCESS protects daemon
    # lifetime semantics, while CREATE_NO_WINDOW remains defense in depth for
    # launch variants where detachment is unavailable or later narrowed.
    return (
        getattr(subprocess, "CREATE_NO_WINDOW", WINDOWS_CREATE_NO_WINDOW)
        | getattr(subprocess, "DETACHED_PROCESS", WINDOWS_DETACHED_PROCESS)
        | getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            WINDOWS_CREATE_NEW_PROCESS_GROUP,
        )
    )


def _windows_hidden_startupinfo() -> Any:
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
    startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startup


def _windows_venv_site_packages(explicit: str | None = None) -> str | None:
    """Resolve the one broker-owned Python import path used on Windows."""
    if explicit is None:
        if sys.prefix == sys.base_prefix:
            return None
        candidate = Path(sys.prefix) / "Lib" / "site-packages"
    else:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            raise BrokerError("invalid Windows venv site-packages handoff")
    if not candidate.is_dir():
        if explicit is not None:
            raise BrokerError("invalid Windows venv site-packages handoff")
        return None
    return str(candidate)


def _windows_base_command(
    env: dict[str, str],
    *,
    site_packages: str | None = None,
) -> str:
    """Bypass a venv redirector that can drop no-window creation flags."""
    candidates = (
        getattr(sys, "_base_executable", None),
        str(Path(sys.base_prefix) / "python.exe"),
    )
    executable = next(
        (str(Path(value)) for value in candidates if value and Path(value).is_file()),
        sys.executable,
    )
    # Never append an inherited loader path. The only permitted Python path is
    # the exact venv site-packages directory computed by the original proxy.
    env.pop("PYTHONPATH", None)
    env.pop(mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV, None)
    resolved_site_packages = _windows_venv_site_packages(site_packages)
    if resolved_site_packages is not None:
        env["PYTHONPATH"] = resolved_site_packages
        env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV] = resolved_site_packages
    return executable


def _daemon_environment(
    source: dict[str, str] | os._Environ[str] | None = None,
    *,
    for_helper: bool = False,
) -> dict[str, str]:
    """Build the long-lived owner environment from an exact contract.

    The first proxy to win daemon election must not donate arbitrary host,
    session, adapter, backend, or Python-loader state to every other client.
    Only OS process plumbing and explicitly owner-scoped lifecycle settings
    survive; broker-owned vault identity is written from canonical paths.
    """
    values = os.environ if source is None else source
    names = DAEMON_OS_ENV_VARS + DAEMON_OWNER_ENV_VARS
    if for_helper:
        names += DAEMON_HELPER_ENV_VARS
    if os.name == "nt":
        folded = {str(key).upper(): value for key, value in values.items()}
        env = {
            name: folded[name.upper()]
            for name in names
            if isinstance(folded.get(name.upper()), str)
        }
    else:
        env = {
            name: values[name]
            for name in names
            if isinstance(values.get(name), str)
        }
    env["LATCH_HOME"] = str(paths.KB_ROOT)
    env["LATCH_KB_DIR"] = str(runtime_dir())
    return env


def _spawn_daemon(
    project_cwd: str,
    *,
    start_reason: str,
    windows_site_packages: str | None = None,
) -> int:
    daemon_py = Path(__file__).resolve().parent / "mcp_daemon.py"
    env = _daemon_environment()
    env["LATCH_MCP_RUNTIME_KEY"] = RUNTIME_KEY
    env["LATCH_MCP_PROTOCOL_VERSION"] = str(PROTOCOL_VERSION)
    env["LATCH_MCP_PROXY_CAPABILITY_EPOCH"] = str(PROXY_CAPABILITY_EPOCH)
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
    executable = sys.executable
    if os.name == "nt":
        executable = _windows_base_command(
            env, site_packages=windows_site_packages
        )
        kwargs["creationflags"] = _windows_creation_flags()
        kwargs["startupinfo"] = _windows_hidden_startupinfo()
    else:
        env["LATCH_MCP_DAEMONIZE"] = "1"

    log_path = runtime_dir() / LOG_FILE
    with log_path.open("ab", buffering=0) as log:
        kwargs["stderr"] = log
        process = subprocess.Popen([executable, str(daemon_py)], **kwargs)
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
        parent_pid=os.getpid(),
        executable=executable,
        argv=[executable, str(daemon_py)],
        creationflags=int(kwargs.get("creationflags", 0)),
        reason=_start_reason(start_reason),
    )
    return process.pid


def ensure_daemon(
    project_cwd: str,
    *,
    start_reason: str = "proxy_connect",
    windows_site_packages: str | None = None,
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
                _spawn_daemon(
                    project_cwd,
                    start_reason=start_reason,
                    windows_site_packages=windows_site_packages,
                )
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
    env = _daemon_environment(for_helper=True)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(paths.KB_ROOT),
        "env": env,
        "close_fds": True,
    }
    executable = sys.executable
    argv = [
        executable,
        str(Path(__file__).resolve()),
        "--ensure-daemon",
        project_cwd,
        "prompt_hook",
    ]
    if os.name == "nt":
        site_packages = _windows_venv_site_packages()
        executable = _windows_base_command(env, site_packages=site_packages)
        argv[0] = executable
        if site_packages is not None:
            argv.extend(["--windows-site-packages", site_packages])
        kwargs["creationflags"] = _windows_creation_flags()
        kwargs["startupinfo"] = _windows_hidden_startupinfo()
    else:
        kwargs["start_new_session"] = True
    try:
        process = subprocess.Popen(argv, **kwargs)
        emit_lifecycle(
            "daemon_wake_requested",
            bootstrap_pid=process.pid,
            parent_pid=os.getpid(),
            executable=executable,
            argv=argv,
            creationflags=int(kwargs.get("creationflags", 0)),
        )
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
    compatibility: str = "migrate",
    legacy_path: bool = False,
) -> Path:
    key = runtime_key or RUNTIME_KEY
    payload = {
        "protocol": PROTOCOL_VERSION,
        "runtime_key": key,
        "owner_runtime_key": owner_runtime_key or RUNTIME_KEY,
        "required_proxy_capability_epoch": PROXY_CAPABILITY_EPOCH,
        "compatibility": compatibility,
        "host": "127.0.0.1",
        "port": int(port),
        "token": token,
        "pid": int(pid),
        "started_at": started_at,
    }
    path = legacy_discovery_path() if legacy_path else discovery_path(key)
    _atomic_json(path, payload)
    return path


def remove_discovery_aliases_if_owner(*, pid: int, token: str) -> None:
    """Remove every runtime-key alias that still points to this exact owner."""
    registry = runtime_dir() / "runtime" / RUNTIME_REGISTRY_DIR
    try:
        paths_to_check = list(registry.glob(f"*/{DISCOVERY_FILE}"))
    except OSError:
        paths_to_check = []
    paths_to_check.append(legacy_discovery_path())
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


def read_live_embed_discovery(
    *,
    runtime_key: str | None = None,
    owner_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return validated embed discovery for one runtime key.

    ``owner_payload`` must be the MCP discovery record already selected by the
    caller.  When supplied, the embed endpoint must belong to that exact live
    process and, for aliases, declare the same blue/green owner.  Centralizing
    this check keeps hook clients and alias publication from independently
    trusting a stale ``embed.sock.json``.
    """
    key = runtime_key or RUNTIME_KEY
    try:
        payload = json.loads(
            embed_discovery_path(key).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("runtime_key") != key:
        return None
    host = payload.get("host")
    port = payload.get("port")
    token = payload.get("token")
    pid = payload.get("pid")
    if (
        host != "127.0.0.1"
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(token, str)
        or not token
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or not _pid_alive(pid)
    ):
        return None
    if owner_payload is not None:
        owner_pid = owner_payload.get("pid")
        owner_key = owner_payload.get("owner_runtime_key")
        if not isinstance(owner_key, str) or not owner_key:
            owner_key = owner_payload.get("runtime_key")
        if not isinstance(owner_key, str) or not owner_key:
            owner_key = RUNTIME_KEY
        if (
            not isinstance(owner_pid, int)
            or isinstance(owner_pid, bool)
            or pid != owner_pid
            or not isinstance(owner_key, str)
            or not owner_key
        ):
            return None
        declared_owner = payload.get("owner_runtime_key")
        if declared_owner is not None and declared_owner != owner_key:
            return None
    return payload


def remove_embed_discovery_if_owner(*, pid: int, token: str) -> None:
    """Remove every embed-discovery alias still owned by this exact PID/token.

    A daemon publishes embed aliases under retained runtime keys during blue/
    green upgrades (``publish_embed_alias``). On shutdown it must retract ALL of
    them — not merely the current key's file — or a retained key keeps an embed
    discovery pointing at this now-dead endpoint, and a retained-key process
    reads it and fails every remote embed. Mirrors
    ``remove_discovery_aliases_if_owner`` for the embed socket.
    """
    registry = runtime_dir() / "runtime" / RUNTIME_REGISTRY_DIR
    try:
        paths_to_check = list(registry.glob(f"*/{EMBED_DISCOVERY_FILE}"))
    except OSError:
        paths_to_check = []
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


def publish_embed_alias(
    retained_key: str,
    *,
    owner_payload: dict[str, Any],
) -> "Path | None":
    """Repoint a retained runtime key's embed discovery at the current owner.

    The blue/green MCP upgrade already aliases ``mcp-daemon.json`` for a retained
    key so a retained proxy keeps reaching the new owner. The embed socket needs
    the same alias, or a process still running under the retained key reads its
    own stale/dead ``embed.sock.json`` and every remote embed fails
    (``embed_daemon_unavailable``). Copy the current owner's live embed endpoint
    under the retained key, tagged with ``owner_runtime_key`` so cleanup and
    client validation can identify it. Returns the alias path, or None when the
    owner has no live embed discovery to alias.
    """
    owner_meta = read_live_embed_discovery(owner_payload=owner_payload)
    if owner_meta is None:
        return None
    owner_key = owner_payload.get("owner_runtime_key")
    if not isinstance(owner_key, str) or not owner_key:
        owner_key = owner_payload.get("runtime_key")
    if not isinstance(owner_key, str) or not owner_key:
        owner_key = RUNTIME_KEY
    alias = dict(owner_meta)
    alias["runtime_key"] = retained_key
    alias["owner_runtime_key"] = owner_key
    dest = embed_discovery_path(retained_key)
    _atomic_json(dest, alias)
    return dest


def _main() -> int:
    if len(sys.argv) in (4, 6) and sys.argv[1] == "--ensure-daemon":
        try:
            windows_site_packages = None
            if len(sys.argv) == 6:
                if sys.argv[4] != "--windows-site-packages":
                    return 2
                windows_site_packages = _windows_venv_site_packages(sys.argv[5])
            ensure_daemon(
                sys.argv[2],
                start_reason=_start_reason(sys.argv[3]),
                windows_site_packages=windows_site_packages,
            )
            return 0
        except Exception as exc:
            emit_lifecycle("daemon_start_failed", reason=str(exc))
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
