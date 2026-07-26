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

import json
import os
import shutil
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator, Protocol


SUPPORTED_MODEL_BACKENDS = frozenset({"claude", "codex", "cursor"})
DEFAULT_GATE_CLASSIFIER_TIMEOUT_S = 300
DEFAULT_GATE_ADVERSARY_TIMEOUT_S = 120
DEFAULT_PROXY_CAP = 32
DEFAULT_PROXY_RETIRE_IDLE_S = 5 * 60.0
DEFAULT_PROXY_HEARTBEAT_S = 30.0
DEFAULT_PROXY_STALE_S = 5 * 60.0
WINDOWS_VENV_SITE_PACKAGES_ENV = "LATCH_MCP_VENV_SITE_PACKAGES"
PROCESS_OS_ENV_VARS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "USERNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
)
CONNECTION_CHILD_COMMON_ENV_VARS = PROCESS_OS_ENV_VARS + (
    # Per-client config/cache roots.
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    # Network and trust configuration used by model CLIs.
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
CONNECTION_CHILD_BACKEND_ENV_VARS = {
    "claude": (
        "CLAUDE_CONFIG_DIR",
        # Exact supported credential names. Never admit wildcard env families.
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLOUD_ML_REGION",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_BIN",
    ),
    "codex": (
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_BIN",
        "LATCH_GATE_CODEX_MODEL",
        "CODEX_GATE_MODEL",
        "LATCH_HEAL_CODEX_MODEL",
        "CODEX_HEAL_MODEL",
        "LATCH_MAINTENANCE_CODEX_MODEL",
        "CODEX_MAINTENANCE_MODEL",
        "LATCH_TREE_CODEX_MODEL",
        "CODEX_TREE_MODEL",
    ),
    "cursor": (
        "CURSOR_API_KEY",
        "CURSOR_AGENT_BIN",
        "LATCH_GATE_CURSOR_MODEL",
        "CURSOR_GATE_MODEL",
        "LATCH_CURSOR_MODEL",
        "CURSOR_MODEL",
    ),
}
CONNECTION_CHILD_ENV_VARS = CONNECTION_CHILD_COMMON_ENV_VARS + tuple(
    name
    for backend_names in CONNECTION_CHILD_BACKEND_ENV_VARS.values()
    for name in backend_names
)
SENSITIVE_CHILD_ENV_VARS = frozenset({
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_BASE_URL",
    "CURSOR_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
})
CONNECTION_CHILD_ENV_MAX_VALUE_BYTES = 8 * 1024
CONNECTION_CHILD_ENV_MAX_JSON_BYTES = 32 * 1024
_CONNECTION_CHILD_ENV_SET = frozenset(CONNECTION_CHILD_ENV_VARS)
_CONNECTION_BINARY_ENV_SET = frozenset({
    "CLAUDE_BIN",
    "CODEX_BIN",
    "CURSOR_AGENT_BIN",
})
_VAULT_CHILD_ENV_VARS = (
    "LATCH_HOME",
    "LATCH_KB_DIR",
    "LATCH_PRODUCTION_DATA_ROOT",
    "LATCH_VAULT_REGISTRY_ROOT",
    "LATCH_DURABILITY_ROOT",
    "LATCH_TEST_ROOT",
    "LATCH_TEST_CAPABILITY",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)


@dataclass(frozen=True)
class ConnectionContext:
    connection_id: str
    project_cwd: str
    session_id: str | None
    session_source: str
    proxy_pid: int
    proxy_started_at: str
    runtime_key: str
    in_compact: bool = False
    unlatched: bool = False
    disabled: bool = False
    write_disabled: bool = False
    in_maintenance: bool = False
    gate_backend: str = "claude"
    maintenance_backend: str = "claude"
    gate_classifier_timeout_s: int = DEFAULT_GATE_CLASSIFIER_TIMEOUT_S
    gate_adversary_timeout_s: int = DEFAULT_GATE_ADVERSARY_TIMEOUT_S
    gate_adversary_enabled: bool = True
    proxy_cap: int = DEFAULT_PROXY_CAP
    proxy_retire_idle_s: float = DEFAULT_PROXY_RETIRE_IDLE_S
    proxy_heartbeat_s: float = DEFAULT_PROXY_HEARTBEAT_S
    proxy_stale_s: float = DEFAULT_PROXY_STALE_S


@dataclass(frozen=True, repr=False)
class PrivateChildEnvironment:
    values: tuple[tuple[str, str], ...] = field(default_factory=tuple)


class RuntimeState(Protocol):
    def snapshot(self) -> dict[str, Any]: ...
    def touch(self) -> None: ...


_CONNECTION: ContextVar[ConnectionContext | None] = ContextVar(
    "latch_mcp_connection", default=None
)
_PRIVATE_CHILD_ENV: ContextVar[PrivateChildEnvironment | None] = ContextVar(
    "latch_mcp_private_child_environment", default=None
)
_DAEMON_STATE: RuntimeState | None = None


@contextmanager
def bind_connection(
    context: ConnectionContext,
    *,
    child_environment: PrivateChildEnvironment | None = None,
) -> Iterator[None]:
    token = _CONNECTION.set(context)
    private_token = _PRIVATE_CHILD_ENV.set(
        child_environment or PrivateChildEnvironment()
    )
    try:
        yield
    finally:
        _PRIVATE_CHILD_ENV.reset(private_token)
        _CONNECTION.reset(token)


def current_connection() -> ConnectionContext | None:
    return _CONNECTION.get()


def resolve_executable_on_path(command: str, path: str | None) -> str | None:
    """Resolve without allowing Windows cwd precedence outside explicit PATH."""
    resolved = shutil.which(command, path=path)
    if resolved is None:
        return None
    if os.path.isabs(command) or os.path.dirname(command):
        return os.path.abspath(resolved)
    if path is None:
        return None

    def identity(value: str) -> str:
        return os.path.normcase(
            os.path.realpath(os.path.abspath(value.strip().strip('"')))
        )

    allowed_parents = {
        identity(entry or os.curdir)
        for entry in path.split(os.pathsep)
    }
    if identity(os.path.dirname(resolved) or os.curdir) not in allowed_parents:
        return None
    return os.path.abspath(resolved)


def connection_snapshot() -> dict[str, Any] | None:
    context = current_connection()
    if context is None:
        return None
    return asdict(context)


def validate_child_environment(
    value: Any,
    *,
    allowed_backends: frozenset[str] | None = None,
) -> PrivateChildEnvironment:
    """Validate and freeze the proxy's exact child-process environment map."""
    if not isinstance(value, dict):
        raise ValueError("invalid child_environment connection metadata")
    allowed_names = _CONNECTION_CHILD_ENV_SET
    if allowed_backends is not None:
        unsupported = allowed_backends - SUPPORTED_MODEL_BACKENDS
        if unsupported:
            raise ValueError("unsupported child_environment backend scope")
        allowed_names = frozenset(CONNECTION_CHILD_COMMON_ENV_VARS).union(
            *(
                CONNECTION_CHILD_BACKEND_ENV_VARS[backend]
                for backend in allowed_backends
            )
        )
    normalized: dict[str, str] = {}
    for name, raw in value.items():
        if not isinstance(name, str) or name not in allowed_names:
            raise ValueError("unsupported child_environment variable")
        if not isinstance(raw, str) or "\0" in raw:
            raise ValueError(f"invalid child_environment value for {name}")
        if name in _CONNECTION_BINARY_ENV_SET and (
            not raw or not os.path.isabs(raw)
        ):
            raise ValueError(
                f"child_environment executable {name} must be absolute"
            )
        if len(raw.encode("utf-8")) > CONNECTION_CHILD_ENV_MAX_VALUE_BYTES:
            raise ValueError(f"child_environment value for {name} is too large")
        normalized[name] = raw
    encoded = json.dumps(
        normalized, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > CONNECTION_CHILD_ENV_MAX_JSON_BYTES:
        raise ValueError("child_environment connection metadata is too large")
    return PrivateChildEnvironment(tuple(sorted(normalized.items())))


def connection_env_value(name: str) -> str | None:
    """Read a child setting from the current connection, never the first owner."""
    context = current_connection()
    if context is None:
        return os.environ.get(name)
    private = _PRIVATE_CHILD_ENV.get() or PrivateChildEnvironment()
    for candidate, value in private.values:
        if candidate == name:
            return value
    return None


def connection_binary(name: str, *, process_default: str) -> str:
    """Return a proxy-resolved executable, failing closed in shared mode.

    A bare command in the daemon can be resolved against the long-lived
    owner's cwd or PATH instead of the invoking connection's environment
    (notably on Windows). Proxies therefore resolve executable paths before
    connecting; absence is an explicit unavailable result, never permission to
    fall back to daemon-global command lookup.
    """
    if current_connection() is None:
        return process_default
    value = connection_env_value(name)
    if value and os.path.isabs(value):
        return value
    raise FileNotFoundError(
        f"{name} was not resolved to an absolute executable by this MCP connection"
    )


def connection_subprocess_environment(backend: str | None = None) -> dict[str, str]:
    """Build the selected model/maintenance child environment.

    Contextless legacy/CLI paths retain their historical process environment.
    Shared connections start from their validated exact map plus canonical
    vault identity, so absent values cannot fall through to the first proxy
    that happened to spawn the owner.
    """
    context = current_connection()
    if context is None:
        return os.environ.copy()
    resolved_backend = backend or context.maintenance_backend
    if resolved_backend not in SUPPORTED_MODEL_BACKENDS:
        raise ValueError(f"unsupported child-process backend {resolved_backend!r}")
    allowed = frozenset(CONNECTION_CHILD_COMMON_ENV_VARS).union(
        CONNECTION_CHILD_BACKEND_ENV_VARS[resolved_backend]
    )
    private = _PRIVATE_CHILD_ENV.get() or PrivateChildEnvironment()
    env = {
        name: value
        for name, value in private.values
        if name in allowed
    }
    for name in _VAULT_CHILD_ENV_VARS:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def autonomous_subprocess_environment() -> dict[str, str]:
    """Build background-child plumbing without connection-private settings."""
    if current_connection() is None:
        return os.environ.copy()
    if os.name == "nt":
        folded = {str(key).upper(): value for key, value in os.environ.items()}
        env = {
            name: folded[name.upper()]
            for name in PROCESS_OS_ENV_VARS
            if isinstance(folded.get(name.upper()), str)
        }
    else:
        env = {
            name: os.environ[name]
            for name in PROCESS_OS_ENV_VARS
            if isinstance(os.environ.get(name), str)
        }
    for name in _VAULT_CHILD_ENV_VARS:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env


def redact_subprocess_output(text: str) -> str:
    """Remove connection credentials and credential-bearing proxy URLs."""
    if not text:
        return text
    private = _PRIVATE_CHILD_ENV.get()
    values = (
        dict(private.values)
        if private is not None
        else {name: os.environ.get(name) for name in SENSITIVE_CHILD_ENV_VARS}
    )
    sensitive = sorted(
        {
            value
            for name, value in values.items()
            if name in SENSITIVE_CHILD_ENV_VARS and isinstance(value, str) and value
        },
        key=len,
        reverse=True,
    )
    redacted = text
    for value in sensitive:
        redacted = redacted.replace(value, "<redacted>")
    return redacted


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
