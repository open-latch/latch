#!/usr/bin/env python3
"""Cold-start seed pass for latch.

This is an explicit, user-approved bootstrap step: read recent local agent
transcripts, use LLM calls to propose low-authority seed candidates, and write
nothing unless the user asks for it. LLM-backed seed is budget-capped and
preview-first.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import codex_transcript  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 14
LOOKBACK_CHOICES = (5, 14, 30)
DEFAULT_MAX_SESSIONS = 20
DEFAULT_MAX_CANDIDATES = 20
DEFAULT_MAX_LLM_CALLS = int(os.environ.get("LATCH_SEED_MAX_LLM_CALLS") or 20)
DEFAULT_LLM_WARNING_THRESHOLD = int(os.environ.get("LATCH_SEED_LLM_CONFIRM_THRESHOLD") or 10)
NO_LLM_INTERNAL_ENV = "LATCH_SEED_ALLOW_NO_LLM"
MAX_SOURCE_CHARS = 120_000
MAX_LLM_SOURCE_CHARS = 28_000
SOURCE_CHOICES = ("claude", "codex", "both")
AGENT_MISTAKE_MIN_CONFIDENCE = 0.85
KB_HOME = Path(__file__).resolve().parent.parent

SEED_INTRO = "Seed latch from prior work for immediate judgment value from latch."

ROLE_LINE_RE = re.compile(r"^\[([A-Za-z0-9_?. -]+)\]\s*(.*)")
USER_ROLES = {"user", "human"}
WHITESPACE_RE = re.compile(r"\s+")

SIGNAL_PATTERNS: list[tuple[str, str, str, float]] = [
    ("rejected_path", "decision", r"\b(ruled out|we rejected|i rejected|project rejected|team rejected|do not use|don't use|not to use|avoid|not going to|shouldn't)\b", 0.72),
    ("decision", "decision", r"\b(we decided|i decided|the decision is|let'?s use|use .* instead)\b", 0.68),
    ("preference", "preference", r"\b(always|never|prefer|from now on|as a rule|i like|i hate)\b", 0.66),
    ("correction", "fact", r"\b(that'?s wrong|not what i meant|still broken|still wrong|doesn'?t work|failed because|failure|root cause)\b", 0.58),
    ("ongoing_workstream", "workstream", r"\b(workstream|project lane|ongoing lane|active lane)\b", 0.60),
    ("open_question", "open_question", r"\b(we need to decide|open question|not sure yet|circle back)\b", 0.55),
]

TRANSIENT_LLM_PATTERNS = (
    r"\b(main|current|active|feature)\s+(worktree|branch)\b",
    r"\b(branch|worktree)\s+(state|path)\b",
    r"\bchecked out\b.*\bworktree\b",
    r"\bfast-?forward(?:ing|ed)?\s+main\b",
    r"\blanded on main\b",
    r"\bbehind\s+origin\b",
    r"\bahead\s+\d+\b",
    r"\buntracked\s+agents\.md\b",
    r"\bdirty\s+readme\.md\b",
    r"\bgit status\b",
    r"\bremote branch\b",
)
MACHINE_LOCAL_LLM_PATTERNS = (
    r"\bbypasspermissions\b",
    r"\bdangerously skip permissions\b",
    r"\bpermissions\.defaultmode\b",
    r"\beffortlevel\b",
    r"\bxhigh\b",
    r"\bmodel effort\b",
    r"\bglobal user settings\b",
    r"\bmachine-wide\b",
)
META_LLM_PATTERNS = (
    r"\b(seed|llm|claude)\s+preview returned\b",
    r"\bpreview returned\s+\d+\b",
    r"\bcandidates?\s+include\b",
    r"\bprevious seed candidates?\b",
    r"\bexpected candidates?\b",
    r"\bmark kb (idea|node)\s+\d+\b",
    r"\btranscript does not include the user's answer\b",
    r"\bassistant asked whether\b",
)
USER_BLAME_LLM_PATTERNS = (
    r"\buser\s+(messed up|failed|broke|ignored|violated|made a mistake)\b",
    r"\byou\s+(messed up|failed|broke|ignored|violated|made a mistake)\b",
)
AGENT_MISTAKE_SIGNALS = {
    "agent_mistake",
    "possible_agent_mistake",
    "violated_prior_decision",
    "violated_preference",
    "contradiction",
}
RETROACTIVE_AGENT_MISTAKE_PATTERNS = (
    r"\bwith hindsight\b",
    r"\bretrospectively\b",
    r"\bbased on later\b",
    r"\blater (clarified|specified|decided|provided|shared|told|explained|changed)\b",
    r"\bnew information (arrived|came later|was provided later|was unavailable)\b",
    r"\bcould not have known\b",
    r"\bdid not have (that|the|this) (information|context)\b",
    r"\bnot available to the agent at the time\b",
)
REPORT_SECTION_DEFS = (
    (
        "decisions_and_rejected_paths",
        "Decisions and rejected paths",
        "Project judgment latch can enforce before future code is written.",
    ),
    (
        "where_left_off",
        "Where you left off",
        "Recent durable outcomes, follow-ups, and state hints worth picking back up.",
    ),
    (
        "patterns_and_preferences",
        "Patterns and preferences",
        "Repeated user constraints and working style future agents should preserve.",
    ),
    (
        "agent_alignment_check",
        "Agent alignment check",
        "High-level direction latch inferred, then strict checks for agent behavior that appears to violate it.",
    ),
    (
        "continuity_notes",
        "Continuity notes",
        "Long-running threads captured only when strongly supported by prior sessions.",
    ),
)


@dataclass(frozen=True)
class SeedSource:
    id: str
    agent: str
    path: str
    mtime: str
    text: str


@dataclass
class SeedCandidate:
    kind: str
    title: str
    body: str
    confidence: float
    signals: list[str]
    source_ids: list[str]
    source_paths: list[str]
    llm_used: bool = False


@dataclass
class SeedReportSection:
    key: str
    title: str
    summary: str
    items: list[SeedCandidate]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Seed latch from prior local agent work.",
        epilog=(
            "Default mode is preview-only and LLM-backed. The seed pass may use "
            "model calls, capped by --max-llm-calls, and asks for confirmation "
            "above the LLM-call warning threshold. Add --apply to write approved "
            "candidates as staging KB evidence."
        ),
    )
    ap.add_argument("--project", default=os.getcwd(),
                    help="project path whose transcripts should be seeded (default: cwd)")
    ap.add_argument("--source", choices=("auto", "claude", "codex", "both"), default="auto",
                    help=("transcript source to scan. Use 'both' to merge Claude and Codex "
                          "by recency; 'auto' prompts interactively when possible"))
    ap.add_argument("--lookback-days", type=int, choices=LOOKBACK_CHOICES,
                    help="retention horizon to scan: 5, 14, or 30 days")
    ap.add_argument("--llm", choices=("yes", "no"), default="yes",
                    help=argparse.SUPPRESS)
    ap.add_argument("--allow-internal-no-llm", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--backend", choices=("claude", "codex"),
                    help="LLM backend for seed refinement (default follows latch model env)")
    ap.add_argument("--max-llm-calls", type=int, default=DEFAULT_MAX_LLM_CALLS,
                    help=f"maximum LLM calls for this seed pass (default: {DEFAULT_MAX_LLM_CALLS})")
    ap.add_argument("--llm-warning-threshold", type=int,
                    default=DEFAULT_LLM_WARNING_THRESHOLD,
                    help=("require a second confirmation above this estimated call count "
                          f"(default: {DEFAULT_LLM_WARNING_THRESHOLD})"))
    ap.add_argument("--calls-per-session", type=int, default=1,
                    help="heuristic LLM calls per selected session for the estimate")
    ap.add_argument("--last-sessions", "--max-sessions", dest="max_sessions",
                    type=int,
                    help=("last N recent sessions to scan "
                          f"(default: {DEFAULT_MAX_SESSIONS}; configurable with --last-sessions N)"))
    ap.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
                    help=f"maximum candidates to show/write (default: {DEFAULT_MAX_CANDIDATES})")
    ap.add_argument("--all-projects", action="store_true",
                    help="scan all recent local transcripts instead of filtering to --project")
    ap.add_argument("--apply", action="store_true",
                    help="write the approved seed candidates to the KB as staging evidence")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="accept confirmations for non-interactive runs")
    ap.add_argument("--format", choices=("text", "json"), default="text",
                    help="output format")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_HOME") or str(Path.home() / ".claude"),
                    help="Claude home directory for transcript discovery")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
                    help="Codex home directory for transcript discovery")
    return ap.parse_args(argv)


def prompt_choices(args: argparse.Namespace) -> None:
    if args.lookback_days is None:
        args.lookback_days = _prompt_int(
            "Retention horizon in days [5/14/30]",
            default=DEFAULT_LOOKBACK_DAYS,
            choices=LOOKBACK_CHOICES,
        )
    if args.source == "auto":
        args.source = _prompt_source(args)
    if args.max_sessions is None:
        args.max_sessions = _prompt_positive_int(
            "Recent sessions to scan (last N)",
            default=DEFAULT_MAX_SESSIONS,
        )


def _prompt_int(prompt: str, *, default: int, choices: tuple[int, ...]) -> int:
    if not sys.stdin.isatty():
        return default
    suffix = f" (default {default}): "
    while True:
        raw = input(prompt + suffix).strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print(f"Please enter one of: {', '.join(map(str, choices))}")
            continue
        if value in choices:
            return value
        print(f"Please enter one of: {', '.join(map(str, choices))}")


def _prompt_positive_int(prompt: str, *, default: int) -> int:
    if not sys.stdin.isatty():
        return default
    suffix = f" (default {default}): "
    while True:
        raw = input(prompt + suffix).strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a positive whole number.")
            continue
        if value > 0:
            return value
        print("Please enter a positive whole number.")


def _prompt_source(args: argparse.Namespace) -> str:
    default = default_source_choice(args)
    if not sys.stdin.isatty():
        if default is None:
            raise SystemExit(
                "Choose a transcript source for non-interactive seed runs: "
                "--source claude, --source codex, or --source both."
            )
        return default
    choices = "/".join(SOURCE_CHOICES)
    suffix = f" (default {default})" if default else ""
    while True:
        raw = input(f"Transcript source [{choices}]{suffix}: ").strip().lower()
        if not raw and default:
            return default
        if not raw:
            print(f"Please enter one of: {', '.join(SOURCE_CHOICES)}")
            continue
        if raw in SOURCE_CHOICES:
            return raw
        print(f"Please enter one of: {', '.join(SOURCE_CHOICES)}")


def default_source_choice(args: argparse.Namespace) -> str | None:
    available = available_sources(args)
    if len(available) == 1:
        return available[0]
    return None


def available_sources(args: argparse.Namespace) -> list[str]:
    out: list[str] = []
    if (Path(args.claude_home) / "projects").is_dir():
        out.append("claude")
    if (Path(args.codex_home) / "sessions").is_dir():
        out.append("codex")
    return out


def _prompt_yes_no(prompt: str, *, default: bool) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n]: " if default else " [y/N]: "
    raw = input(prompt + suffix).strip().lower()
    if not raw:
        return default
    return raw in {"y", "yes"}


def discover_sources(
    *,
    source: str,
    project_path: str,
    lookback_days: int,
    max_sessions: int,
    claude_home: str,
    codex_home: str,
    all_projects: bool = False,
    now: datetime | None = None,
) -> list[SeedSource]:
    cutoff = (now or utc_now()) - timedelta(days=lookback_days)
    roots: list[tuple[str, Path, str]] = []
    selected_agents = source_agents(source)
    if "claude" in selected_agents:
        roots.append(("claude", Path(claude_home) / "projects", "**/*.jsonl"))
    if "codex" in selected_agents:
        roots.append(("codex", Path(codex_home) / "sessions", "**/rollout-*.jsonl"))

    paths: list[tuple[datetime, str, Path]] = []
    for agent, root, pattern in roots:
        if not root.is_dir():
            continue
        for path in root.glob(pattern):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime < cutoff:
                continue
            paths.append((mtime, agent, path))

    out: list[SeedSource] = []
    for mtime, agent, path in sorted(paths, key=lambda item: item[0], reverse=True):
        if len(out) >= max_sessions:
            break
        text = read_source_text(agent, path)
        if not text.strip():
            continue
        if not all_projects and not source_matches_project(path, text, project_path):
            continue
        out.append(SeedSource(
            id=source_id(agent, path, text),
            agent=agent,
            path=str(path),
            mtime=mtime.isoformat(timespec="seconds"),
            text=text[-MAX_SOURCE_CHARS:],
        ))
    return out[:max_sessions]


def source_agents(source: str) -> tuple[str, ...]:
    if source == "claude":
        return ("claude",)
    if source == "codex":
        return ("codex",)
    return ("claude", "codex")


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def source_matches_project(path: Path, text: str, project_path: str) -> bool:
    project = str(Path(project_path).resolve())
    if project and project in text:
        return True
    encoded = _encoded_claude_project_path(project)
    if encoded and encoded in str(path):
        return True
    # Fall back to the repo directory name only when the transcript also looks
    # project-scoped. This avoids importing an unrelated chat that merely
    # mentions a common word.
    name = Path(project).name
    return bool(name and f"cwd=" in text and name in text)


def _encoded_claude_project_path(project: str) -> str:
    # Claude Code stores project transcript dirs as slash-replaced path keys
    # such as a slash-replaced absolute project path. Keep this permissive
    # across OSes.
    return project.replace("\\", "-").replace("/", "-").replace(":", "")


def read_source_text(agent: str, path: Path) -> str:
    if agent == "codex":
        return codex_transcript.read_transcript(path)[-MAX_SOURCE_CHARS:]
    return read_claude_transcript(path)[-MAX_SOURCE_CHARS:]


def read_claude_transcript(path: Path) -> str:
    lines: list[str] = []
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in raw_lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if is_machine_generated_claude_record(obj):
            continue
        role = obj.get("type") or obj.get("role") or "?"
        msg = obj.get("message") or obj
        content = msg.get("content") if isinstance(msg, dict) else None
        text = flatten_content(content) if content is not None else ""
        if not text and isinstance(obj, dict):
            cwd = obj.get("cwd") or obj.get("project_path")
            text = f"cwd={cwd}" if cwd else ""
        if text:
            lines.append(f"[{role}] {text}")
    return "\n\n".join(lines)


def is_machine_generated_claude_record(obj: dict[str, Any]) -> bool:
    """Skip Claude SDK/model-subprocess records, not human Claude Code turns."""
    prompt_source = str(obj.get("promptSource") or "").lower()
    entrypoint = str(obj.get("entrypoint") or "").lower()
    typ = str(obj.get("type") or "").lower()
    if prompt_source == "sdk" or entrypoint == "sdk-cli":
        return True
    if typ == "queue-operation":
        return True
    return False


def flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and "text" in item:
                parts.append(str(item["text"]))
            elif item.get("type") in {"input_text", "output_text"} and "text" in item:
                parts.append(str(item["text"]))
        return "\n".join(p for p in parts if p.strip()).strip()
    return str(content).strip() if content else ""


def source_id(agent: str, path: Path, text: str) -> str:
    if agent == "codex":
        sid = codex_transcript.transcript_session_id(path)
        if sid:
            return f"codex:{sid}"
    return f"{agent}:{path.stem}"


def deterministic_candidates(sources: list[SeedSource], *, max_candidates: int) -> list[SeedCandidate]:
    by_excerpt: dict[str, SeedCandidate] = {}
    for src in sources:
        for excerpt in user_signal_lines(src.text):
            match = classify_excerpt(excerpt)
            if match is None:
                continue
            signal, kind, confidence = match
            key = normalize_excerpt(excerpt)
            title = candidate_title(signal, excerpt)
            body = candidate_body(
                excerpt=excerpt,
                signals=[signal, "deterministic_seed"],
                confidence=confidence,
                source_paths=[src.path],
                source_ids=[src.id],
                llm_used=False,
            )
            existing = by_excerpt.get(key)
            if existing:
                existing.confidence = min(0.95, existing.confidence + 0.05)
                if signal not in existing.signals:
                    existing.signals.append(signal)
                if src.id not in existing.source_ids:
                    existing.source_ids.append(src.id)
                if src.path not in existing.source_paths:
                    existing.source_paths.append(src.path)
                existing.body = candidate_body(
                    excerpt=excerpt,
                    signals=existing.signals,
                    confidence=existing.confidence,
                    source_paths=existing.source_paths,
                    source_ids=existing.source_ids,
                    llm_used=False,
                )
                continue
            by_excerpt[key] = SeedCandidate(
                kind=kind,
                title=title,
                body=body,
                confidence=confidence,
                signals=[signal, "deterministic_seed"],
                source_ids=[src.id],
                source_paths=[src.path],
            )
    candidates = sorted(
        by_excerpt.values(),
        key=lambda c: (c.confidence, len(c.source_ids)),
        reverse=True,
    )
    return candidates[:max_candidates]


def user_signal_lines(text: str) -> list[str]:
    lines: list[str] = []
    in_user_turn = False
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = ROLE_LINE_RE.match(raw)
        if m:
            role = m.group(1).split()[0].lower()
            in_user_turn = role in USER_ROLES
            if not in_user_turn:
                continue
            candidate = m.group(2).strip()
        elif in_user_turn:
            candidate = raw
        else:
            continue
        if should_skip_user_candidate(candidate):
            continue
        if len(candidate) < 20:
            continue
        if classify_excerpt(candidate) is not None:
            lines.append(clip(candidate, 900))
    return lines


def should_skip_user_candidate(text: str) -> bool:
    """Drop injected context/structural fragments from otherwise user turns."""
    stripped = text.strip()
    if not stripped:
        return True
    lower = stripped.lower()
    normalized = re.sub(r"^[-*]\s+", "", lower)
    normalized = re.sub(r"^\d+\.\s+", "", normalized)
    if normalized.startswith((
        "# agents.md instructions",
        "## kb ",
        "## selection ",
        "<instructions>",
        "</instructions>",
        "<environment_context>",
        "</environment_context>",
        "<filesystem>",
        "</filesystem>",
        "<cwd>",
        "<shell>",
        "<current_date>",
        "<timezone>",
        "<!--",
        "body:",
        '"body":',
        "title:",
        '"title":',
        "status:",
        '"status":',
        "relation:",
        '"relation":',
        "kind:",
        '"kind":',
        "id=",
        "kb_get(",
        "kb_search(",
        "toolsearch(",
        "auto-injected",
        "standing directives ",
        "prefer modify and name ",
        "verdict_delta=",
        "your recommendation actually rests on ",
        "never invent a workstream_id ",
        "be specific. prefer concrete facts ",
        "workstream guidance:",
        "relation vocabulary:",
        "guidelines:",
    )):
        return True
    if any(marker in normalized for marker in (
        "related_kb_nodes",
        "verdict_delta=",
        "evidence_type",
        "suggested_remedy",
        "workstream_id",
        "mcp__",
        "kb_gate",
        "injected context",
    )):
        return True
    # Questions are often useful session context, but they are not confirmed
    # seed evidence. LLM mode can turn them into suggestions with nuance later.
    if "?" in normalized:
        return True
    if normalized.startswith((
        "after you finish, please ",
        "please commit",
        "can you try to ",
    )):
        return True
    if lower in {"<instructions>", "</instructions>", "```", "```json"}:
        return True
    # JSON/markdown fragments are frequently injected KB/tool context rather
    # than direct user decisions. Keep full prose sentences; drop fragments.
    if stripped[0] in {"{", "}", "[", "]"}:
        return True
    if stripped[0] in {'"', "'"} and ":" in stripped[:80]:
        return True
    return False


def classify_excerpt(excerpt: str) -> tuple[str, str, float] | None:
    lower = excerpt.lower()
    for signal, kind, pattern, confidence in SIGNAL_PATTERNS:
        if re.search(pattern, lower):
            return signal, kind, confidence
    return None


def normalize_excerpt(excerpt: str) -> str:
    lowered = WHITESPACE_RE.sub(" ", excerpt.lower()).strip()
    return re.sub(r"[^a-z0-9 ]+", "", lowered)[:220]


def candidate_title(signal: str, excerpt: str) -> str:
    prefixes = {
        "rejected_path": "Seeded rejected path",
        "decision": "Seeded decision",
        "preference": "Seeded preference",
        "correction": "Seeded correction signal",
        "ongoing_workstream": "Seeded continuity note",
        "open_question": "Seeded open question",
    }
    cleaned = WHITESPACE_RE.sub(" ", excerpt).strip()
    cleaned = cleaned[:90].rstrip(" .,;:")
    return f"{prefixes.get(signal, 'Seeded signal')}: {cleaned}"


def candidate_body(
    *,
    excerpt: str,
    signals: list[str],
    confidence: float,
    source_paths: list[str],
    source_ids: list[str],
    llm_used: bool,
) -> str:
    sources = "\n".join(f"- {sid} ({path})" for sid, path in zip(source_ids, source_paths))
    mode = "LLM-refined seed pass" if llm_used else "deterministic seed pass"
    return (
        "Seed candidate from prior local agent history. Treat as low-authority "
        "staging evidence until reviewed/promoted.\n\n"
        f"Mode: {mode}\n"
        f"Signals: {', '.join(sorted(set(signals)))}\n\n"
        "Why this helps: it gives latch initial decisions, preferences, rejected "
        "paths, and continuity notes to judge against before a fresh project has "
        "accumulated new compacted sessions.\n\n"
        "Source evidence:\n"
        f"{sources}\n\n"
        "Excerpt:\n"
        f"> {excerpt.strip()}"
    )


def estimate_llm_calls(session_count: int, *, calls_per_session: int, max_llm_calls: int) -> int:
    if session_count <= 0 or calls_per_session <= 0 or max_llm_calls <= 0:
        return 0
    return min(session_count * calls_per_session, max_llm_calls)


def confirm_llm_budget(args: argparse.Namespace, source_count: int) -> bool:
    estimate = estimate_llm_calls(
        source_count,
        calls_per_session=args.calls_per_session,
        max_llm_calls=args.max_llm_calls,
    )
    if args.llm != "yes" or estimate == 0:
        return True
    if estimate <= args.llm_warning_threshold or args.yes:
        return True
    print(
        f"\nLLM seed refinement may make up to {estimate} call(s) "
        f"({source_count} session(s), capped at {args.max_llm_calls})."
    )
    return _prompt_yes_no("Continue with LLM refinement", default=False)


def llm_candidates(
    sources: list[SeedSource],
    *,
    project_path: str,
    max_calls: int,
    max_candidates: int,
    backend: str | None,
) -> list[SeedCandidate]:
    if max_calls <= 0:
        return []
    import budget  # noqa: WPS433
    import model_backends  # noqa: WPS433

    out: list[SeedCandidate] = []
    for src in sources[:max_calls]:
        allowed, state = budget.check_and_record(project_path, category="nonheal")
        if not allowed:
            print(
                "LLM seed refinement stopped: latch non-heal budget cap reached "
                f"({state.get('count_nonheal')}/{budget.DEFAULT_NONHEAL_DAILY_CAP}).",
                file=sys.stderr,
            )
            break
        prompt = seed_prompt(project_path=project_path, source=src)
        result = model_backends.invoke_prompt(
            prompt,
            backend=backend,
            env_names=("LATCH_SEED_BACKEND", "LATCH_MODEL_BACKEND", "LATCH_GATE_BACKEND"),
            default="claude",
            timeout_s=240,
            purpose="seed refinement",
        )
        if result.error or not result.text:
            print(f"LLM seed refinement skipped {src.id}: {result.error}", file=sys.stderr)
            continue
        parsed = parse_json_envelope(result.text)
        for item in parsed.get("seed_candidates", []) if isinstance(parsed, dict) else []:
            cand = candidate_from_llm_item(item, src)
            if cand:
                out.append(cand)
        if len(out) >= max_candidates:
            break
    return dedupe_candidates(out)[:max_candidates]


def seed_prompt(*, project_path: str, source: SeedSource) -> str:
    return (
        "You are helping bootstrap latch, a local KB that preserves a user's "
        "decisions, preferences, rejected paths, and corrections for future coding agents.\n\n"
        "Extract only explicit, reusable seed candidates supported by the transcript. "
        "Prefer concrete decisions, rejected paths, user preferences, corrections, "
        "repeated re-asks, and verified outcomes that would still help a future "
        "agent weeks later.\n\n"
        "You may include a possible_agent_mistake signal only when the transcript "
        "directly shows an agent violating an explicit prior user decision, "
        "preference, or rejected path that was available before or during the "
        "agent action in that same transcript. Do not use later corrections, "
        "later sessions, later user-provided information, or hindsight to label "
        "an earlier agent action as a mistake. If later clarification changes "
        "the frame, extract the clarification as a correction or decision instead. "
        "Only include this signal when very confident, blame the agent rather than "
        "the user, and include the prior judgment plus the violating agent action "
        "in the body. Skip ambiguous mistakes.\n\n"
        "You may include a workstream candidate only when the transcript explicitly "
        "shows an ongoing project lane, repeated smaller idea, recurring follow-up, "
        "or named workstream that would help future agents anchor related decisions, "
        "rejected alternatives, rationale, reopen conditions, and progress. Treat "
        "this as a suggested staging workstream, not confirmed authority. Do not "
        "invent a workstream from a one-off task.\n\n"
        "Do NOT extract transient session bookkeeping: branch/worktree state, dirty "
        "files, commit/PR logistics, local path trivia, main fast-forwards, or "
        "temporary install/debug status unless it captures a durable product lesson. "
        "Do NOT extract machine-local settings churn such as permission bypasses, "
        "model effort changes, or global config edits unless the user framed it as "
        "a reusable preference. Do NOT extract meta-candidates about seed previews, "
        "candidate lists, or whether an assistant should mark a KB node verified. "
        "Do not infer private facts that are not stated. Decision-like candidates "
        "must preserve rejected-path rationale and reopen conditions when present. "
        "Return JSON only with this shape:\n"
        '{"seed_candidates":[{"kind":"workstream|decision|preference|fact|idea|open_question",'
        '"title":"short title","body":"evidence-backed markdown body",'
        '"confidence":0.0,"signals":["decision","rejected_path"]}]}\n\n'
        f"Project path: {project_path}\n"
        f"Source: {source.id} {source.path}\n\n"
        "--- TRANSCRIPT ---\n"
        f"{source.text[-MAX_LLM_SOURCE_CHARS:]}"
    )


def parse_json_envelope(raw: str) -> dict:
    text = raw.strip()
    try:
        outer = json.loads(text)
        if isinstance(outer, dict) and isinstance(outer.get("result"), str):
            text = outer["result"].strip()
        elif isinstance(outer, dict):
            return outer
    except json.JSONDecodeError:
        pass
    if text.startswith("```"):
        text = text.strip("`")
        text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def candidate_from_llm_item(item: Any, src: SeedSource) -> SeedCandidate | None:
    if not isinstance(item, dict):
        return None
    kind = str(item.get("kind") or "fact").strip()
    # Workstream is additive; decision candidates still carry rejected-path rationale.
    if kind not in {"workstream", "decision", "preference", "fact", "idea", "open_question"}:
        kind = "fact"
    title = clip(str(item.get("title") or "Seeded prior-work signal").strip(), 120)
    body_text = str(item.get("body") or "").strip()
    if not body_text:
        return None
    try:
        confidence = float(item.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    confidence = max(0.0, min(0.95, confidence))
    raw_signals = item.get("signals") if isinstance(item.get("signals"), list) else []
    signals = [str(s) for s in raw_signals if str(s).strip()]
    if "llm_seed" not in signals:
        signals.append("llm_seed")
    if high_confidence_agent_mistake(signals) and confidence < AGENT_MISTAKE_MIN_CONFIDENCE:
        return None
    if llm_candidate_skip_reason(kind=kind, title=title, body=body_text, signals=signals):
        return None
    body = (
        "Seed candidate from prior local agent history. Treat as low-authority "
        "staging evidence until reviewed/promoted.\n\n"
        f"{body_text}\n\n"
        f"Signals: {', '.join(sorted(set(signals)))}\n\n"
        "Source evidence:\n"
        f"- {src.id} ({src.path})"
    )
    return SeedCandidate(
        kind=kind,
        title=title,
        body=body,
        confidence=confidence,
        signals=signals,
        source_ids=[src.id],
        source_paths=[src.path],
        llm_used=True,
    )


def llm_candidate_skip_reason(
    *,
    kind: str,
    title: str,
    body: str,
    signals: list[str],
) -> str | None:
    """Drop LLM candidates that are explicit but not durable seed evidence."""
    text = normalize_for_quality_filter(" ".join([title, body, " ".join(signals)]))
    if _matches_any(text, TRANSIENT_LLM_PATTERNS):
        return "transient session bookkeeping"
    if _matches_any(text, MACHINE_LOCAL_LLM_PATTERNS):
        return "machine-local settings churn"
    if _matches_any(text, META_LLM_PATTERNS):
        return "meta seed/candidate chatter"
    if _matches_any(text, USER_BLAME_LLM_PATTERNS):
        return "user-blaming agent mistake framing"
    if normalized_signals(signals) & AGENT_MISTAKE_SIGNALS and _matches_any(
        text, RETROACTIVE_AGENT_MISTAKE_PATTERNS,
    ):
        return "retroactive agent mistake framing"
    if kind == "open_question" and re.search(r"\b(mark|verify|verified)\s+(kb|node|idea)\b", text):
        return "kb bookkeeping open question"
    return None


def normalize_for_quality_filter(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text.lower()).strip()


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def dedupe_candidates(candidates: list[SeedCandidate]) -> list[SeedCandidate]:
    by_key: dict[str, SeedCandidate] = {}
    for cand in candidates:
        key = normalize_excerpt(cand.title + "\n" + cand.body[:400])
        existing = by_key.get(key)
        if not existing or cand.confidence > existing.confidence:
            by_key[key] = cand
    return sorted(by_key.values(), key=lambda c: c.confidence, reverse=True)


def merge_candidate_sets(
    llm: list[SeedCandidate],
    deterministic: list[SeedCandidate],
    *,
    max_candidates: int,
) -> list[SeedCandidate]:
    # LLM candidates lead when present; deterministic candidates fill coverage.
    merged = list(llm)
    for cand in deterministic:
        if any(candidates_overlap(cand, existing) for existing in merged):
            continue
        merged.append(cand)
    return dedupe_candidates(merged)[:max_candidates]


def choose_seed_candidates(
    args: argparse.Namespace,
    llm: list[SeedCandidate],
    deterministic: list[SeedCandidate],
) -> tuple[list[SeedCandidate], bool]:
    """Pick report candidates while keeping the public LLM boundary honest."""
    if args.llm == "yes" and deterministic and not llm:
        return [], True
    return merge_candidate_sets(llm, deterministic, max_candidates=args.max_candidates), False


def candidates_overlap(a: SeedCandidate, b: SeedCandidate) -> bool:
    if report_section_key(a) != report_section_key(b):
        return False
    if not (set(a.source_ids) & set(b.source_ids)):
        return False
    a_terms = candidate_terms(a)
    b_terms = candidate_terms(b)
    if not a_terms or not b_terms:
        return False
    overlap = len(a_terms & b_terms) / min(len(a_terms), len(b_terms))
    return overlap >= 0.55


def candidate_terms(candidate: SeedCandidate) -> set[str]:
    text = normalize_excerpt(candidate.title + " " + candidate.body)
    stop = {
        "seed", "seeded", "candidate", "local", "agent", "history", "treat",
        "staging", "evidence", "confidence", "signals", "source", "from",
        "prior", "with", "this", "that", "because", "before", "after",
    }
    return {w for w in text.split() if len(w) > 3 and w not in stop}


def build_seed_report(candidates: list[SeedCandidate]) -> list[SeedReportSection]:
    by_key = {
        key: SeedReportSection(key=key, title=title, summary=summary, items=[])
        for key, title, summary in REPORT_SECTION_DEFS
    }
    for cand in candidates:
        by_key[report_section_key(cand)].items.append(cand)
    return [by_key[key] for key, _, _ in REPORT_SECTION_DEFS]


def report_section_key(candidate: SeedCandidate) -> str:
    signals = normalized_signals(candidate.signals)
    if "ongoing_workstream" in signals or candidate.kind == "workstream":
        return "continuity_notes"
    if signals & AGENT_MISTAKE_SIGNALS and candidate.confidence >= AGENT_MISTAKE_MIN_CONFIDENCE:
        return "agent_alignment_check"
    if "rejected_path" in signals or candidate.kind == "decision":
        return "decisions_and_rejected_paths"
    if "preference" in signals or candidate.kind == "preference":
        return "patterns_and_preferences"
    return "where_left_off"


def high_confidence_agent_mistake(signals: list[str]) -> bool:
    return bool(normalized_signals(signals) & AGENT_MISTAKE_SIGNALS)


def normalized_signals(signals: list[str]) -> set[str]:
    return {str(signal).strip().lower() for signal in signals if str(signal).strip()}


def candidate_evidence_line(candidate: SeedCandidate) -> str:
    excerpt = ""
    marker = "Excerpt:\n> "
    if marker in candidate.body:
        excerpt = candidate.body.split(marker, 1)[1].splitlines()[0]
    if excerpt:
        return clip(excerpt, 180)
    first_source = candidate.source_ids[0] if candidate.source_ids else "source"
    return f"receipt: {first_source}"


def alignment_direction_items(candidates: list[SeedCandidate], *, limit: int = 3) -> list[SeedCandidate]:
    """Source-backed high-level priorities for the agent-alignment empty state.

    This is a synthesis surface over existing candidates, not another writeable
    candidate class. Rejected paths stay in "Decisions and rejected paths" with
    their rationale; this section only summarizes the higher-level direction an
    agent should align to.
    """
    pool: list[SeedCandidate] = []
    for cand in candidates:
        signals = normalized_signals(cand.signals)
        if signals & AGENT_MISTAKE_SIGNALS:
            continue
        # Do not absorb rejected alternatives into broad direction copy. They
        # need to remain visible as rejected paths that gates can cite.
        if "rejected_path" in signals:
            continue
        # Non-rejected direction only; rejected-path rationale stays in its section.
        if cand.kind not in {"decision", "preference", "idea", "open_question", "fact"}:
            continue
        if not signals & {
            # Decision direction is allowed here only after rejected paths are filtered above.
            "decision",
            "preference",
            "idea",
            "open_question",
            "verified_outcome",
            "correction",
        }:
            continue
        pool.append(cand)

    def rank(cand: SeedCandidate) -> tuple[float, int, float, int]:
        signals = normalized_signals(cand.signals)
        kind_weight = {
            # Rank non-rejected direction signals; rejected alternatives stay separate.
            "decision": 5,
            "preference": 4,
            "idea": 3,
            "open_question": 2,
            "fact": 1,
        }.get(cand.kind, 0)
        # Rejected paths are filtered above, so this only boosts direction signals.
        signal_weight = 1.0 if signals & {"decision", "preference", "idea"} else 0.0
        return (signal_weight, kind_weight, cand.confidence, len(cand.source_ids))

    selected: list[SeedCandidate] = []
    seen: set[str] = set()
    for cand in sorted(pool, key=rank, reverse=True):
        terms = candidate_terms(cand)
        if any(
            terms and prev and len(terms & prev) / min(len(terms), len(prev)) >= 0.65
            for prev in seen_terms(selected)
        ):
            continue
        key = normalize_excerpt(cand.title)
        if key in seen:
            continue
        selected.append(cand)
        seen.add(key)
        if len(selected) >= limit:
            break
    return selected


def seen_terms(candidates: list[SeedCandidate]) -> list[set[str]]:
    return [candidate_terms(cand) for cand in candidates]


def catch_demo_candidate(candidates: list[SeedCandidate]) -> SeedCandidate | None:
    """The strongest rejected-path candidate for the first-wow gate demo."""
    rejected = [
        cand for cand in candidates
        if "rejected_path" in normalized_signals(cand.signals)
    ]
    if not rejected:
        return None
    clean_rejections = [
        cand for cand in rejected
        if not high_confidence_agent_mistake(cand.signals)
    ]
    demo_pool = clean_rejections or rejected
    return sorted(demo_pool, key=lambda c: c.confidence, reverse=True)[0]


def catch_demo_request(candidate: SeedCandidate) -> str:
    """A safe request that should make kb_gate retrieve the seeded rejection."""
    evidence = candidate_evidence_line(candidate)
    if evidence.startswith("receipt:"):
        evidence = candidate.title
    return clip(f"Revive this rejected path: {evidence}", 220)


def slash_command_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def catch_demo_payload(candidate: SeedCandidate) -> dict[str, Any]:
    request = catch_demo_request(candidate)
    gate_script = KB_HOME / "bin" / "run_kb_gate.sh"
    return {
        "candidate": public_candidate_dict(candidate),
        "request": request,
        "slash_command": f"/kb-gate {slash_command_quote(request)}",
        "shell_command": "bash " + shlex.quote(str(gate_script)) + " " + shlex.quote(request),
        "requires_apply": True,
        "expected_outcome": (
            "After you apply the seed, Latch should cite this seeded rejected "
            "path and ask whether to hold the line or override it."
        ),
    }


def seed_report_receipt(
    *,
    sources: list[SeedSource],
    candidates: list[SeedCandidate],
) -> dict[str, Any]:
    """Compact proof receipt for the visible seed-report surface."""
    report = build_seed_report(candidates)
    section_counts = {section.key: len(section.items) for section in report}
    source_total = len(sources)
    source_label = "source" if source_total == 1 else "sources"
    decisions = section_counts.get("decisions_and_rejected_paths", 0)
    continuity = section_counts.get("continuity_notes", 0)
    left_off = section_counts.get("where_left_off", 0)
    preferences = section_counts.get("patterns_and_preferences", 0)
    mistakes = section_counts.get("agent_alignment_check", 0)
    direction_items = len(alignment_direction_items(candidates))
    demo = catch_demo_candidate(candidates)
    next_proof = (
        "After applying this seed, run the catch-demo command below to watch "
        "kb_gate challenge the strongest rejected path before files change."
        if demo else
        "Review/apply the useful candidates first; a rejected-path catch demo "
        "appears when the report contains a clean rejected path."
    )
    summary = (
        f"Latch built this first-wow report from {source_total} selected local "
        f"{source_label}; it is a proof receipt, not a dashboard."
    )
    why = (
        "It surfaced "
        f"{decisions} decision/rejected-path item(s), {left_off} where-left-off "
        f"item(s), {preferences} pattern/preference item(s), {continuity} "
        f"continuity note(s), {direction_items} direction item(s), and "
        f"{mistakes} strictly filtered agent-alignment finding(s) that future "
        "gates can cite before code changes."
    )
    return {
        "label": "Latch seed receipt",
        "source": "latch_seed",
        "must_display_to_user": True,
        "summary": summary,
        "why_it_matters": why,
        "next_proof": next_proof,
        "used": {
            "sources": source_total,
            "source_counts": source_counts(sources),
            "candidates": len(candidates),
            "direction_priorities": direction_items,
            "sections": section_counts,
            "catch_demo": bool(demo),
        },
    }


def write_boundary_message(args: argparse.Namespace) -> str:
    if not args.apply:
        return "Preview only. Re-run with --apply to write these as staging seed candidates."
    if args.yes:
        return (
            "Apply mode with --yes. These candidates will be written as staging "
            "evidence after this report."
        )
    return (
        "Apply mode. Review this report first; candidates are written only if "
        "you approve the prompt below."
    )


def apply_success_message(inserted: list[int], candidates: list[SeedCandidate]) -> str:
    lines = [
        f"Wrote {len(inserted)} staging seed candidate(s): {', '.join(map(str, inserted))}",
    ]
    demo = catch_demo_candidate(candidates)
    if demo:
        payload = catch_demo_payload(demo)
        lines.extend([
            "",
            "Latch proof ready:",
            "The seed is now in the KB. Run the catch demo to watch latch "
            "challenge the strongest rejected path before files change:",
            f"- Claude Code: {payload['slash_command']}",
            f"- Shell: {payload['shell_command']}",
            f"Expected: {payload['expected_outcome']}",
        ])
    else:
        lines.extend([
            "",
            "Latch proof note: no clean rejected path was applied in this seed "
            "run, so there is no catch-demo command yet.",
        ])
    return "\n".join(lines)


def no_llm_disabled_reason(args: argparse.Namespace) -> str | None:
    if args.llm != "no":
        return None
    if args.allow_internal_no_llm or os.environ.get(NO_LLM_INTERNAL_ENV) == "1":
        return None
    return (
        "No-LLM seeding is disabled outside internal/debug baselines. "
        "Use the LLM-backed seed path, or set "
        f"{NO_LLM_INTERNAL_ENV}=1 / pass --allow-internal-no-llm for local "
        "baseline experiments."
    )


def render_text(
    *,
    args: argparse.Namespace,
    sources: list[SeedSource],
    candidates: list[SeedCandidate],
    llm_estimate: int,
) -> str:
    session_cap = args.max_sessions if args.max_sessions is not None else DEFAULT_MAX_SESSIONS
    lines = [
        SEED_INTRO,
        "",
        "Seeding reads selected local Claude and/or Codex chats for this project and "
        "proposes decisions, rejected paths, preferences, and concrete follow-ups "
        "that latch can judge against before the first new compacted session.",
        "",
        f"Project: {Path(args.project).resolve()}",
        f"Transcript source: {args.source}",
        f"Lookback: {args.lookback_days} day(s)",
        f"Session cap: last {session_cap} session(s) (change with --last-sessions N)",
        f"Sources scanned: {len(sources)} ({format_source_counts(sources)})",
        f"LLM-backed seed: {args.llm or 'yes'}"
        + (f" (estimated/capped calls: {llm_estimate})" if args.llm == "yes" else ""),
        f"Candidates: {len(candidates)}",
        "Ranking: sections are ordered for install-time value; items within each "
        "section are strongest-first.",
    ]
    if not candidates:
        lines.extend([
            "",
            (
                "LLM-backed seed produced no writeable candidates, so latch will "
                "not write deterministic fallback candidates in the user-facing "
                "path. Fix the model backend or try a wider source/window and rerun."
            ) if getattr(args, "llm_refinement_empty", False) else (
                "No seed candidates found. Try a wider lookback, higher "
                "--last-sessions, or --all-projects."
            ),
        ])
        return "\n".join(lines) + "\n"
    receipt = seed_report_receipt(sources=sources, candidates=candidates)
    lines.extend([
        "",
        "Latch receipt:",
        receipt["summary"],
        f"Why this mattered: {receipt['why_it_matters']}",
        f"Next proof: {receipt['next_proof']}",
    ])
    lines.extend(["", "Seed report:"])
    for section in build_seed_report(candidates):
        lines.extend(["", f"## {section.title}", section.summary])
        if section.key == "agent_alignment_check":
            direction_items = alignment_direction_items(candidates)
            lines.extend(["", "Direction and priorities:"])
            if direction_items:
                for cand in direction_items:
                    signals = ", ".join(sorted(set(cand.signals)))
                    source_count = len(cand.source_ids)
                    source_label = "source" if source_count == 1 else "sources"
                    lines.extend([
                        f"- [{cand.kind}] {cand.title}",
                        f"  signals={signals}; {source_count} {source_label}",
                        f"  evidence: {candidate_evidence_line(cand)}",
                    ])
            else:
                lines.append("No source-backed direction or priority synthesis found in this pass.")
            lines.extend(["", "Agent behavior:"])
        if not section.items:
            empty = "No high-confidence agent contradictions found in this pass." \
                if section.key == "agent_alignment_check" else "No candidates in this section."
            lines.append(empty)
            continue
        for cand in section.items:
            signals = ", ".join(sorted(set(cand.signals)))
            source_count = len(cand.source_ids)
            source_label = "source" if source_count == 1 else "sources"
            lines.extend([
                f"- [{cand.kind}] {cand.title}",
                f"  signals={signals}; {source_count} {source_label}",
                f"  evidence: {candidate_evidence_line(cand)}",
            ])
    demo = catch_demo_candidate(candidates)
    if demo:
        payload = catch_demo_payload(demo)
        lines.extend([
            "",
            "Try the catch demo:",
            "After you apply this seed, run one of these to watch latch challenge "
            "the strongest rejected path from the report:",
            f"- Claude Code: {payload['slash_command']}",
            f"- Shell: {payload['shell_command']}",
            f"Expected: {payload['expected_outcome']}",
        ])
    lines.extend([
        "",
        write_boundary_message(args),
    ])
    return "\n".join(lines) + "\n"


def render_json(
    *,
    args: argparse.Namespace,
    sources: list[SeedSource],
    candidates: list[SeedCandidate],
    llm_estimate: int,
) -> str:
    session_cap = args.max_sessions if args.max_sessions is not None else DEFAULT_MAX_SESSIONS
    payload = {
        "intro": SEED_INTRO,
        "project": str(Path(args.project).resolve()),
        "source": args.source,
        "lookback_days": args.lookback_days,
        "max_sessions": session_cap,
        "sources_scanned": len(sources),
        "source_counts": source_counts(sources),
        "llm": args.llm or "no",
        "llm_call_estimate": llm_estimate,
        "llm_refinement_empty": bool(getattr(args, "llm_refinement_empty", False)),
        "ranking": (
            "Sections are ordered for install-time value; items within each "
            "section are strongest-first using an internal score."
        ),
        "report": [
            public_report_section_dict(section, candidates=candidates)
            for section in build_seed_report(candidates)
        ],
        "receipt": seed_report_receipt(sources=sources, candidates=candidates) if candidates else None,
        "catch_demo": (
            catch_demo_payload(demo) if (demo := catch_demo_candidate(candidates)) else None
        ),
        "apply": bool(args.apply),
        "write_boundary": write_boundary_message(args),
        "candidates": [public_candidate_dict(c) for c in candidates],
    }
    return json.dumps(payload, indent=2) + "\n"


def public_report_section_dict(
    section: SeedReportSection,
    *,
    candidates: list[SeedCandidate],
) -> dict[str, Any]:
    data = asdict(section)
    data["items"] = [public_candidate_dict(c) for c in section.items]
    if section.key == "agent_alignment_check":
        data["direction_items"] = [
            public_candidate_dict(c)
            for c in alignment_direction_items(candidates)
        ]
    return data


def public_candidate_dict(candidate: SeedCandidate) -> dict[str, Any]:
    data = asdict(candidate)
    data.pop("confidence", None)
    return data


def source_counts(sources: list[SeedSource]) -> dict[str, int]:
    counts = {"claude": 0, "codex": 0}
    for src in sources:
        if src.agent in counts:
            counts[src.agent] += 1
        else:
            counts[src.agent] = counts.get(src.agent, 0) + 1
    return counts


def format_source_counts(sources: list[SeedSource]) -> str:
    counts = source_counts(sources)
    return ", ".join(f"{agent}={count}" for agent, count in counts.items())


def apply_candidates(candidates: list[SeedCandidate], *, project_path: str) -> list[int]:
    import heal  # noqa: WPS433
    import db  # noqa: WPS433

    conn = db.connect(project_path)
    inserted: list[int] = []
    try:
        for cand in candidates:
            result = heal.insert_with_heal(
                conn,
                kind=cand.kind,
                title=cand.title,
                body=cand.body,
                status="staging",
                use_llm=False,
                project_path=project_path,
                artifacts=[{"repo": project_path}],
            )
            inserted.append(int(result["id"]))
    finally:
        conn.close()
    return inserted


def clip(text: str, limit: int) -> str:
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)].rstrip() + "...[truncated]"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    disabled = no_llm_disabled_reason(args)
    if disabled:
        print(disabled, file=sys.stderr)
        return 2
    prompt_choices(args)
    args.project = str(Path(args.project).resolve())

    sources = discover_sources(
        source=args.source,
        project_path=args.project,
        lookback_days=args.lookback_days,
        max_sessions=args.max_sessions,
        claude_home=args.claude_home,
        codex_home=args.codex_home,
        all_projects=args.all_projects,
    )
    llm_estimate = estimate_llm_calls(
        len(sources),
        calls_per_session=args.calls_per_session,
        max_llm_calls=args.max_llm_calls,
    ) if args.llm == "yes" else 0

    if not confirm_llm_budget(args, len(sources)):
        print("Seed pass cancelled before any LLM calls.")
        return 1

    deterministic = deterministic_candidates(sources, max_candidates=args.max_candidates)
    llm = []
    if args.llm == "yes":
        llm = llm_candidates(
            sources,
            project_path=args.project,
            max_calls=args.max_llm_calls,
            max_candidates=args.max_candidates,
            backend=args.backend,
        )
    candidates, llm_refinement_empty = choose_seed_candidates(args, llm, deterministic)
    args.llm_refinement_empty = llm_refinement_empty

    output = render_json(args=args, sources=sources, candidates=candidates, llm_estimate=llm_estimate) \
        if args.format == "json" else render_text(
            args=args, sources=sources, candidates=candidates, llm_estimate=llm_estimate,
        )
    print(output, end="")

    if not args.apply:
        return 0
    if not candidates:
        print("Nothing to write.")
        return 0
    if not args.yes and not _prompt_yes_no(
        f"Write {len(candidates)} candidate(s) to the KB as staging evidence",
        default=False,
    ):
        print("Seed candidates were not written.")
        return 1
    inserted = apply_candidates(candidates, project_path=args.project)
    print(apply_success_message(inserted, candidates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
