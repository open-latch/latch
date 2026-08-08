"""Unit tests for the first-run guided quickstart orchestrator."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import quickstart as qs  # noqa: E402


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
    _assert(args[:2] == ["/py", str(qs.KB_HOME / "src" / "seed.py")], args)
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
        "--scope", "shared",
        "--enable-project-scopes",
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
    )
    _assert(any("Codex CLI" in error for error in errors), errors)
    available["codex"] = "/bin/codex"
    _assert(qs.agent_preflight_errors(
                ("cursor",),
                cursor_model_backend="codex",
                which=lambda command: available.get(command),
            ) == [],
            "available Cursor and compatibility CLIs should pass preflight")
    print("PASS cursor_compatibility_backend_is_preflighted")


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


def test_quickstart_persists_transient_env_pin_for_global_shared_access():
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
            "PYTHONPATH": str(Path(qs.__file__).resolve().parent),
        })
        env.pop("CLAUDE_KB_DIR", None)
        pin = subprocess.run(
            [sys.executable, "-c", (
                "import quickstart; "
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
        persisted = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "import json, paths; "
                    "print(json.loads(paths.KB_LOCATION_FILE.read_text())['kb_dir'])"
                ),
            ],
            cwd=mcp_cwd,
            env=env,
            text=True,
        ).strip()
        _assert(
            persisted == str(target),
            f"later process must see the persisted target: {persisted}",
        )
        resolved = subprocess.run(
            [sys.executable, "-c", "import paths; print(paths.db_path())"],
            cwd=mcp_cwd,
            env=env,
            text=True,
            capture_output=True,
        )
        _assert(resolved.returncode == 0, resolved.stderr)
        _assert(
            resolved.stdout.strip() == str(target / "kb.db"),
            "ordinary Shared mode no longer followed its installed global pin",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS quickstart_persists_transient_env_pin_for_global_shared_access")


def test_quickstart_pins_after_preflight_before_wiring():
    root = Path(tempfile.mkdtemp(prefix="latch-quickstart-pin-order-"))
    project = root / "project"
    project.mkdir()
    original = {
        "resolve_python": qs.install_engine.resolve_python,
        "agent_preflight_errors": qs.agent_preflight_errors,
        "pin_kb_for_quickstart": qs.pin_kb_for_quickstart,
        "scope_policy_for_install": qs.install_engine.scope_policy_for_install,
        "configure_scope_policy": qs.install_engine.configure_scope_policy,
        "build_install_steps": qs.build_install_steps,
        "build_doctor_steps": qs.build_doctor_steps,
        "run_steps": qs.run_steps,
        "resolve_maintenance_executable": qs.paths.resolve_maintenance_executable,
        "write_maintenance_runner": qs.paths.write_maintenance_runner,
        "refresh_pinned_dir": qs.paths.refresh_pinned_dir,
        "apply_latch": qs.project_mode.apply_latch,
        "require_latched": qs.project_config.require_latched,
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
        qs.install_engine.scope_policy_for_install = lambda **kwargs: (
            events.append(("plan_policy", kwargs))
            or qs.project_config.MACHINE_POLICY_EXPLICIT
        )
        qs.install_engine.configure_scope_policy = lambda **kwargs: (
            events.append(("policy", kwargs)) or ("OK", "explicit")
        )
        qs.paths.refresh_pinned_dir = lambda: events.append(("refresh", None))
        qs.project_mode.apply_latch = lambda project, **kwargs: (
            events.append(("scope", (Path(project), kwargs))) or 0
        )
        qs.project_config.require_latched = lambda _project: type(
            "ActiveScope",
            (),
            {
                "source": qs.project_config.SOURCE_EXPLICIT,
                "project_root": project.resolve(),
                "policy": qs.project_config.POLICY_SHARED,
            },
        )()
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
            "--scope", "shared",
            "--enable-project-scopes",
            "--skip-doctor",
            "--no-seed",
        ])
        _assert(rc == 0, f"quickstart should complete, got {rc}")
        _assert(events[:7] == [
            ("plan_policy", {}),
            (
                "policy",
                {"dry_run": False},
            ),
            ("pin", (str(root / "isolated kb"), False)),
            ("refresh", None),
            (
                "scope",
                (
                    project.resolve(),
                    {
                        "policy": "shared",
                        "new_kb": False,
                        "enable_project_scopes": True,
                    },
                ),
            ),
            ("runtime_settings", project.resolve()),
            ("run", ["wire"]),
        ], f"runtime settings must be written after pinning and before wiring: {events}")
    finally:
        qs.install_engine.resolve_python = original["resolve_python"]
        qs.agent_preflight_errors = original["agent_preflight_errors"]
        qs.pin_kb_for_quickstart = original["pin_kb_for_quickstart"]
        qs.install_engine.scope_policy_for_install = original[
            "scope_policy_for_install"
        ]
        qs.install_engine.configure_scope_policy = original[
            "configure_scope_policy"
        ]
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
        qs.project_mode.apply_latch = original["apply_latch"]
        qs.project_config.require_latched = original["require_latched"]
        shutil.rmtree(root, ignore_errors=True)
    print("PASS quickstart_pins_after_preflight_before_wiring")


def _run_scope_only_quickstart(
    monkeypatch,
    project: Path,
    kb_dir: Path,
    *,
    scope: str | None,
    activate: bool = True,
) -> int:
    monkeypatch.setattr(qs, "agent_preflight_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        qs.paths,
        "resolve_maintenance_executable",
        lambda _backend: "/bin/codex",
    )
    monkeypatch.setattr(
        qs.paths,
        "write_maintenance_runner",
        lambda **_kwargs: project.parent / "runtime_settings.json",
    )
    monkeypatch.setattr(qs.paths, "refresh_pinned_dir", lambda: None)
    monkeypatch.setattr(qs, "build_install_steps", lambda **_kwargs: [])
    monkeypatch.setattr(qs, "build_doctor_steps", lambda **_kwargs: [])
    args = [
        "--agents", "codex",
        "--project", str(project),
        "--kb-dir", str(kb_dir),
        "--skip-doctor",
        "--no-seed",
    ]
    if scope is not None:
        args.extend(["--scope", scope])
        if activate:
            args.append("--enable-project-scopes")
    return qs.main(args)


def test_fresh_quickstart_creates_explicit_shared_scope_with_global_pin(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    home = tmp_path / "fresh-home"
    home.mkdir()
    project = tmp_path / "fresh-project"
    project.mkdir()
    vault = test_root / "vaults" / f"fresh-quickstart-{tmp_path.name}"
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(
        qs.project_config.CONTROL_ROOT_ENV,
        str(test_root / "fresh-quickstart-control" / tmp_path.name),
    )
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")

    rc = _run_scope_only_quickstart(
        monkeypatch,
        project,
        vault,
        scope="shared",
    )

    _assert(rc == 0, f"fresh quickstart failed with {rc}")
    _assert(
        qs.project_config.read_machine_policy()
        == qs.project_config.MACHINE_POLICY_EXPLICIT,
        "the pin created by this invocation reclassified a fresh install",
    )
    target = qs.project_config.resolve(project)
    _assert(target.state == qs.project_config.MODE_LATCHED, target)
    _assert(target.source == qs.project_config.SOURCE_EXPLICIT, target)
    _assert(target.project_root == project.resolve(), target)
    _assert(target.policy == qs.project_config.POLICY_SHARED, target)
    _assert(target.kb_dir == vault.resolve(), target)


def test_fresh_quickstart_creates_new_explicit_private_vault(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    home = tmp_path / "private-home"
    home.mkdir()
    project = tmp_path / "private-project"
    project.mkdir()
    global_vault = test_root / "vaults" / f"private-global-{tmp_path.name}"
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(
        qs.project_config.CONTROL_ROOT_ENV,
        str(test_root / "private-quickstart-control" / tmp_path.name),
    )
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")

    rc = _run_scope_only_quickstart(
        monkeypatch,
        project,
        global_vault,
        scope="private",
    )

    _assert(rc == 0, f"private quickstart failed with {rc}")
    target = qs.project_config.resolve(project)
    _assert(target.state == qs.project_config.MODE_LATCHED, target)
    _assert(target.source == qs.project_config.SOURCE_EXPLICIT, target)
    _assert(target.project_root == project.resolve(), target)
    _assert(target.policy == qs.project_config.POLICY_PRIVATE, target)
    _assert(target.kb_dir is not None and target.kb_dir.is_dir(), target)
    _assert(target.kb_dir != global_vault.resolve(), target)
    _assert(list(target.kb_dir.iterdir()) == [], "new Private vault should start empty")
    pin = json.loads((home / "kb_location.json").read_text(encoding="utf-8"))
    _assert(pin["kb_dir"] == str(global_vault.resolve()), pin)


def test_existing_pin_project_opt_in_scopes_selected_project_and_locks_sibling(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    home = tmp_path / "existing-home"
    home.mkdir()
    project = tmp_path / "existing-project"
    project.mkdir()
    sibling = tmp_path / "existing-sibling"
    sibling.mkdir()
    vault = test_root / "vaults" / f"existing-quickstart-{tmp_path.name}"
    vault.mkdir(parents=True)
    pin = home / "kb_location.json"
    pin.write_text(json.dumps({"kb_dir": str(vault)}) + "\n", encoding="utf-8")
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(
        qs.project_config.CONTROL_ROOT_ENV,
        str(test_root / "existing-quickstart-control" / tmp_path.name),
    )
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", pin)

    rc = _run_scope_only_quickstart(
        monkeypatch,
        project,
        vault,
        scope="shared",
    )

    _assert(rc == 0, f"existing-install quickstart failed with {rc}")
    _assert(
        qs.project_config.read_machine_policy()
        == qs.project_config.MACHINE_POLICY_EXPLICIT,
        "explicit --scope choice did not activate project mode",
    )
    target = qs.project_config.resolve(project)
    _assert(
        target.state == qs.project_config.MODE_LATCHED
        and target.source == qs.project_config.SOURCE_EXPLICIT
        and target.project_root == project.resolve()
        and target.kb_dir == vault.resolve(),
        "selected project did not gain an exact Shared boundary",
    )
    sibling_target = qs.project_config.resolve(sibling)
    _assert(
        sibling_target.state == qs.project_config.MODE_LOCKED
        and sibling_target.kb_dir is None,
        "project-mode activation left an unselected sibling globally accessible",
    )


def test_global_shared_access_needs_no_project_scope_choice(
    compatibility_scope_env, tmp_path,
):
    project = tmp_path / "global-project"
    project.mkdir()
    choice, target = qs.resolve_scope_choice(
        project,
        None,
        dry_run=False,
        is_tty=False,
    )
    _assert(choice is None, choice)
    _assert(target.source == qs.project_config.SOURCE_GLOBAL, target)
    _assert(target.state == qs.project_config.MODE_LATCHED, target)


def test_scope_flag_without_activation_confirmation_is_refused(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    project = tmp_path / "unconfirmed-scope-project"
    project.mkdir()
    home = tmp_path / "unconfirmed-scope-home"
    home.mkdir()
    control = test_root / "unconfirmed-scope-control" / tmp_path.name
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(qs.project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")
    vault = test_root / "vaults" / f"unconfirmed-scope-{tmp_path.name}"
    rc = _run_scope_only_quickstart(
        monkeypatch,
        project,
        vault,
        scope="shared",
        activate=False,
    )

    _assert(rc == 2, rc)
    _assert(
        qs.project_config.read_machine_policy()
        == qs.project_config.MACHINE_POLICY_SHARED,
        "unconfirmed --scope still activated project mode",
    )
    _assert(not (project / ".latch").exists(), "unconfirmed --scope wrote a boundary")
    _assert(not control.exists(), "unconfirmed --scope wrote scope control state")


def test_interactive_activation_requires_exact_latch_word(
    compatibility_scope_env, tmp_path,
):
    project = tmp_path / "ceremony-refused-project"
    project.mkdir()
    responses = iter(["projects", "shared", "yes"])
    prompts: list[str] = []
    try:
        qs.resolve_scope_choice(
            project,
            None,
            dry_run=False,
            is_tty=True,
            input_fn=lambda prompt: prompts.append(prompt) or next(responses),
        )
    except ValueError as exc:
        _assert("not confirmed" in str(exc), str(exc))
    else:
        raise AssertionError("casual 'yes' was accepted as one-way activation consent")
    _assert(len(prompts) == 3, prompts)
    _assert("exactly 'latch'" in prompts[-1], prompts[-1])
    _assert(
        qs.project_config.read_machine_policy()
        == qs.project_config.MACHINE_POLICY_SHARED,
        "refused ceremony still activated project mode",
    )


def test_interactive_activation_proceeds_after_exact_latch_word(
    compatibility_scope_env, tmp_path, capsys,
):
    project = tmp_path / "ceremony-confirmed-project"
    project.mkdir()
    responses = iter(["projects", "private", "latch"])

    choice, _target = qs.resolve_scope_choice(
        project,
        None,
        dry_run=False,
        is_tty=True,
        input_fn=lambda _prompt: next(responses),
    )

    _assert(choice == "private", choice)
    out = capsys.readouterr().out
    _assert("One-way change" in out, out)


def test_quickstart_without_scope_keeps_global_shared_mode(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    project = tmp_path / "missing-scope-project"
    project.mkdir()
    home = tmp_path / "missing-scope-home"
    home.mkdir()
    control = test_root / "missing-scope-control" / tmp_path.name
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(qs.project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")
    vault = test_root / "vaults" / f"global-default-{tmp_path.name}"
    rc = _run_scope_only_quickstart(
        monkeypatch,
        project,
        vault,
        scope=None,
    )

    _assert(rc == 0, rc)
    _assert(
        qs.project_config.read_machine_policy()
        == qs.project_config.MACHINE_POLICY_SHARED,
        "ordinary quickstart unexpectedly activated project mode",
    )
    target = qs.project_config.resolve(project)
    _assert(target.source == qs.project_config.SOURCE_GLOBAL, target)
    _assert(target.kb_dir == vault.resolve(), target)
    _assert(not (project / ".latch").exists(), "global mode wrote a project boundary")


def test_dry_run_scope_plan_writes_nothing(
    monkeypatch, tmp_path, capsys,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    project = tmp_path / "dry-run-project"
    project.mkdir()
    home = tmp_path / "dry-run-home"
    home.mkdir()
    control = test_root / "dry-run-control" / tmp_path.name
    global_vault = test_root / "vaults" / f"dry-run-global-{tmp_path.name}"
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(qs.project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")
    monkeypatch.setattr(qs, "agent_preflight_errors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        qs.paths,
        "resolve_maintenance_executable",
        lambda _backend: "/bin/codex",
    )
    monkeypatch.setattr(
        qs.paths,
        "resolve_maintenance_path",
        lambda _executable: "/bin",
    )

    rc = qs.main([
        "--agents", "codex",
        "--project", str(project),
        "--kb-dir", str(global_vault),
        "--scope", "private",
        "--enable-project-scopes",
        "--dry-run",
        "--no-seed",
    ])

    captured = capsys.readouterr()
    _assert(rc == 0, captured)
    _assert(
        f"create an explicit private boundary at {project.resolve()}" in captured.out,
        captured.out,
    )
    _assert(list(project.iterdir()) == [], "dry-run wrote into the project")
    _assert(list(home.iterdir()) == [], "dry-run wrote install state")
    _assert(not control.exists(), "dry-run wrote scope control state")
    _assert(not global_vault.exists(), "dry-run created the global KB directory")


def test_dry_run_without_scope_previews_unchanged_global_mode(
    monkeypatch, tmp_path, capsys,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    project = tmp_path / "dry-run-missing-project"
    project.mkdir()
    home = tmp_path / "dry-run-missing-home"
    home.mkdir()
    control = test_root / "dry-run-missing-control" / tmp_path.name
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(qs.project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")

    rc = qs.main([
        "--agents", "codex",
        "--project", str(project),
        "--dry-run",
    ])

    captured = capsys.readouterr()
    _assert(rc == 0, rc)
    _assert("guided quickstart plan" in captured.out, captured.out)
    _assert("preserve global Shared mode" in captured.out, captured.out)
    _assert(list(project.iterdir()) == [], "failed dry-run wrote into project")
    _assert(list(home.iterdir()) == [], "failed dry-run wrote install state")
    _assert(not control.exists(), "failed dry-run wrote scope control state")


def test_inherited_latched_scope_is_preserved_without_prompt_or_apply(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    home = tmp_path / "inherited-home"
    home.mkdir()
    parent = tmp_path / "consulting-root"
    child = parent / "client-repo"
    child.mkdir(parents=True)
    global_vault = test_root / "vaults" / f"inherited-global-{tmp_path.name}"
    global_vault.mkdir(parents=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(global_vault)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(
        qs.project_config.CONTROL_ROOT_ENV,
        str(test_root / "inherited-control" / tmp_path.name),
    )
    monkeypatch.setattr(qs.install_engine, "KB_LOCATION_PATH", home / "kb_location.json")
    qs.project_config.write_machine_policy(qs.project_config.MACHINE_POLICY_EXPLICIT)
    qs.project_mode.apply_latch(parent, policy=qs.project_config.POLICY_SHARED)
    applied: list[bool] = []
    monkeypatch.setattr(
        qs.project_mode,
        "apply_latch",
        lambda *_args, **_kwargs: applied.append(True) or 0,
    )

    rc = _run_scope_only_quickstart(
        monkeypatch,
        child,
        global_vault,
        scope=None,
    )

    _assert(rc == 0, rc)
    _assert(applied == [], "inherited scope was unnecessarily re-applied")
    target = qs.project_config.resolve(child)
    _assert(target.project_root == parent.resolve(), target)
    _assert(target.policy == qs.project_config.POLICY_SHARED, target)
    _assert(not (child / ".latch").exists(), "quickstart added a child boundary")


def test_scope_prompt_has_no_default(monkeypatch, tmp_path):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    project = tmp_path / "prompt-project"
    project.mkdir()
    monkeypatch.setenv(
        qs.project_config.CONTROL_ROOT_ENV,
        str(test_root / "prompt-control" / tmp_path.name),
    )
    qs.project_config.write_machine_policy(qs.project_config.MACHINE_POLICY_EXPLICIT)
    responses = iter(["", "private"])
    prompts: list[str] = []

    choice, _target = qs.resolve_scope_choice(
        project,
        None,
        dry_run=False,
        is_tty=True,
        input_fn=lambda prompt: prompts.append(prompt) or next(responses),
    )

    _assert(choice == "private", choice)
    _assert(len(prompts) == 2, "empty input should not select a default")


def test_quickstart_does_not_repair_off_or_unsafe_locked_state(
    monkeypatch, tmp_path,
):
    test_root = qs.paths.validated_test_root()
    _assert(test_root is not None, "pytest isolation root is required")
    home = tmp_path / "unsafe-home"
    home.mkdir()
    global_vault = test_root / "vaults" / f"unsafe-global-{tmp_path.name}"
    global_vault.mkdir(parents=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(global_vault)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.setenv(
        qs.project_config.CONTROL_ROOT_ENV,
        str(test_root / "unsafe-control" / tmp_path.name),
    )
    qs.project_config.write_machine_policy(qs.project_config.MACHINE_POLICY_EXPLICIT)

    outer = tmp_path / "unsafe-outer"
    off_child = outer / "off-child"
    unauthorized = tmp_path / "unauthorized"
    off_child.mkdir(parents=True)
    unauthorized.mkdir()
    qs.project_mode.apply_latch(outer, policy=qs.project_config.POLICY_SHARED)
    qs.project_mode.apply_unlatch(off_child)
    qs.project_config.create_scope(
        unauthorized,
        policy=qs.project_config.POLICY_SHARED,
    )

    for project, expected in (
        (off_child, "UNLATCHED"),
        (unauthorized, "LOCKED"),
    ):
        try:
            qs.resolve_scope_choice(
                project,
                "private",
                dry_run=False,
                is_tty=False,
            )
        except ValueError as exc:
            _assert(expected in str(exc), str(exc))
        else:
            raise AssertionError(f"quickstart implicitly repaired {expected} state")


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
                "--scope", "shared",
                "--enable-project-scopes",
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
    test_resolve_agents_requires_choice_without_prompt_or_context()
    test_run_steps_stops_before_later_steps_on_failure()
    test_quickstart_seed_handoff_prints_once_noninteractive()
    test_quickstart_pin_uses_explicit_or_environment_override()
    test_quickstart_persists_transient_env_pin_for_global_shared_access()
    test_quickstart_pins_after_preflight_before_wiring()
    test_quickstart_preflight_failure_does_not_pin()
    test_quickstart_pin_conflict_stops_before_wiring()
    print("\nAll quickstart tests pass.")
