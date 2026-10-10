from __future__ import annotations

import json
import os
import threading
import time
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.hosts import agents_md_sync  # noqa: E402
from latch.hosts import codex_hooks  # noqa: E402
from latch.hosts import codex_wiring  # noqa: E402
from latch.install import install_codex  # noqa: E402
from latch.install import install_engine  # noqa: E402
from latch.install import versioning  # noqa: E402


def _older(text: str) -> str:
    return text.replace(
        f"latch-wiring-version: {versioning.WIRING_VERSION}",
        f"latch-wiring-version: {versioning.WIRING_VERSION - 1}",
    )


def _install_older_bundle(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    agents = project / "AGENTS.md"
    agents.write_text("user-before\n", encoding="utf-8")
    agents_md_sync.sync(agents)
    agents.write_text(_older(agents.read_text(encoding="utf-8")), encoding="utf-8")

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    existing = """theme = "dark"

[mcp_servers.other]
command = "node"
args = []
"""
    rendered, _ = install_codex.merge_config(
        existing, sys.executable, str(ROOT / "src" / "mcp_server.py")
    )
    rendered = rendered.replace("required = true\n", "").replace(
        f'LATCH_WIRING_VERSION = "{versioning.WIRING_VERSION}"\n', ""
    )
    config.write_text(rendered, encoding="utf-8")

    hooks = codex_home / "hooks.json"
    existing_hooks = json.dumps({
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": "/user/start"}],
            }],
        },
    }, indent=2) + "\n"
    rendered_hooks, _ = codex_hooks.merge_hooks(
        existing_hooks,
        sys.executable,
        str(ROOT / "src" / "hooks" / "codex_session_start.py"),
    )
    rendered_hooks = rendered_hooks.replace(
        f" --latch-wiring-version {versioning.WIRING_VERSION}", ""
    )
    hooks.write_text(rendered_hooks, encoding="utf-8")

    skills = tmp_path / "skills"
    install_codex.sync_codex_skills(skills)
    for target in skills.glob("*/SKILL.md"):
        target.write_text(_older(target.read_text(encoding="utf-8")), encoding="utf-8")
    return project, config, hooks, skills


def _repair(project: Path, config: Path, hooks: Path, skills: Path):
    return codex_wiring.repair_project(
        project, config_path=config, hooks_path=hooks, skills_dir=skills
    )


def test_codex_bundle_repairs_once_and_preserves_unrelated_content(tmp_path: Path):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    before_config = config.read_bytes()
    before_hooks = hooks.read_bytes()
    before_agents = (project / "AGENTS.md").read_bytes()
    before_skill = (skills / install_codex.CODEX_SKILL_NAMES[0] / "SKILL.md").read_bytes()

    result = _repair(project, config, hooks, skills)
    assert result.action == "synced"
    assert result.restart_required is True
    assert "Restart or open a new task" in (result.notice or "")

    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["theme"] == "dark"
    assert parsed["mcp_servers"]["other"] == {"command": "node", "args": []}
    latch = parsed["mcp_servers"]["latch"]
    assert latch["required"] is True
    assert latch["env"]["LATCH_WIRING_VERSION"] == str(versioning.WIRING_VERSION)
    assert config.with_name("config.toml.latchbak").read_bytes() == before_config

    hooks_obj = json.loads(hooks.read_text(encoding="utf-8"))
    assert "/user/start" in json.dumps(hooks_obj)
    assert f"--latch-wiring-version {versioning.WIRING_VERSION}" in json.dumps(hooks_obj)
    assert hooks.with_name("hooks.json.latchbak").read_bytes() == before_hooks

    agents = project / "AGENTS.md"
    assert agents.read_text(encoding="utf-8").startswith("user-before\n")
    assert f"latch-wiring-version: {versioning.WIRING_VERSION}" in agents.read_text(
        encoding="utf-8"
    )
    assert agents.with_name("AGENTS.md.latchbak").read_bytes() == before_agents

    first_skill = skills / install_codex.CODEX_SKILL_NAMES[0] / "SKILL.md"
    assert f"latch-wiring-version: {versioning.WIRING_VERSION}" in first_skill.read_text(
        encoding="utf-8"
    )
    assert first_skill.with_name("SKILL.md.latchbak").read_bytes() == before_skill

    snapshot = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    again = _repair(project, config, hooks, skills)
    assert again.action == "unchanged"
    assert again.notice is None
    assert snapshot == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }


