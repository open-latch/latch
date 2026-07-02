"""Unit tests for install_engine — the settings.json merge logic, which is the
risky part (the MCP-registration path just shells out to the `claude` CLI and
is covered by --check/--dry-run in the README verify flow).

Covers the three invariants the installer must hold:
  * permission rule is union-added (never duplicated),
  * dead latch-owned `mcpServers` blocks are removed while other servers survive,
  * latch hooks are replaced (not duplicated) on re-run and unrelated hooks /
    top-level keys are preserved,
  * the whole merge is idempotent (a second run reports no changes),
  * load_snippet substitutes placeholders and carries no mcpServers block.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import install_engine as ie  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp_settings(obj) -> Path:
    d = Path(tempfile.mkdtemp(prefix="latch-ie-"))
    p = d / "settings.json"
    p.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return p


def _with_settings_path(path: Path):
    """Point the module's SETTINGS_PATH at a temp file; return the old value."""
    old = ie.SETTINGS_PATH
    ie.SETTINGS_PATH = path
    return old


def test_is_latch_hook_entry():
    latch = {"matcher": "", "hooks": [
        {"type": "command", "command": "/x/.venv/bin/python /x/src/hooks/stop.py"}]}
    other = {"matcher": "", "hooks": [
        {"type": "command", "command": "prettier --write $FILE"}]}
    _assert(ie._is_latch_hook_entry(latch) is True, "latch hook should be detected")
    _assert(ie._is_latch_hook_entry(other) is False, "unrelated hook must not match")
    print("PASS is_latch_hook_entry")


def test_load_snippet_substitutes_and_drops_mcpservers():
    snip = ie.load_snippet("/ABS/PY")
    _assert("mcpServers" not in snip, "snippet must not carry an mcpServers block")
    _assert("mcp__latch" in snip["permissions"]["allow"],
            "snippet must pre-approve the server-level rule")
    _assert("mcp__claude-kb" in snip["permissions"]["allow"],
            "snippet must keep the legacy server-level rule during the rename")
    cmd = snip["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    _assert("{{PYTHON_PATH}}" not in cmd and "/ABS/PY" in cmd,
            f"placeholder not substituted: {cmd!r}")
    _assert(ie.LATCH_HOOK_MARKER in cmd, "substituted hook should look latch-owned")
    print("PASS load_snippet_substitutes_and_drops_mcpservers")


def test_load_snippet_normalizes_windows_python_path():
    # A raw Windows interpreter path (backslashes) injected verbatim into JSON
    # produces invalid escapes (\U, \A, ...) and crashes json.loads. resolve_python
    # returns exactly such a path on Windows (.venv / sys.executable / which), so
    # load_snippet MUST forward-slash it. Regression for the Windows install crash.
    win_py = r"C:\Users\dev\AppData\Local\Programs\Python\Python311\python.exe"
    snip = ie.load_snippet(win_py)  # must not raise JSONDecodeError
    cmd = snip["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    _assert("\\" not in cmd, f"backslashes must be normalized out of the command: {cmd!r}")
    _assert("C:/Users/dev" in cmd and "{{PYTHON_PATH}}" not in cmd,
            f"windows python path not substituted/normalized: {cmd!r}")
    print("PASS load_snippet_normalizes_windows_python_path")


def test_merge_preserves_others_adds_perm_removes_dead_block():
    existing = {
        "theme": "dark",
        "mcpServers": {
            "latch": {"command": "/old/python", "args": ["/old/src/mcp_server.py"]},
            "claude-kb": {"command": "/old/python", "args": ["/old/src/mcp_server.py"]},
            "other-server": {"command": "node", "args": ["x.js"]},
        },
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "echo hi"}]},
                {"matcher": "", "hooks": [{"type": "command",
                                           "command": "/OLD/py /OLD/src/hooks/session_start.py"}]},
            ],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "log.sh"}]},
            ],
        },
        "permissions": {"allow": ["Bash(git *)", "mcp__claude-kb"]},
    }
    old = _with_settings_path(_tmp_settings(existing))
    try:
        snippet = ie.load_snippet("/PY")
        new, changes = ie.merge_settings(snippet)

        # permission union-added, existing kept, not duplicated
        allow = new["permissions"]["allow"]
        _assert(allow.count("mcp__latch") == 1, f"new perm should appear once: {allow}")
        _assert(allow.count("mcp__claude-kb") == 1,
                f"legacy perm should be preserved for existing registrations: {allow}")
        _assert("Bash(git *)" in allow, "existing permission must be preserved")

        # dead latch-owned blocks removed; unrelated server survives
        _assert("latch" not in new.get("mcpServers", {}), "dead primary block must be removed")
        _assert("claude-kb" not in new.get("mcpServers", {}), "dead block must be removed")
        _assert("other-server" in new["mcpServers"], "unrelated mcp server must survive")

        # every managed event gets exactly one latch hook
        for ev in ie.MANAGED_EVENTS:
            latch = [e for e in new["hooks"][ev] if ie._is_latch_hook_entry(e)]
            expected = 2 if ev == "PostToolUse" else 1
            _assert(len(latch) == expected,
                    f"{ev}: expected {expected} latch hook(s), got {len(latch)}")

        # the user's non-latch SessionStart hook is preserved; the stale latch one replaced
        ss = new["hooks"]["SessionStart"]
        _assert(any(not ie._is_latch_hook_entry(e) for e in ss), "user's hook must survive")
        ss_latch_cmd = [e for e in ss if ie._is_latch_hook_entry(e)][0]["hooks"][0]["command"]
        _assert("/OLD/py" not in ss_latch_cmd and "/PY" in ss_latch_cmd,
                "stale latch hook should be re-pointed to the new interpreter")

        # unrelated event + top-level key preserved
        _assert(new["hooks"]["PreToolUse"], "unrelated hook event must survive")
        _assert(new["theme"] == "dark", "unrelated top-level key must survive")

        _assert(changes, "a real merge should report changes")
        print("PASS merge_preserves_others_adds_perm_removes_dead_block")
    finally:
        ie.SETTINGS_PATH = old


