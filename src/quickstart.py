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
import versioning

KB_HOME = Path(
    os.environ.get("LATCH_HOME")
    or os.environ.get("CLAUDE_KB_HOME")
    or Path(__file__).resolve().parent.parent
)

AGENT_CHOICES = ("claude", "codex", "cursor", "both", "all")

_INTENSITY_NUMBERS = {"1": "quiet", "2": "standard", "3": "full"}


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


def intensity_host_notes(
    agents: Sequence[str], *, cursor_with_hooks: bool = False,
) -> list[str]:
    """Per-host truth for what the shared intensity choice can actually change."""
    selected = set(agents)
    notes: list[str] = []
    if "claude" in selected:
        notes.append(
            "Claude Code: intensity changes the startup brief and similarity-based "
            "prompt surfacing."
        )
    if "codex" in selected:
        notes.append(
            "Codex: intensity changes the startup brief; Codex does not currently "
            "support similarity-based prompt retrieval."
        )
    if "cursor" in selected:
        if cursor_with_hooks:
            notes.append(
                "Cursor: intensity changes the startup brief; the pre-edit gate stays "
                "enabled, but Cursor does not similarity-retrieve on each prompt."
            )
        else:
            notes.append(
                "Cursor: hooks are not selected, so the saved intensity has no current "
                "runtime effect on this surface; managed guidance remains unchanged."
            )
    return notes


