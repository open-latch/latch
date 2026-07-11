"""Small stdio-to-daemon bridge used by every local MCP host context.

The proxy intentionally uses only the standard library and latch's lightweight
path/session helpers.  It preserves the stdio contract hosts already know while
moving FastMCP and the embedding runtime into one shared process.

If the daemon is reclaimed while idle, the proxy remains resident.  On the next
host message it reconnects, replays MCP initialization, and continues without a
fresh task.  If a daemon dies during an in-flight request, the proxy returns an
error instead of replaying a possibly mutating tool call.
"""
from __future__ import annotations

import json
import os
import queue
import select
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import codex_session
import mcp_broker


SESSION_ENV_VARS = (
    "LATCH_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_codex_adapter_env() -> bool:
    for name in ("LATCH_MODEL_BACKEND", "LATCH_GATE_BACKEND", "LATCH_MAINTENANCE_BACKEND"):
        if (os.environ.get(name) or "").strip().lower() == "codex":
            return True
    return bool((os.environ.get("CODEX_HOME") or "").strip())


def _same_path(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))


def _resolve_session(project_cwd: str) -> tuple[str | None, str]:
    for name in SESSION_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, f"env:{name}"

    if not _is_codex_adapter_env():
        return None, "unavailable"

    marker = codex_session.read_marker(project_cwd)
    if not marker:
        return None, "codex_marker_missing"
    marker_project = marker.get("project_path")
    if not isinstance(marker_project, str) or not _same_path(marker_project, project_cwd):
        # A pinned KB has one marker file.  Never attribute one workspace's MCP
        # connection to another workspace merely because they share the vault.
        return None, "codex_marker_project_mismatch"
    session_id = marker.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None, "codex_marker_missing_session"
    return session_id.strip(), "codex_session_start_marker"


