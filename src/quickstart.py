#!/usr/bin/env python3
"""One-command guided first-run path for latch.

This is an orchestrator, not a new installer. It delegates to the existing
Claude Code installer, Codex installer, Cursor installer, doctor checks, and seed command so the
quickstart can be one obvious path without changing core latch behavior.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Callable, Mapping, Sequence

import install_engine
import paths
import project_config
import project_mode
import versioning

KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)

AGENT_CHOICES = ("claude", "codex", "cursor", "both", "all")
SCOPE_CHOICES = (project_config.POLICY_SHARED, project_config.POLICY_PRIVATE)

ONE_WAY_DISCLOSURE = (
    "One-way change: enabling project scopes affects this whole machine. Every\n"
    "location outside an explicitly latched Shared or Private scope becomes\n"
    "LOCKED for Latch, and the machine cannot return to Global Shared mode.\n"
    "No KB content is copied, merged, imported, or deleted."
)


@dataclass(frozen=True)
class Step:
    label: str
    command: list[str]
    cwd: Path


def _stdio_is_tty() -> bool:
    try:
        return bool(sys.stdin) and bool(sys.stdout) and sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _prompt_yes_no(prompt: str, *, default: bool) -> bool:
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def resolve_scope_choice(
    project: Path,
    requested: str | None,
    *,
    dry_run: bool,
    activation_confirmed: bool = False,
    is_tty: bool | None = None,
    input_fn: Callable[[str], str] = input,
) -> tuple[str | None, project_config.ResolvedScope]:
    """Preserve global Shared mode unless project scoping is explicitly chosen."""
    try:
        target = project_config.resolve(project)
    except project_config.ProjectConfigError as exc:
        raise ValueError(f"could not safely resolve this project: {exc}") from exc

    machine_policy = project_config.read_machine_policy()
    if machine_policy == project_config.MACHINE_POLICY_SHARED:
        if requested is not None:
            if not activation_confirmed:
                raise ValueError(
                    "--scope would enable project scopes for this whole machine, "
                    "a one-way change that LOCKS every unscoped location; add "
                    "--enable-project-scopes to confirm it, or omit --scope to "
                    "keep the global shared KB"
                )
            print(ONE_WAY_DISCLOSURE)
            return requested, target
        if dry_run:
            return None, target
        if is_tty is None:
            is_tty = _stdio_is_tty()
        if not is_tty:
            return None, target
        while True:
            try:
                raw_mode = input_fn(
                    "Latch KB mode [global/projects] (default global): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt) as exc:
                raise ValueError("KB mode choice cancelled") from exc
            if raw_mode in {"", "global", "shared"}:
                return None, target
            if raw_mode in {"project", "projects", "scoped"}:
                break
            print("Please enter global or projects.")
        while True:
            try:
                raw_scope = input_fn(
                    "This project's scope [shared/private] (required): "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt) as exc:
                raise ValueError("project scope choice cancelled") from exc
            if raw_scope in SCOPE_CHOICES:
                break
            print("Please enter shared or private.")
        print(ONE_WAY_DISCLOSURE)
        try:
            confirmation = input_fn(
                "Type exactly 'latch' to enable project scopes for this machine "
                "(anything else cancels): "
            )
        except (EOFError, KeyboardInterrupt) as exc:
            raise ValueError("project scope activation cancelled") from exc
        if confirmation.strip() != "latch":
            raise ValueError(
                "project scope activation not confirmed; Global Shared mode is "
                "unchanged"
            )
        return raw_scope, target

    if (
        target.state == project_config.MODE_LATCHED
        and target.source == project_config.SOURCE_EXPLICIT
    ):
        if requested is not None and requested != target.policy:
            raise ValueError(
                f"this project already uses an explicit {target.policy} scope at "
                f"{target.project_root}; use `latch` separately to create or change "
                "a project boundary"
            )
        return None, target

    if target.state == project_config.MODE_UNLATCHED:
        raise ValueError(
            f"this project is UNLATCHED at {target.project_root}; re-latch it "
            "explicitly before running quickstart"
        )
    if (
        target.state == project_config.MODE_LOCKED
        and target.reason_code != project_config.LOCK_OUTSIDE_SCOPE
    ):
        raise ValueError(
            f"this project is LOCKED by unsafe existing scope state: "
            f"{target.reason or 'unknown scope error'}"
        )

    if requested is not None:
        return requested, target
    if dry_run:
        raise ValueError(
            "project-scoped mode requires this root's scope for dry-run: "
            "--scope shared or --scope private"
        )
    if is_tty is None:
        is_tty = _stdio_is_tty()
    if not is_tty:
        raise ValueError(
            "project-scoped mode requires this root's scope: "
            "--scope shared or --scope private"
        )

    while True:
        try:
            raw = input_fn(
                "Project KB scope [shared/private] (required; no default): "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt) as exc:
            raise ValueError(
                "scope choice cancelled; re-run with --scope shared or --scope private"
            ) from exc
        if raw in SCOPE_CHOICES:
            return raw, target
        print("Please enter shared or private; there is no default.")


def detect_agent_context(env: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_HOME"):
        return "codex"
    if (
        env.get("CLAUDECODE")
        or env.get("CLAUDE_CODE_SESSION_ID")
        or env.get("CLAUDE_SESSION_ID")
    ):
        return "claude"
    return None


def normalize_agents(value: str) -> tuple[str, ...]:
    if value == "both":
        return ("claude", "codex")
    if value == "all":
        return ("claude", "codex", "cursor")
    if value in ("claude", "codex", "cursor"):
        return (value,)
    raise ValueError(f"unsupported agent selection: {value}")


def agent_preflight_errors(
    agents: Sequence[str],
    *,
    cursor_model_backend: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Return all missing selected-agent CLIs before any config mutation.

    The outer one-command bootstrap can install Latch's own runtime, but it
    must not silently install or guess a user's coding-agent product. Keeping
    this preflight in quickstart protects direct and remote-bootstrap callers
    alike and avoids the historical half-installed state.
    """
    selected = set(agents)
    errors: list[str] = []
    if "claude" in selected and which("claude") is None:
        errors.append("Claude Code CLI (`claude`) is not on PATH")
    if "codex" in selected and which("codex") is None:
        errors.append("Codex CLI (`codex`) is not on PATH")
    if (
        "cursor" in selected
        and which("agent") is None
        and which("cursor-agent") is None
    ):
        errors.append("Cursor Agent CLI (`agent` or `cursor-agent`) is not on PATH")
    if (
        "cursor" in selected
        and cursor_model_backend in {"claude", "codex"}
        and cursor_model_backend not in selected
        and which(cursor_model_backend) is None
    ):
        label = "Claude Code" if cursor_model_backend == "claude" else "Codex"
        errors.append(
            f"{label} CLI (`{cursor_model_backend}`) selected as the Cursor model "
            "backend is not on PATH"
        )
    return errors


