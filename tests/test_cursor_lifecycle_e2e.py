"""Cross-platform subprocess proof for the Cursor project lifecycle."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "src" / "install_cursor.py"
DOCTOR = ROOT / "src" / "cursor_doctor.py"
UNINSTALL = ROOT / "src" / "uninstall_engine.py"


def _run(args: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    return result


def test_cursor_project_install_doctor_uninstall_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "project"
    home = tmp_path / "home"
    install_home = tmp_path / "latch-home"
    cursor = project / ".cursor"
    commands = cursor / "commands"
    skills = cursor / "skills"
    commands.mkdir(parents=True)
    (skills / "mine").mkdir(parents=True)
    home.mkdir()
    install_home.mkdir()
    for directory in (
        "src",
        "bin",
        "commands",
        "cursor_commands",
        "cursor_skills",
        ".cursor-plugin",
    ):
        shutil.copytree(ROOT / directory, install_home / directory)
    for filename in (
        "settings_snippet.json",
        "claude_md_snippet.md",
        "cursor_rule_snippet.mdc",
    ):
        shutil.copy2(ROOT / filename, install_home / filename)

    # uninstall_engine's legacy all-host check also verifies that no Claude MCP
    # registration remains. Supply a hermetic CLI stub so this Cursor lifecycle
    # proof does not depend on a runner-global Claude installation.
    tool_bin = tmp_path / "bin"
    tool_bin.mkdir()
    if os.name == "nt":
        (tool_bin / "claude.cmd").write_text("@exit /b 1\r\n", encoding="utf-8")
    else:
        claude = tool_bin / "claude"
        claude.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        claude.chmod(0o755)

    mcp = cursor / "mcp.json"
    hooks = cursor / "hooks.json"
    rule = cursor / "rules" / "latch.mdc"
    agents = project / "AGENTS.md"
    user_command = commands / "mine.md"
    user_skill = skills / "mine" / "SKILL.md"
    mcp.write_text(json.dumps({
        "projectSetting": True,
        "mcpServers": {"other": {"command": "other-server"}},
    }), encoding="utf-8")
    hooks.write_text(json.dumps({
        "version": 1,
        "hooks": {"stop": [{"command": "user-stop"}]},
    }), encoding="utf-8")
    agents.write_text("# User instructions\n\nKeep this text.\n", encoding="utf-8")
    user_command.write_bytes(b"user command\n")
    user_skill.write_bytes(b"---\nname: mine\ndescription: user skill\n---\n")

    env = os.environ.copy()
    test_root = Path(env["LATCH_TEST_ROOT"])
    env.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CLAUDE_COMMANDS_DIR": str(home / ".claude" / "commands"),
        "LATCH_HOME": str(install_home),
        "CLAUDE_KB_HOME": str(install_home),
        "LATCH_SCOPE_STATE_ROOT": str(
            test_root / "cursor-lifecycle-control" / tmp_path.name
        ),
        "LATCH_PYTHON": sys.executable,
        "CLAUDE_KB_PYTHON": sys.executable,
        "PATH": str(tool_bin) + os.pathsep + os.environ.get("PATH", ""),
    })
    install_args = [
        sys.executable, str(INSTALL),
        "--python", sys.executable,
        "--mcp-json", str(mcp),
        "--agents-md", str(agents),
        "--rules-mdc", str(rule),
        "--commands-dir", str(commands),
        "--hooks-json", str(hooks),
        "--with-hooks", "--yes",
    ]

    _run(install_args, cwd=project, env=env)
    _run(install_args, cwd=project, env=env)
    _run([*install_args, "--check"], cwd=project, env=env)

    doctor_args = [
        sys.executable, str(DOCTOR),
        "--python", sys.executable,
        "--mcp-json", str(mcp),
        "--agents-md", str(agents),
        "--rules-mdc", str(rule),
        "--commands-dir", str(commands),
        "--hooks-json", str(hooks),
        "--with-hooks", "--skip-cli",
    ]
    doctor_help = _run(
        [sys.executable, str(DOCTOR), "--help"], cwd=project, env=env,
    ).stdout
    if "--skills-dir" in doctor_help:
        doctor_args.extend(["--skills-dir", str(skills)])
    for flag in ("--skip-backend", "--skip-compact"):
        if flag in doctor_help:
            doctor_args.append(flag)
    _run(doctor_args, cwd=project, env=env)

    installed = json.loads(mcp.read_text(encoding="utf-8"))
    assert installed["projectSetting"] is True
    assert set(installed["mcpServers"]) == {"other", "latch"}
    assert user_command.read_bytes() == b"user command\n"
    assert user_skill.read_bytes().startswith(b"---\nname: mine")
    assert "Keep this text." in agents.read_text(encoding="utf-8")

    uninstall_help = _run(
        [sys.executable, str(UNINSTALL), "--help"], cwd=project, env=env,
    ).stdout
    uninstall_args = [
        sys.executable, str(UNINSTALL),
        "--cursor-project", str(project),
    ]
    if "--cursor-only" in uninstall_help:
        uninstall_args.append("--cursor-only")
    _run([*uninstall_args, "--yes"], cwd=project, env=env)
    _run([*uninstall_args, "--check"], cwd=project, env=env)

    remaining_mcp = json.loads(mcp.read_text(encoding="utf-8"))
    assert remaining_mcp == {
        "projectSetting": True,
        "mcpServers": {"other": {"command": "other-server"}},
    }
    remaining_hooks = json.loads(hooks.read_text(encoding="utf-8"))
    assert remaining_hooks["hooks"] == {"stop": [{"command": "user-stop"}]}
    assert user_command.read_bytes() == b"user command\n"
    assert user_skill.read_bytes().startswith(b"---\nname: mine")
    assert "Keep this text." in agents.read_text(encoding="utf-8")
