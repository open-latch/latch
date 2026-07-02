"""Unit tests for doctor.check_commands_installed — the slash-commands-installed
wiring check (id=1468 #1). The env/probe checks shell out to subprocesses and
are exercised by the README verify flow; this covers the new pure-logic check
(presence + <KB_HOME>-resolved, WARN-not-FAIL like the MCP-wiring check)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import doctor  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


class _FakeProc:
    def __init__(self, rc: int, out: str = "", err: str = ""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_mcp_wiring_accepts_legacy_alias():
    old_which = shutil.which
    old_run = subprocess.run
    try:
        shutil.which = lambda name: "/usr/bin/claude" if name == "claude" else old_which(name)

        def fake_run(cmd, capture_output=True, text=True, timeout=30):
            if cmd[1:3] == ["mcp", "get"] and cmd[3] == "latch":
                return _FakeProc(1, err="not found")
            if cmd[1:3] == ["mcp", "get"] and cmd[3] == "claude-kb":
                return _FakeProc(0, out="Name: claude-kb\nStatus: connected\n")
            raise AssertionError(f"unexpected command: {cmd}")

        subprocess.run = fake_run
        _name, level, detail = doctor.check_mcp_wiring()
        _assert(level == doctor.OK, f"legacy alias should be OK, got {level}: {detail}")
        _assert("legacy alias" in detail and "claude-kb" in detail, detail)
        print("PASS mcp_wiring_accepts_legacy_alias")
    finally:
        shutil.which = old_which
        subprocess.run = old_run


def _setup(source_names, dest_bodies=None):
    """Point doctor.SRC_DIR at a temp 'src' whose sibling commands/ holds
    `source_names`, and CLAUDE_COMMANDS_DIR at a temp dest seeded with
    `dest_bodies` ({name: body}). Returns (dest, restore)."""
    root = Path(tempfile.mkdtemp(prefix="latch-doc-"))
    src_dir = root / "src"
    src_dir.mkdir()
    cmds = root / "commands"
    cmds.mkdir()
    for n in source_names:
        (cmds / n).write_text(f"body <KB_HOME> for {n}\n", encoding="utf-8")
    dest = root / "dest"
    dest.mkdir()
    for n, body in (dest_bodies or {}).items():
        (dest / n).write_text(body, encoding="utf-8")
    saved_src = doctor.SRC_DIR
    saved_env = os.environ.get("CLAUDE_COMMANDS_DIR")
    doctor.SRC_DIR = src_dir
    os.environ["CLAUDE_COMMANDS_DIR"] = str(dest)

    def restore():
        doctor.SRC_DIR = saved_src
        if saved_env is None:
            os.environ.pop("CLAUDE_COMMANDS_DIR", None)
        else:
            os.environ["CLAUDE_COMMANDS_DIR"] = saved_env
    return dest, restore


def test_commands_missing_warns():
    dest, restore = _setup(["latch-compact.md", "latch-gate.md"], dest_bodies={})
    try:
        _name, level, detail = doctor.check_commands_installed()
        _assert(level == doctor.WARN, f"missing commands should WARN, got {level}: {detail}")
        _assert("Unknown skill" in detail, f"detail should name the symptom: {detail}")
        _assert(str(dest) in detail, f"detail should include command destination: {detail}")
        _assert("HOME=" in detail and "CLAUDE_COMMANDS_DIR=" in detail,
                f"detail should include shell context for Windows path mismatches: {detail}")
        print("PASS commands_missing_warns")
    finally:
        restore()


def test_commands_present_ok():
    _dest, restore = _setup(
        ["latch-compact.md", "latch-gate.md"],
        dest_bodies={"latch-compact.md": "resolved /home body\n", "latch-gate.md": "resolved\n"})
    try:
        _name, level, detail = doctor.check_commands_installed()
        _assert(level == doctor.OK, f"present+resolved should be OK, got {level}: {detail}")
        print("PASS commands_present_ok")
    finally:
        restore()


def test_commands_unresolved_placeholder_warns():
    _dest, restore = _setup(
        ["latch-compact.md"],
        dest_bodies={"latch-compact.md": "still <KB_HOME> here\n"})
    try:
        _name, level, detail = doctor.check_commands_installed()
        _assert(level == doctor.WARN, f"unresolved placeholder should WARN, got {level}")
        _assert("placeholder" in detail, f"detail should mention placeholder: {detail}")
        _assert("source=" in detail and "dest=" in detail,
                f"detail should include source/dest context: {detail}")
        print("PASS commands_unresolved_placeholder_warns")
    finally:
        restore()


def test_commands_stale_legacy_warns():
    _dest, restore = _setup(
        ["latch-gate.md"],
        dest_bodies={
            "latch-gate.md": "resolved /home body\n",
            "kb-focus.md": "bash /tmp/latch/bin/run_kb_focus.sh list\n",
        })
    try:
        _name, level, detail = doctor.check_commands_installed()
        _assert(level == doctor.WARN, f"stale legacy command should WARN, got {level}: {detail}")
        _assert("stale legacy" in detail and "kb-focus.md" in detail,
                f"detail should name stale command: {detail}")
        print("PASS commands_stale_legacy_warns")
    finally:
        restore()


import json
import sqlite3


def _setup_kb(kbs, *, file_pin=None, env_pin=None):
    """Build a temp CLAUDE_KB_HOME with projects/<name>/kb.db holding `kbs`
    ({name: node_count}). Optionally write kb_location.json (file_pin -> name)
    and/or set CLAUDE_KB_DIR (env_pin -> name). Returns (home, restore)."""
    home = Path(tempfile.mkdtemp(prefix="latch-pin-"))
    for name, rows in kbs.items():
        d = home / "projects" / name
        d.mkdir(parents=True)
        c = sqlite3.connect(d / "kb.db")
        c.execute("CREATE TABLE nodes(id INTEGER)")
        c.executemany("INSERT INTO nodes(id) VALUES(?)", [(i,) for i in range(rows)])
        c.commit()
        c.close()
    if file_pin is not None:
        (home / "kb_location.json").write_text(
            json.dumps({"kb_dir": str(home / "projects" / file_pin)}), encoding="utf-8")
    saved_home = os.environ.get("CLAUDE_KB_HOME")
    saved_latch_home = os.environ.get("LATCH_HOME")
    saved_dir = os.environ.get("CLAUDE_KB_DIR")
    saved_latch_dir = os.environ.get("LATCH_KB_DIR")
    os.environ["CLAUDE_KB_HOME"] = str(home)
    os.environ.pop("LATCH_HOME", None)
    if env_pin is not None:
        os.environ["CLAUDE_KB_DIR"] = str(home / "projects" / env_pin)
    else:
        os.environ.pop("CLAUDE_KB_DIR", None)
    os.environ.pop("LATCH_KB_DIR", None)

    def restore():
        for k, v in (
            ("CLAUDE_KB_HOME", saved_home),
            ("LATCH_HOME", saved_latch_home),
            ("CLAUDE_KB_DIR", saved_dir),
            ("LATCH_KB_DIR", saved_latch_dir),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return home, restore


def test_pin_via_file_ok():
    home, restore = _setup_kb({"repo": 5}, file_pin="repo")
    try:
        _name, level, detail = doctor.check_kb_pin()
        _assert(level == doctor.OK, f"file pin should be OK, got {level}: {detail}")
        _assert("pinned ->" in detail and "kb_location.json" in detail, detail)
        print("PASS pin_via_file_ok")
    finally:
        restore()


def test_pin_via_env_ok():
    home, restore = _setup_kb({"repo": 5}, env_pin="repo")
    try:
        _name, level, detail = doctor.check_kb_pin()
        _assert(level == doctor.OK, f"env pin should be OK, got {level}: {detail}")
        _assert("CLAUDE_KB_DIR" in detail, detail)
        print("PASS pin_via_env_ok")
    finally:
        restore()


def test_pin_via_latch_env_ok():
    home, restore = _setup_kb({"repo": 5})
    saved = os.environ.get("LATCH_KB_DIR")
    try:
        os.environ["LATCH_KB_DIR"] = str(home / "projects" / "repo")
        _name, level, detail = doctor.check_kb_pin()
        _assert(level == doctor.OK, f"LATCH_KB_DIR pin should be OK, got {level}: {detail}")
        _assert("LATCH_KB_DIR" in detail, detail)
        print("PASS pin_via_latch_env_ok")
    finally:
        if saved is None:
            os.environ.pop("LATCH_KB_DIR", None)
        else:
            os.environ["LATCH_KB_DIR"] = saved
        restore()


def test_unpinned_multi_warns_and_ranks_by_nodes():
    home, restore = _setup_kb({"big": 40, "small": 3, "tiny": 1})
    try:
        _name, level, detail = doctor.check_kb_pin()
        _assert(level == doctor.WARN, f"unpinned+multi should WARN, got {level}")
        _assert("will NOT merge" in detail, f"must state latch won't merge: {detail}")
        _assert("--kb-dir" in detail, f"must suggest the lock command: {detail}")
        # biggest-by-nodes must be the recommended target, not biggest-by-size
        rec = detail.rsplit("--kb-dir", 1)[1]
        _assert("big" in rec and "small" not in rec.split("\n")[0],
                f"should recommend the largest-by-nodes KB: {rec}")
        _assert("(1 node)" in detail, f"singular pluralization: {detail}")
        print("PASS unpinned_multi_warns_and_ranks_by_nodes")
    finally:
        restore()


def test_unpinned_single_warns():
    home, restore = _setup_kb({"solo": 7})
    try:
        _name, level, detail = doctor.check_kb_pin()
        _assert(level == doctor.WARN, f"unpinned single should WARN, got {level}")
        _assert("legacy per-cwd" in detail, detail)
        _assert("--kb-dir" in detail, detail)
        print("PASS unpinned_single_warns")
    finally:
        restore()


def test_pinned_with_legacy_dirs_warns():
    # Pinned, but other KB dirs still under projects/ -> WARN about stranded
    # history (future routing is safe), NOT a clean OK (P2 review finding).
    home, restore = _setup_kb({"repo": 9, "old1": 4, "old2": 1}, file_pin="repo")
    try:
        _name, level, detail = doctor.check_kb_pin()
        _assert(level == doctor.WARN, f"pinned + leftovers should WARN, got {level}: {detail}")
        _assert("stranded" in detail, f"must flag stranded history: {detail}")
        _assert("will NOT merge" in detail, f"must say latch won't merge: {detail}")
        _assert("2 other" in detail, f"should count the 2 non-pinned dirs: {detail}")
        print("PASS pinned_with_legacy_dirs_warns")
    finally:
        restore()


def test_powershell_example_uses_kebab_flag():
    # P1 review finding: the PowerShell remediation must use --kb-dir, never the
    # PowerShell-style -KbDir, which src/install_engine.py's argparse rejects.
    cmd = doctor._pin_command("/tmp/x")
    _assert("-KbDir" not in cmd, f"must not emit -KbDir: {cmd}")
    _assert(cmd.count("--kb-dir") == 2, f"both bash + PowerShell use --kb-dir: {cmd}")
    print("PASS powershell_example_uses_kebab_flag")


if __name__ == "__main__":
    test_mcp_wiring_accepts_legacy_alias()
    test_commands_missing_warns()
    test_commands_present_ok()
    test_commands_unresolved_placeholder_warns()
    test_commands_stale_legacy_warns()
    test_pin_via_file_ok()
    test_pin_via_env_ok()
    test_pin_via_latch_env_ok()
    test_unpinned_multi_warns_and_ranks_by_nodes()
    test_unpinned_single_warns()
    test_pinned_with_legacy_dirs_warns()
    test_powershell_example_uses_kebab_flag()
    print("\nAll doctor tests pass.")