class _FakeRun:
    def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def test_mcp_status_accepts_legacy_registration():
    old_run = ie._run
    try:
        def fake_run(cmd, timeout=30):
            if cmd[:3] == ["claude", "mcp", "get"] and cmd[3] == "latch":
                return _FakeRun(1, stderr="not found")
            if cmd[:3] == ["claude", "mcp", "get"] and cmd[3] == "claude-kb":
                return _FakeRun(0, stdout="Command: /PY\nArgs: /srv.py\nStatus: connected\n")
            raise AssertionError(f"unexpected command: {cmd}")

        ie._run = fake_run
        _assert(ie.mcp_status("claude", "/PY", "/srv.py") == "legacy_matches",
                "legacy claude-kb registration should remain supported")
        level, msg = ie.register_mcp("claude", "/PY", "/srv.py", dry_run=False)
        _assert(level == "OK" and "legacy MCP registration" in msg, msg)
        print("PASS mcp_status_accepts_legacy_registration")
    finally:
        ie._run = old_run


def test_register_mcp_fresh_install_uses_latch_name():
    old_run = ie._run
    seen: list[list[str]] = []
    try:
        def fake_run(cmd, timeout=30):
            seen.append(cmd)
            if cmd[:3] == ["claude", "mcp", "get"]:
                return _FakeRun(1, stderr="not found")
            if cmd[:3] == ["claude", "mcp", "add"]:
                return _FakeRun(0, stdout="added")
            raise AssertionError(f"unexpected command: {cmd}")

        ie._run = fake_run
        level, msg = ie.register_mcp("claude", "/PY", "/srv.py", dry_run=False)
        _assert(level == "OK", msg)
        _assert(["claude", "mcp", "add", "latch", "--scope", "user",
                 "--", "/PY", "/srv.py"] in seen,
                f"fresh install should register latch, saw {seen}")
        print("PASS register_mcp_fresh_install_uses_latch_name")
    finally:
        ie._run = old_run


