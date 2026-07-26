"""Tests for vault-owned runtime settings and maintenance configuration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import paths  # noqa: E402


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


def test_refresh_pinned_dir_moves_runtime_policy_to_new_vault(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pin = tmp_path / "kb_location.json"
    vault = tmp_path / "store"
    vault.mkdir()
    executable = _fake_executable(tmp_path)
    monkeypatch.setattr(paths, "KB_LOCATION_FILE", pin)
    monkeypatch.setattr(paths, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(paths, "_PINNED_DIR", None)

    # Simulate quickstart first observing a legacy/unpinned snapshot, then
    # creating the fresh-install pin in the same process.
    assert paths.project_dir(tmp_path / "project") != vault
    pin.write_text(json.dumps({"kb_dir": str(vault)}) + "\n", encoding="utf-8")
    assert paths.refresh_pinned_dir() == vault

    written = paths.write_maintenance_runner(
        backend="codex",
        executable=str(executable),
        home=str(tmp_path),
        search_path=str(tmp_path),
        project_path=tmp_path / "project",
    )
    assert written == vault / paths.VAULT_RUNTIME_SETTINGS_FILENAME
    assert written.is_file()
