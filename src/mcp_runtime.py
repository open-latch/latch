"""Connection-local context for the shared latch MCP runtime.

The public MCP tool functions historically read process globals for cwd and
session attribution because every stdio client owned a dedicated process.  A
shared daemon serves several logical MCP connections in one process, so those
values must follow the connection instead.  Context variables keep the change
small and also propagate through AnyIO's worker-thread bridge for synchronous
FastMCP tools.

This module deliberately has no MCP, NumPy, ONNX, or tokenizer imports.  The
stdio proxy can import it without inheriting the heavyweight server runtime.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any, Iterator, Protocol


@dataclass(frozen=True)
class ConnectionContext:
    connection_id: str
    project_cwd: str
    session_id: str | None
    session_source: str
    proxy_pid: int
    proxy_started_at: str
    runtime_key: str


class RuntimeState(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def touch(self, connection_id: str | None = None) -> None: ...


_CONNECTION: ContextVar[ConnectionContext | None] = ContextVar(
    "latch_mcp_connection", default=None
)
_DAEMON_STATE: RuntimeState | None = None


@contextmanager
def bind_connection(context: ConnectionContext) -> Iterator[None]:
    token = _CONNECTION.set(context)
    try:
        yield
    finally:
        _CONNECTION.reset(token)


def current_connection() -> ConnectionContext | None:
    return _CONNECTION.get()


def connection_snapshot() -> dict[str, Any] | None:
    context = current_connection()
    return asdict(context) if context is not None else None


def set_daemon_state(state: RuntimeState | None) -> None:
    global _DAEMON_STATE
    _DAEMON_STATE = state


def daemon_snapshot() -> dict[str, Any] | None:
    state = _DAEMON_STATE
    return state.snapshot() if state is not None else None


def touch_daemon() -> None:
    """Count non-MCP owner activity such as prompt-hook embedding RPCs."""
    state = _DAEMON_STATE
    if state is not None:
        state.touch()
