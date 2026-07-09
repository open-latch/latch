"""Deterministic evals for the install-time seed report.

These evals are the local instrument panel for the seed/report first-wow path:
small but messy Claude/Codex transcript bundles go through the seed
candidate/report code, then deterministic checks grade whether the report
captured the durable project signals latch cares about. The runner uses no
model calls; synthetic LLM-shaped candidates exercise the agent-mistake
reporting path without turning no-LLM seeding into a public product mode.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import seed  # noqa: E402


@dataclass(frozen=True)
class ReportCheck:
    id: str
    kind: str
    section: str
    phrases: tuple[str, ...]
    signal: str | None = None
    must_have_evidence: bool = True
    should_match: bool = True


@dataclass(frozen=True)
class RealSmokeOptions:
    project: str
    source: str
    lookback_days: int
    max_sessions: int
    claude_home: str
    codex_home: str
    all_projects: bool = False
    max_candidates: int = 20


SECTION_LABELS = {
    "continuity_notes": "continuity notes",
    "where_left_off": "next steps / open questions",
    "decisions_and_rejected_paths": "decisions / rejected paths",
    "patterns_and_preferences": "patterns / preferences",
    "agent_alignment_check": "agent alignment check",
}

DEFAULT_CHECKS = (
    ReportCheck(
        id="internal_workstream_handoff",
        kind="ongoing_workstream",
        section="continuity_notes",
        phrases=("launch workstream handoff",),
        signal="ongoing_workstream",
    ),
    ReportCheck(
        id="next_step_followup",
        kind="next_step",
        section="where_left_off",
        phrases=("installer screenshots",),
        signal="open_question",
    ),
    ReportCheck(
        id="redis_rejected_path",
        kind="decision_rejected_path",
        section="decisions_and_rejected_paths",
        phrases=("not to use redis", "another service"),
        signal="rejected_path",
    ),
    ReportCheck(
        id="background_queue_rejected_path",
        kind="governance_rule",
        section="decisions_and_rejected_paths",
        phrases=("rejected adding a background job queue", "inline task runner"),
        signal="rejected_path",
    ),
    ReportCheck(
        id="oauth_popup_rejected_path",
        kind="governance_rule",
        section="decisions_and_rejected_paths",
        phrases=("do not use oauth popup fallback", "redirect-based login"),
        signal="rejected_path",
    ),
    ReportCheck(
        id="preview_preference",
        kind="pattern_preference",
        section="patterns_and_preferences",
        phrases=("preview seed writes",),
        signal="preference",
    ),
    ReportCheck(
        id="config_merge_preference",
        kind="pattern_preference",
        section="patterns_and_preferences",
        phrases=("merge config changes", "clobber unrelated hooks"),
        signal="preference",
    ),
    ReportCheck(
        id="agent_revived_rejected_path",
        kind="agent_mistake",
        section="agent_alignment_check",
        phrases=("agent revived redis", "violated the prior rejection"),
        signal="possible_agent_mistake",
    ),
    ReportCheck(
        id="agent_added_queue_after_rule",
        kind="agent_mistake",
        section="agent_alignment_check",
        phrases=("agent added a background job queue", "violated the prior rule"),
        signal="possible_agent_mistake",
    ),
    ReportCheck(
        id="low_confidence_agent_mistake_filtered",
        kind="agent_mistake_negative_control",
        section="agent_alignment_check",
        phrases=("low confidence redis mistake",),
        must_have_evidence=False,
        should_match=False,
    ),
    ReportCheck(
        id="user_blaming_agent_mistake_filtered",
        kind="agent_mistake_negative_control",
        section="agent_alignment_check",
        phrases=("user violated redis rejection",),
        must_have_evidence=False,
        should_match=False,
    ),
    ReportCheck(
        id="retroactive_agent_mistake_filtered",
        kind="agent_mistake_negative_control",
        section="agent_alignment_check",
        phrases=("agent missed later redis clarification",),
        must_have_evidence=False,
        should_match=False,
    ),
    ReportCheck(
        id="transient_branch_noise_filtered",
        kind="noise_negative_control",
        section="decisions_and_rejected_paths",
        phrases=("main branch drift",),
        must_have_evidence=False,
        should_match=False,
    ),
    ReportCheck(
        id="injected_context_filtered",
        kind="noise_negative_control",
        section="decisions_and_rejected_paths",
        phrases=("injected context",),
        must_have_evidence=False,
        should_match=False,
    ),
    ReportCheck(
        id="old_out_of_scope_transcript_filtered",
        kind="source_filter_negative_control",
        section="decisions_and_rejected_paths",
        phrases=("old out-of-scope redis rule",),
        must_have_evidence=False,
        should_match=False,
    ),
    ReportCheck(
        id="unrelated_project_transcript_filtered",
        kind="source_filter_negative_control",
        section="decisions_and_rejected_paths",
        phrases=("unrelated project queue rule",),
        must_have_evidence=False,
        should_match=False,
    ),
)


def run_seed_report_eval(real_smoke: RealSmokeOptions | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    root = Path(tempfile.mkdtemp(prefix="latch-seed-report-eval-"))
    try:
        project = root / "project" / "latch-fixture"
        project.mkdir(parents=True)
        claude_home = root / ".claude"
        codex_home = root / ".codex"
        transcript_manifest = write_transcript_bundle(
            project=project,
            claude_home=claude_home,
            codex_home=codex_home,
        )
        sources = seed.discover_sources(
            source="both",
            project_path=str(project),
            lookback_days=30,
            max_sessions=10,
            claude_home=str(claude_home),
            codex_home=str(codex_home),
            now=datetime.now(timezone.utc),
        )
        deterministic = seed.deterministic_candidates(sources, max_candidates=40)
        llm_synthetic, llm_filtered = synthetic_llm_candidate_probe(sources)
        candidates = seed.merge_candidate_sets(
            llm_synthetic,
            deterministic,
            max_candidates=40,
        )
        report = seed.build_seed_report(candidates)
        check_results = grade_report(report, DEFAULT_CHECKS)
        receipt = seed.seed_report_receipt(sources=sources, candidates=candidates)
        demo = seed.catch_demo_candidate(candidates)
        section_counts = {section.key: len(section.items) for section in report}
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        passed = sum(1 for row in check_results if row["passed"])
        result = {
            "ok": passed == len(check_results),
            "thesis": (
                "Seed-report evals grade whether latch's install-time seed "
                "surface finds durable project state with evidence before "
                "first compact: continuity notes, next steps, decisions and "
                "rejected paths, preferences, direction signals, and "
                "high-confidence agent-alignment findings."
            ),
            "summary": {
                "checks": len(check_results),
                "passed": passed,
                "failed": len(check_results) - passed,
                "pass_rate": passed / len(check_results) if check_results else 0.0,
                "elapsed_ms": elapsed_ms,
                "source_counts": seed.source_counts(sources),
                "candidate_count": len(candidates),
                "synthetic_llm_candidate_count": len(llm_synthetic),
                "synthetic_llm_filtered_count": len(llm_filtered),
                "section_counts": section_counts,
                "catch_demo": bool(demo),
            },
            "receipt": receipt,
            "catch_demo": (
                seed.catch_demo_payload(demo) if demo else None
            ),
            "sections": [
                {
                    "key": section.key,
                    "title": section.title,
                    "item_count": len(section.items),
                    "item_titles": [item.title for item in section.items],
                }
                for section in report
            ],
            "checks": check_results,
            "transcripts": transcript_manifest,
            "synthetic_llm_filtered": llm_filtered,
            "notes": [
                "No model calls are made by this runner.",
                (
                    "agent_alignment_check is exercised with synthetic "
                    "LLM-shaped contradiction candidates from fixture evidence; "
                    "the public seed CLI remains LLM-backed."
                ),
                (
                    "Old and unrelated-project transcripts are written into the "
                    "sandbox but should not appear in selected sources."
                ),
            ],
        }
        if real_smoke is not None:
            result["real_smoke"] = run_real_conversation_smoke(real_smoke)
        return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def write_transcript_bundle(
    *,
    project: Path,
    claude_home: Path,
    codex_home: Path,
) -> list[dict[str, str]]:
    encoded = seed._encoded_claude_project_path(str(project.resolve()))
    now = datetime.now(timezone.utc).timestamp()
    manifest: list[dict[str, str]] = []

    codex_path = codex_home / "sessions" / "2026" / "06" / "22" / "rollout-product-constraints.jsonl"
    write_jsonl(codex_path, [
        {
            "type": "session_meta",
            "payload": {"id": "seed-report-eval", "cwd": str(project.resolve())},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": (
                    "We decided not to use Redis for local state because it "
                    "adds another service to operate."
                ),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "Always preview seed writes before applying them.",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": (
                    "We rejected adding a background job queue for the "
                    "no-history demo; use an inline task runner instead."
                ),
            },
        },
    ], mtime=now + 20)
    manifest.append({"source": "codex", "path": str(codex_path), "selected": "yes"})

    claude_path = claude_home / "projects" / encoded / "seed-report-next-steps.jsonl"
    write_jsonl(claude_path, [
        {"type": "system", "cwd": str(project.resolve())},
        {
            "type": "user",
            "message": {
                "content": (
                    "Open question: we need to decide the launch workstream "
                    "handoff before the next PR."
                ),
            },
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "Circle back on installer screenshots after the seed demo."
                ),
            },
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "Always merge config changes into existing settings rather "
                    "than clobber unrelated hooks."
                ),
            },
        },
    ], mtime=now + 10)
    manifest.append({"source": "claude", "path": str(claude_path), "selected": "yes"})

    agent_path = codex_home / "sessions" / "2026" / "06" / "22" / "rollout-agent-mistake-evidence.jsonl"
    write_jsonl(agent_path, [
        {
            "type": "session_meta",
            "payload": {"id": "seed-report-agent-mistake", "cwd": str(project.resolve())},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": (
                    "We decided not to use Redis for local state because it "
                    "adds another service to operate."
                ),
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "I added Redis setup for local state and updated service config.",
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "I also added a background job queue worker for email delivery.",
            },
        },
    ], mtime=now + 30)
    manifest.append({"source": "codex", "path": str(agent_path), "selected": "yes"})

    governance_path = claude_home / "projects" / encoded / "seed-report-governance.jsonl"
    write_jsonl(governance_path, [
        {"type": "system", "cwd": str(project.resolve())},
        {
            "type": "user",
            "message": {
                "content": (
                    "Project rule: do not use OAuth popup fallback for mobile "
                    "login; keep redirect-based login because popup state is fragile."
                ),
            },
        },
        {
            "type": "user",
            "message": {
                "content": (
                    "We decided to keep webhooks in pages/api until auth parity "
                    "tests exist instead of migrating to the app router."
                ),
            },
        },
    ], mtime=now + 5)
    manifest.append({"source": "claude", "path": str(governance_path), "selected": "yes"})

    noise_path = claude_home / "projects" / encoded / "seed-report-noise.jsonl"
    write_jsonl(noise_path, [
        {"type": "system", "cwd": str(project.resolve())},
        {
            "type": "user",
            "message": {
                "content": (
                    "## KB hits\n"
                    "- Always avoid Redis. This is injected context, not a new user decision."
                ),
            },
        },
        {
            "type": "user",
            "message": {"content": "Please inspect the seed report output only."},
        },
    ], mtime=now)
    manifest.append({"source": "claude", "path": str(noise_path), "selected": "yes"})

    old_path = codex_home / "sessions" / "2026" / "04" / "01" / "rollout-old-out-of-scope.jsonl"
    old_mtime = now - (45 * 24 * 60 * 60)
    write_jsonl(old_path, [
        {
            "type": "session_meta",
            "payload": {"id": "old-out-of-scope", "cwd": str(project.resolve())},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "We decided not to use the old out-of-scope Redis rule.",
            },
        },
    ], mtime=old_mtime)
    manifest.append({"source": "codex", "path": str(old_path), "selected": "no-lookback"})

    other_project = project.parent / "other-project"
    other_project.mkdir(parents=True)
    other_path = codex_home / "sessions" / "2026" / "06" / "22" / "rollout-unrelated-project.jsonl"
    write_jsonl(other_path, [
        {
            "type": "session_meta",
            "payload": {"id": "unrelated-project", "cwd": str(other_project.resolve())},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "We decided not to use the unrelated project queue rule.",
            },
        },
    ], mtime=now + 40)
    manifest.append({"source": "codex", "path": str(other_path), "selected": "no-project"})
    return manifest


def write_jsonl(path: Path, rows: list[dict[str, Any]], *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def synthetic_agent_mistake_candidates(sources: list[seed.SeedSource]) -> list[seed.SeedCandidate]:
    kept, _filtered = synthetic_llm_candidate_probe(sources)
    return kept


def synthetic_llm_candidate_probe(
    sources: list[seed.SeedSource],
) -> tuple[list[seed.SeedCandidate], list[dict[str, str]]]:
    source = next((src for src in sources if src.agent == "codex"), None)
    if source is None:
        return [], []
    items = [
        {
            "kind": "fact",
            "title": "Agent revived Redis after rejection",
            "body": (
                "The agent revived Redis after the user rejected Redis for local "
                "state. This violated the prior rejection because Redis adds "
                "another service to operate."
            ),
            "confidence": 0.91,
            "signals": ["possible_agent_mistake"],
        },
        {
            "kind": "fact",
            "title": "Agent added a background job queue after rule",
            "body": (
                "The agent added a background job queue after the user said not "
                "to add a background job queue for the no-history demo. This "
                "violated the prior rule; the compliant path is an inline task runner."
            ),
            "confidence": 0.9,
            "signals": ["possible_agent_mistake"],
        },
        {
            "kind": "fact",
            "title": "Low confidence Redis mistake",
            "body": (
                "The agent may have touched Redis, but the transcript evidence is "
                "ambiguous and should not be treated as a high-confidence mistake."
            ),
            "confidence": 0.7,
            "signals": ["possible_agent_mistake"],
        },
        {
            "kind": "fact",
            "title": "User violated Redis rejection",
            "body": "The user violated the earlier Redis rejection.",
            "confidence": 0.92,
            "signals": ["possible_agent_mistake"],
        },
        {
            "kind": "fact",
            "title": "Agent missed later Redis clarification",
            "body": (
                "With hindsight, later user-provided information clarified Redis "
                "should not be used, but the agent did not have that information at the time."
            ),
            "confidence": 0.93,
            "signals": ["possible_agent_mistake"],
        },
        {
            "kind": "decision",
            "title": "Main branch drift",
            "body": "The main branch worktree was behind origin and then fast-forwarded.",
            "confidence": 0.88,
            "signals": ["decision"],
        },
    ]
    kept: list[seed.SeedCandidate] = []
    filtered: list[dict[str, str]] = []
    for item in items:
        cand = seed.candidate_from_llm_item(item, source)
        if cand is None:
            filtered.append({
                "title": str(item.get("title") or ""),
                "reason": "filtered by seed candidate quality rules",
            })
        else:
            kept.append(cand)
    return kept, filtered


def run_real_conversation_smoke(options: RealSmokeOptions) -> dict[str, Any]:
    """Preview-only local smoke over real transcript folders.

    This intentionally makes no model calls and writes nothing. It is for a
    local operator who wants to see whether a fresh seed pass finds any durable
    signal in existing conversations without turning private transcripts into
    CI fixtures.
    """
    project = str(Path(options.project).resolve())
    sources = seed.discover_sources(
        source=options.source,
        project_path=project,
        lookback_days=options.lookback_days,
        max_sessions=options.max_sessions,
        claude_home=options.claude_home,
        codex_home=options.codex_home,
        all_projects=options.all_projects,
        now=datetime.now(timezone.utc),
    )
    candidates = seed.deterministic_candidates(
        sources,
        max_candidates=options.max_candidates,
    )
    report = seed.build_seed_report(candidates)
    demo = seed.catch_demo_candidate(candidates)
    section_counts = {section.key: len(section.items) for section in report}
    return {
        "preview_only": True,
        "writes_enabled": False,
        "llm_calls": 0,
        "project": project,
        "source": options.source,
        "lookback_days": options.lookback_days,
        "max_sessions": options.max_sessions,
        "all_projects": options.all_projects,
        "sources_scanned": len(sources),
        "source_counts": seed.source_counts(sources),
        "source_refs": [public_source_ref(src) for src in sources],
        "candidate_count": len(candidates),
        "section_counts": section_counts,
        "receipt": (
            seed.seed_report_receipt(sources=sources, candidates=candidates)
            if candidates else None
        ),
        "catch_demo": real_smoke_demo_summary(demo),
        "notes": [
            "Manual smoke only; not used by CI.",
            "No seed writes are applied.",
            "No model calls are made; this checks deterministic capture and report wiring.",
            "Transcript bodies are not included in this result.",
        ],
    }


def real_smoke_demo_summary(candidate: seed.SeedCandidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "available": True,
        "requires_apply": True,
        "candidate_kind": candidate.kind,
        "signals": sorted(seed.normalized_signals(candidate.signals)),
        "source_count": len(candidate.source_ids),
        "redaction": (
            "Candidate title, request text, transcript excerpts, and source paths "
            "are omitted from real-smoke output. Run latch_seed.sh preview to inspect "
            "the private local report interactively."
        ),
    }


def public_source_ref(src: seed.SeedSource) -> dict[str, str]:
    return {
        "agent": src.agent,
        "id": src.id,
        "path_tail": path_tail(src.path),
        "mtime": src.mtime,
    }


def path_tail(path: str, *, parts: int = 4) -> str:
    return "/".join(Path(path).parts[-parts:])


def grade_report(
    report: list[seed.SeedReportSection],
    checks: tuple[ReportCheck, ...],
) -> list[dict[str, Any]]:
    by_key = {section.key: section for section in report}
    return [grade_check(by_key, check) for check in checks]


def grade_check(by_key: dict[str, seed.SeedReportSection], check: ReportCheck) -> dict[str, Any]:
    section = by_key.get(check.section)
    items = section.items if section else []
    matches = [item for item in items if item_matches(item, check)]
    matched = bool(matches)
    evidence_ok = True
    if matched and check.must_have_evidence:
        evidence_ok = all(item_has_evidence(item) for item in matches)
    passed = (matched if check.should_match else not matched) and evidence_ok
    return {
        "id": check.id,
        "kind": check.kind,
        "section": check.section,
        "section_label": SECTION_LABELS.get(check.section, check.section),
        "expected": "present" if check.should_match else "absent",
        "passed": passed,
        "matched_titles": [item.title for item in matches],
        "phrases": list(check.phrases),
        "signal": check.signal,
        "evidence_ok": evidence_ok,
    }


def item_matches(item: seed.SeedCandidate, check: ReportCheck) -> bool:
    haystack = f"{item.title}\n{item.body}".lower()
    if any(phrase.lower() not in haystack for phrase in check.phrases):
        return False
    if check.signal and check.signal not in seed.normalized_signals(item.signals):
        return False
    return True


def item_has_evidence(item: seed.SeedCandidate) -> bool:
    if not item.source_ids or not item.source_paths:
        return False
    body = item.body.lower()
    return "source evidence" in body and (
        "excerpt:" in body or any(src.lower() in body for src in item.source_ids)
    )


def render_markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = [
        "# Seed Report Eval",
        "",
        result["thesis"],
        "",
        "## Summary",
        "",
        f"- Checks: {summary['checks']}",
        f"- Passed: {summary['passed']}",
        f"- Failed: {summary['failed']}",
        f"- Pass rate: {summary['pass_rate']:.0%}",
        f"- Sources: " + ", ".join(
            f"{name}={count}" for name, count in summary["source_counts"].items()
        ),
        f"- Candidates: {summary['candidate_count']}",
        f"- Synthetic LLM-shaped candidates: {summary['synthetic_llm_candidate_count']}",
        f"- Synthetic LLM-shaped candidates filtered: {summary['synthetic_llm_filtered_count']}",
        f"- Catch demo: {'yes' if summary.get('catch_demo') else 'no'}",
        f"- Elapsed: {summary['elapsed_ms']} ms",
        "",
        "## Sections",
        "",
    ]
    for section in result["sections"]:
        lines.extend([
            f"- {section['key']}: {section['item_count']} item(s)",
        ])
    lines.extend(["", "## Checks", ""])
    for check in result["checks"]:
        status = "PASS" if check["passed"] else "FAIL"
        lines.extend([
            f"### {status} {check['id']}",
            "",
            f"- Kind: {check['kind']}",
            f"- Section: {check['section_label']}",
            f"- Expected: {check['expected']}",
            f"- Phrases: {', '.join(check['phrases'])}",
            f"- Matched: {', '.join(check['matched_titles']) or '(none)'}",
            f"- Evidence OK: {check['evidence_ok']}",
            "",
        ])
    lines.extend(["## Notes", ""])
    for note in result["notes"]:
        lines.append(f"- {note}")
    smoke = result.get("real_smoke")
    if isinstance(smoke, dict):
        lines.extend([
            "",
            "## Real Conversation Smoke",
            "",
            "- Preview only: yes",
            "- Writes enabled: no",
            f"- Source: {smoke.get('source')}",
            f"- Sources scanned: {smoke.get('sources_scanned')}",
            f"- Candidates: {smoke.get('candidate_count')}",
            f"- Catch demo: {'yes' if smoke.get('catch_demo') else 'no'}",
        ])
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run latch seed-report evals.")
    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="Output format.",
    )
    parser.add_argument("--output", type=Path, help="Write report to this path.")
    parser.add_argument(
        "--no-fail",
        action="store_true",
        help="Always exit 0 even when checks fail.",
    )
    parser.add_argument(
        "--real-smoke",
        action="store_true",
        help=(
            "Also run a preview-only deterministic smoke over real local "
            "conversations. Requires --real-source. Never writes seed candidates."
        ),
    )
    parser.add_argument(
        "--real-project",
        default=os.getcwd(),
        help="Project path for --real-smoke transcript filtering.",
    )
    parser.add_argument(
        "--real-source",
        choices=("claude", "codex", "both"),
        help="Explicit transcript source for --real-smoke.",
    )
    parser.add_argument(
        "--real-lookback-days",
        type=int,
        choices=seed.LOOKBACK_CHOICES,
        default=seed.DEFAULT_LOOKBACK_DAYS,
        help="Lookback window for --real-smoke.",
    )
    parser.add_argument(
        "--real-last-sessions",
        type=int,
        default=seed.DEFAULT_MAX_SESSIONS,
        help="Recent session cap for --real-smoke.",
    )
    parser.add_argument(
        "--real-claude-home",
        default=os.environ.get("CLAUDE_HOME") or str(Path.home() / ".claude"),
        help="Claude home directory for --real-smoke.",
    )
    parser.add_argument(
        "--real-codex-home",
        default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"),
        help="Codex home directory for --real-smoke.",
    )
    parser.add_argument(
        "--real-all-projects",
        action="store_true",
        help="Let --real-smoke scan all recent transcripts instead of project matches.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.real_smoke and not args.real_source:
        print("--real-smoke requires explicit --real-source claude|codex|both.", file=sys.stderr)
        return 2
    real_smoke = None
    if args.real_smoke:
        real_smoke = RealSmokeOptions(
            project=args.real_project,
            source=args.real_source,
            lookback_days=args.real_lookback_days,
            max_sessions=args.real_last_sessions,
            claude_home=args.real_claude_home,
            codex_home=args.real_codex_home,
            all_projects=args.real_all_projects,
        )
    result = run_seed_report_eval(real_smoke=real_smoke)
    output = (
        json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.format == "json"
        else render_markdown(result)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0 if result["ok"] or args.no_fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
