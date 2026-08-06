"""SCM-independent Latch scope identity and machine-local authorization.

The portable declaration at ``<root>/.latch/scope.json`` contains only a
stable scope UUID and ``shared``/``private`` policy.  It never contains a
machine path.  Latch-owned control state separately records:

* which canonical roots this machine has explicitly authorized; and
* the single exact KB target, mode, and revision for each scope UUID.

The split is deliberate.  A copied declaration or fresh clone is not a
capability and remains LOCKED until that root is authorized locally.  A local
authorization whose declaration is missing or changed also remains a LOCKED
boundary instead of falling through to an outer/global KB.  Every authorized
alias of one scope UUID reads the same central binding, so aliases cannot drift
onto different vaults.

This module is standard-library-only because hooks and the MCP proxy resolve a
scope before importing the heavier runtime.  Git helpers at the bottom are
compatibility/discovery helpers only; no correctness path depends on Git.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import contextlib
import functools
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import tempfile
import uuid


FORMAT_VERSION = 1
PORTABLE_DIR_NAME = ".latch"
PORTABLE_FILE_NAME = "scope.json"
CONTROL_ROOT_ENV = "LATCH_SCOPE_STATE_ROOT"
POLICY_FILE_NAME = "policy.json"
COMPATIBILITY_BINDING_FILE_NAME = "compatibility-global.json"
ROOTS_DIR_NAME = "roots"
SCOPES_DIR_NAME = "scopes"
LOCKS_DIR_NAME = "locks"
SESSIONS_DIR_NAME = "sessions"
ROOT_STATE_DIR_NAME = "root-state"
RUNTIME_DIR_NAME = "runtime"
UNLATCH_STATE_FILE_NAME = "instruction-state.json"
CONTINUITY_EPOCH_FILE_NAME = "continuity-epoch.json"
TRANSITION_LOCK_FILE_NAME = "transition.lock"

ROOT_KIND_SCOPE = "scope"
ROOT_KIND_OFF = "off"

POLICY_SHARED = "shared"
POLICY_PRIVATE = "private"
POLICIES = frozenset({POLICY_SHARED, POLICY_PRIVATE})

MODE_LATCHED = "latched"
MODE_UNLATCHED = "unlatched"
MODE_LOCKED = "locked"
MODES = frozenset({MODE_LATCHED, MODE_UNLATCHED})

LOCK_UNAUTHORIZED_ROOT = "unauthorized-root"
LOCK_GLOBAL_PIN_CHANGED = "global-pin-changed"
LOCK_VAULT_IDENTITY_PENDING = "vault-identity-pending"
LOCK_VAULT_IDENTITY_INITIALIZING = "vault-identity-initializing"
LOCK_OUTSIDE_SCOPE = "outside-scope"
LOCK_PRIVATE_ANCESTOR = "private-ancestor"
LOCK_INTERRUPTED_OFF_REPLACEMENT = "interrupted-off-replacement"

SOURCE_EXPLICIT = "explicit"
SOURCE_OFF_BOUNDARY = "off-boundary"
SOURCE_COMPATIBILITY = "compatibility"

MACHINE_POLICY_EXPLICIT = "explicit"
MACHINE_POLICY_COMPATIBILITY = "compatibility_global"
MACHINE_POLICIES = frozenset(
    {MACHINE_POLICY_EXPLICIT, MACHINE_POLICY_COMPATIBILITY}
)

TEST_ROOT_ENV = "LATCH_TEST_ROOT"
TEST_CAPABILITY_ENV = "LATCH_TEST_CAPABILITY"
TEST_SENTINEL = ".latch-test-root.json"

# Compatibility names for code being migrated off the draft PR architecture.
STATE_DIR_NAME = PORTABLE_DIR_NAME
BINDING_FILE_NAME = PORTABLE_FILE_NAME
KB_TARGET_MARKER_FILE_NAME = ".latch-kb-target.json"
DISABLED_RUNTIME_DIR_NAME = "unlatched-runtime"

AGENT_SESSION_ENV_VARS = (
    "LATCH_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
)
AGENT_ADAPTERS = frozenset({"claude", "codex", "cursor", "vscode"})
CODEX_BACKEND_ENV_VARS = (
    "LATCH_MODEL_BACKEND",
    "LATCH_GATE_BACKEND",
    "CLAUDE_KB_GATE_BACKEND",
    "LATCH_MAINTENANCE_BACKEND",
    "CLAUDE_KB_MAINTENANCE_BACKEND",
    "LATCH_COMPACTOR_BACKEND",
    "CLAUDE_KB_COMPACTOR_BACKEND",
    "CODEX_KB_COMPACTOR_BACKEND",
    "CURSOR_KB_COMPACTOR_BACKEND",
)


class ProjectConfigError(RuntimeError):
    """A scope declaration, local authorization, or target is unsafe."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class ProjectTransitionBusyError(ProjectConfigError):
    """Another latch/unlatch transition currently owns this root."""


@dataclass(frozen=True)
class ScopeMarker:
    project_root: Path
    marker_path: Path
    scope_id: str
    policy: str
    fingerprint: str


@dataclass(frozen=True)
class RootAuthorization:
    project_root: Path
    record_path: Path
    kind: str
    revision: str
    scope_id: str | None
    policy: str | None
    marker_fingerprint: str | None
    remembered_scope_id: str | None
    remembered_revision: str | None
    remembered_kb_dir: Path | None
    remembered_target_fingerprint: str | None
    remembered_vault_uuid: str | None


@dataclass(frozen=True)
class ScopeBinding:
    scope_id: str
    record_path: Path
    policy: str
    mode: str
    kb_dir: Path
    kb_fingerprint: str
    vault_uuid: str | None
    revision: str


@dataclass(frozen=True)
class CompatibilityBinding:
    """Exact machine-local identity for an upgraded global-KB install."""

    record_path: Path
    kb_dir: Path
    kb_fingerprint: str
    vault_uuid: str | None
    revision: str


@dataclass(frozen=True, kw_only=True)
class ResolvedScope:
    """The complete effective scope.  LOCKED is data, never absence."""

    project_root: Path
    state: str
    policy: str | None
    scope_id: str | None
    target_revision: str
    revision: str
    kb_dir: Path | None
    remembered_kb_dir: Path | None
    target_fingerprint: str | None
    vault_uuid: str | None
    marker_path: Path | None
    source: str
    lock_key: str
    reason: str | None = None
    reason_code: str | None = None

    @property
    def mode(self) -> str:
        return self.state

    @property
    def is_latched(self) -> bool:
        return self.state == MODE_LATCHED

    @property
    def state_dir(self) -> Path:
        return state_dir(self.project_root)

    @property
    def disabled_runtime_dir(self) -> Path:
        return control_root() / RUNTIME_DIR_NAME / self.lock_key


# Compatibility alias used by downstream code while it moves to ResolvedScope.
ProjectBinding = ResolvedScope


def current_agent_session_id(
    env: Mapping[str, str] | None = None,
) -> str | None:
    source = os.environ if env is None else env
    return next(
        (
            value.strip()
            for name in AGENT_SESSION_ENV_VARS
            if (value := source.get(name)) and value.strip()
        ),
        None,
    )