def connection_metadata(project_cwd: str | None = None) -> dict[str, Any]:
    cwd = os.path.abspath(project_cwd or os.getcwd())
    session_id, source = _resolve_session(cwd)
    return {
        "connection_id": uuid.uuid4().hex,
        "project_cwd": cwd,
        "session_id": session_id,
        "session_source": source,
        "proxy_pid": os.getpid(),
        "proxy_started_at": _utc_now(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
    }


def _message(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


class ProxyBridge:
    def __init__(self, metadata: dict[str, Any]):
        self.metadata = metadata
        self._lease = ProxyLease(metadata)
        self._input: queue.Queue[bytes | None] = queue.Queue()
        self._wake_read, self._wake_write = socket.socketpair()
        self._wake_read.setblocking(False)
        self._wake_write.setblocking(False)
        self._sock: socket.socket | None = None
        self._socket_buffer = bytearray()
        self._stdin_eof = False
        self._init_line: bytes | None = None
        self._initialized_line: bytes | None = None
        self._init_id: Any = None
        self._init_completed = False
        self._replaying = False
        self._replay_id: Any = None
        self._deferred: list[bytes] = []
        self._pending: set[Any] = set()
        self.retired = False

    def _stdin_reader(self) -> None:
        try:
            while True:
                line = sys.stdin.buffer.readline()
                if not line:
                    break
                self._input.put(line)
                self._wake()
        finally:
            self._input.put(None)
            self._wake()

    def _wake(self) -> None:
        try:
            self._wake_write.send(b"x")
        except (BlockingIOError, OSError):
            pass

    def _connect(self, *, replay: bool) -> None:
        # Refresh only unresolved Codex marker attribution.  SessionStart can
        # finish after the host starts this stdio process.
        if self.metadata.get("session_id") is None:
            session_id, source = _resolve_session(self.metadata["project_cwd"])
            self.metadata["session_id"] = session_id
            self.metadata["session_source"] = source
        sock, _payload = mcp_broker.connect_mcp(self.metadata)
        self._sock = sock
        self._socket_buffer.clear()
        if replay and self._init_line is not None:
            sock.sendall(self._init_line)
            self._replaying = True
            self._replay_id = self._init_id

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._socket_buffer.clear()

    def _emit(self, line: bytes) -> None:
        sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
        sys.stdout.buffer.flush()

    def _emit_request_error(self, request_id: Any, message: str) -> None:
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32001, "message": message},
        }
        self._emit(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def _daemon_lost(self, reason: str) -> None:
        self._close_socket()
        for request_id in list(self._pending):
            self._emit_request_error(
                request_id,
                f"Latch shared daemon disconnected during the request ({reason}); retry once. "
                "The proxy did not replay the call because it may have mutated state.",
            )
        self._pending.clear()
        if self._deferred:
            for line in self._deferred:
                message = _message(line) or {}
                if "id" in message:
                    self._emit_request_error(
                        message["id"],
                        "Latch shared daemon could not be reinitialized; retry once.",
                    )
            self._deferred.clear()
        self._replaying = False
        self._replay_id = None

    def _forward(self, line: bytes) -> None:
        message = _message(line)
        if message is not None:
            method = message.get("method")
            if method == "initialize" and "id" in message:
                self._init_line = line
                self._init_id = message["id"]
                self._init_completed = False
                self._initialized_line = None
            elif method == "notifications/initialized":
                self._initialized_line = line
            if "id" in message and method:
                self._pending.add(message["id"])
        assert self._sock is not None
        self._sock.sendall(line)

    def _handle_host_line(self, line: bytes) -> None:
        self._lease.touch()
        if not line.endswith(b"\n"):
            line += b"\n"
        message = _message(line) or {}
        is_fresh_initialize = message.get("method") == "initialize"
        if is_fresh_initialize:
            # The host is explicitly opening a new logical session.  Do not
            # replay an older initialize message first.
            self._replaying = False
            self._deferred.clear()

        if self._sock is None:
            try:
                replay = bool(self._init_line is not None and not is_fresh_initialize)
                self._connect(replay=replay)
            except (OSError, mcp_broker.BrokerError) as exc:
                if "id" in message:
                    self._emit_request_error(message["id"], str(exc))
                return

        if self._replaying and not is_fresh_initialize:
            self._deferred.append(line)
            return

        try:
            self._forward(line)
        except OSError as exc:
            if "id" in message:
                self._pending.add(message["id"])
            self._daemon_lost(str(exc))

    def _finish_replay(self) -> None:
        self._replaying = False
        self._replay_id = None
        try:
            if self._initialized_line is not None:
                assert self._sock is not None
                self._sock.sendall(self._initialized_line)
            deferred, self._deferred = self._deferred, []
            for line in deferred:
                self._forward(line)
        except OSError as exc:
            self._daemon_lost(str(exc))

    def _handle_daemon_line(self, line: bytes) -> None:
        self._lease.touch()
        message = _message(line) or {}
        if self._replaying and message.get("id") == self._replay_id:
            if "error" in message:
                self._daemon_lost("replayed initialize was rejected")
                return
            self._finish_replay()
            return

        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            self._pending.discard(request_id)
            if request_id == self._init_id and "result" in message:
                self._init_completed = True
        self._emit(line)

    def _read_daemon(self) -> None:
        sock = self._sock
        if sock is None:
            return
        try:
            chunk = sock.recv(65536)
        except BlockingIOError:
            return
        except OSError as exc:
            self._daemon_lost(str(exc))
            return
        if not chunk:
            self._daemon_lost("owner exited or was reclaimed")
            return
        self._socket_buffer.extend(chunk)
        while b"\n" in self._socket_buffer:
            raw, _, rest = self._socket_buffer.partition(b"\n")
            self._socket_buffer = bytearray(rest)
            if raw:
                self._handle_daemon_line(raw + b"\n")

    def run(self) -> int:
        reader = threading.Thread(target=self._stdin_reader, name="latch-mcp-stdin", daemon=True)
        reader.start()
        self._lease.start()

        try:
            while True:
                sock = self._sock
                watched = [self._wake_read]
                if sock is not None:
                    watched.append(sock)
                try:
                    readable, _, _ = select.select(
                        watched, [], [], self._lease.seconds_until_heartbeat()
                    )
                except (OSError, ValueError):
                    readable = [self._wake_read]

                if self._wake_read in readable:
                    try:
                        while self._wake_read.recv(4096):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                    while True:
                        try:
                            item = self._input.get_nowait()
                        except queue.Empty:
                            break
                        if item is None:
                            self._stdin_eof = True
                            self._close_socket()
                            break
                        self._handle_host_line(item)

                if sock is not None and sock in readable and sock is self._sock:
                    self._read_daemon()

                if self._lease.heartbeat_due():
                    self._lease.heartbeat()
                    if (
                        not self._pending
                        and not self._replaying
                        and self._lease.should_retire()
                    ):
                        sys.stderr.write(
                            "[latch] retiring idle over-cap MCP proxy; start a fresh task "
                            "if this host does not reconnect it automatically\n"
                        )
                        sys.stderr.flush()
                        self.retired = True
                        return 0

                if self._stdin_eof:
                    return 0
        finally:
            self._close_socket()
            self._wake_read.close()
            self._wake_write.close()
            self._lease.close()


class ProxyLease:
    """One self-owned lease file; peers never signal or kill each other."""

    def __init__(self, metadata: dict[str, Any]):
        self.connection_id = str(metadata["connection_id"])
        self.pid = int(metadata["proxy_pid"])
        self.runtime_key = str(metadata["runtime_key"])
        self.started_epoch = time.time()
        self.last_activity_epoch = self.started_epoch
        self._last_heartbeat_monotonic = 0.0
        self.policy = mcp_broker.proxy_policy()

    def _write(self) -> None:
        mcp_broker.write_proxy_lease(
            self.connection_id,
            {
                "connection_id": self.connection_id,
                "pid": self.pid,
                "runtime_key": self.runtime_key,
                "started_epoch": self.started_epoch,
                "last_activity_epoch": self.last_activity_epoch,
                "heartbeat_epoch": time.time(),
            },
        )
        self._last_heartbeat_monotonic = time.monotonic()

    def start(self) -> None:
        self._write()

    def touch(self) -> None:
        self.last_activity_epoch = time.time()

    def heartbeat_due(self) -> bool:
        return self.seconds_until_heartbeat() <= 0.0

    def seconds_until_heartbeat(self) -> float:
        elapsed = time.monotonic() - self._last_heartbeat_monotonic
        return max(0.0, float(self.policy["heartbeat_s"]) - elapsed)

    def heartbeat(self) -> None:
        self._write()

    def should_retire(self) -> bool:
        cap = int(self.policy["cap"])
        if cap <= 0:
            return False
        inventory = mcp_broker.proxy_inventory()
        retained = {str(row.get("connection_id")) for row in inventory[:cap]}
        idle = time.time() - self.last_activity_epoch
        return (
            len(inventory) > cap
            and self.connection_id not in retained
            and idle >= float(self.policy["retire_idle_s"])
        )

    def close(self) -> None:
        mcp_broker.remove_proxy_lease(self.connection_id)


def _exec_legacy_server() -> None:
    env = os.environ.copy()
    env["LATCH_MCP_LEGACY"] = "1"
    server = Path(__file__).resolve().parent / "mcp_server.py"
    os.execve(sys.executable, [sys.executable, str(server)], env)


def main() -> int:
    metadata = connection_metadata()
    if os.environ.get("LATCH_MCP_FORCE_LEGACY"):
        _exec_legacy_server()
    try:
        # Establish readiness before consuming stdin.  If startup fails, the
        # compatibility fallback can exec without losing the host's initialize
        # request.
        mcp_broker.ensure_daemon(metadata["project_cwd"])
    except mcp_broker.BrokerError as exc:
        if os.environ.get("LATCH_MCP_NO_LEGACY_FALLBACK"):
            sys.stderr.write(f"[latch] shared MCP daemon unavailable: {exc}\n")
            return 1
        sys.stderr.write(f"[latch] shared MCP daemon unavailable; using legacy server: {exc}\n")
        _exec_legacy_server()
    bridge = ProxyBridge(metadata)
    code = bridge.run()
    if bridge.retired:
        # The stdin reader is intentionally blocked because the host still owns
        # the pipe.  Avoid CPython's buffered-stdin shutdown lock; bridge.run()
        # has already closed sockets and removed this proxy's lease.
        os._exit(code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
