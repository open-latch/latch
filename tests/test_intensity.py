from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import paths  # noqa: E402
import db  # noqa: E402
import gate  # noqa: E402
import intensity_cli  # noqa: E402


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