def resolve_latch_intensity(
    value: str | None,
    *,
    project: Path,
    agents: Sequence[str],
    kb_dir: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    cursor_with_hooks: bool = False,
    is_tty: bool | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> tuple[str, str]:
    """Choose the tier quickstart will persist, prompting when possible.

    A valid ``LATCH_INTENSITY`` is normally a process-scoped runtime override.
    During quickstart it is also treated as an explicit installation choice;
    an applying run writes the selected value to ``latch_settings.json``.
    """
    values = os.environ if env is None else env
    evidence_kb_dir = (
        install_engine.absolute_kb_dir(os.fspath(kb_dir))
        if kb_dir is not None
        else None
    )
    raw_env = values.get("LATCH_INTENSITY")
    env_choice = None
    if raw_env is not None:
        env_choice = paths.normalize_latch_intensity(raw_env)
        if env_choice is None:
            raise ValueError(
                f"invalid LATCH_INTENSITY={raw_env!r}; unset it or choose quiet, "
                "standard, or full before running quickstart"
            )

    saved = paths.configured_latch_intensity()
    settings_is_non_file = (
        (paths.LATCH_SETTINGS_FILE.exists() or paths.LATCH_SETTINGS_FILE.is_symlink())
        and not paths.LATCH_SETTINGS_FILE.is_file()
    )
    if settings_is_non_file:
        raise ValueError(
            f"cannot save Latch intensity because {paths.LATCH_SETTINGS_FILE} "
            "exists but is not a regular file; remove or rename that path, then "
            "rerun quickstart"
        )
    if (
        saved is None
        and paths.LATCH_SETTINGS_FILE.is_file()
        and value is None
        and env_choice is None
    ):
        _value, _source, warning = paths.latch_intensity_state(env={})
        raise ValueError(
            f"cannot safely choose a tier because {warning or paths.LATCH_SETTINGS_FILE}; "
            "repair or remove "
            "the file, then rerun quickstart"
        )
    if saved is not None:
        default = saved
        reason = "saved setting"
    elif paths.kb_has_evidence(project, kb_dir=evidence_kb_dir):
        default = paths.LEGACY_LATCH_INTENSITY
        reason = "preserving existing Full behavior"
    else:
        default = paths.FRESH_INSTALL_LATCH_INTENSITY
        reason = "fresh-install default"

    if value is not None:
        selected = paths.normalize_latch_intensity(value)
        if selected is None:
            raise ValueError(
                f"unsupported Latch intensity {value!r}; choose "
                + ", ".join(paths.LATCH_INTENSITIES)
            )
        reason = "command-line choice"
    else:
        selected = default

    if env_choice is not None:
        if value is not None and selected != env_choice:
            raise ValueError(
                f"--latch-intensity {selected} conflicts with "
                f"LATCH_INTENSITY={env_choice}; unset the environment override "
                "or make the two choices match"
            )
        selected = env_choice
        reason = "environment override"

    if is_tty is None:
        is_tty = _stdio_is_tty()

    output_fn(
        "Scope: this intensity applies to every project and host using this "
        "Latch install."
    )
    if env_choice is not None:
        output_fn(
            "LATCH_INTENSITY is normally a process-only override. Quickstart treats "
            "this valid value as the requested install-wide choice and persists it "
            f"to {paths.LATCH_SETTINGS_FILE} on apply; dry-run makes no change."
        )
    if value is None and env_choice is None and is_tty:
        output_fn("")
        output_fn("How proactively should Latch surface project judgment?")
        output_fn("")
        output_fn(
            "All levels keep the static project contract, including its live Latch "
            "read before each response, correction reminders on supported prompt-hook "
            "hosts, and the same gate check when invoked. Intensity controls "
            "hook-added briefs and prompt context—not whether the agent can or should "
            "query Latch."
        )
        output_fn("")
        output_fn("1  Quiet")
        output_fn(
            "   Up to 1 workstream and 1 open question at startup; no "
            "similarity-based prompt retrieval."
        )
        output_fn(
            "   Turns off hook-added similarity hits; prior judgment can still surface "
            "through contract-driven Latch reads or the gate."
        )
        output_fn("2  Standard")
        output_fn(
            "   Runs a lightweight local topic-similarity check on each eligible "
            "prompt, injecting up to 3 KB hits only on the first prompt or a topic "
            "change; brief up to 3 workstreams, 2 questions, and 2 ideas."
        )
        output_fn(
            "   Gives up same-topic resurfacing, 2 prompt-hit slots, and the broader "
            "Full brief; explicit search and gate remain available."
        )
        output_fn("3  Full — best protection")
        output_fn(
            "   Up to 5 KB hits on every eligible prompt, including "
            "same-topic follow-ups; startup brief up to 5 workstreams, 3 questions, "
            "and 5 ideas."
        )
        output_fn(
            "   Recommended for long-lived, multi-agent, handoff-heavy, or "
            "costly-to-rebuild projects; it uses the most prompt context."
        )
        output_fn(
            "   Also keeps explicit no-hit receipts and standing-guideline capture "
            "nudges on supported prompt-hook hosts."
        )
        output_fn("")
        output_fn(
            "Evidence boundary: tier-level rebuild savings are not measured yet, so "
            "Latch makes no universal savings claim."
        )
        output_fn(
            "Frozen synthetic policy check (7 prompt events): the expected guardrail "
            "reference was present in hook-added context for 0/5 Quiet, 2/5 Standard, "
            "and 5/5 Full labeled opportunities; emitted context was 0, 1,712, and "
            "3,056 characters. Authored scores and weights make this true by "
            "construction—not observed savings."
        )
        for note in intensity_host_notes(agents, cursor_with_hooks=cursor_with_hooks):
            output_fn(note)
        default_number = {v: k for k, v in _INTENSITY_NUMBERS.items()}[default]
        while True:
            try:
                raw = input_fn(
                    f"Choose intensity [1 quiet / 2 standard / 3 full] [{default_number}]: "
                ).strip().lower()
            except EOFError:
                raw = ""
            if not raw:
                selected = default
                break
            candidate = _INTENSITY_NUMBERS.get(raw, raw)
            normalized = paths.normalize_latch_intensity(candidate)
            if normalized is not None:
                selected = normalized
                reason = "interactive choice"
                break
            output_fn("Please choose 1/quiet, 2/standard, or 3/full.")
    else:
        for note in intensity_host_notes(agents, cursor_with_hooks=cursor_with_hooks):
            output_fn(note)

    output_fn(f"Latch intensity: {selected.title()} ({reason}).")
    return selected, reason


def seed_source_for_agents(agents: Sequence[str], requested: str = "auto") -> str:
    if requested != "auto":
        return requested
    selected = set(agents)
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
    override = (
        kb_dir
        or os.environ.get("LATCH_KB_DIR")
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
) -> list[str]:
    return [
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


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(part)) for part in command)