def test_codex_bundle_does_not_touch_unmanaged_or_newer_project(tmp_path: Path):
    config = tmp_path / "config.toml"
    hooks = tmp_path / "hooks.json"
    config.write_text("not inspected", encoding="utf-8")
    hooks.write_text("not inspected", encoding="utf-8")
    skills = tmp_path / "skills"
    before = {config: config.read_bytes(), hooks: hooks.read_bytes()}

    unmanaged = tmp_path / "unmanaged"
    unmanaged.mkdir()
    result = _repair(unmanaged, config, hooks, skills)
    assert result.action == "unmanaged"
    assert {path: path.read_bytes() for path in before} == before

    newer = tmp_path / "newer"
    newer.mkdir()
    agents_md_sync.sync(newer / "AGENTS.md")
    text = (newer / "AGENTS.md").read_text(encoding="utf-8").replace(
        f"latch-wiring-version: {versioning.WIRING_VERSION}",
        "latch-wiring-version: 999",
    )
    (newer / "AGENTS.md").write_text(text, encoding="utf-8")
    result = _repair(newer, config, hooks, skills)
    assert result.action == "newer"
    assert {path: path.read_bytes() for path in before} == before


def test_codex_bundle_malformed_config_fails_open_without_advancing_marker(
    tmp_path: Path,
):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    config.write_text("[mcp_servers.latch\n", encoding="utf-8")
    before_agents = (project / "AGENTS.md").read_bytes()
    before_config = config.read_bytes()

    result = _repair(project, config, hooks, skills)
    assert result.action == "error"
    assert "task will continue" in (result.notice or "")
    assert (project / "AGENTS.md").read_bytes() == before_agents
    assert config.read_bytes() == before_config
    assert not config.with_name("config.toml.latchbak").exists()


def test_codex_bundle_partial_failure_keeps_retry_marker_old(
    tmp_path: Path, monkeypatch,
):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    agents = project / "AGENTS.md"
    before_agents = agents.read_bytes()
    original_sync = install_codex.sync_codex_skills
    monkeypatch.setattr(
        install_codex,
        "sync_codex_skills",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("skill write failed")),
    )

    failed = _repair(project, config, hooks, skills)
    assert failed.action == "error"
    assert agents.read_bytes() == before_agents
    assert "required" not in tomllib.loads(config.read_text(encoding="utf-8"))[
        "mcp_servers"
    ]["latch"]

    monkeypatch.setattr(install_codex, "sync_codex_skills", original_sync)
    retried = _repair(project, config, hooks, skills)
    assert retried.action == "synced"
    assert agents_md_sync.wiring_state(agents) == "current"


def test_codex_bundle_refuses_newer_global_managed_surface(tmp_path: Path):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            install_codex.END_MARK,
            'LATCH_WIRING_VERSION = "999"\n' + install_codex.END_MARK,
        ),
        encoding="utf-8",
    )
    before = config.read_bytes()
    result = _repair(project, config, hooks, skills)
    assert result.action == "error"
    assert "newer wiring 999" in (result.notice or "")
    assert config.read_bytes() == before
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "older"


@pytest.mark.parametrize(
    "case",
    (
        "markerless",
        "half-marked",
        "duplicate-marked",
        "table-outside-region",
        "wrong-target",
    ),
)
def test_codex_bundle_refuses_unrecognized_config_ownership(
    tmp_path: Path, case: str,
):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    text = config.read_text(encoding="utf-8")
    if case == "markerless":
        text = text.replace(install_codex.BEGIN_MARK + "\n", "").replace(
            install_codex.END_MARK + "\n", ""
        )
    elif case == "half-marked":
        text = text.replace(install_codex.END_MARK + "\n", "")
    elif case == "duplicate-marked":
        text = text.replace(
            install_codex.BEGIN_MARK,
            install_codex.BEGIN_MARK + "\n" + install_codex.BEGIN_MARK,
            1,
        )
    elif case == "table-outside-region":
        text = text.replace(install_codex.BEGIN_MARK + "\n", "").replace(
            install_codex.END_MARK + "\n", ""
        )
        text += f"\n{install_codex.BEGIN_MARK}\n{install_codex.END_MARK}\n"
    else:
        installed = tomllib.loads(text)["mcp_servers"]["latch"]["args"][0]
        text = text.replace(
            f"args = [{install_codex._toml_string(installed)}]",
            'args = ["/user/mcp_server.py"]',
        )
    config.write_text(text, encoding="utf-8")
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }

    result = _repair(project, config, hooks, skills)
    assert result.action == "error"
    assert "task will continue" in (result.notice or "")
    assert before == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "older"


