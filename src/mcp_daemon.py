"""Shared multi-connection latch MCP daemon.

The daemon accepts a small authenticated prelude followed by ordinary newline-
delimited MCP JSON-RPC.  Each connection receives an independent MCP protocol
session while sharing one FastMCP registry, ONNX InferenceSession, tokenizer,
and warm embed listener.

This is an internal loopback transport.  Public hosts continue to speak the
standard stdio transport to ``mcp_proxy.py``; no host-specific protocol is
required.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any


_PROCESS_STARTED_MONOTONIC = time.monotonic()


def _daemonize_posix() -> bool:
    """Double-fork before heavyweight imports; return True in bootstrap parent.

    A plain detached ``Popen`` remains a child of the long-lived stdio proxy and
    becomes a zombie after idle reclamation.  Double-forking reparents the real
    daemon to the OS while the proxy synchronously reaps the short bootstrap.
    """
    first = os.fork()
    if first > 0:
        return True
    os.setsid()
    second = os.fork()
    if second > 0:
        os._exit(0)
    os.environ.pop("LATCH_MCP_DAEMONIZE", None)
    return False


if (
    __name__ == "__main__"
    and os.name != "nt"
    and os.environ.get("LATCH_MCP_DAEMONIZE")
):
    if _daemonize_posix():
        raise SystemExit(0)

import mcp_broker  # noqa: E402


_OWNER_FENCE = None
_REQUESTED_RUNTIME_KEY = os.environ.get("LATCH_MCP_RUNTIME_KEY") or mcp_broker.RUNTIME_KEY


def _requested_protocol_version() -> int:
    raw = os.environ.get("LATCH_MCP_PROTOCOL_VERSION")
    if raw is None:
        # Proxies from before the explicit handshake all spoke protocol v1.
        return 1
    try:
        return int(raw)
    except ValueError:
        return -1


def _requested_proxy_capability_epoch() -> int:
    raw = os.environ.get("LATCH_MCP_PROXY_CAPABILITY_EPOCH")
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return -1


def _publish_upgrade_alias(
    runtime_key: str,
    payload: dict[str, Any],
    *,
    capable: bool,
) -> None:
    compatibility = "migrate" if capable else "fresh_task_required"
    values = {
        "port": int(payload["port"]),
        "token": str(payload["token"]),
        "pid": int(payload["pid"]),
        "started_at": str(payload.get("started_at") or "unknown"),
        "runtime_key": runtime_key,
        "owner_runtime_key": mcp_broker.RUNTIME_KEY,
        "compatibility": compatibility,
    }
    mcp_broker.publish_discovery(**values)
    if not capable:
        mcp_broker.publish_discovery(**values, legacy_path=True)
    # Publish the matching embed alias so a retained-key process reaches the
    # owner's live embedder instead of its own stale embed.sock.json (KB id=1912).
    mcp_broker.publish_embed_alias(runtime_key)


def _alias_ready_owner(runtime_key: str, *, capable: bool) -> bool:
    """Point a retained proxy key at the ready current owner."""
    deadline = time.monotonic() + mcp_broker._start_timeout()
    while time.monotonic() < deadline:
        payload = mcp_broker.read_discovery()
        if payload is not None and mcp_broker.probe_discovery(payload):
            _publish_upgrade_alias(runtime_key, payload, capable=capable)
            mcp_broker.emit_lifecycle(
                "daemon_upgrade_alias_published",
                requested_runtime_key=runtime_key,
                owner_runtime_key=mcp_broker.RUNTIME_KEY,
                compatibility=("migrate" if capable else "fresh_task_required"),
            )
            return True
        time.sleep(0.05)
    return False


if __name__ == "__main__":
    try:
        mcp_broker.runtime_key_dir(_REQUESTED_RUNTIME_KEY)
    except ValueError as exc:
        sys.stderr.write(f"[latch] invalid requested MCP runtime key: {exc}\n")
        raise SystemExit(1)
    requested_protocol = _requested_protocol_version()
    if requested_protocol != mcp_broker.PROTOCOL_VERSION:
        message = (
            "Latch was upgraded across an incompatible MCP runtime protocol. "
            "Start a fresh task so the host launches a compatible proxy."
        )
        mcp_broker.publish_start_failure(_REQUESTED_RUNTIME_KEY, message)
        mcp_broker.emit_lifecycle(
            "daemon_upgrade_incompatible",
            requested_protocol=requested_protocol,
            current_protocol=mcp_broker.PROTOCOL_VERSION,
        )
        raise SystemExit(1)
    requested_capability = _requested_proxy_capability_epoch()
    if requested_capability > mcp_broker.PROXY_CAPABILITY_EPOCH:
        message = (
            "Latch proxy capability is newer than this runtime. Reinstall Latch "
            "and start a fresh task so the host launches a matching proxy."
        )
        mcp_broker.publish_start_failure(_REQUESTED_RUNTIME_KEY, message)
        mcp_broker.emit_lifecycle(
            "daemon_upgrade_incompatible",
            requested_proxy_capability_epoch=requested_capability,
            current_proxy_capability_epoch=mcp_broker.PROXY_CAPABILITY_EPOCH,
        )
        raise SystemExit(1)
    requested_capable = requested_capability >= mcp_broker.PROXY_CAPABILITY_EPOCH
    _OWNER_FENCE = mcp_broker.acquire_owner_fence()
    if _OWNER_FENCE is None:
        if (
            _REQUESTED_RUNTIME_KEY != mcp_broker.RUNTIME_KEY
            and _alias_ready_owner(
                _REQUESTED_RUNTIME_KEY, capable=requested_capable
            )
        ):
            raise SystemExit(0)
        mcp_broker.emit_lifecycle(
            "daemon_owner_conflict", reason="runtime owner fence already held"
        )
        if _REQUESTED_RUNTIME_KEY != mcp_broker.RUNTIME_KEY:
            mcp_broker.publish_start_failure(
                _REQUESTED_RUNTIME_KEY,
                "Latch was upgraded, but the compatible shared runtime did not become "
                "ready. Start a fresh task and run latch doctor if the problem persists.",
            )
        raise SystemExit(0)

import anyio  # noqa: E402
import mcp.types as mcp_types  # noqa: E402
from anyio.abc import SocketAttribute, SocketStream  # noqa: E402
from mcp.shared.message import SessionMessage  # noqa: E402

import mcp_runtime  # noqa: E402
import mcp_server  # noqa: E402


DEFAULT_IDLE_TTL_S = 60 * 60.0
MAX_PRELUDE_BYTES = 64 * 1024
MAX_MCP_LINE_BYTES = 4 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idle_ttl() -> float:
    raw = os.environ.get("LATCH_MCP_DAEMON_IDLE_TTL_SEC")
    try:
        return max(1.0, float(raw)) if raw is not None else DEFAULT_IDLE_TTL_S
    except ValueError:
        return DEFAULT_IDLE_TTL_S


def _cold_start_duration_ms() -> float:
    """Wall time from broker spawn request through model-ready publication."""
    try:
        requested = float(os.environ["LATCH_MCP_START_REQUEST_EPOCH"])
        return round(max(0.0, time.time() - requested) * 1000.0, 3)
    except (KeyError, ValueError):
        return round(
            (time.monotonic() - _PROCESS_STARTED_MONOTONIC) * 1000.0, 3
        )


class DaemonState:
    def __init__(self, *, started_at: str, idle_ttl_s: float):
        self.started_at = started_at
        self.idle_ttl_s = idle_ttl_s
        self._started_monotonic = time.monotonic()
        self._last_activity = self._started_monotonic
        self._lock = threading.Lock()
        self._connections: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, set[str]] = {}
        self._accepted = 0
        self._peak_connections = 0
        self._activity_generation = 0

    def _mark_activity_locked(self) -> None:
        self._last_activity = time.monotonic()
        self._activity_generation += 1

    def register(self, metadata: dict[str, Any]) -> str:
        requested = metadata.get("connection_id")
        connection_id = requested if isinstance(requested, str) and requested else uuid.uuid4().hex
        with self._lock:
            # A duplicated id should not merge leases.  This also prevents an
            # accidental client retry from erasing an existing live session.
            if connection_id in self._connections:
                connection_id = uuid.uuid4().hex
            self._mark_activity_locked()
            self._accepted += 1
            self._connections[connection_id] = {
                "session_source": metadata.get("session_source"),
            }
            self._peak_connections = max(
                self._peak_connections, len(self._connections)
            )
            self._pending[connection_id] = set()
        return connection_id

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            self._mark_activity_locked()
            self._connections.pop(connection_id, None)
            self._pending.pop(connection_id, None)

    def touch(self) -> None:
        with self._lock:
            self._mark_activity_locked()

    def request_started(self, connection_id: str, request_id: Any) -> None:
        with self._lock:
            self._mark_activity_locked()
            self._pending.setdefault(connection_id, set()).add(repr(request_id))

    def request_finished(self, connection_id: str, request_id: Any) -> None:
        with self._lock:
            self._mark_activity_locked()
            self._pending.setdefault(connection_id, set()).discard(repr(request_id))

    def idle_candidate(self) -> int | None:
        with self._lock:
            inflight = sum(len(items) for items in self._pending.values())
            idle = time.monotonic() - self._last_activity
            return self._activity_generation if inflight == 0 and idle >= self.idle_ttl_s else None

    def cancel_reclaim_if_unchanged(
        self, generation: int, cancel_scope: Any
    ) -> dict[str, Any] | None:
        """Atomically revalidate idleness and trigger cancellation.

        Activity between the first idle observation and this commit changes the
        generation, so a newly started request cannot be reclaimed.
        """
        with self._lock:
            now = time.monotonic()
            inflight = sum(len(items) for items in self._pending.values())
            idle = now - self._last_activity
            if (
                generation != self._activity_generation
                or inflight != 0
                or idle < self.idle_ttl_s
            ):
                return None
            snapshot = self._snapshot_locked(now)
            cancel_scope.cancel()
            return snapshot

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        sources: dict[str, int] = {}
        for item in self._connections.values():
            source = str(item.get("session_source") or "unavailable")
            sources[source] = sources.get(source, 0) + 1
        return {
            "mode": "shared_daemon",
            "pid": os.getpid(),
            "started_at": self.started_at,
            "uptime_s": round(now - self._started_monotonic, 3),
            "idle_ttl_s": self.idle_ttl_s,
            "last_activity_age_s": round(now - self._last_activity, 3),
            "active_connections": len(self._connections),
            "peak_connections": self._peak_connections,
            "inflight_requests": sum(len(items) for items in self._pending.values()),
            "connections_accepted": self._accepted,
            "session_sources": sources,
            "runtime_key": mcp_broker.RUNTIME_KEY,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(time.monotonic())


async def _read_line(
    stream: SocketStream,
    buffer: bytearray,
    *,
    limit: int,
) -> tuple[bytes, bytearray]:
    while b"\n" not in buffer:
        chunk = await stream.receive(65536)
        if not chunk:
            raise anyio.EndOfStream
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise ValueError("line exceeds runtime limit")
    line, _, rest = buffer.partition(b"\n")
    return bytes(line), bytearray(rest)


def _context_from(metadata: dict[str, Any], connection_id: str) -> mcp_runtime.ConnectionContext:
    project_cwd = metadata.get("project_cwd")
    if not isinstance(project_cwd, str) or not project_cwd:
        project_cwd = os.environ.get("LATCH_MCP_INITIAL_PROJECT_CWD") or os.getcwd()
    session_id = metadata.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        session_id = None
    proxy_pid = metadata.get("proxy_pid")
    if not isinstance(proxy_pid, int):
        proxy_pid = -1
    return mcp_runtime.ConnectionContext(
        connection_id=connection_id,
        project_cwd=os.path.abspath(project_cwd),
        session_id=session_id,
        session_source=str(metadata.get("session_source") or "unavailable"),
        proxy_pid=proxy_pid,
        proxy_started_at=str(metadata.get("proxy_started_at") or "unknown"),
        runtime_key=str(metadata.get("runtime_key") or "unknown"),
    )


async def _run_mcp_connection(
    stream: SocketStream,
    initial: bytearray,
    metadata: dict[str, Any],
    state: DaemonState,
) -> None:
    connection_id = state.register(metadata)
    context = _context_from(metadata, connection_id)
    read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](0)

    async def reader() -> None:
        buffer = initial
        try:
            async with read_send:
                while True:
                    line, buffer = await _read_line(
                        stream, buffer, limit=MAX_MCP_LINE_BYTES
                    )
                    try:
                        message = mcp_types.JSONRPCMessage.model_validate_json(line)
                    except Exception as exc:
                        await read_send.send(exc)
                        continue
                    payload = message.model_dump(by_alias=True, exclude_none=True)
                    if isinstance(payload, dict) and "method" in payload and "id" in payload:
                        state.request_started(connection_id, payload["id"])
                    else:
                        state.touch()
                    await read_send.send(SessionMessage(message))
        except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
            return

    async def writer() -> None:
        try:
            async with write_receive:
                async for session_message in write_receive:
                    await _send_session_message(
                        stream, state, connection_id, session_message
                    )
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(reader)
            tg.start_soon(writer)
            with mcp_runtime.bind_connection(context):
                await mcp_server.run_shared_session(read_receive, write_send)
            tg.cancel_scope.cancel()
    finally:
        state.unregister(connection_id)
        try:
            await stream.aclose()
        except Exception:
            pass


_FRESH_TASK_MESSAGE = (
    "This Latch proxy predates the shared-runtime capability epoch and cannot "
    "safely join the current owner. Start a fresh task so the host launches the "
    "updated proxy; the request was not executed."
)


async def _run_fresh_task_rejection(
    stream: SocketStream,
    initial: bytearray,
    metadata: dict[str, Any],
    state: DaemonState,
) -> None:
    """Complete initialize, then reject real work with an actionable error.

    Pre-capability proxies discard an initialize error during reconnect. A
    minimal successful initialize lets their replay finish so the deferred
    request itself receives the bounded fresh-task error. No tool reaches
    FastMCP, so unknown mutation outcomes remain impossible on this path.
    """
    connection_id = state.register(metadata)
    buffer = initial
    try:
        while True:
            line, buffer = await _read_line(
                stream, buffer, limit=MAX_MCP_LINE_BYTES
            )
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            method = message.get("method")
            state.touch()
            if method == "initialize" and "id" in message:
                params = message.get("params")
                protocol_version = (
                    params.get("protocolVersion")
                    if isinstance(params, dict)
                    else "2024-11-05"
                )
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": protocol_version,
                        "capabilities": {},
                        "serverInfo": {
                            "name": "latch",
                            "version": "fresh-task-required",
                        },
                    },
                }
                await stream.send(
                    json.dumps(response, separators=(",", ":")).encode() + b"\n"
                )
            elif "id" in message:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32002, "message": _FRESH_TASK_MESSAGE},
                }
                await stream.send(
                    json.dumps(response, separators=(",", ":")).encode() + b"\n"
                )
                mcp_broker.emit_lifecycle(
                    "proxy_upgrade_fresh_task_required",
                    requested_runtime_key=metadata.get("runtime_key"),
                    requested_proxy_capability_epoch=metadata.get(
                        "proxy_capability_epoch", 0
                    ),
                )
    finally:
        state.unregister(connection_id)


async def _send_session_message(
    stream: SocketStream,
    state: DaemonState,
    connection_id: str,
    session_message: SessionMessage,
) -> None:
    """Deliver first, then clear request state.

    A failed transport send leaves the request pending until connection teardown,
    preventing idle reclamation during the response-delivery window.
    """
    payload = session_message.message.model_dump(by_alias=True, exclude_none=True)
    line = session_message.message.model_dump_json(
        by_alias=True, exclude_none=True
    ).encode("utf-8")
    await stream.send(line + b"\n")
    if isinstance(payload, dict) and "id" in payload and (
        "result" in payload or "error" in payload
    ):
        state.request_finished(connection_id, payload["id"])
    else:
        state.touch()


async def _handle_connection(stream: SocketStream, state: DaemonState, token: str) -> None:
    buffer = bytearray()
    try:
        line, buffer = await _read_line(stream, buffer, limit=MAX_PRELUDE_BYTES)
        metadata = json.loads(line.decode("utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("prelude must be an object")
        supplied = str(metadata.get("token") or "")
        if not secrets.compare_digest(supplied, token):
            await stream.send(b'{"ok":false,"error":"unauthorized"}\n')
            return
        if metadata.get("protocol") != mcp_broker.PROTOCOL_VERSION:
            await stream.send(b'{"ok":false,"error":"protocol_mismatch"}\n')
            return
        runtime_key = metadata.get("runtime_key")
        # The private discovery token authenticates compatible aliases; the key
        # remains connection attribution rather than a second security secret.
        if not isinstance(runtime_key, str):
            await stream.send(b'{"ok":false,"error":"runtime_mismatch"}\n')
            return
        op = metadata.get("op")
        if op == "probe":
            state.touch()
            response = {"ok": True, "pid": os.getpid(), "runtime_key": mcp_broker.RUNTIME_KEY}
            await stream.send(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            return
        if op != "mcp":
            await stream.send(b'{"ok":false,"error":"unknown_operation"}\n')
            return
        capability_epoch = metadata.get("proxy_capability_epoch")
        if not isinstance(capability_epoch, int):
            capability_epoch = 0
        if capability_epoch < mcp_broker.PROXY_CAPABILITY_EPOCH:
            await _run_fresh_task_rejection(stream, buffer, metadata, state)
            return
        await _run_mcp_connection(stream, buffer, metadata, state)
    except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
        return
    except Exception as exc:
        sys.stderr.write(f"[latch] shared MCP connection failed: {exc}\n")
    finally:
        try:
            await stream.aclose()
        except Exception:
            pass


async def _idle_monitor(state: DaemonState, cancel_scope: anyio.CancelScope) -> None:
    interval = min(5.0, max(0.25, state.idle_ttl_s / 4.0))
    while True:
        await anyio.sleep(interval)
        generation = state.idle_candidate()
        if generation is not None:
            snapshot = state.cancel_reclaim_if_unchanged(generation, cancel_scope)
            if snapshot is None:
                continue
            mcp_broker.emit_lifecycle(
                "daemon_idle_exit",
                reason="idle_ttl",
                idle_ttl_s=state.idle_ttl_s,
                uptime_s=snapshot["uptime_s"],
                idle_duration_s=snapshot["last_activity_age_s"],
                peak_connections=snapshot["peak_connections"],
                connections_accepted=snapshot["connections_accepted"],
            )
            return


async def _main_async() -> None:
    global _OWNER_FENCE
    if _OWNER_FENCE is None:
        _OWNER_FENCE = mcp_broker.acquire_owner_fence()
        if _OWNER_FENCE is None:
            raise RuntimeError("runtime owner fence already held")
    started_at = _utc_now()
    token = secrets.token_hex(32)
    state = DaemonState(started_at=started_at, idle_ttl_s=_idle_ttl())
    mcp_runtime.set_daemon_state(state)
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    host, port = listener.extra(SocketAttribute.local_address)
    if host != "127.0.0.1":
        raise RuntimeError(f"shared MCP daemon bound unexpected host {host!r}")

    initialized = False
    try:
        initial_cwd = os.environ.get("LATCH_MCP_INITIAL_PROJECT_CWD") or os.getcwd()
        mcp_server.initialize_runtime(initial_cwd, start_embed_listener=True)
        initialized = True
        async with anyio.create_task_group() as tg:
            mcp_broker.publish_discovery(
                port=int(port), token=token, pid=os.getpid(), started_at=started_at
            )
            if _REQUESTED_RUNTIME_KEY != mcp_broker.RUNTIME_KEY:
                _publish_upgrade_alias(
                    _REQUESTED_RUNTIME_KEY,
                    {
                        "port": port,
                        "token": token,
                        "pid": os.getpid(),
                        "started_at": started_at,
                    },
                    capable=(
                        _requested_proxy_capability_epoch()
                        >= mcp_broker.PROXY_CAPABILITY_EPOCH
                    ),
                )
            mcp_broker.emit_lifecycle(
                "daemon_started",
                pid=os.getpid(),
                reason=str(os.environ.get("LATCH_MCP_START_REASON") or "unknown"),
                cold_start_duration_ms=_cold_start_duration_ms(),
                idle_ttl_s=state.idle_ttl_s,
                upgrade_alias=(_REQUESTED_RUNTIME_KEY != mcp_broker.RUNTIME_KEY),
            )
            tg.start_soon(_idle_monitor, state, tg.cancel_scope)
            await listener.serve(lambda stream: _handle_connection(stream, state, token))
    except Exception:
        if not initialized:
            mcp_broker.publish_start_failure(
                _REQUESTED_RUNTIME_KEY,
                "Latch upgraded but the compatible shared runtime failed to "
                "initialize. Start a fresh task and run latch doctor for the "
                "startup details.",
            )
        raise
    finally:
        mcp_runtime.set_daemon_state(None)
        if initialized:
            mcp_server.shutdown_runtime()
        mcp_broker.remove_discovery_aliases_if_owner(pid=os.getpid(), token=token)
        await listener.aclose()


def main() -> int:
    global _OWNER_FENCE
    try:
        anyio.run(_main_async)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        mcp_broker.emit_lifecycle("daemon_failed", reason=str(exc))
        sys.stderr.write(f"[latch] shared MCP daemon failed: {exc}\n")
        return 1
    finally:
        if _OWNER_FENCE is not None:
            _OWNER_FENCE.close()
            _OWNER_FENCE = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
