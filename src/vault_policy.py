"""Fail-closed policy for explicitly protected consultant roots.

This module is intentionally standard-library-only.  Normal Latch processes
call :func:`enforce` before resolving a KB path or accepting an MCP connection.
When no ``.latch-vault`` state directory exists and no vault environment is
present, the function returns immediately and ordinary Latch behavior is
unchanged.

The state directory is the opt-in tripwire.  Once it exists, a missing,
malformed, copied, nested, or mismatched binding is an error; Latch must never
fall back to an outer KB merely because the marker became unreadable.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any, Mapping


SCHEMA_VERSION = 2
POLICY_NAME = "consultant-vault-fail-closed-v1"
STATE_DIR_NAME = ".latch-vault"
MARKER_NAME = "binding.json"
INSTALL_ROOT = Path(__file__).resolve().parent.parent

_ACTIVE_ENV = "LATCH_VAULT_MODE"
_ROOT_ENV = "LATCH_VAULT_PROTECTED_ROOT"
_BINDING_ENV = "LATCH_VAULT_BINDING_ID"
_FINGERPRINT_ENV = "LATCH_VAULT_FINGERPRINT"
_VAULT_ENV_NAMES = (_ACTIVE_ENV, _ROOT_ENV, _BINDING_ENV, _FINGERPRINT_ENV)
_BLOCKED_OPERATIONS = frozenset({"compaction", "outer_import", "seed"})


class VaultPolicyError(RuntimeError):
    """Raised before a protected process can touch non-vault Latch state."""


@dataclass(frozen=True)
class VaultBinding:
    protected_root: Path
    state_dir: Path
    home_dir: Path
    kb_dir: Path
    temp_dir: Path
    marker_path: Path
    install_root: Path
    binding_id: str
    fingerprint: str

    def connection_metadata(self) -> dict[str, Any]:
        """Non-secret identity checked at every proxy/daemon boundary."""
        return {
            "schema_version": SCHEMA_VERSION,
            "protected_root": str(self.protected_root),
            "kb_dir": str(self.kb_dir),
            "binding_id": self.binding_id,
            "fingerprint": self.fingerprint,
        }


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _canonical_existing_dir(value: str | os.PathLike[str], label: str) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise VaultPolicyError(f"{label} does not resolve: {candidate}: {exc}") from exc
    if not resolved.is_dir():
        raise VaultPolicyError(f"{label} is not a directory: {resolved}")
    return resolved


def _assert_private_real_dir(path: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise VaultPolicyError(f"{label} is missing or unreadable: {path}: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise VaultPolicyError(f"{label} must not be a symlink: {path}")
    if not stat.S_ISDIR(mode):
        raise VaultPolicyError(f"{label} is not a directory: {path}")
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise VaultPolicyError(
            f"{label} grants group or other access: {path}"
        )


def binding_id(root: Path, install_root: Path = INSTALL_ROOT) -> str:
    material = f"latch-vault-v{SCHEMA_VERSION}\0{root}\0{install_root}\0{POLICY_NAME}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def expected_marker(root: Path, install_root: Path = INSTALL_ROOT) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": POLICY_NAME,
        "protected_root": str(root),
        "install_root": str(install_root),
        "binding_id": binding_id(root, install_root),
    }


def marker_fingerprint(marker: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(marker), separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def state_paths(
    root: str | os.PathLike[str], install_root: Path = INSTALL_ROOT
) -> VaultBinding:
    canonical_root = _canonical_existing_dir(root, "protected root")
    canonical_install = _canonical_existing_dir(install_root, "Latch install")
    state_dir = canonical_root / STATE_DIR_NAME
    marker = expected_marker(canonical_root, canonical_install)
    return VaultBinding(
        protected_root=canonical_root,
        state_dir=state_dir,
        home_dir=state_dir / "home",
        kb_dir=state_dir / "kb",
        temp_dir=state_dir / "tmp",
        marker_path=state_dir / MARKER_NAME,
        install_root=canonical_install,
        binding_id=str(marker["binding_id"]),
        fingerprint=marker_fingerprint(marker),
    )


def discover_root(start: str | os.PathLike[str]) -> Path | None:
    """Find exactly one opted-in ancestor, treating state-dir damage as opt-in."""
    candidate = Path(start).expanduser()
    try:
        current = candidate.resolve(strict=True)
    except OSError:
        # Ordinary Latch historically accepts lexical/nonexistent paths (most
        # notably Windows drive paths exercised on POSIX).  A non-strict walk
        # preserves that behavior while still finding any existing ancestor
        # tripwire.  Active vault mode performs a strict check in enforce().
        current = candidate.resolve(strict=False)
    if current.is_file():
        current = current.parent
    roots: list[Path] = []
    for candidate in (current, *current.parents):
        state_dir = candidate / STATE_DIR_NAME
        try:
            state_dir.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise VaultPolicyError(
                f"vault state tripwire is unreadable: {state_dir}: {exc}"
            ) from exc
        roots.append(candidate)
    if len(roots) > 1:
        joined = ", ".join(str(path) for path in roots)
        raise VaultPolicyError(f"nested or conflicting protected roots: {joined}")
    return roots[0] if roots else None


def load_binding(
    root: str | os.PathLike[str], install_root: Path = INSTALL_ROOT
) -> VaultBinding:
    binding = state_paths(root, install_root)
    _assert_private_real_dir(binding.state_dir, "vault state directory")
    _assert_private_real_dir(binding.home_dir, "vault home directory")
    _assert_private_real_dir(binding.kb_dir, "vault KB directory")
    _assert_private_real_dir(binding.temp_dir, "vault temporary directory")
    try:
        mode = binding.marker_path.lstat().st_mode
    except OSError as exc:
        raise VaultPolicyError(
            f"vault binding is missing or unreadable: {binding.marker_path}: {exc}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise VaultPolicyError(
            f"vault binding must be a private regular file: {binding.marker_path}"
        )
    if os.name != "nt" and stat.S_IMODE(mode) & 0o077:
        raise VaultPolicyError("vault binding grants group or other access")
    try:
        value = json.loads(binding.marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VaultPolicyError(f"vault binding is malformed: {exc}") from exc
    expected = expected_marker(binding.protected_root, binding.install_root)
    if value != expected:
        raise VaultPolicyError(
            "vault binding does not exactly match this protected root and Latch install"
        )
    return binding


def _same_path(value: str, expected: Path) -> bool:
    try:
        return Path(value).expanduser().resolve(strict=True) == expected
    except OSError:
        return False


def _same_future_path(value: str, expected: Path) -> bool:
    """Compare an output file path whose leaf may not exist yet."""
    try:
        return (
            Path(value).expanduser().resolve(strict=False)
            == expected.resolve(strict=False)
        )
    except OSError:
        return False


def _require_exact_path_env(
    env: Mapping[str, str], name: str, expected: Path
) -> None:
    value = (env.get(name) or "").strip()
    if not value or not _same_path(value, expected):
        raise VaultPolicyError(f"{name} must be pinned to {expected}")


def validate_environment(binding: VaultBinding, env: Mapping[str, str]) -> None:
    if env.get(_ACTIVE_ENV) != "1":
        raise VaultPolicyError("protected root requires an active latch-vault binding")
    if (env.get(_ROOT_ENV) or "").strip() != str(binding.protected_root):
        raise VaultPolicyError("vault protected-root environment does not match the marker")
    if (env.get(_BINDING_ENV) or "").strip() != binding.binding_id:
        raise VaultPolicyError("vault binding id does not match the marker")
    if (env.get(_FINGERPRINT_ENV) or "").strip() != binding.fingerprint:
        raise VaultPolicyError("vault binding fingerprint does not match the marker")
    if env.get("LATCH_VAULT_DISABLE_SQLITE_VEC") != "1":
        raise VaultPolicyError("vaulted SQLite native-extension policy is missing")
    for name in ("LATCH_HOME", "CLAUDE_KB_HOME"):
        _require_exact_path_env(env, name, binding.home_dir)
    for name in ("LATCH_KB_DIR", "CLAUDE_KB_DIR"):
        _require_exact_path_env(env, name, binding.kb_dir)
    for name in ("TMPDIR", "TEMP", "TMP"):
        _require_exact_path_env(env, name, binding.temp_dir)
    blocker = binding.install_root / "bin" / "latch_vault_model_block.py"
    for name in ("CLAUDE_BIN", "CODEX_BIN", "CURSOR_AGENT_BIN"):
        _require_exact_path_env(env, name, blocker)
    exact_values = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_KB_LOG_RAW_QUERY": "0",
        "CLAUDE_KB_GIT_SNAPSHOT": "0",
        "LATCH_DISABLE_WRITE": "1",
        "CLAUDE_KB_DISABLE_WRITE": "1",
    }
    for name, expected in exact_values.items():
        if env.get(name) != expected:
            raise VaultPolicyError(
                f"{name} must equal {expected!r} in consultant vault mode"
            )
    launcher_log = env.get("LATCH_MCP_LAUNCHER_LOG") or ""
    expected_log = binding.home_dir / "mcp-launcher.log"
    if not _same_future_path(launcher_log, expected_log):
        raise VaultPolicyError(
            f"LATCH_MCP_LAUNCHER_LOG must be pinned to {expected_log}"
        )
    for name in (
        "LATCH_MCP_FORCE_LEGACY",
        "LATCH_MCP_ALLOW_LEGACY_FALLBACK",
        "LATCH_MCP_LEGACY",
    ):
        if env.get(name):
            raise VaultPolicyError(f"{name} is forbidden in consultant vault mode")
    debug_path = (env.get("CLAUDE_KB_DEBUG_LOG") or "").strip()
    if debug_path:
        try:
            resolved = Path(debug_path).expanduser().resolve(strict=False)
        except OSError as exc:
            raise VaultPolicyError(f"invalid vault debug path: {exc}") from exc
        if not _is_within(resolved, binding.state_dir):
            raise VaultPolicyError("CLAUDE_KB_DEBUG_LOG escapes the protected root")


def enforce(
    project_cwd: str | os.PathLike[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path = INSTALL_ROOT,
) -> VaultBinding | None:
    """Return the validated binding, or ``None`` for ordinary unvaulted use."""
    environment = os.environ if env is None else env
    mode = environment.get(_ACTIVE_ENV)
    has_partial_env = any(environment.get(name) for name in _VAULT_ENV_NAMES)
    cwd_value = project_cwd or os.getcwd()
    if mode == "1" or has_partial_env:
        cwd = _canonical_existing_dir(cwd_value, "project directory")
    else:
        cwd = Path(cwd_value).expanduser().resolve(strict=False)
    discovered = discover_root(cwd)

    if mode != "1":
        if discovered is not None:
            raise VaultPolicyError(
                f"protected root {discovered} requires `latch-vault claude` or "
                "`latch-vault codex`; outer Latch fallback is blocked"
            )
        if has_partial_env:
            raise VaultPolicyError("partial or inactive latch-vault environment is forbidden")
        return None

    root_value = (environment.get(_ROOT_ENV) or "").strip()
    if not root_value:
        raise VaultPolicyError("active latch-vault environment has no protected root")
    binding = load_binding(root_value, install_root)
    if discovered is None or discovered != binding.protected_root:
        raise VaultPolicyError(
            "vaulted process or request is outside its bound protected root"
        )
    if not _is_within(cwd, binding.protected_root):
        raise VaultPolicyError("project directory escapes the protected root")
    validate_environment(binding, environment)
    return binding


def validate_connection_metadata(
    metadata: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    install_root: Path = INSTALL_ROOT,
) -> VaultBinding | None:
    project_cwd = metadata.get("project_cwd")
    if not isinstance(project_cwd, str) or not project_cwd.strip():
        # Pre-capability ordinary proxies did not send project_cwd.  Preserve
        # their established upgrade/fresh-task path, but never extend that
        # compatibility lane into a vaulted runtime.
        binding = enforce(env=env, install_root=install_root)
        if binding is not None:
            raise VaultPolicyError("vaulted MCP connection has no project_cwd")
        if metadata.get("vault_binding") not in (None, {}):
            raise VaultPolicyError(
                "unvaulted runtime rejected vaulted connection metadata"
            )
        return None
    binding = enforce(project_cwd, env=env, install_root=install_root)
    supplied = metadata.get("vault_binding")
    if binding is None:
        if supplied not in (None, {}):
            raise VaultPolicyError("unvaulted runtime rejected vaulted connection metadata")
        return None
    if not isinstance(supplied, Mapping) or dict(supplied) != binding.connection_metadata():
        raise VaultPolicyError("MCP vault binding handshake does not match the marker")
    return binding


def require_operation_allowed(
    operation: str, project_cwd: str | os.PathLike[str] | None = None
) -> None:
    binding = enforce(project_cwd)
    if binding is not None and operation in _BLOCKED_OPERATIONS:
        raise VaultPolicyError(
            f"{operation} is disabled by consultant vault server policy"
        )
