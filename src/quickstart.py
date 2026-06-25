#!/usr/bin/env python3
"""One-command guided first-run path for latch.

This is an orchestrator, not a new installer. It delegates to the existing
Claude Code installer, Codex installer, doctor checks, and seed command so the
quickstart can be one obvious path without changing core latch behavior.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Callable, Mapping, Sequence

import install_engine

KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)

AGENT_CHOICES = ("claude", "codex", "both")


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
    if value in ("claude", "codex"):
        return (value,)
    raise ValueError(f"unsupported agent selection: {value}")


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
            "--agents claude, --agents codex, or --agents both."
            + detected
        )

    suffix = f" (default {default})" if default else ""
    if default == "codex":
        print("Detected Codex. Type 'both' if you also want Claude Code wired.")
    elif default == "claude":
        print("Detected Claude Code. Type 'both' if you also want Codex wired.")
    while True:
        raw = input_fn(f"Agent surfaces [claude/codex/both]{suffix}: ").strip().lower()
        if not raw and default:
            return normalize_agents(default)
        if raw in AGENT_CHOICES:
            return normalize_agents(raw)
        print("Please enter one of: claude, codex, both")


def seed_source_for_agents(agents: Sequence[str], requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    selected = set(agents)
    if selected == {"claude", "codex"}:
        return "both"
    if selected == {"claude"}:
        return "claude"
    if selected == {"codex"}:
        return "codex"
    return "both"


def _src(name: str) -> str:
    return str(KB_HOME / "src" / name)


def _project_file(project: Path, name: str) -> str:
    return str((project / name).resolve())


def build_install_steps(
    *,
    agents: Sequence[str],
    python_path: str,
    project: Path,
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
    return steps


def build_doctor_steps(
    *,
    agents: Sequence[str],
    python_path: str,
    project: Path,
    full_codex_doctor: bool = False,
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
    return steps


def seed_command_args(
    *,
    python_path: str,
    project: Path,
    source: str,
    last_sessions: int,
) -> list[str]:
    return [
        python_path,
        _src("seed.py"),
        "--project",
        str(project),
        "--source",
        source,
        "--last-sessions",
        str(last_sessions),
        "--apply",
    ]


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def print_plan(steps: Sequence[Step], seed_command: Sequence[str] | None) -> None:
    print("\nlatch guided quickstart plan\n")
    for idx, step in enumerate(steps, start=1):
        print(f"{idx}. {step.label}")
        print(f"   cwd: {step.cwd}")
        print(f"   cmd: {format_command(step.command)}")
    if seed_command:
        print(f"{len(steps) + 1}. Start seed-first setup")
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
    last_sessions: int,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    command = seed_command_args(
        python_path=python_path,
        project=project,
        source=source,
        last_sessions=last_sessions,
    )
    command_text = format_command(command)
    print()
    print(install_engine.seed_next_step_message(command_text))
    print()
    if not _stdio_is_tty():
        print("Non-interactive shell: quickstart is wired. Run the seed command above "
              "from the project when you are ready.")
        return
    print(f"Current seed target: {project}")
    if not _prompt_yes_no("Run LLM-backed seed now for this project?", default=True):
        print("Skipped seed. Run the command above later to avoid a cold start.")
        return
    result = run(command)
    if result.returncode == 0:
        print("Seed step finished.")
    else:
        print(f"Seed step exited with status {result.returncode}; wiring is still complete.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Guided first OSS quickstart for latch (Claude Code, Codex, or both)."
    )
    ap.add_argument("--agents", choices=("auto", "claude", "codex", "both"), default="auto",
                    help="agent surfaces to wire (default: prompt or detect current agent)")
    ap.add_argument("--project", default=os.getcwd(),
                    help="project repo to wire and seed (default: cwd)")
    ap.add_argument("--python", help="interpreter to register for latch")
    ap.add_argument("--seed-source", choices=("auto", "claude", "codex", "both"), default="auto",
                    help="transcript source for seed setup (default follows --agents)")
    ap.add_argument("--last-sessions", type=int, default=20,
                    help="recent sessions to scan during seed setup (default: 20)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the install/check/seed plan without writing anything")
    ap.add_argument("--skip-doctor", action="store_true",
                    help="skip post-install doctor/check commands")
    ap.add_argument("--full-codex-doctor", action="store_true",
                    help="include Codex compact/summarizer probes in the Codex doctor")
    ap.add_argument("--no-seed", action="store_true",
                    help="print the seed command but do not offer to run it")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    if not project.exists():
        print(f"error: project path does not exist: {project}", file=sys.stderr)
        return 2
    if args.last_sessions <= 0:
        print("error: --last-sessions must be positive", file=sys.stderr)
        return 2

    python_path = install_engine.resolve_python(args.python)
    try:
        agents = resolve_agents(args.agents)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    source = seed_source_for_agents(agents, args.seed_source)
    steps = build_install_steps(agents=agents, python_path=python_path, project=project)
    if not args.skip_doctor:
        steps.extend(build_doctor_steps(
            agents=agents,
            python_path=python_path,
            project=project,
            full_codex_doctor=args.full_codex_doctor,
        ))
    seed_cmd = seed_command_args(
        python_path=python_path,
        project=project,
        source=source,
        last_sessions=args.last_sessions,
    )

    print("\nlatch guided quickstart")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  project      : {project}")
    print(f"  interpreter  : {python_path}")
    print(f"  agents       : {', '.join(agents)}")
    print(f"  seed source  : {source}")
    print(f"  last sessions: {args.last_sessions}")
    print(f"  mode         : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}")

    if args.dry_run:
        print_plan(steps, seed_cmd)
        return 0

    rc = run_steps(steps)
    if rc != 0:
        print("\nSeed setup was not offered because quickstart wiring/checks did not finish.")
        return rc

    if args.no_seed:
        print()
        print(install_engine.seed_next_step_message(format_command(seed_cmd)))
        print()
        return 0

    offer_seed_after_quickstart(
        python_path=python_path,
        project=project,
        source=source,
        last_sessions=args.last_sessions,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