def print_plan(steps: Sequence[Step], seed_command: Sequence[str] | None) -> None:
    print("\nlatch guided quickstart plan\n")
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
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> None:
    command = seed_command_args(
        python_path=python_path,
        project=project,
        source=source,
        backend=backend,
        lookback_days=lookback_days,
        last_sessions=last_sessions,
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
                          "Cursor requires a current SessionStart marker)"))
    ap.add_argument("--kb-dir",
                    help=("pin this installation to one explicit KB directory; "
                          "fresh installs otherwise use <LATCH_HOME>/store"))
    ap.add_argument("--lookback-days", type=int, default=90,
                    help="history horizon for initial-KB seeding (default: 90)")
    ap.add_argument("--last-sessions", type=int, default=50,
                    help="maximum sessions selected for initial-KB seeding (default: 50)")
    ap.add_argument("--latch-intensity", choices=paths.LATCH_INTENSITIES,
                    help=("how proactively Latch surfaces project judgment; quickstart "
                          "defaults genuinely fresh installs to standard, while a "
                          "settings-less runtime retains legacy full"))
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
    ap.add_argument("--no-seed", action="store_true",
                    help="leave the initial KB pending and print its review command")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    project = Path(args.project).expanduser().resolve()
    if not project.exists():
        print(f"error: project path does not exist: {project}", file=sys.stderr)
        return 2
    if args.lookback_days <= 0 or args.last_sessions <= 0:
        print("error: --lookback-days and --last-sessions must be positive", file=sys.stderr)
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

    try:
        intensity, intensity_reason = resolve_latch_intensity(
            args.latch_intensity,
            project=project,
            agents=agents,
            kb_dir=args.kb_dir,
            cursor_with_hooks=args.cursor_with_hooks,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    source = seed_source_for_agents(agents, args.seed_source)
    backend = seed_backend_for_agents(
        agents,
        cursor_model_backend=args.cursor_model_backend,
    )
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
    )

    print("\nlatch guided quickstart")
    print(f"  version      : {versioning.LATCH_VERSION} (wiring {versioning.WIRING_VERSION})")
    print(f"  KB_HOME      : {KB_HOME}")
    print(f"  project      : {project}")
    print(f"  interpreter  : {python_path}")
    print(f"  agents       : {', '.join(agents)}")
    print(f"  seed source  : {source}")
    print(f"  seed backend : {backend}")
    print(f"  lookback days: {args.lookback_days}")
    print(f"  intensity    : {intensity} ({intensity_reason})")
    if "cursor" in agents:
        print(f"  Cursor backend: {args.cursor_model_backend or 'cursor (native default)'}")
        print(f"  Cursor hooks  : {'enabled' if args.cursor_with_hooks else 'not installed'}")
    print(f"  session cap  : {args.last_sessions}")
    print("  initial KB   : pending review")
    print(f"  mode         : {'DRY-RUN (no writes)' if args.dry_run else 'apply'}")

    pin_level, pin_msg = pin_kb_for_quickstart(args.kb_dir, dry_run=args.dry_run)
    print(f"  KB pin       : [{pin_level}] {pin_msg}")
    if pin_level == "ERROR":
        print("Quickstart stopped before agent configuration or seed writes.", file=sys.stderr)
        return 2

    if args.dry_run:
        print_plan(steps, seed_cmd)
        return 0

    try:
        settings_path = paths.write_latch_intensity(intensity)
    except (OSError, ValueError) as e:
        print(f"error: could not save Latch intensity: {e}", file=sys.stderr)
        print("No agent configuration changes were written.", file=sys.stderr)
        return 2
    print(f"  settings     : {settings_path}")

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
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