def test_codex_bundle_skill_collision_preflights_before_any_write(tmp_path: Path):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    collision = (
        skills
        / install_codex.CODEX_SKILL_NAMES[0]
        / "agents"
        / "openai.yaml"
    )
    collision.unlink()
    collision.mkdir()
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }

    result = _repair(project, config, hooks, skills)
    assert result.action == "error"
    assert "user-owned Codex skill" in (result.notice or "")
    assert before == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "older"


def test_codex_bundle_respects_skipped_optional_surfaces(tmp_path: Path):
    project, config, _hooks, _skills = _install_older_bundle(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "[features]\nhooks = true\n\n", ""
        ),
        encoding="utf-8",
    )
    skipped_hooks = tmp_path / "skipped-hooks.json"
    skipped_skills = tmp_path / "skipped-skills"
    skipped_skills.write_text("unrelated personal skills root\n", encoding="utf-8")
    before_skills = skipped_skills.read_bytes()
    result = codex_wiring.repair_project(
        project,
        config_path=config,
        hooks_path=skipped_hooks,
        skills_dir=skipped_skills,
        repair_global=True,
    )
    assert result.action == "synced"
    assert not skipped_hooks.exists()
    assert skipped_skills.read_bytes() == before_skills
    assert tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["latch"][
        "required"
    ] is True
    assert "features" not in tomllib.loads(config.read_text(encoding="utf-8"))
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "older"


@pytest.mark.parametrize(
    "feature_line",
    (
        "features = { js_repl = true }",
        "features.js_repl = true",
    ),
)
def test_mcp_fallback_preserves_skipped_hook_feature_forms(
    tmp_path: Path, feature_line: str,
):
    project, config, _hooks, skills = _install_older_bundle(tmp_path)
    legacy = config.read_text(encoding="utf-8").replace(
        "[features]\nhooks = true\n\n", ""
    )
    config.write_text(f"{feature_line}\n\n{legacy}", encoding="utf-8")
    skipped_hooks = tmp_path / "skipped-hooks.json"

    result = codex_wiring.repair_project(
        project,
        config_path=config,
        hooks_path=skipped_hooks,
        skills_dir=skills,
        repair_global=True,
    )

    assert result.action == "synced"
    rendered = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(rendered)
    assert feature_line in rendered
    assert parsed["features"] == {"js_repl": True}
    assert parsed["mcp_servers"]["latch"]["required"] is True
    assert not skipped_hooks.exists()
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "older"


def test_codex_bundle_preserves_unowned_optional_surfaces(tmp_path: Path):
    project, config, _hooks, _skills = _install_older_bundle(tmp_path)
    hooks = tmp_path / "user-hooks.json"
    hooks.write_text(json.dumps({"hooks": {"SessionStart": ["user"]}}), encoding="utf-8")
    skills = tmp_path / "user-skills"
    user_skill = skills / install_codex.CODEX_SKILL_NAMES[0] / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("user-owned skill\n", encoding="utf-8")
    before_hooks = hooks.read_bytes()
    before_skill = user_skill.read_bytes()

    result = codex_wiring.repair_project(
        project,
        config_path=config,
        hooks_path=hooks,
        skills_dir=skills,
        repair_global=True,
    )
    assert result.action == "synced"
    assert hooks.read_bytes() == before_hooks
    assert user_skill.read_bytes() == before_skill