def resolve_agents(
    value: str,
    *,
    env: Mapping[str, str] | None = None,
    is_tty: bool | None = None,
    input_fn: Callable[[str], str] = input,
) -> tuple[str, ...]:
    """Resolve --agents, prompting only for the explicit auto mode."""
    if value != "auto":
        return normalize_agents(value)

    default = detect_agent_context(env)
    if is_tty is None:
        is_tty = _stdio_is_tty()
    if not is_tty:
        detected = f" Detected current surface: {default}." if default else ""
        raise ValueError(
            "Choose agent surfaces for non-interactive quickstart: "
            "--agents claude, --agents codex, --agents cursor, --agents both, or --agents all."
            + detected
        )

    suffix = f" (default {default})" if default else ""
    if default == "codex":
        print("Detected Codex. Type 'both' if you also want Claude Code wired, or 'all' for Cursor too.")
    elif default == "claude":
        print("Detected Claude Code. Type 'both' if you also want Codex wired, or 'all' for Cursor too.")
    while True:
        raw = input_fn(f"Agent surfaces [claude/codex/cursor/both/all]{suffix}: ").strip().lower()
        if not raw and default:
            return normalize_agents(default)
        if raw in AGENT_CHOICES:
            return normalize_agents(raw)
        print("Please enter one of: claude, codex, cursor, both, all")