def test_merge_idempotent():
    existing = {
        "hooks": {"SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "keep.sh"}]}]},
        "permissions": {"allow": []},
    }
    old = _with_settings_path(_tmp_settings(existing))
    try:
        snippet = ie.load_snippet("/PY")
        new1, changes1 = ie.merge_settings(snippet)
        _assert(changes1, "first merge should change things")
        # persist the merged result, then merge again from that state
        ie.SETTINGS_PATH.write_text(json.dumps(new1, indent=2), encoding="utf-8")
        new2, changes2 = ie.merge_settings(snippet)
        _assert(changes2 == [], f"second merge must be a no-op, got: {changes2}")
        print("PASS merge_idempotent")
    finally:
        ie.SETTINGS_PATH = old


def test_write_settings_backs_up_existing():
    existing = {"theme": "light", "mcpServers": {"claude-kb": {"command": "x"}}}
    old = _with_settings_path(_tmp_settings(existing))
    try:
        snippet = ie.load_snippet("/PY")
        new, _ = ie.merge_settings(snippet)
        ie.write_settings(new)
        backup = ie.SETTINGS_PATH.with_suffix(ie.SETTINGS_PATH.suffix + ".latchbak")
        _assert(backup.exists(), "a .latchbak backup must be written")
        bobj = json.loads(backup.read_text(encoding="utf-8"))
        _assert("claude-kb" in bobj.get("mcpServers", {}),
                "backup must retain the pre-mutation (dead-block) state for rollback")
        nobj = json.loads(ie.SETTINGS_PATH.read_text(encoding="utf-8"))
        _assert("claude-kb" not in nobj.get("mcpServers", {}),
                "written settings must have the dead legacy block removed")
        print("PASS write_settings_backs_up_existing")
    finally:
        ie.SETTINGS_PATH = old


def _tmp_commands_env(kb_home: str, command_bodies: dict):
    """Create a temp commands/ source + an empty dest, point the module globals
    at them, and return (src, dest, restore). command_bodies maps filename ->
    body containing the <KB_HOME> placeholder."""
    root = Path(tempfile.mkdtemp(prefix="latch-cmd-"))
    src = root / "commands"
    src.mkdir()
    for name, body in command_bodies.items():
        (src / name).write_text(body, encoding="utf-8")
    dest = root / "dest_commands"
    saved = (ie.COMMANDS_SRC, ie.COMMANDS_DEST, ie.KB_HOME)
    ie.COMMANDS_SRC, ie.COMMANDS_DEST, ie.KB_HOME = src, dest, Path(kb_home)

    def restore():
        ie.COMMANDS_SRC, ie.COMMANDS_DEST, ie.KB_HOME = saved
    return src, dest, restore


def test_install_commands_copies_and_substitutes():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch",
        {"latch-compact.md": "run <KB_HOME>/bin/run_compact_now.sh\n",
         "latch-gate.md": "see <KB_HOME>/src\n"})
    try:
        level, changes = ie.install_commands(dry_run=False)
        _assert(level == "OK", f"expected OK, got {level}")
        _assert((dest / "latch-compact.md").exists(), "latch-compact.md should be installed")
        _assert(not (dest / "kb-compact.md").exists(), "fresh install should not create legacy alias")
        body = (dest / "latch-compact.md").read_text(encoding="utf-8")
        _assert("<KB_HOME>" not in body, "placeholder must be resolved")
        _assert("/opt/latch/bin/run_compact_now.sh" in body, f"KB_HOME not substituted: {body!r}")
        _assert(len(changes) == 2, f"expected 2 installs, got {changes}")
        print("PASS install_commands_copies_and_substitutes")
    finally:
        restore()


def test_install_commands_dry_run_writes_nothing():
    _src, dest, restore = _tmp_commands_env("/opt/latch", {"latch-compact.md": "x <KB_HOME>\n"})
    try:
        level, changes = ie.install_commands(dry_run=True)
        _assert(level == "OK", f"expected OK, got {level}")
        _assert(not dest.exists() or not list(dest.glob("*")), "dry-run must write nothing")
        _assert(changes and changes[0].startswith("would install"), f"dry-run changes: {changes}")
        print("PASS install_commands_dry_run_writes_nothing")
    finally:
        restore()


def test_commands_status_missing_then_present_then_unresolved():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch", {"latch-compact.md": "x <KB_HOME>\n", "latch-gate.md": "y <KB_HOME>\n"})
    try:
        ok, label = ie.commands_status()
        _assert(not ok, f"missing commands should fail --check: {label}")
        ie.install_commands(dry_run=False)            # now present + resolved
        ok, label = ie.commands_status()
        _assert(ok, f"after install, --check should pass: {label}")
        # plant an unresolved placeholder -> fail
        (dest / "latch-gate.md").write_text("still <KB_HOME> here\n", encoding="utf-8")
        ok, label = ie.commands_status()
        _assert(not ok and "placeholder" in label, f"unresolved placeholder should fail: {label}")
        print("PASS commands_status_missing_then_present_then_unresolved")
    finally:
        restore()


