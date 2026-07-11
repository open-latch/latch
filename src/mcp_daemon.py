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

import anyio  # noqa: E402
import mcp.types as mcp_types  # noqa: E402
from anyio.abc import SocketAttribute, SocketStream  # noqa: E402
from mcp.shared.message import SessionMessage  # noqa: E402

import mcp_broker  # noqa: E402
import mcp_runtime  # noqa: E402
import mcp_server  # noqa: E402


DEFAULT_IDLE_TTL_S = 60 * 60.0
MAX_PRELUDE_BYTES = 64 * 1024
MAX_MCP_LINE_BYTES = 4 * 1024 * 1024
_TEST_DROP_RESPONSE_USED = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _idle_ttl() -> float:
    raw = os.environ.get("LATCH_MCP_DAEMON_IDLE_TTL_SEC")
    try:
        return max(1.0, float(raw)) if raw is not None else DEFAULT_IDLE_TTL_S
    except ValueError:
        return DEFAULT_IDLE_TTL_S


def _drop_response_for_test(payload: dict[str, Any]) -> bool:
    """Deterministic post-handler failure seam used by the integration test.

    The response has already been produced (and a mutating handler committed)
    when the writer sees it.  No production behavior changes unless the
    explicitly test-scoped environment variable is set.
    """
    global _TEST_DROP_RESPONSE_USED
    wanted = os.environ.get("LATCH_MCP_TEST_DROP_RESPONSE_ID_ONCE")
    if _TEST_DROP_RESPONSE_USED or not wanted or "id" not in payload:
        return False
    if str(payload["id"]) != wanted:
        return False
    _TEST_DROP_RESPONSE_USED = True
    return True


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

    def register(self, metadata: dict[str, Any]) -> str:
        requested = metadata.get("connection_id")
        connection_id = requested if isinstance(requested, str) and requested else uuid.uuid4().hex
        with self._lock:
            # A duplicated id should not merge leases.  This also prevents an
            # accidental client retry from erasing an existing live session.
            if connection_id in self._connections:
                connection_id = uuid.uuid4().hex
            now = time.monotonic()
            self._last_activity = now
            self._accepted += 1
            self._connections[connection_id] = {
                "connected_at": _utc_now(),
                "project_cwd": metadata.get("project_cwd"),
                "session_source": metadata.get("session_source"),
                "proxy_pid": metadata.get("proxy_pid"),
            }
            self._pending[connection_id] = set()
        return connection_id

    def unregister(self, connection_id: str) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            self._connections.pop(connection_id, None)
            self._pending.pop(connection_id, None)

    def touch(self, connection_id: str | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            self._last_activity = now

    def request_started(self, connection_id: str, request_id: Any) -> None:
        with self._lock:
            now = time.monotonic()
            self._last_activity = now
            self._pending.setdefault(connection_id, set()).add(repr(request_id))

    def request_finished(self, connection_id: str, request_id: Any) -> None:
        with self._lock:
            self._last_activity = time.monotonic()
            self._pending.setdefault(connection_id, set()).discard(repr(request_id))

    def should_reclaim(self) -> bool:
        with self._lock:
            inflight = sum(len(items) for items in self._pending.values())
            idle = time.monotonic() - self._last_activity
            return inflight == 0 and idle >= self.idle_ttl_s

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
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
                "inflight_requests": sum(len(items) for items in self._pending.values()),
                "connections_accepted": self._accepted,
                "session_sources": sources,
                "runtime_key": mcp_broker.RUNTIME_KEY,
            }


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
                        state.touch(connection_id)
                    await read_send.send(SessionMessage(message))
        except (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError):
            return

    async def writer() -> None:
        try:
            async with write_receive:
                async for session_message in write_receive:
                    payload = session_message.message.model_dump(
                        by_alias=True, exclude_none=True
                    )
                    if isinstance(payload, dict) and "id" in payload and (
                        "result" in payload or "error" in payload
                    ):
                        state.request_finished(connection_id, payload["id"])
                    else:
                        state.touch(connection_id)
                    if isinstance(payload, dict) and _drop_response_for_test(payload):
                        await stream.aclose()
                        return
                    line = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    ).encode("utf-8")
                    await stream.send(line + b"\n")
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
        if metadata.get("runtime_key") != mcp_broker.RUNTIME_KEY:
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
        if state.should_reclaim():
            snapshot = state.snapshot()
            mcp_broker.emit_lifecycle(
                "daemon_idle_exit",
                idle_ttl_s=state.idle_ttl_s,
                uptime_s=snapshot["uptime_s"],
                connections_accepted=snapshot["connections_accepted"],
            )
            cancel_scope.cancel()
            return


async def _main_async() -> None:
    expected_key = os.environ.get("LATCH_MCP_RUNTIME_KEY")
    if expected_key and expected_key != mcp_broker.RUNTIME_KEY:
        raise RuntimeError("proxy and daemon runtime keys differ")

    started_at = _utc_now()
    token = secrets.token_hex(32)
    state = DaemonState(started_at=started_at, idle_ttl_s=_idle_ttl())
    mcp_runtime.set_daemon_state(state)
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    host, port = listener.extra(SocketAttribute.local_address)
    if host != "127.0.0.1":
        raise RuntimeError(f"shared MCP daemon bound unexpected host {host!r}")

    # Preserve current warm latency, but pay the model cost once per vault
    # instead of once per host-created stdio process.
    initial_cwd = os.environ.get("LATCH_MCP_INITIAL_PROJECT_CWD") or os.getcwd()
    mcp_server.initialize_runtime(initial_cwd, start_embed_listener=True)
    mcp_broker.publish_discovery(
        port=int(port), token=token, pid=os.getpid(), started_at=started_at
    )
    mcp_broker.emit_lifecycle(
        "daemon_started", pid=os.getpid(), idle_ttl_s=state.idle_ttl_s
    )

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_idle_monitor, state, tg.cancel_scope)
            await listener.serve(lambda stream: _handle_connection(stream, state, token))
    finally:
        mcp_runtime.set_daemon_state(None)
        mcp_server.shutdown_runtime()
        mcp_broker.remove_discovery_if_owner(pid=os.getpid(), token=token)
        await listener.aclose()


def main() -> int:
    try:
        anyio.run(_main_async)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        mcp_broker.emit_lifecycle("daemon_failed", reason=str(exc))
        sys.stderr.write(f"[latch] shared MCP daemon failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