def seed_source_for_agents(
    agents: Sequence[str],
    requested: str = "auto",
    *,
    cursor_history: bool = False,
) -> str:
    if requested != "auto":
        return requested
    selected = set(agents)
    if cursor_history and "cursor" in selected:
        # Cursor-only quickstart historically seeded any existing Claude and
        # Codex logs. Opt-in should add Cursor, not silently remove those
        # already-supported sources.
        return "all"
    seedable = selected & {"claude", "codex"}
    if seedable == {"claude", "codex"} or not seedable:
        return "both"
    if seedable == {"claude"}:
        return "claude"
    if seedable == {"codex"}:
        return "codex"
    return "both"


def seed_backend_for_agents(
    agents: Sequence[str],
    *,
    cursor_model_backend: str | None = None,
) -> str:
    """Choose the extractor from installed surfaces, not transcript source.

    Source selection answers whose history to read. Backend selection answers
    which installed agent should refine it. Stable Claude > Codex > Cursor
    precedence preserves the historical mixed-surface default while making
    Codex-only and Cursor-only first runs use the CLI their preflight proved.
    """
    selected = set(agents)
    if "claude" in selected:
        return "claude"
    if "codex" in selected:
        return "codex"
    if "cursor" in selected:
        return cursor_model_backend or "cursor"
    raise ValueError("at least one agent surface is required")


def pin_kb_for_quickstart(
    kb_dir: str | None,
    *,
    dry_run: bool,
) -> tuple[str, str]:
    """Persist the one-KB source of truth before any surface is wired."""
    override = kb_dir
    if override is None:
        override = (
            os.environ.get("LATCH_KB_DIR")
            or os.environ.get("CLAUDE_KB_DIR")
        )
    return install_engine.pin_kb_dir(override, dry_run)


def _src(name: str) -> str:
    return str(KB_HOME / "src" / name)


def _project_file(project: Path, name: str) -> str:
    return str((project / name).resolve())


def build_install_steps(
    *,
    agents: Sequence[str],
    python_path: str,
    project: Path,
    cursor_model_backend: str | None = None,
    cursor_with_hooks: bool = False,
) -> list[Step]:
    steps: list[Step] = []
    selected = set(agents)
    if "claude" in selected:
        steps.append(Step(
            "Wire Claude Code engine",
            [
                python_path,
                _src("install_engine.py"),
                "--python",
                python_path,
                "--no-seed-prompt",
                "--suppress-seed-output",
            ],
            KB_HOME,
        ))
        steps.append(Step(
            "Sync Claude Code project contract",
            [
                python_path,
                _src("claude_md_sync.py"),
                "--yes",
                _project_file(project, "CLAUDE.md"),
            ],
            project,
        ))
    if "codex" in selected:
        steps.append(Step(
            "Wire Codex",
            [
                python_path,
                _src("install_codex.py"),
                "--python",
                python_path,
                "--agents-md",
                _project_file(project, "AGENTS.md"),
                "--yes",
                "--no-seed-prompt",
                "--suppress-seed-output",
            ],
            project,
        ))
    if "cursor" in selected:
        command = [
            python_path,
            _src("install_cursor.py"),
            "--python",
            python_path,
            "--agents-md",
            _project_file(project, "AGENTS.md"),
            "--yes",
        ]
        if cursor_model_backend:
            command.extend(["--model-backend", cursor_model_backend])
        if cursor_with_hooks:
            command.append("--with-hooks")
        steps.append(Step("Wire Cursor", command, project))
    return steps


