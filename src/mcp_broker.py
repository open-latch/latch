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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import paths


PROTOCOL_VERSION = 1
DISCOVERY_FILE = "mcp-daemon.json"
START_LOCK_FILE = "mcp-daemon.start.lock"
LOG_FILE = "mcp-daemon.log"
PROXY_LEASE_DIR = "mcp-proxies"
DEFAULT_START_TIMEOUT_S = 30.0
DEFAULT_CONNECT_TIMEOUT_S = 2.0
DEFAULT_PROXY_CAP = 32
DEFAULT_PROXY_RETIRE_IDLE_S = 5 * 60.0
DEFAULT_PROXY_HEARTBEAT_S = 30.0


class BrokerError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_key() -> str:
    """Fingerprint protocol-sensitive code and the vendored model identity.

    A changed key causes a blue/green owner transition: new proxies start a new
    daemon while already-connected proxies can finish against the old one.  The
    old owner then leaves on its idle timeout.  This avoids killing unrelated
    sessions during an upgrade.
    """
    root = Path(__file__).resolve().parent
    files = (
        root / "mcp_broker.py",
        root / "mcp_proxy.py",
        root / "mcp_daemon.py",
        root / "mcp_server.py",
        root / "mcp_runtime.py",
        paths.KB_ROOT / "vendor" / "model.onnx",
        paths.KB_ROOT / "vendor" / "tokenizer.json",
    )
    h = hashlib.sha256(f"protocol={PROTOCOL_VERSION}".encode())
    for path in files:
        try:
            st = path.stat()
            identity = f"{path.name}:{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            identity = f"{path.name}:missing"
        h.update(identity.encode())
    return h.hexdigest()[:20]


RUNTIME_KEY = _runtime_key()


def runtime_dir() -> Path:
    return paths.ensure_project_dir()


def discovery_path() -> Path:
    return runtime_dir() / DISCOVERY_FILE


def start_lock_path() -> Path:
    return runtime_dir() / START_LOCK_FILE


def proxy_lease_dir() -> Path:
    path = runtime_dir() / "runtime" / PROXY_LEASE_DIR
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
    }


def write_proxy_lease(connection_id: str, payload: dict[str, Any]) -> Path:
    path = proxy_lease_dir() / f"{connection_id}.json"
    _atomic_json(path, payload)
    return path


def remove_proxy_lease(connection_id: str) -> None:
    try:
        (proxy_lease_dir() / f"{connection_id}.json").unlink()
    except OSError:
        pass


def proxy_inventory() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in proxy_lease_dir().glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        pid = payload.get("pid") if isinstance(payload, dict) else None
        if not isinstance(pid, int) or not _pid_alive(pid):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        rows.append(payload)
    rows.sort(
        key=lambda row: (
            float(row.get("last_activity_epoch") or 0.0),
            float(row.get("started_epoch") or 0.0),
        ),
        reverse=True,
    )
    return rows


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


def read_discovery(*, require_current: bool = True) -> dict[str, Any] | None:
    try:
        payload = json.loads(discovery_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("protocol") != PROTOCOL_VERSION:
        return None
    if require_current and payload.get("runtime_key") != RUNTIME_KEY:
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


def _probe(payload: dict[str, Any], timeout: float = DEFAULT_CONNECT_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection(
            (payload["host"], int(payload["port"])), timeout=timeout
        ) as sock:
            sock.settimeout(timeout)
            _send_prelude(
                sock,
                {
                    "token": payload["token"],
                    "runtime_key": RUNTIME_KEY,
                    "proxy_pid": os.getpid(),
                },
                op="probe",
            )
            line = sock.makefile("rb").readline(4096)
            response = json.loads(line.decode("utf-8"))
            return bool(response.get("ok") and response.get("pid") == payload["pid"])
    except (OSError, ValueError, KeyError):
        return False


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
        "created_monotonic": time.monotonic(),
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


def _spawn_daemon(project_cwd: str) -> int:
    daemon_py = Path(__file__).resolve().parent / "mcp_daemon.py"
    env = os.environ.copy()
    env["LATCH_KB_DIR"] = str(runtime_dir())
    env["LATCH_MCP_DAEMON_PROCESS"] = "1"
    env["LATCH_MCP_RUNTIME_KEY"] = RUNTIME_KEY
    env["LATCH_MCP_INITIAL_PROJECT_CWD"] = project_cwd

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
    return process.pid


def ensure_daemon(project_cwd: str) -> dict[str, Any]:
    payload = read_discovery()
    if payload is not None and _probe(payload):
        return payload

    deadline = time.monotonic() + _start_timeout()
    acquired = _acquire_start_lock()
    if acquired:
        try:
            payload = read_discovery()
            if payload is None or not _probe(payload):
                _spawn_daemon(project_cwd)
            while time.monotonic() < deadline:
                payload = read_discovery()
                if payload is not None and _probe(payload):
                    return payload
                time.sleep(0.05)
        finally:
            _release_start_lock()
    else:
        while time.monotonic() < deadline:
            payload = read_discovery()
            if payload is not None and _probe(payload):
                return payload
            time.sleep(0.05)

    raise BrokerError(
        f"shared latch MCP daemon did not become ready within {_start_timeout():.1f}s"
    )


def connect_mcp(metadata: dict[str, Any]) -> tuple[socket.socket, dict[str, Any]]:
    payload = ensure_daemon(str(metadata.get("project_cwd") or os.getcwd()))
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
            payload = ensure_daemon(str(metadata.get("project_cwd") or os.getcwd()))
    raise BrokerError(f"could not connect to shared latch MCP daemon: {last_error}")


def publish_discovery(*, port: int, token: str, pid: int, started_at: str) -> Path:
    payload = {
        "protocol": PROTOCOL_VERSION,
        "runtime_key": RUNTIME_KEY,
        "host": "127.0.0.1",
        "port": int(port),
        "token": token,
        "pid": int(pid),
        "started_at": started_at,
        "kb_dir": str(runtime_dir()),
    }
    path = discovery_path()
    _atomic_json(path, payload)
    return path


def remove_discovery_if_owner(*, pid: int, token: str) -> None:
    try:
        payload = json.loads(discovery_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if payload.get("pid") == pid and secrets.compare_digest(
        str(payload.get("token") or ""), token
    ):
        try:
            discovery_path().unlink()
        except OSError:
            pass