def is_agent_context(env: Mapping[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    adapter = (source.get("LATCH_ADAPTER") or "").strip().lower()
    configured_agent_backend = any(
        (source.get(name) or "").strip().lower() in AGENT_ADAPTERS
        for name in CODEX_BACKEND_ENV_VARS
    )
    return bool(
        current_agent_session_id(source)
        or (source.get("CLAUDECODE") or "").strip()
        or (source.get("CODEX_HOME") or "").strip()
        or (source.get("CURSOR_PLUGIN_ROOT") or "").strip()
        or adapter in AGENT_ADAPTERS
        or configured_agent_backend
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        _same_path(left, right)
        or _is_relative_to(left, right)
        or _is_relative_to(right, left)
    )


def _validated_test_root(
    env: Mapping[str, str] | None = None,
) -> Path | None:
    values = os.environ if env is None else env
    raw_root = values.get(TEST_ROOT_ENV)
    capability = values.get(TEST_CAPABILITY_ENV)
    if raw_root is None and capability is None:
        return None
    if not raw_root or not capability:
        raise ProjectConfigError(
            "incomplete Latch test capability; refusing scope-state resolution"
        )
    try:
        root = Path(raw_root).resolve(strict=True)
        payload = json.loads((root / TEST_SENTINEL).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProjectConfigError(
            "invalid Latch test root sentinel; refusing scope-state resolution"
        ) from exc
    expected = str(payload.get("capability_sha256") or "")
    actual = hashlib.sha256(capability.encode("utf-8")).hexdigest()
    if not expected or not hmac.compare_digest(expected, actual):
        raise ProjectConfigError(
            "Latch test capability does not match its scope-state root"
        )
    return root


def _platform_data_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (
            Path(base) / "Latch"
            if base
            else Path.home() / "AppData" / "Local" / "Latch"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Latch"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "latch"


def control_root(env: Mapping[str, str] | None = None) -> Path:
    """Return Latch-owned machine state, capability-isolated in tests."""
    values = os.environ if env is None else env
    test_root = _validated_test_root(values)
    configured = values.get(CONTROL_ROOT_ENV)
    if test_root is not None:
        candidate = (
            Path(configured).expanduser().resolve(strict=False)
            if configured
            else test_root / "scope-control"
        )
        if candidate == test_root or not _is_relative_to(candidate, test_root):
            raise ProjectConfigError(
                "test scope state must stay below the authenticated test root"
            )
        return candidate
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ProjectConfigError("LATCH_SCOPE_STATE_ROOT must be absolute")
        return candidate.resolve(strict=False)
    return _platform_data_root() / "control"


def _install_home() -> Path:
    configured = os.environ.get("LATCH_HOME") or os.environ.get("CLAUDE_KB_HOME")
    return Path(configured).expanduser() if configured else Path(__file__).resolve().parent.parent


def _canonical_start(
    value: str | os.PathLike[str] | None,
    *,
    require_exists: bool = False,
) -> Path:
    raw = Path(value or os.getcwd()).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    try:
        resolved = raw.resolve(strict=require_exists)
    except OSError as exc:
        raise ProjectConfigError(f"project path does not resolve: {raw}: {exc}") from exc
    if resolved.exists() and resolved.is_file():
        return resolved.parent
    return resolved


def _require_scope_root(value: str | os.PathLike[str]) -> Path:
    root = _canonical_start(value, require_exists=True)
    if not root.is_dir():
        raise ProjectConfigError(f"scope root must be a directory: {root}")
    if root == Path(root.anchor):
        raise ProjectConfigError("refusing to create a Latch scope at a filesystem root")
    try:
        home = Path.home().resolve(strict=True)
    except OSError:
        home = Path.home().resolve(strict=False)
    if _same_path(root, home):
        raise ProjectConfigError(
            "refusing to scope the entire home directory; choose a narrower root"
        )
    return root


def _root_key(root: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()


def _scope_key(scope_id: str) -> str:
    return scope_id.replace("-", "")


def portable_marker_path(root: str | os.PathLike[str]) -> Path:
    return _canonical_start(root) / PORTABLE_DIR_NAME / PORTABLE_FILE_NAME


def local_binding_path(root: str | os.PathLike[str]) -> Path:
    canonical = _canonical_start(root)
    return control_root() / ROOTS_DIR_NAME / f"{_root_key(canonical)}.json"


def scope_binding_path(scope_id: str) -> Path:
    canonical = _canonical_uuid(scope_id, label="scope_id")
    return control_root() / SCOPES_DIR_NAME / f"{_scope_key(canonical)}.json"


def machine_policy_path() -> Path:
    return control_root() / POLICY_FILE_NAME


def compatibility_binding_path() -> Path:
    return control_root() / COMPATIBILITY_BINDING_FILE_NAME


def state_dir(project_root: str | os.PathLike[str]) -> Path:
    root = _canonical_start(project_root)
    return control_root() / ROOT_STATE_DIR_NAME / _root_key(root)


def ensure_state_dir(project_root: str | os.PathLike[str]) -> Path:
    directory = state_dir(project_root)
    _ensure_real_directory(directory)
    return directory


def unlatch_state_path(project_root: str | os.PathLike[str]) -> Path:
    return state_dir(project_root) / UNLATCH_STATE_FILE_NAME


def continuity_epoch_path(project_root: str | os.PathLike[str]) -> Path:
    return state_dir(project_root) / CONTINUITY_EPOCH_FILE_NAME


def _continuity_epoch(start: str | os.PathLike[str]) -> str | None:
    """Fingerprint every ancestor epoch that can invalidate delayed work."""
    current = _canonical_start(start)
    epochs: list[str] = []
    for root in (current, *current.parents):
        path = continuity_epoch_path(root)
        if not (path.exists() or path.is_symlink()):
            continue
        payload = _read_regular_json(path, label="continuity epoch")
        if set(payload) != {"format", "root", "revision"} or payload.get(
            "format"
        ) != FORMAT_VERSION:
            raise ProjectConfigError(f"continuity epoch has unsupported fields: {path}")
        recorded_root = payload.get("root")
        if not isinstance(recorded_root, str) or not Path(recorded_root).is_absolute():
            raise ProjectConfigError(f"continuity epoch has an invalid root: {path}")
        if not _same_path(Path(os.path.normpath(recorded_root)), root):
            raise ProjectConfigError(f"continuity epoch belongs to another root: {path}")
        revision = _hex(payload.get("revision"), 32, label="continuity epoch revision")
        epochs.append(f"{os.path.normcase(str(root))}\0{revision}")
    if not epochs:
        return None
    return hashlib.sha256("\0".join(epochs).encode("utf-8")).hexdigest()


def _bump_continuity_epoch(root: Path) -> None:
    """Permanently stale work from before an inherited subtree was re-latched."""
    atomic_json(
        continuity_epoch_path(root),
        {
            "format": FORMAT_VERSION,
            "root": str(root),
            "revision": secrets.token_hex(16),
        },
    )


def access_lock_path(target_or_root: ResolvedScope | str | os.PathLike[str]) -> Path:
    if isinstance(target_or_root, ResolvedScope):
        key = target_or_root.lock_key
    else:
        target = resolve(target_or_root)
        key = target.lock_key
    directory = control_root() / LOCKS_DIR_NAME
    _ensure_real_directory(directory)
    return directory / f"access-{key}.lock"


def _ensure_real_directory(path: Path, *, mode: int = 0o700) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise ProjectConfigError(f"scope state must be a real directory: {path}")
        return
    try:
        path.mkdir(parents=True, mode=mode)
    except FileExistsError:
        # A second process may have created the same control directory after
        # our initial check.  Accept only the same safe final object.
        pass
    if path.is_symlink() or not path.is_dir():
        raise ProjectConfigError(f"scope state must be a real directory: {path}")
    try:
        path.chmod(mode)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_text(path: Path, body: str, *, mode: int = 0o600) -> None:
    """Durably replace one single-link regular text file."""
    _ensure_real_directory(path.parent)
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise ProjectConfigError(f"refusing to replace unsafe file: {path}")
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
            try:
                temp.chmod(mode)
            except OSError:
                pass
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_json(path: Path, payload: dict[str, object], *, mode: int = 0o600) -> None:
    atomic_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n", mode=mode)


def durable_unlink(path: Path) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_nlink != 1)):
        raise ProjectConfigError(f"refusing to remove unsafe file: {path}")
    if path.exists():
        path.unlink()
        _fsync_directory(path.parent)


def _read_regular_json(path: Path, *, label: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProjectConfigError(f"{label} must be a single-link regular file: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ProjectConfigError:
        raise
    except (OSError, ValueError) as exc:
        raise ProjectConfigError(f"{label} is unreadable or malformed: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProjectConfigError(f"{label} must contain a JSON object: {path}")
    return payload


def _canonical_uuid(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise ProjectConfigError(f"{label} must be a UUID")
    try:
        canonical = str(uuid.UUID(value))
    except ValueError as exc:
        raise ProjectConfigError(f"{label} must be a UUID") from exc
    if not hmac.compare_digest(canonical, value):
        raise ProjectConfigError(f"{label} must use canonical UUID spelling")
    return canonical


def _hex(value: object, length: int, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise ProjectConfigError(f"{label} is invalid")
    return value


def _marker_fingerprint(scope_id: str, policy: str) -> str:
    body = json.dumps(
        {"format": FORMAT_VERSION, "policy": policy, "scope_id": scope_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _directory_fingerprint(directory: Path) -> str:
    canonical = validated_kb_path(directory)
    metadata = canonical.stat()
    body = "\0".join(
        (
            os.path.normcase(str(canonical)),
            str(metadata.st_dev),
            str(metadata.st_ino),
        )
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _read_vault_uuid(directory: Path) -> str | None:
    database = directory / "kb.db"
    try:
        metadata = database.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ProjectConfigError(f"unsafe SQLite database in selected KB: {database}")
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vault_identity'"
            ).fetchone()
            if table is None:
                return None
            rows = connection.execute(
                "SELECT vault_uuid FROM vault_identity ORDER BY slot"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ProjectConfigError(f"could not read selected KB identity: {database}: {exc}") from exc
    if not rows:
        raise ProjectConfigError(
            "selected KB identity initialization is still in progress",
            code=LOCK_VAULT_IDENTITY_INITIALIZING,
        )
    if len(rows) != 1:
        raise ProjectConfigError("selected KB must contain exactly one immutable vault identity")
    return _canonical_uuid(str(rows[0][0]), label="vault_uuid")


def _load_marker(root: Path) -> ScopeMarker:
    directory = root / PORTABLE_DIR_NAME
    if directory.is_symlink() or not directory.is_dir():
        raise ProjectConfigError(f"portable scope state must be a real directory: {directory}")
    path = directory / PORTABLE_FILE_NAME
    payload = _read_regular_json(path, label="portable scope declaration")
    if set(payload) != {"format", "scope_id", "policy"} or payload.get("format") != FORMAT_VERSION:
        raise ProjectConfigError(f"portable scope declaration has unsupported fields: {path}")
    scope_id = _canonical_uuid(payload.get("scope_id"), label="scope_id")
    policy = payload.get("policy")
    if policy not in POLICIES:
        raise ProjectConfigError(f"portable scope declaration has invalid policy: {path}")
    policy = str(policy)
    return ScopeMarker(
        project_root=root,
        marker_path=path,
        scope_id=scope_id,
        policy=policy,
        fingerprint=_marker_fingerprint(scope_id, policy),
    )


def _load_root_authorization(root: Path) -> RootAuthorization:
    path = local_binding_path(root)
    payload = _read_regular_json(path, label="local root authorization")
    common = {"format", "kind", "root", "revision"}
    kind = payload.get("kind")
    expected = (
        common | {"scope_id", "policy", "marker_fingerprint"}
        if kind == ROOT_KIND_SCOPE
        else common
        | {
            "remembered_scope_id",
            "remembered_revision",
            "remembered_kb_dir",
            "remembered_target_fingerprint",
            "remembered_vault_uuid",
            "policy",
        }
        if kind == ROOT_KIND_OFF
        else set()
    )
    if not expected or set(payload) != expected or payload.get("format") != FORMAT_VERSION:
        raise ProjectConfigError(f"local root authorization has unsupported fields: {path}")
    raw_root = payload.get("root")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise ProjectConfigError(f"local root authorization has invalid root: {path}")
    recorded_root = Path(os.path.normpath(raw_root))
    if not _same_path(recorded_root, root):
        raise ProjectConfigError(f"local root authorization belongs to another root: {path}")
    revision = _hex(payload.get("revision"), 32, label="root authorization revision")
    if kind == ROOT_KIND_SCOPE:
        scope_id = _canonical_uuid(payload.get("scope_id"), label="scope_id")
        policy = payload.get("policy")
        if policy not in POLICIES:
            raise ProjectConfigError(f"local root authorization has invalid policy: {path}")
        marker_fingerprint = _hex(
            payload.get("marker_fingerprint"), 64, label="marker fingerprint"
        )
        return RootAuthorization(
            root,
            path,
            ROOT_KIND_SCOPE,
            revision,
            scope_id,
            str(policy),
            marker_fingerprint,
            None,
            None,
            None,
            None,
            None,
        )
    remembered_scope_id = payload.get("remembered_scope_id")
    if remembered_scope_id is not None:
        remembered_scope_id = _canonical_uuid(remembered_scope_id, label="remembered_scope_id")
    remembered_revision = _hex(
        payload.get("remembered_revision"), 32, label="remembered revision"
    )
    raw_remembered_kb = payload.get("remembered_kb_dir")
    if not isinstance(raw_remembered_kb, str) or not Path(raw_remembered_kb).is_absolute():
        raise ProjectConfigError(f"OFF boundary has invalid remembered KB path: {path}")
    remembered_kb_dir = Path(os.path.normpath(raw_remembered_kb))
    remembered_fingerprint = payload.get("remembered_target_fingerprint")
    if remembered_fingerprint is not None:
        remembered_fingerprint = _hex(
            remembered_fingerprint, 64, label="remembered target fingerprint"
        )
    raw_remembered_vault = payload.get("remembered_vault_uuid")
    remembered_vault_uuid = (
        _canonical_uuid(raw_remembered_vault, label="remembered_vault_uuid")
        if raw_remembered_vault is not None
        else None
    )
    policy = payload.get("policy")
    if policy not in POLICIES:
        raise ProjectConfigError(f"OFF boundary has invalid remembered policy: {path}")
    return RootAuthorization(
        root,
        path,
        ROOT_KIND_OFF,
        revision,
        None,
        str(policy),
        None,
        remembered_scope_id,
        remembered_revision,
        remembered_kb_dir,
        remembered_fingerprint,
        remembered_vault_uuid,
    )


def _scope_binding_from_payload(
    path: Path,
    payload: dict[str, object],
    *,
    expected_scope_id: str | None = None,
) -> ScopeBinding:
    expected = {
        "format",
        "scope_id",
        "policy",
        "mode",
        "kb_dir",
        "kb_fingerprint",
        "vault_uuid",
        "revision",
    }
    if set(payload) != expected or payload.get("format") != FORMAT_VERSION:
        raise ProjectConfigError(f"machine scope binding has unsupported fields: {path}")
    observed_id = _canonical_uuid(payload.get("scope_id"), label="scope_id")
    if expected_scope_id is not None and observed_id != expected_scope_id:
        raise ProjectConfigError(f"machine scope binding has the wrong scope id: {path}")
    if path != scope_binding_path(observed_id):
        raise ProjectConfigError(f"machine scope binding has the wrong filename: {path}")
    policy = payload.get("policy")
    mode = payload.get("mode")
    if policy not in POLICIES or mode not in MODES:
        raise ProjectConfigError(f"machine scope binding has invalid policy or mode: {path}")
    raw_kb = payload.get("kb_dir")
    if not isinstance(raw_kb, str) or not Path(raw_kb).is_absolute():
        raise ProjectConfigError(f"machine scope binding has invalid KB path: {path}")
    kb_dir = Path(os.path.normpath(raw_kb))
    fingerprint = _hex(payload.get("kb_fingerprint"), 64, label="KB fingerprint")
    raw_vault = payload.get("vault_uuid")
    vault_uuid = (
        _canonical_uuid(raw_vault, label="vault_uuid")
        if raw_vault is not None
        else None
    )
    revision = _hex(payload.get("revision"), 32, label="scope revision")
    return ScopeBinding(
        observed_id,
        path,
        str(policy),
        str(mode),
        kb_dir,
        fingerprint,
        vault_uuid,
        revision,
    )


def _load_scope_binding(scope_id: str) -> ScopeBinding:
    path = scope_binding_path(scope_id)
    payload = _read_regular_json(path, label="machine scope binding")
    return _scope_binding_from_payload(
        path,
        payload,
        expected_scope_id=scope_id,
    )


def _load_compatibility_binding() -> CompatibilityBinding:
    path = compatibility_binding_path()
    payload = _read_regular_json(path, label="compatibility KB binding")
    expected = {
        "format",
        "kb_dir",
        "kb_fingerprint",
        "vault_uuid",
        "revision",
    }
    if set(payload) != expected or payload.get("format") != FORMAT_VERSION:
        raise ProjectConfigError(
            f"compatibility KB binding has unsupported fields: {path}"
        )
    raw_kb = payload.get("kb_dir")
    if not isinstance(raw_kb, str) or not Path(raw_kb).is_absolute():
        raise ProjectConfigError(
            f"compatibility KB binding has an invalid KB path: {path}"
        )
    raw_vault = payload.get("vault_uuid")
    return CompatibilityBinding(
        record_path=path,
        kb_dir=Path(os.path.normpath(raw_kb)),
        kb_fingerprint=_hex(
            payload.get("kb_fingerprint"), 64, label="KB fingerprint"
        ),
        vault_uuid=(
            _canonical_uuid(raw_vault, label="vault_uuid")
            if raw_vault is not None
            else None
        ),
        revision=_hex(
            payload.get("revision"), 32, label="compatibility revision"
        ),
    )


def _pin_file() -> Path:
    return _install_home() / "kb_location.json"


def _global_kb_dir(
    *,
    required: bool,
    check_private_collision: bool = False,
) -> Path | None:
    latch_override = (os.environ.get("LATCH_KB_DIR") or "").strip() or None
    legacy_override = (os.environ.get("CLAUDE_KB_DIR") or "").strip() or None
    if latch_override and legacy_override:
        latch_candidate = Path(latch_override).expanduser()
        legacy_candidate = Path(legacy_override).expanduser()
        if not latch_candidate.is_absolute() or not legacy_candidate.is_absolute():
            raise ProjectConfigError("global KB environment targets must be absolute")
        if not _same_path(
            Path(os.path.normpath(str(latch_candidate))),
            Path(os.path.normpath(str(legacy_candidate))),
        ):
            raise ProjectConfigError(
                "LATCH_KB_DIR and CLAUDE_KB_DIR select different global KBs; "
                "unset one or make both targets identical",
                code=LOCK_GLOBAL_PIN_CHANGED,
            )
    raw = latch_override or legacy_override
    if raw:
        candidate = Path(raw).expanduser()
    else:
        path = _pin_file()
        if not (path.exists() or path.is_symlink()):
            if required:
                raise ProjectConfigError(
                    "the shared/global KB is not pinned; run the installer or select a Private KB"
                )
            return None
        payload = _read_regular_json(path, label="install KB pin")
        raw_pin = payload.get("kb_dir")
        if not isinstance(raw_pin, str) or not Path(raw_pin).is_absolute():
            raise ProjectConfigError(f"install KB pin has an invalid KB path: {path}")
        candidate = Path(raw_pin)
    selected = validated_kb_path(candidate)
    if check_private_collision:
        fingerprint = _directory_fingerprint(selected)
        owner = _target_reservation_owner(
            selected,
            fingerprint,
            vault_uuid=_read_vault_uuid(selected),
            reserved_policies=frozenset({POLICY_PRIVATE}),
        )
        if owner is not None:
            raise ProjectConfigError(
                f"the global KB pin collides with Private scope {owner}; refusing access"
            )
    return selected


def read_machine_policy() -> str:
    """Return the persisted policy; absence is always strict/fail-closed.

    The installer writes ``compatibility_global`` once when it detects an
    existing install.  Runtime inference from a pin would make deletion of this
    policy file silently reopen global access.
    """
    path = machine_policy_path()
    if not (path.exists() or path.is_symlink()):
        return MACHINE_POLICY_EXPLICIT
    payload = _read_regular_json(path, label="machine scope policy")
    if set(payload) != {"format", "policy"} or payload.get("format") != FORMAT_VERSION:
        raise ProjectConfigError(f"machine scope policy has unsupported fields: {path}")
    policy = payload.get("policy")
    if policy not in MACHINE_POLICIES:
        raise ProjectConfigError(f"machine scope policy is invalid: {path}")
    return str(policy)


def write_machine_policy(policy: str) -> None:
    if policy not in MACHINE_POLICIES:
        raise ProjectConfigError(f"unsupported machine scope policy: {policy!r}")
    atomic_json(machine_policy_path(), {"format": FORMAT_VERSION, "policy": policy})


def _write_compatibility_binding(
    kb_dir: Path,
    kb_fingerprint: str,
    vault_uuid: str | None,
    *,
    revision: str | None = None,
) -> CompatibilityBinding:
    path = compatibility_binding_path()
    atomic_json(
        path,
        {
            "format": FORMAT_VERSION,
            "kb_dir": str(kb_dir),
            "kb_fingerprint": kb_fingerprint,
            "vault_uuid": vault_uuid,
            "revision": revision or secrets.token_hex(16),
        },
    )
    return _load_compatibility_binding()


def _validate_live_compatibility_binding(
    binding: CompatibilityBinding,
) -> tuple[Path, str]:
    selected = _global_kb_dir(required=True, check_private_collision=True)
    assert selected is not None
    if not _same_path(selected, binding.kb_dir):
        raise ProjectConfigError(
            "the global KB pin changed; explicitly re-authorize the compatibility binding",
            code=LOCK_GLOBAL_PIN_CHANGED,
        )
    fingerprint = _directory_fingerprint(selected)
    if not hmac.compare_digest(fingerprint, binding.kb_fingerprint):
        raise ProjectConfigError(
            "the compatibility KB directory identity changed; explicitly re-authorize it",
            code=LOCK_GLOBAL_PIN_CHANGED,
        )
    observed_uuid = _read_vault_uuid(selected)
    if binding.vault_uuid is None and observed_uuid is not None:
        raise ProjectConfigError(
            "the compatibility KB established an immutable vault identity; finalization is pending",
            code=LOCK_VAULT_IDENTITY_PENDING,
        )
    if binding.vault_uuid is not None and observed_uuid is None:
        raise ProjectConfigError(
            "the compatibility KB immutable vault identity is missing"
        )
    if (
        binding.vault_uuid is not None
        and observed_uuid is not None
        and not hmac.compare_digest(binding.vault_uuid, observed_uuid)
    ):
        raise ProjectConfigError(
            "the compatibility KB immutable vault identity changed"
        )
    return selected, fingerprint


def initialize_compatibility_binding() -> CompatibilityBinding:
    """Bind an upgraded install to its exact existing global KB.

    This is installer wiring, not runtime inference.  An existing binding is
    only accepted when its path, directory identity, and immutable vault UUID
    still match exactly.
    """
    if read_machine_policy() != MACHINE_POLICY_COMPATIBILITY:
        raise ProjectConfigError(
            "compatibility binding is only valid in compatibility mode"
        )
    with scope_registry_lock():
        path = compatibility_binding_path()
        if path.exists() or path.is_symlink():
            binding = _load_compatibility_binding()
            _validate_live_compatibility_binding(binding)
            return binding
        selected = _global_kb_dir(required=True, check_private_collision=True)
        assert selected is not None
        return _write_compatibility_binding(
            selected,
            _directory_fingerprint(selected),
            _read_vault_uuid(selected),
        )


def reauthorize_compatibility_binding() -> CompatibilityBinding:
    """Explicitly bind compatibility mode to the current global KB pin."""
    if read_machine_policy() != MACHINE_POLICY_COMPATIBILITY:
        raise ProjectConfigError(
            "compatibility binding is only valid in compatibility mode"
        )
    with scope_registry_lock():
        selected = _global_kb_dir(required=True, check_private_collision=True)
        assert selected is not None
        return _write_compatibility_binding(
            selected,
            _directory_fingerprint(selected),
            _read_vault_uuid(selected),
        )


def _candidate_flags(root: Path) -> tuple[bool, bool]:
    directory = root / PORTABLE_DIR_NAME
    marker = directory / PORTABLE_FILE_NAME
    portable = (
        directory.is_symlink()
        or (directory.exists() and not directory.is_dir())
        or marker.exists()
        or marker.is_symlink()
    )
    local = local_binding_path(root)
    return portable, local.exists() or local.is_symlink()


def _target_revision(base_revision: str, kb_dir: Path, fingerprint: str) -> str:
    body = "\0".join((base_revision, os.path.normcase(str(kb_dir)), fingerprint))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _effective_revision(
    target_revision: str,
    start: str | os.PathLike[str],
) -> str:
    """Fold subtree continuity into the revision carried by delayed work."""
    epoch = _continuity_epoch(start)
    if epoch is None:
        return target_revision
    body = "\0".join((target_revision, epoch))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _with_effective_revision(
    target: ResolvedScope | None,
    start: str | os.PathLike[str],
) -> ResolvedScope | None:
    if target is None:
        return None
    return replace(
        target,
        revision=_effective_revision(target.target_revision, start),
    )


def _locked(
    root: Path,
    reason: str,
    *,
    marker: ScopeMarker | None = None,
    authorization: RootAuthorization | None = None,
    binding: ScopeBinding | None = None,
    reason_code: str | None = None,
) -> ResolvedScope:
    # Machine-local authorization is the fail-closed authority when a portable
    # marker is tampered.  Never let untrusted marker policy weaken a remembered
    # Private boundary into Shared.
    scope_id = (
        authorization.scope_id
        if authorization is not None
        else binding.scope_id
        if binding is not None
        else marker.scope_id
        if marker is not None
        else None
    )
    observed_policies = {
        value
        for value in (
            authorization.policy if authorization is not None else None,
            binding.policy if binding is not None else None,
            marker.policy if marker is not None else None,
        )
        if value in POLICIES
    }
    # Any remembered Private authority dominates a conflicting Shared value.
    # LOCKED conflicts must never weaken the privacy lattice.
    policy = (
        POLICY_PRIVATE
        if POLICY_PRIVATE in observed_policies
        else POLICY_SHARED
        if POLICY_SHARED in observed_policies
        else None
    )
    trusted_scope_id = (
        authorization.scope_id
        if authorization is not None
        and authorization.kind == ROOT_KIND_SCOPE
        and authorization.scope_id is not None
        and binding is not None
        and binding.scope_id == authorization.scope_id
        else None
    )
    target_revision = (
        _target_revision(
            binding.revision,
            binding.kb_dir,
            binding.kb_fingerprint,
        )
        if trusted_scope_id is not None and binding is not None
        else authorization.revision
        if authorization is not None
        else _root_key(root)[:32]
    )
    return ResolvedScope(
        project_root=root,
        state=MODE_LOCKED,
        policy=policy,
        scope_id=scope_id,
        target_revision=target_revision,
        revision=target_revision,
        kb_dir=None,
        remembered_kb_dir=binding.kb_dir if binding is not None else None,
        target_fingerprint=(binding.kb_fingerprint if binding is not None else None),
        vault_uuid=binding.vault_uuid if binding is not None else None,
        marker_path=(
            marker.marker_path
            if marker is not None
            else root / PORTABLE_DIR_NAME / PORTABLE_FILE_NAME
        ),
        source=SOURCE_EXPLICIT,
        lock_key=(
            "shared-global"
            if trusted_scope_id is not None and policy == POLICY_SHARED
            else f"scope-{_scope_key(trusted_scope_id)}"
            if trusted_scope_id is not None
            else f"root-{_root_key(root)}"
        ),
        reason=reason,
        reason_code=reason_code,
    )


def _locked_compatibility(
    root: Path,
    reason: str,
    *,
    binding: CompatibilityBinding | None = None,
    reason_code: str | None = None,
) -> ResolvedScope:
    """Return a fail-closed compatibility target without losing its identity."""
    target_revision = (
        _target_revision(
            binding.revision,
            binding.kb_dir,
            binding.kb_fingerprint,
        )
        if binding is not None
        else _root_key(root)[:32]
    )
    target = ResolvedScope(
        project_root=root,
        state=MODE_LOCKED,
        policy=POLICY_SHARED,
        scope_id=None,
        target_revision=target_revision,
        revision=target_revision,
        kb_dir=None,
        remembered_kb_dir=(binding.kb_dir if binding is not None else None),
        target_fingerprint=(
            binding.kb_fingerprint if binding is not None else None
        ),
        vault_uuid=binding.vault_uuid if binding is not None else None,
        marker_path=None,
        source=SOURCE_COMPATIBILITY,
        lock_key="shared-global",
        reason=reason,
        reason_code=reason_code,
    )
    effective = _with_effective_revision(target, root)
    assert effective is not None
    return effective


def _validate_live_binding(binding: ScopeBinding) -> tuple[Path, str]:
    current = validated_kb_path(binding.kb_dir)
    fingerprint = _directory_fingerprint(current)
    if not hmac.compare_digest(fingerprint, binding.kb_fingerprint):
        raise ProjectConfigError(
            f"bound KB directory identity changed: {binding.kb_dir}; explicitly repin it"
        )
    observed_uuid = _read_vault_uuid(current)
    if binding.vault_uuid is None and observed_uuid is not None:
        raise ProjectConfigError(
            "bound KB established an immutable vault identity; finalize that identity before reuse",
            code=LOCK_VAULT_IDENTITY_PENDING,
        )
    if binding.vault_uuid is not None and observed_uuid is None:
        raise ProjectConfigError("bound KB immutable vault identity is missing")
    if binding.vault_uuid is not None and observed_uuid is not None and not hmac.compare_digest(
        binding.vault_uuid, observed_uuid
    ):
        raise ProjectConfigError("bound KB immutable vault identity changed")
    if binding.policy == POLICY_SHARED:
        global_kb = _global_kb_dir(
            required=True,
            check_private_collision=True,
        )
        assert global_kb is not None
        if not _same_path(global_kb, current):
            raise ProjectConfigError(
                "the global KB pin changed; explicitly re-authorize this Shared scope",
                code=LOCK_GLOBAL_PIN_CHANGED,
            )
    return current, fingerprint


def _private_ancestor_guard(
    target: ResolvedScope,
    ancestor: ResolvedScope | None,
) -> ResolvedScope:
    """A nearer broken/Shared boundary can never mask an outer Private one."""
    if (
        ancestor is None
        or ancestor.policy != POLICY_PRIVATE
        or target.policy == POLICY_PRIVATE
    ):
        return target
    detail = (
        f"{target.reason}; " if target.reason else ""
    ) + f"this boundary cannot bypass outer Private scope {ancestor.project_root}"
    return replace(
        target,
        state=MODE_LOCKED,
        policy=POLICY_PRIVATE,
        kb_dir=None,
        reason=detail,
        reason_code=LOCK_PRIVATE_ANCESTOR,
    )


def _resolve_candidates_base(entries: list[Path]) -> ResolvedScope | None:
    for index, root in enumerate(entries):
        portable, local = _candidate_flags(root)
        if not portable and not local:
            continue
        marker: ScopeMarker | None = None
        authorization: RootAuthorization | None = None
        binding: ScopeBinding | None = None
        marker_error: ProjectConfigError | None = None
        authorization_error: ProjectConfigError | None = None
        if portable:
            try:
                marker = _load_marker(root)
            except ProjectConfigError as exc:
                marker_error = exc
        if local:
            try:
                authorization = _load_root_authorization(root)
            except ProjectConfigError as exc:
                authorization_error = exc
        parent_loaded = False
        parent: ResolvedScope | None = None

        def parent_scope() -> ResolvedScope | None:
            nonlocal parent_loaded, parent
            if not parent_loaded:
                parent = _resolve_candidates(entries[index + 1 :])
                parent_loaded = True
            return parent

        def finish(target: ResolvedScope) -> ResolvedScope:
            if target.policy == POLICY_PRIVATE:
                return target
            return _private_ancestor_guard(target, parent_scope())

        if marker_error is not None or authorization_error is not None:
            known_scope_id = (
                authorization.scope_id
                if authorization is not None
                and authorization.kind == ROOT_KIND_SCOPE
                and authorization.scope_id is not None
                else marker.scope_id
                if marker is not None
                else None
            )
            if known_scope_id is not None:
                try:
                    binding = _load_scope_binding(known_scope_id)
                except ProjectConfigError:
                    binding = None
            errors = "; ".join(
                str(error)
                for error in (marker_error, authorization_error)
                if error is not None
            )
            return finish(_locked(
                root,
                errors,
                marker=marker,
                authorization=authorization,
                binding=binding,
                reason_code=(
                    marker_error.code
                    if marker_error is not None and marker_error.code is not None
                    else authorization_error.code
                    if authorization_error is not None
                    else None
                ),
            ))
        if authorization is not None and authorization.kind == ROOT_KIND_OFF:
            if marker is not None:
                return finish(_locked(
                    root,
                    "an OFF boundary conflicts with a portable scope declaration",
                    marker=marker,
                    authorization=authorization,
                    reason_code=LOCK_INTERRUPTED_OFF_REPLACEMENT,
                ))
            parent = parent_scope()
            parent_matches = bool(
                parent is not None
                and parent.state == MODE_LATCHED
                and parent.scope_id == authorization.remembered_scope_id
                and parent.revision == authorization.remembered_revision
                and parent.target_fingerprint == authorization.remembered_target_fingerprint
            )
            return finish(ResolvedScope(
                project_root=root,
                state=MODE_UNLATCHED,
                policy=authorization.policy,
                scope_id=authorization.remembered_scope_id,
                target_revision=authorization.revision,
                revision=authorization.revision,
                kb_dir=None,
                remembered_kb_dir=authorization.remembered_kb_dir,
                target_fingerprint=authorization.remembered_target_fingerprint,
                vault_uuid=authorization.remembered_vault_uuid,
                marker_path=None,
                source=SOURCE_OFF_BOUNDARY,
                lock_key=f"root-{_root_key(root)}",
                reason=(
                    None
                    if parent_matches
                    else "the remembered parent target changed; re-latch requires a new explicit choice"
                ),
            ))
        if marker is None:
            if (
                authorization is not None
                and authorization.kind == ROOT_KIND_SCOPE
                and authorization.scope_id is not None
            ):
                try:
                    binding = _load_scope_binding(authorization.scope_id)
                except ProjectConfigError:
                    binding = None
            return finish(_locked(
                root,
                "portable scope declaration is missing",
                authorization=authorization,
                binding=binding,
            ))
        if authorization is None:
            return finish(_locked(
                root,
                "this declared root is not authorized on this machine",
                marker=marker,
                reason_code=LOCK_UNAUTHORIZED_ROOT,
            ))
        if authorization.kind != ROOT_KIND_SCOPE:
            return finish(_locked(root, "invalid root authorization kind", marker=marker, authorization=authorization))
        if marker.scope_id == authorization.scope_id:
            try:
                binding = _load_scope_binding(marker.scope_id)
            except ProjectConfigError:
                binding = None
        if (
            marker.scope_id != authorization.scope_id
            or marker.policy != authorization.policy
            or marker.fingerprint != authorization.marker_fingerprint
        ):
            return finish(_locked(
                root,
                "portable scope declaration does not match its local authorization",
                marker=marker,
                authorization=authorization,
                binding=binding,
            ))
        if binding is None:
            try:
                binding = _load_scope_binding(marker.scope_id)
            except ProjectConfigError as exc:
                return finish(_locked(root, str(exc), marker=marker, authorization=authorization))
        if binding.policy != marker.policy:
            return finish(_locked(
                root,
                "portable scope policy does not match the central scope binding",
                marker=marker,
                authorization=authorization,
                binding=binding,
            ))
        if marker.policy == POLICY_SHARED:
            parent = parent_scope()
            if parent is not None and parent.policy == POLICY_PRIVATE:
                return finish(_locked(
                    root,
                    f"a Shared scope cannot be nested below Private scope {parent.project_root}",
                    marker=marker,
                    authorization=authorization,
                    binding=binding,
                ))
        try:
            kb_dir, fingerprint = _validate_live_binding(binding)
        except ProjectConfigError as exc:
            return finish(_locked(
                root,
                str(exc),
                marker=marker,
                authorization=authorization,
                binding=binding,
                reason_code=exc.code,
            ))
        revision = _target_revision(binding.revision, kb_dir, fingerprint)
        state = binding.mode
        return finish(ResolvedScope(
            project_root=root,
            state=state,
            policy=binding.policy,
            scope_id=binding.scope_id,
            target_revision=revision,
            revision=revision,
            kb_dir=kb_dir if state == MODE_LATCHED else None,
            remembered_kb_dir=kb_dir,
            target_fingerprint=fingerprint,
            vault_uuid=binding.vault_uuid,
            marker_path=marker.marker_path,
            source=SOURCE_EXPLICIT,
            lock_key=(
                "shared-global"
                if binding.policy == POLICY_SHARED
                else f"scope-{_scope_key(binding.scope_id)}"
            ),
            reason=None,
        ))
    return None


def _resolve_candidates(entries: list[Path]) -> ResolvedScope | None:
    if not entries:
        return None
    return _with_effective_revision(
        _resolve_candidates_base(entries),
        entries[0],
    )


def resolve(start: str | os.PathLike[str] | None = None) -> ResolvedScope:
    """Resolve the nearest explicit boundary, failing closed on every gap."""
    current = _canonical_start(start)
    explicit = _resolve_candidates([current, *current.parents])
    if explicit is not None:
        return explicit
    if read_machine_policy() == MACHINE_POLICY_COMPATIBILITY:
        try:
            binding = _load_compatibility_binding()
        except ProjectConfigError as binding_error:
            # A missing receipt never grants authority.  Still inspect the pin
            # read-only so an absent pin or Private collision is named clearly.
            try:
                _global_kb_dir(required=True, check_private_collision=True)
            except ProjectConfigError as pin_error:
                binding_error = pin_error
            return _locked_compatibility(
                current,
                f"global KB compatibility binding is unsafe: {binding_error}",
                reason_code=binding_error.code,
            )
        try:
            kb_dir, fingerprint = _validate_live_compatibility_binding(binding)
            revision = _target_revision(binding.revision, kb_dir, fingerprint)
        except ProjectConfigError as exc:
            return _locked_compatibility(
                current,
                f"global KB compatibility binding is unsafe: {exc}",
                binding=binding,
                reason_code=exc.code,
            )
        target = ResolvedScope(
            project_root=current,
            state=MODE_LATCHED,
            policy=POLICY_SHARED,
            scope_id=None,
            target_revision=revision,
            revision=revision,
            kb_dir=kb_dir,
            remembered_kb_dir=kb_dir,
            target_fingerprint=fingerprint,
            vault_uuid=binding.vault_uuid,
            marker_path=None,
            source=SOURCE_COMPATIBILITY,
            lock_key="shared-global",
            reason="legacy install-level global KB behavior",
        )
        effective = _with_effective_revision(target, current)
        assert effective is not None
        return effective
    return _locked(
        current,
        "this location is outside every authorized Latch scope",
        reason_code=LOCK_OUTSIDE_SCOPE,
    )


def require_latched(start: str | os.PathLike[str] | None = None) -> ResolvedScope:
    target = resolve(start)
    if target.state == MODE_UNLATCHED:
        raise ProjectConfigError(
            f"Latch is UNLATCHED for {target.project_root}; no KB access is allowed"
        )
    if target.state == MODE_LOCKED or target.kb_dir is None:
        raise ProjectConfigError(
            f"Latch is LOCKED for {target.project_root}: {target.reason or 'no safe KB target'}"
        )
    return target


def discover(start: str | os.PathLike[str] | None = None) -> ProjectBinding | None:
    """Compatibility adapter; new code should call :func:`resolve`.

    Legacy/global compatibility remains ``None`` so untouched pre-PR callers
    retain their historical pin path until they are migrated.  Explicit
    LATCHED/UNLATCHED/LOCKED roots always return their full target.
    """
    target = resolve(start)
    return None if target.source == "compatibility" else target


def project_root(start: str | os.PathLike[str] | None = None) -> Path:
    return resolve(start).project_root


def _quiesce_scope_mutation(function):
    """Take canonical data-plane authority before any registry mutation."""
    @functools.wraps(function)
    def guarded(root_value, *args, **kwargs):
        root = _require_scope_root(root_value)
        # Local import avoids project_config <-> lockfile import recursion.
        import lockfile

        with lockfile.scope_mutation_lock(str(root)):
            return function(root, *args, **kwargs)

    return guarded


@_quiesce_scope_mutation
def create_scope(
    root_value: str | os.PathLike[str],
    *,
    policy: str,
    scope_id: str | None = None,
) -> ScopeMarker:
    """Create portable intent only; local authorization is separate."""
    root = _require_scope_root(root_value)
    if policy not in POLICIES:
        raise ProjectConfigError(f"unsupported scope policy: {policy!r}")
    with scope_registry_lock():
        _assert_project_root_outside_reserved_targets(root)
        parent = resolve(root.parent)
        if policy == POLICY_SHARED and parent.policy == POLICY_PRIVATE:
            raise ProjectConfigError(
                f"a Shared scope cannot be nested below Private scope {parent.project_root}"
            )
        path = root / PORTABLE_DIR_NAME / PORTABLE_FILE_NAME
        portable, local = _candidate_flags(root)
        if portable:
            existing = _load_marker(root)
            if existing.policy == policy and (
                scope_id is None or existing.scope_id == scope_id
            ):
                return existing
            raise ProjectConfigError(
                "scope already exists with different identity or policy; use an explicit transition"
            )
        if local:
            authorization = _load_root_authorization(root)
            if authorization.kind == ROOT_KIND_OFF:
                raise ProjectConfigError(
                    "this root is currently an OFF boundary; re-latch or explicitly replace it"
                )
            raise ProjectConfigError(
                "local authorization exists without its portable declaration; repair or remove it explicitly"
            )
        chosen_id = (
            _canonical_uuid(scope_id, label="scope_id")
            if scope_id
            else str(uuid.uuid4())
        )
        _ensure_real_directory(path.parent)
        atomic_json(
            path,
            {"format": FORMAT_VERSION, "scope_id": chosen_id, "policy": policy},
            mode=0o644,
        )
        return _load_marker(root)


def _write_scope_binding(
    scope_id: str,
    *,
    policy: str,
    mode: str,
    kb_dir: Path,
    kb_fingerprint: str,
    vault_uuid: str | None,
    revision: str | None = None,
) -> ScopeBinding:
    path = scope_binding_path(scope_id)
    atomic_json(
        path,
        {
            "format": FORMAT_VERSION,
            "scope_id": scope_id,
            "policy": policy,
            "mode": mode,
            "kb_dir": str(kb_dir),
            "kb_fingerprint": kb_fingerprint,
            "vault_uuid": vault_uuid,
            "revision": revision or secrets.token_hex(16),
        },
    )
    return _load_scope_binding(scope_id)


def _write_root_scope_authorization(root: Path, marker: ScopeMarker) -> RootAuthorization:
    path = local_binding_path(root)
    atomic_json(
        path,
        {
            "format": FORMAT_VERSION,
            "kind": ROOT_KIND_SCOPE,
            "root": str(root),
            "scope_id": marker.scope_id,
            "policy": marker.policy,
            "marker_fingerprint": marker.fingerprint,
            "revision": secrets.token_hex(16),
        },
    )
    return _load_root_authorization(root)


def _authorized_scope_components(
    root_value: str | os.PathLike[str],
) -> tuple[Path, ScopeMarker, RootAuthorization, ScopeBinding]:
    """Load an exact, locally authorized scope root for a mutation.

    A merely copied portable marker is intent, not authority.  Mutation APIs
    deliberately do not accept a LOCKED resolution as proof of ownership.
    """
    root = _require_scope_root(root_value)
    marker = _load_marker(root)
    authorization = _load_root_authorization(root)
    if (
        authorization.kind != ROOT_KIND_SCOPE
        or authorization.scope_id != marker.scope_id
        or authorization.policy != marker.policy
        or authorization.marker_fingerprint != marker.fingerprint
    ):
        raise ProjectConfigError(
            "portable scope declaration does not match its local authorization"
        )
    binding = _load_scope_binding(marker.scope_id)
    if binding.policy != marker.policy:
        raise ProjectConfigError(
            "portable scope policy does not match the central scope binding"
        )
    return root, marker, authorization, binding


def _target_reservation_owner(
    selected: Path,
    fingerprint: str,
    *,
    vault_uuid: str | None = None,
    except_scope_id: str | None = None,
    reserved_policies: frozenset[str],
) -> str | None:
    """Return a scope reserving this exact, nested, or copied target."""
    directory = control_root() / SCOPES_DIR_NAME
    if not directory.exists():
        return None
    if directory.is_symlink() or not directory.is_dir():
        raise ProjectConfigError(f"scope bindings must be a real directory: {directory}")
    for path in directory.iterdir():
        if path.suffix != ".json":
            continue
        payload = _read_regular_json(path, label="machine scope binding")
        binding = _scope_binding_from_payload(path, payload)
        scope_id = binding.scope_id
        if except_scope_id is not None and scope_id == except_scope_id:
            continue
        if binding.policy not in reserved_policies:
            continue
        if _paths_overlap(binding.kb_dir, selected) or hmac.compare_digest(
            binding.kb_fingerprint, fingerprint
        ):
            return scope_id
        if (
            vault_uuid is not None
            and binding.vault_uuid is not None
            and hmac.compare_digest(binding.vault_uuid, vault_uuid)
        ):
            return scope_id
    return None


def _reserved_kb_targets() -> list[Path]:
    """Return every machine-known KB target while the registry lock is held."""
    targets: list[Path] = []
    directory = control_root() / SCOPES_DIR_NAME
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectConfigError(
                f"scope bindings must be a real directory: {directory}"
            )
        for path in directory.iterdir():
            if path.suffix != ".json":
                continue
            binding = _scope_binding_from_payload(
                path,
                _read_regular_json(path, label="machine scope binding"),
            )
            targets.append(binding.kb_dir)

    compatibility_path = compatibility_binding_path()
    if compatibility_path.exists() or compatibility_path.is_symlink():
        targets.append(_load_compatibility_binding().kb_dir)

    global_target = _global_kb_dir(required=False)
    if global_target is not None:
        targets.append(global_target)

    unique: dict[str, Path] = {}
    for target in targets:
        unique.setdefault(os.path.normcase(str(target)), target)
    return list(unique.values())


def _assert_project_root_outside_reserved_targets(
    root: Path,
    *,
    additional_targets: Iterable[Path] = (),
) -> None:
    """A project root may never contain or live inside a KB target."""
    targets = [*_reserved_kb_targets(), *additional_targets]
    for target in targets:
        if _paths_overlap(root, target):
            raise ProjectConfigError(
                f"project root must stay outside reserved KB target {target}"
            )


def _assert_private_target_available(
    selected: Path,
    fingerprint: str,
    *,
    vault_uuid: str | None = None,
    except_scope_id: str | None = None,
) -> None:
    owner = _target_reservation_owner(
        selected,
        fingerprint,
        vault_uuid=vault_uuid,
        except_scope_id=except_scope_id,
        reserved_policies=POLICIES,
    )
    if owner is not None:
        raise ProjectConfigError(
            f"this KB is reserved by another Latch scope ({owner})"
        )


def _validate_private_target(root: Path, kb_dir: str | os.PathLike[str]) -> Path:
    """Reject broad or project-overlapping private KB directories."""
    selected = validated_kb_path(kb_dir)
    try:
        home = Path.home().resolve(strict=True)
    except OSError:
        home = Path.home().resolve(strict=False)
    for boundary, label in (
        (Path(selected.anchor), "filesystem root"),
        (home, "home directory"),
    ):
        if _same_path(selected, boundary) or _is_relative_to(boundary, selected):
            raise ProjectConfigError(
                f"Private KB must not overlap the {label}: {selected}"
            )
    for boundary, label in (
        (root, "selected project root"),
        (control_root().resolve(strict=False), "Latch control state"),
        (_install_home().resolve(strict=False), "Latch application checkout"),
    ):
        if (
            _same_path(selected, boundary)
            or _is_relative_to(selected, boundary)
            or _is_relative_to(boundary, selected)
        ):
            raise ProjectConfigError(
                f"Private KB must not overlap the {label}: {selected}"
            )
    # A vault must stay outside every already-authorized project root, not just
    # the root currently being configured.
    directory = control_root() / ROOTS_DIR_NAME
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise ProjectConfigError(
                f"root authorizations must be a real directory: {directory}"
            )
        for record in directory.iterdir():
            if record.suffix != ".json":
                continue
            payload = _read_regular_json(record, label="local root authorization")
            raw_root = payload.get("root")
            if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
                raise ProjectConfigError(
                    f"local root authorization has invalid root: {record}"
                )
            other = Path(os.path.normpath(raw_root))
            if (
                _same_path(selected, other)
                or _is_relative_to(selected, other)
                or _is_relative_to(other, selected)
            ):
                raise ProjectConfigError(
                    f"Private KB must stay outside authorized project root {other}"
                )
    return selected


@_quiesce_scope_mutation
def authorize_scope(
    root_value: str | os.PathLike[str],
    *,
    kb_dir: str | os.PathLike[str] | None = None,
    vault_uuid: str | None = None,
    mode: str = MODE_LATCHED,
) -> ResolvedScope:
    """Authorize one root against the central binding for its scope UUID.

    Selecting an existing KB is read-only: this function reads an existing
    immutable UUID when present but never creates a DB, table, or in-vault
    marker.
    """
    root = _require_scope_root(root_value)
    marker = _load_marker(root)
    if mode not in MODES:
        raise ProjectConfigError(f"unsupported scope mode: {mode!r}")
    with scope_registry_lock():
        _assert_project_root_outside_reserved_targets(root)
        parent = resolve(root.parent)
        if marker.policy == POLICY_SHARED and parent.policy == POLICY_PRIVATE:
            raise ProjectConfigError(
                f"a Shared scope cannot be nested below Private scope {parent.project_root}"
            )
        binding_path = scope_binding_path(marker.scope_id)
        existing_binding = (
            _load_scope_binding(marker.scope_id)
            if binding_path.exists() or binding_path.is_symlink()
            else None
        )
        if marker.policy == POLICY_SHARED:
            if kb_dir is not None or vault_uuid is not None:
                raise ProjectConfigError(
                    "a Shared scope uses the preserved global KB and cannot select another KB"
                )
            selected = _global_kb_dir(
                required=True,
                check_private_collision=True,
            )
            assert selected is not None
        else:
            if kb_dir is None and existing_binding is not None:
                selected = _validate_private_target(root, existing_binding.kb_dir)
            elif kb_dir is None:
                raise ProjectConfigError(
                    "a Private scope requires an existing or new empty KB directory"
                )
            else:
                selected = _validate_private_target(root, kb_dir)
            global_kb = _global_kb_dir(required=False)
            if global_kb is not None and _paths_overlap(selected, global_kb):
                raise ProjectConfigError("a Private scope cannot bind the shared/global KB")
        _assert_project_root_outside_reserved_targets(
            root,
            additional_targets=(selected,),
        )
        fingerprint = _directory_fingerprint(selected)
        observed_uuid = _read_vault_uuid(selected)
        if vault_uuid is not None:
            requested_uuid = _canonical_uuid(vault_uuid, label="vault_uuid")
            if observed_uuid is not None and not hmac.compare_digest(
                requested_uuid, observed_uuid
            ):
                raise ProjectConfigError(
                    "selected KB immutable UUID does not match the requested UUID"
                )
            observed_uuid = requested_uuid
        if marker.policy == POLICY_PRIVATE:
            _assert_private_target_available(
                selected,
                fingerprint,
                vault_uuid=observed_uuid,
                except_scope_id=marker.scope_id,
            )
        if existing_binding is not None:
            binding = existing_binding
            if binding.policy != marker.policy:
                raise ProjectConfigError(
                    "scope policy conflicts with its existing machine binding"
                )
            if not _same_path(binding.kb_dir, selected) or not hmac.compare_digest(
                binding.kb_fingerprint, fingerprint
            ):
                raise ProjectConfigError(
                    "this scope UUID is already bound to a different KB; use an explicit repin transition"
                )
            if binding.vault_uuid is None and observed_uuid is not None:
                raise ProjectConfigError(
                    "bound KB established an immutable vault identity; finalize that identity before authorizing another root",
                    code=LOCK_VAULT_IDENTITY_PENDING,
                )
            if binding.vault_uuid is not None and observed_uuid is None:
                raise ProjectConfigError(
                    "bound KB immutable vault identity is missing"
                )
            if (
                binding.vault_uuid is not None
                and observed_uuid is not None
                and not hmac.compare_digest(binding.vault_uuid, observed_uuid)
            ):
                raise ProjectConfigError("bound KB immutable vault identity changed")
            # Existing central mode wins.  Authorizing a copied root must not
            # implicitly relatch or unlatch every alias.
        else:
            binding = _write_scope_binding(
                marker.scope_id,
                policy=marker.policy,
                mode=mode,
                kb_dir=selected,
                kb_fingerprint=fingerprint,
                vault_uuid=observed_uuid,
            )
        root_path = local_binding_path(root)
        if root_path.exists() or root_path.is_symlink():
            existing = _load_root_authorization(root)
            if (
                existing.kind != ROOT_KIND_SCOPE
                or existing.scope_id != marker.scope_id
                or existing.policy != marker.policy
                or existing.marker_fingerprint != marker.fingerprint
            ):
                raise ProjectConfigError(
                    "this root already has conflicting machine-local state"
                )
        else:
            _write_root_scope_authorization(root, marker)
        target = resolve(root)
        if target.state != binding.mode:
            raise ProjectConfigError(
                f"scope authorization did not resolve as {binding.mode}: {target.reason}"
            )
        return target


@_quiesce_scope_mutation
def set_scope_mode(
    root_value: str | os.PathLike[str],
    mode: str,
) -> ResolvedScope:
    if mode not in MODES:
        raise ProjectConfigError(f"unsupported scope mode: {mode!r}")
    with scope_registry_lock():
        root, _marker, _authorization, binding = _authorized_scope_components(
            root_value
        )
        _validate_live_binding(binding)
        if binding.mode == mode:
            return resolve(root)
        _write_scope_binding(
            binding.scope_id,
            policy=binding.policy,
            mode=mode,
            kb_dir=binding.kb_dir,
            kb_fingerprint=binding.kb_fingerprint,
            vault_uuid=binding.vault_uuid,
        )
        return resolve(root)


@_quiesce_scope_mutation
def repin_private_scope(
    root_value: str | os.PathLike[str],
    kb_dir: str | os.PathLike[str],
) -> ResolvedScope:
    with scope_registry_lock():
        root, _marker, _authorization, binding = _authorized_scope_components(
            root_value
        )
        if binding.policy != POLICY_PRIVATE:
            raise ProjectConfigError("only a Private scope can be repinned independently")
        selected = _validate_private_target(root, kb_dir)
        global_kb = _global_kb_dir(required=False)
        if global_kb is not None and _paths_overlap(selected, global_kb):
            raise ProjectConfigError("a Private scope cannot bind the shared/global KB")
        fingerprint = _directory_fingerprint(selected)
        observed_uuid = _read_vault_uuid(selected)
        _assert_private_target_available(
            selected,
            fingerprint,
            vault_uuid=observed_uuid,
            except_scope_id=binding.scope_id,
        )
        _write_scope_binding(
            binding.scope_id,
            policy=binding.policy,
            mode=binding.mode,
            kb_dir=selected,
            kb_fingerprint=fingerprint,
            vault_uuid=observed_uuid,
        )
        return resolve(root)


def _matching_shared_bindings(
    kb_dir: Path,
    fingerprint: str,
) -> list[ScopeBinding]:
    directory = control_root() / SCOPES_DIR_NAME
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ProjectConfigError(
            f"scope bindings must be a real directory: {directory}"
        )
    matches: list[ScopeBinding] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        binding = _scope_binding_from_payload(
            path,
            _read_regular_json(path, label="machine scope binding"),
        )
        if (
            binding.policy == POLICY_SHARED
            and _same_path(binding.kb_dir, kb_dir)
            and hmac.compare_digest(binding.kb_fingerprint, fingerprint)
        ):
            matches.append(binding)
    return matches


def _finalize_shared_global_bindings(
    kb_dir: Path,
    fingerprint: str,
    vault_uuid: str,
) -> None:
    """Finalize every exact binding for one shared/global vault.

    All conflicts are checked before the first write.  If a process stops
    between the atomic per-record writes, retrying is safe: finalized records
    retain their revisions and pending records are completed.
    """
    bindings = _matching_shared_bindings(kb_dir, fingerprint)
    compatibility: CompatibilityBinding | None = None
    compatibility_path = compatibility_binding_path()
    if compatibility_path.exists() or compatibility_path.is_symlink():
        compatibility = _load_compatibility_binding()
        if not (
            _same_path(compatibility.kb_dir, kb_dir)
            and hmac.compare_digest(
                compatibility.kb_fingerprint, fingerprint
            )
        ):
            compatibility = None

    for binding in bindings:
        if binding.vault_uuid is not None and not hmac.compare_digest(
            binding.vault_uuid, vault_uuid
        ):
            raise ProjectConfigError(
                f"Shared scope {binding.scope_id} records a different vault identity"
            )
    if (
        compatibility is not None
        and compatibility.vault_uuid is not None
        and not hmac.compare_digest(compatibility.vault_uuid, vault_uuid)
    ):
        raise ProjectConfigError(
            "the compatibility binding records a different vault identity"
        )

    for binding in bindings:
        if binding.vault_uuid is None:
            _write_scope_binding(
                binding.scope_id,
                policy=binding.policy,
                mode=binding.mode,
                kb_dir=kb_dir,
                kb_fingerprint=fingerprint,
                vault_uuid=vault_uuid,
                revision=binding.revision,
            )
    if compatibility is not None and compatibility.vault_uuid is None:
        _write_compatibility_binding(
            kb_dir,
            fingerprint,
            vault_uuid,
            revision=compatibility.revision,
        )


def finalize_scope_vault_identity(
    root_value: str | os.PathLike[str],
    *,
    expected_revision: str,
    vault_uuid: str,
    continuity_root: str | os.PathLike[str] | None = None,
) -> ResolvedScope:
    """Record the immutable UUID created under an already-leased binding.

    Creating a brand-new empty vault is intentionally a two-step operation:
    authorization records the exact empty directory, then the first DB open
    creates its immutable identity.  This narrow finalizer joins those steps
    without changing the binding revision or accepting a repin race.
    """
    expected = _hex(expected_revision, 32, label="expected scope revision")
    requested_uuid = _canonical_uuid(vault_uuid, label="vault_uuid")
    with scope_registry_lock():
        root, _marker, _authorization, binding = _authorized_scope_components(
            root_value
        )
        current = validated_kb_path(binding.kb_dir)
        fingerprint = _directory_fingerprint(current)
        if not hmac.compare_digest(fingerprint, binding.kb_fingerprint):
            raise ProjectConfigError("bound KB directory identity changed before finalization")
        live_revision = _target_revision(binding.revision, current, fingerprint)
        if not hmac.compare_digest(live_revision, expected):
            raise ProjectConfigError(
                "scope binding changed before vault identity finalization"
            )
        observed_uuid = _read_vault_uuid(current)
        if observed_uuid is None or not hmac.compare_digest(
            observed_uuid, requested_uuid
        ):
            raise ProjectConfigError(
                "new vault identity does not match the bound KB"
            )
        if binding.policy == POLICY_PRIVATE:
            if binding.vault_uuid is not None:
                if not hmac.compare_digest(binding.vault_uuid, requested_uuid):
                    raise ProjectConfigError("bound KB immutable vault identity changed")
                return resolve(continuity_root or root)
            _assert_private_target_available(
                current,
                fingerprint,
                vault_uuid=requested_uuid,
                except_scope_id=binding.scope_id,
            )
        else:
            global_kb = _global_kb_dir(
                required=True,
                check_private_collision=True,
            )
            assert global_kb is not None
            if not _same_path(global_kb, current):
                raise ProjectConfigError(
                    "the global KB pin changed before vault identity finalization"
                )
        if binding.policy == POLICY_SHARED:
            _finalize_shared_global_bindings(
                current,
                fingerprint,
                requested_uuid,
            )
        else:
            _write_scope_binding(
                binding.scope_id,
                policy=binding.policy,
                mode=binding.mode,
                kb_dir=current,
                kb_fingerprint=fingerprint,
                vault_uuid=requested_uuid,
                revision=binding.revision,
            )
        return resolve(continuity_root or root)


def finalize_compatibility_vault_identity(
    *,
    expected_target_revision: str,
    vault_uuid: str,
    continuity_root: str | os.PathLike[str] | None = None,
) -> ResolvedScope:
    """Finalize first identity adoption for the exact compatibility vault."""
    expected = _hex(
        expected_target_revision,
        32,
        label="expected compatibility revision",
    )
    requested_uuid = _canonical_uuid(vault_uuid, label="vault_uuid")
    with scope_registry_lock():
        binding = _load_compatibility_binding()
        current = validated_kb_path(binding.kb_dir)
        fingerprint = _directory_fingerprint(current)
        if not hmac.compare_digest(fingerprint, binding.kb_fingerprint):
            raise ProjectConfigError(
                "compatibility KB directory identity changed before finalization"
            )
        live_revision = _target_revision(binding.revision, current, fingerprint)
        if not hmac.compare_digest(live_revision, expected):
            raise ProjectConfigError(
                "compatibility binding changed before vault identity finalization"
            )
        global_kb = _global_kb_dir(
            required=True,
            check_private_collision=True,
        )
        assert global_kb is not None
        if not _same_path(global_kb, current):
            raise ProjectConfigError(
                "the global KB pin changed before vault identity finalization"
            )
        observed_uuid = _read_vault_uuid(current)
        if observed_uuid is None or not hmac.compare_digest(
            observed_uuid, requested_uuid
        ):
            raise ProjectConfigError(
                "new vault identity does not match the compatibility KB"
            )
        _finalize_shared_global_bindings(
            current,
            fingerprint,
            requested_uuid,
        )
        return resolve(continuity_root or os.getcwd())


def authorized_scope_roots(scope_id: str) -> list[Path]:
    """Return canonical roots locally authorized for one central scope."""
    canonical = _canonical_uuid(scope_id, label="scope_id")
    directory = control_root() / ROOTS_DIR_NAME
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ProjectConfigError(f"root authorizations must be a real directory: {directory}")
    roots: list[Path] = []
    for path in directory.iterdir():
        if path.suffix != ".json":
            continue
        payload = _read_regular_json(path, label="local root authorization")
        if payload.get("kind") != ROOT_KIND_SCOPE or payload.get("scope_id") != canonical:
            continue
        raw_root = payload.get("root")
        if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
            raise ProjectConfigError(f"local root authorization has invalid root: {path}")
        root = Path(os.path.normpath(raw_root))
        authorization = _load_root_authorization(root)
        if authorization.scope_id == canonical:
            roots.append(root)
    return sorted(roots, key=lambda item: os.path.normcase(str(item)))


@_quiesce_scope_mutation
def reauthorize_shared_scope(root_value: str | os.PathLike[str]) -> ResolvedScope:
    """Bind a Shared scope to the current explicit global pin after repin."""
    with scope_registry_lock():
        root, marker, _authorization, current = _authorized_scope_components(
            root_value
        )
        if marker.policy != POLICY_SHARED or current.policy != POLICY_SHARED:
            raise ProjectConfigError("only a Shared scope follows the global KB pin")
        selected = _global_kb_dir(
            required=True,
            check_private_collision=True,
        )
        assert selected is not None
        fingerprint = _directory_fingerprint(selected)
        _write_scope_binding(
            current.scope_id,
            policy=POLICY_SHARED,
            mode=current.mode,
            kb_dir=selected,
            kb_fingerprint=fingerprint,
            vault_uuid=_read_vault_uuid(selected),
        )
        return resolve(root)


@_quiesce_scope_mutation
def convert_shared_scope_to_private(
    root_value: str | os.PathLike[str],
    kb_dir: str | os.PathLike[str],
) -> ResolvedScope:
    """Lock down one whole Shared scope for future activity, without copying.

    The central binding changes first.  Therefore a crash or unavailable alias
    leaves stale aliases LOCKED; none can continue reading the global KB.
    """
    root = _require_scope_root(root_value)
    with scope_registry_lock():
        marker = _load_marker(root)
        authorization = _load_root_authorization(root)
        if (
            authorization.kind != ROOT_KIND_SCOPE
            or authorization.scope_id != marker.scope_id
        ):
            raise ProjectConfigError(
                "only an authorized Shared scope can become Private"
            )
        current = _load_scope_binding(marker.scope_id)
        if current.policy == POLICY_SHARED:
            if (
                marker.policy != POLICY_SHARED
                or authorization.policy != POLICY_SHARED
                or authorization.marker_fingerprint != marker.fingerprint
            ):
                raise ProjectConfigError(
                    "only an intact authorized Shared scope can become Private"
                )
        elif current.policy == POLICY_PRIVATE:
            # This is only an idempotent repair path for an interrupted
            # Shared-to-Private transition.  It never changes Private back.
            allowed_residue = bool(
                marker.policy == POLICY_PRIVATE
                and authorization.policy in POLICIES
                and authorization.marker_fingerprint
                == _marker_fingerprint(current.scope_id, authorization.policy)
            ) or bool(
                marker.policy == POLICY_SHARED
                and authorization.policy == POLICY_SHARED
                and authorization.marker_fingerprint == marker.fingerprint
            )
            if not allowed_residue:
                raise ProjectConfigError(
                    "Private scope state is not a repairable transition residue"
                )
        else:
            raise ProjectConfigError("unsupported central scope policy")

        selected = _validate_private_target(root, kb_dir)
        global_kb = _global_kb_dir(required=False)
        if global_kb is not None and _paths_overlap(selected, global_kb):
            raise ProjectConfigError("the new Private scope must use a separate KB")
        fingerprint = _directory_fingerprint(selected)
        vault_uuid = _read_vault_uuid(selected)
        _assert_private_target_available(
            selected,
            fingerprint,
            vault_uuid=vault_uuid,
            except_scope_id=current.scope_id,
        )
        if current.policy == POLICY_SHARED:
            current = _write_scope_binding(
                current.scope_id,
                policy=POLICY_PRIVATE,
                mode=current.mode,
                kb_dir=selected,
                kb_fingerprint=fingerprint,
                vault_uuid=vault_uuid,
            )
        else:
            if not _same_path(current.kb_dir, selected) or not hmac.compare_digest(
                current.kb_fingerprint, fingerprint
            ):
                raise ProjectConfigError(
                    "the interrupted Private transition is bound to another KB"
                )
            if current.vault_uuid is None and vault_uuid is not None:
                current = _write_scope_binding(
                    current.scope_id,
                    policy=POLICY_PRIVATE,
                    mode=current.mode,
                    kb_dir=selected,
                    kb_fingerprint=fingerprint,
                    vault_uuid=vault_uuid,
                    revision=current.revision,
                )
            if current.vault_uuid is not None and vault_uuid is None:
                raise ProjectConfigError(
                    "bound KB immutable vault identity is missing"
                )
            if (
                current.vault_uuid is not None
                and vault_uuid is not None
                and not hmac.compare_digest(current.vault_uuid, vault_uuid)
            ):
                raise ProjectConfigError("bound KB immutable vault identity changed")

        for alias in authorized_scope_roots(current.scope_id):
            if not alias.is_dir():
                continue
            try:
                alias_marker = _load_marker(alias)
                alias_authorization = _load_root_authorization(alias)
            except ProjectConfigError:
                # A missing/tampered alias remains a Private LOCKED boundary.
                continue
            if (
                alias_marker.scope_id != current.scope_id
                or alias_authorization.scope_id != current.scope_id
            ):
                continue
            if alias_marker.policy == POLICY_SHARED:
                if (
                    alias_authorization.policy != POLICY_SHARED
                    or alias_authorization.marker_fingerprint
                    != alias_marker.fingerprint
                ):
                    continue
                atomic_json(
                    alias_marker.marker_path,
                    {
                        "format": FORMAT_VERSION,
                        "scope_id": current.scope_id,
                        "policy": POLICY_PRIVATE,
                    },
                    mode=0o644,
                )
                _write_root_scope_authorization(alias, _load_marker(alias))
            elif alias_marker.policy == POLICY_PRIVATE:
                if (
                    alias_authorization.policy == POLICY_PRIVATE
                    and alias_authorization.marker_fingerprint
                    == alias_marker.fingerprint
                ):
                    continue
                if (
                    alias_authorization.policy == POLICY_SHARED
                    and alias_authorization.marker_fingerprint
                    == _marker_fingerprint(current.scope_id, POLICY_SHARED)
                ):
                    _write_root_scope_authorization(alias, alias_marker)

        result = resolve(root)
        if result.state == MODE_LOCKED:
            raise ProjectConfigError(
                f"scope is safely LOCKED after partial Shared-to-Private transition: {result.reason}"
            )
        return result


@_quiesce_scope_mutation
def create_off_boundary(
    root_value: str | os.PathLike[str],
) -> ResolvedScope:
    """Create a machine-local OFF boundary without creating a vault."""
    root = _require_scope_root(root_value)
    portable, local = _candidate_flags(root)
    if portable:
        target = resolve(root)
        if target.project_root != root or target.scope_id is None:
            raise ProjectConfigError(f"cannot unlatch unsafe scope state: {target.reason}")
        return set_scope_mode(root, MODE_UNLATCHED)
    with scope_registry_lock():
        _assert_project_root_outside_reserved_targets(root)
        portable, local = _candidate_flags(root)
        if portable:
            raise ProjectConfigError(
                "scope state changed while creating the OFF boundary; retry"
            )
        if local:
            authorization = _load_root_authorization(root)
            if authorization.kind == ROOT_KIND_OFF:
                return resolve(root)
            raise ProjectConfigError(
                "this root has conflicting machine-local scope state"
            )
        parent = require_latched(root.parent)
        atomic_json(
            local_binding_path(root),
            {
                "format": FORMAT_VERSION,
                "kind": ROOT_KIND_OFF,
                "root": str(root),
                "policy": parent.policy,
                "remembered_scope_id": parent.scope_id,
                "remembered_revision": parent.revision,
                "remembered_kb_dir": str(parent.kb_dir),
                "remembered_target_fingerprint": parent.target_fingerprint,
                "remembered_vault_uuid": parent.vault_uuid,
                "revision": secrets.token_hex(16),
            },
        )
        return resolve(root)


@_quiesce_scope_mutation
def remove_off_boundary(root_value: str | os.PathLike[str]) -> ResolvedScope:
    root = _require_scope_root(root_value)
    with scope_registry_lock():
        authorization = _load_root_authorization(root)
        if authorization.kind != ROOT_KIND_OFF:
            raise ProjectConfigError("this root is not an OFF boundary")
        parent = require_latched(root.parent)
        if (
            parent.scope_id != authorization.remembered_scope_id
            or parent.revision != authorization.remembered_revision
            or parent.target_fingerprint
            != authorization.remembered_target_fingerprint
        ):
            raise ProjectConfigError(
                "the remembered parent scope changed; choose Shared or Private explicitly"
            )
        # Removing an OFF boundary can reveal the identical inherited parent
        # revision that existed before it was created.  Persist a root-local
        # epoch first so tasks from before the off/on cycle remain stale even
        # though the effective KB is the same.  A crash between these writes
        # leaves the safer OFF boundary intact.
        _bump_continuity_epoch(root)
        durable_unlink(authorization.record_path)
        return resolve(root)


@_quiesce_scope_mutation
def replace_off_boundary(
    root_value: str | os.PathLike[str],
    *,
    policy: str,
) -> ScopeMarker:
    """Replace one OFF boundary with explicit portable scope intent.

    The new marker is published while OFF authority still denies data access,
    then the subtree continuity epoch is bumped before OFF is removed.  A
    crash at any intermediate point therefore leaves the root LOCKED or OFF,
    and retrying the same explicit choice completes safely.
    """
    root = _require_scope_root(root_value)
    if policy not in POLICIES:
        raise ProjectConfigError(f"unsupported scope policy: {policy!r}")
    parent = resolve(root.parent)
    if policy == POLICY_SHARED and parent.policy == POLICY_PRIVATE:
        raise ProjectConfigError(
            f"a Shared scope cannot be nested below Private scope {parent.project_root}"
        )
    with scope_registry_lock():
        authorization = _load_root_authorization(root)
        if authorization.kind != ROOT_KIND_OFF:
            raise ProjectConfigError("this root is not an OFF boundary")
        marker_path = portable_marker_path(root)
        if marker_path.exists() or marker_path.is_symlink():
            marker = _load_marker(root)
            if marker.policy != policy:
                raise ProjectConfigError(
                    "an interrupted OFF replacement chose a different scope policy"
                )
        else:
            marker = ScopeMarker(
                project_root=root,
                scope_id=str(uuid.uuid4()),
                policy=policy,
                marker_path=marker_path,
                fingerprint="",
            )
            _ensure_real_directory(marker_path.parent)
            atomic_json(
                marker_path,
                {
                    "format": FORMAT_VERSION,
                    "scope_id": marker.scope_id,
                    "policy": marker.policy,
                },
                mode=0o644,
            )
            marker = _load_marker(root)
        _bump_continuity_epoch(root)
        durable_unlink(authorization.record_path)
        return marker


@_quiesce_scope_mutation
def write_binding(
    root_value: str | os.PathLike[str],
    *,
    mode: str,
    kb_dir: str | os.PathLike[str] | None,
) -> ResolvedScope:
    """Compatibility helper for old tests/callers; public flows choose policy."""
    root = _require_scope_root(root_value)
    marker_path = portable_marker_path(root)
    if not (marker_path.exists() or marker_path.is_symlink()):
        global_kb = _global_kb_dir(required=False)
        policy = (
            POLICY_SHARED
            if kb_dir is None
            or (global_kb is not None and _same_path(validated_kb_path(kb_dir), global_kb))
            else POLICY_PRIVATE
        )
        create_scope(root, policy=policy)
    marker = _load_marker(root)
    target = resolve(root)
    if target.state == MODE_LOCKED:
        target = authorize_scope(
            root,
            kb_dir=kb_dir if marker.policy == POLICY_PRIVATE else None,
            mode=mode,
        )
    elif (
        marker.policy == POLICY_PRIVATE
        and kb_dir is not None
        and target.kb_dir is not None
        and not _same_path(target.kb_dir, validated_kb_path(kb_dir))
    ):
        # Preserve the legacy helper's repin contract for internal callers and
        # older integrations.  Public flows call ``repin_private_scope``
        # directly, but silently ignoring a changed target here would leave
        # old session receipts valid for the previous vault.
        target = repin_private_scope(root, kb_dir)
        if target.state != mode:
            target = set_scope_mode(root, mode)
    elif target.state != mode:
        target = set_scope_mode(root, mode)
    return target


def validated_kb_path(kb_dir: str | os.PathLike[str]) -> Path:
    directory = Path(kb_dir).expanduser()
    if not directory.is_absolute():
        raise ProjectConfigError(f"bound KB must be absolute: {directory}")
    lexical = Path(os.path.normpath(str(directory)))
    try:
        if lexical.is_symlink():
            raise OSError("path is a symlink")
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ProjectConfigError(f"bound KB is missing or unsafe: {lexical}: {exc}") from exc
    if not resolved.is_dir() or not _same_path(resolved, lexical):
        raise ProjectConfigError(f"bound KB directory identity changed: {lexical} -> {resolved}")
    return resolved


def validated_bound_kb_dir(binding: ResolvedScope) -> Path | None:
    if binding.state != MODE_LATCHED:
        return None
    if binding.kb_dir is None:
        return None
    selected = validated_kb_path(binding.kb_dir)
    fingerprint = _directory_fingerprint(selected)
    if binding.target_fingerprint is None or not hmac.compare_digest(
        fingerprint, binding.target_fingerprint
    ):
        raise ProjectConfigError(
            f"bound KB directory identity changed: {binding.kb_dir}"
        )
    return selected


def mark_kb_target(kb_dir: str | os.PathLike[str]) -> None:
    """Compatibility no-op that validates read-only and never mutates a KB."""
    validated_kb_path(kb_dir)


def kb_target_is_marked(kb_dir: str | os.PathLike[str]) -> bool:
    """The retired in-vault marker grants no authority."""
    validated_kb_path(kb_dir)
    return False


def _lock_file_for_root(root: Path) -> Path:
    directory = control_root() / LOCKS_DIR_NAME
    _ensure_real_directory(directory)
    return directory / f"root-{_root_key(root)}-{TRANSITION_LOCK_FILE_NAME}"


def _validate_advisory_file(path: Path, fd: int) -> None:
    opened = os.fstat(fd)
    current = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise ProjectConfigError(f"transition lock must be a single-link regular file: {path}")


@contextlib.contextmanager
def _transition_file_lock(path: Path, busy_message: str):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ProjectConfigError(f"transition lock must be a regular file: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    locked = False
    try:
        _validate_advisory_file(path, fd)
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            _validate_advisory_file(path, fd)
        except OSError as exc:
            raise ProjectTransitionBusyError(busy_message) from exc
        yield
    finally:
        if locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


@contextlib.contextmanager
def transition_lock(root_value: str | os.PathLike[str]):
    """Fail fast when another scope transition owns this non-Git root."""
    root = _require_scope_root(root_value)
    path = _lock_file_for_root(root)
    with _transition_file_lock(
        path,
        f"another latch/unlatch transition is already running for {root}",
    ):
        yield


@contextlib.contextmanager
def scope_registry_lock():
    """Serialize central bindings and reverse-target uniqueness checks."""
    directory = control_root() / LOCKS_DIR_NAME
    _ensure_real_directory(directory)
    with _transition_file_lock(
        directory / "scope-registry.lock",
        "another Latch scope binding transition is already running",
    ):
        yield


def _session_path(session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return control_root() / SESSIONS_DIR_NAME / f"{digest}.json"


def _session_payload(
    target: ResolvedScope,
    root_value: str | os.PathLike[str],
    *,
    inactive: bool,
) -> dict[str, object]:
    return {
        "format": FORMAT_VERSION,
        "scope_id": target.scope_id,
        "revision": target.revision,
        "target_fingerprint": target.target_fingerprint,
        "source": target.source,
        "inactive": inactive,
    }


def _record_session_target(
    target: ResolvedScope,
    session_id: str,
    root_value: str | os.PathLike[str],
    *,
    inactive: bool,
) -> str:
    path = _session_path(session_id)
    expected = _session_payload(target, root_value, inactive=inactive)
    if path.exists() or path.is_symlink():
        existing = _read_regular_json(path, label="session scope receipt")
        if existing == expected:
            return target.revision
        raise ProjectConfigError(
            "this agent task already belongs to another or older Latch scope; start a fresh task"
        )
    atomic_json(path, expected)
    return target.revision


def record_session_boundary(
    root_value: str | os.PathLike[str],
    session_id: str,
) -> str | None:
    """Fence a task that starts while its scope is LOCKED or UNLATCHED.

    This stores only machine-local scope identity. It never resolves or opens a
    KB target, and a later latch transition makes the receipt stale so the task
    cannot inherit newly authorized knowledge.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    target = resolve(root_value)
    path = _session_path(session_id)
    expected = _session_payload(target, root_value, inactive=True)
    if path.exists() or path.is_symlink():
        _read_regular_json(path, label="session scope receipt")
    # Seeing a task while its location is inactive is a one-way safety fence.
    # Replacing an older active receipt only removes authority; it can never
    # grant the task access to this or another KB later.
    atomic_json(path, expected)
    return target.revision


def record_session_binding(
    root_value: str | os.PathLike[str],
    session_id: str,
) -> str | None:
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    target = require_latched(root_value)
    return _record_session_target(
        target,
        session_id,
        root_value,
        inactive=False,
    )


def current_session_revision(
    root_value: str | os.PathLike[str],
    session_id: str,
    *,
    resolved_target: ResolvedScope | None = None,
    allow_identity_recovery: bool = False,
) -> str | None:
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    target = resolved_target or resolve(root_value)
    recoverable = bool(
        allow_identity_recovery
        and target.state == MODE_LOCKED
        and target.reason_code
        in {LOCK_VAULT_IDENTITY_INITIALIZING, LOCK_VAULT_IDENTITY_PENDING}
        and target.remembered_kb_dir is not None
        and target.target_fingerprint is not None
    )
    if target.state != MODE_LATCHED and not recoverable:
        return None
    path = _session_path(session_id)
    if not (path.exists() or path.is_symlink()):
        return None
    payload = _read_regular_json(path, label="session scope receipt")
    expected = _session_payload(
        target,
        root_value,
        inactive=False,
    )
    return target.revision if payload == expected else None


def clear_session_binding(
    root_value: str | os.PathLike[str],
    session_id: str,
    *,
    expected_revision: str | None = None,
) -> None:
    """Validate ownership while retaining the permanent stale-task fence."""
    if not session_id:
        return
    target = resolve(root_value)
    if expected_revision is not None and target.revision != expected_revision:
        return
    path = _session_path(session_id)
    if not (path.exists() or path.is_symlink()):
        return
    payload = _read_regular_json(path, label="session scope receipt")
    if expected_revision is not None and payload.get("revision") != expected_revision:
        return
    # Deliberately retained: agent products can resume an old conversation
    # after SessionEnd, so deleting this mapping would let it cross a vault.
    return


def git_root(start: str | os.PathLike[str] | None = None) -> Path | None:
    """Optional discovery helper only; never a scope authority."""
    current = _canonical_start(start)
    for directory in (current, *current.parents):
        entry = directory / ".git"
        if entry.exists() or entry.is_symlink():
            return directory
    return None


def _git_dir(root: Path) -> Path:
    """Compatibility parser for tests/adapters; scope state never lives here."""
    entry = root / ".git"
    if entry.is_symlink():
        raise ProjectConfigError(f"Git metadata entry must not be a symlink: {entry}")
    if entry.is_dir():
        directory = entry
    elif entry.is_file():
        lines = entry.read_text(encoding="utf-8").splitlines()
        if len(lines) != 1 or not lines[0].startswith("gitdir: "):
            raise ProjectConfigError(f"invalid Git metadata pointer: {entry}")
        directory = Path(lines[0][len("gitdir: ") :].strip()).expanduser()
        if not directory.is_absolute():
            directory = root / directory
    else:
        raise ProjectConfigError(f"Git metadata entry is missing: {entry}")
    resolved = directory.resolve(strict=True)
    if not resolved.is_dir():
        raise ProjectConfigError(f"Git metadata is not a directory: {resolved}")
    return resolved
