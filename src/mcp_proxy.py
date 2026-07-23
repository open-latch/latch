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
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import codex_session
import mcp_broker
import mcp_runtime


SESSION_ENV_VARS = (
    "LATCH_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
)
COMPACT_ENV_VARS = ("LATCH_IN_COMPACT", "CLAUDE_KB_IN_COMPACT")
UNLATCHED_ENV_VARS = ("LATCH_UNLATCHED",)
DISABLED_ENV_VARS = ("LATCH_DISABLE", "CLAUDE_KB_DISABLE")
WRITE_DISABLED_ENV_VARS = (
    "LATCH_DISABLE_WRITE",
    "CLAUDE_KB_DISABLE_WRITE",
)
IN_MAINTENANCE_ENV_VARS = ("CLAUDE_KB_IN_MAINTENANCE",)
GATE_CLASSIFIER_TIMEOUT_ENV_VARS = (
    "LATCH_GATE_CLASSIFIER_TIMEOUT_S",
    "CLAUDE_KB_GATE_CLASSIFIER_TIMEOUT_S",
)
GATE_ADVERSARY_TIMEOUT_ENV_VARS = (
    "LATCH_GATE_ADVERSARY_TIMEOUT_S",
    "CLAUDE_KB_GATE_ADVERSARY_TIMEOUT_S",
)
GATE_BACKEND_ENV_VARS = (
    "LATCH_GATE_BACKEND",
    "CLAUDE_KB_GATE_BACKEND",
    "LATCH_MODEL_BACKEND",
)
MAINTENANCE_BACKEND_ENV_VARS = (
    "LATCH_MAINTENANCE_BACKEND",
    "CLAUDE_KB_MAINTENANCE_BACKEND",
    "LATCH_MODEL_BACKEND",
    "LATCH_GATE_BACKEND",
    "CLAUDE_KB_GATE_BACKEND",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_codex_adapter_env() -> bool:
    for name in ("LATCH_MODEL_BACKEND", "LATCH_GATE_BACKEND", "LATCH_MAINTENANCE_BACKEND"):
        if (os.environ.get(name) or "").strip().lower() == "codex":
            return True
    return bool((os.environ.get("CODEX_HOME") or "").strip())


def _is_cursor_adapter_env() -> bool:
    return (os.environ.get("LATCH_ADAPTER") or "").strip().lower() == "cursor"


def _same_path(left: str | os.PathLike, right: str | os.PathLike) -> bool:
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return os.path.abspath(os.fspath(left)) == os.path.abspath(os.fspath(right))


def _resolve_session(project_cwd: str) -> tuple[str | None, str]:
    # Cursor reuses one MCP process across conversations without a verified
    # per-request conversation id.  Never let process-level backend variables
    # or a workspace Codex marker attribute those interleaved requests.
    if _is_cursor_adapter_env():
        return None, "unavailable"

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


def _env_flag(names: tuple[str, ...]) -> bool:
    return any(bool(os.environ.get(name)) for name in names)


def _connection_backend(names: tuple[str, ...], *, purpose: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if not value:
            continue
        backend = value.strip().lower()
        if backend not in mcp_runtime.SUPPORTED_MODEL_BACKENDS:
            supported = ", ".join(sorted(mcp_runtime.SUPPORTED_MODEL_BACKENDS))
            raise ValueError(
                f"unsupported {purpose} backend {value!r} from {name}; "
                f"expected one of: {supported}"
            )
        return backend
    return "claude"


def _connection_positive_int(names: tuple[str, ...], *, default: int) -> int:
    for name in names:
        raw = os.environ.get(name)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value >= 1:
            return value
    return default


def _source_env_value(source: Mapping[str, str], name: str) -> str | None:
    if os.name != "nt":
        return source.get(name)
    folded = {str(key).upper(): value for key, value in source.items()}
    value = folded.get(name.upper())
    return value if isinstance(value, str) else None


def _which_on_explicit_path(command: str, path: str | None) -> str | None:
    """Resolve a command without Python 3.11/Windows cwd preemption."""
    return mcp_runtime.resolve_executable_on_path(command, path)


def _child_process_environment(
    source: Mapping[str, str] | None = None,
    *,
    backends: frozenset[str] = mcp_runtime.SUPPORTED_MODEL_BACKENDS,
) -> dict[str, str]:
    """Capture the exact private environment model children may receive."""
    values = os.environ if source is None else source
    unsupported = backends - mcp_runtime.SUPPORTED_MODEL_BACKENDS
    if unsupported:
        raise ValueError(
            f"unsupported child-process backend {sorted(unsupported)[0]!r}"
        )
    allowed_names = list(mcp_runtime.CONNECTION_CHILD_COMMON_ENV_VARS)
    for backend in sorted(backends):
        allowed_names.extend(
            mcp_runtime.CONNECTION_CHILD_BACKEND_ENV_VARS[backend]
        )
    child: dict[str, str] = {}
    seen_names: set[str] = set()
    for name in allowed_names:
        identity = name.upper() if os.name == "nt" else name
        if identity in seen_names:
            continue
        seen_names.add(identity)
        value = _source_env_value(values, name)
        if value is not None:
            child[name] = value
    path = _source_env_value(values, "PATH")
    binary_specs = {
        "claude": ("CLAUDE_BIN", ("claude",)),
        "codex": ("CODEX_BIN", ("codex",)),
        "cursor": ("CURSOR_AGENT_BIN", ("agent", "cursor-agent")),
    }
    for name, _candidates in binary_specs.values():
        child.pop(name, None)
    for backend in sorted(backends):
        name, candidates = binary_specs[backend]
        configured = _source_env_value(values, name)
        if configured:
            resolved = _which_on_explicit_path(configured, path)
            if resolved is None:
                raise ValueError(f"{name} does not resolve to an executable")
            child[name] = resolved
            continue
        child.pop(name, None)
        for candidate in candidates:
            resolved = _which_on_explicit_path(candidate, path)
            if resolved is not None:
                child[name] = resolved
                break
    private = mcp_runtime.validate_child_environment(child)
    return dict(private.values)


def connection_metadata(project_cwd: str | None = None) -> dict[str, Any]:
    cwd = os.path.abspath(project_cwd or os.getcwd())
    session_id, source = _resolve_session(cwd)
    gate_backend = _connection_backend(GATE_BACKEND_ENV_VARS, purpose="gate")
    maintenance_backend = _connection_backend(
        MAINTENANCE_BACKEND_ENV_VARS, purpose="maintenance"
    )
    return {
        "connection_id": uuid.uuid4().hex,
        "project_cwd": cwd,
        "session_id": session_id,
        "session_source": source,
        "proxy_pid": os.getpid(),
        "proxy_started_at": _utc_now(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
        "in_compact": _env_flag(COMPACT_ENV_VARS),
        "unlatched": _env_flag(UNLATCHED_ENV_VARS),
        "disabled": _env_flag(DISABLED_ENV_VARS),
        "write_disabled": _env_flag(WRITE_DISABLED_ENV_VARS),
        "in_maintenance": _env_flag(IN_MAINTENANCE_ENV_VARS),
        "gate_backend": gate_backend,
        "maintenance_backend": maintenance_backend,
        "gate_classifier_timeout_s": _connection_positive_int(
            GATE_CLASSIFIER_TIMEOUT_ENV_VARS,
            default=mcp_runtime.DEFAULT_GATE_CLASSIFIER_TIMEOUT_S,
        ),
        "gate_adversary_timeout_s": _connection_positive_int(
            GATE_ADVERSARY_TIMEOUT_ENV_VARS,
            default=mcp_runtime.DEFAULT_GATE_ADVERSARY_TIMEOUT_S,
        ),
        "gate_adversary_enabled": (
            os.environ.get("CLAUDE_KB_ADVERSARY", "1") != "0"
        ),
        "proxy_policy": mcp_broker.proxy_policy(),
        "child_process_env": _child_process_environment(
            backends=frozenset({gate_backend, maintenance_backend})
        ),
    }


def _message(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------- #
# Tool-surface trim (KB decision id=2224): fresh installs advertise latch_*
# only.  The shared daemon's registry serves EVERY install on the machine, so
# it always keeps both name families; each install's surface is decided here,
# in its own per-host proxy, from the env the host launched it with.  Existing
# installs (no flag) pass every frame through byte-identical.
# --------------------------------------------------------------------------- #
TOOL_SURFACE_ENV = "LATCH_TOOL_SURFACE"
_HIDDEN_TOOL_PREFIX = "kb_"
# latch_* tools kept in the daemon registry (the shared-runtime invariant test
# and daemon debugging call them directly) but never advertised on a trimmed
# install.  Their kb_* aliases are already hidden by the prefix rule.  Unlike
# kb_* aliases these are NOT rejected on call — they stay reachable by name for
# diagnostics; they are only dropped from tools/list.
_SURFACE_HIDDEN_TOOLS = frozenset({"latch_runtime_status"})


def is_hidden_from_listing(name: str) -> bool:
    """Single source of the trimmed-surface hide policy.

    Both trim paths use this so they can never drift: the stdio proxy filters
    these names out of tools/list, and the legacy one-process fallback prunes
    the same names from its own registry (mcp_server.prune_hidden_surface_tools).
    """
    return name.startswith(_HIDDEN_TOOL_PREFIX) or name in _SURFACE_HIDDEN_TOOLS


def trimmed_tool_surface(env: Mapping[str, str] | None = None) -> bool:
    """True when this install exposes the latch_* tool names only."""
    source = os.environ if env is None else env
    return (source.get(TOOL_SURFACE_ENV) or "").strip().lower() == "latch"


def filter_tools_list_result(message: dict[str, Any]) -> dict[str, Any]:
    """Drop kb_* aliases from a ``tools/list`` result.

    Returns the original message object unchanged when nothing was filtered so
    callers can preserve the exact daemon bytes on the passthrough path.
    """
    result = message.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return message
    kept = [
        tool
        for tool in tools
        if not (
            isinstance(tool, dict)
            and is_hidden_from_listing(str(tool.get("name") or ""))
        )
    ]
    if len(kept) == len(tools):
        return message
    filtered = dict(message)
    filtered["result"] = {**result, "tools": kept}
    return filtered


def rejected_kb_call(message: dict[str, Any]) -> str | None:
    """The kb_* tool name a trimmed-surface install must reject, if any."""
    if message.get("method") != "tools/call":
        return None
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if isinstance(name, str) and name.startswith(_HIDDEN_TOOL_PREFIX):
        return name
    return None


class ProxyBridge:
    def __init__(self, metadata: dict[str, Any]):
        self.metadata = metadata
        self._lease = ProxyLease(metadata)
        self._input: queue.Queue[bytes | None] = queue.Queue()
        self._wake_read, self._wake_write = socket.socketpair()
        self._wake_read.setblocking(False)
        self._wake_write.setblocking(False)
        self._sock: socket.socket | None = None
        self._owner_pid: int | None = None
        self._socket_buffer = bytearray()
        self._stdin_eof = False
        self._init_line: bytes | None = None
        self._initialized_line: bytes | None = None
        self._init_id: Any = None
        self._replaying = False
        self._replay_id: Any = None
        self._deferred: list[bytes] = []
        self._pending: dict[Any, str] = {}
        self._trimmed_surface = trimmed_tool_surface()
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
        sock, payload = mcp_broker.connect_mcp(
            self.metadata,
            start_reason="daemon_reconnect" if replay else "proxy_connect",
        )
        owner_runtime_key = payload.get("owner_runtime_key")
        if not isinstance(owner_runtime_key, str):
            owner_runtime_key = str(payload["runtime_key"])
        try:
            self._lease.migrate_scope(owner_runtime_key)
        except Exception:
            sock.close()
            raise
        self._sock = sock
        self._owner_pid = int(payload["pid"])
        self._socket_buffer.clear()
        if replay and self._init_line is not None:
            sock.sendall(self._init_line)
            self._replaying = True
            self._replay_id = self._init_id

    def _close_socket(self) -> None:
        sock, self._sock = self._sock, None
        self._owner_pid = None
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
        for request_id, operation in list(self._pending.items()):
            self._emit_request_error(
                request_id,
                f"Latch shared daemon disconnected during {operation} ({reason}). "
                "The outcome is unknown: the operation may have committed before the "
                "response was lost. Inspect current latch state before deciding whether "
                "to issue a new operation; the proxy did not replay it.",
            )
        if self._pending:
            mcp_broker.emit_lifecycle(
                "daemon_disconnect_unknown_outcome",
                pending_count=len(self._pending),
                reason=reason,
                runtime_key=self._lease.runtime_key,
            )
        self._pending.clear()
        if self._deferred:
            for line in self._deferred:
                message = _message(line) or {}
                if "id" in message:
                    self._emit_request_error(
                        message["id"],
                        "Latch shared daemon could not be reinitialized. This deferred "
                        "request was not sent; issue a new request after runtime recovery.",
                    )
            self._deferred.clear()
        self._replaying = False
        self._replay_id = None

    def _forward(self, line: bytes) -> None:
        message = _message(line)
        if message is not None:
            if self._trimmed_surface:
                rejected = rejected_kb_call(message)
                if rejected is not None:
                    if "id" in message:
                        preferred = "latch_" + rejected[len(_HIDDEN_TOOL_PREFIX):]
                        self._emit_request_error(
                            message["id"],
                            f"Tool {rejected!r} is not available on this install; "
                            f"use {preferred!r} instead.",
                        )
                    return
            method = message.get("method")
            if method == "initialize" and "id" in message:
                self._init_line = line
                self._init_id = message["id"]
                self._initialized_line = None
            elif method == "notifications/initialized":
                self._initialized_line = line
            if "id" in message and method:
                params = message.get("params")
                tool = params.get("name") if method == "tools/call" and isinstance(params, dict) else None
                self._pending[message["id"]] = (
                    f"tools/call {tool}" if isinstance(tool, str) else str(method)
                )
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

        # The shared daemon is a local child/peer whose PID came from the
        # authenticated discovery payload used for this socket.  Windows may
        # defer surfacing a dead peer's TCP reset until the next send, so check
        # that exact owner before a new request can become in-flight.  A death
        # after this check remains an unknown outcome and is never replayed.
        if (
            self._sock is not None
            and self._owner_pid is not None
            and not self._pending
            and not self._replaying
            and not mcp_broker._pid_alive(self._owner_pid)
        ):
            self._daemon_lost(f"owner process {self._owner_pid} exited")

        if self._sock is None:
            try:
                replay = bool(self._init_line is not None and not is_fresh_initialize)
                self._connect(replay=replay)
            except (OSError, mcp_broker.BrokerError) as exc:
                mcp_broker.emit_lifecycle(
                    "daemon_reconnect_failed" if self._init_line is not None else "daemon_start_failed",
                    connection_id=self.metadata["connection_id"],
                    reason=str(exc),
                    runtime_key=self._lease.runtime_key,
                )
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
                self._pending[message["id"]] = str(message.get("method") or "request")
            self._daemon_lost(str(exc))

    def _finish_replay(self) -> bool:
        self._replaying = False
        self._replay_id = None
        try:
            if self._initialized_line is not None:
                assert self._sock is not None
                self._sock.sendall(self._initialized_line)
            while self._deferred:
                line = self._deferred.pop(0)
                self._forward(line)
            return True
        except OSError as exc:
            self._daemon_lost(str(exc))
            return False

    def _handle_daemon_line(self, line: bytes) -> None:
        self._lease.touch()
        message = _message(line) or {}
        if self._replaying and message.get("id") == self._replay_id:
            if "error" in message:
                self._daemon_lost("replayed initialize was rejected")
                return
            if self._finish_replay():
                mcp_broker.emit_lifecycle(
                    "daemon_reconnect_succeeded",
                    connection_id=self.metadata["connection_id"],
                    runtime_key=self._lease.runtime_key,
                )
            return

        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            method = self._pending.pop(request_id, None)
            if (
                self._trimmed_surface
                and method == "tools/list"
                and "result" in message
            ):
                filtered = filter_tools_list_result(message)
                if filtered is not message:
                    self._emit(
                        json.dumps(filtered, separators=(",", ":")).encode("utf-8")
                    )
                    return
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

                # Drain owner state before accepting another host request.  On
                # Windows, terminating the owner surfaces as WSAECONNRESET
                # rather than a clean EOF.  If stdin and the owner socket are
                # ready together, processing stdin first can make a request
                # look in-flight even though the reset was already pending.
                if sock is not None and sock in readable and sock is self._sock:
                    self._read_daemon()

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
                        mcp_broker.emit_lifecycle(
                            "proxy_retired",
                            connection_id=self.metadata["connection_id"],
                            reason="idle_over_cap",
                            cap=int(self._lease.policy["cap"]),
                            over_cap_duration_s=self._lease.over_cap_duration_s(),
                            runtime_key=self._lease.runtime_key,
                        )
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
        self.proxy_capability_epoch = int(
            metadata.get("proxy_capability_epoch")
            or mcp_broker.PROXY_CAPABILITY_EPOCH
        )
        self.started_epoch = time.time()
        self.last_activity_epoch = self.started_epoch
        self._last_heartbeat_monotonic = 0.0
        self._over_cap_since_epoch: float | None = None
        self.policy = mcp_broker.proxy_policy()

    def _write(self) -> None:
        mcp_broker.write_proxy_lease(
            self.connection_id,
            {
                "connection_id": self.connection_id,
                "pid": self.pid,
                "runtime_key": self.runtime_key,
                "proxy_capability_epoch": self.proxy_capability_epoch,
                "started_epoch": self.started_epoch,
                "last_activity_epoch": self.last_activity_epoch,
                "heartbeat_epoch": time.time(),
                "over_cap_since_epoch": self._over_cap_since_epoch,
            },
            runtime_key=self.runtime_key,
        )
        self._last_heartbeat_monotonic = time.monotonic()

    def start(self) -> None:
        self._write()
        inventory = mcp_broker.proxy_inventory(owner_runtime_key=self.runtime_key)
        mcp_broker.emit_lifecycle(
            "proxy_started",
            connection_id=self.connection_id,
            live_leases=len(inventory),
            cap=int(self.policy["cap"]),
            runtime_key=self.runtime_key,
        )
        if int(self.policy["cap"]) > 0 and len(inventory) > int(self.policy["cap"]):
            self._over_cap_since_epoch = time.time()
            self._write()
            mcp_broker.emit_lifecycle(
                "proxy_over_cap",
                connection_id=self.connection_id,
                live_leases=len(inventory),
                cap=int(self.policy["cap"]),
                runtime_key=self.runtime_key,
            )

    def migrate_scope(self, owner_runtime_key: str) -> None:
        """Atomically join the ready owner's one capability lease pool."""
        if owner_runtime_key == self.runtime_key:
            return
        previous_runtime_key = self.runtime_key
        self.runtime_key = owner_runtime_key
        try:
            self._write()
        except Exception:
            self.runtime_key = previous_runtime_key
            raise
        mcp_broker.remove_proxy_lease(
            self.connection_id,
            runtime_key=previous_runtime_key,
            reason="proxy_lease_migrated",
        )
        inventory = mcp_broker.proxy_inventory(owner_runtime_key=self.runtime_key)
        mcp_broker.emit_lifecycle(
            "proxy_lease_migrated",
            connection_id=self.connection_id,
            previous_runtime_key=previous_runtime_key,
            live_leases=len(inventory),
            cap=int(self.policy["cap"]),
            runtime_key=self.runtime_key,
        )

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
        inventory = mcp_broker.proxy_inventory(owner_runtime_key=self.runtime_key)
        previous = self._over_cap_since_epoch
        if len(inventory) > cap:
            if self._over_cap_since_epoch is None:
                self._over_cap_since_epoch = time.time()
        else:
            self._over_cap_since_epoch = None
        if self._over_cap_since_epoch != previous:
            # Persist only pressure transitions; routine heartbeats already
            # wrote the lease immediately before this check.
            self._write()
        retained = {str(row.get("connection_id")) for row in inventory[:cap]}
        idle = time.time() - self.last_activity_epoch
        return (
            len(inventory) > cap
            and self.connection_id not in retained
            and idle >= float(self.policy["retire_idle_s"])
        )

    def over_cap_duration_s(self) -> float | None:
        if self._over_cap_since_epoch is None:
            return None
        return round(max(0.0, time.time() - self._over_cap_since_epoch), 3)

    def close(self) -> None:
        mcp_broker.remove_proxy_lease(
            self.connection_id,
            runtime_key=self.runtime_key,
            reason="proxy_exit",
        )


def _exec_legacy_server() -> None:
    env = os.environ.copy()
    env["LATCH_MCP_LEGACY"] = "1"
    server = Path(__file__).resolve().parent / "mcp_server.py"
    os.execve(sys.executable, [sys.executable, str(server)], env)


def main() -> int:
    if os.environ.get("LATCH_MCP_FORCE_LEGACY"):
        mcp_broker.emit_lifecycle("legacy_fallback", reason="forced_by_env")
        _exec_legacy_server()
        return 0
    try:
        metadata = connection_metadata()
    except ValueError as exc:
        sys.stderr.write(f"[latch] invalid MCP connection configuration: {exc}\n")
        return 2
    try:
        # Establish readiness before consuming stdin.  If startup fails, the
        # compatibility fallback can exec without losing the host's initialize
        # request.
        payload = mcp_broker.ensure_daemon(
            metadata["project_cwd"], start_reason="proxy_start"
        )
        owner_runtime_key = payload.get("owner_runtime_key")
        if isinstance(owner_runtime_key, str):
            metadata["runtime_key"] = owner_runtime_key
    except mcp_broker.BrokerError as exc:
        if os.environ.get("LATCH_MCP_ALLOW_LEGACY_FALLBACK"):
            mcp_broker.emit_lifecycle("legacy_fallback", reason=str(exc))
            sys.stderr.write(
                f"[latch] shared MCP daemon unavailable; explicit legacy fallback enabled: {exc}\n"
            )
            _exec_legacy_server()
        mcp_broker.emit_lifecycle("daemon_start_failed", reason=str(exc))
        sys.stderr.write(
            f"[latch] shared MCP daemon unavailable: {exc}. "
            "Run latch doctor; legacy mode is opt-in with "
            "LATCH_MCP_ALLOW_LEGACY_FALLBACK=1.\n"
        )
        return 1
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
