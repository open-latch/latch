"""Tests for vault-owned runtime settings and maintenance configuration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import paths  # noqa: E402
import lockfile  # noqa: E402
import project_config  # noqa: E402


def _fake_executable(directory: Path, name: str = "codex") -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    executable = directory / f"{name}{suffix}"
    executable.write_text(
        "@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_vault_maintenance_runner_is_absolute_persisted_and_validated(
    tmp_path: Path,
) -> None:
    executable = _fake_executable(tmp_path)
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    settings = tmp_path / "runtime_settings.json"
    settings.write_text(
        '{"daemon_idle_ttl_s": 41}\n',
        encoding="utf-8",
    )

    paths.write_maintenance_runner(
        backend="CoDeX",
        executable=str(executable),
        home=str(tmp_path),
        search_path=os.pathsep.join((str(tmp_path), str(helper_dir))),
        runtime_settings_file=settings,
    )

    assert paths.configured_maintenance_runner(settings) == (
        "codex",
        str(executable),
        str(tmp_path),
        os.pathsep.join((str(tmp_path), str(helper_dir))),
    )
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "daemon_idle_ttl_s": 41,
        "maintenance_backend": "codex",
        "maintenance_executable": str(executable),
        "maintenance_home": str(tmp_path),
        "maintenance_path": os.pathsep.join((str(tmp_path), str(helper_dir))),
    }


def test_vault_maintenance_runner_fails_closed_when_missing_or_stale(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "runtime_settings.json"
    executable = _fake_executable(tmp_path)
    with pytest.raises(ValueError, match="not configured"):
        paths.configured_maintenance_runner(settings)

    settings.write_text(
        json.dumps({
            "maintenance_backend": "codex",
            "maintenance_executable": "codex",
            "maintenance_home": str(tmp_path),
            "maintenance_path": str(tmp_path),
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be absolute"):
        paths.configured_maintenance_runner(settings)

    for field, value, message in (
        ("maintenance_executable", "~/codex", "executable must be absolute"),
        ("maintenance_home", "~", "home must be absolute"),
        (
            "maintenance_path",
            os.pathsep.join(("relative-bin", str(tmp_path))),
            "PATH entry must be absolute",
        ),
    ):
        payload = {
            "maintenance_backend": "codex",
            "maintenance_executable": str(executable),
            "maintenance_home": str(tmp_path),
            "maintenance_path": str(tmp_path),
        }
        payload[field] = value
        settings.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            paths.configured_maintenance_runner(settings)

    settings.write_text(
        json.dumps({
            "maintenance_backend": "codex",
            "maintenance_executable": str(tmp_path / "missing-codex"),
            "maintenance_home": str(tmp_path),
            "maintenance_path": str(tmp_path),
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not executable"):
        paths.configured_maintenance_runner(settings)


def test_vault_maintenance_runner_preserves_stable_executable_symlink(
    tmp_path: Path,
) -> None:
    versioned = _fake_executable(tmp_path, "codex-v1")
    stable = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    try:
        stable.symlink_to(versioned)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    settings = tmp_path / "runtime_settings.json"

    paths.write_maintenance_runner(
        backend="codex",
        executable=str(stable),
        home=str(tmp_path),
        search_path=str(tmp_path),
        runtime_settings_file=settings,
    )

    assert paths.configured_maintenance_runner(settings)[1] == str(stable)


def test_maintenance_path_drops_cwd_dependent_entries(tmp_path: Path) -> None:
    executable = tmp_path / "bin" / "codex"
    executable.parent.mkdir()
    system_bin = tmp_path / "system-bin"
    system_bin.mkdir()
    resolved = paths.resolve_maintenance_path(
        str(executable),
        env={"PATH": os.pathsep.join(("relative-bin", str(system_bin), ""))},
    )
    assert resolved.split(os.pathsep) == [
        str(executable.parent),
        str(system_bin),
    ]


def test_vault_runtime_settings_are_gitignored() -> None:
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "store/runtime_settings.json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_vault_daemon_ttl_is_scoped_and_invalid_policy_fails_soft(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "runtime_settings.json"
    settings.write_text('{"daemon_idle_ttl_s": 12.5}\n', encoding="utf-8")
    assert paths.configured_daemon_idle_ttl(
        default=3600,
        runtime_settings_file=settings,
    ) == 12.5

    settings.write_text('{"daemon_idle_ttl_s": 1e309}\n', encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="invalid daemon_idle_ttl_s"):
        assert paths.configured_daemon_idle_ttl(
            default=3600,
            runtime_settings_file=settings,
        ) == 3600

    target = tmp_path / "outside.json"
    target.write_text('{"daemon_idle_ttl_s": 1}\n', encoding="utf-8")
    settings.unlink()
    settings.symlink_to(target)
    with pytest.warns(RuntimeWarning, match="invalid vault runtime settings path"):
        assert paths.configured_daemon_idle_ttl(
            default=3600,
            runtime_settings_file=settings,
        ) == 3600
    with pytest.raises(ValueError, match="regular vault-local file"):
        paths.configured_maintenance_runner(settings)


def test_pin_refresh_requires_explicit_compatibility_reauthorization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    test_root = paths.validated_test_root()
    assert test_root is not None
    pin = Path(os.environ["LATCH_HOME"]) / "kb_location.json"
    vault = test_root / "vaults" / f"runtime-repin-{tmp_path.name}"
    vault.mkdir(parents=True)
    executable = _fake_executable(tmp_path)
    monkeypatch.setattr(paths, "KB_LOCATION_FILE", pin)
    monkeypatch.setattr(paths, "_PINNED_DIR", None)
    project = tmp_path / "project"
    project.mkdir()

    # Refresh sees the installer pin, but the data plane stays fail-closed until
    # the machine-local compatibility binding explicitly authorizes that exact
    # replacement vault.
    assert paths.project_dir(project) != vault
    pin.write_text(json.dumps({"kb_dir": str(vault)}) + "\n", encoding="utf-8")
    assert paths.refresh_pinned_dir() == vault
    locked = project_config.resolve(project)
    assert locked.state == project_config.MODE_LOCKED
    assert locked.reason_code == project_config.LOCK_GLOBAL_PIN_CHANGED
    with pytest.raises(lockfile.ProjectTargetChangedError, match="locked"):
        paths.write_maintenance_runner(
            backend="codex",
            executable=str(executable),
            home=str(tmp_path),
            search_path=str(tmp_path),
            project_path=project,
        )

    project_config.reauthorize_compatibility_binding()

    written = paths.write_maintenance_runner(
        backend="codex",
        executable=str(executable),
        home=str(tmp_path),
        search_path=str(tmp_path),
        project_path=project,
    )
    assert written == vault / paths.VAULT_RUNTIME_SETTINGS_FILENAME
    assert written.is_file()


def test_runtime_settings_write_holds_project_target_through_replace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    executable = _fake_executable(tmp_path)
    transition_entered = threading.Event()
    waiter: threading.Thread | None = None

    def write_while_transition_waits(updates, runtime_settings_file, **_kwargs):
        nonlocal waiter

        def transition() -> None:
            with lockfile.project_access_lock(str(project), exclusive=True):
                transition_entered.set()

        waiter = threading.Thread(target=transition)
        waiter.start()
        assert not transition_entered.wait(timeout=0.2)
        return runtime_settings_file

    monkeypatch.setattr(paths, "_write_vault_runtime_settings", write_while_transition_waits)
    written = paths.write_maintenance_runner(
        backend="codex",
        executable=str(executable),
        home=str(tmp_path),
        search_path=str(tmp_path),
        project_path=project,
    )
    assert written.name == paths.VAULT_RUNTIME_SETTINGS_FILENAME
    assert transition_entered.wait(timeout=2)
    assert waiter is not None
    waiter.join(timeout=2)
    assert not waiter.is_alive()