def build_doctor_steps(
    *,
    agents: Sequence[str],
    python_path: str,
    project: Path,
    full_codex_doctor: bool = False,
    cursor_model_backend: str | None = None,
    cursor_with_hooks: bool = False,
) -> list[Step]:
    steps: list[Step] = []
    selected = set(agents)
    if "claude" in selected:
        steps.extend([
            Step(
                "Check Claude Code engine wiring",
                [python_path, _src("install_engine.py"), "--python", python_path, "--check"],
                project,
            ),
            Step("Run latch doctor", [python_path, _src("doctor.py")], project),
        ])
    if "codex" in selected:
        codex_doctor = [
            python_path,
            _src("codex_doctor.py"),
            "--python",
            python_path,
            "--agents-md",
            _project_file(project, "AGENTS.md"),
        ]
        if not full_codex_doctor:
            codex_doctor.extend(["--skip-compact", "--skip-summarizer"])
        steps.extend([
            Step(
                "Check Codex wiring",
                [
                    python_path,
                    _src("install_codex.py"),
                    "--python",
                    python_path,
                    "--agents-md",
                    _project_file(project, "AGENTS.md"),
                    "--check",
                ],
                project,
            ),
            Step("Run latch Codex doctor", codex_doctor, project),
        ])
    if "cursor" in selected:
        cursor_check = [
            python_path,
            _src("install_cursor.py"),
            "--python",
            python_path,
            "--agents-md",
            _project_file(project, "AGENTS.md"),
            "--check",
        ]
        cursor_doctor = [
            python_path,
            _src("cursor_doctor.py"),
            "--python",
            python_path,
            "--agents-md",
            _project_file(project, "AGENTS.md"),
        ]
        if cursor_model_backend:
            cursor_check.extend(["--model-backend", cursor_model_backend])
            cursor_doctor.extend(["--model-backend", cursor_model_backend])
        if cursor_with_hooks:
            cursor_check.append("--with-hooks")
            cursor_doctor.append("--with-hooks")
        steps.extend([
            Step("Check Cursor wiring", cursor_check, project),
            Step("Run latch Cursor doctor", cursor_doctor, project),
        ])
    return steps


def seed_command_args(
    *,
    python_path: str,
    project: Path,
    source: str,
    backend: str,
    lookback_days: int = 90,
    last_sessions: int = 50,
    cursor_history: bool = False,
) -> list[str]:
    command = [
        python_path,
        _src("seed.py"),
        "--project",
        str(project),
        "--source",
        source,
        "--backend",
        backend,
        "--lookback-days",
        str(lookback_days),
        "--last-sessions",
        str(last_sessions),
        "--apply",
    ]
    if cursor_history:
        command.append("--cursor-history")
    return command


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def print_plan(
    steps: Sequence[Step],
    seed_command: Sequence[str] | None,
    *,
    scope_summary: str | None = None,
) -> None:
    print("\nlatch guided quickstart plan\n")
    if scope_summary:
        print(f"Scope: {scope_summary}")
        print()
    for idx, step in enumerate(steps, start=1):
        print(f"{idx}. {step.label}")
        print(f"   cwd: {step.cwd}")
        print(f"   cmd: {format_command(step.command)}")
    if seed_command:
        print(f"{len(steps) + 1}. Build the initial decision KB (review before staging writes)")
        print(f"   cmd: {format_command(seed_command)}")
    print()


