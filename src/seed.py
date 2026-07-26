#!/usr/bin/env python3
"""Cold-start seed pass for latch.

This is an explicit, user-approved bootstrap step: read recent local agent
transcripts, use LLM calls to propose low-authority seed candidates, and write
nothing unless the user asks for it. LLM-backed seed is budget-capped and
preview-first.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sqlite3
import stat
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import codex_transcript  # noqa: E402
import cursor_transcript  # noqa: E402
import paths  # noqa: E402

DEFAULT_LOOKBACK_DAYS = 90
LOOKBACK_CHOICES = (5, 14, 30, 90)
DEFAULT_MAX_SESSIONS = 50
DEFAULT_MAX_CANDIDATES = 20
# With the per-source cap gone, the global bound scales with how many sessions
# were actually read so a 30-session seed is not squeezed into one 20-candidate
# budget, while HARD_MAX_TOTAL_CANDIDATES keeps any run bounded by construction.
CANDIDATES_PER_SESSION_TARGET = 8
HARD_MAX_TOTAL_CANDIDATES = 200
# 20 is the DEFAULT, not a ceiling. It used to be a hard clamp, so
# `--max-llm-calls 500` silently became 20 and sessions 21..N were selected,
# reported as selected, and then never read — the exact "latch missed my
# history" failure this default now surfaces instead of hiding. The real
# ceiling only exists so a typo cannot start an unbounded model-call run.
DEFAULT_MAX_LLM_CALLS_BASE = 20
HARD_MAX_LLM_CALLS_CEILING = int(
    os.environ.get("LATCH_SEED_MAX_LLM_CALLS_CEILING") or 500
)
HARD_MAX_LLM_CALLS = DEFAULT_MAX_LLM_CALLS_BASE  # back-compat alias
DEFAULT_MAX_LLM_CALLS = min(
    int(os.environ.get("LATCH_SEED_MAX_LLM_CALLS") or DEFAULT_MAX_LLM_CALLS_BASE),
    HARD_MAX_LLM_CALLS_CEILING,
)
DEFAULT_LLM_WARNING_THRESHOLD = int(os.environ.get("LATCH_SEED_LLM_CONFIRM_THRESHOLD") or 10)
NO_LLM_INTERNAL_ENV = "LATCH_SEED_ALLOW_NO_LLM"
MAX_SOURCE_CHARS = 120_000
MAX_LLM_SOURCE_CHARS = 28_000
MAX_SOURCE_INVENTORY = 200
MAX_SOURCE_SCAN = 1_000
RECENT_SOURCE_RESERVE = 0.20
# No per-source candidate cap. A flat cap truncated node-dense sessions before
# the model's judgment mattered: measured against the restored KB, a 28-node
# session could return at most 6, holding total recall to a 60.6% ceiling.
# Boundedness now comes from the global --max-candidates bound applied in
# merge_candidate_sets, so a run stays cost-bounded without silently dropping
# the richest sessions.
MAX_CANDIDATES_PER_SOURCE = None
SEED_EXTRACTOR_VERSION = "seed-v2"
# Exact-meaning identity is intentionally independent of the extractor release:
# upgrading extraction must not duplicate an unchanged reviewed claim.
SEED_CLAIM_KEY_VERSION = 1
MAX_INLINE_CORROBORATIONS = 8
SOURCE_SUBJECT_CHARS = 96
REVIEW_BODY_CHARS = 720
SEED_PREVIEW_MAX_AGE_HOURS = 24
SEED_PREVIEW_CACHE_MAX_FILES = 8
SOURCE_CHOICES = ("claude", "codex", "cursor", "both", "all")
AGENT_MISTAKE_MIN_CONFIDENCE = 0.85
KB_HOME = Path(__file__).resolve().parent.parent

SEED_INTRO = "Build Latch's initial decision KB for immediate judgment value."
SEED_CANDIDATE_PREAMBLE = (
    "Seed candidate from prior local agent history. Treat as low-authority "
    "staging evidence until reviewed/promoted."
)

ROLE_LINE_RE = re.compile(r"^\[([A-Za-z0-9_?. -]+)\]\s*(.*)")
CURSOR_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
USER_ROLES = {"user", "human"}
# Assistant turns are second-tier evidence: only consulted when no user turn
# qualifies, and always labelled. User authority still overrides them.
ASSISTANT_EVIDENCE_ROLES = {"assistant", "agent", "model"}
WHITESPACE_RE = re.compile(r"\s+")

# High-confidence patterns only. This is a bounded safety layer, not a claim of
# exhaustive DLP. Redaction happens immediately after transcript flattening and
# is repeated on model output before it can enter candidates or caches.
SEED_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "private-key-boundary",
        re.compile(
            r"-----(?:BEGIN|END) (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        ),
    ),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "bearer-token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}"),
    ),
)
SEED_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?P<name>api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
    r"password|passwd|secret)\b(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^\s'\";,]{8,})(?P=quote)"
)
SEED_CREDENTIAL_URL_RE = re.compile(
    r"(?i)\b(?P<scheme>[a-z][a-z0-9+.-]*://)"
    r"(?P<user>[^\s/@:]+):(?P<password>[^\s/@]+)@"
)

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
    content_digest: str = ""
    value_score: float = 0.0
    redaction_count: int = 0
    subject: str = ""
    history_discovered: bool = False


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
    source_mtimes: list[str] = field(default_factory=list)
    source_digests: list[str] = field(default_factory=list)
    workstream_key: str | None = None
    source_excerpts: list[str] = field(default_factory=list)


class SeedPreviewError(RuntimeError):
    pass


class CursorSeedPreviewError(SeedPreviewError):
    pass


class SeedWriteBlocked(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class SeedApprovalError(ValueError):
    pass


class SeedSourceLedgerError(RuntimeError):
    """The existing source-import ledger could not be read safely."""


@dataclass
class SeedReportSection:
    key: str
    title: str
    summary: str
    items: list[SeedCandidate]


@dataclass
class SeedReviewCluster:
    cluster_id: str
    section_key: str
    items: list[SeedCandidate]


@dataclass
class SeedApplyResult:
    inserted_ids: list[int] = field(default_factory=list)
    skipped_import_keys: list[str] = field(default_factory=list)
    skipped_node_ids: list[int] = field(default_factory=list)
    corroborated_import_keys: list[str] = field(default_factory=list)
    corroborated_node_ids: list[int] = field(default_factory=list)
    resumed_import_keys: list[str] = field(default_factory=list)
    resumed_node_ids: list[int] = field(default_factory=list)
    pending_source_import_keys: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    workstream_attachments: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.failures


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_seed_observed_at(value: str) -> str:
    """Return one comparable UTC timestamp or reject malformed provenance."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("source observed_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("source observed_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def latest_candidate_observed_at(candidate: SeedCandidate) -> str | None:
    values = [
        normalize_seed_observed_at(value)
        for value in candidate.source_mtimes if str(value).strip()
    ]
    return max(values, default=None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build Latch's initial decision KB from prior local agent work.",
        epilog=(
            "Default mode is preview-only and LLM-backed. The seed pass may use "
            "model calls, capped by --max-llm-calls, and asks for confirmation "
            "above the LLM-call warning threshold. Add --apply to write approved "
            "candidates as staging KB evidence."
        ),
    )
    ap.add_argument("--project", default=os.getcwd(),
                    help="project path whose transcripts should be seeded (default: cwd)")
    ap.add_argument("--source", choices=("auto", *SOURCE_CHOICES), default="auto",
                    help=("transcript source to scan. 'both' means Claude+Codex; "
                          "'all' also includes Cursor when an exact transcript or "
                          "--cursor-history is supplied"))
    ap.add_argument("--lookback-days", type=int, choices=LOOKBACK_CHOICES,
                    help="retention horizon to scan: 5, 14, 30, or 90 days")
    ap.add_argument("--llm", choices=("yes", "no"), default="yes",
                    help=argparse.SUPPRESS)
    ap.add_argument("--allow-internal-no-llm", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--backend", choices=("claude", "codex", "cursor"),
                    help="LLM backend for seed refinement (default follows latch model env)")
    ap.add_argument("--max-llm-calls", type=int, default=DEFAULT_MAX_LLM_CALLS,
                    help=f"maximum LLM calls for this seed pass (default: {DEFAULT_MAX_LLM_CALLS})")
    ap.add_argument("--llm-warning-threshold", type=int,
                    default=DEFAULT_LLM_WARNING_THRESHOLD,
                    help=("require a second confirmation above this estimated call count "
                          f"(default: {DEFAULT_LLM_WARNING_THRESHOLD})"))
    ap.add_argument("--calls-per-session", type=int, default=1,
                    help=argparse.SUPPRESS)
    ap.add_argument("--last-sessions", "--max-sessions", dest="max_sessions",
                    type=int,
                    help=("maximum selected sessions after bounded value/recency ranking "
                          f"(default: {DEFAULT_MAX_SESSIONS}; configurable with --last-sessions N)"))
    ap.add_argument("--max-candidates", type=int, default=None,
                    help=("maximum candidates to show/write (default: scales with "
                          f"sessions read, {CANDIDATES_PER_SESSION_TARGET}/session, "
                          f"floor {DEFAULT_MAX_CANDIDATES}, "
                          f"hard max {HARD_MAX_TOTAL_CANDIDATES})"))
    ap.add_argument("--all-projects", action="store_true",
                    help="scan all recent local transcripts instead of filtering to --project")
    workstream = ap.add_mutually_exclusive_group()
    workstream.add_argument(
        "--workstream-id", type=int,
        help="attach approved candidates to an existing Latch workstream id",
    )
    workstream.add_argument(
        "--new-workstream", metavar="TITLE",
        help=("initialize a reviewed staging workstream with this title and limit "
              "extraction to evidence relevant to it"),
    )
    ap.add_argument(
        "--force-reimport", action="store_true",
        help="re-run extraction even when the same source digest was already fully applied",
    )
    ap.add_argument("--apply", action="store_true",
                    help="write the approved seed candidates to the KB as staging evidence")
    ap.add_argument(
        "--approve-candidate",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "with --apply, approve one candidate review ID from this report "
            "(repeatable)"
        ),
    )
    ap.add_argument(
        "--approve-cluster",
        action="append",
        default=[],
        metavar="ID",
        help=(
            "with --apply, approve one possible-duplicate cluster as one "
            "corroborated candidate (repeatable)"
        ),
    )
    ap.add_argument(
        "--dismiss-all",
        action="store_true",
        help=(
            "with --apply and an exact --preview-digest, reject every candidate "
            "and finalize the reviewed source revisions"
        ),
    )
    ap.add_argument("--yes", "-y", action="store_true",
                    help="accept confirmations for non-interactive runs")
    ap.add_argument("--format", choices=("text", "json"), default="text",
                    help="output format")
    ap.add_argument("--claude-home", default=os.environ.get("CLAUDE_HOME") or str(Path.home() / ".claude"),
                    help="Claude home directory for transcript discovery")
    ap.add_argument("--codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
                    help="Codex home directory for transcript discovery")
    ap.add_argument("--cursor-transcript", action="append", default=[], metavar="PATH",
                    help=("explicit Cursor transcript path (repeatable). Without this, "
                          "--source cursor uses only the current-session marker "
                          "unless --cursor-history is explicitly enabled"))
    ap.add_argument(
        "--cursor-history",
        action="store_true",
        help=(
            "opt in to metadata-verified local Cursor IDE history for this "
            "project; excludes Cursor CLI, cloud chats, other projects, and "
            "subagents"
        ),
    )
    ap.add_argument(
        "--cursor-home",
        default=os.environ.get("CURSOR_HOME") or str(Path.home() / ".cursor"),
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--cursor-state-db",
        default=os.environ.get("CURSOR_STATE_DB"),
        help=argparse.SUPPRESS,
    )
    ap.add_argument("--cursor-session-id",
                    help=("exact current Cursor session id shown in prompt context; "
                          "required for marker-based --source cursor"))
    ap.add_argument("--preview-digest",
                    help=("exact digest returned by a seed preview; required for "
                          "scoped non-interactive approval and for Cursor apply"))
    return ap.parse_args(argv)


def prompt_choices(args: argparse.Namespace) -> None:
    stream = sys.stderr if args.format == "json" else sys.stdout
    for raw in args.cursor_transcript:
        path = Path(raw).expanduser()
        if not path.is_file():
            raise SystemExit(f"Explicit Cursor transcript is not a readable file: {path}")
    if args.lookback_days is None:
        args.lookback_days = _prompt_int(
            "Retention horizon in days [5/14/30/90]",
            default=DEFAULT_LOOKBACK_DAYS,
            choices=LOOKBACK_CHOICES,
            stream=stream,
        )
    if args.source == "auto":
        args.source = _prompt_source(args, stream=stream)
    if args.max_sessions is None:
        args.max_sessions = _prompt_positive_int(
            "Maximum sessions to select after value/recency ranking",
            default=DEFAULT_MAX_SESSIONS,
            stream=stream,
        )


def _prompt_int(
    prompt: str,
    *,
    default: int,
    choices: tuple[int, ...],
    stream: Any = sys.stdout,
) -> int:
    if not sys.stdin.isatty():
        return default
    suffix = f" (default {default}): "
    while True:
        print(prompt + suffix, end="", file=stream, flush=True)
        try:
            raw = input().strip()
        except EOFError:
            return default
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print(
                f"Please enter one of: {', '.join(map(str, choices))}",
                file=stream,
            )
            continue
        if value in choices:
            return value
        print(
            f"Please enter one of: {', '.join(map(str, choices))}",
            file=stream,
        )


def _prompt_positive_int(
    prompt: str,
    *,
    default: int,
    stream: Any = sys.stdout,
) -> int:
    if not sys.stdin.isatty():
        return default
    suffix = f" (default {default}): "
    while True:
        print(prompt + suffix, end="", file=stream, flush=True)
        try:
            raw = input().strip()
        except EOFError:
            return default
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a positive whole number.", file=stream)
            continue
        if value > 0:
            return value
        print("Please enter a positive whole number.", file=stream)


def _prompt_source(
    args: argparse.Namespace,
    *,
    stream: Any = sys.stdout,
) -> str:
    default = default_source_choice(args)
    if not sys.stdin.isatty():
        if default is None:
            raise SystemExit(
                "Choose a transcript source for non-interactive seed runs: "
                "--source claude, --source codex, --source cursor, "
                "--source both, or --source all."
            )
        return default
    choices = "/".join(SOURCE_CHOICES)
    suffix = f" (default {default})" if default else ""
    while True:
        print(
            f"Transcript source [{choices}]{suffix}: ",
            end="",
            file=stream,
            flush=True,
        )
        try:
            raw = input().strip().lower()
        except EOFError as exc:
            if default:
                return default
            raise SystemExit(
                "Choose a transcript source: --source claude, --source codex, "
                "--source cursor, --source both, or --source all."
            ) from exc
        if not raw and default:
            return default
        if not raw:
            print(
                f"Please enter one of: {', '.join(SOURCE_CHOICES)}",
                file=stream,
            )
            continue
        if raw in SOURCE_CHOICES:
            return raw
        print(
            f"Please enter one of: {', '.join(SOURCE_CHOICES)}",
            file=stream,
        )


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
    explicit = [Path(path).expanduser() for path in getattr(args, "cursor_transcript", [])]
    if any(path.is_file() for path in explicit):
        out.append("cursor")
    elif getattr(args, "cursor_history", False):
        try:
            history = cursor_transcript.discover_project_history(
                str(Path(args.project).expanduser().resolve()),
                cursor_home=getattr(args, "cursor_home", None),
                state_db=getattr(args, "cursor_state_db", None),
            )
        except cursor_transcript.CursorTranscriptError:
            history = []
        if history:
            out.append("cursor")
    elif not explicit and getattr(args, "cursor_session_id", None):
        try:
            cursor_transcript.resolve_current(
                str(Path(args.project).expanduser().resolve()),
                session_id=args.cursor_session_id,
            )
        except cursor_transcript.CursorTranscriptError:
            pass
        else:
            out.append("cursor")
    return out


def _prompt_yes_no(prompt: str, *, default: bool) -> bool:
    return _prompt_yes_no_on_stream(
        prompt,
        default=default,
        stream=sys.stdout,
    )


def _prompt_yes_no_on_stream(
    prompt: str,
    *,
    default: bool,
    stream: Any,
) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n]: " if default else " [y/N]: "
    print(prompt + suffix, end="", file=stream, flush=True)
    try:
        raw = input().strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in {"y", "yes"}


def redact_seed_text(text: str) -> tuple[str, int]:
    """Remove high-confidence secrets and return only an aggregate count.

    The replacement labels identify the detector class without retaining any
    portion of the secret value. Callers should treat this as defense in depth,
    not as an exhaustive secret scanner.
    """
    redacted = text
    total = 0
    for label, pattern in SEED_SECRET_PATTERNS:
        redacted, count = pattern.subn(f"<redacted:{label}>", redacted)
        total += count

    def replace_assignment(match: re.Match[str]) -> str:
        return f"{match.group('name')}{match.group('sep')}<redacted:credential>"

    redacted, count = SEED_SECRET_ASSIGNMENT_RE.subn(replace_assignment, redacted)
    total += count

    def replace_url(match: re.Match[str]) -> str:
        return f"{match.group('scheme')}<redacted:url-credentials>@"

    redacted, count = SEED_CREDENTIAL_URL_RE.subn(replace_url, redacted)
    total += count
    return redacted, total


def public_source_id(source_id: str) -> str:
    """Redact a source locator before model, report, or retrieval exposure."""
    redacted = redact_seed_text(str(source_id))[0]
    return WHITESPACE_RE.sub(" ", redacted).strip()


SEED_SOURCE_ID_PREFIX = "seed-source-id:"
SEED_SOURCE_ID_RE = re.compile(r"^seed-source-id:[0-9a-f]{64}$")


def seed_source_identity(source_id: str) -> str:
    """Return one idempotent opaque identity for a local source locator."""
    value = str(source_id)
    if SEED_SOURCE_ID_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(
        value.encode("utf-8", errors="replace")
    ).hexdigest()
    return f"{SEED_SOURCE_ID_PREFIX}{digest}"


def source_subject(text: str) -> str:
    """Derive a short local-review subject only from already-redacted text."""
    safe_text, _ = redact_seed_text(text)

    def cleaned(candidate: str) -> str:
        if should_skip_subject_candidate(candidate):
            return ""
        candidate = re.sub(r"<(?!redacted:)[^>]{1,80}>", " ", candidate)
        candidate = WHITESPACE_RE.sub(" ", candidate).strip(" -#*")
        if not candidate or candidate.startswith("/"):
            return ""
        return clip(candidate, SOURCE_SUBJECT_CHARS) if len(candidate) >= 12 else ""

    # Codex exposes a user-facing thread name in session metadata. Prefer it
    # over guessing from a first request, but only after the same redaction and
    # structural filtering used for transcript-derived subjects.
    for raw in safe_text.splitlines():
        if not raw.startswith("[session_meta] "):
            continue
        match = re.search(r"\bthread_name=(.+)$", raw)
        if match and (subject := cleaned(match.group(1))):
            return subject

    in_user_turn = False
    for raw in safe_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        match = ROLE_LINE_RE.match(raw)
        if match:
            role = match.group(1).split()[0].lower()
            in_user_turn = role in USER_ROLES
            if not in_user_turn:
                continue
            candidate = match.group(2).strip()
        elif in_user_turn:
            candidate = raw
        else:
            continue
        if subject := cleaned(candidate):
            return subject
    return ""


