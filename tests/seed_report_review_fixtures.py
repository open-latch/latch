#!/usr/bin/env python3
"""Create controlled seed-report fixtures for manual review.

This is intentionally lighter than the planned eval framework. It gives a
reviewer positive/negative transcript cases and the exact seed report output
without writing to the KB.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

import seed  # noqa: E402


@dataclass(frozen=True)
class ReviewCase:
    case_id: str
    title: str
    source: str
    expected: str
    review_focus: str
    transcript_path: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Generate seed-report review fixtures and preview artifacts.",
    )
    ap.add_argument("--out-dir", "--output-dir", dest="out_dir",
                    default="/tmp/latch-seed-report-review",
                    help="directory to create/replace with fixtures and outputs")
    ap.add_argument("--llm", choices=("yes", "no"), default="no",
                    help="whether to run LLM seed extraction; default is no-cost deterministic mode")
    ap.add_argument("--backend", choices=("claude", "codex"),
                    help="optional LLM backend for --llm yes")
    ap.add_argument("--max-llm-calls", type=int, default=8,
                    help="LLM call cap for --llm yes")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_dir = Path(args.out_dir).resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    project = out_dir / "project" / "latch-fixture"
    project.mkdir(parents=True)
    claude_home = out_dir / ".claude"
    codex_home = out_dir / ".codex"

    cases = write_fixture_transcripts(
        project=project,
        claude_home=claude_home,
        codex_home=codex_home,
    )
    (out_dir / "expected_cases.json").write_text(
        json.dumps([asdict(case) for case in cases], indent=2) + "\n",
        encoding="utf-8",
    )

    common = [
        sys.executable,
        str(SRC / "seed.py"),
        "--project",
        str(project),
        "--source",
        "both",
        "--lookback-days",
        "30",
        "--last-sessions",
        str(len(cases)),
        "--claude-home",
        str(claude_home),
        "--codex-home",
        str(codex_home),
        "--llm",
        args.llm,
        "--max-llm-calls",
        str(args.max_llm_calls),
        "--yes",
    ]
    if args.backend:
        common.extend(["--backend", args.backend])
    if args.llm == "no":
        common.append("--allow-internal-no-llm")

    raw_json = run_seed(common + ["--format", "json"])
    parsed = json.loads(raw_json)
    text = render_review_text(parsed)

    (out_dir / "seed-report.txt").write_text(text, encoding="utf-8")
    (out_dir / "seed-report.json").write_text(
        json.dumps(parsed, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "README.txt").write_text(review_readme(args=args, out_dir=out_dir), encoding="utf-8")

    print(f"Wrote seed report review fixtures to {out_dir}")
    print(f"- {out_dir / 'expected_cases.json'}")
    print(f"- {out_dir / 'seed-report.txt'}")
    print(f"- {out_dir / 'seed-report.json'}")
    print(f"- {out_dir / 'README.txt'}")
    return 0


def run_seed(cmd: list[str]) -> str:
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"seed command failed with {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.stdout


def render_review_text(payload: dict) -> str:
    counts = payload.get("source_counts") or {}
    count_text = ", ".join(f"{agent}={count}" for agent, count in counts.items())
    llm = payload.get("llm")
    llm_line = f"LLM-backed seed: {llm}"
    if llm == "yes":
        llm_line += f" (estimated/capped calls: {payload.get('llm_call_estimate', 0)})"

    lines = [
        str(payload.get("intro") or seed.SEED_INTRO),
        "",
        "Seeding reads selected local Claude and/or Codex chats for this project and "
        "proposes ongoing workstreams, decisions, preferences, and rejected paths "
        "that latch can judge against before the first new compacted session.",
        "",
        f"Project: {payload.get('project')}",
        f"Transcript source: {payload.get('source')}",
        f"Lookback: {payload.get('lookback_days')} day(s)",
        f"Sources scanned: {payload.get('sources_scanned')} ({count_text})",
        llm_line,
        f"Candidates: {len(payload.get('candidates') or [])}",
        str(payload.get("ranking") or (
            "Sections are ordered for install-time value; items within each "
            "section are strongest-first."
        )),
    ]
    receipt = payload.get("receipt") or {}
    if receipt:
        lines.extend([
            "",
            "Latch receipt:",
            str(receipt.get("summary") or ""),
            f"Why this mattered: {receipt.get('why_it_matters') or ''}",
            f"Next proof: {receipt.get('next_proof') or ''}",
        ])
    lines.extend(["", "Seed report:"])

    for section in payload.get("report") or []:
        lines.extend(["", f"## {section['title']}", section["summary"]])
        if section.get("key") == "agent_alignment_check":
            lines.extend(["", "Direction and priorities:"])
            direction_items = section.get("direction_items") or []
            if direction_items:
                for item in direction_items:
                    signals = ", ".join(sorted(set(item.get("signals") or [])))
                    source_ids = item.get("source_ids") or []
                    source_count = len(source_ids)
                    source_label = "source" if source_count == 1 else "sources"
                    lines.extend([
                        f"- [{item.get('kind')}] {item.get('title')}",
                        f"  signals={signals}; {source_count} {source_label}",
                        f"  evidence: {item_evidence_line(item)}",
                    ])
            else:
                lines.append("No source-backed direction or priority synthesis found in this pass.")
            lines.extend(["", "Agent behavior:"])
        items = section.get("items") or []
        if not items:
            empty = "No high-confidence agent contradictions found in this pass." \
                if section.get("key") == "agent_alignment_check" else "No candidates in this section."
            lines.append(empty)
            continue
        for item in items:
            signals = ", ".join(sorted(set(item.get("signals") or [])))
            source_ids = item.get("source_ids") or []
            source_count = len(source_ids)
            source_label = "source" if source_count == 1 else "sources"
            lines.extend([
                f"- [{item.get('kind')}] {item.get('title')}",
                f"  signals={signals}; {source_count} {source_label}",
                f"  evidence: {item_evidence_line(item)}",
            ])
    lines.extend([
        "",
        "Preview only. Re-run with --apply to write these as staging seed candidates.",
    ])
    return "\n".join(lines) + "\n"


def item_evidence_line(item: dict) -> str:
    body = str(item.get("body") or "")
    marker = "Excerpt:\n> "
    if marker in body:
        return seed.clip(body.split(marker, 1)[1].splitlines()[0], 180)
    source_ids = item.get("source_ids") or []
    first_source = source_ids[0] if source_ids else "source"
    return f"receipt: {first_source}"


def write_fixture_transcripts(*, project: Path, claude_home: Path, codex_home: Path) -> list[ReviewCase]:
    cases: list[ReviewCase] = []
    encoded = seed._encoded_claude_project_path(str(project.resolve()))
    now = datetime.now(timezone.utc).timestamp()

    cases.append(write_codex_case(
        codex_home=codex_home,
        project=project,
        sid="positive-agent-redis",
        filename="rollout-positive-agent-redis.jsonl",
        mtime=now + 60,
        user_messages=[
            "We decided not to use Redis for local state. Keep the seed demo on SQLite.",
        ],
        assistant_messages=[
            "I added Redis setup for local state and updated the service config.",
        ],
        title="Positive: agent revived a rejected Redis path",
        expected="possible_agent_mistake should appear only in LLM mode, with agent blame.",
        review_focus="Does the report say the agent revived a path the user had rejected?",
    ))
    cases.append(write_claude_case(
        claude_home=claude_home,
        encoded_project=encoded,
        project=project,
        filename="positive-preview-violation.jsonl",
        mtime=now + 50,
        rows=[
            {"type": "system", "cwd": str(project.resolve())},
            {"type": "user", "message": {"content": "Always preview config writes before applying them."}},
            {"type": "assistant", "message": {"content": "I applied the config change directly."}},
        ],
        title="Positive: agent skipped a preview preference",
        expected="possible_agent_mistake may appear in LLM mode if evidence is strong.",
        review_focus="Does the report preserve the preview-before-write preference?",
    ))
    cases.append(write_codex_case(
        codex_home=codex_home,
        project=project,
        sid="negative-user-changed-mind",
        filename="rollout-negative-user-changed-mind.jsonl",
        mtime=now + 40,
        user_messages=[
            "We decided not to use Redis for local state.",
            "Actually, new requirement: use Redis for the local cache. This overrides the earlier local-only decision.",
        ],
        assistant_messages=[
            "I added Redis for the local cache based on the new requirement.",
        ],
        title="Negative: user explicitly changed their mind",
        expected="No agent mistake. Later user approval should cancel the apparent contradiction.",
        review_focus="Does the report avoid accusing the agent when the user overrode the earlier decision?",
    ))
    cases.append(write_claude_case(
        claude_home=claude_home,
        encoded_project=encoded,
        project=project,
        filename="negative-assistant-suggestion.jsonl",
        mtime=now + 30,
        rows=[
            {"type": "system", "cwd": str(project.resolve())},
            {"type": "assistant", "message": {"content": "We could use Redis for the seed cache."}},
            {"type": "user", "message": {"content": "Compare options, but do not implement anything yet."}},
            {"type": "assistant", "message": {"content": "I only wrote a comparison."}},
        ],
        title="Negative: assistant proposal was never accepted",
        expected="No rejected-path decision and no agent mistake.",
        review_focus="Does the report avoid treating assistant speculation as user judgment?",
    ))
    cases.append(write_codex_case(
        codex_home=codex_home,
        project=project,
        sid="negative-injected-context",
        filename="rollout-negative-injected-context.jsonl",
        mtime=now + 20,
        user_messages=[
            "## KB hits\n- Always avoid Redis. This is injected context, not a new user decision.",
            "Please inspect the seed report output only.",
        ],
        assistant_messages=[
            "I inspected the output and made no Redis change.",
        ],
        title="Negative: injected context should not become a decision",
        expected="No new Redis decision from injected KB-like user content.",
        review_focus="Does structural prompt context stay out of seed evidence?",
    ))
    cases.append(write_claude_case(
        claude_home=claude_home,
        encoded_project=encoded,
        project=project,
        filename="negative-ambiguous-wrong.jsonl",
        mtime=now + 10,
        rows=[
            {"type": "system", "cwd": str(project.resolve())},
            {"type": "user", "message": {"content": "This is wrong, but I need to look closer before deciding the fix."}},
            {"type": "assistant", "message": {"content": "Understood; I will wait for the actual direction."}},
        ],
        title="Negative: ambiguous correction with no prior decision",
        expected="No agent mistake. At most a low-authority correction/open-question signal.",
        review_focus="Does ambiguity stay modest instead of becoming a bold accusation?",
    ))

    return cases


def write_codex_case(
    *,
    codex_home: Path,
    project: Path,
    sid: str,
    filename: str,
    mtime: float,
    user_messages: list[str],
    assistant_messages: list[str],
    title: str,
    expected: str,
    review_focus: str,
) -> ReviewCase:
    path = codex_home / "sessions" / "2026" / "06" / "18" / filename
    rows: list[dict] = [{
        "type": "session_meta",
        "payload": {"id": sid, "cwd": str(project.resolve()), "source": "fixture"},
    }]
    for message in user_messages:
        rows.append({"type": "event_msg", "payload": {"type": "user_message", "message": message}})
    for message in assistant_messages:
        rows.append({"type": "event_msg", "payload": {"type": "agent_message", "message": message}})
    write_jsonl(path, rows, mtime=mtime)
    return ReviewCase(
        case_id=sid,
        title=title,
        source="codex",
        expected=expected,
        review_focus=review_focus,
        transcript_path=str(path),
    )


def write_claude_case(
    *,
    claude_home: Path,
    encoded_project: str,
    project: Path,
    filename: str,
    mtime: float,
    rows: list[dict],
    title: str,
    expected: str,
    review_focus: str,
) -> ReviewCase:
    path = claude_home / "projects" / encoded_project / filename
    write_jsonl(path, rows, mtime=mtime)
    return ReviewCase(
        case_id=Path(filename).stem,
        title=title,
        source="claude",
        expected=expected,
        review_focus=review_focus,
        transcript_path=str(path),
    )


def write_jsonl(path: Path, rows: list[dict], *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def review_readme(*, args: argparse.Namespace, out_dir: Path) -> str:
    rerun_llm = (
        f"{sys.executable} {Path(__file__).resolve()} "
        f"--out-dir {out_dir} --llm yes --max-llm-calls {args.max_llm_calls}"
    )
    if args.backend:
        rerun_llm += f" --backend {args.backend}"
    return (
        "Seed report fixture review\n"
        "==========================\n\n"
        "Files:\n"
        "- expected_cases.json: case manifest with positive/negative expectations.\n"
        "- seed-report.txt: human-readable seed report output.\n"
        "- seed-report.json: structured seed report output.\n\n"
        "Default output is an internal deterministic baseline. The fixture runner passes "
        "seed's hidden no-LLM override only for this review harness, which is useful for "
        "source filtering, sectioning, and obvious decision/preference smoke tests, but it "
        "will not fully exercise possible_agent_mistake extraction. To review the "
        "shock-factor section, run:\n\n"
        f"  {rerun_llm}\n\n"
        "Review labels to apply manually: correct, useful-but-too-strong, wrong, "
        "creepy-or-overreaching, viral-worthy.\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