def test_install_commands_updates_existing_legacy_aliases():
    _src, dest, restore = _tmp_commands_env(
        "/new/latch", {
            "latch-gate.md": "bash <KB_HOME>/bin/run_latch_gate.sh\n",
            "latch-gate-report.md": "bash <KB_HOME>/bin/latch_gate_report.sh\n",
            "latch-compact.md": "bash <KB_HOME>/bin/run_compact_now.sh\n",
        })
    try:
        dest.mkdir()
        (dest / "kb-gate.md").write_text(
            "bash /old/latch/bin/run_kb_gate.sh\n", encoding="utf-8")
        (dest / "kb-compact.md").write_text(
            "bash /old/latch/bin/run_compact_now.sh\n", encoding="utf-8")
        (dest / "kb-gate-report.md").write_text(
            "bash /old/latch/bin/latch_gate_report.sh\n", encoding="utf-8")
        level, changes = ie.install_commands(dry_run=False)
        _assert(level == "OK", f"expected OK, got {level}")
        gate_body = (dest / "kb-gate.md").read_text(encoding="utf-8")
        compact_body = (dest / "kb-compact.md").read_text(encoding="utf-8")
        report_body = (dest / "kb-gate-report.md").read_text(encoding="utf-8")
        _assert("/new/latch/bin/run_kb_gate.sh" in gate_body,
                f"legacy gate alias should keep legacy wrapper path: {gate_body!r}")
        _assert("/new/latch/bin/run_latch_gate.sh" not in gate_body,
                f"legacy gate alias must not require new wrapper permission: {gate_body!r}")
        _assert("/new/latch/bin/run_compact_now.sh" in compact_body,
                f"non-gate legacy alias should still refresh to primary body: {compact_body!r}")
        _assert("/new/latch/bin/latch_gate_report.sh" in report_body,
                f"newer legacy report alias should refresh to primary body: {report_body!r}")
        _assert(any("updated legacy alias kb-gate.md" in c for c in changes), changes)
        _assert(any("updated legacy alias kb-gate-report.md" in c for c in changes), changes)
        print("PASS install_commands_updates_existing_legacy_aliases")
    finally:
        restore()


def test_install_commands_preserves_user_owned_legacy_aliases():
    _src, dest, restore = _tmp_commands_env(
        "/new/latch", {"latch-gate.md": "bash <KB_HOME>/bin/run_latch_gate.sh\n"})
    try:
        dest.mkdir()
        custom = "do something unrelated\n"
        (dest / "kb-gate.md").write_text(custom, encoding="utf-8")
        (dest / "kb-project-direction.md").write_text(custom, encoding="utf-8")
        level, changes = ie.install_commands(dry_run=False)
        _assert(level == "OK", f"expected OK, got {level}")
        _assert((dest / "kb-gate.md").read_text(encoding="utf-8") == custom,
                "user-owned legacy alias must not be overwritten")
        _assert((dest / "kb-project-direction.md").read_text(encoding="utf-8") == custom,
                "user-owned stale legacy command must not be pruned")
        _assert(any("skipped legacy alias kb-gate.md" in c for c in changes), changes)
        _assert(any("skipped stale legacy command kb-project-direction.md" in c
                    for c in changes), changes)
        print("PASS install_commands_preserves_user_owned_legacy_aliases")
    finally:
        restore()


def test_install_commands_prunes_stale_latch_owned_commands():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch", {"latch-gate.md": "bash <KB_HOME>/bin/run_latch_gate.sh\n"})
    try:
        dest.mkdir()
        (dest / "kb-focus.md").write_text(
            "bash /opt/latch/bin/run_kb_focus.sh list\n", encoding="utf-8")
        level, changes = ie.install_commands(dry_run=False)
        _assert(level == "OK", f"expected OK, got {level}")
        _assert(not (dest / "kb-focus.md").exists(),
                "stale latch-owned kb-focus command should be pruned")
        _assert(any("removed stale legacy command kb-focus.md" in c for c in changes), changes)
        ok, label = ie.commands_status()
        _assert(ok, f"status should pass after stale command prune: {label}")
        print("PASS install_commands_prunes_stale_latch_owned_commands")
    finally:
        restore()


