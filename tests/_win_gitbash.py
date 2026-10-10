"""Run the repository's bash scripts under Git Bash on Windows.

A bare ``"bash"`` argv[0] is wrong on Windows: ``subprocess`` goes through
CreateProcess, which searches the Windows system directory before PATH, so it
starts ``C:\\Windows\\System32\\bash.exe``, the WSL launcher, even when Git
Bash is first on PATH. Without an installed distribution that launcher prints
a UTF-16 "no installed distributions" notice and exits 1.

``bash()`` therefore returns the literal ``"bash"`` on POSIX, so argv is
unchanged there, and on Windows the first Git Bash in PATH order (the bash a
GitHub ``shell: bash`` step uses), never the WSL launcher.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _under(path: str, root: str | None) -> bool:
    if not root:
        return False
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    return path.startswith(root.rstrip("\\/") + os.sep)


def _is_wsl_launcher(path: str) -> bool:
    # System32\bash.exe and the WindowsApps execution alias both start WSL.
    windows = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    local = os.environ.get("LOCALAPPDATA")
    apps = os.path.join(local, "Microsoft", "WindowsApps") if local else None
    return _under(path, windows) or _under(path, apps)


@functools.lru_cache(maxsize=None)
def _git_bash() -> str | None:
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry.strip('"'):
            continue
        candidate = os.path.join(entry.strip('"'), "bash.exe")
        if os.path.isfile(candidate) and not _is_wsl_launcher(candidate):
            return candidate
    # Git for Windows' installer default puts only Git\cmd on PATH; its
    # bash.exe lives in the sibling Git\bin.
    git = shutil.which("git")
    if git:
        candidate = Path(git).resolve().parent.parent / "bin" / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def bash() -> str:
    """argv[0] for running a repository bash script."""
    if os.name != "nt":
        return "bash"
    found = _git_bash()
    if found is None:
        pytest.fail(
            "Git Bash not found: no bash.exe outside the WSL launcher is on PATH "
            "and none sits next to git; install Git for Windows or put its bin "
            "directory on PATH"
        )
    return found


@functools.lru_cache(maxsize=None)
def _cygpath_unix(path: str) -> str:
    result = subprocess.run(
        [bash(), "-lc", 'cygpath -u "$1"', "bash", path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def output_mentions_path(output: str, path: str | Path) -> bool:
    """Whether a script's output names ``path``.

    On POSIX this is plain ``str(path) in output``. Under Git Bash a script
    spells a Windows path as ``/d/a/...`` (its own ``pwd``), ``D:/a/...``
    (``pwd -W``), or the caller's ``D:\\a\\...``, so on Windows any of those
    spellings counts, compared case-insensitively.
    """
    if os.name != "nt":
        return str(path) in output
    flat = output.replace("\\", "/").lower()
    forms = (Path(path).as_posix(), _cygpath_unix(str(path)))
    return any(form.lower() in flat for form in forms)