def test_codex_bundle_refuses_newer_owned_hook_without_writes(tmp_path: Path):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    hooks.write_text(
        hooks.read_text(encoding="utf-8").replace(
            "codex_session_start.py",
            "codex_session_start.py --latch-wiring-version 999",
        ),
        encoding="utf-8",
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    result = _repair(project, config, hooks, skills)
    assert result.action == "error"
    assert "newer wiring 999" in (result.notice or "")
    assert before == {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        (r"C:\Python311\python.exe", r"C:\Python311\python.exe"),
        (r"C:\Python311\pythonw.exe", r"C:\Python311\python.exe"),
        (
            r"C:\Program Files\Latch\venv\Scripts\pythonw.exe",
            r"C:\Program Files\Latch\venv\Scripts\python.exe",
        ),
        ("C:/Program Files/Latch/venv/Scripts/pythonw.exe",
         "C:/Program Files/Latch/venv/Scripts/python.exe"),
    ),
)
def test_console_python_preserves_windows_paths(command: str, expected: str):
    assert codex_wiring._console_python(command) == expected


def test_codex_bundle_uses_managed_command_not_ambient_python(
    tmp_path: Path, monkeypatch,
):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    current = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(current)
    old_command = parsed["mcp_servers"]["latch"]["command"]
    windows_command = r"C:\Program Files\Latch\venv\Scripts\pythonw.exe"
    current = current.replace(
        f"command = {install_codex._toml_string(old_command)}",
        f"command = {install_codex._toml_string(windows_command)}",
    )
    config.write_text(current, encoding="utf-8")
    monkeypatch.setenv("LATCH_PYTHON", "/ambient/must-not-win")
    seen: list[tuple[str, str]] = []

    def fake_launch(python_path: str, server_py: str):
        seen.append((python_path, server_py))
        return windows_command, r"C:\Latch\src\mcp_launcher_win.py"

    monkeypatch.setattr(install_engine, "mcp_launch_command", fake_launch)
    result = _repair(project, config, hooks, skills)
    assert result.action == "synced"
    assert seen == [
        (
            r"C:\Program Files\Latch\venv\Scripts\python.exe",
                str(ROOT / "src" / "latch" / "mcp" / "mcp_server.py"),
        )
    ]


def test_mcp_fallback_repairs_global_bundle_even_if_project_marker_is_current(
    tmp_path: Path,
):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    agents = project / "AGENTS.md"
    agents.write_text(
        agents.read_text(encoding="utf-8").replace(
            f"latch-wiring-version: {versioning.WIRING_VERSION - 1}",
            f"latch-wiring-version: {versioning.WIRING_VERSION}",
        ),
        encoding="utf-8",
    )
    before_agents = agents.read_bytes()

    result = codex_wiring.repair_project(
        project,
        config_path=config,
        hooks_path=hooks,
        skills_dir=skills,
        repair_global=True,
    )
    assert result.action == "synced"
    assert tomllib.loads(config.read_text(encoding="utf-8"))["mcp_servers"]["latch"][
        "required"
    ] is True
    assert agents.read_bytes() == before_agents


def test_mcp_fallback_upgrades_exact_v1_codex_config_shape(
    tmp_path: Path, monkeypatch,
):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    legacy = config.read_text(encoding="utf-8")
    legacy = legacy.replace("[features]\nhooks = true\n\n", "")
    legacy = legacy.replace('LATCH_ADAPTER = "codex"\n', "")
    assert "[features]" not in legacy
    assert "LATCH_ADAPTER" not in legacy
    assert "required =" not in legacy
    assert "LATCH_WIRING_VERSION" not in legacy
    config.write_text(legacy, encoding="utf-8")

    monkeypatch.setattr(install_codex, "CONFIG_PATH", config)
    monkeypatch.setattr(install_codex, "HOOKS_PATH", hooks)
    monkeypatch.setattr(install_codex, "DEFAULT_SKILLS_DIR", skills)
    monkeypatch.chdir(project)
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    monkeypatch.delenv("LATCH_WIRING_VERSION", raising=False)
    monkeypatch.setenv("LATCH_MODEL_BACKEND", "codex")
    monkeypatch.setenv("LATCH_GATE_BACKEND", "codex")
    monkeypatch.setenv("LATCH_TOOL_SURFACE", "latch")

    result = codex_wiring.repair_from_mcp_startup()
    assert result.action == "synced"
    parsed = tomllib.loads(config.read_text(encoding="utf-8"))
    assert parsed["features"]["hooks"] is True
    server = parsed["mcp_servers"]["latch"]
    assert server["required"] is True
    assert server["env"]["LATCH_ADAPTER"] == "codex"
    assert server["env"]["LATCH_WIRING_VERSION"] == str(versioning.WIRING_VERSION)
    assert parsed["theme"] == "dark"
    assert parsed["mcp_servers"]["other"] == {"command": "node", "args": []}
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "older"

    session_result = _repair(project, config, hooks, skills)
    assert session_result.action == "synced"
    assert "Restart or open a new task" in (session_result.notice or "")
    assert agents_md_sync.wiring_state(project / "AGENTS.md") == "current"