def test_commands_status_flags_stale_latch_owned_commands():
    _src, dest, restore = _tmp_commands_env(
        "/opt/latch", {"latch-gate.md": "bash <KB_HOME>/bin/run_latch_gate.sh\n"})
    try:
        ie.install_commands(dry_run=False)
        (dest / "kb-project-direction.md").write_text(
            "bash /opt/latch/bin/latch_direction.sh\n", encoding="utf-8")
        ok, label = ie.commands_status()
        _assert(not ok and "stale legacy" in label,
                f"status should flag stale latch-owned commands: {label}")
        print("PASS commands_status_flags_stale_latch_owned_commands")
    finally:
        restore()


def test_default_commands_hide_workstream_control_surfaces():
    command_names = {path.name for path in (ROOT / "commands").glob("*.md")}
    _assert("kb-focus.md" not in command_names,
            f"workstream focus should not be a default slash command: {command_names}")
    _assert("kb-project-direction.md" not in command_names,
            f"project direction should not be a default slash command: {command_names}")
    _assert(
            "latch-gate.md" in command_names
            and "latch-gate-report.md" in command_names
            and "latch-compact.md" in command_names,
            f"core commands should still be installed: {command_names}")
    _assert(
            "kb-gate.md" not in command_names
            and "kb-gate-report.md" not in command_names
            and "kb-compact.md" not in command_names,
            f"legacy aliases should not be fresh/default command sources: {command_names}")
    print("PASS default_commands_hide_workstream_control_surfaces")


def test_command_change_summary_separates_migration_actions():
    summary = ie._command_change_summary([
        "installed latch-gate.md",
        "installed latch-compact.md",
        "updated legacy alias kb-gate.md -> latch-gate.md",
        "removed stale legacy command kb-focus.md",
        "skipped legacy alias kb-tree.md (looks user-owned)",
    ])
    _assert("2 installed" in summary, summary)
    _assert("1 legacy alias(es) updated" in summary, summary)
    _assert("1 stale command(s) removed" in summary, summary)
    _assert("1 user-owned file(s) skipped" in summary, summary)
    print("PASS command_change_summary_separates_migration_actions")


def test_resolve_python_override_and_env():
    # explicit override that exists -> absolute resolved path
    _assert(ie.resolve_python(sys.executable) == str(Path(sys.executable).resolve()),
            "override to an existing path should resolve absolute")
    # Fresh env var is honored first; legacy env var remains supported.
    saved_latch = os.environ.get("LATCH_PYTHON")
    saved_legacy = os.environ.get("CLAUDE_KB_PYTHON")
    try:
        os.environ.pop("LATCH_PYTHON", None)
        os.environ["CLAUDE_KB_PYTHON"] = sys.executable
        _assert(ie.resolve_python(None) == str(Path(sys.executable).resolve()),
                "CLAUDE_KB_PYTHON should be honored")

        os.environ["LATCH_PYTHON"] = sys.executable
        _assert(ie.resolve_python(None) == str(Path(sys.executable).resolve()),
                "LATCH_PYTHON should be honored")
    finally:
        if saved_latch is None:
            os.environ.pop("LATCH_PYTHON", None)
        else:
            os.environ["LATCH_PYTHON"] = saved_latch
        if saved_legacy is None:
            os.environ.pop("CLAUDE_KB_PYTHON", None)
        else:
            os.environ["CLAUDE_KB_PYTHON"] = saved_legacy
    print("PASS resolve_python_override_and_env")


