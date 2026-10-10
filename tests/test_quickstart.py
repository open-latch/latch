"""Unit tests for the first-run guided quickstart orchestrator."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.install import quickstart as qs  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _cmd_text(step: qs.Step) -> str:
    return " ".join(step.command)


def test_build_steps_for_both_delegates_to_existing_installers():
    project = Path("/tmp/example-project")
    steps = qs.build_install_steps(
        agents=("claude", "codex"),
        python_path="/py",
        project=project,
    )
    texts = [_cmd_text(step) for step in steps]

    _assert(any("install_engine.py" in text
                and "--no-seed-prompt" in text
                and "--suppress-seed-output" in text for text in texts),
            f"Claude Code engine install should delegate quietly to install_engine: {texts}")
    _assert(any("claude_md_sync.py" in text and "--yes" in text
                and str(project / "CLAUDE.md") in text for text in texts),
            f"Claude Code project contract should sync CLAUDE.md: {texts}")
    _assert(any("install_codex.py" in text and "--agents-md" in text
                and str(project / "AGENTS.md") in text
                and "--no-seed-prompt" in text
                and "--suppress-seed-output" in text for text in texts),
            f"Codex install should delegate quietly to install_codex: {texts}")
    print("PASS build_steps_for_both_delegates_to_existing_installers")


def test_build_steps_for_cursor_delegates_to_cursor_installer():
    project = Path("/tmp/example-project")
    steps = qs.build_install_steps(
        agents=("cursor",),
        python_path="/py",
        project=project,
        cursor_model_backend="codex",
        cursor_with_hooks=True,
    )
    texts = [_cmd_text(step) for step in steps]

    _assert(len(steps) == 1, steps)
    _assert(any("install_cursor.py" in text
                and "--agents-md" in text
                and str(project / "AGENTS.md") in text
                and "--model-backend codex" in text
                and "--with-hooks" in text for text in texts),
            f"Cursor quickstart should delegate to install_cursor with backend: {texts}")
    print("PASS build_steps_for_cursor_delegates_to_cursor_installer")


def test_build_doctor_steps_cover_selected_surfaces():
    project = Path("/tmp/example-project")
    steps = qs.build_doctor_steps(
        agents=("claude", "codex"),
        python_path="/py",
        project=project,
    )
    texts = [_cmd_text(step) for step in steps]

    _assert(any("install_engine.py" in text and "--check" in text for text in texts),
            f"Claude Code --check missing: {texts}")
    _assert(any("doctor.py" in text for text in texts),
            f"latch doctor missing: {texts}")
    _assert(any("install_codex.py" in text and "--check" in text
                and str(project / "AGENTS.md") in text for text in texts),
            f"Codex --check missing: {texts}")
    _assert(any("codex_doctor.py" in text and "--skip-compact" in text
                and "--skip-summarizer" in text for text in texts),
            f"Codex doctor should default to static install checks: {texts}")
    print("PASS build_doctor_steps_cover_selected_surfaces")


def test_build_doctor_steps_cover_cursor_surface():
    project = Path("/tmp/example-project")
    steps = qs.build_doctor_steps(
        agents=("cursor",),
        python_path="/py",
        project=project,
        cursor_model_backend="claude",
        cursor_with_hooks=True,
    )
    texts = [_cmd_text(step) for step in steps]
    _assert(any("install_cursor.py" in text and "--check" in text
                and "--model-backend claude" in text
                and "--with-hooks" in text for text in texts),
            f"Cursor install --check missing: {texts}")
    _assert(any("cursor_doctor.py" in text
                and "--model-backend claude" in text
                and "--with-hooks" in text for text in texts),
            f"Cursor doctor missing: {texts}")
    print("PASS build_doctor_steps_cover_cursor_surface")


def test_seed_source_follows_agents_by_default():
    _assert(qs.seed_source_for_agents(("claude",), "auto") == "claude",
            "Claude-only quickstart should seed Claude transcripts by default")
    _assert(qs.seed_source_for_agents(("codex",), "auto") == "codex",
            "Codex-only quickstart should seed Codex transcripts by default")
    _assert(qs.seed_source_for_agents(("claude", "codex"), "auto") == "both",
            "Both-agent quickstart should seed both transcript sources by default")
    _assert(qs.seed_source_for_agents(("cursor",), "auto") == "both",
            "Cursor-only quickstart should seed existing Claude/Codex transcripts by default")
    _assert(qs.seed_source_for_agents(("claude", "codex", "cursor"), "auto") == "both",
            "All-agent quickstart should seed both supported transcript sources")
    _assert(qs.seed_source_for_agents(("claude",), "both") == "both",
            "Explicit seed source should win")
    _assert(qs.seed_source_for_agents(("cursor",), "cursor") == "cursor",
            "Explicit Cursor-origin seed source should win")
    _assert(
        qs.seed_source_for_agents(
            ("cursor",), "auto", cursor_history=True,
        ) == "all",
        "Cursor history opt-in should add Cursor without dropping existing sources",
    )
    _assert(
        qs.seed_source_for_agents(
            ("claude", "codex", "cursor"), "auto", cursor_history=True,
        ) == "all",
        "all-agent Cursor history opt-in should retain every selected provider",
    )
    print("PASS seed_source_follows_agents_by_default")


def test_seed_backend_follows_selected_agent_surface():
    _assert(qs.seed_backend_for_agents(("claude",)) == "claude",
            "Claude-only quickstart should refine with Claude")
    _assert(qs.seed_backend_for_agents(("codex",)) == "codex",
            "Codex-only quickstart should refine with Codex")
    _assert(qs.seed_backend_for_agents(("cursor",)) == "cursor",
            "Cursor-only quickstart should refine with Cursor by default")
    _assert(qs.seed_backend_for_agents(
                ("cursor",), cursor_model_backend="codex",
            ) == "codex",
            "Cursor compatibility backend should also refine the initial KB")
    _assert(qs.seed_backend_for_agents(("claude", "codex")) == "claude",
            "mixed Claude/Codex installs should preserve the Claude default")
    _assert(qs.seed_backend_for_agents(("cursor", "codex", "claude")) == "claude",
            "backend precedence must not depend on sequence order")
    print("PASS seed_backend_follows_selected_agent_surface")


def test_seed_command_includes_project_source_sessions_and_apply():
    project = Path("/tmp/example project")
    args = qs.seed_command_args(
        python_path="/py",
        project=project,
        source="both",
        backend="codex",
        last_sessions=50,
    )
    _assert(args[:2] == ["/py", str(qs.KB_HOME / "src" / "latch" / "pipeline" / "seed.py")], args)
    _assert("--project" in args and str(project) in args, args)
    _assert("--source" in args and "both" in args, args)
    _assert(args.count("--backend") == 1 and "codex" in args, args)
    _assert("--lookback-days" in args and "90" in args, args)
    _assert("--last-sessions" in args and "50" in args, args)
    _assert("--apply" in args, args)
    formatted = qs.format_command(args)
    _assert("'/tmp/example project'" in formatted,
            f"formatted command should quote project paths with spaces: {formatted}")
    print("PASS seed_command_includes_project_source_sessions_and_apply")


def test_seed_command_propagates_cursor_history_only_when_opted_in():
    base = qs.seed_command_args(
        python_path="/py",
        project=Path("/tmp/example-project"),
        source="cursor",
        backend="cursor",
    )
    opted_in = qs.seed_command_args(
        python_path="/py",
        project=Path("/tmp/example-project"),
        source="cursor",
        backend="cursor",
        cursor_history=True,
    )
    _assert("--cursor-history" not in base, base)
    _assert(opted_in[-1] == "--cursor-history", opted_in)
    parsed = qs.parse_args([
        "--agents", "cursor",
        "--project", "/tmp/example-project",
        "--cursor-history",
    ])
    defaulted = qs.parse_args([
        "--agents", "cursor",
        "--project", "/tmp/example-project",
    ])
    _assert(parsed.cursor_history is True, parsed)
    _assert(defaulted.cursor_history is False, defaulted)
    print("PASS seed_command_propagates_cursor_history_only_when_opted_in")


def test_initial_kb_defaults_and_dry_run_plan_are_explicit():
    args = qs.parse_args(["--agents", "codex", "--project", "/tmp/example-project"])
    _assert(args.lookback_days == 90 and args.last_sessions == 50,
            f"unexpected initial-KB defaults: {args}")

    command = qs.seed_command_args(
        python_path="/py",
        project=Path("/tmp/example-project"),
        source="codex",
        backend="codex",
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        qs.print_plan([], command)
    text = output.getvalue()
    _assert("Build the initial decision KB (review before staging writes)" in text,
            f"dry-run plan should make initial-KB authority boundaries explicit:\n{text}")
    _assert("--lookback-days 90" in text and "--last-sessions 50" in text,
            f"dry-run plan should expose the acquisition bounds:\n{text}")
    _assert("--source codex --backend codex" in text,
            f"dry-run plan should pin source and model backend independently:\n{text}")
    print("PASS initial_kb_defaults_and_dry_run_plan_are_explicit")


def test_dry_run_reports_unresolved_maintenance_cli_without_stopping(
    monkeypatch,
    tmp_path,
    capsys,
):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        qs,
        "agent_preflight_errors",
        lambda *_args, **_kwargs: ["Codex CLI (`codex`) is not on PATH"],
    )
    monkeypatch.setattr(
        qs.paths,
        "resolve_maintenance_executable",
        lambda _backend: (_ for _ in ()).throw(
            ValueError("could not resolve Codex")
        ),
    )
    monkeypatch.setattr(
        qs,
        "pin_kb_for_quickstart",
        lambda *_args, **_kwargs: ("DRY", "would pin"),
    )

    rc = qs.main([
        "--agents", "codex",
        "--project", str(project),
        "--python", sys.executable,
        "--dry-run",
    ])

    output = capsys.readouterr()
    _assert(rc == 0, f"dry-run should remain available: {output}")
    _assert("<unresolved: could not resolve Codex>" in output.out, output.out)
    _assert("warning: Codex CLI" in output.out, output.out)


def test_resolve_agents_requires_choice_noninteractive_even_with_context():
    try:
        qs.resolve_agents(
            "auto",
            env={"CODEX_THREAD_ID": "sid"},
            is_tty=False,
        )
    except ValueError as exc:
        _assert("--agents all" in str(exc) and "Detected current surface: codex" in str(exc),
                f"unexpected error: {exc}")
    else:
        raise AssertionError("non-interactive auto mode should require explicit --agents")
    print("PASS resolve_agents_requires_choice_noninteractive_even_with_context")


def test_resolve_agents_uses_detected_default_when_interactive():
    agents = qs.resolve_agents(
        "auto",
        env={"CODEX_THREAD_ID": "sid"},
        is_tty=True,
        input_fn=lambda _prompt: "",
    )
    _assert(agents == ("codex",), f"expected Codex default, got {agents}")
    print("PASS resolve_agents_uses_detected_default_when_interactive")


def test_resolve_agents_accepts_cursor_and_all():
    _assert(qs.normalize_agents("cursor") == ("cursor",), "cursor should be selectable")
    _assert(qs.normalize_agents("all") == ("claude", "codex", "cursor"),
            "all should include Claude, Codex, and Cursor")
    print("PASS resolve_agents_accepts_cursor_and_all")


def test_agent_preflight_reports_every_missing_selected_cli():
    available = {"codex": "/usr/local/bin/codex"}
    errors = qs.agent_preflight_errors(
        ("claude", "codex", "cursor"),
        which=lambda command: available.get(command),
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    _assert(len(errors) == 2, f"expected Claude and Cursor errors, got {errors}")
    _assert(any("Claude Code CLI" in error for error in errors), errors)
    _assert(any("Cursor Agent CLI" in error for error in errors), errors)
    _assert(not any("Codex CLI" in error for error in errors), errors)
    print("PASS agent_preflight_reports_every_missing_selected_cli")


def test_agent_preflight_accepts_cursor_agent_alias():
    errors = qs.agent_preflight_errors(
        ("cursor",),
        which=lambda command: "/bin/cursor-agent" if command == "cursor-agent" else None,
    )
    _assert(errors == [], f"cursor-agent alias should satisfy preflight: {errors}")
    print("PASS agent_preflight_accepts_cursor_agent_alias")


def test_cursor_compatibility_backend_is_preflighted():
    available = {"agent": "/bin/agent"}
    errors = qs.agent_preflight_errors(
        ("cursor",),
        cursor_model_backend="codex",
        which=lambda command: available.get(command),
        run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    _assert(any("Codex CLI" in error for error in errors), errors)
    _assert(
        any("Cursor model backend" in error for error in errors),
        f"missing Codex should name why Cursor needs it: {errors}",
    )
    available["codex"] = "/bin/codex"
    _assert(qs.agent_preflight_errors(
                ("cursor",),
                cursor_model_backend="codex",
                which=lambda command: available.get(command),
                run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
            ) == [],
            "available Cursor and compatibility CLIs should pass preflight")
    print("PASS cursor_compatibility_backend_is_preflighted")


def test_codex_preflight_runs_absolute_version_probe():
    root = Path(tempfile.mkdtemp(prefix="latch-codex-preflight-"))
    candidate = root / "bin" / ".." / "codex.exe"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="codex-cli 1.0\n")

    try:
        errors = qs.agent_preflight_errors(
            ("codex",),
            which=lambda _command: str(candidate),
            run=fake_run,
            platform_name="posix",
        )
        _assert(errors == [], errors)
        _assert(len(calls) == 1, calls)
        command, kwargs = calls[0]
        _assert(command == [str(candidate.resolve()), "--version"], command)
        _assert(kwargs["timeout"] == qs.CODEX_VERSION_PROBE_TIMEOUT_SECONDS, kwargs)
        _assert(kwargs["capture_output"] is True and kwargs["text"] is True, kwargs)
        _assert(kwargs["check"] is False, kwargs)
        _assert("creationflags" not in kwargs, kwargs)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS codex_preflight_runs_absolute_version_probe")


def test_codex_preflight_rejects_windows_access_denied_with_install_guidance():
    candidate = str(Path(tempfile.gettempdir()) / "WindowsApps" / "codex.exe")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def access_denied(command, **kwargs):
        calls.append((command, kwargs))
        raise PermissionError(5, "Access is denied")

    errors = qs.agent_preflight_errors(
        ("codex",),
        which=lambda _command: candidate,
        run=access_denied,
        platform_name="nt",
    )

    _assert(len(errors) == 1, errors)
    _assert("access denied" in errors[0].lower(), errors)
    _assert("standalone Codex CLI" in errors[0], errors)
    _assert("https://chatgpt.com/codex/install.ps1" in errors[0], errors)
    _assert(calls[0][0] == [str(Path(candidate).resolve()), "--version"], calls)
    _assert(
        calls[0][1]["creationflags"] == qs.WINDOWS_CREATE_NO_WINDOW,
        calls,
    )
    print("PASS codex_preflight_rejects_windows_access_denied_with_install_guidance")


def test_codex_preflight_reports_timeout_oserror_and_nonzero():
    candidate = str(Path(tempfile.gettempdir()) / "codex")

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    cases = [
        (timeout, "timed out"),
        (
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("launch failed")
            ),
            "could not be launched",
        ),
        (
            lambda command, **_kwargs: subprocess.CompletedProcess(command, 23),
            "status 23",
        ),
    ]
    for fake_run, expected in cases:
        errors = qs.agent_preflight_errors(
            ("codex",),
            which=lambda _command: candidate,
            run=fake_run,
            platform_name="posix",
        )
        _assert(len(errors) == 1 and expected in errors[0], errors)
    print("PASS codex_preflight_reports_timeout_oserror_and_nonzero")


def test_resolve_agents_requires_choice_without_prompt_or_context():
    try:
        qs.resolve_agents("auto", env={}, is_tty=False)
    except ValueError as exc:
        _assert("--agents claude" in str(exc), f"unexpected error: {exc}")
    else:
        raise AssertionError("non-interactive auto mode without context should fail")
    print("PASS resolve_agents_requires_choice_without_prompt_or_context")


def test_run_steps_stops_before_later_steps_on_failure():
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int):
            self.returncode = returncode

    def fake_run(command, cwd=None):
        calls.append(list(command))
        return Result(3)

    steps = [
        qs.Step("first", ["one"], Path("/tmp")),
        qs.Step("second", ["two"], Path("/tmp")),
    ]
    rc = qs.run_steps(steps, run=fake_run)
    _assert(rc == 3, f"expected failure rc, got {rc}")
    _assert(calls == [["one"]], f"quickstart should stop at first failure: {calls}")
    print("PASS run_steps_stops_before_later_steps_on_failure")


def test_quickstart_seed_handoff_prints_once_noninteractive():
    old_tty = qs._stdio_is_tty
    output = io.StringIO()
    try:
        qs._stdio_is_tty = lambda: False
        with contextlib.redirect_stdout(output):
            qs.offer_seed_after_quickstart(
                python_path="/py",
                project=Path("/tmp/example-project"),
                source="both",
                backend="claude",
                last_sessions=50,
                run=lambda _command: None,
            )
    finally:
        qs._stdio_is_tty = old_tty
    text = output.getvalue()
    _assert(text.count("Build latch's initial decision KB") == 1,
            f"quickstart should emit one final seed handoff, got:\n{text}")
    _assert("quickstart wiring is complete, but the initial KB is pending" in text,
            f"noninteractive quickstart should not run seed automatically:\n{text}")
    print("PASS quickstart_seed_handoff_prints_once_noninteractive")


def test_quickstart_pin_uses_explicit_or_environment_override():
    original_pin = qs.install_engine.pin_kb_dir
    saved_latch = os.environ.get("LATCH_KB_DIR")
    saved_legacy = os.environ.get("CLAUDE_KB_DIR")
    calls: list[tuple[str | None, bool]] = []
    try:
        qs.install_engine.pin_kb_dir = lambda value, dry: (
            calls.append((value, dry)) or ("OK", "pinned")
        )
        os.environ["LATCH_KB_DIR"] = "/env/latch-kb"
        os.environ["CLAUDE_KB_DIR"] = "/env/legacy-kb"
        _assert(qs.pin_kb_for_quickstart(None, dry_run=False) == ("OK", "pinned"),
                "pin helper should forward the installer result")
        _assert(qs.pin_kb_for_quickstart("/explicit/kb", dry_run=True) ==
                ("OK", "pinned"), "explicit pin should be accepted")
        _assert(qs.pin_kb_for_quickstart("", dry_run=False) == ("OK", "pinned"),
                "explicit empty input must reach the installer's fail-closed validator")
        _assert(calls == [
            ("/env/latch-kb", False),
            ("/explicit/kb", True),
            ("", False),
        ], calls)
    finally:
        qs.install_engine.pin_kb_dir = original_pin
        if saved_latch is None:
            os.environ.pop("LATCH_KB_DIR", None)
        else:
            os.environ["LATCH_KB_DIR"] = saved_latch
        if saved_legacy is None:
            os.environ.pop("CLAUDE_KB_DIR", None)
        else:
            os.environ["CLAUDE_KB_DIR"] = saved_legacy
    print("PASS quickstart_pin_uses_explicit_or_environment_override")


def test_quickstart_persists_transient_env_pin_for_later_processes():
    root = Path(tempfile.mkdtemp(prefix="latch-quickstart-durable-pin-"))
    target = (
        qs.paths.validated_test_root()
        / "vaults"
        / f"quickstart-durable-{root.name}"
    )
    seed_cwd = root / "seed cwd"
    mcp_cwd = root / "different mcp cwd"
    seed_cwd.mkdir()
    mcp_cwd.mkdir()
    try:
        env = os.environ.copy()
        env.update({
            "LATCH_HOME": str(root),
            "LATCH_KB_DIR": str(target),
            "PYTHONPATH": str(next(p for p in Path(qs.__file__).resolve().parents if p.name == "src")),
        })
        env.pop("CLAUDE_KB_DIR", None)
        pin = subprocess.run(
            [sys.executable, "-c", (
                "from latch.install import quickstart; "
                "level, message = quickstart.pin_kb_for_quickstart(None, dry_run=False); "
                "print(level, message)"
            )],
            cwd=seed_cwd,
            env=env,
            text=True,
            capture_output=True,
        )
        _assert(pin.returncode == 0 and pin.stdout.startswith("OK "),
                f"quickstart subprocess should persist its effective target: {pin.stderr}")

        # A later Codex/MCP process starts elsewhere without the bootstrap env.
        env.pop("LATCH_KB_DIR", None)
        env.pop(qs.paths.TEST_ROOT_ENV, None)
        env.pop(qs.paths.TEST_CAPABILITY_ENV, None)
        later_db = subprocess.check_output(
            [sys.executable, "-c", "from latch.store import paths; print(paths.db_path())"],
            cwd=mcp_cwd,
            env=env,
            text=True,
        ).strip()
        expected_db = str(target / "kb.db")
        _assert(later_db == expected_db,
                f"later MCP must use the persisted seed target: "
                f"got={later_db}, expected={expected_db}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS quickstart_persists_transient_env_pin_for_later_processes")


def test_quickstart_pins_after_preflight_before_wiring():
    root = Path(tempfile.mkdtemp(prefix="latch-quickstart-pin-order-"))
    project = root / "project"
    project.mkdir()
    original = {
        "resolve_python": qs.install_engine.resolve_python,
        "agent_preflight_errors": qs.agent_preflight_errors,
        "pin_kb_for_quickstart": qs.pin_kb_for_quickstart,
        "build_install_steps": qs.build_install_steps,
        "build_doctor_steps": qs.build_doctor_steps,
        "run_steps": qs.run_steps,
        "resolve_maintenance_executable": qs.paths.resolve_maintenance_executable,
        "write_maintenance_runner": qs.paths.write_maintenance_runner,
        "refresh_pinned_dir": qs.paths.refresh_pinned_dir,
    }
    events: list[tuple[str, object]] = []
    try:
        qs.install_engine.resolve_python = lambda _value: "/py"
        qs.agent_preflight_errors = lambda *_args, **_kwargs: []
        qs.paths.resolve_maintenance_executable = lambda _backend: "/bin/codex"
        qs.paths.write_maintenance_runner = lambda **kwargs: (
            events.append(("runtime_settings", kwargs["project_path"]))
            or (root / "runtime_settings.json")
        )
        qs.pin_kb_for_quickstart = lambda value, *, dry_run: (
            events.append(("pin", (value, dry_run))) or ("OK", "pinned")
        )
        qs.paths.refresh_pinned_dir = lambda: events.append(("refresh", None))
        qs.build_install_steps = lambda **_kwargs: [
            qs.Step("wire", ["wire"], project),
        ]
        qs.build_doctor_steps = lambda **_kwargs: []
        qs.run_steps = lambda steps, **_kwargs: (
            events.append(("run", [step.label for step in steps])) or 0
        )
        rc = qs.main([
            "--agents", "codex",
            "--project", str(project),
            "--kb-dir", str(root / "isolated kb"),
            "--skip-doctor",
            "--no-seed",
        ])
        _assert(rc == 0, f"quickstart should complete, got {rc}")
        _assert(events[:4] == [
            ("pin", (str(root / "isolated kb"), False)),
            ("refresh", None),
            ("runtime_settings", project.resolve()),
            ("run", ["wire"]),
        ], f"runtime settings must be written after pinning and before wiring: {events}")
    finally:
        qs.install_engine.resolve_python = original["resolve_python"]
        qs.agent_preflight_errors = original["agent_preflight_errors"]
        qs.pin_kb_for_quickstart = original["pin_kb_for_quickstart"]
        qs.build_install_steps = original["build_install_steps"]
        qs.build_doctor_steps = original["build_doctor_steps"]
        qs.run_steps = original["run_steps"]
        qs.paths.resolve_maintenance_executable = original[
            "resolve_maintenance_executable"
        ]
        qs.paths.write_maintenance_runner = original[
            "write_maintenance_runner"
        ]
        qs.paths.refresh_pinned_dir = original["refresh_pinned_dir"]
        shutil.rmtree(root, ignore_errors=True)
    print("PASS quickstart_pins_after_preflight_before_wiring")


def test_quickstart_preflight_failure_does_not_pin():
    root = Path(tempfile.mkdtemp(prefix="latch-quickstart-preflight-pin-"))
    project = root / "project"
    project.mkdir()
    original_resolve = qs.install_engine.resolve_python
    original_preflight = qs.agent_preflight_errors
    original_pin = qs.pin_kb_for_quickstart
    called: list[bool] = []
    try:
        qs.install_engine.resolve_python = lambda _value: "/py"
        qs.agent_preflight_errors = lambda *_args, **_kwargs: ["Codex CLI missing"]
        qs.pin_kb_for_quickstart = lambda *_args, **_kwargs: (
            called.append(True) or ("OK", "pinned")
        )
        rc = qs.main(["--agents", "codex", "--project", str(project)])
        _assert(rc == 2, f"preflight should fail with status 2, got {rc}")
        _assert(called == [], "preflight failure must happen before KB pin writes")
    finally:
        qs.install_engine.resolve_python = original_resolve
        qs.agent_preflight_errors = original_preflight
        qs.pin_kb_for_quickstart = original_pin
        shutil.rmtree(root, ignore_errors=True)
    print("PASS quickstart_preflight_failure_does_not_pin")


def test_quickstart_pin_conflict_stops_before_wiring():
    root = Path(tempfile.mkdtemp(prefix="latch-quickstart-pin-conflict-"))
    project = root / "project"
    project.mkdir()
    original = {
        "resolve_python": qs.install_engine.resolve_python,
        "agent_preflight_errors": qs.agent_preflight_errors,
        "pin_kb_for_quickstart": qs.pin_kb_for_quickstart,
        "resolve_maintenance_executable": qs.paths.resolve_maintenance_executable,
        "run_steps": qs.run_steps,
    }
    ran: list[bool] = []
    try:
        qs.install_engine.resolve_python = lambda _value: "/py"
        qs.agent_preflight_errors = lambda *_args, **_kwargs: []
        qs.paths.resolve_maintenance_executable = lambda _backend: "/bin/codex"
        qs.run_steps = lambda *_args, **_kwargs: ran.append(True) or 0
        for level in ("ERROR", "FAIL"):
            qs.pin_kb_for_quickstart = lambda *_args, level=level, **_kwargs: (
                level, "effective target is unsafe or conflicts with existing pin"
            )
            rc = qs.main([
                "--agents", "codex",
                "--project", str(project),
                "--skip-doctor",
                "--no-seed",
            ])
            _assert(rc == 2, f"{level} pin should fail with status 2, got {rc}")
            _assert(
                ran == [],
                f"{level} pin must stop before installer subprocesses",
            )
    finally:
        qs.install_engine.resolve_python = original["resolve_python"]
        qs.agent_preflight_errors = original["agent_preflight_errors"]
        qs.pin_kb_for_quickstart = original["pin_kb_for_quickstart"]
        qs.paths.resolve_maintenance_executable = original[
            "resolve_maintenance_executable"
        ]
        qs.run_steps = original["run_steps"]
        shutil.rmtree(root, ignore_errors=True)
    print("PASS quickstart_pin_conflict_stops_before_wiring")


if __name__ == "__main__":
    test_build_steps_for_both_delegates_to_existing_installers()
    test_build_steps_for_cursor_delegates_to_cursor_installer()
    test_build_doctor_steps_cover_selected_surfaces()
    test_build_doctor_steps_cover_cursor_surface()
    test_seed_source_follows_agents_by_default()
    test_seed_backend_follows_selected_agent_surface()
    test_seed_command_includes_project_source_sessions_and_apply()
    test_seed_command_propagates_cursor_history_only_when_opted_in()
    test_initial_kb_defaults_and_dry_run_plan_are_explicit()
    test_resolve_agents_requires_choice_noninteractive_even_with_context()
    test_resolve_agents_uses_detected_default_when_interactive()
    test_resolve_agents_accepts_cursor_and_all()
    test_agent_preflight_reports_every_missing_selected_cli()
    test_agent_preflight_accepts_cursor_agent_alias()
    test_cursor_compatibility_backend_is_preflighted()
    test_codex_preflight_runs_absolute_version_probe()
    test_codex_preflight_rejects_windows_access_denied_with_install_guidance()
    test_codex_preflight_reports_timeout_oserror_and_nonzero()
    test_resolve_agents_requires_choice_without_prompt_or_context()
    test_run_steps_stops_before_later_steps_on_failure()
    test_quickstart_seed_handoff_prints_once_noninteractive()
    test_quickstart_pin_uses_explicit_or_environment_override()
    test_quickstart_persists_transient_env_pin_for_later_processes()
    test_quickstart_pins_after_preflight_before_wiring()
    test_quickstart_preflight_failure_does_not_pin()
    test_quickstart_pin_conflict_stops_before_wiring()
    print("\nAll quickstart tests pass.")
