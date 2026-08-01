"""Negligible Codex bundle check and safe one-time self-repair."""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import agents_md_sync
import managed_doc_sync as mds
from versioning import ROOT, WIRING_VERSION

_MARKER_RE = re.compile(
    r"(?:latch-wiring-version:\s*|--latch-wiring-version\s+)([0-9]+)"
)
_LOCK_TIMEOUT_S = 5.0
_LOCK_POLL_S = 0.05


@dataclass(frozen=True)
class RepairResult:
    action: str
    notice: str | None = None
    restart_required: bool = False


def _manual(project: Path) -> str:
    if os.name == "nt":
        command = f'& "{sys.executable}" "{ROOT / "src" / "install_codex.py"}" --yes'
    else:
        command = (
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(str(ROOT / 'src' / 'install_codex.py'))} --yes"
        )
    return (
        f"from `{project}`, run `{command}`"
    )


def _embedded_version(text: str) -> int | None:
    match = _MARKER_RE.search(text)
    return int(match.group(1)) if match else None


def _owned_server_details(text: str) -> tuple[str, str]:
    """Return the installed latch command pair without trusting unrelated TOML."""
    import install_codex

    if (
        text.count(install_codex.BEGIN_MARK) != 1
        or text.count(install_codex.END_MARK) != 1
        or text.index(install_codex.BEGIN_MARK) >= text.index(install_codex.END_MARK)
    ):
        raise RuntimeError(
            "Codex config has no single recognized latch-managed MCP region"
        )
    managed_region = text.split(install_codex.BEGIN_MARK, 1)[1].split(
        install_codex.END_MARK, 1
    )[0]
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Codex config is invalid TOML: {exc}") from exc
    servers = parsed.get("mcp_servers")
    if not isinstance(servers, dict):
        raise RuntimeError("Codex config has no latch-owned MCP server entry")
    owned = [name for name in install_codex.ALL_SERVER_NAMES if name in servers]
    if len(owned) != 1 or not isinstance(servers[owned[0]], dict):
        raise RuntimeError(
            "Codex config must contain exactly one recognized latch-owned MCP server entry"
        )
    if not any(
        install_codex._table_header(line) == (f"mcp_servers.{owned[0]}", False)
        for line in managed_region.splitlines()
    ):
        raise RuntimeError(
            "Codex latch MCP table is outside the recognized managed region"
        )
    server = servers[owned[0]]
    command = server.get("command")
    args = server.get("args")
    if (
        not isinstance(command, str)
        or not command.strip()
        or not isinstance(args, list)
        or not args
        or not isinstance(args[0], str)
        or not args[0].strip()
    ):
        raise RuntimeError("Codex latch MCP entry has an unsupported command shape")
    normalized_server = args[0].replace("\\", "/").rstrip("/")
    expected_servers = {
        str(ROOT / "src" / name).replace("\\", "/").rstrip("/")
        for name in ("mcp_server.py", "mcp_launcher_win.py")
    }
    if re.match(r"^[A-Za-z]:/", normalized_server):
        normalized_server = normalized_server.casefold()
        expected_servers = {value.casefold() for value in expected_servers}
    if normalized_server not in expected_servers:
        raise RuntimeError(
            "Codex managed MCP entry does not target this Latch installation"
        )
    env = server.get("env")
    embedded = env.get("LATCH_WIRING_VERSION") if isinstance(env, dict) else None
    if embedded is not None:
        try:
            installed = int(embedded)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Codex MCP entry has an invalid wiring version") from exc
        if installed > WIRING_VERSION:
            raise RuntimeError(
                f"Codex MCP entry has newer wiring {installed}; refusing downgrade"
            )
    return command, args[0]


def _validate_owned_hooks(text: str) -> bool:
    """Validate installed latch hooks and return whether the file is owned."""
    import codex_hooks

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        normalized = text.replace("\\", "/")
        if any(f"/src/hooks/{name}" in normalized for name in codex_hooks.OWNED_HOOK_BASENAMES):
            raise RuntimeError(f"Codex hooks are invalid JSON: {exc}") from exc
        return False
    hooks = obj.get("hooks") if isinstance(obj, dict) else None
    commands = [
        str(hook.get("command", ""))
        for groups in hooks.values() if isinstance(hooks, dict) and isinstance(groups, list)
        for group in groups if isinstance(group, dict)
        for hook in group.get("hooks", []) if isinstance(group.get("hooks"), list)
        if codex_hooks._is_owned_hook(hook)
    ] if isinstance(hooks, dict) else []
    if not commands:
        return False
    for command in commands:
        version = _embedded_version(command)
        if version is not None and version > WIRING_VERSION:
            raise RuntimeError(
                f"Codex hooks have newer wiring {version}; refusing downgrade"
            )
    return True


