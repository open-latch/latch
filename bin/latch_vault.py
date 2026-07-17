#!/usr/bin/env python3
"""Quarantined launcher for consultant client work.

The launcher creates the binding consumed by ``src/vault_policy.py``.  Normal
Latch behavior remains unchanged when no protected-root tripwire exists.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Mapping, Sequence


_BOOTSTRAP_INSTALL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BOOTSTRAP_INSTALL_ROOT / "src"))
import vault_policy  # noqa: E402

SCHEMA_VERSION = vault_policy.SCHEMA_VERSION
STATE_DIR_NAME = vault_policy.STATE_DIR_NAME
MARKER_NAME = vault_policy.MARKER_NAME
STATIC_LINKS = ("vendor",)
INSTALL_ROOT = vault_policy.INSTALL_ROOT
MODEL_BLOCKER = INSTALL_ROOT / "bin" / "latch_vault_model_block.py"


class VaultError(RuntimeError):
    """A fail-closed vault binding or launch error."""


@dataclass(frozen=True)
class VaultState:
    protected_root: Path
    state_dir: Path
    home_dir: Path
    kb_dir: Path
    temp_dir: Path
    marker_path: Path
    binding_id: str


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _canonical_existing_dir(value: str | os.PathLike[str]) -> Path:
    candidate = Path(value).expanduser()
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"protected root does not resolve: {candidate}: {exc}") from exc
    if not root.is_dir():
        raise VaultError(f"protected root is not a directory: {root}")
    if root == Path(root.anchor):
        raise VaultError("refusing to use a filesystem root as the protected root")
    if _is_within(root, INSTALL_ROOT) or _is_within(INSTALL_ROOT, root):
        raise VaultError(
            "protected root and the Latch install must not contain one another"
        )
    return root


def _binding_id(root: Path) -> str:
    return vault_policy.binding_id(root, INSTALL_ROOT)


def _state_for(root: Path) -> VaultState:
    state_dir = root / STATE_DIR_NAME
    return VaultState(
        protected_root=root,
        state_dir=state_dir,
        home_dir=state_dir / "home",
        kb_dir=state_dir / "kb",
        temp_dir=state_dir / "tmp",
        marker_path=state_dir / MARKER_NAME,
        binding_id=_binding_id(root),
    )


def _assert_real_dir(path: Path, label: str) -> None:
    try:
        stat_result = path.lstat()
    except FileNotFoundError as exc:
        raise VaultError(f"{label} is missing: {path}") from exc
    if path.is_symlink():
        raise VaultError(f"{label} must not be a symlink: {path}")
    if not path.is_dir():
        raise VaultError(f"{label} is not a directory: {path}")
    if not stat_result:
        raise VaultError(f"could not inspect {label}: {path}")


def _mkdir_private(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        _assert_real_dir(path, label)
    else:
        path.mkdir(mode=0o700)
        _assert_real_dir(path, label)
    try:
        path.chmod(0o700)
    except OSError as exc:
        raise VaultError(f"could not secure {label}: {path}: {exc}") from exc


def _expected_marker(state: VaultState) -> dict[str, object]:
    return vault_policy.expected_marker(state.protected_root, INSTALL_ROOT)


def _write_marker(state: VaultState) -> None:
    expected = _expected_marker(state)
    if state.marker_path.exists() or state.marker_path.is_symlink():
        _load_marker(state)
        return

    temp_path = state.state_dir / f".{MARKER_NAME}.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(expected, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, state.marker_path)
        state.marker_path.chmod(0o600)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise VaultError(f"could not write vault binding: {exc}") from exc


def _load_marker(state: VaultState) -> None:
    if state.marker_path.is_symlink():
        raise VaultError(f"vault binding must not be a symlink: {state.marker_path}")
    try:
        raw = state.marker_path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise VaultError(f"vault binding is unreadable or malformed: {exc}") from exc
    if value != _expected_marker(state):
        raise VaultError(
            "vault binding does not exactly match this protected root and Latch install"
        )


def _static_link_source(name: str) -> Path:
    try:
        return (INSTALL_ROOT / name).resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"required static engine input is missing: {name}: {exc}") from exc


def _validate_static_link(home_dir: Path, name: str) -> None:
    source = _static_link_source(name)
    target = home_dir / name
    if not target.is_symlink():
        raise VaultError(f"static overlay path must be the expected symlink: {target}")
    try:
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"static overlay link is broken: {target}: {exc}") from exc
    if resolved != source:
        raise VaultError(f"static overlay link points to the wrong source: {target}")


def _ensure_static_link(home_dir: Path, name: str) -> None:
    source = _static_link_source(name)
    target = home_dir / name
    if target.exists() or target.is_symlink():
        _validate_static_link(home_dir, name)
        return
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        raise VaultError(
            f"could not create read-only static overlay link {target} -> {source}: {exc}"
        ) from exc


def _schema_bytes() -> bytes:
    source = INSTALL_ROOT / "src" / "schema.sql"
    try:
        return source.read_bytes()
    except OSError as exc:
        raise VaultError(f"required schema is missing or unreadable: {source}: {exc}") from exc


def _validate_schema_copy(home_dir: Path) -> None:
    schema_dir = home_dir / "src"
    _assert_real_dir(schema_dir, "vault schema directory")
    target = schema_dir / "schema.sql"
    if target.is_symlink() or not target.is_file():
        raise VaultError(f"vault schema must be a private regular file: {target}")
    try:
        actual = target.read_bytes()
    except OSError as exc:
        raise VaultError(f"vault schema is unreadable: {target}: {exc}") from exc
    if actual != _schema_bytes():
        raise VaultError("vault schema copy does not match this Latch install")


def _ensure_schema_copy(home_dir: Path) -> None:
    schema_dir = home_dir / "src"
    _mkdir_private(schema_dir, "vault schema directory")
    target = schema_dir / "schema.sql"
    if target.exists() or target.is_symlink():
        _validate_schema_copy(home_dir)
        return

    temp_path = schema_dir / f".schema.sql.{os.getpid()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_schema_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        target.chmod(0o400)
    except OSError as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise VaultError(f"could not create private vault schema: {exc}") from exc


def _configure_git_exclude(root: Path) -> bool:
    """Keep the local vault out of an ordinary clone without touching tracked files."""
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return False
    info_dir = git_dir / "info"
    if info_dir.exists() and (info_dir.is_symlink() or not info_dir.is_dir()):
        raise VaultError(f"unsafe git info directory: {info_dir}")
    info_dir.mkdir(mode=0o700, exist_ok=True)
    exclude = info_dir / "exclude"
    if exclude.is_symlink():
        raise VaultError(f"git exclude file must not be a symlink: {exclude}")
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    entry = f"/{STATE_DIR_NAME}/"
    if entry in {line.strip() for line in existing.splitlines()}:
        return True
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{entry}\n")
    return True


def initialize(root_value: str | os.PathLike[str]) -> VaultState:
    root = _canonical_existing_dir(root_value)
    try:
        existing_root = vault_policy.discover_root(root)
    except vault_policy.VaultPolicyError as exc:
        raise VaultError(str(exc)) from exc
    if existing_root is not None and existing_root != root:
        raise VaultError(
            f"refusing nested vault inside protected root {existing_root}"
        )
    state = _state_for(root)
    _mkdir_private(state.state_dir, "vault state directory")
    _mkdir_private(state.home_dir, "vault home directory")
    _mkdir_private(state.kb_dir, "vault KB directory")
    _mkdir_private(state.temp_dir, "vault temporary directory")
    _ensure_schema_copy(state.home_dir)
    for name in STATIC_LINKS:
        _ensure_static_link(state.home_dir, name)
    _write_marker(state)
    _configure_git_exclude(root)
    try:
        vault_policy.load_binding(root, INSTALL_ROOT)
    except vault_policy.VaultPolicyError as exc:
        raise VaultError(str(exc)) from exc
    return state


def _find_protected_root(start: Path) -> Path:
    try:
        current = start.resolve(strict=True)
    except OSError as exc:
        raise VaultError(f"current directory does not resolve: {start}: {exc}") from exc
    if not current.is_dir():
        current = current.parent
    for candidate in (current, *current.parents):
        # The state directory itself is the persistent tripwire.  A missing or
        # damaged marker must be reported as damage, never as an uninitialized
        # ordinary repository.
        state_dir = candidate / STATE_DIR_NAME
        if state_dir.exists() or state_dir.is_symlink():
            return candidate
    raise VaultError(
        f"no {STATE_DIR_NAME}/{MARKER_NAME} found; run `latch-vault init ROOT` first"
    )


def load(root_value: str | os.PathLike[str] | None = None) -> VaultState:
    root = _canonical_existing_dir(
        root_value if root_value is not None else _find_protected_root(Path.cwd())
    )
    state = _state_for(root)
    try:
        discovered_root = vault_policy.discover_root(root)
    except vault_policy.VaultPolicyError as exc:
        raise VaultError(str(exc)) from exc
    if discovered_root != root:
        raise VaultError("protected root tripwire does not match the requested root")
    _assert_real_dir(state.state_dir, "vault state directory")
    _assert_real_dir(state.home_dir, "vault home directory")
    _assert_real_dir(state.kb_dir, "vault KB directory")
    _assert_real_dir(state.temp_dir, "vault temporary directory")
    _validate_schema_copy(state.home_dir)
    for name in STATIC_LINKS:
        _validate_static_link(state.home_dir, name)
    _load_marker(state)
    vault_policy.load_binding(root, INSTALL_ROOT)
    return state


def build_environment(
    state: VaultState,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the fully bound child environment; never mutate the parent env."""
    env = dict(os.environ if base is None else base)
    env.update(
        {
            "LATCH_VAULT_MODE": "1",
            "LATCH_VAULT_PROTECTED_ROOT": str(state.protected_root),
            "LATCH_VAULT_BINDING_ID": state.binding_id,
            "LATCH_VAULT_FINGERPRINT": vault_policy.marker_fingerprint(
                _expected_marker(state)
            ),
            "LATCH_HOME": str(state.home_dir),
            "CLAUDE_KB_HOME": str(state.home_dir),
            "LATCH_KB_DIR": str(state.kb_dir),
            "CLAUDE_KB_DIR": str(state.kb_dir),
            "TMPDIR": str(state.temp_dir),
            "TEMP": str(state.temp_dir),
            "TMP": str(state.temp_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Vault mode uses the existing brute-force cosine fallback.  This
            # avoids loading a native SQLite extension into the NDA-specific
            # process while preserving deterministic search behavior.
            "LATCH_VAULT_DISABLE_SQLITE_VEC": "1",
            "CLAUDE_KB_LOG_RAW_QUERY": "0",
            "CLAUDE_KB_GIT_SNAPSHOT": "0",
            # Stop/SessionEnd are the transcript-compaction write path.  Keep
            # deterministic MCP reads and explicit local KB writes available,
            # but never auto-read a host transcript or auto-launch compaction.
            "LATCH_DISABLE_WRITE": "1",
            "CLAUDE_KB_DISABLE_WRITE": "1",
            "LATCH_MCP_LAUNCHER_LOG": str(state.home_dir / "mcp-launcher.log"),
            "CLAUDE_BIN": str(MODEL_BLOCKER),
            "CODEX_BIN": str(MODEL_BLOCKER),
            "CURSOR_AGENT_BIN": str(MODEL_BLOCKER),
        }
    )
    # These inherited overrides can write arbitrary paths.  Vault mode owns
    # their destination instead of trusting ambient shell configuration.
    for name in (
        "CLAUDE_KB_DEBUG_LOG",
        "CLAUDE_KB_IN_COMPACT",
        "LATCH_IN_COMPACT",
        "LATCH_MCP_FORCE_LEGACY",
        "LATCH_MCP_ALLOW_LEGACY_FALLBACK",
        "LATCH_MCP_LEGACY",
        "LATCH_MCP_INITIAL_PROJECT_CWD",
        "LATCH_MCP_RUNTIME_KEY",
        "LATCH_MCP_START_REASON",
        "LATCH_MCP_START_REQUEST_EPOCH",
    ):
        env.pop(name, None)
    return env


def _global_disable_reason() -> str | None:
    if os.environ.get("LATCH_DISABLE") or os.environ.get("CLAUDE_KB_DISABLE"):
        return "the Latch disable environment switch is set"
    if os.environ.get("LATCH_UNLATCHED"):
        return "LATCH_UNLATCHED is set"
    for name in ("DISABLE", "UNLATCHED"):
        if (INSTALL_ROOT / name).exists():
            return f"the global {name} safety sentinel exists"
    return None


def _receipt(state: VaultState, *, ready: bool) -> str:
    status = "READY" if ready else "INITIALIZED"
    return "\n".join(
        [
            f"Latch vault: {status}",
            f"Protected root: {state.protected_root}",
            f"Binding: {state.binding_id}",
            f"Fingerprint: {vault_policy.marker_fingerprint(_expected_marker(state))}",
            f"Vault state: {state.state_dir}",
            "Outer KB: disconnected in latch-vault launched sessions",
            "Latch model subprocesses: blocked (account identity unverified)",
            "Automatic transcript compaction: off",
            "Host coding-agent account/storage: unchanged; client policy applies",
            "Uninitialized repositories: ordinary Latch behavior unchanged",
            "This initialized root: Latch requires the vault launcher",
        ]
    )


def launch(
    host: str,
    state: VaultState,
    host_args: Sequence[str],
) -> None:
    disabled = _global_disable_reason()
    if disabled:
        raise VaultError(f"refusing to launch while {disabled}")
    host_path = shutil.which(host)
    if not host_path:
        raise VaultError(f"host command is not installed or not on PATH: {host}")
    host_path = str(Path(host_path).resolve(strict=True))
    if Path(host_path) == MODEL_BLOCKER.resolve(strict=True):
        raise VaultError("host command resolved to the vaulted-mode model blocker")

    cwd = Path.cwd().resolve(strict=True)
    if not _is_within(cwd, state.protected_root):
        raise VaultError(
            "launch must be invoked from inside the bound protected root; "
            "outside --root invocation is refused"
        )

    env = build_environment(state)
    print(_receipt(state, ready=True), file=sys.stderr, flush=True)
    os.execve(host_path, [host, *host_args], env)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latch-vault",
        description=(
            "Experimental consultant launcher: client-local Latch state with "
            "the outer KB disconnected and Latch-owned model calls blocked."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialize a protected client root")
    init.add_argument("root", nargs="?", default=".")

    status = sub.add_parser("status", help="show the deterministic vault binding")
    status.add_argument("root", nargs="?")

    for host in ("claude", "codex"):
        run = sub.add_parser(host, help=f"launch {host} inside an initialized vault")
        run.add_argument("--root", help="protected root; otherwise discover it upward from cwd")
        run.add_argument("host_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            state = initialize(args.root)
            print(_receipt(state, ready=False))
            print("Next: run `latch-vault claude` or `latch-vault codex` from this root.")
            return 0
        if args.command == "status":
            state = load(args.root)
            print(_receipt(state, ready=True))
            disabled = _global_disable_reason()
            print(f"Launch safety: {'BLOCKED — ' + disabled if disabled else 'ready'}")
            return 0
        state = load(args.root)
        host_args = list(args.host_args)
        if host_args and host_args[0] == "--":
            host_args.pop(0)
        launch(args.command, state, host_args)
        return 0
    except VaultError as exc:
        print(f"Latch vault: BLOCKED\nReason: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