def test_seed_next_step_message_names_immediate_value_and_preview():
    msg = ie.seed_next_step_message(
        "bash bin/latch_seed.sh --lookback-days 14 --apply"
    )
    _assert("immediate judgment value from latch" in msg,
            "seed next-step copy should name immediate judgment value")
    _assert("bash bin/latch_seed.sh --lookback-days 14 --apply" in msg,
            "seed next-step copy should include the command")
    _assert("selected local Claude and/or Codex chats" in msg,
            "seed next-step copy should explain what gets read")
    _assert("uses LLM calls" in msg and "LLM-call budget guardrail" in msg,
            "seed next-step copy should make LLM use and budget guardrail clear")
    _assert("structured seed report" in msg and "staging evidence" in msg,
            "seed next-step copy should explain approval/authority")
    _assert("catch-demo command" in msg and "after you approve staging evidence" in msg,
            "seed next-step copy should name the post-apply rejected-path demo")
    _assert("repeats that proof command after a successful write" in msg,
            "seed next-step copy should name the post-write proof command")
    _assert("default 20" in msg and "--last-sessions N" in msg,
            "seed next-step copy should make the session cap configurable")
    _assert("harvest" not in msg.lower() and "glean" not in msg.lower(),
            "seed copy should avoid rejected naming")
    print("PASS seed_next_step_message_names_immediate_value_and_preview")


def test_seed_command_args_use_llm_apply_and_project():
    project = Path("/tmp/example project")
    args = ie.seed_command_args(python_path="/py", project=project, source="codex")
    _assert(args[:2] == ["/py", str(ie.KB_HOME / "src" / "seed.py")],
            f"seed command should run seed.py with chosen python: {args}")
    _assert("--project" in args and str(project) in args,
            f"seed command should target the project path: {args}")
    _assert("--source" in args and "codex" in args,
            f"seed command should preserve source selection: {args}")
    _assert("--llm" not in args,
            f"install-time seed should rely on default LLM-backed mode, not expose --llm: {args}")
    _assert("--apply" in args,
            f"install-time seed should preview then offer staging writes: {args}")
    formatted = ie.format_command(args)
    _assert("'/tmp/example project'" in formatted,
            f"formatted command should quote paths with spaces: {formatted}")
    print("PASS seed_command_args_use_llm_apply_and_project")


def test_offer_seed_after_install_noninteractive_does_not_run():
    calls = []
    old_tty = ie._stdio_is_tty
    old_run = ie.subprocess.run
    try:
        ie._stdio_is_tty = lambda: False
        ie.subprocess.run = lambda args: calls.append(args)
        ie.offer_seed_after_install(
            python_path="/py",
            source="auto",
            project=Path("/tmp/example-project"),
        )
        _assert(calls == [], f"noninteractive seed offer must not run subprocess: {calls}")
    finally:
        ie._stdio_is_tty = old_tty
        ie.subprocess.run = old_run
    print("PASS offer_seed_after_install_noninteractive_does_not_run")


def test_no_seed_prompt_prints_seed_handoff_unless_suppressed():
    original = {
        "SETTINGS_PATH": ie.SETTINGS_PATH,
        "find_claude": ie.find_claude,
        "apply_preflight_errors": ie.apply_preflight_errors,
        "install_commands": ie.install_commands,
        "pin_kb_dir": ie.pin_kb_dir,
    }
    settings = _tmp_settings({})
    try:
        ie.SETTINGS_PATH = settings
        ie.find_claude = lambda: None
        ie.apply_preflight_errors = lambda _claude: []
        ie.install_commands = lambda _dry_run: ("OK", ["command"])
        ie.pin_kb_dir = lambda _kb_dir, _dry_run: ("OK", "pinned")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ie.main(["--python", sys.executable, "--no-seed-prompt"])
        text = output.getvalue()
        _assert(rc == 0, f"installer should complete in patched test path, got {rc}:\n{text}")
        _assert(text.count("Seed latch from prior work") == 1,
                f"--no-seed-prompt should still print standalone seed handoff:\n{text}")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ie.main([
                "--python",
                sys.executable,
                "--no-seed-prompt",
                "--suppress-seed-output",
            ])
        text = output.getvalue()
        _assert(rc == 0, f"suppressed installer should complete, got {rc}:\n{text}")
        _assert("Seed latch from prior work" not in text,
                f"--suppress-seed-output should silence installer seed handoff:\n{text}")
    finally:
        for name, value in original.items():
            setattr(ie, name, value)
    print("PASS no_seed_prompt_prints_seed_handoff_unless_suppressed")