def _console_python(command: str) -> str:
    """Map a managed Windows pythonw MCP command back to console Python."""
    normalized = command.replace("\\", "/")
    if normalized.lower().endswith("/pythonw.exe"):
        return command[: -len("pythonw.exe")] + "python.exe"
    return command


def _managed_skill_names(skills_dir: Path) -> tuple[str, ...]:
    import install_codex

    managed: list[str] = []
    for name in install_codex.CODEX_SKILL_NAMES:
        target = skills_dir / name / "SKILL.md"
        if target.is_file() and install_codex.CODEX_SKILL_MARKER in target.read_text(
            encoding="utf-8", errors="replace"
        ):
            managed.append(name)
    return tuple(managed)


def _assert_no_newer_skills(skills_dir: Path, names: tuple[str, ...]) -> None:
    import install_codex

    for name in names:
        target = skills_dir / name / "SKILL.md"
        if not target.is_file():
            continue
        text = target.read_text(encoding="utf-8")
        if install_codex.CODEX_SKILL_MARKER not in text:
            continue
        version = _embedded_version(text)
        if version is not None and version > WIRING_VERSION:
            raise RuntimeError(f"{target} has newer wiring {version}; refusing downgrade")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return kernel32.GetLastError() != 87
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _write_lock_pid(fd: int) -> None:
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))