def run_steps(
    steps: Sequence[Step],
    *,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    for step in steps:
        print(f"\n==> {step.label}")
        print(f"cwd: {step.cwd}")
        print(f"$ {format_command(step.command)}")
        result = run(step.command, cwd=str(step.cwd))
        if result.returncode != 0:
            print(f"\nQuickstart stopped: {step.label} exited {result.returncode}.")
            return result.returncode
    return 0


def offer_seed_after_quickstart(
    *,
    python_path: str,
    project: Path,
    source: str,
    backend: str,
    lookback_days: int = 90,
    last_sessions: int = 50,
    cursor_history: bool = False,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    command = seed_command_args(
        python_path=python_path,
        project=project,
        source=source,
        backend=backend,
        lookback_days=lookback_days,
        last_sessions=last_sessions,
        cursor_history=cursor_history,
    )
    command_text = format_command(command)
    print()
    print(install_engine.seed_next_step_message(command_text))
    print()
    if not _stdio_is_tty():
        print("Non-interactive shell: quickstart wiring is complete, but the initial KB "
              "is pending. Run the command above from the project to review it.")
        return
    print(f"Initial-KB target: {project}")
    if not _prompt_yes_no("Build the review-first initial KB now for this project?", default=True):
        print("Initial KB remains pending. Run the command above when you are ready to review it.")
        return
    result = run(command)
    if result.returncode == 0:
        print("Initial-KB review finished; only entries you approved were staged.")
    else:
        print(f"Initial-KB step exited with status {result.returncode}; wiring is still complete.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Guided first OSS quickstart for latch (Claude Code, Codex, Cursor, or all)."
    )
    ap.add_argument("--agents", choices=("auto", "claude", "codex", "cursor", "both", "all"), default="auto",
                    help="agent surfaces to wire (default: prompt or detect current agent)")
    ap.add_argument("--project", default=os.getcwd(),
                    help="project repo to wire and seed (default: cwd)")
    ap.add_argument("--python", help="interpreter to register for latch")
    ap.add_argument("--seed-source",
                    choices=("auto", "claude", "codex", "cursor", "both", "all"),
                    default="auto",
                    help=("transcript source for seed setup (default follows --agents; "
                          "Cursor uses a current-session marker unless "
                          "--cursor-history is explicitly enabled)"))
    ap.add_argument("--kb-dir",
                    help=("pin this installation to one explicit KB directory; "
                          "fresh installs otherwise use the platform data root "
                          "outside the source checkout"))
    ap.add_argument(
        "--scope",
        choices=SCOPE_CHOICES,
        help=(
            "KB boundary for this project: shared uses the global pin; "
            "private creates a new isolated KB"
        ),
    )
    ap.add_argument(
        "--enable-project-scopes",
        action="store_true",
        help=(
            "confirm the one-way machine-wide switch to project scopes; "
            "required with --scope while the machine is in Global Shared mode"
        ),
    )
    ap.add_argument("--lookback-days", type=int, default=90,
                    help="history horizon for initial-KB seeding (default: 90)")
    ap.add_argument("--last-sessions", type=int, default=50,
                    help="maximum sessions selected for initial-KB seeding (default: 50)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the install/check/seed plan without writing anything")
    ap.add_argument("--skip-doctor", action="store_true",
                    help="skip post-install doctor/check commands")
    ap.add_argument("--full-codex-doctor", action="store_true",
                    help="include Codex compact/summarizer probes in the Codex doctor")
    ap.add_argument("--cursor-model-backend", choices=("cursor", "claude", "codex"),
                    help="Cursor model backend (default: native Cursor Agent CLI)")
    ap.add_argument("--cursor-with-hooks", action="store_true",
                    help="install and verify opt-in Cursor session/gate/activity hooks")
    ap.add_argument(
        "--cursor-history",
        action="store_true",
        help=(
            "opt in to metadata-verified local Cursor IDE history for this "
            "project during initial-KB seeding "
            "(CLI/cloud/other projects/subagents excluded)"
        ),
    )
    ap.add_argument("--no-seed", action="store_true",
                    help="leave the initial KB pending and print its review command")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(f"error: project path is not a directory: {project}", file=sys.stderr)
        return 2
    if args.lookback_days <= 0 or args.last_sessions <= 0:
        print("error: --lookback-days and --last-sessions must be positive", file=sys.stderr)
        return 2

    try:
        scope_choice, initial_scope = resolve_scope_choice(
            project,
            args.scope,
            dry_run=args.dry_run,
            activation_confirmed=args.enable_project_scopes,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "No scope, agent configuration, doctor, or seed changes were written.",
            file=sys.stderr,
        )
        return 2

    python_path = install_engine.resolve_python(args.python)
    try:
        agents = resolve_agents(args.agents)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    preflight_errors = agent_preflight_errors(
        agents,
        cursor_model_backend=args.cursor_model_backend,
    )
    if preflight_errors:
        stream = sys.stdout if args.dry_run else sys.stderr
        label = "warning" if args.dry_run else "error"
        for message in preflight_errors:
            print(f"{label}: {message}", file=stream)
        if not args.dry_run:
            print("No agent configuration changes were written.", file=sys.stderr)
            return 2

    if args.cursor_history and "cursor" not in agents:
        print(
            "error: --cursor-history requires Cursor in --agents.",
            file=sys.stderr,
        )
        return 2
    cursor_history = bool(args.cursor_history)
    may_prompt_for_cursor_history = (
        "cursor" in agents
        and args.seed_source in {"auto", "cursor", "all"}
        and not args.dry_run
        and not args.no_seed
        and _stdio_is_tty()
    )
    if not cursor_history and may_prompt_for_cursor_history:
        print(
            "Cursor IDE history is stored in a private local project layout. "
            "This opt-in admits only conversations Cursor's local metadata "
            "assigns to this project and still requires the normal redacted "
            "source review before model use."
        )
        cursor_history = _prompt_yes_no(
            "Include local Cursor IDE history in the initial KB",
            default=False,
        )
    source = seed_source_for_agents(
        agents,
        args.seed_source,
        cursor_history=cursor_history,
    )
    if cursor_history and source not in {"cursor", "all"}:
        print(
            "error: --cursor-history requires --seed-source cursor, all, or auto.",
            file=sys.stderr,
        )
        return 2
    backend = seed_backend_for_agents(
        agents,
        cursor_model_backend=args.cursor_model_backend,
    )
    try:
        maintenance_executable = paths.resolve_maintenance_executable(backend)
    except ValueError as e:
        if not args.dry_run:
            print(f"error: {e}", file=sys.stderr)
            return 2
        maintenance_executable = f"<unresolved: {e}>"
    try:
        maintenance_path = paths.resolve_maintenance_path(
            maintenance_executable
            if os.path.isabs(maintenance_executable)
            else None
        )
    except ValueError as e:
        if not args.dry_run:
            print(f"error: {e}", file=sys.stderr)
            return 2
        maintenance_path = f"<unresolved: {e}>"
    maintenance_home = str(Path.home().resolve())
    install_steps = build_install_steps(
        agents=agents,
        python_path=python_path,
        project=project,
        cursor_model_backend=args.cursor_model_backend,
        cursor_with_hooks=args.cursor_with_hooks,
    )
    doctor_steps: list[Step] = []
    if not args.skip_doctor:
        doctor_steps = build_doctor_steps(
            agents=agents,
            python_path=python_path,
            project=project,
            full_codex_doctor=args.full_codex_doctor,
            cursor_model_backend=args.cursor_model_backend,
            cursor_with_hooks=args.cursor_with_hooks,
        )
    steps = [*install_steps, *doctor_steps]
    seed_cmd = seed_command_args(
        python_path=python_path,
        project=project,
        source=source,
        backend=backend,
        lookback_days=args.lookback_days,
        last_sessions=args.last_sessions,
        cursor_history=cursor_history,
    )

    print("\nlatch guided quickstart")
    print(f"  version      : {versioning.LATCH_VERSION} (wiring {versioning.WIRING_VERSION})")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  project      : {project}")
    print(f"  interpreter  : {python_path}")
    print(f"  agents       : {', '.join(agents)}")
    print(f"  seed source  : {source}")
    print(f"  seed backend : {backend}")
    print(f"  maintenance  : {backend} ({maintenance_executable})")
    print(f"  lookback days: {args.lookback_days}")
    if "cursor" in agents:
        print(f"  Cursor backend: {args.cursor_model_backend or 'cursor (native default)'}")
        print(f"  Cursor hooks  : {'enabled' if args.cursor_with_hooks else 'not installed'}")
        print(f"  Cursor history: {'opted in' if cursor_history else 'not selected'}")
    print(f"  session cap  : {args.last_sessions}")
    print("  initial KB   : pending review")
    if scope_choice is None and initial_scope.source == project_config.SOURCE_EXPLICIT:
        print(
            "  project scope: preserve "
            f"{initial_scope.policy} at {initial_scope.project_root}"
        )
    elif scope_choice is None:
        print("  project scope: global Shared mode (unchanged)")
    else:
        print(f"  project scope: create explicit {scope_choice}")
    print(f"  mode         : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}")

    try:
        install_engine.scope_policy_for_install()
    except project_config.ProjectConfigError as exc:
        print(f"  scope policy : [FAIL] existing policy is unsafe: {exc}")
        return 2
    policy_level, policy_msg = install_engine.configure_scope_policy(
        dry_run=args.dry_run,
    )
    print(f"  scope policy : [{policy_level}] {policy_msg}")
    if policy_level == "FAIL":
        return 2

    pin_level, pin_msg = pin_kb_for_quickstart(args.kb_dir, dry_run=args.dry_run)
    print(f"  KB pin       : [{pin_level}] {pin_msg}")
    if pin_level in {"ERROR", "FAIL"}:
        print("Quickstart stopped before agent configuration or seed writes.", file=sys.stderr)
        return 2
    if not args.dry_run:
        paths.refresh_pinned_dir()

    if args.dry_run:
        scope_summary = (
            f"preserve existing explicit {initial_scope.policy} scope at "
            f"{initial_scope.project_root}"
            if scope_choice is None
            and initial_scope.source == project_config.SOURCE_EXPLICIT
            else "preserve global Shared mode"
            if scope_choice is None
            else f"create an explicit {scope_choice} boundary at {project}"
        )
        print_plan(steps, seed_cmd, scope_summary=scope_summary)
        return 0

    if scope_choice is not None:
        try:
            scope_rc = project_mode.apply_latch(
                project,
                policy=scope_choice,
                new_kb=(scope_choice == project_config.POLICY_PRIVATE),
                enable_project_scopes=True,
            )
        except (OSError, project_config.ProjectConfigError) as exc:
            print(f"error: could not create the project scope: {exc}", file=sys.stderr)
            print("Quickstart stopped before agent wiring, doctor, or seed.", file=sys.stderr)
            return 2
        if scope_rc != 0:
            print("error: project scope setup did not complete", file=sys.stderr)
            return 2

    try:
        active_scope = project_config.require_latched(project)
    except project_config.ProjectConfigError as exc:
        print(f"error: project is not safely LATCHED: {exc}", file=sys.stderr)
        print("Quickstart stopped before agent wiring, doctor, or seed.", file=sys.stderr)
        return 2
    if scope_choice is not None and (
        active_scope.source != project_config.SOURCE_EXPLICIT
        or active_scope.project_root != project
        or active_scope.policy != scope_choice
    ):
        print(
            "error: quickstart did not establish the requested exact project scope",
            file=sys.stderr,
        )
        print("Quickstart stopped before agent wiring, doctor, or seed.", file=sys.stderr)
        return 2

    try:
        runtime_settings_path = paths.write_maintenance_runner(
            backend=backend,
            executable=maintenance_executable,
            home=maintenance_home,
            search_path=maintenance_path,
            project_path=project,
        )
    except (OSError, ValueError) as e:
        print(f"error: could not save Latch runtime settings: {e}", file=sys.stderr)
        print("No agent configuration changes were written.", file=sys.stderr)
        return 2
    print(f"  vault policy : {runtime_settings_path}")

    rc = run_steps(install_steps)
    if rc != 0:
        print("\nSeed setup was not offered because quickstart wiring/checks did not finish.")
        return rc

    rc = run_steps(doctor_steps)
    if rc != 0:
        print("\nSeed setup was not offered because quickstart wiring/checks did not finish.")
        return rc

    if args.no_seed:
        print()
        print("Initial KB remains pending because --no-seed was selected.")
        print(install_engine.seed_next_step_message(format_command(seed_cmd)))
        print()
        return 0

    offer_seed_after_quickstart(
        python_path=python_path,
        project=project,
        source=source,
        backend=backend,
        lookback_days=args.lookback_days,
        last_sessions=args.last_sessions,
        cursor_history=cursor_history,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
