from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latch.hosts import agents_md_sync as ams  # noqa: E402
from latch.hosts import claude_md_sync as cms  # noqa: E402
from latch.install import unlatch  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _run_unlatch(home: Path, cwd: Path, *args: str) -> str:
    env = os.environ.copy()
    env["LATCH_HOME"] = str(home)
    env["LATCH_PYTHON"] = sys.executable
    env.pop("CLAUDE_KB_HOME", None)
    env.pop("LATCH_UNLATCHED", None)
    env.pop("LATCH_DISABLE", None)
    env.pop("CLAUDE_KB_DISABLE", None)
    proc = subprocess.run(
        ["bash", str(ROOT / "bin" / "unlatch.sh"), *args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _run_confirm(action: str, home: Path, cwd: Path) -> str:
    return _run_unlatch(home, cwd, "--confirm", action)


def _run_enable(home: Path, cwd: Path) -> str:
    env = os.environ.copy()
    env["LATCH_HOME"] = str(home)
    env["LATCH_PYTHON"] = sys.executable
    env.pop("CLAUDE_KB_HOME", None)
    env.pop("LATCH_UNLATCHED", None)
    env.pop("LATCH_DISABLE", None)
    env.pop("CLAUDE_KB_DISABLE", None)
    proc = subprocess.run(
        ["bash", str(ROOT / "bin" / "latch_enable.sh")],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _run_hook_unlatched(script: str, home: Path, cwd: Path) -> str:
    env = os.environ.copy()
    env["LATCH_HOME"] = str(home)
    env.pop("CLAUDE_KB_HOME", None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "src" / "latch" / "hooks" / script)],
        cwd=cwd,
        env=env,
        text=True,
        input="{}",
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _run_instruction_mask(home: Path, cwd: Path, action: str) -> str:
    env = os.environ.copy()
    env["LATCH_HOME"] = str(home)
    env["LATCH_PYTHON"] = sys.executable
    env.pop("CLAUDE_KB_HOME", None)
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "src" / "latch" / "install" / "unlatch.py"),
            action,
            "--project",
            str(cwd),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def _setup(tmp: Path) -> tuple[Path, Path, Path, Path, Path]:
    home = tmp / "home"
    project = tmp / "project"
    home.mkdir()
    project.mkdir()
    (home / "src").symlink_to(ROOT / "src", target_is_directory=True)
    projects = home / "projects"
    projects.mkdir()
    kb_marker = projects / "keep.db"
    kb_marker.write_text("kb data stays put\n", encoding="utf-8")

    claude = project / "CLAUDE.md"
    agents = project / "AGENTS.md"
    claude.write_text("# Project rules\n\nKeep this Claude rule.\n", encoding="utf-8")
    agents.write_text("# Agent rules\n\nKeep this agent rule.\n", encoding="utf-8")
    cms.sync(claude, kb_home=str(home))
    ams.sync(agents, kb_home=str(home))
    _assert(cms.BEGIN_MARK in claude.read_text(encoding="utf-8"),
            "precondition: CLAUDE.md has latch managed region")
    _assert(ams.BEGIN_MARK in agents.read_text(encoding="utf-8"),
            "precondition: AGENTS.md has latch managed region")
    return home, project, kb_marker, claude, agents


def _assert_unlatched(home: Path, claude: Path, agents: Path):
    _assert((home / "UNLATCHED").is_file(), "unlatch should create UNLATCHED")
    _assert((home / "DISABLE").is_file(), "unlatch should create DISABLE")
    _assert((home / "UNLATCH_STATE.json").is_file(),
            "off should remember masked instruction files")
    claude_off = claude.read_text(encoding="utf-8")
    agents_off = agents.read_text(encoding="utf-8")
    _assert(unlatch.BEGIN_MARK in claude_off and cms.BEGIN_MARK not in claude_off,
            "CLAUDE.md should carry only unlatched override while off")
    _assert(unlatch.BEGIN_MARK in agents_off and ams.BEGIN_MARK not in agents_off,
            "AGENTS.md should carry only unlatched override while off")
    _assert("If LATCH_UNLATCHED is set, unset it too" in claude_off,
            "CLAUDE.md unlatched override should mention env-forced unlatch")
    _assert("If LATCH_UNLATCHED is set, unset it too" in agents_off,
            "AGENTS.md unlatched override should mention env-forced unlatch")


def _assert_latch_restored(home: Path, claude: Path, agents: Path):
    _assert(not (home / "UNLATCHED").exists(), "resume should remove UNLATCHED")
    _assert(not (home / "DISABLE").exists(), "resume should remove DISABLE")
    _assert(not (home / "UNLATCH_STATE.json").exists(),
            "resume should remove unlatch instruction state")
    claude_on = claude.read_text(encoding="utf-8")
    agents_on = agents.read_text(encoding="utf-8")
    _assert(unlatch.BEGIN_MARK not in claude_on and cms.BEGIN_MARK in claude_on,
            "CLAUDE.md unlatched override should be removed and latch region restored")
    _assert(unlatch.BEGIN_MARK not in agents_on and ams.BEGIN_MARK in agents_on,
            "AGENTS.md unlatched override should be removed and latch region restored")


def _records_by_path(home: Path) -> dict[str, dict]:
    data = json.loads((home / "UNLATCH_STATE.json").read_text(encoding="utf-8"))
    return {
        str(Path(record["path"]).resolve()): record
        for record in data.get("instruction_files", [])
    }


def _assert_record(home: Path, path: Path, kind: str):
    records = _records_by_path(home)
    key = str(path.resolve())
    record = records.get(key)
    _assert(record is not None, f"state should record {key}; got {records}")
    _assert(record.get("kind") == kind, f"{key} should be recorded as {kind}")
    _assert(record.get("had_managed_region") is True,
            f"{key} should keep restore state")


def test_unlatch_off_status_on_cycle():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_test_"))
    try:
        home, project, kb_marker, claude, agents = _setup(tmp)
        inspect = _run_unlatch(home, project)
        _assert("Latch is currently LATCHED" in inspect, inspect)
        _assert(not (home / "UNLATCHED").exists(),
                "inspection must not create UNLATCHED")
        _assert(not (home / "DISABLE").exists(),
                "inspection must not create DISABLE")

        off = _run_confirm("unlatch", home, project)
        _assert("Latch is now UNLATCHED" in off, off)
        _assert("KB files stay local and unchanged" in off, off)
        _assert("masked latch managed region" in off, off)
        _assert_unlatched(home, claude, agents)
        _assert("If LATCH_UNLATCHED is set, unset it too"
                in (home / "UNLATCHED").read_text(encoding="utf-8"),
                "UNLATCHED receipt should mention env-forced unlatch")
        _assert("If LATCH_UNLATCHED is set, unset it too"
                in (home / "DISABLE").read_text(encoding="utf-8"),
                "DISABLE receipt should mention env-forced unlatch")

        status = _run_unlatch(home, project)
        _assert("[UNLATCHED]" in status, status)
        _assert("prompt KB injection" in status, status)
        _assert("/unlatch and status commands remain available" in status, status)
        _assert("unlatched override present" in status, status)

        # Unlatch on is the high-level "resume latch" path: it clears both the
        # full unlatch state and the advanced write-only sentinel if present.
        (home / "DISABLE_WRITE").write_text("write off\n", encoding="utf-8")
        on = _run_confirm("latch", home, project)
        _assert("Latch is now LATCHED" in on, on)
        _assert("restored latch managed region" in on, on)
        _assert_latch_restored(home, claude, agents)
        _assert(not (home / "DISABLE_WRITE").exists(), "on should remove DISABLE_WRITE")
        _assert(kb_marker.read_text(encoding="utf-8") == "kb data stays put\n",
                "unlatch on/off must not delete KB data")
        print("PASS unlatch_off_status_on_cycle")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unlatch_confirmation_contract_is_strict():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_confirm_strict_"))
    try:
        home, project, _kb_marker, _claude, _agents = _setup(tmp)
        env = os.environ.copy()
        env["LATCH_HOME"] = str(home)
        env["LATCH_PYTHON"] = sys.executable
        env.pop("CLAUDE_KB_HOME", None)
        env.pop("LATCH_UNLATCHED", None)

        extra = subprocess.run(
            ["bash", str(ROOT / "bin" / "unlatch.sh"), "--confirm", "unlatch", "extra"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
        )
        _assert(extra.returncode == 2, extra)
        _assert(not (home / "UNLATCHED").exists(),
                "extra confirmation args must not change state")

        upper = subprocess.run(
            ["bash", str(ROOT / "bin" / "unlatch.sh"), "--confirm", "UNLATCH"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
        )
        _assert(upper.returncode == 2, upper)
        _assert(not (home / "UNLATCHED").exists(),
                "uppercase confirmation must not change state")

        ps1 = (ROOT / "bin" / "unlatch.ps1").read_text(encoding="utf-8")
        _assert("switch -CaseSensitive ($Confirm)" in ps1,
                "PowerShell confirmation matching must be case-sensitive")
        print("PASS unlatch_confirmation_contract_is_strict")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nested_off_masks_ancestor_and_on_restores_exact_records():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_nested_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        claude.write_text(
            claude.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nClaude footer outside latch region stays.\n",
            encoding="utf-8",
        )
        agents.write_text(
            agents.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nAgents footer outside latch region stays.\n",
            encoding="utf-8",
        )
        nested = project / "app" / "subdir"
        other_nested = project / "other" / "nested"
        nested.mkdir(parents=True)
        other_nested.mkdir(parents=True)

        off = _run_confirm("unlatch", home, nested)
        _assert("masked latch managed region" in off, off)
        _assert_unlatched(home, claude, agents)
        _assert_record(home, claude, "claude")
        _assert_record(home, agents, "agents")
        claude_off = claude.read_text(encoding="utf-8")
        agents_off = agents.read_text(encoding="utf-8")
        _assert("Keep this Claude rule." in claude_off, "Claude body text survived off")
        _assert("Claude footer outside latch region stays." in claude_off,
                "Claude footer survived off")
        _assert("Keep this agent rule." in agents_off, "Agent body text survived off")
        _assert("Agents footer outside latch region stays." in agents_off,
                "Agent footer survived off")

        second = _run_confirm("unlatch", home, project / "app" / "subdir")
        _assert("already UNLATCHED" in second, second)
        _assert_record(home, claude, "claude")
        _assert_record(home, agents, "agents")

        on = _run_confirm("latch", home, other_nested)
        _assert("restored latch managed region" in on, on)
        _assert_latch_restored(home, claude, agents)
        claude_on = claude.read_text(encoding="utf-8")
        agents_on = agents.read_text(encoding="utf-8")
        _assert("Keep this Claude rule." in claude_on, "Claude body text survived on")
        _assert("Claude footer outside latch region stays." in claude_on,
                "Claude footer survived on")
        _assert("Keep this agent rule." in agents_on, "Agent body text survived on")
        _assert("Agents footer outside latch region stays." in agents_on,
                "Agent footer survived on")
        print("PASS nested_off_masks_ancestor_and_on_restores_exact_records")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nested_off_masks_ancestor_latch_regions_without_touching_plain_nested_files():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_plain_nested_"))
    try:
        home, project, _kb_marker, root_claude, root_agents = _setup(tmp)
        app = project / "app"
        nested = app / "subdir"
        nested.mkdir(parents=True)
        nested_claude = app / "CLAUDE.md"
        nested_agents = app / "AGENTS.md"
        nested_claude.write_text("# Nested Claude rules\n\nKeep this nested rule.\n",
                                 encoding="utf-8")
        nested_agents.write_text("# Nested agent rules\n\nKeep this nested agent rule.\n",
                                 encoding="utf-8")
        nested_claude_before = nested_claude.read_text(encoding="utf-8")
        nested_agents_before = nested_agents.read_text(encoding="utf-8")

        _run_confirm("unlatch", home, nested)
        _assert_unlatched(home, root_claude, root_agents)
        _assert(nested_claude.read_text(encoding="utf-8") == nested_claude_before,
                "plain nested CLAUDE.md should not be rewritten")
        _assert(nested_agents.read_text(encoding="utf-8") == nested_agents_before,
                "plain nested AGENTS.md should not be rewritten")
        records = _records_by_path(home)
        _assert(str(root_claude.resolve()) in records,
                "state should record root CLAUDE.md with latch snippet")
        _assert(str(root_agents.resolve()) in records,
                "state should record root AGENTS.md with latch snippet")
        _assert(str(nested_claude.resolve()) not in records,
                "state should not record plain nested CLAUDE.md")
        _assert(str(nested_agents.resolve()) not in records,
                "state should not record plain nested AGENTS.md")

        on = _run_confirm("latch", home, nested)
        _assert("restored latch managed region" in on, on)
        _assert_latch_restored(home, root_claude, root_agents)
        _assert(nested_claude.read_text(encoding="utf-8") == nested_claude_before,
                "plain nested CLAUDE.md should remain untouched after restore")
        _assert(nested_agents.read_text(encoding="utf-8") == nested_agents_before,
                "plain nested AGENTS.md should remain untouched after restore")
        print("PASS nested_off_masks_ancestor_latch_regions_without_touching_plain_nested_files")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_nested_off_masks_all_ancestor_latch_regions():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_all_ancestors_"))
    try:
        home, project, _kb_marker, root_claude, root_agents = _setup(tmp)
        app = project / "app"
        nested = app / "subdir"
        other_nested = project / "other" / "nested"
        nested.mkdir(parents=True)
        other_nested.mkdir(parents=True)
        app_claude = app / "CLAUDE.md"
        app_agents = app / "AGENTS.md"
        app_claude.write_text("# App Claude rules\n\nKeep this app rule.\n",
                              encoding="utf-8")
        app_agents.write_text("# App agent rules\n\nKeep this app agent rule.\n",
                              encoding="utf-8")
        cms.sync(app_claude, kb_home=str(home))
        ams.sync(app_agents, kb_home=str(home))

        _run_confirm("unlatch", home, nested)
        _assert_unlatched(home, root_claude, root_agents)
        _assert(unlatch.BEGIN_MARK in app_claude.read_text(encoding="utf-8")
                and cms.BEGIN_MARK not in app_claude.read_text(encoding="utf-8"),
                "app CLAUDE.md latch region should be masked")
        _assert(unlatch.BEGIN_MARK in app_agents.read_text(encoding="utf-8")
                and ams.BEGIN_MARK not in app_agents.read_text(encoding="utf-8"),
                "app AGENTS.md latch region should be masked")
        _assert_record(home, root_claude, "claude")
        _assert_record(home, root_agents, "agents")
        _assert_record(home, app_claude, "claude")
        _assert_record(home, app_agents, "agents")

        on = _run_confirm("latch", home, other_nested)
        _assert("restored latch managed region" in on, on)
        _assert_latch_restored(home, root_claude, root_agents)
        app_claude_on = app_claude.read_text(encoding="utf-8")
        app_agents_on = app_agents.read_text(encoding="utf-8")
        _assert(unlatch.BEGIN_MARK not in app_claude_on and cms.BEGIN_MARK in app_claude_on,
                "app CLAUDE.md should be restored")
        _assert(unlatch.BEGIN_MARK not in app_agents_on and ams.BEGIN_MARK in app_agents_on,
                "app AGENTS.md should be restored")
        print("PASS nested_off_masks_all_ancestor_latch_regions")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unlatch_roundtrip_preserves_managed_region_position():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_roundtrip_order_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        claude.write_text(
            claude.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nClaude footer after latch region.\n",
            encoding="utf-8",
        )
        agents.write_text(
            agents.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nAgents footer after latch region.\n",
            encoding="utf-8",
        )
        original_claude = claude.read_text(encoding="utf-8")
        original_agents = agents.read_text(encoding="utf-8")

        _run_confirm("unlatch", home, project)
        _run_confirm("latch", home, project)

        claude_on = claude.read_text(encoding="utf-8")
        agents_on = agents.read_text(encoding="utf-8")
        _assert(claude_on == original_claude,
                "CLAUDE.md should round-trip without moving the latch region")
        _assert(agents_on == original_agents,
                "AGENTS.md should round-trip without moving the latch region")
        _assert(claude_on.index("Keep this Claude rule.") < claude_on.index(cms.BEGIN_MARK)
                < claude_on.index("Claude footer after latch region."),
                "CLAUDE.md latch region should stay between prefix and footer")
        _assert(agents_on.index("Keep this agent rule.") < agents_on.index(ams.BEGIN_MARK)
                < agents_on.index("Agents footer after latch region."),
                "AGENTS.md latch region should stay between prefix and footer")
        print("PASS unlatch_roundtrip_preserves_managed_region_position")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unlatched_override_keeps_latch_home_breadcrumb():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_breadcrumb_"))
    try:
        home, project, _kb_marker, _claude, agents = _setup(tmp)
        _run_confirm("unlatch", home, project)
        agents_off = agents.read_text(encoding="utf-8")
        _assert(f"UNLATCHED_LATCH_HOME={home}" in agents_off,
                "unlatched override should preserve a resume path after AGENTS snippet masking")
        skill = (ROOT / ".agents" / "skills" / "source-command-unlatch" / "SKILL.md").read_text(encoding="utf-8")
        _assert("UNLATCHED_LATCH_HOME=" in skill,
                "Codex /unlatch skill should know how to recover latch home while unlatched")
        print("PASS unlatched_override_keeps_latch_home_breadcrumb")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_repeated_off_preserves_restore_state():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_repeat_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        _run_confirm("unlatch", home, project)
        second = _run_confirm("unlatch", home, project)
        _assert("already UNLATCHED" in second, second)
        _assert_unlatched(home, claude, agents)
        on = _run_confirm("latch", home, project)
        _assert("restored latch managed region" in on, on)
        _assert_latch_restored(home, claude, agents)
        print("PASS repeated_off_preserves_restore_state")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_stranded_unlatched_marker_retries_instruction_mask():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_stranded_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        (home / "UNLATCHED").write_text(
            "stranded marker without instruction state\n", encoding="utf-8")

        second = _run_confirm("unlatch", home, project)

        _assert("already UNLATCHED" in second, second)
        _assert("Retrying instruction mask" in second, second)
        _assert_unlatched(home, claude, agents)
        print("PASS stranded_unlatched_marker_retries_instruction_mask")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_latch_confirm_warns_when_env_still_forces_unlatched():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_env_still_off_"))
    try:
        home, project, _kb_marker, _claude, _agents = _setup(tmp)
        env = os.environ.copy()
        env["LATCH_HOME"] = str(home)
        env["LATCH_PYTHON"] = sys.executable
        env["LATCH_UNLATCHED"] = "1"
        env.pop("CLAUDE_KB_HOME", None)
        status = subprocess.run(
            ["bash", str(ROOT / "bin" / "unlatch.sh")],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        _assert("hooks stay off until LATCH_UNLATCHED is unset" in status.stdout,
                status.stdout)
        proc = subprocess.run(
            ["bash", str(ROOT / "bin" / "unlatch.sh"), "--confirm", "latch"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        _assert("environment disable flag is still set" in proc.stdout, proc.stdout)
        _assert("Latch is now LATCHED" not in proc.stdout, proc.stdout)
        print("PASS latch_confirm_warns_when_env_still_forces_unlatched")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_prompt_does_not_fail_when_python_missing():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_status_no_python_"))
    try:
        home, project, _kb_marker, _claude, _agents = _setup(tmp)
        env = os.environ.copy()
        env["LATCH_HOME"] = str(home)
        env["LATCH_PYTHON"] = str(tmp / "missing-python")
        env.pop("CLAUDE_KB_HOME", None)
        proc = subprocess.run(
            ["bash", str(ROOT / "bin" / "unlatch.sh")],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        _assert("Latch is currently LATCHED" in proc.stdout, proc.stdout)
        _assert("instruction mask status unavailable" in proc.stdout, proc.stdout)
        _assert(not (home / "UNLATCHED").exists(), "status must not mutate")
        print("PASS status_prompt_does_not_fail_when_python_missing")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_repeated_instruction_mask_preserves_restore_state():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_mask_repeat_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        _run_confirm("unlatch", home, project)
        repeated = _run_instruction_mask(home, project, "off")
        _assert("preserved latch restore state" in repeated, repeated)
        _assert_record(home, claude, "claude")
        _assert_record(home, agents, "agents")
        on = _run_confirm("latch", home, project)
        _assert("restored latch managed region" in on, on)
        _assert_latch_restored(home, claude, agents)
        print("PASS repeated_instruction_mask_preserves_restore_state")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unlatched_hooks_emit_receipt_without_heavy_runtime_imports():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_hooks_"))
    try:
        home = tmp / "home"
        project = tmp / "project"
        home.mkdir()
        project.mkdir()
        (home / "UNLATCHED").write_text("unlatched\n", encoding="utf-8")

        for script in ("user_prompt_submit.py", "stop.py", "session_end.py"):
            out = _run_hook_unlatched(script, home, project)
            payload = json.loads(out)
            context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
            _assert("Latch is currently UNLATCHED" in context,
                    f"{script} should emit unlatched receipt: {out}")
            _assert("for this latch install" in context,
                    f"{script} should name install-level scope: {out}")
            _assert("If LATCH_UNLATCHED is set, unset it too" in context,
                    f"{script} should mention env-forced unlatch: {out}")
        print("PASS unlatched_hooks_emit_receipt_without_heavy_runtime_imports")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unlatched_gate_cli_fast_fails_without_opening_kb():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_gate_cli_"))
    try:
        home = tmp / "home"
        project = tmp / "project"
        home.mkdir()
        project.mkdir()
        (home / "UNLATCHED").write_text("unlatched\n", encoding="utf-8")
        env = os.environ.copy()
        env["LATCH_HOME"] = str(home)
        env.pop("CLAUDE_KB_HOME", None)

        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "src" / "latch" / "gate" / "kb_gate_cli.py"),
                str(project),
                "change the app",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

        payload = json.loads(proc.stdout)
        _assert(payload.get("ok") is False, payload)
        _assert(payload.get("reason") == "unlatched", payload)
        findings = payload.get("findings", {})
        _assert("If LATCH_UNLATCHED is set, unset it too"
                in findings.get("why_it_matters", ""), payload)
        _assert(payload.get("chain_summary", {}).get("seed_count") == 0, payload)
        _assert(not (home / "projects").exists(),
                "unlatched gate CLI must not create/open a project KB")
        print("PASS unlatched_gate_cli_fast_fails_without_opening_kb")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_on_skips_missing_recorded_instruction_file():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_missing_restore_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        _run_confirm("unlatch", home, project)
        claude.unlink()

        on = _run_confirm("latch", home, project)
        _assert("skipped missing instruction file" in on, on)
        _assert("not recreating latch managed region" in on, on)
        _assert(not claude.exists(), "resume should not recreate a deleted CLAUDE.md")
        _assert(not (home / "UNLATCHED").exists(), "resume should remove UNLATCHED")
        _assert(not (home / "DISABLE").exists(), "resume should remove DISABLE")
        _assert(not (home / "UNLATCH_STATE.json").exists(),
                "resume should remove unlatch instruction state")
        agents_on = agents.read_text(encoding="utf-8")
        _assert(unlatch.BEGIN_MARK not in agents_on and ams.BEGIN_MARK in agents_on,
                "existing AGENTS.md should still restore")
        print("PASS on_skips_missing_recorded_instruction_file")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_latch_enable_restores_unlatch_instruction_masks():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_enable_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        _run_confirm("unlatch", home, project)
        out = _run_enable(home, project)
        _assert("restored latch managed region" in out, out)
        _assert("latch ENABLED" in out, out)
        _assert_latch_restored(home, claude, agents)
        print("PASS latch_enable_restores_unlatch_instruction_masks")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_latch_enable_warns_when_env_still_forces_unlatched():
    tmp = Path(tempfile.mkdtemp(prefix="unlatch_enable_env_"))
    try:
        home, project, _kb_marker, claude, agents = _setup(tmp)
        _run_confirm("unlatch", home, project)
        env = os.environ.copy()
        env["LATCH_HOME"] = str(home)
        env["LATCH_PYTHON"] = sys.executable
        env["LATCH_UNLATCHED"] = "1"
        env.pop("CLAUDE_KB_HOME", None)
        proc = subprocess.run(
            ["bash", str(ROOT / "bin" / "latch_enable.sh")],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        _assert("environment disable flag is still set" in proc.stdout, proc.stdout)
        _assert("Unset it before expecting hooks to resume" in proc.stdout, proc.stdout)
        _assert("hooks resume on the next prompt" not in proc.stdout, proc.stdout)
        _assert_latch_restored(home, claude, agents)
        print("PASS latch_enable_warns_when_env_still_forces_unlatched")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_unlatch_off_status_on_cycle()
    test_unlatch_confirmation_contract_is_strict()
    test_nested_off_masks_ancestor_and_on_restores_exact_records()
    test_nested_off_masks_ancestor_latch_regions_without_touching_plain_nested_files()
    test_nested_off_masks_all_ancestor_latch_regions()
    test_unlatch_roundtrip_preserves_managed_region_position()
    test_unlatched_override_keeps_latch_home_breadcrumb()
    test_repeated_off_preserves_restore_state()
    test_stranded_unlatched_marker_retries_instruction_mask()
    test_latch_confirm_warns_when_env_still_forces_unlatched()
    test_status_prompt_does_not_fail_when_python_missing()
    test_repeated_instruction_mask_preserves_restore_state()
    test_unlatched_hooks_emit_receipt_without_heavy_runtime_imports()
    test_unlatched_gate_cli_fast_fails_without_opening_kb()
    test_on_skips_missing_recorded_instruction_file()
    test_latch_enable_restores_unlatch_instruction_masks()
    test_latch_enable_warns_when_env_still_forces_unlatched()
    print("\nAll unlatch tests pass.")