@contextmanager
def _repair_lock(config_path: Path):
    """Serialize global Codex bundle repair across tasks and MCP processes."""
    lock_path = config_path.with_name("latch-wiring.lock")
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(
                str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            try:
                raw_pid = lock_path.read_text(encoding="utf-8").splitlines()[0]
                pid = int(raw_pid)
            except (OSError, ValueError, IndexError):
                pid = None
            malformed_stale = False
            if pid is None:
                try:
                    malformed_stale = (
                        time.time() - lock_path.stat().st_mtime >= _LOCK_TIMEOUT_S
                    )
                except OSError:
                    pass
            if (pid is not None and not _pid_alive(pid)) or malformed_stale:
                try:
                    lock_path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Codex wiring repair lock remained busy for {_LOCK_TIMEOUT_S:g}s"
                )
            time.sleep(_LOCK_POLL_S)
    try:
        _write_lock_pid(fd)
    except Exception:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def repair_project(
    project: str | Path,
    *,
    config_path: Path | None = None,
    hooks_path: Path | None = None,
    skills_dir: Path | None = None,
    repair_global: bool = False,
) -> RepairResult:
    """Repair an already-wired Codex bundle once after WIRING_VERSION changes.

    AGENTS.md is the project opt-in and project marker. It is written last, so
    any partial failure leaves the older marker in place and the next task can
    retry. MCP startup may additionally repair a recognized global managed
    bundle when this project marker is current or absent.
    """
    root = Path(project).expanduser().resolve()
    agents_path = root / "AGENTS.md"
    initial_state = agents_md_sync.wiring_state(agents_path)
    if initial_state == mds.CURRENT and not repair_global:
        return RepairResult("unchanged")
    if initial_state in (mds.ABSENT, mds.MISSING) and not repair_global:
        return RepairResult("unmanaged")
    import codex_hooks
    import install_codex
    import install_engine

    config = config_path or install_codex.CONFIG_PATH
    hooks = hooks_path or install_codex.HOOKS_PATH
    skills = skills_dir or install_codex.DEFAULT_SKILLS_DIR
    try:
        if not config.is_file():
            raise RuntimeError("Codex config has no recognized latch-owned MCP block")
        with _repair_lock(config):
            state = agents_md_sync.wiring_state(agents_path)
            if state == mds.CURRENT and not repair_global:
                return RepairResult("unchanged")
            if state in (mds.ABSENT, mds.MISSING) and not repair_global:
                return RepairResult("unmanaged")
            if state == mds.NEWER:
                return RepairResult(
                    "newer",
                    "_⚠ Codex project wiring is newer than this Latch engine. Latch "
                    "did not downgrade it; update Latch before opening another task._",
                )
            if state == mds.INVALID:
                return RepairResult(
                    "invalid",
                    f"_⚠ Latch could not read the Codex wiring marker. This task will "
                    f"continue; {_manual(root)} manually._",
                )

            current_config = config.read_text(encoding="utf-8")
            command, _installed_server = _owned_server_details(current_config)
            console_python = _console_python(command)
            canonical_server = str(ROOT / "src" / "mcp_server.py")
            desired_command, desired_server = install_engine.mcp_launch_command(
                console_python, canonical_server
            )
            hook_changes: list[str] = []
            desired_hooks = ""
            owned_hooks = False
            if hooks.is_file():
                current_hooks = hooks.read_text(encoding="utf-8")
                owned_hooks = _validate_owned_hooks(current_hooks)
                if owned_hooks:
                    desired_hooks, hook_changes = codex_hooks.merge_hooks(
                        current_hooks,
                        console_python,
                        str(ROOT / "src" / "hooks" / "codex_session_start.py"),
                    )
            desired_config, config_changes = install_codex.merge_config(
                current_config,
                desired_command,
                desired_server,
                enable_hooks=owned_hooks,
            )

            managed_skills = _managed_skill_names(skills)
            if managed_skills:
                _assert_no_newer_skills(skills, managed_skills)
                install_codex._raise_skill_collisions(
                    install_codex.codex_skill_collisions(
                        skills, names=managed_skills
                    )
                )

            if hook_changes:
                codex_hooks.write_hooks(hooks, desired_hooks)
            skill_changes = (
                install_codex.sync_codex_skills(skills, names=managed_skills)
                if managed_skills
                else []
            )
            # The config's LATCH_WIRING_VERSION is the global-bundle commit
            # marker, so write it only after optional owned surfaces converge.
            if config_changes:
                install_codex.write_config(config, desired_config)

            agents_action = "unchanged"
            if not repair_global and state in (mds.OLDER, mds.LEGACY):
                agents_action = agents_md_sync.sync(agents_path, create=False)
                if agents_action not in ("synced", "unchanged"):
                    raise RuntimeError(
                        f"AGENTS.md wiring repair returned {agents_action}"
                    )
    except Exception as exc:
        return RepairResult(
            "error",
            f"_⚠ Latch could not safely repair older Codex wiring ({exc}). This "
            f"task will continue; {_manual(root)} manually._",
        )

    changed = bool(config_changes or hook_changes or skill_changes or agents_action == "synced")
    if not changed:
        return RepairResult("unchanged")
    trust_notice = (
        " Codex may ask you to trust the refreshed SessionStart hook."
        if hook_changes
        else ""
    )
    return RepairResult(
        "synced",
        "_↻ Latch repaired older Codex wiring once; only recognized latch-owned "
        "files/regions changed and backups were kept. Restart or open a new task "
        f"to activate required MCP startup.{trust_notice}_",
        restart_required=True,
    )


def repair_from_mcp_startup() -> RepairResult:
    """Repair older Codex wiring from the MCP path every existing install runs."""
    adapter = (os.environ.get("LATCH_ADAPTER") or "").strip().lower()
    if adapter and adapter != "codex":
        return RepairResult("not-codex")
    if not adapter and not (
        (os.environ.get("LATCH_MODEL_BACKEND") or "").strip().lower() == "codex"
        and (os.environ.get("LATCH_GATE_BACKEND") or "").strip().lower() == "codex"
        and (os.environ.get("LATCH_TOOL_SURFACE") or "").strip().lower() == "latch"
    ):
        return RepairResult("not-codex")
    inherited = (os.environ.get("LATCH_WIRING_VERSION") or "").strip()
    if inherited:
        try:
            installed = int(inherited)
        except ValueError:
            installed = 0
        if installed > WIRING_VERSION:
            return RepairResult(
                "newer",
                "_⚠ Codex MCP wiring is newer than this Latch engine. Latch did "
                "not downgrade it; update Latch before opening another task._",
            )
        if installed == WIRING_VERSION:
            return RepairResult("unchanged")
    return repair_project(Path.cwd(), repair_global=True)
