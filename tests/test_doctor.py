"""Unit tests for doctor.check_commands_installed — the slash-commands-installed
wiring check (id=1468 #1). The env/probe checks shell out to subprocesses and
are exercised by the README verify flow; this covers the new pure-logic check
(presence + <KB_HOME>-resolved, WARN-not-FAIL like the MCP-wiring check)."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
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


def test_commands_unresolved_review_literal_placeholder_warns():
    _dest, restore = _setup(
        ["latch-review.md"],
        dest_bodies={
            "latch-review.md": "bash <LATCH_REVIEW_POSIX_LITERAL> --pr 75\n"
        },
    )
    try:
        _name, level, detail = doctor.check_commands_installed()
        _assert(level == doctor.WARN, f"unresolved review placeholder should WARN: {detail}")
        _assert("install-path placeholder" in detail, detail)
        print("PASS commands_unresolved_review_literal_placeholder_warns")
    finally:
        restore()


def test_commands_stale_legacy_warns():
    _dest, restore = _setup(
        ["latch-gate.md"],
        dest_bodies={
            "latch-gate.md": "resolved /home body\n",
            "latch-baseline.md": "bash /tmp/latch/bin/latch_baseline.sh status\n",
            "kb-focus.md": "bash /tmp/latch/bin/run_kb_focus.sh list\n",
            "mission-control.md": (
                "Call kb_profile_active, then kb_profile_bind. "
                "Escalates the current user into mission-control verification profile.\n"
            ),
        })
    try:
        _name, level, detail = doctor.check_commands_installed()
        _assert(level == doctor.WARN, f"stale legacy command should WARN, got {level}: {detail}")
        _assert("stale legacy" in detail and "latch-baseline.md" in detail
                and "kb-focus.md" in detail
                and "mission-control.md" in detail,
                f"detail should name stale command: {detail}")
        print("PASS commands_stale_legacy_warns")
    finally:
        restore()


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


def test_mcp_lifecycle_warns_on_recent_pressure(monkeypatch):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 3,
        "counts": {"proxy_over_cap": 2, "prompt_retrieval_degraded": 1},
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, f"recent pressure should WARN: {detail}")
    _assert("proxy_over_cap=2" in detail, detail)
    _assert("prompt_retrieval_degraded=1" in detail, detail)


def test_mcp_lifecycle_ok_names_operational_contract(monkeypatch):
    import mcp_broker

    monkeypatch.delenv("LATCH_MCP_ALLOW_LEGACY_FALLBACK", raising=False)
    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0, "counts": {},
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker,
        "proxy_lease_state",
            lambda **_kwargs: {"live": [{"connection_id": "a"}]},
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: {"pid": 123})
    monkeypatch.setattr(mcp_broker, "probe_discovery", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(doctor.paths, "maintenance_runner_status", lambda: {
        "configured": True,
        "backend": "codex",
        "error": None,
        "remedy": None,
    })
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.OK, detail)
    _assert("live leases=1/32" in detail and "retire idle=300s" in detail, detail)


def test_mcp_lifecycle_warns_when_autonomous_maintenance_is_unconfigured(
    monkeypatch,
):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0, "counts": {},
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(doctor.paths, "maintenance_runner_status", lambda: {
        "configured": False,
        "backend": None,
        "error": "missing",
        "remedy": "rerun latch quickstart for this vault",
    })

    _name, level, detail = doctor.check_mcp_runtime_lifecycle()

    _assert(level == doctor.WARN, detail)
    _assert("autonomous maintenance is not configured" in detail, detail)
    _assert("rerun latch quickstart" in detail, detail)


def test_mcp_lifecycle_warns_at_configured_75_percent_high_water(monkeypatch):
    import mcp_broker

    monkeypatch.delenv("LATCH_MCP_ALLOW_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("LATCH_MCP_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0,
        "counts": {},
        "proxy_high_water": 8,
        "current_over_cap_duration_s": 0,
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 10, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, detail)
    _assert("75% review threshold 8/10" in detail, detail)
    _assert(doctor._proxy_high_water_warn_at(32) == 24, "default threshold must be 24")
    _assert(doctor._proxy_high_water_warn_at(0) is None, "unbounded cap must not warn")


def test_mcp_lifecycle_warns_while_over_cap(monkeypatch):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0,
        "counts": {},
        "proxy_high_water": 5,
        "currently_over_cap": True,
        "current_over_cap_duration_s": 42.5,
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, detail)
    _assert("currently over cap for at least 42.5s" in detail, detail)


def test_mcp_lifecycle_retirement_warning_names_host_boundary(monkeypatch):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 1,
        "counts": {"proxy_retired": 1},
        "proxy_high_water": 33,
        "current_over_cap_duration_s": 0,
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, detail)
    _assert("cannot prove same-task host restart" in detail, detail)
    _assert("confirm reconnect or start a fresh task" in detail, detail)


def test_mcp_lifecycle_warns_on_dead_discovery(monkeypatch):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0,
        "counts": {},
        "proxy_high_water": 0,
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: {"pid": 999999})
    monkeypatch.setattr(mcp_broker, "probe_discovery", lambda *_args, **_kwargs: False)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, detail)
    _assert("unreachable owner pid=999999" in detail, detail)


def test_mcp_lifecycle_warns_on_current_stale_leases(monkeypatch):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0,
        "counts": {},
        "proxy_high_water": 2,
        "current_stale_leases": 2,
        "max_stale_lease_age_s": 412.5,
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(
        mcp_broker, "proxy_lease_state", lambda **_kwargs: {"live": []}
    )
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, detail)
    _assert("2 stale live lease(s)" in detail, detail)
    _assert("412.5s" in detail, detail)


def test_mcp_lifecycle_warns_on_registry_wide_historical_pools(monkeypatch):
    import mcp_broker

    monkeypatch.setattr(mcp_broker, "lifecycle_summary", lambda **_kwargs: {
        "warning_count": 0,
        "counts": {},
        "proxy_high_water": 3,
    })
    monkeypatch.setattr(mcp_broker, "proxy_policy", lambda: {
        "cap": 32, "retire_idle_s": 300.0, "heartbeat_s": 30.0, "stale_s": 300.0,
    })
    monkeypatch.setattr(mcp_broker, "proxy_lease_state", lambda **_kwargs: {
        "live": [{"connection_id": "current"}],
        "legacy_incompatible": [{"connection_id": "old"}],
        "unassociated_capable": [{"connection_id": "waiting"}],
        "other_live_owner": [{"connection_id": "blue-green"}],
        "observed_live": [{"connection_id": str(index)} for index in range(4)],
    })
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: {"pid": 123})
    monkeypatch.setattr(mcp_broker, "probe_discovery", lambda *_args, **_kwargs: True)
    _name, level, detail = doctor.check_mcp_runtime_lifecycle()
    _assert(level == doctor.WARN, detail)
    _assert("pre-capability proxy lease(s)" in detail, detail)
    _assert("included in the owner cap" in detail, detail)
    _assert("another live blue/green owner" in detail, detail)


if __name__ == "__main__":
    test_mcp_wiring_accepts_legacy_alias()
    test_commands_missing_warns()
    test_commands_present_ok()
    test_commands_unresolved_placeholder_warns()
    test_commands_unresolved_review_literal_placeholder_warns()
    test_commands_stale_legacy_warns()
    test_pin_via_file_ok()
    test_pin_via_env_ok()
    test_pin_via_latch_env_ok()
    test_unpinned_multi_warns_and_ranks_by_nodes()
    test_unpinned_single_warns()
    test_pinned_with_legacy_dirs_warns()
    test_powershell_example_uses_kebab_flag()
    # pytest supplies monkeypatch to the two lifecycle tests above.
    print("\nAll doctor tests pass.")
