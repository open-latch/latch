"""Deterministic evals for the install-time seed report.

These evals are the local instrument panel for the seed/report first-wow path:
small Claude/Codex transcript bundles go through the seed candidate/report code,
then deterministic checks grade whether the report captured the durable project
signals latch cares about. The runner uses no model calls; the synthetic
agent-mistake candidate exercises the LLM-candidate reporting path without
turning no-LLM seeding into a public product mode.
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
        id="preview_preference",
        kind="pattern_preference",
        section="patterns_and_preferences",
        phrases=("preview seed writes",),
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
        id="low_confidence_agent_mistake_filtered",
        kind="agent_mistake_negative_control",
        section="agent_alignment_check",
        phrases=("low confidence redis mistake",),
        must_have_evidence=False,
        should_match=False,
    ),
)


def run_seed_report_eval() -> dict[str, Any]:
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
        deterministic = seed.deterministic_candidates(sources, max_candidates=20)
        llm_synthetic = synthetic_agent_mistake_candidates(sources)
        candidates = seed.merge_candidate_sets(
            llm_synthetic,
            deterministic,
            max_candidates=20,
        )
        report = seed.build_seed_report(candidates)
        check_results = grade_report(report, DEFAULT_CHECKS)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        passed = sum(1 for row in check_results if row["passed"])
        return {
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
            },
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
            "notes": [
                "No model calls are made by this runner.",
                (
                    "agent_alignment_check is exercised with a synthetic "
                    "LLM-shaped contradiction candidate from fixture evidence; "
                    "the public seed CLI remains LLM-backed."
                ),
            ],
        }
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

    codex_path = codex_home / "sessions" / "2026" / "06" / "22" / "rollout-seed-report-eval.jsonl"
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
    ], mtime=now + 20)
    manifest.append({"source": "codex", "path": str(codex_path)})

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
    ], mtime=now + 10)
    manifest.append({"source": "claude", "path": str(claude_path)})
    return manifest


def write_jsonl(path: Path, rows: list[dict[str, Any]], *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def synthetic_agent_mistake_candidates(sources: list[seed.SeedSource]) -> list[seed.SeedCandidate]:
    source = next((src for src in sources if src.agent == "codex"), None)
    if source is None:
        return []
    good = seed.candidate_from_llm_item({
        "kind": "fact",
        "title": "Agent revived Redis after rejection",
        "body": (
            "The agent revived Redis after the user rejected Redis for local "
            "state. This violated the prior rejection because Redis adds "
            "another service to operate."
        ),
        "confidence": 0.91,
        "signals": ["possible_agent_mistake"],
    }, source)
    low_confidence = seed.candidate_from_llm_item({
        "kind": "fact",
        "title": "Low confidence Redis mistake",
        "body": (
            "The agent may have touched Redis, but the transcript evidence is "
            "ambiguous and should not be treated as a high-confidence mistake."
        ),
        "confidence": 0.7,
        "signals": ["possible_agent_mistake"],
    }, source)
    out = [cand for cand in (good, low_confidence) if cand is not None]
    return out


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_seed_report_eval()
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