def test_codex_bundle_repairs_are_serialized(tmp_path: Path, monkeypatch):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    original_sync = install_codex.sync_codex_skills
    state_lock = threading.Lock()
    active = 0
    max_active = 0

    def slow_sync(*args, **kwargs):
        nonlocal active, max_active
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.08)
        try:
            return original_sync(*args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(install_codex, "sync_codex_skills", slow_sync)
    barrier = threading.Barrier(3)
    results: list[codex_wiring.RepairResult] = []

    def run_repair():
        barrier.wait()
        results.append(codex_wiring.repair_project(
            project,
            config_path=config,
            hooks_path=hooks,
            skills_dir=skills,
            repair_global=True,
        ))

    threads = [threading.Thread(target=run_repair) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result.action for result in results) == ["synced", "unchanged"]
    assert max_active == 1
    assert not config.with_name("latch-wiring.lock").exists()


def test_codex_bundle_reclaims_stale_malformed_repair_lock(tmp_path: Path):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    lock = config.with_name("latch-wiring.lock")
    lock.write_bytes(b"")
    stale = time.time() - codex_wiring._LOCK_TIMEOUT_S - 1
    os.utime(lock, (stale, stale))
    result = codex_wiring.repair_project(
        project,
        config_path=config,
        hooks_path=hooks,
        skills_dir=skills,
        repair_global=True,
    )
    assert result.action == "synced"
    assert not lock.exists()


def test_codex_bundle_lock_write_failure_cleans_up(tmp_path: Path, monkeypatch):
    project, config, hooks, skills = _install_older_bundle(tmp_path)
    lock = config.with_name("latch-wiring.lock")
    monkeypatch.setattr(
        codex_wiring,
        "_write_lock_pid",
        lambda _fd: (_ for _ in ()).throw(OSError("simulated lock write failure")),
    )
    result = codex_wiring.repair_project(
        project,
        config_path=config,
        hooks_path=hooks,
        skills_dir=skills,
        repair_global=True,
    )
    assert result.action == "error"
    assert not lock.exists()


def test_mcp_startup_repair_targets_only_codex(monkeypatch, tmp_path: Path):
    calls: list[Path] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        codex_wiring,
        "repair_project",
        lambda project, **_kwargs: calls.append(Path(project))
        or codex_wiring.RepairResult("synced"),
    )
    monkeypatch.setenv("LATCH_MODEL_BACKEND", "codex")
    monkeypatch.setenv("LATCH_GATE_BACKEND", "codex")
    monkeypatch.setenv("LATCH_TOOL_SURFACE", "latch")
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)
    assert codex_wiring.repair_from_mcp_startup().action == "synced"
    assert calls == [tmp_path]

    calls.clear()
    monkeypatch.setenv("LATCH_WIRING_VERSION", str(versioning.WIRING_VERSION))
    assert codex_wiring.repair_from_mcp_startup().action == "unchanged"
    assert calls == []
    agents_md_sync.sync(tmp_path / "AGENTS.md")
    (tmp_path / "AGENTS.md").write_text(
        _older((tmp_path / "AGENTS.md").read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    assert codex_wiring.repair_from_mcp_startup().action == "unchanged"
    assert calls == []
    monkeypatch.delenv("LATCH_WIRING_VERSION", raising=False)

    calls.clear()
    for adapter in ("cursor", "claude-desktop", "vscode-copilot"):
        monkeypatch.setenv("LATCH_ADAPTER", adapter)
        assert codex_wiring.repair_from_mcp_startup().action == "not-codex"
        assert calls == []

    monkeypatch.setenv("LATCH_ADAPTER", "codex")
    monkeypatch.delenv("LATCH_MODEL_BACKEND", raising=False)
    monkeypatch.delenv("LATCH_GATE_BACKEND", raising=False)
    monkeypatch.delenv("LATCH_TOOL_SURFACE", raising=False)
    assert codex_wiring.repair_from_mcp_startup().action == "synced"
    assert calls == [tmp_path]
