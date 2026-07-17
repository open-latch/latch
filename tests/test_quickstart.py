"""Unit tests for the first-run guided quickstart orchestrator."""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import quickstart as qs  # noqa: E402


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
    print("PASS seed_source_follows_agents_by_default")


def test_seed_command_includes_project_source_sessions_and_apply():
    project = Path("/tmp/example project")
    args = qs.seed_command_args(
        python_path="/py",
        project=project,
        source="both",
        last_sessions=50,
    )
    _assert(args[:2] == ["/py", str(qs.KB_HOME / "src" / "seed.py")], args)
    _assert("--project" in args and str(project) in args, args)
    _assert("--source" in args and "both" in args, args)
    _assert("--lookback-days" in args and "90" in args, args)
    _assert("--last-sessions" in args and "50" in args, args)
    _assert("--apply" in args, args)
    formatted = qs.format_command(args)
    _assert("'/tmp/example project'" in formatted,
            f"formatted command should quote project paths with spaces: {formatted}")
    print("PASS seed_command_includes_project_source_sessions_and_apply")


def test_initial_kb_defaults_and_dry_run_plan_are_explicit():
    args = qs.parse_args(["--agents", "codex", "--project", "/tmp/example-project"])
    _assert(args.lookback_days == 90 and args.last_sessions == 50,
            f"unexpected initial-KB defaults: {args}")

    command = qs.seed_command_args(
        python_path="/py",
        project=Path("/tmp/example-project"),
        source="codex",
    )
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        qs.print_plan([], command)
    text = output.getvalue()
    _assert("Build the initial decision KB (review before staging writes)" in text,
            f"dry-run plan should make initial-KB authority boundaries explicit:\n{text}")
    _assert("--lookback-days 90" in text and "--last-sessions 50" in text,
            f"dry-run plan should expose the acquisition bounds:\n{text}")
    print("PASS initial_kb_defaults_and_dry_run_plan_are_explicit")


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


if __name__ == "__main__":
    test_build_steps_for_both_delegates_to_existing_installers()
    test_build_steps_for_cursor_delegates_to_cursor_installer()
    test_build_doctor_steps_cover_selected_surfaces()
    test_build_doctor_steps_cover_cursor_surface()
    test_seed_source_follows_agents_by_default()
    test_seed_command_includes_project_source_sessions_and_apply()
    test_initial_kb_defaults_and_dry_run_plan_are_explicit()
    test_resolve_agents_requires_choice_noninteractive_even_with_context()
    test_resolve_agents_uses_detected_default_when_interactive()
    test_resolve_agents_accepts_cursor_and_all()
    test_agent_preflight_reports_every_missing_selected_cli()
    test_agent_preflight_accepts_cursor_agent_alias()
    test_resolve_agents_requires_choice_without_prompt_or_context()
    test_run_steps_stops_before_later_steps_on_failure()
    test_quickstart_seed_handoff_prints_once_noninteractive()
    print("\nAll quickstart tests pass.")
