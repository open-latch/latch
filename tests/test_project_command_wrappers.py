"""The public shell commands are thin status/confirmed-action frontends."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import paths
import project_config


@pytest.fixture
def command_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[Path, dict[str, str]]:
    test_root = paths.validated_test_root()
    assert test_root is not None
    repo = Path(__file__).resolve().parent.parent
    control = test_root / "command-wrapper-tests" / tmp_path.name
    shared = test_root / "vaults" / "command-wrapper-global" / tmp_path.name
    shared.mkdir(parents=True)
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(repo))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.setenv("LATCH_KB_DIR", str(shared))
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    env = dict(os.environ)
    env["LATCH_HOME"] = str(repo)
    env["LATCH_PYTHON"] = sys.executable
    return repo, env


def _run(script: Path, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_latch_and_unlatch_wrappers_round_trip_one_private_scope(
    command_env: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    repo, env = command_env
    root = tmp_path / "client"
    root.mkdir()
    latch = repo / "bin" / "latch.sh"
    unlatch = repo / "bin" / "unlatch.sh"

    inspected = _run(latch, root, env)
    assert inspected.returncode == 2
    assert "state    : LOCKED" in inspected.stdout
    assert not (root / ".latch" / "scope.json").exists()

    created = _run(
        latch,
        root,
        env,
        "--confirm",
        "latch",
        "--private",
        "--new-kb",
    )
    assert created.returncode == 0, created.stderr
    active = project_config.resolve(root)
    assert active.state == project_config.MODE_LATCHED
    assert active.policy == project_config.POLICY_PRIVATE
    original_kb = active.kb_dir

    off_preview = _run(unlatch, root, env)
    assert off_preview.returncode == 0
    assert project_config.resolve(root).state == project_config.MODE_LATCHED

    disabled = _run(unlatch, root, env, "--confirm", "unlatch")
    assert disabled.returncode == 0, disabled.stderr
    off = project_config.resolve(root)
    assert off.state == project_config.MODE_UNLATCHED
    assert off.remembered_kb_dir == original_kb

    restored = _run(latch, root, env, "--confirm", "latch")
    assert restored.returncode == 0, restored.stderr
    again = project_config.resolve(root)
    assert again.state == project_config.MODE_LATCHED
    assert again.kb_dir == original_kb


def test_public_wrappers_require_their_exact_confirmation_words(
    command_env: tuple[Path, dict[str, str]], tmp_path: Path,
) -> None:
    repo, env = command_env
    root = tmp_path / "client"
    root.mkdir()

    wrong_latch = _run(repo / "bin" / "latch.sh", root, env, "--confirm", "yes")
    wrong_unlatch = _run(
        repo / "bin" / "unlatch.sh", root, env, "--confirm", "latch"
    )
    assert wrong_latch.returncode == 2
    assert wrong_unlatch.returncode == 2
    assert not (root / ".latch" / "scope.json").exists()