def test_apply_preflight_blocks_without_claude_cli():
    missing = ie.apply_preflight_errors(None)
    _assert(missing, "apply preflight should block when claude CLI is missing")
    _assert("Claude Code CLI" in missing[0],
            f"preflight should name the missing Claude Code CLI: {missing!r}")
    _assert("install_engine.sh" in " ".join(missing),
            f"preflight should tell the user how to recover: {missing!r}")
    _assert(ie.apply_preflight_errors("/usr/local/bin/claude") == [],
            "apply preflight should pass when claude CLI is present")
    print("PASS apply_preflight_blocks_without_claude_cli")


def test_restart_next_step_message_names_vscode_and_claude_code():
    msg = ie.restart_next_step_message()
    _assert("Restart VS Code" in msg,
            "restart message should help VS Code users know what to restart")
    _assert("Claude Code" in msg,
            "restart message should still cover non-VS Code Claude Code users")
    _assert("MCP roster" in msg and "latch_* tools" in msg,
            "restart message should explain why restart matters")
    print("PASS restart_next_step_message_names_vscode_and_claude_code")


def test_posttooluse_hook_wired_with_matcher_and_preserves_others():
    # The deterministic activity surface must be a managed event, carry both
    # latch and legacy claude-kb matchers, point at post_tool_use.py, and not clobber a user's
    # own PostToolUse hook.
    _assert("PostToolUse" in ie.MANAGED_EVENTS, "PostToolUse must be a managed event")

    snippet = ie.load_snippet("/PY")
    ptu_entries = snippet["hooks"]["PostToolUse"]
    _assert(len(ptu_entries) == 2, "snippet should define primary + legacy PostToolUse entries")
    matchers = {entry.get("matcher") for entry in ptu_entries}
    _assert(matchers == {"mcp__latch__.*", "mcp__claude-kb__.*"},
            f"PostToolUse must cover primary and legacy KB MCP tools, got {matchers!r}")
    for entry in ptu_entries:
        cmd = entry["hooks"][0]["command"]
        _assert("post_tool_use.py" in cmd and "/PY" in cmd,
                f"PostToolUse command should run post_tool_use.py under /PY, got {cmd!r}")

    existing = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "audit.sh"}]},
            ],
        },
    }
    old = _with_settings_path(_tmp_settings(existing))
    try:
        new, _ = ie.merge_settings(ie.load_snippet("/PY"))
        ptu_hooks = new["hooks"]["PostToolUse"]
        latch = [e for e in ptu_hooks if ie._is_latch_hook_entry(e)]
        non_latch = [e for e in ptu_hooks if not ie._is_latch_hook_entry(e)]
        _assert(len(latch) == 2,
                f"expected primary + legacy latch PostToolUse hooks, got {len(latch)}")
        _assert(any(e["hooks"][0]["command"] == "audit.sh" for e in non_latch),
                "user's own PostToolUse hook must be preserved")
        print("PASS posttooluse_hook_wired_with_matcher_and_preserves_others")
    finally:
        _with_settings_path(old)


if __name__ == "__main__":
    test_is_latch_hook_entry()
    test_load_snippet_substitutes_and_drops_mcpservers()
    test_load_snippet_normalizes_windows_python_path()
    test_merge_preserves_others_adds_perm_removes_dead_block()
    test_mcp_status_accepts_legacy_registration()
    test_register_mcp_fresh_install_uses_latch_name()
    test_merge_idempotent()
    test_posttooluse_hook_wired_with_matcher_and_preserves_others()
    test_write_settings_backs_up_existing()
    test_install_commands_copies_and_substitutes()
    test_install_commands_dry_run_writes_nothing()
    test_commands_status_missing_then_present_then_unresolved()
    test_install_commands_updates_existing_legacy_aliases()
    test_install_commands_preserves_user_owned_legacy_aliases()
    test_install_commands_prunes_stale_latch_owned_commands()
    test_commands_status_flags_stale_latch_owned_commands()
    test_default_commands_hide_workstream_control_surfaces()
    test_command_change_summary_separates_migration_actions()
    test_resolve_python_override_and_env()
    test_seed_next_step_message_names_immediate_value_and_preview()
    test_seed_command_args_use_llm_apply_and_project()
    test_offer_seed_after_install_noninteractive_does_not_run()
    test_no_seed_prompt_prints_seed_handoff_unless_suppressed()
    test_apply_preflight_blocks_without_claude_cli()
    test_restart_next_step_message_names_vscode_and_claude_code()
    print("\nAll install_engine tests pass.")
