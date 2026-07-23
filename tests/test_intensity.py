from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import paths  # noqa: E402
import db  # noqa: E402
import gate  # noqa: E402
import intensity_cli  # noqa: E402


def _fake_executable(directory: Path, name: str = "codex") -> Path:
    suffix = ".cmd" if os.name == "nt" else ""
    executable = directory / f"{name}{suffix}"
    executable.write_text(
        "@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def test_pytest_isolates_install_wide_intensity_settings() -> None:
    assert paths.LATCH_SETTINGS_FILE != paths.KB_ROOT / "latch_settings.json"
    assert not paths.LATCH_SETTINGS_FILE.exists()
    assert os.environ["LATCH_INTENSITY"] == "full"


def test_pytest_intensity_isolation_propagates_to_child_process(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "latch_settings.json"
    settings.write_text('{"intensity": "standard"}\n', encoding="utf-8")
    code = (
        "import json, sys; from pathlib import Path; "
        "sys.path.insert(0, str(Path.cwd() / 'src')); import paths; "
        "paths.LATCH_SETTINGS_FILE = Path(sys.argv[1]); "
        "print(json.dumps(paths.latch_intensity_state()))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(settings)],
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(proc.stdout) == ["full", "env", None]


def test_missing_settings_preserve_legacy_full(tmp_path: Path) -> None:
    value, source, warning = paths.latch_intensity_state(
        env={}, settings_file=tmp_path / "missing.json"
    )
    assert (value, source, warning) == ("full", "legacy_default", None)


def test_file_setting_and_env_override_are_uncached(tmp_path: Path) -> None:
    settings = tmp_path / "latch_settings.json"
    paths.write_latch_intensity("standard", settings)
    assert paths.latch_intensity(env={}, settings_file=settings) == "standard"

    paths.write_latch_intensity("quiet", settings)
    assert paths.latch_intensity(env={}, settings_file=settings) == "quiet"
    assert paths.latch_intensity(
        env={"LATCH_INTENSITY": "FULL"}, settings_file=settings
    ) == "full"


def test_invalid_explicit_configuration_falls_back_with_warning(tmp_path: Path) -> None:
    settings = tmp_path / "latch_settings.json"
    settings.write_text('{"intensity":"maximum"}\n', encoding="utf-8")
    value, source, warning = paths.latch_intensity_state(
        env={}, settings_file=settings
    )
    assert (value, source) == ("quiet", "fallback")
    assert warning and "invalid intensity" in warning

    value, source, warning = paths.latch_intensity_state(
        env={"LATCH_INTENSITY": "maximum"}, settings_file=settings
    )
    assert (value, source) == ("quiet", "fallback")
    assert warning and "invalid LATCH_INTENSITY" in warning

    paths.write_latch_intensity("standard", settings)
    value, source, warning = paths.latch_intensity_state(
        env={"LATCH_INTENSITY": "maximum"}, settings_file=settings
    )
    assert (value, source) == ("standard", "settings")
    assert warning and "using saved standard" in warning


def test_settings_without_intensity_key_fail_safe_with_specific_warning(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "latch_settings.json"
    settings.write_text('{"future_key": 7}\n', encoding="utf-8")

    value, source, warning = paths.latch_intensity_state(
        env={}, settings_file=settings
    )

    assert (value, source) == ("quiet", "fallback")
    assert warning and f"missing intensity key in {settings}" in warning


def test_non_file_settings_path_fails_safe_with_warning(tmp_path: Path) -> None:
    settings = tmp_path / "latch_settings.json"
    settings.mkdir()
    value, source, warning = paths.latch_intensity_state(
        env={}, settings_file=settings
    )
    assert (value, source) == ("quiet", "fallback")
    assert warning and "not a regular file" in warning

    broken_link = tmp_path / "broken-settings.json"
    broken_link.symlink_to(tmp_path / "missing-target.json")
    value, source, warning = paths.latch_intensity_state(
        env={}, settings_file=broken_link
    )
    assert (value, source) == ("quiet", "fallback")
    assert warning and "not a regular file" in warning


def test_write_preserves_unrelated_settings(tmp_path: Path) -> None:
    settings = tmp_path / "latch_settings.json"
    settings.write_text(json.dumps({"future_key": 7}) + "\n", encoding="utf-8")
    paths.write_latch_intensity("standard", settings)
    assert json.loads(settings.read_text(encoding="utf-8")) == {
        "future_key": 7,
        "intensity": "standard",
    }


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
    settings = tmp_path / "latch_settings.json"
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


def test_kb_evidence_detection_is_read_only(tmp_path: Path, monkeypatch) -> None:
    db_file = tmp_path / "kb.db"
    monkeypatch.setattr(paths, "KB_ROOT", tmp_path)
    monkeypatch.setattr(paths, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(paths, "db_path", lambda _cwd=None: db_file)
    assert paths.kb_has_evidence(tmp_path) is False
    assert not db_file.exists()

    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert paths.kb_has_evidence(tmp_path) is False

    conn = sqlite3.connect(db_file)
    conn.execute("INSERT INTO nodes DEFAULT VALUES")
    conn.commit()
    conn.close()
    assert paths.kb_has_evidence(tmp_path) is True


def test_gate_assembly_does_not_consult_intensity(tmp_path: Path, monkeypatch) -> None:
    conn = db.connect(str(tmp_path / "gate-kb"))
    try:
        monkeypatch.setattr(
            gate.paths,
            "latch_intensity",
            lambda: (_ for _ in ()).throw(
                AssertionError("gate assembly must be tier-invariant")
            ),
        )
        assembly = gate.assemble_gate(conn, "", focus_seed=False)
        assert assembly["seeds"] == []
        assert assembly["chains"] == []
    finally:
        conn.close()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_status_uses_the_runtime_resolver_for_whitespace_env() -> None:
    env = os.environ.copy()
    env["LATCH_HOME"] = str(ROOT)
    env["LATCH_PYTHON"] = sys.executable
    env["LATCH_INTENSITY"] = "  QuIeT  "
    proc = subprocess.run(
        ["bash", str(ROOT / "bin" / "latch_status.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "Latch intensity: Quiet (env)" in proc.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
def test_status_leads_with_unlatched_state_before_intensity() -> None:
    env = os.environ.copy()
    env["LATCH_HOME"] = str(ROOT)
    env["LATCH_PYTHON"] = sys.executable
    env["LATCH_UNLATCHED"] = "1"
    proc = subprocess.run(
        ["bash", str(ROOT / "bin" / "latch_status.sh")],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert proc.stdout.index("[UNLATCHED]") < proc.stdout.index("Latch intensity:")


def test_intensity_cli_set_json_is_machine_readable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings = tmp_path / "latch_settings.json"
    monkeypatch.setattr(paths, "LATCH_SETTINGS_FILE", settings)
    monkeypatch.delenv("LATCH_INTENSITY", raising=False)
    assert intensity_cli.main(["quiet", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["intensity"] == "quiet"
    assert payload["source"] == "settings"


def test_doctor_retier_hint_is_install_root_qualified() -> None:
    hint = paths.latch_intensity_change_hint()
    assert "bash " in hint
    assert str(paths.KB_ROOT / "bin" / "latch_intensity.sh") in hint
    assert str(paths.KB_ROOT / "bin" / "latch_intensity.ps1") in hint