def source_content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def source_revision_token(source: SeedSource) -> str:
    """Privacy-safe in-memory key for one exact source revision."""
    payload = json.dumps(
        [seed_source_identity(source.id), source.content_digest],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_value_score(text: str, *, focus_query: str | None = None) -> float:
    """Score explicit human judgment before spending model calls.

    This deliberately reads only the already-redacted flattened transcript.
    It is a selection heuristic, never an authority score.
    """
    signal_weights = {
        "rejected_path": 6.0,
        "decision": 5.0,
        "correction": 4.5,
        "preference": 4.0,
        "ongoing_workstream": 3.0,
        "open_question": 2.0,
    }
    lines = user_signal_lines(text)
    score = 0.0
    seen: set[str] = set()
    for line in lines:
        match = classify_excerpt(line)
        if match is None:
            continue
        signal = match[0]
        score += signal_weights.get(signal, 1.0)
        seen.add(signal)
    score += min(len(seen), 5) * 0.75

    focus_terms = {
        term for term in normalize_excerpt(focus_query or "").split()
        if len(term) >= 3
    }
    if focus_terms:
        normalized = re.sub(
            r"[^a-z0-9 ]+", " ", WHITESPACE_RE.sub(" ", text.lower())
        )
        matched = sum(1 for term in focus_terms if term in normalized)
        coverage = matched / len(focus_terms)
        score += coverage * 10.0
    return round(score, 4)


def select_sources(
    sources: list[SeedSource], *, max_sessions: int,
) -> list[SeedSource]:
    """Reserve recent coverage, then fill by durable-signal value.

    The returned order is value-first because it is also the order in which the
    bounded model-call budget is spent. Recent sources remain in the selected
    set even when their judgment score is low.
    """
    if max_sessions <= 0 or not sources:
        return []
    cap = min(max_sessions, len(sources))
    mandatory = sorted(
        (
            source for source in sources
            if source.agent == "cursor" and not source.history_discovered
        ),
        key=lambda src: src.mtime,
        reverse=True,
    )[:cap]
    mandatory_ids = {(src.agent, src.id, src.content_digest) for src in mandatory}
    optional = [
        source for source in sources
        if (source.agent, source.id, source.content_digest) not in mandatory_ids
    ]
    recent_count = min(cap, max(1, math.ceil(cap * RECENT_SOURCE_RESERVE)))
    recent = sorted(optional, key=lambda src: src.mtime, reverse=True)[
        : min(recent_count, max(0, cap - len(mandatory)))
    ]
    recent_ids = mandatory_ids | {
        (src.agent, src.id, src.content_digest) for src in recent
    }
    remainder = [
        src for src in sources
        if (src.agent, src.id, src.content_digest) not in recent_ids
    ]
    remainder.sort(key=lambda src: (src.value_score, src.mtime, src.id), reverse=True)
    selected = mandatory + recent + remainder[: max(0, cap - len(mandatory) - len(recent))]
    selected.sort(key=lambda src: (src.value_score, src.mtime, src.id), reverse=True)
    return selected


def discover_sources(
    *,
    source: str,
    project_path: str,
    lookback_days: int,
    max_sessions: int,
    claude_home: str,
    codex_home: str,
    cursor_transcripts: list[str] | tuple[str, ...] = (),
    cursor_session_id: str | None = None,
    cursor_history: bool = False,
    cursor_home: str | None = None,
    cursor_state_db: str | None = None,
    all_projects: bool = False,
    focus_query: str | None = None,
    stats: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[SeedSource]:
    if stats is not None:
        stats.update({
            "inventory_considered": 0,
            "source_unavailable": 0,
            "source_invalid": 0,
            "project_excluded": 0,
            "cursor_history_discovered": 0,
            "cursor_history_unavailable": 0,
        })
    cutoff = (now or utc_now()) - timedelta(days=lookback_days)
    roots: list[tuple[str, Path, str]] = []
    selected_agents = source_agents(source)
    if "claude" in selected_agents:
        roots.append(("claude", Path(claude_home) / "projects", "**/*.jsonl"))
    if "codex" in selected_agents:
        roots.append(("codex", Path(codex_home) / "sessions", "**/rollout-*.jsonl"))

    paths: list[tuple[datetime, str, Path, str | None, bool]] = []
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
            paths.append((mtime, agent, path, None, False))

    if "cursor" in selected_agents:
        explicit = list(dict.fromkeys(str(Path(raw).expanduser().resolve()) for raw in cursor_transcripts))
        resolved_cursor: list[tuple[str | None, Path]] = []
        if explicit:
            for raw in explicit:
                path = Path(raw)
                if not path.is_file():
                    raise cursor_transcript.CursorTranscriptError(
                        f"explicit Cursor transcript is not a readable file: {path}"
                    )
                sid = None
                try:
                    current_sid, current_path = cursor_transcript.resolve_current(
                        project_path, session_id=cursor_session_id,
                    )
                except cursor_transcript.CursorTranscriptError:
                    pass
                else:
                    if current_path == path:
                        sid = current_sid
                resolved_cursor.append((sid, path))
        else:
            if cursor_session_id:
                try:
                    sid, path = cursor_transcript.resolve_current(
                        project_path, session_id=cursor_session_id,
                    )
                except cursor_transcript.CursorTranscriptError:
                    if source == "cursor" and not cursor_history:
                        raise
                else:
                    resolved_cursor.append((sid, path))
            elif not cursor_history and source == "cursor":
                # Preserve the exact-current default and its actionable error.
                sid, path = cursor_transcript.resolve_current(
                    project_path, session_id=cursor_session_id,
                )
                resolved_cursor.append((sid, path))

        exact_paths: set[Path] = set()
        for sid, path in resolved_cursor:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                resolved = path.resolve()
                exact_paths.add(resolved)
                paths.append((mtime, "cursor", resolved, sid, True))

        if cursor_history:
            try:
                history = cursor_transcript.discover_project_history(
                    project_path,
                    cursor_home=cursor_home,
                    state_db=cursor_state_db,
                )
            except cursor_transcript.CursorTranscriptError:
                if source == "cursor":
                    raise
                history = []
                if stats is not None:
                    stats["source_unavailable"] += 1
                    stats["cursor_history_unavailable"] = 1
            if stats is not None:
                stats["cursor_history_discovered"] = len(history)
            for sid, path in history:
                if path in exact_paths:
                    continue
                try:
                    mtime = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc,
                    )
                except OSError:
                    continue
                if mtime >= cutoff:
                    paths.append((mtime, "cursor", path, sid, False))

    ordered = sorted(paths, key=lambda item: item[0], reverse=True)
    # Exact current/explicit Cursor inputs are user-selected and must not be
    # crowded out by provider history. Opt-in Cursor history joins the normal
    # bounded inventory and receives no provider-level priority.
    cursor_items = [item for item in ordered if item[4]][
        :MAX_SOURCE_INVENTORY
    ]
    history_items = [item for item in ordered if not item[4]]
    scan_items = cursor_items + history_items[
        : max(0, MAX_SOURCE_SCAN - len(cursor_items))
    ]

    eligible: list[SeedSource] = []
    for mtime, agent, path, session_id, _exact in scan_items:
        if len(eligible) >= MAX_SOURCE_INVENTORY:
            break
        if stats is not None:
            stats["inventory_considered"] += 1
        if not path.is_file():
            if stats is not None:
                stats["source_unavailable"] += 1
            continue
        try:
            raw_text = read_source_text(agent, path)
        except OSError:
            if stats is not None:
                stats["source_unavailable"] += 1
            continue
        if not raw_text.strip():
            if stats is not None:
                stats["source_invalid"] += 1
            continue
        if agent != "cursor" and not all_projects \
                and not source_matches_project(path, raw_text, project_path):
            if stats is not None:
                stats["project_excluded"] += 1
            continue
        # Redact before truncation so a token/private-key envelope straddling
        # the retention boundary cannot leak an unrecognizable suffix.
        redacted_text, redaction_count = redact_seed_text(raw_text)
        subject = source_subject(redacted_text)
        text = redacted_text[-MAX_SOURCE_CHARS:]
        digest = source_content_digest(text)
        eligible.append(SeedSource(
            id=source_id(agent, path, text, session_id=session_id),
            agent=agent,
            path=str(path),
            mtime=mtime.isoformat(timespec="seconds"),
            text=text,
            content_digest=digest,
            value_score=source_value_score(text, focus_query=focus_query),
            redaction_count=redaction_count,
            subject=subject,
            history_discovered=bool(agent == "cursor" and not _exact),
        ))
    selected = select_sources(eligible, max_sessions=max_sessions)
    if stats is not None:
        stats["eligible"] = len(eligible)
        stats["selected"] = len(selected)
    return selected


def source_agents(source: str) -> tuple[str, ...]:
    if source == "claude":
        return ("claude",)
    if source == "codex":
        return ("codex",)
    if source == "cursor":
        return ("cursor",)
    if source == "all":
        return ("claude", "codex", "cursor")
    return ("claude", "codex")


def uses_cursor_session_preview(args: argparse.Namespace) -> bool:
    """Keep the slash-command digest lane exclusive to exact current-session seed."""
    return args.source == "cursor" and not bool(args.cursor_history)


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
        return codex_transcript.read_transcript(path)
    if agent == "cursor":
        return read_cursor_transcript(path)
    return read_claude_transcript(path)


def read_cursor_transcript(path: Path) -> str:
    """Flatten a hook-provided Cursor transcript without storage discovery.

    Cursor documents the transcript path but does not make private history
    scanning part of the hook contract. Accept JSONL role/message shapes and
    preserve plain-text transcripts as a fallback.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines: list[str] = []
    parsed_rows = 0
    for line in raw.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        parsed_rows += 1
        message = obj.get("message")
        role = obj.get("role") or obj.get("type") or "?"
        content: Any = obj.get("content") or obj.get("text")
        if isinstance(message, dict):
            role = message.get("role") or role
            content = message.get("content") or message.get("text") or content
        elif isinstance(message, str):
            content = message
        text = flatten_content(content)
        if str(role).strip().lower() in USER_ROLES:
            queries = [match.strip() for match in CURSOR_USER_QUERY_RE.findall(text)]
            if queries:
                text = "\n".join(query for query in queries if query)
        if text:
            lines.append(f"[{role}] {text}")
    if lines:
        return "\n\n".join(lines)
    return raw if not parsed_rows else ""


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


def source_id(
    agent: str,
    path: Path,
    text: str,
    *,
    session_id: str | None = None,
) -> str:
    if agent == "codex":
        sid = codex_transcript.transcript_session_id(path)
        if sid:
            return f"codex:{sid}"
    if agent == "cursor" and session_id:
        return f"cursor:{session_id}"
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
                source_mtimes=[src.mtime],
                source_digests=[src.content_digest],
                llm_used=False,
            )
            existing = by_excerpt.get(key)
            if existing:
                existing.confidence = min(0.95, existing.confidence + 0.05)
                if signal not in existing.signals:
                    existing.signals.append(signal)
                existing_revisions = {
                    (ref["id"], ref["digest"])
                    for ref in candidate_source_refs(existing)
                }
                if (src.id, src.content_digest) not in existing_revisions:
                    existing.source_ids.append(src.id)
                    existing.source_paths.append(src.path)
                    existing.source_mtimes.append(src.mtime)
                    existing.source_digests.append(src.content_digest)
                    existing.source_excerpts.append(excerpt)
                else:
                    idx = next(
                        idx for idx, ref in enumerate(candidate_source_refs(existing))
                        if (ref["id"], ref["digest"])
                        == (src.id, src.content_digest)
                    )
                    while len(existing.source_paths) <= idx:
                        existing.source_paths.append("")
                    while len(existing.source_mtimes) <= idx:
                        existing.source_mtimes.append("")
                    while len(existing.source_digests) <= idx:
                        existing.source_digests.append("")
                    while len(existing.source_excerpts) <= idx:
                        existing.source_excerpts.append("")
                    existing.source_paths[idx] = existing.source_paths[idx] or src.path
                    existing.source_mtimes[idx] = existing.source_mtimes[idx] or src.mtime
                    existing.source_digests[idx] = (
                        existing.source_digests[idx] or src.content_digest
                    )
                    existing.source_excerpts[idx] = (
                        existing.source_excerpts[idx] or excerpt
                    )
                existing.body = candidate_body(
                    excerpt=excerpt,
                    signals=existing.signals,
                    confidence=existing.confidence,
                    source_paths=existing.source_paths,
                    source_ids=existing.source_ids,
                    source_mtimes=existing.source_mtimes,
                    source_digests=existing.source_digests,
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
                source_mtimes=[src.mtime],
                source_digests=[src.content_digest],
                source_excerpts=[excerpt],
            )
    return balanced_candidate_selection(
        list(by_excerpt.values()), max_candidates=max_candidates,
    )


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


def _is_injected_user_fragment(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lower = stripped.lower()
    normalized = re.sub(r"^[-*]\s+", "", lower)
    normalized = re.sub(r"^\d+\.\s+", "", normalized)
    if normalized.startswith((
        "# agents.md instructions",
        "# files mentioned by the user",
        "## my request for ",
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
        "latch_get(",
        "latch_search(",
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
        # Host-injected session boilerplate observed in real transcripts: the
        # Codex plugin notice and ambient-context preambles read as prose, so
        # prefix matching is the only reliable filter.
        "here is a list of plugins",
        "this block is automatically supplied",
        "<system-reminder",
        "</system-reminder",
    )):
        return True
    # Plugin-registry entries ("Atlassian Rovo (atlassian-rovo@openai-curated-
    # remote)") trail the plugin notice inside the same injected block.
    if re.search(r"\([a-z0-9._-]+@[a-z0-9._-]+\)", normalized):
        return True
    if any(marker in normalized for marker in (
        "related_kb_nodes",
        "verdict_delta=",
        "evidence_type",
        "suggested_remedy",
        "workstream_id",
        "mcp__",
        "kb_gate",
        "latch_gate",
        "injected context",
        "not part of the user's request",
        "ambient ui state",
        "system-reminder",
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


def should_skip_subject_candidate(text: str) -> bool:
    """Drop injected structure while retaining useful question/request subjects."""
    return _is_injected_user_fragment(text)


def should_skip_user_candidate(text: str) -> bool:
    """Drop injected context/structural fragments from otherwise user turns."""
    if _is_injected_user_fragment(text):
        return True
    stripped = text.strip()
    lower = stripped.lower()
    normalized = re.sub(r"^[-*]\s+", "", lower)
    normalized = re.sub(r"^\d+\.\s+", "", normalized)
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


def _normalized_claim_text(candidate: "SeedCandidate") -> str:
    """Full untruncated normalization of title + claim for exact comparison."""
    combined = candidate.title + " " + candidate_core_text(candidate)
    lowered = WHITESPACE_RE.sub(" ", combined.lower()).strip()
    return re.sub(r"[^a-z0-9 ]+", "", lowered)


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
    source_mtimes: list[str],
    source_digests: list[str],
    llm_used: bool,
) -> str:
    del source_paths  # exact local locators stay in structured provenance, not retrieval text
    sources = "\n".join(
        f"- {public_source_id(sid)}; "
        f"observed_at={WHITESPACE_RE.sub(' ', mtime).strip() or 'unknown'}; "
        f"digest={digest[:16] or 'unknown'}"
        for sid, mtime, digest in _aligned_source_values(
            source_ids, source_mtimes, source_digests,
        )
    )
    mode = "LLM-refined seed pass" if llm_used else "deterministic seed pass"
    return (
        f"{SEED_CANDIDATE_PREAMBLE}\n\n"
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


def _aligned_source_values(
    source_ids: list[str], source_mtimes: list[str], source_digests: list[str],
) -> list[tuple[str, str, str]]:
    """Keep provenance arrays aligned even for legacy/test candidates."""
    out: list[tuple[str, str, str]] = []
    for idx, source_id in enumerate(source_ids):
        mtime = source_mtimes[idx] if idx < len(source_mtimes) else ""
        digest = source_digests[idx] if idx < len(source_digests) else ""
        out.append((source_id, mtime, digest))
    return out


def estimate_llm_calls(session_count: int, *, calls_per_session: int, max_llm_calls: int) -> int:
    if session_count <= 0 or calls_per_session <= 0 or max_llm_calls <= 0:
        return 0
    return min(session_count * calls_per_session, max_llm_calls)


def _source_mtime_at_or_after(source: SeedSource, cutoff: datetime) -> bool:
    """Window-shrink filter over already-discovered sources.

    A smaller lookback is always a subset of the one discovery already used,
    so re-filtering in place is exact and avoids a second full scan.
    """
    try:
        observed = datetime.fromisoformat(source.mtime)
    except (TypeError, ValueError):
        return True
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return observed >= cutoff


def coverage_plan(
    args: argparse.Namespace, eligible: int, project_path: str | None = None,
) -> dict[str, Any]:
    """Exact per-run coverage, derived from pre-scan counts before any call.

    `eligible` is the number of in-window sessions discover_sources actually
    found, so `covered`/`shortfall` are counted facts rather than an estimate.
    Both stopping conditions are reported: this run's --max-llm-calls and the
    shared daily non-heal budget, because whichever is smaller is the one that
    truncates, and a truncation the user cannot see reads as latch failing.
    """
    per_run = max(0, int(getattr(args, "max_llm_calls", 0) or 0))
    remaining_daily = None
    if project_path is not None:
        try:
            import budget  # noqa: WPS433
            remaining_daily = budget.remaining_nonheal(project_path)
        except Exception:  # budget storage is advisory here, never fatal
            remaining_daily = None
    binding_limit = per_run
    binding = "per_run_cap"
    if remaining_daily is not None and remaining_daily < per_run:
        binding_limit, binding = remaining_daily, "daily_budget"
    covered = min(eligible, binding_limit)
    return {
        "eligible_sessions": eligible,
        "covered_sessions": covered,
        "shortfall_sessions": max(0, eligible - covered),
        "full_coverage": covered >= eligible,
        "per_run_cap": per_run,
        "remaining_daily_nonheal": remaining_daily,
        "binding_constraint": binding,
        "calls_needed_for_full_coverage": eligible,
    }


def render_coverage_lines(plan: dict[str, Any]) -> list[str]:
    eligible = plan["eligible_sessions"]
    covered = plan["covered_sessions"]
    if plan["full_coverage"]:
        return [f"Coverage: all {eligible} session(s) in the window will be read."]
    lines = [
        f"Coverage: {covered} of {eligible} session(s) will be read — "
        f"{plan['shortfall_sessions']} would be skipped.",
        "This is a cap, not a latch failure: the skipped sessions are never "
        "opened, so nothing in them can be found.",
    ]
    if plan["binding_constraint"] == "daily_budget":
        lines.append(
            f"The binding limit is your daily non-heal model-call budget "
            f"({plan['remaining_daily_nonheal']} call(s) left today), not "
            f"--max-llm-calls ({plan['per_run_cap']}). Run "
            "/latch-budget-approve to lift it for the rest of today."
        )
    else:
        lines.append(
            f"The binding limit is --max-llm-calls ({plan['per_run_cap']}). "
            f"Full coverage needs {plan['calls_needed_for_full_coverage']}."
        )
    return lines


def confirm_llm_budget(
    args: argparse.Namespace, source_count: int, project_path: str | None = None,
) -> bool:
    """Confirm coverage before spending calls; never truncate silently."""
    if args.llm != "yes":
        return True
    plan = coverage_plan(args, source_count, project_path)
    args.coverage_plan = plan
    estimate = estimate_llm_calls(
        source_count,
        calls_per_session=1,
        max_llm_calls=args.max_llm_calls,
    )
    if estimate == 0:
        return True

    stream = sys.stderr if args.format == "json" else sys.stdout
    quiet = plan["full_coverage"] and estimate <= args.llm_warning_threshold
    if quiet or args.yes:
        # Non-interactive runs still SAY what they will skip, so one-command
        # activation (id=1986) stays unattended without hiding a shortfall.
        if not plan["full_coverage"]:
            for line in render_coverage_lines(plan):
                print(line, file=stream)
        return True

    print("", file=stream)
    for line in render_coverage_lines(plan):
        print(line, file=stream)
    if plan["full_coverage"]:
        # default=False keeps the pre-existing consent posture: an unattended
        # run must not start spending model calls. --yes is the documented way
        # to accept non-interactively, and it is handled above.
        return _prompt_yes_no_on_stream(
            f"Continue with LLM refinement ({estimate} call(s))",
            default=False,
            stream=stream,
        )
    choice = _prompt_coverage_choice(plan, stream=stream)
    if choice == "full":
        args.max_llm_calls = plan["calls_needed_for_full_coverage"]
        return True
    if choice == "reduce":
        return _prompt_reduce_window(args, stream=stream)
    return choice == "as_is"


def _prompt_coverage_choice(plan: dict[str, Any], *, stream) -> str:
    options = (
        ("full", f"raise the cap to {plan['calls_needed_for_full_coverage']} "
                 "for full coverage"),
        ("reduce", "reduce the lookback window so the cap covers it"),
        ("as_is", f"run as is, reading the {plan['covered_sessions']} most "
                  "recent session(s)"),
        ("cancel", "cancel"),
    )
    print("", file=stream)
    for key, label in options:
        print(f"  [{key}] {label}", file=stream)
    valid = {key for key, _ in options}
    for _ in range(3):
        try:
            raw = input("Choose [full/reduce/as_is/cancel] (default: as_is): ")
        except EOFError:
            # No stdin at all means no consent was given. Enter still selects
            # as_is; only an unattended run without --yes cancels.
            return "cancel"
        answer = raw.strip().lower() or "as_is"
        if answer in valid:
            return answer
        print("Enter one of: full, reduce, as_is, cancel.", file=stream)
    return "cancel"


def _prompt_reduce_window(args: argparse.Namespace, *, stream) -> bool:
    smaller = [days for days in LOOKBACK_CHOICES if days < args.lookback_days]
    if not smaller:
        print("Already at the smallest lookback window.", file=stream)
        return True
    print(f"Lookback choices smaller than {args.lookback_days}: "
          f"{', '.join(str(d) for d in smaller)}", file=stream)
    chosen = _prompt_int(
        "New lookback window in days",
        default=smaller[-1],
        choices=tuple(smaller),
        stream=stream,
    )
    args.lookback_days = chosen
    args.rescan_after_window_change = True
    return True


def prepare_llm_budget_storage(project_path: str) -> bool:
    """Fail before source consent when local budget state is not writable."""
    import budget  # noqa: WPS433

    try:
        budget.prepare_storage(project_path)
    except OSError:
        print(
            "Initial-KB extraction stopped before source consent: "
            "budget_storage_unavailable. Latch could not prepare its local "
            "budget directory; grant access to the configured Latch data "
            "directory and rerun.",
            file=sys.stderr,
        )
        return False
    return True


def source_review_lines(sources: list[SeedSource]) -> list[str]:
    lines = [
        "Selected local source receipts (transcript text is redacted before model use):"
    ]
    for source in sources:
        safe_subject, _ = redact_seed_text(source.subject)
        subject = (
            f"; subject={json.dumps(clip(safe_subject, SOURCE_SUBJECT_CHARS))}"
            if safe_subject.strip() else ""
        )
        lines.append(
            f"- {public_source_id(source.id)}; provider={source.agent}; "
            f"observed_at={source.mtime}; "
            f"digest={source.content_digest[:16]}; redactions={source.redaction_count}"
            f"{subject}"
        )
    return lines


def confirm_source_use(args: argparse.Namespace, sources: list[SeedSource]) -> bool:
    if args.llm != "yes" or not sources:
        return True
    stream = sys.stderr if args.format == "json" else sys.stdout
    print("\n" + "\n".join(source_review_lines(sources)), file=stream)
    if args.yes:
        return True
    return _prompt_yes_no_on_stream(
        "Use these redacted sources for LLM-backed initial-KB extraction",
        default=False,
        stream=stream,
    )


def planned_llm_sources(
    sources: list[SeedSource], *, max_calls: int,
) -> list[SeedSource]:
    """Choose the exact sources that may cross the model boundary this run.

    Re-selecting at the call cap preserves the recent reserve inside the actual
    model plan, rather than merely inside the larger acquisition window.
    """
    max_calls = min(max_calls, HARD_MAX_LLM_CALLS_CEILING)
    if max_calls <= 0:
        return []
    return select_sources(sources, max_sessions=min(len(sources), max_calls))


def llm_candidates(
    sources: list[SeedSource],
    *,
    project_path: str,
    max_calls: int,
    max_candidates: int,
    backend: str | None,
    focus_workstream: str | None = None,
    stats: dict[str, Any] | None = None,
) -> list[SeedCandidate]:
    max_calls = min(max_calls, HARD_MAX_LLM_CALLS_CEILING)
    if stats is not None:
        stats.update({
            "attempted": 0,
            "succeeded": 0,
            "failed": 0,
            "source_ids": [],
            "source_revision_tokens": [],
            "succeeded_source_ids": [],
            "failed_source_ids": [],
            "succeeded_source_revision_tokens": [],
            "failed_source_revision_tokens": [],
            "accepted_candidates_by_source": {},
            "accepted_candidates_by_revision": {},
            "budget_storage_unavailable": False,
        })
    if max_calls <= 0:
        return []
    import budget  # noqa: WPS433
    import model_backends  # noqa: WPS433

    out: list[SeedCandidate] = []
    call_sources = sources[:max_calls]
    total_calls = len(call_sources)
    for index, src in enumerate(call_sources, start=1):
        try:
            allowed, state = budget.check_and_record(
                project_path, category="nonheal",
            )
        except OSError:
            if stats is not None:
                stats["budget_storage_unavailable"] = True
            print(
                "LLM seed refinement stopped before model use: "
                "budget_storage_unavailable. Latch could not write its local "
                "budget state; grant access to the configured Latch data "
                "directory and rerun.",
                file=sys.stderr,
                flush=True,
            )
            break
        if not allowed:
            print(
                "LLM seed refinement stopped: latch non-heal budget cap reached "
                f"({state.get('count_nonheal')}/{budget.DEFAULT_NONHEAL_DAILY_CAP}).",
                file=sys.stderr,
                flush=True,
            )
            break
        print(
            f"Seed extraction progress: {index}/{total_calls} started.",
            file=sys.stderr,
            flush=True,
        )
        prompt = seed_prompt(
            project_path=project_path,
            source=src,
            focus_workstream=focus_workstream,
        )
        if stats is not None:
            stats["attempted"] += 1
            stats["source_ids"].append(src.id)
            stats["source_revision_tokens"].append(source_revision_token(src))
        result = model_backends.invoke_prompt(
            prompt,
            backend=backend,
            env_names=("LATCH_SEED_BACKEND", "LATCH_MODEL_BACKEND", "LATCH_GATE_BACKEND"),
            default="claude",
            timeout_s=240,
            purpose="seed refinement",
        )
        if result.error or not result.text:
            if stats is not None:
                stats["failed"] += 1
                stats["failed_source_ids"].append(src.id)
                stats["failed_source_revision_tokens"].append(
                    source_revision_token(src)
                )
            print(
                f"LLM seed refinement skipped source {index}/{total_calls}: "
                "extractor_failed "
                "(backend details withheld; retryable).",
                file=sys.stderr,
                flush=True,
            )
            print(
                f"Seed extraction progress: {index}/{total_calls} failed safely.",
                file=sys.stderr,
                flush=True,
            )
            continue
        safe_output, output_redactions = redact_seed_text(result.text)
        if stats is not None and output_redactions:
            stats["output_redactions"] = stats.get("output_redactions", 0) + output_redactions
        parsed = parse_json_envelope(safe_output)
        valid_envelope = (
            isinstance(parsed, dict) and isinstance(parsed.get("seed_candidates"), list)
        )
        items = parsed.get("seed_candidates", []) if valid_envelope else []
        accepted = 0
        for item in items:
            cand = candidate_from_llm_item(item, src)
            if cand:
                out.append(cand)
                accepted += 1
                if MAX_CANDIDATES_PER_SOURCE is not None \
                        and accepted >= MAX_CANDIDATES_PER_SOURCE:
                    break
        if stats is not None:
            if valid_envelope:
                stats["succeeded"] += 1
                stats["succeeded_source_ids"].append(src.id)
                stats["succeeded_source_revision_tokens"].append(
                    source_revision_token(src)
                )
                stats["accepted_candidates_by_source"][src.id] = accepted
                stats["accepted_candidates_by_revision"][
                    source_revision_token(src)
                ] = accepted
            else:
                stats["failed"] += 1
                stats["failed_source_ids"].append(src.id)
                stats["failed_source_revision_tokens"].append(
                    source_revision_token(src)
                )
        if valid_envelope:
            print(
                f"Seed extraction progress: {index}/{total_calls} completed; "
                f"accepted={accepted}.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"Seed extraction progress: {index}/{total_calls} failed safely.",
                file=sys.stderr,
                flush=True,
            )
    return balanced_candidate_selection(
        dedupe_candidates(out), max_candidates=max_candidates,
    )


def seed_prompt(
    *, project_path: str, source: SeedSource, focus_workstream: str | None = None,
) -> str:
    focus = ""
    if focus_workstream:
        safe_focus, _ = redact_seed_text(focus_workstream)
        focus = (
            "\nThis is a targeted new-workstream initialization. Extract only evidence "
            f"materially relevant to this user-supplied workstream: {safe_focus!r}. "
            "Use workstream_key=\"requested\" for every returned candidate. Skip "
            "otherwise durable but unrelated history.\n"
        )
    safe_project, _ = redact_seed_text(Path(project_path).name)
    safe_source_text, _ = redact_seed_text(source.text)
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
        f"{focus}"
        "Do NOT extract transient session bookkeeping: branch/worktree state, dirty "
        "files, commit/PR logistics, local path trivia, main fast-forwards, or "
        "temporary install/debug status unless it captures a durable product lesson. "
        "Do NOT extract machine-local settings churn such as permission bypasses, "
        "model effort changes, or global config edits unless the user framed it as "
        "a reusable preference. Do NOT extract meta-candidates about seed previews, "
        "candidate lists, or whether an assistant should mark a KB node verified. "
        "Do not infer private facts that are not stated. Decision-like candidates "
        "must preserve rejected-path rationale and reopen conditions when present. "
        "For every candidate carrying the rejected_path signal, also return a "
        "rejected_path field that names only the disallowed approach in affirmative "
        "language, without 'do not', 'never', 'avoid', or the governing replacement. "
        "For source_excerpt, copy one short exact supporting excerpt from the "
        "transcript; do not paraphrase or invent it. "
        "Example: rejected_path='sandboxed preview first, followed by an elevated retry'. "
        "Return JSON only with this shape:\n"
        '{"seed_candidates":[{"kind":"workstream|decision|preference|fact|idea|open_question",'
        '"title":"short title","body":"evidence-backed markdown body",'
        '"source_excerpt":"short exact excerpt copied from the transcript",'
        '"confidence":0.0,"signals":["decision","rejected_path"],'
        '"rejected_path":"affirmative description of disallowed approach",'
        '"workstream_key":"optional stable key matching a returned workstream"}]}\n\n'
        f"Project: {safe_project}\n"
        f"Source: {public_source_id(source.id)}; provider={source.agent}; "
        f"observed_at={source.mtime}; "
        f"digest={source.content_digest[:16]}\n\n"
        "--- TRANSCRIPT ---\n"
        f"{safe_source_text[-MAX_LLM_SOURCE_CHARS:]}"
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


MIN_CITED_EXCERPT_CHARS = 24
MAX_CITED_EXCERPT_CHARS = 600
ASSISTANT_EXCERPT_PREFIX = "assistant: "


def assistant_evidence_excerpt(
    cited: str, *, source: SeedSource, focus_terms: set[str], normalized,
) -> str:
    """Fallback evidence from assistant turns, explicitly labelled as such.

    User turns are only 0.1% of a transcript, so restricting evidence to them
    left most candidates citing an unrelated user line or nothing at all —
    measured, only 8.5% of shown excerpts actually supported their candidate.
    Assistant turns carry the reasoning most claims actually come from.

    This never competes with user evidence: the caller only reaches here when
    no user turn qualified, and a user correction that fails closed still
    suppresses the excerpt entirely. The `assistant: ` prefix keeps the receipt
    honest about who said it, so a reader never mistakes model reasoning for a
    user instruction.
    """
    best: tuple[float, int, str] | None = None
    cited_hit: tuple[int, str] | None = None
    in_assistant = False
    index = 0
    for raw in source.text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        match = ROLE_LINE_RE.match(raw)
        if match:
            role = match.group(1).split()[0].lower()
            in_assistant = role in ASSISTANT_EVIDENCE_ROLES
            if not in_assistant:
                continue
            line = match.group(2).strip()
        elif in_assistant:
            line = raw
        else:
            continue
        if should_skip_subject_candidate(line) or len(line) < 20:
            continue
        index += 1
        terms = {t for t in normalized(line).split() if len(t) > 3}
        shared = focus_terms & terms
        if len(shared) < 2:
            continue
        safe, _ = redact_seed_text(line)
        text = ASSISTANT_EXCERPT_PREFIX + clip(safe, 360)
        score = len(shared) / max(1, min(len(focus_terms), len(terms)))
        if cited and cited in WHITESPACE_RE.sub(" ", safe):
            if cited_hit is None or index > cited_hit[0]:
                cited_hit = (index, text)
        if best is None or (score, index) > (best[0], best[1]):
            best = (score, index, text)
    if cited_hit is not None:
        return cited_hit[1]
    return best[2] if best else ""


def verbatim_model_excerpt(value: Any, *, source: SeedSource) -> str:
    """Accept the model's cited excerpt only if it really is in the transcript.

    Matching is whitespace-insensitive because the model reflows what it
    copies, but it is otherwise exact — no paraphrase, no fuzzy overlap. The
    haystack is the already-redacted `source.text`, so a hit cannot reintroduce
    unredacted content.
    """
    if not isinstance(value, str):
        return ""
    excerpt = WHITESPACE_RE.sub(" ", value).strip()
    if len(excerpt) < MIN_CITED_EXCERPT_CHARS:
        return ""
    excerpt = excerpt[:MAX_CITED_EXCERPT_CHARS]
    haystack = WHITESPACE_RE.sub(" ", source.text)
    if excerpt not in haystack:
        return ""
    # Re-run redaction anyway: cheap, and keeps the guarantee local rather than
    # depending on source.text having been redacted upstream.
    safe, _ = redact_seed_text(excerpt)
    return safe


def grounded_source_excerpt(
    value: Any,
    *,
    source: SeedSource,
    title: str,
    body: str,
) -> str:
    """Return a redacted transcript-grounded excerpt, never model-only prose.

    Evidence is still re-selected from redacted user turns, and every existing
    invariant holds: assistant prose is never citable, a newer conflicting user
    correction still fails closed, and the relevance gate is unchanged.

    What changed is only WHICH surviving user turn is cited. Term-overlap
    ranking alone frequently picked a turn unrelated to the claim — measured
    against the restored KB, the excerpt shown failed to support its own
    candidate in the large majority of cases, which makes a seeded receipt
    actively misleading. So when the model names the turn it relied on and
    that turn independently clears every check, that turn is cited instead of
    the highest-overlap one. An unverifiable citation is ignored, not trusted.
    """

    def normalized(text: str) -> str:
        return re.sub(
            r"[^a-z0-9 ]+", " ",
            WHITESPACE_RE.sub(" ", text.lower()),
        ).strip()

    cited = verbatim_model_excerpt(value, source=source)

    focus_terms = {
        term for term in normalized(f"{title} {body}").split()
        if len(term) > 3
    }
    focus_candidate = SeedCandidate(
        kind="decision",
        title=title,
        body=body,
        confidence=0.0,
        signals=[],
        source_ids=[],
        source_paths=[],
    )
    focus_targets = candidate_choice_targets(focus_candidate)
    ranked_candidates: list[tuple[float, int, str]] = []
    cited_matches: list[tuple[int, str]] = []
    in_user_turn = False
    user_turn_index = 0
    for raw in source.text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        match = ROLE_LINE_RE.match(raw)
        if match:
            role = match.group(1).split()[0].lower()
            in_user_turn = role in USER_ROLES
            if not in_user_turn:
                continue
            candidate = match.group(2).strip()
        elif in_user_turn:
            candidate = raw
        else:
            continue
        if should_skip_user_candidate(candidate) or len(candidate) < 20:
            continue
        user_turn_index += 1
        normalized_candidate = normalized(candidate)
        candidate_terms = {
            term for term in normalized_candidate.split() if len(term) > 3
        }
        shared = focus_terms & candidate_terms
        if len(shared) < 2:
            continue
        safe_candidate, _ = redact_seed_text(candidate)
        evidence_candidate = SeedCandidate(
            kind="decision",
            title="",
            body=safe_candidate,
            confidence=0.0,
            signals=[],
            source_ids=[],
            source_paths=[],
        )
        evidence_targets = candidate_choice_targets(evidence_candidate)
        if focus_targets and not (focus_targets & evidence_targets):
            # A newer user correction to a different explicit choice makes
            # older support stale. Fail closed instead of citing that history.
            if candidates_directionally_conflict(
                focus_candidate, evidence_candidate,
            ):
                ranked_candidates.clear()
                ranked_candidates.append((-1.0, user_turn_index, ""))
            continue
        if candidates_directionally_conflict(
            focus_candidate, evidence_candidate,
        ):
            ranked_candidates.clear()
            ranked_candidates.append((-1.0, user_turn_index, ""))
            continue
        score = len(shared) / max(1, min(len(focus_terms), len(candidate_terms)))
        ranked_candidates.append((
            score,
            user_turn_index,
            clip(safe_candidate, 360),
        ))
        if cited and cited in WHITESPACE_RE.sub(" ", safe_candidate):
            cited_matches.append((score, user_turn_index, clip(safe_candidate, 360)))
    if not ranked_candidates:
        # No user turn supports this claim. Rather than show nothing, cite the
        # assistant turn it actually came from, clearly labelled.
        return assistant_evidence_excerpt(
            cited, source=source, focus_terms=focus_terms, normalized=normalized,
        )
    best = max(ranked_candidates, key=lambda item: (item[0], item[1]))
    if best[0] < 0:
        # Fail-closed on a newer conflicting user correction outranks the
        # model's citation AND any assistant text: the claim is stale, so it
        # gets no receipt at all rather than one that looks supportive.
        return ""
    if cited_matches:
        # The model told us which turn it relied on. Term-overlap ranking
        # regularly picks a different, unrelated turn, which is what made
        # seeded receipts fail to support their own claims. Honour the
        # citation, but only after it has cleared every check above:
        # user-turn origin, relevance, staleness, and redaction.
        cited_score, cited_index, cited_text = max(
            cited_matches, key=lambda item: (item[1],),
        )
        # Recency still outranks the citation. If the user restated or refined
        # the same point in a later turn that scores at least as well, cite
        # that one: a citation identifies the claim, not which phrasing of it
        # is current.
        superseding = [
            item for item in ranked_candidates
            if item[1] > cited_index and item[0] >= cited_score
        ]
        if superseding:
            return max(superseding, key=lambda item: (item[0], item[1]))[2]
        return cited_text
    return best[2]


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
    rejected_path = clip(str(item.get("rejected_path") or "").strip(), 180)
    workstream_key = normalize_workstream_key(item.get("workstream_key"))
    if kind == "workstream" and not workstream_key:
        workstream_key = normalize_workstream_key(title)
    if "rejected_path" in normalized_signals(signals) and not rejected_path:
        signals = [signal for signal in signals if signal.strip().lower() != "rejected_path"]
    if "llm_seed" not in signals:
        signals.append("llm_seed")
    if high_confidence_agent_mistake(signals) and confidence < AGENT_MISTAKE_MIN_CONFIDENCE:
        return None
    if llm_candidate_skip_reason(kind=kind, title=title, body=body_text, signals=signals):
        return None
    source_excerpt = grounded_source_excerpt(
        item.get("source_excerpt"),
        source=src,
        title=title,
        body=body_text,
    )
    rejected_path_block = f"\n\nRejected path:\n> {rejected_path}" if rejected_path else ""
    excerpt_block = (
        f"\n\nExcerpt:\n> {source_excerpt}" if source_excerpt else ""
    )
    body = (
        "Seed candidate from prior local agent history. Treat as low-authority "
        "staging evidence until reviewed/promoted.\n\n"
        f"{body_text}{rejected_path_block}\n\n"
        f"Signals: {', '.join(sorted(set(signals)))}\n\n"
        "Source evidence:\n"
        f"- {src.id}; observed_at={src.mtime}; digest={src.content_digest[:16]}"
        f"{excerpt_block}"
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
        source_mtimes=[src.mtime],
        source_digests=[src.content_digest],
        workstream_key=workstream_key,
        source_excerpts=[source_excerpt],
    )


def normalize_workstream_key(value: Any) -> str | None:
    raw = WHITESPACE_RE.sub(" ", str(value or "")).strip().lower()
    if not raw:
        return None
    normalized = re.sub(r"[^a-z0-9._:-]+", "-", raw).strip("-:._")
    return normalized[:80] or None


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
    merged: list[SeedCandidate] = []
    for cand in sorted(candidates, key=candidate_rank_score, reverse=True):
        existing = next(
            (item for item in merged if safe_candidates_equivalent(cand, item)),
            None,
        )
        if existing is None:
            merged.append(cand)
            continue
        merge_candidate_provenance(existing, cand)
    return sorted(merged, key=candidate_rank_score, reverse=True)


def merge_candidate_sets(
    llm: list[SeedCandidate],
    deterministic: list[SeedCandidate],
    *,
    max_candidates: int,
) -> list[SeedCandidate]:
    # LLM candidates lead in quality, while deterministic candidates can add
    # coverage or corroborating provenance. The public no-LLM boundary remains
    # enforced by choose_seed_candidates.
    return balanced_candidate_selection(
        dedupe_candidates([*llm, *deterministic]),
        max_candidates=max_candidates,
    )


def effective_max_candidates(args: argparse.Namespace) -> int:
    """Resolve the global candidate bound; an explicit --max-candidates wins.

    Left unset, the bound scales with the number of sessions actually read so
    removing the per-source cap does not simply move the truncation to a flat
    global limit. HARD_MAX_TOTAL_CANDIDATES keeps every run bounded.
    """
    explicit = getattr(args, "max_candidates", None)
    if explicit is not None:
        return int(explicit)
    sessions = int(getattr(args, "sources_selected", 0) or 0)
    scaled = max(DEFAULT_MAX_CANDIDATES, sessions * CANDIDATES_PER_SESSION_TARGET)
    return min(scaled, HARD_MAX_TOTAL_CANDIDATES)


def choose_seed_candidates(
    args: argparse.Namespace,
    llm: list[SeedCandidate],
    deterministic: list[SeedCandidate],
) -> tuple[list[SeedCandidate], bool]:
    """Pick report candidates while keeping the public LLM boundary honest."""
    if args.llm == "yes" and deterministic and not llm:
        return [], True
    return merge_candidate_sets(
        llm, deterministic, max_candidates=effective_max_candidates(args),
    ), False


def requested_workstream_key(title: str) -> str:
    digest = hashlib.sha256(
        normalize_for_quality_filter(title).encode("utf-8")
    ).hexdigest()[:16]
    return f"requested:{digest}"


def workstream_scope_key(args: argparse.Namespace) -> str:
    if getattr(args, "workstream_id", None):
        return f"existing:{int(args.workstream_id)}"
    if getattr(args, "new_workstream", None):
        return requested_workstream_key(str(args.new_workstream))
    return "project"


def new_workstream_candidate(title: str) -> SeedCandidate:
    key = requested_workstream_key(title)
    digest = hashlib.sha256(normalize_for_quality_filter(title).encode("utf-8")).hexdigest()
    safe_title, _ = redact_seed_text(clip(title, 120))
    body = (
        "User-requested workstream initialization. Treat this anchor as staging "
        "until reviewed/promoted; imported child judgments remain independently "
        "reviewable low-authority evidence.\n\n"
        f"Requested workstream: {safe_title}\n\n"
        "Signals: requested_workstream, ongoing_workstream\n\n"
        "Source evidence:\n"
        f"- user:new-workstream; observed_at=review-time; digest={digest[:16]}"
    )
    return SeedCandidate(
        kind="workstream",
        title=safe_title,
        body=body,
        confidence=0.95,
        signals=["requested_workstream", "ongoing_workstream"],
        source_ids=["user:new-workstream"],
        source_paths=[""],
        llm_used=False,
        source_mtimes=[""],
        source_digests=[digest],
        workstream_key=key,
    )


def apply_requested_workstream_scope(
    candidates: list[SeedCandidate], *, new_workstream: str | None,
    workstream_id: int | None, max_candidates: int | None = None,
) -> list[SeedCandidate]:
    if new_workstream:
        key = requested_workstream_key(new_workstream)
        # Targeted initialization relies on the model's relevance judgment;
        # deterministic regex fill can contain unrelated project-wide signals.
        targeted = [
            candidate for candidate in candidates
            if candidate.llm_used
            and candidate.kind != "workstream"
            and candidate.workstream_key == "requested"
        ]
        for candidate in targeted:
            candidate.workstream_key = key
        cap = max_candidates if max_candidates is not None else len(targeted) + 1
        parent = new_workstream_candidate(new_workstream)
        return [parent, *balanced_candidate_selection(
            targeted, max_candidates=max(0, cap - 1),
        )]
    if workstream_id:
        key = f"existing:{int(workstream_id)}"
        targeted = [
            candidate for candidate in candidates if candidate.kind != "workstream"
        ]
        for candidate in targeted:
            candidate.workstream_key = key
        return targeted
    return candidates


def sanitize_reserved_workstream_keys(
    candidates: list[SeedCandidate], *, existing_workstream_id: int | None,
) -> list[SeedCandidate]:
    """Reserve ``existing:ID`` for the user's explicit CLI target only."""
    allowed = (
        f"existing:{int(existing_workstream_id)}"
        if existing_workstream_id is not None else None
    )
    for candidate in candidates:
        key = candidate.workstream_key
        if candidate.kind != "workstream" and key \
                and key.startswith("existing:") and key != allowed:
            candidate.workstream_key = None
    return candidates


def resolve_unambiguous_workstream_links(
    candidates: list[SeedCandidate],
) -> list[SeedCandidate]:
    """Attach an unkeyed child only when provenance and topic resolve one parent.

    Source co-location alone is insufficient because one transcript can discuss
    several lanes. Duplicate parents for a key are ambiguous and remain for the
    apply boundary to reject without creating either parent.
    """
    parents_by_key: dict[str, list[SeedCandidate]] = {}
    for candidate in candidates:
        if candidate.kind != "workstream":
            continue
        if not candidate.workstream_key:
            candidate.workstream_key = normalize_workstream_key(candidate.title)
        if candidate.workstream_key:
            parents_by_key.setdefault(candidate.workstream_key, []).append(candidate)

    unique_parents = {
        key: values[0] for key, values in parents_by_key.items() if len(values) == 1
    }
    keys_by_source: dict[tuple[str, str], set[str]] = {}
    for key, parent in unique_parents.items():
        for ref in candidate_source_refs(parent):
            keys_by_source.setdefault((ref["id"], ref["digest"]), set()).add(key)

    generic_parent_terms = {
        "seeded", "continuity", "note", "workstream", "ongoing", "project",
        "requested", "initialization",
    }
    for child in candidates:
        if child.kind == "workstream" or child.workstream_key:
            continue
        resolved: list[str] = []
        for ref in candidate_source_refs(child):
            keys = keys_by_source.get((ref["id"], ref["digest"]), set())
            if len(keys) != 1:
                resolved = []
                break
            resolved.append(next(iter(keys)))
        if not resolved or len(set(resolved)) != 1:
            continue
        key = resolved[0]
        parent = unique_parents.get(key)
        if parent is None:
            continue
        parent_terms = candidate_terms(parent) - generic_parent_terms
        key_terms = {
            term for term in re.split(r"[^a-z0-9]+", key.lower())
            if len(term) > 3 and term not in generic_parent_terms
        }
        if not (candidate_terms(child) & (parent_terms | key_terms)):
            continue
        child.workstream_key = key
    return candidates


def candidates_overlap(a: SeedCandidate, b: SeedCandidate) -> bool:
    return safe_candidates_equivalent(a, b)


def candidate_core_text(
    candidate: SeedCandidate, *, persisted_body: bool = False,
) -> str:
    """Claim-bearing text only; treat only Latch-owned scaffold as structure."""
    body = candidate.body
    if persisted_body:
        # Only persisted nodes are known to contain Latch's trusted receipt.
        # The last receipt is generated by body_with_import_receipt; model text
        # cannot place content after it. Trim the adjacent source block too.
        receipt_at = body.rfind(
            "\n\nSeed import receipt:\n- Latch-Seed-Import-Key: "
        )
        if receipt_at >= 0:
            source_at = body.rfind(
                "\n\nSeed source receipts:\n", 0, receipt_at,
            )
            body = body[:source_at if source_at >= 0 else receipt_at]

    core = body
    trusted_wrapper = (
        body.startswith(f"{SEED_CANDIDATE_PREAMBLE}\n\nMode: ")
        and "\n\nWhy this helps:" in body
    )
    if trusted_wrapper:
        source_at = body.find("\n\nSource evidence:\n")
        excerpt_at = (
            body.find("\n\nExcerpt:\n> ", source_at)
            if source_at >= 0 else -1
        )
        if excerpt_at >= 0:
            core = body[excerpt_at + len("\n\nExcerpt:\n> "):]
    elif body.startswith(SEED_CANDIDATE_PREAMBLE):
        core = body[len(SEED_CANDIDATE_PREAMBLE):]
        core = core.split("\n\nSignals:", 1)[0]
        core = core.split("\n\nSource evidence:", 1)[0]
    return WHITESPACE_RE.sub(" ", core).strip()


def durable_signal_set(candidate: SeedCandidate) -> set[str]:
    return normalized_signals(candidate.signals) - {
        "llm_seed", "deterministic_seed", "seed", "corroborated",
    }


def candidate_negation_signature(candidate: SeedCandidate) -> tuple[bool, bool]:
    text = normalize_for_quality_filter(candidate.title + " " + candidate_core_text(candidate))
    negated = bool(re.search(r"\b(no|not|never|avoid|reject(?:ed)?|ruled out|don'?t)\b", text))
    rejected = "rejected_path" in normalized_signals(candidate.signals)
    return rejected, negated


def candidate_term_polarities(candidate: SeedCandidate) -> dict[str, str]:
    """Extract only explicit local direction around durable claim terms.

    This is intentionally narrow: it exists to prevent destructive semantic
    merges such as "use SQLite, not Postgres" with the inverse ruling. Unknown
    terms remain unclassified and therefore cannot create authority.
    """
    text = normalize_for_quality_filter(
        candidate_core_text(candidate)
    ).replace("don't", "dont")
    tokens = re.findall(r"[a-z0-9]+", text)
    durable_terms = candidate_terms(candidate)
    negative_before = {
        "not", "never", "dont", "avoid", "avoiding", "reject", "rejected",
        "without", "forbid", "forbidden", "disallow", "disallowed",
    }
    negative_after = {"forbidden", "disallowed", "rejected"}
    positive = {"use", "choose", "keep", "prefer", "adopt", "require", "required"}
    out: dict[str, str] = {}
    for idx, token in enumerate(tokens):
        if token not in durable_terms:
            continue
        before = set(tokens[max(0, idx - 3):idx])
        after = set(tokens[idx + 1:idx + 3])
        is_negative = bool(before & negative_before or after & negative_after)
        if "over" in before or ("instead" in before and "of" in before):
            is_negative = True
        is_positive = bool(before & positive or after & {"required"})
        polarity = "negative" if is_negative else "positive" if is_positive else ""
        if not polarity:
            continue
        previous = out.get(token)
        if previous is None:
            out[token] = polarity
        elif previous != polarity:
            out.pop(token, None)
    return out


def candidate_choice_targets(candidate: SeedCandidate) -> set[str]:
    """Extract a single-word target from an explicit technology choice."""
    text = normalize_for_quality_filter(
        candidate.title + " " + candidate_core_text(candidate)
    )
    tokens = re.findall(r"[a-z0-9]+", text)
    choice_verbs = {"use", "choose", "prefer", "adopt", "select"}
    target_fillers = {
        "a", "an", "the", "our", "this", "that", "new", "existing",
        "local", "remote", "managed", "embedded", "self", "hosted",
    }
    targets: set[str] = set()
    for idx, token in enumerate(tokens):
        if token not in choice_verbs:
            continue
        for target in tokens[idx + 1:idx + 5]:
            if target in target_fillers:
                continue
            targets.add(target)
            break
    return targets


def _has_opposing_action_stance(a: SeedCandidate, b: SeedCandidate) -> bool:
    left = set(re.findall(
        r"[a-z0-9]+",
        normalize_for_quality_filter(
            a.title + " " + candidate_core_text(a)
        ),
    ))
    right = set(re.findall(
        r"[a-z0-9]+",
        normalize_for_quality_filter(
            b.title + " " + candidate_core_text(b)
        ),
    ))
    action_pairs = (
        (
            {"enable", "enabled", "enabling", "activate", "activated", "activating"},
            {"disable", "disabled", "disabling", "deactivate", "deactivated", "deactivating"},
        ),
        (
            {"allow", "allows", "allowed", "allowing", "permit", "permits", "permitted"},
            {"disallow", "disallows", "disallowed", "forbid", "forbids", "forbidden", "prohibit", "prohibited"},
        ),
    )
    for positive, negative in action_pairs:
        left_positive, left_negative = bool(left & positive), bool(left & negative)
        right_positive, right_negative = bool(right & positive), bool(right & negative)
        if left_positive != left_negative and right_positive != right_negative:
            if left_positive != right_positive:
                return True
    return False


def _has_opposing_lexical_stance(a: SeedCandidate, b: SeedCandidate) -> bool:
    left_text = normalize_for_quality_filter(
        a.title + " " + candidate_core_text(a)
    )
    right_text = normalize_for_quality_filter(
        b.title + " " + candidate_core_text(b)
    )
    left_tokens = set(re.findall(r"[a-z0-9]+", left_text))
    right_tokens = set(re.findall(r"[a-z0-9]+", right_text))
    token_pairs = (
        ({"public"}, {"private"}),
        (
            {"manual", "manually"},
            {"automatic", "automated", "automatically"},
        ),
        ({"required", "mandatory"}, {"optional"}),
        (
            {"retain", "retained", "retaining", "keep", "kept"},
            {
                "delete", "deleted", "deleting", "remove", "removed",
                "removing", "drop", "dropped", "dropping",
            },
        ),
    )
    for positive, negative in token_pairs:
        left_positive, left_negative = (
            bool(left_tokens & positive), bool(left_tokens & negative)
        )
        right_positive, right_negative = (
            bool(right_tokens & positive), bool(right_tokens & negative)
        )
        if left_positive != left_negative and right_positive != right_negative:
            if left_positive != right_positive:
                return True

    read_only = re.compile(r"\bread(?:\s*[-/]\s*|\s+)only\b|\breadonly\b")
    read_write = re.compile(
        r"\bread(?:\s*[-/]\s*|\s+(?:and\s+)?)write\b"
        r"|\breadwrite\b|\bwriteable\b|\bwritable\b"
    )
    left_only, left_write = bool(read_only.search(left_text)), bool(
        read_write.search(left_text)
    )
    right_only, right_write = bool(read_only.search(right_text)), bool(
        read_write.search(right_text)
    )
    return (
        left_only != left_write
        and right_only != right_write
        and left_only != right_only
    )


CLAIM_PIVOT_TERMS = {
    "allow": "allow", "allowed": "allow", "allows": "allow",
    "always": "always",
    "block": "block", "blocked": "block", "blocks": "block",
    "deny": "deny", "denied": "deny", "denies": "deny",
    "disable": "disable", "disabled": "disable", "disables": "disable",
    "disabling": "disable",
    "enable": "enable", "enabled": "enable", "enables": "enable",
    "enabling": "enable",
    "never": "never",
    "off": "off",
    "on": "on",
    "read": "read",
    "write": "write",
}


def _claim_pivot_sequence(text: str) -> tuple[str, ...]:
    """Ordered claim pivots (numbers and polarity verbs) for binding checks."""
    raw_tokens = _semantic_raw_tokens(text)
    pivots: list[str] = []
    for index, token in enumerate(raw_tokens):
        previous = raw_tokens[index - 1] if index else ""
        if re.fullmatch(r"v?\d+(?:[./]\d+)*", token):
            pivots.append(token)
            continue
        if token == "on" and previous in {
            "based", "depend", "depends", "relies", "rely",
        }:
            continue
        canonical = CLAIM_PIVOT_TERMS.get(token)
        if canonical is not None:
            pivots.append(canonical)
    return tuple(pivots)


def _has_crossed_pivot_bindings(a: SeedCandidate, b: SeedCandidate) -> bool:
    """Same pivot multiset in a different order reads as a crossed binding.

    Catches order-blind false equivalence such as "raise timeout from 30 to
    60" vs "from 60 to 30" or scoped enable/disable inversions, which share
    an identical token set and defeat unordered comparisons.
    """
    for left_text, right_text in (
        (a.title, b.title),
        (candidate_core_text(a), candidate_core_text(b)),
    ):
        left = _claim_pivot_sequence(left_text)
        right = _claim_pivot_sequence(right_text)
        if left and right and left != right and sorted(left) == sorted(right):
            return True
    return False


def candidates_directionally_conflict(a: SeedCandidate, b: SeedCandidate) -> bool:
    if _has_crossed_pivot_bindings(a, b):
        return True
    left = candidate_term_polarities(a)
    right = candidate_term_polarities(b)
    if any(
        left[term] != right[term]
        for term in left.keys() & right.keys()
    ):
        return True
    if _has_opposing_action_stance(a, b):
        return True
    if _has_opposing_lexical_stance(a, b):
        return True

    left_targets = candidate_choice_targets(a)
    right_targets = candidate_choice_targets(b)
    if len(left_targets) != 1 or len(right_targets) != 1:
        return False
    if left_targets == right_targets:
        return False
    left_context = candidate_terms(a) - left_targets
    right_context = candidate_terms(b) - right_targets
    shared_context = left_context & right_context
    context_overlap = (
        len(shared_context) / min(len(left_context), len(right_context))
        if left_context and right_context else 0.0
    )
    return len(shared_context) >= 2 and context_overlap >= 0.60


def safe_candidates_equivalent(a: SeedCandidate, b: SeedCandidate) -> bool:
    """Conservative semantic dedup; conflicting polarity always stays separate."""
    if report_section_key(a) != report_section_key(b):
        return False
    if a.workstream_key and b.workstream_key and a.workstream_key != b.workstream_key:
        return False
    if candidate_negation_signature(a) != candidate_negation_signature(b):
        return False
    if candidates_directionally_conflict(a, b):
        return False
    a_signals, b_signals = durable_signal_set(a), durable_signal_set(b)
    if a_signals and b_signals and not (a_signals & b_signals):
        return False
    # Compare the full normalized claim, not the 220-char excerpt key: claims
    # that diverge only past the truncation point must not merge as exact.
    exact_a = _normalized_claim_text(a)
    exact_b = _normalized_claim_text(b)
    if exact_a == exact_b:
        return True
    anchor_a = candidate_semantic_anchor_terms(a)
    anchor_b = candidate_semantic_anchor_terms(b)
    if not anchor_a or anchor_a != anchor_b:
        return False
    a_terms, b_terms = candidate_terms(a), candidate_terms(b)
    if not a_terms or not b_terms:
        return False
    overlap = len(a_terms & b_terms) / min(len(a_terms), len(b_terms))
    threshold = 0.72 if candidate_revision_keys(a) & candidate_revision_keys(b) else 0.82
    return overlap >= threshold


def candidate_source_refs(candidate: SeedCandidate) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for idx, source_id in enumerate(candidate.source_ids):
        refs.append({
            "id": source_id,
            "path": candidate.source_paths[idx] if idx < len(candidate.source_paths) else "",
            "mtime": candidate.source_mtimes[idx] if idx < len(candidate.source_mtimes) else "",
            "digest": candidate.source_digests[idx] if idx < len(candidate.source_digests) else "",
            "excerpt": (
                candidate.source_excerpts[idx]
                if idx < len(candidate.source_excerpts) else ""
            ),
        })
    return refs


def candidate_revision_keys(candidate: SeedCandidate) -> set[tuple[str, str]]:
    return {
        (seed_source_identity(ref["id"]), ref["digest"])
        for ref in candidate_source_refs(candidate)
    }


def merge_candidate_provenance(target: SeedCandidate, incoming: SeedCandidate) -> None:
    refs = {
        (seed_source_identity(ref["id"]), ref["digest"]): ref
        for ref in candidate_source_refs(target)
    }
    for ref in candidate_source_refs(incoming):
        revision = (seed_source_identity(ref["id"]), ref["digest"])
        current = refs.get(revision)
        if current is None:
            refs[revision] = ref
        else:
            for key in ("path", "mtime", "digest", "excerpt"):
                if not current.get(key) and ref.get(key):
                    current[key] = ref[key]
    ordered = [refs[key] for key in sorted(refs)]
    target.source_ids = [ref["id"] for ref in ordered]
    target.source_paths = [ref["path"] for ref in ordered]
    target.source_mtimes = [ref["mtime"] for ref in ordered]
    target.source_digests = [ref["digest"] for ref in ordered]
    target.source_excerpts = [ref["excerpt"] for ref in ordered]
    target.signals = sorted(set(target.signals) | set(incoming.signals) | {"corroborated"})
    target.confidence = min(0.95, max(target.confidence, incoming.confidence) + 0.03)
    target.llm_used = target.llm_used or incoming.llm_used
    if not target.workstream_key:
        target.workstream_key = incoming.workstream_key
    target.body = refresh_candidate_provenance(target)


def refresh_candidate_provenance(candidate: SeedCandidate) -> str:
    marker = "Source evidence:\n"
    if marker not in candidate.body:
        return candidate.body
    prefix, rest = candidate.body.split(marker, 1)
    suffix = ""
    if "\n\nExcerpt:" in rest:
        suffix = "\n\nExcerpt:" + rest.split("\n\nExcerpt:", 1)[1]
    lines = "\n".join(
        f"- {ref['id']}; observed_at={ref['mtime'] or 'unknown'}; "
        f"digest={(ref['digest'][:16] if ref['digest'] else 'unknown')}"
        for ref in candidate_source_refs(candidate)
    )
    return prefix + marker + lines + suffix


def candidate_rank_score(candidate: SeedCandidate) -> tuple[float, float, int, float, str]:
    signals = normalized_signals(candidate.signals)
    kind_weight = {
        "decision": 5.0,
        "preference": 4.0,
        "workstream": 3.5,
        "open_question": 2.5,
        "idea": 2.0,
        "fact": 1.5,
    }.get(candidate.kind, 1.0)
    signal_weight = 0.0
    for signal, weight in {
        "rejected_path": 4.0,
        "decision": 3.0,
        "correction": 2.8,
        "preference": 2.3,
        "ongoing_workstream": 1.8,
        "open_question": 1.0,
        "possible_agent_mistake": 1.5,
    }.items():
        if signal in signals:
            signal_weight += weight
    revision_count = len(candidate_revision_keys(candidate))
    corroboration = min(max(revision_count - 1, 0), 4) * 0.8
    model_bonus = 0.25 if candidate.llm_used else 0.0
    recency = latest_candidate_observed_at(candidate) or ""
    return (
        kind_weight + signal_weight + corroboration + model_bonus,
        candidate.confidence,
        revision_count,
        model_bonus,
        recency,
    )


def balanced_candidate_selection(
    candidates: list[SeedCandidate], *, max_candidates: int,
) -> list[SeedCandidate]:
    if max_candidates <= 0:
        return []
    ranked = sorted(candidates, key=candidate_rank_score, reverse=True)
    selected: list[SeedCandidate] = []
    selected_ids: set[int] = set()
    # Preserve one strongest item from every non-empty report section before a
    # single abundant class consumes the cap.
    for key, _title, _summary in REPORT_SECTION_DEFS:
        candidate = next((item for item in ranked if report_section_key(item) == key), None)
        if candidate is None:
            continue
        selected.append(candidate)
        selected_ids.add(id(candidate))
        if len(selected) >= max_candidates:
            return selected
    for candidate in ranked:
        if id(candidate) in selected_ids:
            continue
        selected.append(candidate)
        if len(selected) >= max_candidates:
            break
    return selected


def candidate_terms(candidate: SeedCandidate) -> set[str]:
    text = normalize_excerpt(candidate.title + " " + candidate_core_text(candidate))
    stop = {
        "seed", "seeded", "candidate", "local", "agent", "history", "treat",
        "staging", "evidence", "confidence", "signals", "source", "from",
        "prior", "with", "this", "that", "because", "before", "after",
        "depend", "depends", "dependency", "prerequisite", "prerequisites",
    }
    return {w for w in text.split() if len(w) > 3 and w not in stop}


SEMANTIC_ANCHOR_TOKEN_RE = re.compile(
    r"v\d+(?:\.\d+)*|\d+(?:[./]\d+)*|[a-z]+"
)
SEMANTIC_SHORT_ANCHORS = {
    "ai", "api", "db", "go", "js", "ml", "off", "on", "ssl", "tls", "ui",
}
SEMANTIC_ANCHOR_FRAMING = {
    "a", "about", "adopt", "adopted", "agent", "an", "candidate",
    "choose", "chosen", "decide", "decided", "decision", "decisions",
    "depend", "dependent", "depends", "evidence", "excerpt", "expose",
    "for", "from", "have", "into", "is", "keep", "kept", "local", "make",
    "must", "path", "prefer", "prerequisite", "prerequisites", "project",
    "reject", "rejected", "rejection", "require", "requires", "required",
    "ruled", "seed", "seeded", "select", "selected", "should", "staging",
    "stated", "that", "the", "through", "use", "user", "uses", "using",
    "with",
}
SEMANTIC_ANCHOR_CANONICAL = {
    "alter": "change",
    "altered": "change",
    "alters": "change",
    "approvals": "approval",
    "changed": "change",
    "changes": "change",
    "changing": "change",
    "deployments": "deployment",
    "endpoints": "endpoint",
    "modified": "change",
    "modifies": "change",
    "modify": "change",
    "reporting": "report",
    "reports": "report",
}


def _semantic_raw_tokens(text: str) -> list[str]:
    normalized = normalize_for_quality_filter(text)
    normalized = re.sub(
        r"\bread[\s/-]*only\b|\breadonly\b", " read only ", normalized,
    )
    normalized = re.sub(
        r"\bread[\s/-]*(?:and[\s/-]*)?write\b|\breadwrite\b",
        " read write ",
        normalized,
    )
    return SEMANTIC_ANCHOR_TOKEN_RE.findall(normalized)


def _semantic_anchor_value(
    token: str,
    *,
    previous: str = "",
) -> str | None:
    if token == "on" and previous in {
        "depend", "depends", "relies", "rely",
    }:
        return None
    if token in SEMANTIC_ANCHOR_FRAMING:
        return None
    if (
        len(token) <= 3
        and token not in SEMANTIC_SHORT_ANCHORS
        and not re.fullmatch(r"v?\d+(?:[./]\d+)*", token)
    ):
        return None
    return SEMANTIC_ANCHOR_CANONICAL.get(token, token)


def _nearest_semantic_anchor(
    raw_tokens: list[str],
    start: int,
    step: int,
) -> str | None:
    index = start
    while 0 <= index < len(raw_tokens):
        previous = raw_tokens[index - 1] if index else ""
        value = _semantic_anchor_value(
            raw_tokens[index], previous=previous,
        )
        if value not in {"after", "before"} and value is not None:
            return value
        index += step
    return None


def _semantic_relation_anchors(text: str) -> set[str]:
    raw_tokens = _semantic_raw_tokens(text)
    relations: set[str] = set()
    for index, token in enumerate(raw_tokens):
        if token in {"before", "after"}:
            left = _nearest_semantic_anchor(raw_tokens, index - 1, -1)
            right = _nearest_semantic_anchor(raw_tokens, index + 1, 1)
            if left and right:
                earlier, later = (
                    (left, right) if token == "before" else (right, left)
                )
                relations.add(f"order:{earlier}>{later}")
        if token in {"depend", "depends"} \
                and index + 1 < len(raw_tokens) \
                and raw_tokens[index + 1] == "on":
            dependent = _nearest_semantic_anchor(
                raw_tokens, index - 1, -1,
            )
            prerequisite = _nearest_semantic_anchor(
                raw_tokens, index + 2, 1,
            )
            if dependent and prerequisite:
                relations.add(
                    f"dependency:{dependent}>{prerequisite}"
                )
        if token == "prerequisite":
            try:
                for_index = raw_tokens.index("for", index + 1)
            except ValueError:
                continue
            prerequisite = _nearest_semantic_anchor(
                raw_tokens, index - 1, -1,
            )
            dependent = _nearest_semantic_anchor(
                raw_tokens, for_index + 1, 1,
            )
            if dependent and prerequisite:
                relations.add(
                    f"dependency:{dependent}>{prerequisite}"
                )
    return relations


def _semantic_assignment_anchors(text: str) -> set[str]:
    """Bind repeated choice targets to their roles without broad NLP."""
    normalized = normalize_for_quality_filter(text)
    match = re.search(
        r"\b(?:adopt|choose|prefer|select|use)\b(?P<tail>.+)",
        normalized,
    )
    if match is None:
        return set()
    assignments: list[tuple[str, str]] = []
    for raw_segment in re.split(r"\band\b|[,;]", match.group("tail")):
        segment = re.sub(
            r"^\s*(?:adopt|choose|prefer|select|use)\s+", "",
            raw_segment,
        ).strip()
        pair = re.match(
            r"(?P<target>.+?)\s+(?:as|for)\s+(?P<role>.+)$",
            segment,
        )
        if pair is None:
            continue
        target_terms = [
            value for index, token in enumerate(
                _semantic_raw_tokens(pair.group("target"))
            )
            if (
                value := _semantic_anchor_value(
                    token,
                    previous=(
                        _semantic_raw_tokens(pair.group("target"))[index - 1]
                        if index else ""
                    ),
                )
            ) is not None
        ]
        role_terms = {
            value for index, token in enumerate(
                _semantic_raw_tokens(pair.group("role"))
            )
            if (
                value := _semantic_anchor_value(
                    token,
                    previous=(
                        _semantic_raw_tokens(pair.group("role"))[index - 1]
                        if index else ""
                    ),
                )
            ) is not None
        }
        if target_terms and role_terms:
            assignments.append((
                target_terms[-1],
                "|".join(sorted(role_terms)),
            ))
    if len(assignments) < 2:
        return set()
    return {
        f"assignment:{target}>{role}" for target, role in assignments
    }


def candidate_semantic_anchor_terms(candidate: SeedCandidate) -> set[str]:
    """Normalize claim-bearing anchors whose mismatch must block merging."""
    anchors: set[str] = set()
    for text in (candidate.title, candidate_core_text(candidate)):
        raw_tokens = _semantic_raw_tokens(text)
        for index, token in enumerate(raw_tokens):
            previous = raw_tokens[index - 1] if index else ""
            value = _semantic_anchor_value(token, previous=previous)
            if value is not None:
                anchors.add(value)
        anchors.update(_semantic_relation_anchors(text))
        anchors.update(_semantic_assignment_anchors(text))
    return anchors


def candidate_review_id(candidate: SeedCandidate) -> str:
    payload = {
        "kind": candidate.kind,
        "title": normalize_for_quality_filter(candidate.title),
        "claim": normalize_for_quality_filter(candidate_core_text(candidate)),
        "signals": sorted(durable_signal_set(candidate)),
        "workstream_key": candidate.workstream_key,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "cand-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _review_terms(text: str) -> set[str]:
    stop = {
        "about", "after", "before", "candidate", "evidence", "from", "have",
        "into", "project", "seed", "seeded", "should", "that", "their",
        "there", "these", "this", "with",
    }
    normalized = re.sub(
        r"[^a-z0-9]+", " ", WHITESPACE_RE.sub(" ", text.lower())
    )
    return {
        term for term in normalized.split()
        if len(term) > 3 and term not in stop
    }


def _term_overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


NUMERIC_ANCHOR_RE = re.compile(r"v?\d+(?:[./]\d+)*")


def _numeric_anchor_terms(anchors: set[str]) -> set[str]:
    return {term for term in anchors if NUMERIC_ANCHOR_RE.fullmatch(term)}


def _relation_anchor_sets(anchors: set[str]) -> dict[str, set[str]]:
    relations: dict[str, set[str]] = {}
    for anchor in anchors:
        prefix, _, payload = anchor.partition(":")
        if prefix in {"order", "dependency", "assignment"} and ">" in payload:
            relations.setdefault(prefix, set()).add(payload)
    return relations


def _candidate_pivot_set(candidate: SeedCandidate) -> set[str]:
    return set(_claim_pivot_sequence(candidate.title)) | set(
        _claim_pivot_sequence(candidate_core_text(candidate))
    )


def _substantive_substitution(a: SeedCandidate, b: SeedCandidate) -> bool:
    """Near-identical texts differing by one or two content words oppose.

    "encrypted transport" vs "plaintext transport" style swaps share almost
    all vocabulary; a genuine cross-session paraphrase differs much more
    broadly. Morphological variants (raise/raised, report/reporting) pair off
    by shared stem and do not count as substitutions.
    """
    def substantive(term: str, others: set[str]) -> bool:
        if term in SEMANTIC_ANCHOR_FRAMING:
            return False
        return not any(term[:4] == other[:4] for other in others)

    def canonical_terms(text: str) -> set[str]:
        return {
            SEMANTIC_ANCHOR_CANONICAL.get(term, term)
            for term in _review_terms(text)
        }

    # Judge the claim text only: titles are short generated summaries where
    # benign adverb variation (genuinely/strictly) is routine, while the claim
    # is what a merge would actually write.
    left = canonical_terms(candidate_core_text(a))
    right = canonical_terms(candidate_core_text(b))
    only_left = {t for t in left - right if substantive(t, right)}
    only_right = {t for t in right - left if substantive(t, left)}
    return 0 < len(only_left) <= 2 and 0 < len(only_right) <= 2


def _semantic_anchors_conflict(a: SeedCandidate, b: SeedCandidate) -> bool:
    """Block review grouping when claim-bearing specifics disagree.

    Cross-session paraphrases never share identical vocabulary, so demanding
    full anchor-set equality made the paraphrase path unreachable in practice.
    Instead, refuse grouping exactly when the anchors that carry claim
    substance disagree: differing numbers/versions, differing polarity pivots
    (on/off, enable/disable), ordering/dependency/assignment relations that
    do not line up, or a small substantive word substitution in otherwise
    matching text.
    """
    anchors_a = candidate_semantic_anchor_terms(a)
    anchors_b = candidate_semantic_anchor_terms(b)
    if _numeric_anchor_terms(anchors_a) != _numeric_anchor_terms(anchors_b):
        return True
    if _candidate_pivot_set(a) != _candidate_pivot_set(b):
        return True
    left_relations = _relation_anchor_sets(anchors_a)
    right_relations = _relation_anchor_sets(anchors_b)
    if any(
        left_relations[prefix] != right_relations[prefix]
        for prefix in left_relations.keys() & right_relations.keys()
    ):
        return True
    return _substantive_substitution(a, b)


def _workstream_keys_review_compatible(
    left: str | None, right: str | None,
) -> bool:
    """Tolerate LLM slug drift for the same workstream in review grouping.

    Keys are model-authored strings, so the same lane routinely gets
    different slugs across sessions (observed live:
    pr46-governed-workstream-lifecycle vs pr46-lifecycle-automation).
    Review-only: the write-path dedupe keeps exact-key strictness.
    """
    if not left or not right or left == right:
        return True
    left_tokens = {t for t in re.split(r"[^a-z0-9]+", left.lower()) if t}
    right_tokens = {t for t in re.split(r"[^a-z0-9]+", right.lower()) if t}
    if not left_tokens or not right_tokens:
        return False
    shared = left_tokens & right_tokens
    overlap = len(shared) / min(len(left_tokens), len(right_tokens))
    return len(shared) >= 2 and overlap > 0.5


def review_cluster_compatible(a: SeedCandidate, b: SeedCandidate) -> bool:
    """Looser review-only paraphrase grouping with the write safety rails."""
    if a.kind != b.kind or report_section_key(a) != report_section_key(b):
        return False
    if not _workstream_keys_review_compatible(
        a.workstream_key, b.workstream_key,
    ):
        return False
    if candidate_negation_signature(a) != candidate_negation_signature(b):
        return False
    if candidates_directionally_conflict(a, b):
        return False
    a_signals, b_signals = durable_signal_set(a), durable_signal_set(b)
    if a_signals and b_signals and not (a_signals & b_signals):
        return False
    if safe_candidates_equivalent(a, b):
        return True
    if _semantic_anchors_conflict(a, b):
        return False

    title_left = _review_terms(a.title)
    title_right = _review_terms(b.title)
    claim_left = _review_terms(candidate_core_text(a))
    claim_right = _review_terms(candidate_core_text(b))
    title_shared = len(title_left & title_right)
    claim_shared = len(claim_left & claim_right)
    title_overlap = _term_overlap(title_left, title_right)
    claim_overlap = _term_overlap(claim_left, claim_right)
    exact_title = normalize_excerpt(a.title) == normalize_excerpt(b.title)
    return (
        title_shared >= 2
        and claim_shared >= 2
        and title_overlap >= 0.60
        and claim_overlap >= 0.45
    ) or (
        exact_title
        and claim_shared >= 2
        and claim_overlap >= 0.35
    ) or (
        # Near-superset titles (one title extends the other) earn the same
        # looser claim bar as exact titles: the opposition guards above, not
        # these similarity floors, are the never-merge defense.
        title_shared >= 4
        and title_overlap >= 0.80
        and claim_shared >= 2
        and claim_overlap >= 0.35
    )


def review_cluster_id(items: list[SeedCandidate]) -> str:
    payload = {
        "section": report_section_key(items[0]) if items else "",
        "candidate_ids": sorted(candidate_review_id(item) for item in items),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "cluster-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def build_review_clusters(
    candidates: list[SeedCandidate],
) -> list[SeedReviewCluster]:
    groups: list[list[SeedCandidate]] = []
    for candidate in sorted(candidates, key=candidate_review_id):
        group = next(
            (
                existing for existing in groups
                if all(
                    review_cluster_compatible(candidate, member)
                    for member in existing
                )
            ),
            None,
        )
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)
    clusters = [
        SeedReviewCluster(
            cluster_id=review_cluster_id(items),
            section_key=report_section_key(items[0]),
            items=items,
        )
        for items in groups
    ]
    return sorted(clusters, key=lambda cluster: cluster.cluster_id)


def review_cluster_representative(cluster: SeedReviewCluster) -> SeedCandidate:
    """Choose a stable representative independent of source recency/ranking."""
    return sorted(
        cluster.items,
        key=lambda candidate: (
            0 if candidate.llm_used else 1,
            normalize_for_quality_filter(candidate.title),
            candidate_review_id(candidate),
        ),
    )[0]


def merge_review_cluster(cluster: SeedReviewCluster) -> SeedCandidate:
    source_representative = review_cluster_representative(cluster)
    representative = copy.deepcopy(source_representative)
    for candidate in cluster.items:
        if candidate is source_representative:
            continue
        merge_candidate_provenance(representative, candidate)
    return representative


def whole_report_approval(candidates: list[SeedCandidate]) -> list[SeedCandidate]:
    """Approve every item with the same cluster semantics the report shows.

    The report promises that approving a duplicate cluster writes one stable
    representative with combined provenance; whole-report approval must not
    silently downgrade that into one staging node per variant.
    """
    return [
        merge_review_cluster(cluster)
        for cluster in build_review_clusters(candidates)
    ]


def candidate_observation_window(
    candidate: SeedCandidate,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    observed: list[datetime] = []
    for value in candidate.source_mtimes:
        if not str(value).strip():
            continue
        try:
            observed.append(
                datetime.fromisoformat(
                    normalize_seed_observed_at(value)
                )
            )
        except ValueError:
            continue
    if not observed:
        return {
            "oldest_observed_at": None,
            "latest_observed_at": None,
            "age_days": None,
            "freshness_note": (
                "Observation time unavailable; verify current authority before approval."
            ),
        }
    oldest = min(observed)
    latest = max(observed)
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_days = max(0, (current.astimezone(timezone.utc) - latest).days)
    return {
        "oldest_observed_at": oldest.isoformat(),
        "latest_observed_at": latest.isoformat(),
        "age_days": age_days,
        "freshness_note": (
            f"Latest evidence is {age_days} day(s) old; seed evidence is "
            "historical and must be checked against newer decisions before promotion."
        ),
    }


def candidate_review_claim(candidate: SeedCandidate) -> str:
    claim, _ = redact_seed_text(candidate_core_text(candidate))
    return clip(claim, REVIEW_BODY_CHARS)


def candidate_review_rationale(candidate: SeedCandidate) -> str:
    signals = normalized_signals(candidate.signals)
    source_count = len(candidate_revision_keys(candidate))
    corroboration = (
        f" {source_count} distinct source revisions corroborate it."
        if source_count > 1 else ""
    )
    if signals & AGENT_MISTAKE_SIGNALS:
        purpose = "Future gates can check for a previously observed alignment failure."
    elif "rejected_path" in signals:
        purpose = "Future agents can avoid reviving a previously rejected approach."
    elif candidate.kind == "decision":
        purpose = "Future agents can preserve an explicit prior judgment."
    elif candidate.kind == "preference":
        purpose = "Future agents can preserve a reusable working preference."
    elif candidate.kind == "workstream":
        purpose = "Future agents can reconnect related decisions and follow-up work."
    else:
        purpose = "Future agents can compare new work with this prior project evidence."
    return purpose + corroboration


def resolve_approval_selection(
    candidates: list[SeedCandidate],
    *,
    candidate_ids: list[str] | tuple[str, ...] = (),
    cluster_ids: list[str] | tuple[str, ...] = (),
) -> tuple[list[SeedCandidate], bool]:
    """Resolve exact review IDs and report whether every item was covered."""
    clusters = build_review_clusters(candidates)
    candidate_map = {
        candidate_review_id(candidate): candidate for candidate in candidates
    }
    cluster_map = {cluster.cluster_id: cluster for cluster in clusters}
    normalized_candidates = [
        str(value).strip() for value in candidate_ids if str(value).strip()
    ]
    normalized_clusters = [
        str(value).strip() for value in cluster_ids if str(value).strip()
    ]
    if (candidate_ids or cluster_ids) \
            and not normalized_candidates and not normalized_clusters:
        raise SeedApprovalError("approval selectors must contain a non-empty review ID")
    if len(normalized_candidates) != len(set(normalized_candidates)) \
            or len(normalized_clusters) != len(set(normalized_clusters)):
        raise SeedApprovalError("approval selectors must not contain duplicate review IDs")
    requested_candidates = set(normalized_candidates)
    requested_clusters = set(normalized_clusters)
    unknown_candidates = requested_candidates - set(candidate_map)
    unknown_clusters = requested_clusters - set(cluster_map)
    if unknown_candidates or unknown_clusters:
        parts: list[str] = []
        if unknown_candidates:
            parts.append(
                "unknown candidate IDs: " + ", ".join(sorted(unknown_candidates))
            )
        if unknown_clusters:
            parts.append(
                "unknown cluster IDs: " + ", ".join(sorted(unknown_clusters))
            )
        raise SeedApprovalError("; ".join(parts))

    selected: list[SeedCandidate] = []
    covered_ids: set[str] = set()
    for cluster_id in sorted(requested_clusters):
        cluster = cluster_map[cluster_id]
        selected.append(merge_review_cluster(cluster))
        covered_ids.update(candidate_review_id(item) for item in cluster.items)
    for candidate_id in sorted(requested_candidates):
        if candidate_id in covered_ids:
            continue
        selected.append(candidate_map[candidate_id])
        covered_ids.add(candidate_id)

    selected_parent_keys = {
        candidate.workstream_key
        for candidate in selected
        if candidate.kind == "workstream" and candidate.workstream_key
    }
    available_parent_keys = {
        candidate.workstream_key
        for candidate in candidates
        if candidate.kind == "workstream" and candidate.workstream_key
    }
    missing_parent_keys = {
        candidate.workstream_key
        for candidate in selected
        if candidate.kind != "workstream"
        and candidate.workstream_key
        and not candidate.workstream_key.startswith("existing:")
        and candidate.workstream_key in available_parent_keys
        and candidate.workstream_key not in selected_parent_keys
    }
    if missing_parent_keys:
        raise SeedApprovalError(
            "selected child candidates require their reviewable workstream "
            "parent(s): " + ", ".join(sorted(missing_parent_keys))
        )
    all_ids = {candidate_review_id(candidate) for candidate in candidates}
    return selected, covered_ids == all_ids


def prompt_approval_selection(
    candidates: list[SeedCandidate],
    *,
    stream: Any = None,
) -> tuple[list[SeedCandidate], bool]:
    stream = stream or sys.stdout
    if not sys.stdin.isatty():
        return [], False
    prompt = (
        "Approve seed review items (all, none, candidate/cluster IDs separated "
        "by spaces, or blank to cancel): "
    )
    while True:
        print(prompt, end="", file=stream, flush=True)
        try:
            raw = input().strip()
        except EOFError:
            return [], False
        if not raw:
            return [], False
        if raw.lower() in {"all", "y", "yes"}:
            return whole_report_approval(candidates), True
        if raw.lower() == "none":
            return [], True
        tokens = [token for token in re.split(r"[\s,]+", raw) if token]
        candidate_ids = [token for token in tokens if token.startswith("cand-")]
        cluster_ids = [token for token in tokens if token.startswith("cluster-")]
        invalid = [
            token for token in tokens
            if not token.startswith("cand-") and not token.startswith("cluster-")
        ]
        if invalid:
            print("Unrecognized review IDs: " + ", ".join(invalid), file=stream)
            continue
        try:
            return resolve_approval_selection(
                candidates,
                candidate_ids=candidate_ids,
                cluster_ids=cluster_ids,
            )
        except SeedApprovalError as exc:
            print(f"Approval selection unavailable: {exc}", file=stream)


def build_seed_report(candidates: list[SeedCandidate]) -> list[SeedReportSection]:
    by_key = {
        key: SeedReportSection(key=key, title=title, summary=summary, items=[])
        for key, title, summary in REPORT_SECTION_DEFS
    }
    for cand in candidates:
        by_key[report_section_key(cand)].items.append(cand)
    for section in by_key.values():
        section.items.sort(key=candidate_rank_score, reverse=True)
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
    for marker in ("Rejected path:\n> ", "Excerpt:\n> "):
        if marker in candidate.body:
            excerpt = candidate.body.split(marker, 1)[1].splitlines()[0]
            break
    if excerpt:
        return clip(excerpt, 180)
    first_source = (
        public_source_id(candidate.source_ids[0])
        if candidate.source_ids else "source"
    )
    return f"receipt: {first_source}"


def candidate_review_source_lines(candidate: SeedCandidate) -> list[str]:
    lines: list[str] = []
    for ref in candidate_source_refs(candidate):
        safe_excerpt, _ = redact_seed_text(ref.get("excerpt") or "")
        excerpt = (
            f"; excerpt={json.dumps(clip(safe_excerpt, 220))}"
            if safe_excerpt.strip() else ""
        )
        lines.append(
            f"    - {public_source_id(ref['id'])}; "
            f"observed_at={ref['mtime'] or 'unknown'}; "
            f"digest={(ref['digest'][:16] if ref['digest'] else 'unknown')}"
            f"{excerpt}"
        )
    return lines or ["    - source receipt unavailable"]


def render_review_cluster_lines(cluster: SeedReviewCluster) -> list[str]:
    lines: list[str] = []
    if len(cluster.items) > 1:
        source_revisions = {
            revision
            for candidate in cluster.items
            for revision in candidate_revision_keys(candidate)
        }
        lines.extend([
            (
                f"### Possible duplicate cluster {cluster.cluster_id}: "
                f"{len(cluster.items)} non-conflicting variants; "
                f"{len(source_revisions)} source revision(s)"
            ),
            (
                "Approving the cluster writes one stable representative with "
                "the combined provenance; approve a candidate ID to keep only "
                "that exact variant."
            ),
        ])
    for candidate in cluster.items:
        safe_title, _ = redact_seed_text(candidate.title)
        signals = ", ".join(sorted(set(candidate.signals)))
        source_count = len(candidate_revision_keys(candidate))
        source_label = "source revision" if source_count == 1 else "source revisions"
        observation = candidate_observation_window(candidate)
        lines.extend([
            f"- [{candidate.kind}] {safe_title}",
            (
                f"  review_id={candidate_review_id(candidate)}; "
                f"cluster_id={cluster.cluster_id}"
            ),
            f"  signals={signals}; {source_count} {source_label}",
            f"  rationale: {candidate_review_rationale(candidate)}",
            f"  candidate body: {candidate_review_claim(candidate)}",
            f"  evidence: {candidate_evidence_line(candidate)}",
            (
                "  observation: "
                f"oldest={observation['oldest_observed_at'] or 'unknown'}; "
                f"latest={observation['latest_observed_at'] or 'unknown'}; "
                f"age_days={observation['age_days'] if observation['age_days'] is not None else 'unknown'}"
            ),
            f"  freshness: {observation['freshness_note']}",
            "  source evidence:",
            *candidate_review_source_lines(candidate),
        ])
    return lines


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


def high_confidence_agent_mistake_candidate(candidate: SeedCandidate) -> bool:
    return (
        high_confidence_agent_mistake(candidate.signals)
        and candidate.confidence >= AGENT_MISTAKE_MIN_CONFIDENCE
    )


def catch_demo_candidate(candidates: list[SeedCandidate]) -> SeedCandidate | None:
    """The strongest candidate for the separate post-apply gate check.

    Clean rejected paths stay first because they make the gate loop easiest to
    inspect. If no clean rejected path exists, a high-confidence prior agent
    mistake can carry the same gate-check loop without adding another surface.
    """
    rejected = [
        cand for cand in candidates
        if "rejected_path" in normalized_signals(cand.signals)
    ]
    clean_rejections = [
        cand for cand in rejected
        if not high_confidence_agent_mistake(cand.signals)
    ]
    agent_mistakes = [
        cand for cand in candidates
        if high_confidence_agent_mistake_candidate(cand)
    ]
    demo_pool = clean_rejections or agent_mistakes or rejected
    if not demo_pool:
        return None
    return sorted(demo_pool, key=lambda c: c.confidence, reverse=True)[0]


def catch_demo_request(candidate: SeedCandidate) -> str:
    """A safe request that should make latch_gate retrieve the seeded evidence."""
    evidence = candidate_evidence_line(candidate)
    if evidence.startswith("receipt:"):
        evidence = candidate.title
    if high_confidence_agent_mistake_candidate(candidate):
        return clip(
            f"Implement the approach involved in this prior agent mistake: {evidence}",
            220,
        )
    return clip(f"Revive this rejected path: {evidence}", 220)


def slash_command_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def catch_demo_payload(candidate: SeedCandidate) -> dict[str, Any]:
    request = catch_demo_request(candidate)
    gate_script = KB_HOME / "bin" / "run_latch_gate.sh"
    evidence_target = (
        "prior agent-mistake evidence"
        if high_confidence_agent_mistake_candidate(candidate)
        else "seeded rejected path"
    )
    return {
        "candidate": public_candidate_dict(candidate),
        "request": request,
        "slash_command": f"/latch-gate {slash_command_quote(request)}",
        "shell_command": "bash " + shlex.quote(str(gate_script)) + " " + shlex.quote(request),
        "requires_apply": True,
        "expected_outcome": (
            f"After you apply the seed, Latch should cite this {evidence_target} "
            "and ask whether to hold the line, redirect, or override it."
        ),
    }


def seed_report_receipt(
    *,
    sources: list[SeedSource],
    candidates: list[SeedCandidate],
) -> dict[str, Any]:
    """Compact initial-KB receipt for the visible review surface."""
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
    next_step = (
        "Review and apply the staging candidates, then use the separate gate-check "
        "command below to verify the strongest rejected path or prior agent mistake "
        "is available before files change."
        if demo else
        "Review and apply the useful candidates. A gate-check command appears when "
        "the initial KB contains a clean rejected path or high-confidence prior "
        "agent mistake."
    )
    summary = (
        f"Latch assembled this initial decision-KB review from {source_total} "
        f"selected local {source_label}; transcripts remain evidence inputs, "
        "not the product surface."
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
        "label": "Latch initial-KB receipt",
        "source": "latch_seed",
        "must_display_to_user": True,
        "summary": summary,
        "why_it_matters": why,
        "next_step": next_step,
        "used": {
            "sources": source_total,
            "source_counts": source_counts(sources),
            "candidates": len(candidates),
            "direction_priorities": direction_items,
            "sections": section_counts,
            "catch_demo": bool(demo),
            "redactions": sum(source.redaction_count for source in sources),
        },
    }


def write_boundary_message(args: argparse.Namespace) -> str:
    if not args.apply:
        if getattr(args, "preview_digest", None):
            cursor_history_flag = (
                " --cursor-history"
                if getattr(args, "cursor_history", False)
                else ""
            )
            return (
                "Preview only. This exact review is cached for 24 hours. Re-run "
                f"with{cursor_history_flag} --preview-digest "
                f"{args.preview_digest} --apply and the "
                "candidate/cluster IDs you approve."
            )
        return "Preview only. Re-run with --apply to write these as staging seed candidates."
    if getattr(args, "preview_digest", None):
        if getattr(args, "dismiss_all", False):
            return (
                "Digest-bound reject-all mode. No candidates will be written; "
                "the exact reviewed source revisions will be finalized as dismissed."
            )
        return (
            "Digest-bound apply mode. Only candidates from the exact reviewed "
            "preview can be written as staging evidence."
        )
    if args.approve_candidate or args.approve_cluster:
        return (
            "Apply mode with scoped review IDs. Only the selected candidates "
            "or cluster representatives will be written as staging evidence; "
            "unselected items are dismissed for these exact source revisions."
        )
    if args.yes:
        return (
            "Apply mode with --yes. These candidates will be written as staging "
            "evidence after this report."
        )
    return (
        "Apply mode. Review this report first; candidates are written only if "
        "you approve the prompt below."
    )


def apply_success_message(
    inserted: list[int] | SeedApplyResult, candidates: list[SeedCandidate],
) -> str:
    apply_result = inserted if isinstance(inserted, SeedApplyResult) else SeedApplyResult(
        inserted_ids=list(inserted)
    )
    inserted_ids = apply_result.inserted_ids
    lines = [
        f"Wrote {len(inserted_ids)} staging seed candidate(s): "
        f"{', '.join(map(str, inserted_ids)) or 'none'}",
        f"Exact import no-ops: {len(apply_result.skipped_import_keys)}",
        f"Provenance corroborations: {len(apply_result.corroborated_import_keys)}",
        f"Resumed incomplete imports: {len(apply_result.resumed_import_keys)}",
        (
            "Source revisions left pending for later candidate review: "
            f"{len(apply_result.pending_source_import_keys)}"
        ),
    ]
    if apply_result.workstream_attachments:
        lines.append(
            "Staging workstream attachments: "
            + ", ".join(
                f"{key} -> {node_id}"
                for key, node_id in sorted(apply_result.workstream_attachments.items())
            )
        )
    if apply_result.failures:
        lines.append(
            f"Retryable failures: {len(apply_result.failures)} (rerun resumes incomplete items)"
        )
    demo = catch_demo_candidate(candidates) if apply_result.complete else None
    if demo:
        payload = catch_demo_payload(demo)
        lines.extend([
            "",
            "Latch gate check ready:",
            "The approved staging seed is now in the KB. Run the gate check to watch Latch "
            "challenge the strongest rejected path or prior agent mistake "
            "before files change:",
            f"- Claude Code / Cursor: {payload['slash_command']}",
            f"- Shell: {payload['shell_command']}",
            f"Expected: {payload['expected_outcome']}",
        ])
    elif apply_result.complete:
        lines.extend([
            "",
            "Latch gate-check note: no clean rejected path or high-confidence prior "
            "agent mistake was applied in this seed run, so there is no "
            "catch-demo command yet.",
        ])
    else:
        lines.extend([
            "",
            "Latch gate check is pending until the retryable import failures are resolved.",
        ])
    return "\n".join(lines)


def public_apply_receipt(
    result: SeedApplyResult,
    *,
    approved_candidates: list[SeedCandidate],
    dismissed_all: bool,
) -> dict[str, Any]:
    """Return a truthful apply receipt without exposing local import keys."""
    return {
        "complete": result.complete,
        "dismissed_all": bool(dismissed_all),
        "approved_candidate_count": len(approved_candidates),
        "inserted_ids": list(result.inserted_ids),
        "inserted_count": len(result.inserted_ids),
        "exact_import_noop_count": len(result.skipped_import_keys),
        "corroboration_count": len(result.corroborated_import_keys),
        "resumed_import_count": len(result.resumed_import_keys),
        "pending_source_count": len(result.pending_source_import_keys),
        "failure_count": len(result.failures),
        "failures": [
            {"error_code": str(failure.get("error_code") or "internal")}
            for failure in result.failures
        ],
        "workstream_attachments": dict(result.workstream_attachments),
    }


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
        "Initial-KB setup reads selected local agent chats for this project and "
        "proposes decisions, rejected paths, preferences, and concrete follow-ups "
        "that latch can judge against before the first new compacted session.",
        "",
        f"Project: {Path(args.project).resolve()}",
        f"Transcript source: {args.source}",
        "Cursor IDE history: "
        + (
            "opted in (metadata-verified current project only; "
            "CLI/cloud/subagents excluded)"
            if getattr(args, "cursor_history", False)
            else "not selected"
        ),
        f"Lookback: {args.lookback_days} day(s)",
        f"Selection cap: {session_cap} session(s) after value/recency ranking",
        f"Sources selected: {len(sources)} ({format_source_counts(sources)})",
        f"Exact unchanged sources already applied: "
        f"{int(getattr(args, 'sources_skipped_unchanged', 0))}",
        f"Secrets redacted before downstream use: "
        f"{sum(source.redaction_count for source in sources)}",
        f"LLM-backed seed: {args.llm or 'yes'}"
        + (f" (estimated/capped calls: {llm_estimate})" if args.llm == "yes" else ""),
        f"Candidates: {len(candidates)}",
        "Ranking: sections preserve high-value coverage; items within each "
        "section are strongest-first.",
    ]
    if args.llm == "yes":
        stats = dict(getattr(args, "llm_stats", {}) or {})
        lines.append(
            "LLM source outcomes: "
            f"attempted={int(stats.get('attempted', 0))}, "
            f"succeeded={int(stats.get('succeeded', 0))}, "
            f"failed={int(stats.get('failed', 0))}, "
            f"deferred={int(getattr(args, 'sources_deferred', 0))}."
        )
    discovery = dict(getattr(args, "discovery_stats", {}) or {})
    if discovery:
        lines.append(
            "Discovery outcomes: "
            f"unavailable={int(discovery.get('source_unavailable', 0))}, "
            f"invalid={int(discovery.get('source_invalid', 0))}, "
            f"project_excluded={int(discovery.get('project_excluded', 0))}."
        )
    if not candidates:
        lines.extend([
            "",
            (
                "LLM-backed seed produced no writeable candidates, so latch will "
                "not write deterministic fallback candidates in the user-facing "
                "path. Fix the model backend or try a wider source/window and rerun."
            ) if getattr(args, "llm_refinement_empty", False) else (
                "Initial KB is already current for the selected unchanged sources."
                if getattr(args, "sources_skipped_unchanged", 0) else
                "No seed candidates found. Try a wider lookback, higher "
                "--last-sessions, or --all-projects."
            ),
        ])
        return "\n".join(lines) + "\n"
    receipt = seed_report_receipt(sources=sources, candidates=candidates)
    lines.extend([
        "",
        "Latch initial-KB receipt:",
        receipt["summary"],
        f"Why this mattered: {receipt['why_it_matters']}",
        f"Next step: {receipt['next_step']}",
    ])
    lines.extend([
        "",
        "Seed report:",
        (
            "Review controls: each item has a stable candidate ID. Possible "
            "duplicates share a cluster ID. With --apply, use repeatable "
            "--approve-candidate ID or --approve-cluster ID for scoped approval; "
            "--yes without selectors preserves whole-report approval. A scoped "
            "selection is final for the reviewed source revisions, so include "
            "every candidate or cluster you want to keep in that one command."
        ),
    ])
    if getattr(args, "preview_digest", None):
        lines.append(
            "Exact preview binding: "
            f"{args.preview_digest} (expires after "
            f"{SEED_PREVIEW_MAX_AGE_HOURS} hours)."
        )
    elif getattr(args, "preview_cache_unavailable", False):
        lines.append(
            "Exact preview cache unavailable at the configured Latch vault. "
            "Scoped non-interactive approval is disabled; use --apply without "
            "selectors to review and choose IDs in one interactive run."
        )
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
        clusters = build_review_clusters(section.items)
        clusters.sort(
            key=lambda cluster: candidate_rank_score(
                review_cluster_representative(cluster)
            ),
            reverse=True,
        )
        for cluster in clusters:
            lines.extend(render_review_cluster_lines(cluster))
    demo = catch_demo_candidate(candidates)
    if demo:
        payload = catch_demo_payload(demo)
        lines.extend([
            "",
            "Optional gate check:",
            "After you apply this seed, run one of these to watch latch challenge "
            "the strongest rejected path or prior agent mistake from the report:",
            f"- Claude Code / Cursor: {payload['slash_command']}",
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
        "ok": True,
        "intro": SEED_INTRO,
        "project": str(Path(args.project).resolve()),
        "source": args.source,
        "cursor_history": {
            "enabled": bool(getattr(args, "cursor_history", False)),
            "scope": "current_project_local_ide_only",
            "excluded": ["cursor_cli", "cloud_chats", "other_projects", "subagents"],
        },
        "lookback_days": args.lookback_days,
        "max_sessions": session_cap,
        "sources_scanned": len(sources),
        "sources_selected": int(getattr(args, "sources_selected", len(sources))),
        "sources_deferred": int(getattr(args, "sources_deferred", 0)),
        "sources_skipped_unchanged": int(
            getattr(args, "sources_skipped_unchanged", 0)
        ),
        "source_counts": source_counts(sources),
        "source_receipts": [
            {
                "id": public_source_id(source.id),
                "agent": source.agent,
                "mtime": source.mtime,
                "digest": source.content_digest,
                "redaction_count": source.redaction_count,
                "subject": clip(
                    redact_seed_text(source.subject)[0], SOURCE_SUBJECT_CHARS,
                ),
            }
            for source in sources
        ],
        "llm": args.llm or "no",
        "coverage": dict(getattr(args, "coverage_plan", {}) or {}),
        "llm_call_estimate": llm_estimate,
        "llm_calls": public_seed_stats(
            dict(getattr(args, "llm_stats", {}) or {})
        ),
        "discovery": dict(getattr(args, "discovery_stats", {}) or {}),
        "llm_refinement_empty": bool(getattr(args, "llm_refinement_empty", False)),
        "ranking": (
            "Sections preserve high-value coverage; items within each "
            "section are strongest-first using an internal score."
        ),
        "review_controls": {
            "candidate_option": "--approve-candidate ID",
            "cluster_option": "--approve-cluster ID",
            "whole_report_option": "--yes",
            "reject_report_option": "--dismiss-all",
            "preview_digest": getattr(args, "preview_digest", None),
            "preview_max_age_hours": SEED_PREVIEW_MAX_AGE_HOURS,
            "scoped_apply_requires_preview_digest": True,
            "scoped_selection_finalizes_source_revisions": True,
            "preview_cache_available": not bool(
                getattr(args, "preview_cache_unavailable", False)
            ),
        },
        "review_clusters": [
            public_review_cluster_dict(cluster)
            for cluster in build_review_clusters(candidates)
        ],
        "report": [
            public_report_section_dict(section, candidates=candidates)
            for section in build_seed_report(candidates)
        ],
        "receipt": seed_report_receipt(sources=sources, candidates=candidates) if candidates else None,
        "catch_demo": (
            catch_demo_payload(demo) if (demo := catch_demo_candidate(candidates)) else None
        ),
        "preview_digest": getattr(args, "preview_digest", None),
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
    data.pop("source_paths", None)
    data["source_ids"] = [public_source_id(value) for value in candidate.source_ids]
    data["source_excerpts"] = [
        redact_seed_text(str(value))[0] for value in candidate.source_excerpts
    ]
    data["title"] = redact_seed_text(str(data.get("title") or ""))[0]
    data["body"] = redact_seed_text(str(data.get("body") or ""))[0]
    data["review_id"] = candidate_review_id(candidate)
    data["review_rationale"] = candidate_review_rationale(candidate)
    data["review_claim"] = candidate_review_claim(candidate)
    data["observation"] = candidate_observation_window(candidate)
    data["source_receipts"] = [
        {
            "id": public_source_id(ref["id"]),
            "observed_at": ref["mtime"] or None,
            "digest": ref["digest"],
            "excerpt": redact_seed_text(ref.get("excerpt") or "")[0] or None,
        }
        for ref in candidate_source_refs(candidate)
    ]
    return data


def public_review_cluster_dict(cluster: SeedReviewCluster) -> dict[str, Any]:
    representative = review_cluster_representative(cluster)
    source_revisions = {
        revision
        for candidate in cluster.items
        for revision in candidate_revision_keys(candidate)
    }
    return {
        "cluster_id": cluster.cluster_id,
        "section": cluster.section_key,
        "possible_duplicates": len(cluster.items) > 1,
        "candidate_ids": [
            candidate_review_id(candidate) for candidate in cluster.items
        ],
        "representative_candidate_id": candidate_review_id(representative),
        "source_revision_count": len(source_revisions),
        "items": [public_candidate_dict(candidate) for candidate in cluster.items],
    }


def public_seed_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Redact source identifiers in otherwise aggregate extraction telemetry."""
    out = dict(stats)
    for key, value in list(out.items()):
        if key.endswith("_by_source"):
            out.pop(key, None)
            continue
        if key.endswith("source_ids") and isinstance(value, list):
            out[key] = [public_source_id(str(item)) for item in value]
    return out


def source_counts(sources: list[SeedSource]) -> dict[str, int]:
    counts = {"claude": 0, "codex": 0, "cursor": 0}
    for src in sources:
        if src.agent in counts:
            counts[src.agent] += 1
        else:
            counts[src.agent] = counts.get(src.agent, 0) + 1
    return counts


def format_source_counts(sources: list[SeedSource]) -> str:
    counts = source_counts(sources)
    return ", ".join(f"{agent}={count}" for agent, count in counts.items())


def _cursor_seed_preview_path(project_path: str, session_id: str) -> Path:
    sid_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
    return paths.ensure_project_dir(project_path) / f"cursor_seed_preview.{sid_key}.json"


def _cursor_seed_preview_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def seed_preview_source_locator(agent: str, source_id: str) -> str:
    """Return a stable non-path locator suitable for short-lived caches."""
    provider = re.sub(r"[^a-z0-9_-]+", "-", agent.lower()).strip("-") \
        or "unknown"
    identity = hashlib.sha256(
        str(source_id).encode("utf-8", errors="replace")
    ).hexdigest()[:24]
    return f"seed-source:{provider}:{identity}"


def _cached_seed_source_dict(source: SeedSource) -> dict[str, Any]:
    data = asdict(source)
    data["id"] = seed_source_identity(source.id)
    data["path"] = seed_preview_source_locator(source.agent, source.id)
    data["text"] = ""
    data["subject"] = redact_seed_text(source.subject)[0]
    return data


def _cached_seed_candidate_dict(
    candidate: SeedCandidate,
    *,
    source_agents: dict[str, str],
) -> dict[str, Any]:
    data = asdict(candidate)
    data["title"] = redact_seed_text(candidate.title)[0]
    data["body"] = redact_seed_text(candidate.body)[0]
    data["signals"] = [
        redact_seed_text(signal)[0] for signal in candidate.signals
    ]
    data["source_ids"] = [
        seed_source_identity(source_id) for source_id in candidate.source_ids
    ]
    data["source_paths"] = [
        seed_preview_source_locator(
            source_agents.get(
                source_id, source_id.partition(":")[0] or "unknown",
            ),
            source_id,
        )
        for source_id in candidate.source_ids
    ]
    data["source_excerpts"] = [
        redact_seed_text(excerpt)[0]
        for excerpt in candidate.source_excerpts
    ]
    if candidate.workstream_key is not None:
        data["workstream_key"] = redact_seed_text(
            candidate.workstream_key
        )[0]
    return data


def _validate_seed_preview_age(
    body: dict[str, Any],
    *,
    label: str,
    error_type: type[SeedPreviewError],
    now: datetime | None = None,
) -> None:
    raw_created_at = body.get("created_at")
    try:
        created_at = datetime.fromisoformat(str(raw_created_at))
    except (TypeError, ValueError) as exc:
        raise error_type(f"{label} seed preview timestamp is missing or invalid") from exc
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = current.astimezone(timezone.utc) - created_at.astimezone(timezone.utc)
    if age < timedelta(minutes=-5):
        raise error_type(f"{label} seed preview timestamp is in the future")
    if age > timedelta(hours=SEED_PREVIEW_MAX_AGE_HOURS):
        raise error_type(
            f"{label} seed preview expired after "
            f"{SEED_PREVIEW_MAX_AGE_HOURS} hours; rerun preview"
        )


def write_cursor_seed_preview(
    *,
    project_path: str,
    session_id: str,
    sources: list[SeedSource],
    candidates: list[SeedCandidate],
    llm_estimate: int,
    apply_sources: list[SeedSource] | None = None,
    source_failure_codes: dict[str, str] | None = None,
    workstream_scope: str = "project",
    llm_stats: dict[str, Any] | None = None,
    discovery_stats: dict[str, Any] | None = None,
    llm_refinement_empty: bool = False,
) -> str:
    """Cache the exact reviewed Cursor set without retaining transcript text."""
    cached_sources = list(sources)
    cached_apply_sources = (
        list(apply_sources) if apply_sources is not None else cached_sources
    )
    source_agents = {
        source.id: source.agent
        for source in [*cached_sources, *cached_apply_sources]
    }
    payload = {
        "version": 4,
        "extractor_version": SEED_EXTRACTOR_VERSION,
        "created_at": utc_now().isoformat(),
        "project_fingerprint": project_scope_fingerprint(project_path),
        "session_fingerprint": hashlib.sha256(
            session_id.encode("utf-8", errors="replace")
        ).hexdigest(),
        "sources": [
            _cached_seed_source_dict(source)
            for source in cached_sources
        ],
        "apply_sources": [
            _cached_seed_source_dict(source)
            for source in cached_apply_sources
        ],
        "source_failure_codes": dict(source_failure_codes or {}),
        "workstream_scope": workstream_scope,
        "llm_stats": public_seed_stats(dict(llm_stats or {})),
        "discovery_stats": dict(discovery_stats or {}),
        "llm_refinement_empty": bool(llm_refinement_empty),
        "candidates": [
            _cached_seed_candidate_dict(
                candidate, source_agents=source_agents,
            )
            for candidate in candidates
        ],
        "llm_estimate": int(llm_estimate),
    }
    digest = _cursor_seed_preview_digest(payload)
    body = {**payload, "preview_digest": digest}
    path = _cursor_seed_preview_path(project_path, session_id)
    prune_cursor_seed_preview_cache(
        project_path,
        keep=SEED_PREVIEW_CACHE_MAX_FILES - 1,
    )
    _write_private_preview_file(path, body)
    return digest


def load_cursor_seed_preview(
    *, project_path: str, session_id: str, preview_digest: str,
    include_apply_state: bool = False,
    now: datetime | None = None,
) -> Any:
    """Load only the exact cached Cursor preview approved by digest."""
    if not re.fullmatch(r"[0-9a-f]{64}", preview_digest or ""):
        raise CursorSeedPreviewError("Cursor seed preview digest is missing or invalid")
    path = _cursor_seed_preview_path(project_path, session_id)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CursorSeedPreviewError("Cursor seed preview cache is missing or unreadable") from exc
    if not isinstance(body, dict):
        raise CursorSeedPreviewError("Cursor seed preview cache is malformed")
    recorded_digest = body.pop("preview_digest", None)
    if recorded_digest != preview_digest or _cursor_seed_preview_digest(body) != preview_digest:
        raise CursorSeedPreviewError("Cursor seed preview digest does not match cached candidates")
    session_fingerprint = hashlib.sha256(
        session_id.encode("utf-8", errors="replace")
    ).hexdigest()
    if body.get("project_fingerprint") != project_scope_fingerprint(
        project_path
    ) or body.get("session_fingerprint") != session_fingerprint:
        raise CursorSeedPreviewError("Cursor seed preview belongs to another project or session")
    if body.get("version") != 4 \
            or body.get("extractor_version") != SEED_EXTRACTOR_VERSION:
        raise CursorSeedPreviewError(
            "seed extractor changed; rerun preview before apply"
        )
    try:
        _validate_seed_preview_age(
            body,
            label="Cursor",
            error_type=CursorSeedPreviewError,
            now=now,
        )
    except CursorSeedPreviewError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        sources = [SeedSource(**item) for item in body.get("sources", [])]
        candidates = [SeedCandidate(**item) for item in body.get("candidates", [])]
        estimate = int(body.get("llm_estimate", 0))
    except (TypeError, ValueError) as exc:
        raise CursorSeedPreviewError("Cursor seed preview candidates are malformed") from exc
    if not include_apply_state:
        return sources, candidates, estimate
    try:
        apply_sources = [
            SeedSource(**item) for item in body.get("apply_sources", [])
        ]
        source_failure_codes = {
            str(key): str(value)
            for key, value in dict(body.get("source_failure_codes") or {}).items()
        }
        workstream_scope = str(body["workstream_scope"])
        llm_stats = dict(body.get("llm_stats") or {})
        discovery_stats = dict(body.get("discovery_stats") or {})
        refinement_empty = bool(body.get("llm_refinement_empty", False))
    except (KeyError, TypeError, ValueError) as exc:
        raise CursorSeedPreviewError(
            "Cursor seed preview apply state is malformed"
        ) from exc
    if not workstream_scope or any(
        code not in {"extractor_failed", "source_unavailable", "source_invalid"}
        for code in source_failure_codes.values()
    ):
        raise CursorSeedPreviewError("Cursor seed preview apply state is invalid")
    return (
        sources,
        candidates,
        estimate,
        apply_sources,
        source_failure_codes,
        workstream_scope,
        llm_stats,
        discovery_stats,
        refinement_empty,
    )


def remove_cursor_seed_preview(project_path: str, session_id: str) -> None:
    try:
        _cursor_seed_preview_path(project_path, session_id).unlink()
    except OSError:
        pass


def _seed_preview_path(project_path: str, preview_digest: str) -> Path:
    return (
        paths.project_dir(project_path)
        / f"seed_preview.{preview_digest[:24]}.json"
    )


def _write_private_preview_file(path: Path, body: dict[str, Any]) -> None:
    """Atomically write a preview cache file that is 0600 from creation.

    Creating at umask-default and chmodding afterwards leaves a window where
    the full redacted review payload is world-readable; open the temp file
    with restrictive permissions instead.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(body, indent=2) + "\n")
    os.replace(tmp, path)


def write_seed_preview(
    *,
    project_path: str,
    source_choice: str,
    sources: list[SeedSource],
    candidates: list[SeedCandidate],
    llm_estimate: int,
    apply_sources: list[SeedSource],
    source_failure_codes: dict[str, str],
    workstream_scope: str,
    llm_stats: dict[str, Any],
    discovery_stats: dict[str, Any],
    llm_refinement_empty: bool,
    cursor_history: bool = False,
) -> str:
    """Cache one exact shared-provider review without retaining transcript text.

    Exact current-session Cursor slash commands keep their separate
    session-bound receipt cache. Opt-in Cursor history uses this shared lane.
    """
    cached_sources = list(sources)
    cached_apply_sources = list(apply_sources)
    source_agents = {
        source.id: source.agent
        for source in [*cached_sources, *cached_apply_sources]
    }
    payload = {
        "version": 3,
        "extractor_version": SEED_EXTRACTOR_VERSION,
        "created_at": utc_now().isoformat(),
        "project_fingerprint": project_scope_fingerprint(project_path),
        "source_choice": source_choice,
        "cursor_history": bool(cursor_history),
        "sources": [
            _cached_seed_source_dict(source) for source in cached_sources
        ],
        "apply_sources": [
            _cached_seed_source_dict(source)
            for source in cached_apply_sources
        ],
        "source_failure_codes": dict(source_failure_codes),
        "workstream_scope": workstream_scope,
        "llm_stats": public_seed_stats(dict(llm_stats)),
        "discovery_stats": dict(discovery_stats),
        "llm_refinement_empty": bool(llm_refinement_empty),
        "candidates": [
            _cached_seed_candidate_dict(
                candidate, source_agents=source_agents,
            )
            for candidate in candidates
        ],
        "llm_estimate": int(llm_estimate),
    }
    digest = _cursor_seed_preview_digest(payload)
    body = {**payload, "preview_digest": digest}
    path = _seed_preview_path(project_path, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    prune_seed_preview_cache(
        project_path,
        keep=SEED_PREVIEW_CACHE_MAX_FILES - 1,
    )
    _write_private_preview_file(path, body)
    return digest


def load_seed_preview(
    *,
    project_path: str,
    source_choice: str,
    preview_digest: str,
    cursor_history: bool = False,
    now: datetime | None = None,
) -> tuple[
    list[SeedSource],
    list[SeedCandidate],
    int,
    list[SeedSource],
    dict[str, str],
    str,
    dict[str, Any],
    dict[str, Any],
    bool,
]:
    """Load an exact shared-provider review approved by digest."""
    if not re.fullmatch(r"[0-9a-f]{64}", preview_digest or ""):
        raise SeedPreviewError("seed preview digest is missing or invalid")
    path = _seed_preview_path(project_path, preview_digest)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SeedPreviewError("seed preview cache is missing or unreadable") from exc
    if not isinstance(body, dict):
        raise SeedPreviewError("seed preview cache is malformed")
    recorded_digest = body.pop("preview_digest", None)
    if recorded_digest != preview_digest \
            or _cursor_seed_preview_digest(body) != preview_digest:
        raise SeedPreviewError("seed preview digest does not match cached candidates")
    if body.get("project_fingerprint") != project_scope_fingerprint(project_path) \
            or body.get("source_choice") != source_choice:
        raise SeedPreviewError("seed preview belongs to another project or source choice")
    cached_cursor_history = body.get("cursor_history", False)
    if not isinstance(cached_cursor_history, bool):
        raise SeedPreviewError("seed preview apply state is malformed")
    if cached_cursor_history != bool(cursor_history):
        raise SeedPreviewError(
            "seed preview Cursor history consent does not match; rerun apply "
            "with the same --cursor-history choice"
        )
    if body.get("version") != 3 \
            or body.get("extractor_version") != SEED_EXTRACTOR_VERSION:
        raise SeedPreviewError("seed extractor changed; rerun preview before apply")
    try:
        _validate_seed_preview_age(
            body,
            label="Reviewed",
            error_type=SeedPreviewError,
            now=now,
        )
    except SeedPreviewError:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    try:
        sources = [SeedSource(**item) for item in body.get("sources", [])]
        candidates = [SeedCandidate(**item) for item in body.get("candidates", [])]
        llm_estimate = int(body.get("llm_estimate", 0))
        apply_sources = [
            SeedSource(**item) for item in body.get("apply_sources", [])
        ]
        source_failure_codes = {
            str(key): str(value)
            for key, value in dict(body.get("source_failure_codes") or {}).items()
        }
        workstream_scope = str(body["workstream_scope"])
        llm_stats = dict(body.get("llm_stats") or {})
        discovery_stats = dict(body.get("discovery_stats") or {})
        refinement_empty = bool(body.get("llm_refinement_empty", False))
    except (KeyError, TypeError, ValueError) as exc:
        raise SeedPreviewError("seed preview apply state is malformed") from exc
    if not workstream_scope or any(
        code not in {"extractor_failed", "source_unavailable", "source_invalid"}
        for code in source_failure_codes.values()
    ):
        raise SeedPreviewError("seed preview apply state is invalid")
    return (
        sources,
        candidates,
        llm_estimate,
        apply_sources,
        source_failure_codes,
        workstream_scope,
        llm_stats,
        discovery_stats,
        refinement_empty,
    )


def remove_seed_preview(project_path: str, preview_digest: str) -> None:
    try:
        _seed_preview_path(project_path, preview_digest).unlink()
    except OSError:
        pass


def _prune_seed_preview_pattern(
    project_path: str,
    *,
    pattern: str,
    keep: int,
    now: datetime | None,
) -> None:
    parent = paths.project_dir(project_path)
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    retained: list[tuple[datetime, Path]] = []
    try:
        cache_paths = list(parent.glob(pattern))
    except OSError:
        return
    for path in cache_paths:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            created_at = datetime.fromisoformat(str(body.get("created_at")))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        age = current.astimezone(timezone.utc) - created_at.astimezone(timezone.utc)
        if age > timedelta(hours=SEED_PREVIEW_MAX_AGE_HOURS):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        retained.append((created_at, path))
    retained.sort(key=lambda item: item[0], reverse=True)
    for _created_at, path in retained[max(0, keep):]:
        try:
            path.unlink()
        except OSError:
            pass


def prune_seed_preview_cache(
    project_path: str,
    *,
    keep: int = SEED_PREVIEW_CACHE_MAX_FILES,
    now: datetime | None = None,
) -> None:
    """Bound abandoned generic review metadata and delete expired previews."""
    _prune_seed_preview_pattern(
        project_path,
        pattern="seed_preview.*.json",
        keep=keep,
        now=now,
    )


def prune_cursor_seed_preview_cache(
    project_path: str,
    *,
    keep: int = SEED_PREVIEW_CACHE_MAX_FILES,
    now: datetime | None = None,
) -> None:
    """Bound abandoned Cursor review metadata and delete expired previews."""
    _prune_seed_preview_pattern(
        project_path,
        pattern="cursor_seed_preview.*.json",
        keep=keep,
        now=now,
    )


def project_scope_fingerprint(project_path: str) -> str:
    resolved = str(Path(project_path).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def resolve_existing_workstream_target(
    project_path: str, workstream_id: int,
) -> tuple[int | None, str | None]:
    """Resolve a seed parent without creating/migrating a preview-time vault."""
    try:
        db_file = paths.db_path(project_path)
    except Exception:
        return None, "the project KB location could not be resolved"
    if not db_file.is_file():
        return None, "the project KB does not exist"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        import workstreams  # noqa: WPS433
        resolution = workstreams.resolve_active(conn, int(workstream_id))
    except sqlite3.Error:
        return None, "the project KB could not be read"
    finally:
        if conn is not None:
            conn.close()
    active_id = resolution.get("active_id")
    if active_id is not None:
        return int(active_id), None
    state = resolution.get("state")
    if state == "not_found":
        return None, "the requested node does not exist"
    if state == "not_workstream":
        return None, "the requested node is not a workstream"
    if state == "closed":
        return None, "the requested workstream is closed"
    return None, f"the requested workstream identity is {state}"


def existing_workstream_preflight(
    project_path: str, workstream_id: int,
) -> str | None:
    """Backward-compatible error-only wrapper for preview validation."""
    _, error = resolve_existing_workstream_target(project_path, workstream_id)
    return error


def seed_source_import_key(
    source: SeedSource, *, project_path: str, workstream_scope: str,
) -> str:
    payload = {
        "version": SEED_EXTRACTOR_VERSION,
        "project": project_scope_fingerprint(project_path),
        "workstream_scope": workstream_scope,
        "source_id": seed_source_identity(source.id),
        "content_digest": source.content_digest,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def split_applied_sources(
    sources: list[SeedSource], *, project_path: str, workstream_scope: str,
) -> tuple[list[SeedSource], list[SeedSource]]:
    """Read an existing ledger without creating or migrating preview-time state.

    A genuinely absent database or ledger table is a valid first run. Once a
    database exists, read failures are ambiguous and must stop extraction so a
    corrupt or inaccessible ledger cannot silently trigger duplicate imports.
    """
    if not sources:
        return [], []
    try:
        db_file = paths.db_path(project_path)
    except Exception as exc:
        raise SeedSourceLedgerError(
            "The seed source ledger location could not be resolved safely."
        ) from exc
    try:
        mode = db_file.stat().st_mode
    except FileNotFoundError:
        try:
            db_file.lstat()
        except FileNotFoundError:
            return sources, []
        except OSError as exc:
            raise SeedSourceLedgerError(
                "The seed source ledger could not be inspected safely."
            ) from exc
        raise SeedSourceLedgerError(
            "The seed source ledger points to unavailable existing state."
        )
    except OSError as exc:
        raise SeedSourceLedgerError(
            "The seed source ledger could not be inspected safely."
        ) from exc
    if not stat.S_ISREG(mode):
        raise SeedSourceLedgerError(
            "The seed source ledger location is not a readable database file."
        )
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='seed_source_import'"
        ).fetchone()
        if table is None:
            return sources, []
        keyed = {
            seed_source_import_key(
                source, project_path=project_path, workstream_scope=workstream_scope,
            ): source
            for source in sources
        }
        placeholders = ",".join("?" for _ in keyed)
        applied_keys = {
            str(row[0]) for row in conn.execute(
                f"SELECT import_key FROM seed_source_import "
                f"WHERE state = 'applied' AND import_key IN ({placeholders})",
                tuple(keyed),
            ).fetchall()
        }
        pending = [source for key, source in keyed.items() if key not in applied_keys]
        applied = [source for key, source in keyed.items() if key in applied_keys]
        return pending, applied
    except (OSError, sqlite3.Error) as exc:
        raise SeedSourceLedgerError(
            "The existing seed source ledger is unavailable or unreadable."
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def candidate_import_key(
    candidate: SeedCandidate,
    *,
    project_path: str,
    target_workstream_id: int | None = None,
) -> str:
    refs = sorted(
        f"{seed_source_identity(ref['id'])}:{ref['digest']}"
        for ref in candidate_source_refs(candidate)
    )
    payload = {
        "version": SEED_EXTRACTOR_VERSION,
        "project": project_scope_fingerprint(project_path),
        "kind": candidate.kind,
        "title": normalize_for_quality_filter(candidate.title),
        "claim": normalize_for_quality_filter(candidate_core_text(candidate)),
        "sources": refs,
        "workstream_key": candidate.workstream_key,
        "target_workstream_id": target_workstream_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def candidate_claim_key(
    candidate: SeedCandidate,
    *,
    project_path: str,
    target_workstream_id: int | None = None,
    persisted_body: bool = False,
) -> str:
    """Stable exact-meaning key used to union corroboration across batches."""
    payload = {
        "claim_key_version": SEED_CLAIM_KEY_VERSION,
        "project": project_scope_fingerprint(project_path),
        "kind": candidate.kind,
        "title": normalize_for_quality_filter(candidate.title),
        "claim": normalize_for_quality_filter(
            candidate_core_text(candidate, persisted_body=persisted_body)
        ),
        "workstream_key": candidate.workstream_key,
        "target_workstream_id": target_workstream_id,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def candidate_source_fingerprint(candidate: SeedCandidate) -> str:
    refs = sorted(
        f"{seed_source_identity(ref['id'])}:{ref['digest']}"
        for ref in candidate_source_refs(candidate)
    )
    return hashlib.sha256("\n".join(refs).encode("utf-8")).hexdigest()


def body_with_import_receipt(
    candidate: SeedCandidate,
    *,
    import_key: str,
    project_path: str,
    workstream_id: int | None,
) -> str:
    refs = candidate_source_refs(candidate)
    source_lines = "\n".join(
        f"- {ref['id']}; observed_at={ref['mtime'] or 'unknown'}; "
        f"digest={(ref['digest'] or 'unknown')}"
        for ref in refs
    ) or "- user-approved explicit initialization"
    scope = Path(project_path).resolve().name
    # Redact every untrusted field as one block so an envelope split across a
    # candidate, source identifier, or project label cannot evade detection.
    # Trusted receipt fields are appended only after that redaction pass.
    untrusted = (
        f"{candidate.body.rstrip()}\n\n"
        "Seed source receipts:\n"
        f"{source_lines}\n\n"
        f"Seed project label: {scope}"
    )
    safe_untrusted, _ = redact_seed_text(untrusted)
    workstream = str(workstream_id) if workstream_id is not None else "unattached"
    rendered = (
        f"{safe_untrusted.rstrip()}\n\n"
        "Seed import receipt:\n"
        f"- Latch-Seed-Import-Key: {import_key}\n"
        f"- extractor: {SEED_EXTRACTOR_VERSION}\n"
        f"- project_fingerprint: {project_scope_fingerprint(project_path)}\n"
        f"- workstream_id: {workstream}\n"
        "- authority: staging; requires review before canonical promotion"
    )
    return rendered


def node_matches_seed_claim(
    node: dict[str, Any],
    candidate: SeedCandidate,
    *,
    project_path: str,
    target_workstream_id: int | None,
    claim_key: str,
) -> bool:
    """Require current claim text and scope to match before provenance union."""
    if node.get("kind") != candidate.kind or node.get("status") == "stale":
        return False
    expected_parent = None if candidate.kind == "workstream" else target_workstream_id
    if node.get("workstream_id") != expected_parent:
        return False
    snapshot = SeedCandidate(
        kind=str(node.get("kind") or ""),
        title=str(node.get("title") or ""),
        body=str(node.get("body") or ""),
        confidence=0.0,
        signals=[],
        source_ids=[],
        source_paths=[],
        source_mtimes=[],
        source_digests=[],
        llm_used=False,
        workstream_key=candidate.workstream_key,
    )
    return candidate_claim_key(
        snapshot,
        project_path=project_path,
        target_workstream_id=expected_parent,
        persisted_body=True,
    ) == claim_key


def backfill_legacy_seed_claims(conn: Any, *, project_path: str) -> int:
    """Recover additive claim/recency metadata from intact local seed nodes."""
    import db as db_store  # noqa: WPS433

    resolved_project = str(Path(project_path).resolve())
    rows = conn.execute(
        """
        SELECT si.import_key, si.source_import_keys_json, si.workstream_key,
               si.workstream_id, n.kind, n.title, n.body, n.status,
               n.workstream_id AS current_workstream_id
        FROM seed_import si
        JOIN nodes n ON n.id = si.node_id
        WHERE si.project_path = ? AND si.claim_key IS NULL
        ORDER BY si.import_key
        """,
        (resolved_project,),
    ).fetchall()
    updated = 0
    for raw in rows:
        row = dict(raw)
        expected_parent = (
            None if row["kind"] == "workstream" else row["workstream_id"]
        )
        if row["current_workstream_id"] != expected_parent:
            continue
        snapshot = SeedCandidate(
            kind=str(row["kind"]),
            title=str(row["title"] or ""),
            body=str(row["body"] or ""),
            confidence=0.0,
            signals=[],
            source_ids=[],
            source_paths=[],
            source_mtimes=[],
            source_digests=[],
            llm_used=False,
            workstream_key=row["workstream_key"],
        )
        claim_key = candidate_claim_key(
            snapshot,
            project_path=project_path,
            target_workstream_id=expected_parent,
            persisted_body=True,
        )
        observed_values: list[str] = []
        try:
            source_keys = json.loads(row["source_import_keys_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            source_keys = []
        for source_key in source_keys if isinstance(source_keys, list) else []:
            source_row = db_store.get_seed_source_import(conn, str(source_key))
            if source_row is None:
                continue
            try:
                observed_values.append(
                    normalize_seed_observed_at(source_row["source_mtime"])
                )
            except (KeyError, TypeError, ValueError):
                continue
        try:
            db_store.backfill_seed_import_receipt(
                conn,
                row["import_key"],
                claim_key=claim_key,
                observed_at=max(observed_values, default=None),
            )
        except (KeyError, db_store.SeedImportLedgerError):
            continue
        updated += 1
    return updated


def _find_import_marker_node(conn: Any, import_key: str) -> int | None:
    marker = f"Latch-Seed-Import-Key: {import_key}"
    row = conn.execute(
        "SELECT id FROM nodes WHERE status != 'stale' AND instr(body, ?) > 0 "
        "ORDER BY id LIMIT 1",
        (marker,),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def append_seed_corroboration(
    conn: Any, node_id: int, candidate: SeedCandidate, import_key: str,
) -> None:
    """Add provenance to an existing staging seed node without changing claim."""
    import db as db_store  # noqa: WPS433

    row = conn.execute(
        "SELECT body, status FROM nodes WHERE id = ?", (node_id,)
    ).fetchone()
    if row is None or row["status"] != "staging":
        raise ValueError("seed corroboration target is not an active staging node")
    marker = f"Latch-Seed-Import-Key: {import_key}"
    body = str(row["body"] or "")
    if marker in body:
        return
    inline_count = body.count("\n\nAdditional seed corroboration:\n")
    if inline_count >= MAX_INLINE_CORROBORATIONS:
        # Full, unbounded provenance lives in seed_import/seed_source_import.
        # Keep retrieval text compact even after many explicit import passes.
        return
    refs = "\n".join(
        f"- {public_source_id(ref['id'])}; "
        f"observed_at={WHITESPACE_RE.sub(' ', ref['mtime']).strip() or 'unknown'}; "
        f"digest={ref['digest'] or 'unknown'}"
        for ref in candidate_source_refs(candidate)
    )
    safe_refs, _ = redact_seed_text(refs)
    db_store.update_node(
        conn,
        node_id,
        body=(
            body.rstrip()
            + "\n\nAdditional seed corroboration:\n"
            + f"- {marker}\n"
            + safe_refs
            + (
                "\n- inline provenance cap reached; later receipts remain in "
                "the structured seed ledger"
                if inline_count + 1 == MAX_INLINE_CORROBORATIONS else ""
            )
        ),
    )


def apply_candidates(
    candidates: list[SeedCandidate],
    *,
    project_path: str,
    existing_workstream_id: int | None = None,
    sources: list[SeedSource] | None = None,
    workstream_scope: str = "project",
    source_failure_codes: dict[str, str] | None = None,
    finalize_sources: bool = True,
) -> SeedApplyResult:
    import heal  # noqa: WPS433
    import db  # noqa: WPS433
    import artifacts as artifact_store  # noqa: WPS433
    import lockfile  # noqa: WPS433
    import workstreams  # noqa: WPS433

    if paths.is_unlatched_mode():
        raise SeedWriteBlocked(
            "unlatched",
            "Latch is Unlatched; initial-KB apply was blocked before any write.",
        )

    candidates = sanitize_reserved_workstream_keys(
        candidates, existing_workstream_id=existing_workstream_id,
    )
    if existing_workstream_id is not None:
        candidates = [
            candidate for candidate in candidates if candidate.kind != "workstream"
        ]
    candidates = resolve_unambiguous_workstream_links(candidates)
    parent_counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.kind == "workstream" and candidate.workstream_key:
            parent_counts[candidate.workstream_key] = (
                parent_counts.get(candidate.workstream_key, 0) + 1
            )
    ambiguous_parent_keys = {
        key for key, count in parent_counts.items() if count != 1
    }

    result = SeedApplyResult()
    source_rows: dict[str, dict[str, Any]] = {}
    source_key_by_ref: dict[tuple[str, str], str] = {}
    source_error_by_key: dict[str, str] = {}

    def mark_candidate_sources(candidate: SeedCandidate, error_code: str) -> None:
        for ref in candidate_source_refs(candidate):
            source_key = source_key_by_ref.get(
                (seed_source_identity(ref["id"]), ref["digest"])
            )
            if source_key is not None:
                source_error_by_key.setdefault(source_key, error_code)

    try:
        lock_context = lockfile.writer_lock(project_path)
        with lock_context:
            conn = db.connect(project_path)
            try:
                if existing_workstream_id is not None:
                    requested_existing_id = int(existing_workstream_id)
                    resolution = workstreams.resolve_membership_target(
                        conn, requested_existing_id,
                    )
                    if not resolution["ok"]:
                        raise SeedWriteBlocked(
                            "invalid_workstream",
                            "The requested existing workstream is closed, missing, "
                            "or not a resolvable workstream.",
                        )
                    existing_workstream_id = int(
                        resolution["resolved_workstream_id"]
                    )
                    if existing_workstream_id != requested_existing_id:
                        old_key = f"existing:{requested_existing_id}"
                        new_key = f"existing:{existing_workstream_id}"
                        for candidate in candidates:
                            if candidate.workstream_key == old_key:
                                candidate.workstream_key = new_key
                        if workstream_scope == old_key:
                            workstream_scope = new_key

                for source in sources or []:
                    source_key = seed_source_import_key(
                        source,
                        project_path=project_path,
                        workstream_scope=workstream_scope,
                    )
                    row = db.begin_seed_source_import(
                        conn,
                        import_key=source_key,
                        source_id=seed_source_identity(source.id),
                        source_agent=source.agent,
                        source_path=source.path,
                        source_mtime=source.mtime,
                        source_digest=source.content_digest,
                        project_path=str(Path(project_path).resolve()),
                        workstream_key=(
                            None if workstream_scope == "project" else workstream_scope
                        ),
                        extractor_name="latch_seed",
                        extractor_version=SEED_EXTRACTOR_VERSION,
                        retry_failed=True,
                        retry_pending=True,
                    )
                    source_rows[source_key] = row
                    source_key_by_ref[
                        (seed_source_identity(source.id), source.content_digest)
                    ] = source_key
                    revision_token = source_revision_token(source)
                    if revision_token in (source_failure_codes or {}):
                        source_error_by_key[source_key] = (
                            source_failure_codes or {}
                        )[revision_token]

                backfill_legacy_seed_claims(conn, project_path=project_path)

                workstream_nodes: dict[str, int] = {}
                if existing_workstream_id is not None:
                    workstream_nodes[f"existing:{int(existing_workstream_id)}"] = int(
                        existing_workstream_id
                    )
                failed_workstreams: set[str] = set(ambiguous_parent_keys)
                candidate_workstream_keys = {
                    candidate.workstream_key
                    for candidate in candidates
                    if candidate.workstream_key
                    and not candidate.workstream_key.startswith("existing:")
                }
                resolved_project = str(Path(project_path).resolve())
                for candidate_key in sorted(candidate_workstream_keys):
                    prior_nodes = db.find_seed_workstream_nodes(
                        conn,
                        project_path=resolved_project,
                        workstream_key=candidate_key,
                    )
                    active = [
                        row for row in prior_nodes if row.get("status") != "stale"
                    ]
                    if len(active) == 1 and len(prior_nodes) == 1:
                        workstream_nodes[candidate_key] = int(active[0]["id"])
                    elif prior_nodes:
                        # Multiple or stale prior parents require explicit human
                        # reconciliation/reopen semantics, never nearest-parent reuse.
                        failed_workstreams.add(candidate_key)
                ordered = sorted(
                    candidates,
                    key=lambda candidate: (
                        0 if candidate.kind == "workstream" else 1,
                        -candidate_rank_score(candidate)[0],
                        candidate.title,
                    ),
                )

                for candidate in ordered:
                    target_workstream_id: int | None = None
                    key = candidate.workstream_key
                    if candidate.kind == "workstream" \
                            and key in failed_workstreams:
                        import_key = candidate_import_key(
                            candidate, project_path=project_path,
                        )
                        mark_candidate_sources(candidate, "candidate_invalid")
                        result.failures.append({
                            "import_key": import_key,
                            "error_code": "candidate_invalid",
                        })
                        continue
                    if candidate.kind == "workstream" and key:
                        target_workstream_id = workstream_nodes.get(key)
                    if candidate.kind != "workstream" and not key \
                            and existing_workstream_id is not None:
                        key = f"existing:{int(existing_workstream_id)}"
                        candidate.workstream_key = key
                    if candidate.kind != "workstream" and key:
                        if key.startswith("existing:"):
                            try:
                                target_workstream_id = int(key.split(":", 1)[1])
                            except ValueError:
                                target_workstream_id = None
                            row = db.get_node(conn, target_workstream_id) \
                                if target_workstream_id is not None else None
                            if row is None or row.get("kind") != "workstream" \
                                    or row.get("status") == "stale":
                                failed_workstreams.add(key)
                        else:
                            target_workstream_id = workstream_nodes.get(key)
                            if target_workstream_id is None:
                                failed_workstreams.add(key)

                    import_key = candidate_import_key(
                        candidate,
                        project_path=project_path,
                        target_workstream_id=(
                            None if candidate.kind == "workstream"
                            else target_workstream_id
                        ),
                    )
                    claim_key = candidate_claim_key(
                        candidate,
                        project_path=project_path,
                        target_workstream_id=(
                            None if candidate.kind == "workstream"
                            else target_workstream_id
                        ),
                    )
                    if candidate.kind == "workstream" \
                            and target_workstream_id is not None:
                        current_parent = db.get_node(conn, target_workstream_id)
                        if current_parent is None or not node_matches_seed_claim(
                            current_parent,
                            candidate,
                            project_path=project_path,
                            target_workstream_id=None,
                            claim_key=claim_key,
                        ):
                            if key:
                                failed_workstreams.add(key)
                            mark_candidate_sources(candidate, "candidate_invalid")
                            result.failures.append({
                                "import_key": import_key,
                                "error_code": "candidate_invalid",
                            })
                            continue
                    if key in failed_workstreams and candidate.kind != "workstream":
                        mark_candidate_sources(candidate, "workstream_attach_failed")
                        result.failures.append({
                            "import_key": import_key,
                            "error_code": "workstream_attach_failed",
                        })
                        continue

                    source_import_keys = [
                        source_key_by_ref[
                            (seed_source_identity(ref["id"]), ref["digest"])
                        ]
                        for ref in candidate_source_refs(candidate)
                        if (
                            seed_source_identity(ref["id"]),
                            ref["digest"],
                        ) in source_key_by_ref
                    ]
                    ledger_row: dict[str, Any] | None = None
                    node_id: int | None = None
                    try:
                        existing = db.get_seed_import(conn, import_key)
                        if existing is not None and existing["state"] == "applied":
                            existing = db.backfill_seed_import_receipt(
                                conn,
                                import_key,
                                claim_key=claim_key,
                                observed_at=latest_candidate_observed_at(candidate),
                            )
                            node_id = int(existing["node_id"])
                            active_node = db.get_node(conn, node_id)
                            if active_node is None \
                                    or active_node.get("status") == "stale" \
                                    or active_node.get("kind") != candidate.kind:
                                if candidate.kind == "workstream" and key:
                                    failed_workstreams.add(key)
                                mark_candidate_sources(candidate, "candidate_invalid")
                                result.failures.append({
                                    "import_key": import_key,
                                    "error_code": "candidate_invalid",
                                })
                                continue
                            result.skipped_import_keys.append(import_key)
                            result.skipped_node_ids.append(node_id)
                            if candidate.kind == "workstream" and key:
                                workstream_nodes[key] = node_id
                                result.workstream_attachments[key] = node_id
                            continue

                        claim_nodes = db.find_seed_claim_nodes(
                            conn, claim_key=claim_key,
                        )
                        valid_claim_nodes = [
                            row for row in claim_nodes
                            if node_matches_seed_claim(
                                row,
                                candidate,
                                project_path=project_path,
                                target_workstream_id=(
                                    None if candidate.kind == "workstream"
                                    else target_workstream_id
                                ),
                                claim_key=claim_key,
                            )
                        ]
                        if claim_nodes and (
                            len(claim_nodes) != 1 or len(valid_claim_nodes) != 1
                        ):
                            if candidate.kind == "workstream" and key:
                                failed_workstreams.add(key)
                            mark_candidate_sources(candidate, "candidate_invalid")
                            result.failures.append({
                                "import_key": import_key,
                                "error_code": "candidate_invalid",
                            })
                            continue
                        claim_reuse_id = (
                            int(valid_claim_nodes[0]["id"])
                            if len(valid_claim_nodes) == 1 else None
                        )

                        ledger_row = db.begin_seed_import(
                            conn,
                            import_key=import_key,
                            claim_key=claim_key,
                            project_path=str(Path(project_path).resolve()),
                            extractor_name="latch_seed",
                            extractor_version=SEED_EXTRACTOR_VERSION,
                            observed_at=latest_candidate_observed_at(candidate),
                            source_import_keys=source_import_keys,
                            source_ids=[
                                seed_source_identity(source_id)
                                for source_id in candidate.source_ids
                            ],
                            workstream_key=key,
                            workstream_id=(
                                None if candidate.kind == "workstream"
                                else target_workstream_id
                            ),
                            retry_failed=True,
                            retry_pending=True,
                        )
                        node_id = (
                            int(ledger_row["node_id"])
                            if ledger_row.get("node_id") is not None
                            else target_workstream_id
                            if candidate.kind == "workstream"
                            and target_workstream_id is not None
                            else claim_reuse_id
                            if claim_reuse_id is not None
                            else _find_import_marker_node(conn, import_key)
                        )
                        if node_id is None:
                            safe_title, _ = redact_seed_text(candidate.title)
                            inserted = heal.insert_with_heal(
                                conn,
                                kind=candidate.kind,
                                title=safe_title,
                                body=body_with_import_receipt(
                                    candidate,
                                    import_key=import_key,
                                    project_path=project_path,
                                    workstream_id=target_workstream_id,
                                ),
                                status="staging",
                                use_llm=False,
                                workstream_id=target_workstream_id,
                                project_path=project_path,
                                artifacts=[{"repo": project_path}],
                            )
                            node_id = int(inserted["id"])
                            db.set_seed_import_node(conn, import_key, node_id)
                            result.inserted_ids.append(node_id)
                        else:
                            db.set_seed_import_node(conn, import_key, node_id)
                            if ledger_row.get("created") and (
                                claim_reuse_id is not None
                                or (
                                    candidate.kind == "workstream"
                                    and target_workstream_id is not None
                                )
                            ):
                                result.corroborated_import_keys.append(import_key)
                                result.corroborated_node_ids.append(node_id)
                            else:
                                result.resumed_import_keys.append(import_key)
                                result.resumed_node_ids.append(node_id)
                            reused_node = db.get_node(conn, node_id)
                            if reused_node is not None \
                                    and reused_node.get("status") == "staging":
                                append_seed_corroboration(
                                    conn, node_id, candidate, import_key,
                                )

                        artifact_store.capture_for_node(
                            conn,
                            node_id,
                            artifacts=[{"repo": project_path}],
                            project_cwd=project_path,
                        )
                        db.finish_seed_import(
                            conn, import_key, state="applied", node_id=node_id,
                        )
                        if candidate.kind == "workstream" and key:
                            workstream_nodes[key] = node_id
                            result.workstream_attachments[key] = node_id
                    except Exception:
                        if candidate.kind == "workstream" and key:
                            failed_workstreams.add(key)
                        # Workstream resolution and validation happen before
                        # this write/checkpoint block.  Failures here describe
                        # the node write, not attachment selection; unresolved
                        # parents are rejected above with the distinct
                        # workstream_attach_failed code.
                        error_code = "node_write_failed"
                        mark_candidate_sources(candidate, error_code)
                        if ledger_row is not None:
                            try:
                                db.finish_seed_import(
                                    conn,
                                    import_key,
                                    state="failed",
                                    node_id=node_id,
                                    error_code=error_code,
                                )
                            except Exception:
                                pass
                        result.failures.append({
                            "import_key": import_key,
                            "error_code": error_code,
                        })

                source_outcomes: dict[str, tuple[str, str | None]] = {}
                for source_key, row in source_rows.items():
                    if row.get("state") == "applied":
                        source_error = source_error_by_key.get(source_key)
                        if source_error:
                            result.failures.append({
                                "import_key": source_key,
                                "error_code": source_error,
                            })
                        continue
                    source_error = source_error_by_key.get(source_key)
                    if not finalize_sources and not source_error:
                        result.pending_source_import_keys.append(source_key)
                        continue
                    source_state = "failed" if source_error else "applied"
                    source_outcomes[source_key] = (source_state, source_error)
                    if source_error:
                        result.failures.append({
                            "import_key": source_key,
                            "error_code": source_error,
                        })
                if source_outcomes:
                    try:
                        db.finish_seed_source_imports(conn, source_outcomes)
                    except Exception:
                        for source_key in source_outcomes:
                            result.failures.append({
                                "import_key": source_key,
                                "error_code": "internal",
                            })
            finally:
                conn.close()
    except SeedWriteBlocked:
        raise
    except Exception as exc:
        # The lock timeout occurs before ledger or node writes. Do not expose
        # local paths or raw exception text in the structured receipt.
        reason = "compaction_in_progress" \
            if exc.__class__.__name__ == "CompactionInProgressError" else "write_failed"
        raise SeedWriteBlocked(
            reason,
            "Initial-KB apply is retryable; no batch lock was acquired."
            if reason == "compaction_in_progress" else
            "Initial-KB apply could not start safely.",
        ) from exc
    return result


def clip(text: str, limit: int) -> str:
    text = WHITESPACE_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 15)].rstrip() + "...[truncated]"


def emit_seed_cli_failure(
    args: argparse.Namespace,
    *,
    code: str,
    message: str,
) -> None:
    if args.format == "json":
        print(json.dumps({"ok": False, "error": code}))
        print(message, file=sys.stderr)
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dismiss_all and not args.apply:
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message="--dismiss-all requires --apply.",
        )
        return 2
    if args.dismiss_all and not args.preview_digest:
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message=(
                "--dismiss-all requires the exact --preview-digest from the "
                "reviewed preview."
            ),
        )
        return 2
    if args.dismiss_all and (
        args.yes or args.approve_candidate or args.approve_cluster
    ):
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message=(
                "--dismiss-all cannot be combined with --yes, "
                "--approve-candidate, or --approve-cluster."
            ),
        )
        return 2
    if args.yes and (args.approve_candidate or args.approve_cluster):
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message=(
                "--yes cannot be combined with --approve-candidate or "
                "--approve-cluster."
            ),
        )
        return 2
    if (args.approve_candidate or args.approve_cluster) and not args.apply:
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message="--approve-candidate and --approve-cluster require --apply.",
        )
        return 2
    if args.preview_digest and not args.apply:
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message="--preview-digest is valid only with --apply.",
        )
        return 2
    if (args.approve_candidate or args.approve_cluster) \
            and not args.preview_digest:
        emit_seed_cli_failure(
            args,
            code="invalid_seed_arguments",
            message=(
                "Scoped --approve-candidate/--approve-cluster requires the exact "
                "--preview-digest from the reviewed preview. Alternatively run "
                "--apply without selectors and choose IDs in that same prompt."
            ),
        )
        return 2
    disabled = no_llm_disabled_reason(args)
    if disabled:
        print(disabled, file=sys.stderr)
        return 2
    prompt_choices(args)
    args.project = str(Path(args.project).resolve())
    if args.cursor_history and "cursor" not in source_agents(args.source):
        print(
            "--cursor-history requires Cursor in the selected source; use "
            "--source cursor or --source all (or --source auto when it "
            "resolves to one of them).",
            file=sys.stderr,
        )
        return 2
    if args.max_sessions is None or args.max_sessions <= 0:
        print("--last-sessions must be positive.", file=sys.stderr)
        return 2
    # max_candidates is None when unset — it then scales with sessions read
    # (effective_max_candidates); only an explicit value is range-checked.
    if (args.max_candidates is not None and args.max_candidates <= 0) \
            or args.max_llm_calls < 0:
        print("--max-candidates must be positive and --max-llm-calls non-negative.", file=sys.stderr)
        return 2
    if args.max_llm_calls > HARD_MAX_LLM_CALLS_CEILING:
        print(
            f"--max-llm-calls cannot exceed the hard "
            f"{HARD_MAX_LLM_CALLS_CEILING}-call initial-KB boundary.",
            file=sys.stderr,
        )
        return 2
    if args.workstream_id is not None and args.workstream_id <= 0:
        print("--workstream-id must be a positive node id.", file=sys.stderr)
        return 2
    if args.workstream_id is not None:
        resolved_workstream_id, workstream_error = resolve_existing_workstream_target(
            args.project, args.workstream_id,
        )
        if workstream_error:
            print(
                f"--workstream-id {args.workstream_id} is unavailable: "
                f"{workstream_error}.",
                file=sys.stderr,
            )
            return 2
        args.workstream_id = resolved_workstream_id
    if args.new_workstream is not None:
        safe_workstream, _ = redact_seed_text(args.new_workstream.strip())
        if not safe_workstream:
            print("--new-workstream needs a non-empty title.", file=sys.stderr)
            return 2
        args.new_workstream = safe_workstream
    scope_key = workstream_scope_key(args)
    args.sources_skipped_unchanged = 0
    args.sources_selected = 0
    args.sources_deferred = 0
    args.llm_stats = {}
    args.discovery_stats = {}
    args.preview_cache_unavailable = False
    cursor_session_preview = uses_cursor_session_preview(args)
    cached_cursor_apply = cursor_session_preview and args.apply
    cached_seed_apply = (
        not cursor_session_preview
        and args.apply
        and bool(args.preview_digest)
    )
    if cached_cursor_apply:
        if not args.cursor_session_id or not args.preview_digest:
            emit_seed_cli_failure(
                args,
                code="invalid_seed_arguments",
                message=(
                    "Cursor seed apply requires --cursor-session-id and the exact "
                    "--preview-digest returned by the reviewed preview."
                ),
            )
            return 2
        try:
            (
                sources,
                candidates,
                llm_estimate,
                apply_sources,
                apply_failure_codes,
                cached_scope,
                cached_llm_stats,
                cached_discovery_stats,
                cached_refinement_empty,
            ) = load_cursor_seed_preview(
                project_path=args.project,
                session_id=args.cursor_session_id,
                preview_digest=args.preview_digest,
                include_apply_state=True,
            )
        except CursorSeedPreviewError as exc:
            emit_seed_cli_failure(
                args,
                code="seed_preview_unavailable",
                message=f"Cursor seed apply unavailable: {exc}",
            )
            return 2
        if cached_scope != scope_key:
            emit_seed_cli_failure(
                args,
                code="seed_preview_scope_mismatch",
                message=(
                    "Cursor seed apply scope does not match the reviewed preview; "
                    "rerun preview with the same workstream options."
                ),
            )
            return 2
        args.llm_refinement_empty = cached_refinement_empty
        args.llm_stats = {**cached_llm_stats, "cached_preview": True}
        args.discovery_stats = cached_discovery_stats
        args.sources_selected = len(sources)
        attempted_tokens = {
            source_revision_token(source) for source in apply_sources
        }
        args.sources_deferred = sum(
            source_revision_token(source) not in attempted_tokens
            for source in sources
        )
    elif cached_seed_apply:
        try:
            (
                sources,
                candidates,
                llm_estimate,
                apply_sources,
                apply_failure_codes,
                cached_scope,
                cached_llm_stats,
                cached_discovery_stats,
                cached_refinement_empty,
            ) = load_seed_preview(
                project_path=args.project,
                source_choice=args.source,
                preview_digest=args.preview_digest,
                cursor_history=bool(args.cursor_history),
            )
        except SeedPreviewError as exc:
            emit_seed_cli_failure(
                args,
                code="seed_preview_unavailable",
                message=f"Seed apply unavailable: {exc}",
            )
            return 2
        if cached_scope != scope_key:
            emit_seed_cli_failure(
                args,
                code="seed_preview_scope_mismatch",
                message=(
                    "Seed apply scope does not match the reviewed preview; rerun "
                    "preview with the same workstream options."
                ),
            )
            return 2
        args.llm_refinement_empty = cached_refinement_empty
        args.llm_stats = {**cached_llm_stats, "cached_preview": True}
        args.discovery_stats = cached_discovery_stats
        args.sources_selected = len(sources)
        attempted_tokens = {
            source_revision_token(source) for source in apply_sources
        }
        args.sources_deferred = sum(
            source_revision_token(source) not in attempted_tokens
            for source in sources
        )
    else:
        try:
            sources = discover_sources(
                source=args.source,
                project_path=args.project,
                lookback_days=args.lookback_days,
                max_sessions=args.max_sessions,
                claude_home=args.claude_home,
                codex_home=args.codex_home,
                cursor_transcripts=args.cursor_transcript,
                cursor_session_id=args.cursor_session_id,
                cursor_history=args.cursor_history,
                cursor_home=args.cursor_home,
                cursor_state_db=args.cursor_state_db,
                all_projects=args.all_projects,
                focus_query=args.new_workstream,
                stats=args.discovery_stats,
            )
        except cursor_transcript.CursorTranscriptError as e:
            print(f"Cursor seed source unavailable: {e}", file=sys.stderr)
            return 2
        if not args.force_reimport:
            try:
                sources, already_applied = split_applied_sources(
                    sources,
                    project_path=args.project,
                    workstream_scope=scope_key,
                )
            except SeedSourceLedgerError:
                emit_seed_cli_failure(
                    args,
                    code="seed_source_ledger_unavailable",
                    message=(
                        "Seed extraction stopped because the existing source "
                        "ledger could not be read safely."
                    ),
                )
                return 1
            args.sources_skipped_unchanged = len(already_applied)

        args.sources_selected = len(sources)

        if args.llm == "yes" and sources \
                and not prepare_llm_budget_storage(args.project):
            if args.format == "json":
                print(json.dumps({
                    "ok": False,
                    "error": "budget_storage_unavailable",
                }))
            return 1

        # Coverage is confirmed against every eligible session, before the cap
        # truncates anything. planned_llm_sources() already applies the cap, so
        # asking after it would report coverage of the truncated list as full
        # and hide the shortfall this prompt exists to surface. The answer can
        # raise the cap or shrink the window, so the source list is only fixed
        # afterwards -- which is also why consent runs on the final list.
        if not confirm_llm_budget(args, len(sources), args.project):
            emit_seed_cli_failure(
                args,
                code="llm_budget_cancelled",
                message="Seed pass cancelled before any LLM calls.",
            )
            return 1

        if getattr(args, "rescan_after_window_change", False):
            cutoff = utc_now() - timedelta(days=args.lookback_days)
            sources = [s for s in sources if _source_mtime_at_or_after(s, cutoff)]
            args.sources_selected = len(sources)
            args.coverage_plan = coverage_plan(args, len(sources), args.project)

        call_sources = (
            planned_llm_sources(sources, max_calls=args.max_llm_calls)
            if args.llm == "yes" else sources
        )
        llm_estimate = estimate_llm_calls(
            len(call_sources),
            calls_per_session=1,
            max_llm_calls=args.max_llm_calls,
        ) if args.llm == "yes" else 0

        if not confirm_source_use(args, call_sources):
            emit_seed_cli_failure(
                args,
                code="source_consent_cancelled",
                message="Initial-KB extraction cancelled before any LLM calls.",
            )
            return 1

        llm = []
        if args.llm == "yes" and call_sources:
            llm = llm_candidates(
                call_sources,
                project_path=args.project,
                max_calls=args.max_llm_calls,
                max_candidates=effective_max_candidates(args),
                backend=args.backend,
                focus_workstream=args.new_workstream,
                stats=args.llm_stats,
            )
            if args.llm_stats.get("budget_storage_unavailable"):
                if args.format == "json":
                    print(json.dumps({
                        "ok": False,
                        "error": "budget_storage_unavailable",
                    }))
                return 1
        if args.llm == "yes":
            attempted_revisions = set(
                args.llm_stats.get("source_revision_tokens", [])
            )
            accepted_by_revision = args.llm_stats.get(
                "accepted_candidates_by_revision", {}
            )
            # Deterministic extraction may corroborate a source only after that
            # source returned at least one accepted model candidate. A valid
            # empty model result stays empty rather than being bypassed.
            deterministic_sources = [
                source for source in call_sources
                if int(accepted_by_revision.get(source_revision_token(source), 0)) > 0
            ]
            apply_sources = [
                source for source in call_sources
                if source_revision_token(source) in attempted_revisions
            ]
            deferred_ids = [
                source.id for source in sources
                if source_revision_token(source) not in attempted_revisions
            ]
            args.llm_stats["planned_source_ids"] = [
                source.id for source in call_sources
            ]
            args.llm_stats["deferred_source_ids"] = deferred_ids
            args.sources_deferred = len(deferred_ids)
        else:
            deterministic_sources = sources
            apply_sources = sources
        deterministic = deterministic_candidates(
            deterministic_sources, max_candidates=effective_max_candidates(args),
        )
        if args.llm == "yes":
            deterministic = [
                candidate for candidate in deterministic
                if candidate.kind != "workstream"
            ]
        candidates, llm_refinement_empty = choose_seed_candidates(args, llm, deterministic)
        candidates = sanitize_reserved_workstream_keys(
            candidates, existing_workstream_id=args.workstream_id,
        )
        candidates = resolve_unambiguous_workstream_links(candidates)
        candidates = apply_requested_workstream_scope(
            candidates,
            new_workstream=args.new_workstream,
            workstream_id=args.workstream_id,
            max_candidates=effective_max_candidates(args),
        )
        args.llm_refinement_empty = bool(
            llm_refinement_empty
            or (
                args.llm == "yes"
                and bool(sources)
                and args.llm_stats.get("succeeded", 0) == 0
            )
        )
        apply_failure_codes = {
            revision: "extractor_failed"
            for revision in args.llm_stats.get(
                "failed_source_revision_tokens", []
            )
        }
        if cursor_session_preview:
            if not args.cursor_session_id:
                print("Cursor seed preview requires --cursor-session-id.", file=sys.stderr)
                return 2
            try:
                args.preview_digest = write_cursor_seed_preview(
                    project_path=args.project,
                    session_id=args.cursor_session_id,
                    sources=sources,
                    candidates=candidates,
                    llm_estimate=llm_estimate,
                    apply_sources=apply_sources,
                    source_failure_codes=apply_failure_codes,
                    workstream_scope=scope_key,
                    llm_stats=args.llm_stats,
                    discovery_stats=args.discovery_stats,
                    llm_refinement_empty=args.llm_refinement_empty,
                )
            except OSError:
                emit_seed_cli_failure(
                    args,
                    code="seed_preview_cache_unavailable",
                    message=(
                        "Cursor seed preview stopped because its exact-review "
                        "cache could not be written safely."
                    ),
                )
                return 1
        elif candidates and not args.apply:
            try:
                args.preview_digest = write_seed_preview(
                    project_path=args.project,
                    source_choice=args.source,
                    sources=sources,
                    candidates=candidates,
                    llm_estimate=llm_estimate,
                    apply_sources=apply_sources,
                    source_failure_codes=apply_failure_codes,
                    workstream_scope=scope_key,
                    llm_stats=args.llm_stats,
                    discovery_stats=args.discovery_stats,
                    llm_refinement_empty=args.llm_refinement_empty,
                    cursor_history=bool(args.cursor_history),
                )
            except OSError:
                args.preview_digest = None
                args.preview_cache_unavailable = True
                print(
                    "Seed preview remains available, but exact-preview caching "
                    "is unavailable at the configured Latch vault. Scoped "
                    "non-interactive apply is disabled; use one interactive "
                    "--apply run or repair vault write access.",
                    file=sys.stderr,
                )

    if args.dismiss_all and not candidates:
        emit_seed_cli_failure(
            args,
            code="seed_approval_selection_unavailable",
            message=(
                "--dismiss-all requires a reviewed preview containing at least "
                "one candidate."
            ),
        )
        return 2

    output = (
        render_json(
            args=args,
            sources=sources,
            candidates=candidates,
            llm_estimate=llm_estimate,
        )
        if args.format == "json"
        else render_text(
            args=args,
            sources=sources,
            candidates=candidates,
            llm_estimate=llm_estimate,
        )
    )
    if not args.apply:
        print(output, end="")
        return 0
    status_stream = sys.stderr if args.format == "json" else sys.stdout
    if args.format == "text":
        print(output, end="")
    elif (
        candidates
        and not args.yes
        and not args.dismiss_all
        and not args.approve_candidate
        and not args.approve_cluster
    ):
        # Interactive JSON apply still needs to expose the review IDs, while
        # stdout remains reserved for exactly one final machine receipt.
        print(output, end="", file=sys.stderr)
    if args.llm_refinement_empty and not candidates:
        emit_seed_cli_failure(
            args,
            code="llm_extraction_incomplete",
            message=(
                "Nothing was applied because LLM extraction did not complete "
                "successfully."
            ),
        )
        return 1

    approved_candidates = whole_report_approval(candidates)
    selection_confirmed = bool(args.yes) or not candidates
    if args.dismiss_all:
        approved_candidates = []
        selection_confirmed = True
    elif args.approve_candidate or args.approve_cluster:
        try:
            approved_candidates, _selection_covers_all = resolve_approval_selection(
                candidates,
                candidate_ids=args.approve_candidate,
                cluster_ids=args.approve_cluster,
            )
        except SeedApprovalError as exc:
            emit_seed_cli_failure(
                args,
                code="seed_approval_selection_unavailable",
                message=f"Seed approval selection unavailable: {exc}",
            )
            return 2
        selection_confirmed = True
    elif candidates and not args.yes:
        approved_candidates, selection_finalized = prompt_approval_selection(
            candidates,
            stream=status_stream,
        )
        selection_confirmed = bool(approved_candidates) or selection_finalized
    if candidates and not approved_candidates and not selection_confirmed:
        emit_seed_cli_failure(
            args,
            code="seed_approval_cancelled",
            message="Seed candidates were not written because approval was cancelled.",
        )
        return 1
    dismissed_all = bool(
        args.dismiss_all
        or (candidates and selection_confirmed and not approved_candidates)
    )
    try:
        applied = apply_candidates(
            approved_candidates,
            project_path=args.project,
            existing_workstream_id=args.workstream_id,
            sources=apply_sources,
            workstream_scope=scope_key,
            source_failure_codes=apply_failure_codes,
            # Choosing a scoped subset is a completed review: selected items
            # are approved and every unselected item is intentionally dismissed
            # for these exact source revisions.
            finalize_sources=True,
        )
    except SeedWriteBlocked as exc:
        emit_seed_cli_failure(
            args,
            code=exc.reason,
            message=f"Initial-KB apply blocked ({exc.reason}): {exc}",
        )
        return 1
    if isinstance(applied, list):  # compatibility for embedders/tests of the old helper
        applied = SeedApplyResult(inserted_ids=list(applied))
    if cached_cursor_apply and args.cursor_session_id and applied.complete:
        remove_cursor_seed_preview(args.project, args.cursor_session_id)
    if cached_seed_apply and args.preview_digest and applied.complete:
        remove_seed_preview(args.project, args.preview_digest)
    print(
        apply_success_message(applied, approved_candidates),
        file=status_stream,
    )
    if args.format == "json":
        final_payload = json.loads(output)
        approved_demo = (
            catch_demo_candidate(approved_candidates)
            if applied.complete else None
        )
        final_payload.update({
            "ok": applied.complete,
            "apply": True,
            "write_performed": True,
            "catch_demo": (
                catch_demo_payload(approved_demo) if approved_demo else None
            ),
            "apply_receipt": public_apply_receipt(
                applied,
                approved_candidates=approved_candidates,
                dismissed_all=dismissed_all,
            ),
        })
        if not applied.complete:
            final_payload["error"] = "apply_incomplete"
        print(json.dumps(final_payload, indent=2))
    return 0 if applied.complete else 1


if __name__ == "__main__":
    sys.exit(main())
