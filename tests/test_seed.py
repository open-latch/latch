from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import seed  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _has_key(obj, key: str) -> bool:
    if isinstance(obj, dict):
        return key in obj or any(_has_key(value, key) for value in obj.values())
    if isinstance(obj, list):
        return any(_has_key(item, key) for item in obj)
    return False


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_deterministic_seed_candidates_from_claude_transcript():
    root = Path(tempfile.mkdtemp(prefix="latch-seed-"))
    project = root / "repo" / "latch"
    project.mkdir(parents=True)
    encoded = seed._encoded_claude_project_path(str(project.resolve()))
    transcript = root / ".claude" / "projects" / encoded / "session.jsonl"
    _write_jsonl(transcript, [
        {"type": "system", "cwd": str(project.resolve())},
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "We decided not to use Redis for latch's local cache."}
        ]}},
        {"type": "user", "message": {"content": "Always keep latch local-first and preview writes."}},
    ])

    sources = seed.discover_sources(
        source="claude",
        project_path=str(project),
        lookback_days=5,
        max_sessions=10,
        claude_home=str(root / ".claude"),
        codex_home=str(root / ".codex"),
        now=datetime.now(timezone.utc),
    )
    _assert(len(sources) == 1, f"expected one source, got {sources}")
    candidates = seed.deterministic_candidates(sources, max_candidates=10)
    titles = "\n".join(c.title for c in candidates)
    _assert("Seeded rejected path" in titles, f"missing rejected-path candidate: {titles}")
    _assert("Seeded preference" in titles, f"missing preference candidate: {titles}")
    bodies = "\n".join(c.body for c in candidates)
    _assert("low-authority staging evidence" in bodies, "seed candidates should be staged")
    _assert("harvest" not in bodies.lower() and "glean" not in bodies.lower(),
            "seed candidates should avoid rejected naming")
    print("PASS deterministic_seed_candidates_from_claude_transcript")


def test_machine_generated_claude_records_are_ignored():
    root = Path(tempfile.mkdtemp(prefix="latch-seed-sdk-"))
    project = root / "repo" / "latch"
    project.mkdir(parents=True)
    encoded = seed._encoded_claude_project_path(str(project.resolve()))
    transcript = root / ".claude" / "projects" / encoded / "sdk.jsonl"
    _write_jsonl(transcript, [
        {
            "type": "user",
            "promptSource": "sdk",
            "entrypoint": "sdk-cli",
            "cwd": str(project.resolve()),
            "message": {"role": "user", "content": "Always ignore this gate prompt."},
        },
        {
            "type": "queue-operation",
            "cwd": str(project.resolve()),
            "content": "We decided not to use this machine-generated prompt.",
        },
    ])

    sources = seed.discover_sources(
        source="claude",
        project_path=str(project),
        lookback_days=5,
        max_sessions=10,
        claude_home=str(root / ".claude"),
        codex_home=str(root / ".codex"),
        now=datetime.now(timezone.utc),
    )
    _assert(sources == [], f"sdk-cli transcript should not seed sources: {sources}")
    print("PASS machine_generated_claude_records_are_ignored")


def test_both_source_selection_uses_global_recency_split():
    root = Path(tempfile.mkdtemp(prefix="latch-seed-both-"))
    project = root / "repo" / "latch"
    project.mkdir(parents=True)
    encoded = seed._encoded_claude_project_path(str(project.resolve()))
    base_ts = datetime.now(timezone.utc).timestamp()

    for i in range(6):
        path = root / ".codex" / "sessions" / "2026" / "06" / "18" / f"rollout-codex-{i}.jsonl"
        _write_jsonl(path, [
            {"type": "session_meta", "payload": {"id": f"codex-{i}", "cwd": str(project.resolve())}},
            {"type": "event_msg", "payload": {
                "type": "user_message",
                "message": f"We decided not to use Codex-only hidden import {i}.",
            }},
        ])
        os.utime(path, (base_ts + 100 - i, base_ts + 100 - i))

    for i in range(6):
        path = root / ".claude" / "projects" / encoded / f"claude-{i}.jsonl"
        _write_jsonl(path, [
            {"type": "system", "cwd": str(project.resolve())},
            {"type": "user", "message": {
                "content": f"We decided not to use Claude-only hidden import {i}."
            }},
        ])
        os.utime(path, (base_ts + 50 - i, base_ts + 50 - i))

    sources = seed.discover_sources(
        source="both",
        project_path=str(project),
        lookback_days=5,
        max_sessions=10,
        claude_home=str(root / ".claude"),
        codex_home=str(root / ".codex"),
        now=datetime.fromtimestamp(base_ts + 200, tz=timezone.utc),
    )
    counts = seed.source_counts(sources)
    _assert(len(sources) == 10, f"expected newest ten sources, got {len(sources)}")
    _assert(counts["codex"] == 6 and counts["claude"] == 4,
            f"expected 6 Codex + 4 Claude by recency, got {counts}")
    _assert([src.agent for src in sources[:6]] == ["codex"] * 6,
            f"newest six should be Codex sessions: {sources}")

    claude_only = seed.discover_sources(
        source="claude",
        project_path=str(project),
        lookback_days=5,
        max_sessions=10,
        claude_home=str(root / ".claude"),
        codex_home=str(root / ".codex"),
        now=datetime.fromtimestamp(base_ts + 200, tz=timezone.utc),
    )
    _assert(len(claude_only) == 6 and {src.agent for src in claude_only} == {"claude"},
            f"claude source should not include Codex sessions: {claude_only}")
    print("PASS both_source_selection_uses_global_recency_split")


def test_auto_source_noninteractive_requires_explicit_choice_when_ambiguous():
    root = Path(tempfile.mkdtemp(prefix="latch-seed-auto-source-"))
    (root / ".claude" / "projects").mkdir(parents=True)
    (root / ".codex" / "sessions").mkdir(parents=True)
    args = seed.parse_args([
        "--lookback-days", "5",
        "--claude-home", str(root / ".claude"),
        "--codex-home", str(root / ".codex"),
    ])
    try:
        seed.prompt_choices(args)
    except SystemExit as exc:
        _assert("--source claude" in str(exc), f"unexpected auto-source error: {exc}")
    else:
        raise AssertionError("ambiguous non-interactive --source auto should require a choice")
    print("PASS auto_source_noninteractive_requires_explicit_choice_when_ambiguous")


def test_auto_source_noninteractive_uses_only_available_source():
    root = Path(tempfile.mkdtemp(prefix="latch-seed-auto-single-"))
    (root / ".codex" / "sessions").mkdir(parents=True)
    args = seed.parse_args([
        "--lookback-days", "5",
        "--claude-home", str(root / ".claude"),
        "--codex-home", str(root / ".codex"),
    ])
    seed.prompt_choices(args)
    _assert(args.source == "codex",
            f"single available source should be selected automatically: {args.source}")
    print("PASS auto_source_noninteractive_uses_only_available_source")


def test_llm_call_estimate_is_capped():
    _assert(seed.estimate_llm_calls(0, calls_per_session=1, max_llm_calls=10) == 0,
            "zero sessions should estimate zero calls")
    _assert(seed.estimate_llm_calls(3, calls_per_session=2, max_llm_calls=10) == 6,
            "estimate should multiply sessions by calls per session")
    _assert(seed.estimate_llm_calls(30, calls_per_session=2, max_llm_calls=7) == 7,
            "estimate should respect max LLM calls")
    print("PASS llm_call_estimate_is_capped")


def test_seed_help_hides_internal_no_llm_switch():
    parser = seed.parse_args
    args = parser(["--lookback-days", "5"])
    _assert(args.llm == "yes", f"seed should default to LLM-backed mode: {args.llm}")
    defaulted = parser(["--lookback-days", "5", "--source", "codex"])
    seed.prompt_choices(defaulted)
    _assert(defaulted.max_sessions == 20,
            f"seed should default to last 20 sessions: {defaulted.max_sessions}")
    last_sessions = parser(["--lookback-days", "5", "--last-sessions", "10"])
    _assert(last_sessions.max_sessions == 10,
            f"--last-sessions should set max_sessions: {last_sessions.max_sessions}")
    max_sessions_alias = parser(["--lookback-days", "5", "--max-sessions", "12"])
    _assert(max_sessions_alias.max_sessions == 12,
            f"--max-sessions alias should still set max_sessions: {max_sessions_alias.max_sessions}")
    internal = parser(["--lookback-days", "5", "--llm", "no"])
    _assert(internal.llm == "no", "internal no-LLM switch should remain parseable")
    print("PASS seed_help_hides_internal_no_llm_switch")


def test_no_llm_requires_internal_override():
    args = seed.parse_args(["--lookback-days", "5", "--llm", "no"])
    _assert(seed.no_llm_disabled_reason(args) is not None,
            "no-LLM mode should be disabled without an explicit internal override")

    internal = seed.parse_args([
        "--lookback-days", "5",
        "--llm", "no",
        "--allow-internal-no-llm",
    ])
    _assert(seed.no_llm_disabled_reason(internal) is None,
            "internal override should allow deterministic baseline runs")
    print("PASS no_llm_requires_internal_override")


def test_llm_candidates_suppress_overlapping_deterministic_candidates():
    args = seed.parse_args([
        "--lookback-days", "14",
        "--source", "codex",
        "--project", os.getcwd(),
    ])
    src_id = "claude:session"
    src_path = "/tmp/session.jsonl"
    llm = seed.SeedCandidate(
        kind="decision",
        title="Use local SQLite for runtime state",
        body="The user stated: We decided not to use Redis for runtime state. Keep SQLite.",
        confidence=0.95,
        signals=["llm_seed", "decision", "rejected_path"],
        source_ids=[src_id],
        source_paths=[src_path],
        llm_used=True,
    )
    deterministic = seed.SeedCandidate(
        kind="decision",
        title="Seeded rejected path: We decided not to use Redis for runtime state",
        body="Excerpt: We decided not to use Redis for runtime state. Keep SQLite.",
        confidence=0.72,
        signals=["deterministic_seed", "rejected_path"],
        source_ids=[src_id],
        source_paths=[src_path],
    )
    merged = seed.merge_candidate_sets([llm], [deterministic], max_candidates=5)
    _assert(len(merged) == 1 and merged[0].llm_used,
            f"LLM candidate should suppress overlapping deterministic duplicate: {merged}")
    chosen, blocked = seed.choose_seed_candidates(args, [llm], [deterministic])
    _assert(not blocked and len(chosen) == 1 and chosen[0].llm_used,
            f"LLM-backed mode should keep LLM candidates: {chosen}, blocked={blocked}")
    print("PASS llm_candidates_suppress_overlapping_deterministic_candidates")


def test_agent_mistake_does_not_suppress_clean_rejected_path():
    src_id = "codex:session"
    src_path = "/tmp/rollout.jsonl"
    agent_mistake = seed.SeedCandidate(
        kind="fact",
        title="Agent revived Redis after rejection",
        body=(
            "The agent revived Redis after the user rejected Redis for local "
            "state. This violated the prior rejection because Redis adds "
            "another service to operate."
        ),
        confidence=0.91,
        signals=["llm_seed", "possible_agent_mistake"],
        source_ids=[src_id],
        source_paths=[src_path],
        llm_used=True,
    )
    rejected_path = seed.SeedCandidate(
        kind="decision",
        title="Seeded rejected path: We decided not to use Redis for local state",
        body=(
            "Excerpt:\n"
            "> We decided not to use Redis for local state because it adds "
            "another service to operate."
        ),
        confidence=0.72,
        signals=["deterministic_seed", "rejected_path"],
        source_ids=[src_id],
        source_paths=[src_path],
    )

    merged = seed.merge_candidate_sets([agent_mistake], [rejected_path], max_candidates=5)
    report = seed.build_seed_report(merged)
    by_key = {section.key: section for section in report}
    _assert(
        len(by_key["agent_alignment_check"].items) == 1,
        f"agent mistake should stay in alignment-check section: {report}",
    )
    _assert(
        len(by_key["decisions_and_rejected_paths"].items) == 1,
        f"clean rejected path should not be suppressed by agent mistake: {report}",
    )
    print("PASS agent_mistake_does_not_suppress_clean_rejected_path")


def test_llm_mode_blocks_deterministic_only_write_candidates():
    args = seed.parse_args([
        "--lookback-days", "14",
        "--source", "codex",
        "--project", os.getcwd(),
    ])
    deterministic = seed.SeedCandidate(
        kind="decision",
        title="Seeded rejected path: Avoid Redis",
        body="Excerpt:\n> We decided not to use Redis for local state.",
        confidence=0.72,
        signals=["deterministic_seed", "rejected_path"],
        source_ids=["codex:test"],
        source_paths=["/tmp/test.jsonl"],
    )
    chosen, blocked = seed.choose_seed_candidates(args, [], [deterministic])
    _assert(chosen == [] and blocked,
            f"public LLM mode must not write deterministic-only candidates: {chosen}")
    args.llm_refinement_empty = blocked
    out = seed.render_text(args=args, sources=[], candidates=chosen, llm_estimate=1)
    _assert("LLM-backed seed produced no writeable candidates" in out,
            f"report should explain the safe no-write boundary: {out}")
    payload = json.loads(seed.render_json(args=args, sources=[], candidates=chosen, llm_estimate=1))
    _assert(payload["llm_refinement_empty"] is True,
            f"json should expose the LLM-empty boundary: {payload}")
    print("PASS llm_mode_blocks_deterministic_only_write_candidates")


def test_llm_candidate_quality_filter_drops_sample_noise():
    src = seed.SeedSource(
        id="claude:sample",
        agent="claude",
        path="/tmp/sample.jsonl",
        mtime="2026-06-17T00:00:00+00:00",
        text="",
    )
    noisy = [
        {
            "kind": "fact",
            "title": "Main worktree path for latch",
            "body": "main was checked out and updated in a separate worktree.",
            "signals": ["workflow_context"],
        },
        {
            "kind": "decision",
            "title": "Enable bypass permissions globally",
            "body": "Set permissions.defaultMode to bypassPermissions in global user settings.",
            "signals": ["permission_mode"],
        },
        {
            "kind": "preference",
            "title": "Cold-start seed candidates include Redis rejection and preview preference",
            "body": "The one-call Claude preview returned three expected candidates.",
            "signals": ["preference"],
        },
        {
            "kind": "open_question",
            "title": "Mark KB idea 335 verified",
            "body": "The assistant asked whether to mark the KB idea verified; the transcript does not include the user's answer.",
            "signals": ["open_question"],
        },
    ]
    kept = [seed.candidate_from_llm_item(item, src) for item in noisy]
    _assert(kept == [None, None, None, None],
            f"sample LLM noise should be filtered: {kept}")
    print("PASS llm_candidate_quality_filter_drops_sample_noise")


def test_llm_candidate_quality_filter_keeps_durable_signals():
    src = seed.SeedSource(
        id="claude:sample",
        agent="claude",
        path="/tmp/sample.jsonl",
        mtime="2026-06-17T00:00:00+00:00",
        text="",
    )
    durable = [
        {
            "kind": "fact",
            "title": "Claude cold-start seed sandbox passed end-to-end",
            "body": "A Claude-backed sandbox run for the cold-start seed path passed deterministic tests, no-call threshold cancellation, one-call preview, and full temporary-KB apply.",
            "confidence": 0.95,
            "signals": ["verification"],
        },
        {
            "kind": "fact",
            "title": "No-call threshold guard cancels before spending LLM calls",
            "body": "The no-call threshold phase warned about one possible call and cancelled before any LLM call.",
            "confidence": 0.95,
            "signals": ["budget_guardrail"],
        },
        {
            "kind": "open_question",
            "title": "Investigate `/kb-compact` cold-start failure",
            "body": "A colleague hit a compact failure where Claude subprocess output could not be parsed as JSON; investigate command invocation and error surfacing.",
            "confidence": 0.8,
            "signals": ["debugging_followup"],
        },
        {
            "kind": "preference",
            "title": "Preserve existing settings when editing config",
            "body": "Future config edits should merge into existing settings rather than clobbering unrelated hooks or allow rules.",
            "confidence": 0.82,
            "signals": ["config_preservation"],
        },
        {
            "kind": "workstream",
            "title": "Install seed proof loop",
            "body": (
                "The transcript repeatedly returns to the install seed proof "
                "loop and rejected-path demo follow-up."
            ),
            "confidence": 0.84,
            "signals": ["ongoing_workstream"],
        },
    ]
    kept = [seed.candidate_from_llm_item(item, src) for item in durable]
    _assert(all(c is not None for c in kept),
            f"durable seed signals should survive quality filter: {kept}")
    titles = "\n".join(c.title for c in kept if c)
    _assert("sandbox passed" in titles and "Preserve existing settings" in titles
            and "Install seed proof loop" in titles,
            f"expected durable titles to survive: {titles}")
    print("PASS llm_candidate_quality_filter_keeps_durable_signals")


def test_agent_mistake_candidates_require_high_confidence_and_agent_blame():
    src = seed.SeedSource(
        id="codex:sample",
        agent="codex",
        path="/tmp/rollout.jsonl",
        mtime="2026-06-18T00:00:00+00:00",
        text="",
    )
    low_confidence = seed.candidate_from_llm_item({
        "kind": "fact",
        "title": "Agent revived Redis after rejection",
        "body": "The agent implemented Redis after the user had rejected Redis.",
        "confidence": 0.7,
        "signals": ["possible_agent_mistake"],
    }, src)
    _assert(low_confidence is None,
            f"low-confidence agent mistake should be filtered: {low_confidence}")

    user_blaming = seed.candidate_from_llm_item({
        "kind": "fact",
        "title": "User violated Redis rejection",
        "body": "The user violated the earlier Redis rejection.",
        "confidence": 0.92,
        "signals": ["possible_agent_mistake"],
    }, src)
    _assert(user_blaming is None,
            f"user-blaming agent mistake should be filtered: {user_blaming}")

    hindsight = seed.candidate_from_llm_item({
        "kind": "fact",
        "title": "Agent missed later Redis clarification",
        "body": "With hindsight, later user-provided information clarified Redis should "
                "not be used, but the agent did not have that information at the time.",
        "confidence": 0.93,
        "signals": ["possible_agent_mistake"],
    }, src)
    _assert(hindsight is None,
            f"hindsight-based agent mistake should be filtered: {hindsight}")

    high_confidence = seed.candidate_from_llm_item({
        "kind": "fact",
        "title": "Agent revived Redis after rejection",
        "body": "The user rejected Redis for local state, but the agent later added Redis setup. "
                "Latch would flag this before code is written.",
        "confidence": 0.91,
        "signals": ["possible_agent_mistake", "rejected_path"],
    }, src)
    _assert(high_confidence is not None,
            "high-confidence agent-blamed mistake should survive")
    print("PASS agent_mistake_candidates_require_high_confidence_and_agent_blame")


def test_seed_report_groups_candidates_into_demo_sections():
    args = seed.parse_args([
        "--lookback-days", "14",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--source", "both",
        "--project", os.getcwd(),
    ])
    candidates = [
        seed.SeedCandidate(
            kind="workstream",
            title="Launch workstream handoff",
            body=(
                "Excerpt:\n"
                "> Open question: we need to decide the launch workstream "
                "handoff before the next PR."
            ),
            confidence=0.82,
            signals=["llm_seed", "ongoing_workstream"],
            source_ids=["claude:w"],
            source_paths=["/tmp/w.jsonl"],
            llm_used=True,
        ),
        seed.SeedCandidate(
            kind="decision",
            title="Use SQLite for local state",
            body="Excerpt:\n> We decided not to use Redis for local state.",
            confidence=0.9,
            signals=["llm_seed", "decision", "rejected_path"],
            source_ids=["claude:a"],
            source_paths=["/tmp/a.jsonl"],
            llm_used=True,
        ),
        seed.SeedCandidate(
            kind="preference",
            title="Preview writes before applying",
            body="Excerpt:\n> Always preview seed writes.",
            confidence=0.86,
            signals=["llm_seed", "preference"],
            source_ids=["codex:b"],
            source_paths=["/tmp/b.jsonl"],
            llm_used=True,
        ),
        seed.SeedCandidate(
            kind="fact",
            title="Agent revived Redis after rejection",
            body="The user rejected Redis; the agent later added Redis setup.",
            confidence=0.91,
            signals=["llm_seed", "possible_agent_mistake", "rejected_path"],
            source_ids=["codex:c"],
            source_paths=["/tmp/c.jsonl"],
            llm_used=True,
        ),
        seed.SeedCandidate(
            kind="open_question",
            title="Decide whether to expose seed report in installer",
            body="Excerpt:\n> We need to decide whether seed report should be installer-visible.",
            confidence=0.7,
            signals=["llm_seed", "open_question"],
            source_ids=["claude:d"],
            source_paths=["/tmp/d.jsonl"],
            llm_used=True,
        ),
    ]

    report = seed.build_seed_report(candidates)
    by_key = {section.key: section for section in report}
    _assert(report[0].key == "decisions_and_rejected_paths",
            f"decisions/rejected paths should lead the install-time report: {report}")
    _assert(by_key["continuity_notes"].items[0].title.startswith("Launch workstream"),
            f"workstream should be preserved internally but displayed as continuity: {report}")
    _assert(by_key["decisions_and_rejected_paths"].items[0].title.startswith("Use SQLite"),
            f"decision should land in decisions section: {report}")
    _assert(by_key["patterns_and_preferences"].items[0].title.startswith("Preview writes"),
            f"preference should land in patterns section: {report}")
    _assert(by_key["agent_alignment_check"].items[0].title.startswith("Agent revived"),
            f"agent mistake should land in alignment-check section: {report}")
    _assert(by_key["where_left_off"].items[0].title.startswith("Decide whether"),
            f"open question should land in left-off section: {report}")
    _assert(seed.alignment_direction_items(candidates)[0].title.startswith("Preview writes"),
            f"alignment direction should synthesize from source-backed candidates: {report}")

    out = seed.render_text(args=args, sources=[], candidates=candidates, llm_estimate=0)
    _assert("Latch receipt:" in out and "proof receipt, not a dashboard" in out,
            f"rendered report should include a visible latch receipt: {out}")
    _assert("Why this mattered:" in out and "future gates can cite" in out,
            f"receipt should explain why the report matters: {out}")
    _assert("Next proof:" in out and "latch_gate challenge the strongest rejected path" in out,
            f"receipt should point to the rejected-path proof loop: {out}")
    _assert("Seed report:" in out and "## Decisions and rejected paths" in out,
            f"rendered report should lead with decisions/rejected paths: {out}")
    _assert("## Continuity notes" in out and "Ongoing workstreams" not in out,
            f"rendered report should not foreground workstreams: {out}")
    _assert("## Where you left off" in out,
            f"rendered report should have visible sections: {out}")
    _assert("## Agent alignment check" in out and "Direction and priorities:" in out,
            f"rendered report should include the alignment synthesis: {out}")
    _assert("Agent behavior:" in out and "Agent revived Redis" in out,
            f"rendered report should surface high-confidence agent contradictions: {out}")
    _assert("receipt: codex:c" in out,
            f"agent mistake should include a source receipt fallback: {out}")
    _assert("confidence=" not in out and "Confidence:" not in out,
            f"rendered report should not expose numeric confidence scores: {out}")
    _assert("strongest-first" in out,
            f"rendered report should explain score-free ranking: {out}")
    _assert("Try the catch demo:" in out and "/latch-gate" in out and "run_latch_gate.sh" in out,
            f"rendered report should include a rejected-path catch demo: {out}")
    _assert("After you apply this seed" in out,
            f"catch demo should not imply preview-only candidates are already in the KB: {out}")
    _assert("Revive this rejected path" in out and "We decided not to use Redis" in out,
            f"catch demo should derive a concrete rejected-path request: {out}")

    payload = json.loads(seed.render_json(args=args, sources=[], candidates=candidates, llm_estimate=0))
    _assert("report" in payload and payload["report"][0]["key"] == "decisions_and_rejected_paths",
            f"json report should be structured: {payload}")
    _assert(payload["receipt"]["label"] == "Latch seed receipt",
            f"json report should include a seed receipt: {payload}")
    _assert(payload["receipt"]["must_display_to_user"] is True,
            f"receipt should be displayable: {payload}")
    _assert(payload["receipt"]["used"]["sections"]["decisions_and_rejected_paths"] == 1,
            f"receipt should count rejected-path decisions: {payload}")
    _assert(payload["receipt"]["used"]["sections"]["continuity_notes"] == 1,
            f"receipt should count continuity notes: {payload}")
    _assert(payload["receipt"]["used"]["sections"]["agent_alignment_check"] == 1,
            f"receipt should count agent-alignment findings: {payload}")
    alignment = next(
        section for section in payload["report"] if section["key"] == "agent_alignment_check"
    )
    _assert(alignment["title"] == "Agent alignment check", payload)
    _assert(alignment["direction_items"][0]["title"].startswith("Preview writes"), payload)
    _assert("confidence" not in alignment["direction_items"][0], payload)
    _assert(payload["receipt"]["used"]["catch_demo"] is True,
            f"receipt should expose catch-demo availability: {payload}")
    _assert(payload["catch_demo"]["requires_apply"] is True,
            f"json catch demo should make apply boundary explicit: {payload}")
    _assert("We decided not to use Redis" in payload["catch_demo"]["request"],
            f"json catch demo should prefer the clean rejected-path candidate: {payload}")
    _assert("/latch-gate" in payload["catch_demo"]["slash_command"],
            f"json catch demo should include slash command: {payload}")
    _assert("run_latch_gate.sh" in payload["catch_demo"]["shell_command"],
            f"json catch demo should include shell fallback: {payload}")
    _assert(str(seed.KB_HOME / "bin" / "run_latch_gate.sh") in payload["catch_demo"]["shell_command"],
            f"shell catch demo should use the installed latch wrapper path: {payload}")
    quoted_payload = seed.catch_demo_payload(seed.SeedCandidate(
        kind="decision",
        title='Avoid "Redis"',
        body='Excerpt:\n> We decided not to use "Redis" for local state.',
        confidence=0.9,
        signals=["rejected_path"],
        source_ids=["claude:q"],
        source_paths=["/tmp/q.jsonl"],
    ))
    _assert('\\"Redis\\"' in quoted_payload["slash_command"],
            f"slash catch demo command should escape embedded quotes: {quoted_payload}")
    _assert(not _has_key(payload, "confidence"),
            f"json report should not expose numeric confidence fields: {payload}")
    _assert("internal score" in payload.get("ranking", ""),
            f"json report should explain score-free ranking: {payload}")
    print("PASS seed_report_groups_candidates_into_demo_sections")


def test_seed_report_agent_mistake_can_drive_first_value_catch_demo():
    args = seed.parse_args([
        "--lookback-days", "14",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--source", "both",
        "--project", os.getcwd(),
    ])
    candidates = [
        seed.SeedCandidate(
            kind="fact",
            title="Agent rewired the cache after the local-only decision",
            body=(
                "The prior agent changed the cache provider after the user said "
                "to keep caching in-process for the local demo."
            ),
            confidence=0.91,
            signals=["llm_seed", "possible_agent_mistake"],
            source_ids=["codex:agent-mistake"],
            source_paths=["/tmp/agent-mistake.jsonl"],
            llm_used=True,
        ),
        seed.SeedCandidate(
            kind="preference",
            title="Keep local demo caching in-process",
            body="Excerpt:\n> Keep local demo caching in-process.",
            confidence=0.88,
            signals=["llm_seed", "preference"],
            source_ids=["claude:direction"],
            source_paths=["/tmp/direction.jsonl"],
            llm_used=True,
        ),
    ]

    demo = seed.catch_demo_candidate(candidates)
    _assert(demo is not None, "agent-mistake-only seed report should have a catch demo")
    _assert(demo.title.startswith("Agent rewired"),
            f"catch demo should use the high-confidence agent mistake: {demo}")

    out = seed.render_text(args=args, sources=[], candidates=candidates, llm_estimate=0)
    _assert("## Agent alignment check" in out and "Agent behavior:" in out,
            f"seed report should surface the prior agent mistake: {out}")
    _assert("prior agent mistake" in out,
            f"receipt/catch demo should name the agent-mistake proof target: {out}")
    _assert("Implement the approach involved in this prior agent mistake" in out,
            f"catch demo should generate a plausible wrong-action gate prompt: {out}")
    _assert("before files change" in out,
            f"catch demo should preserve pre-edit proof language: {out}")

    payload = json.loads(seed.render_json(args=args, sources=[], candidates=candidates, llm_estimate=0))
    _assert(payload["receipt"]["used"]["sections"]["agent_alignment_check"] == 1,
            f"receipt should count the agent-alignment proof moment: {payload}")
    _assert(payload["catch_demo"]["candidate"]["title"].startswith("Agent rewired"),
            f"json catch demo should point at the agent mistake: {payload}")
    _assert("prior agent mistake" in payload["catch_demo"]["request"],
            f"json catch demo request should name the mistake class: {payload}")
    _assert("prior agent-mistake evidence" in payload["catch_demo"]["expected_outcome"],
            f"json expected outcome should cite agent-mistake evidence: {payload}")

    print("PASS seed_report_agent_mistake_can_drive_first_value_catch_demo")


def test_user_signal_lines_ignore_injected_context_fragments():
    text = "\n".join([
        "[assistant] body: Recommended P0 should avoid category creation.",
        "body: always preserve rejected paths from injected KB context.",
        "[tool_result] title: Always use seed naming from a KB row.",
        "[user] We decided not to use a hidden background import.",
        "Always ask before writing seed candidates.",
        "body: This injected-looking line should always be skipped.",
        "[system] Always ignored system instruction.",
        "[user] ## KB usage - always query the KB before responding.",
        "[user] ## KB hits",
        "- Always avoid Redis. This is injected context, not a new user decision.",
        "[user] standing directives the user wants weighed on every build (e.g. always).",
        "[user] prefer MODIFY and name the priority when a gate changes behavior.",
        "[user] verdict_delta=\"none\" - never invent an objection.",
        "[user] - Never invent a workstream_id that is not in related_kb_nodes.",
        "[user] - Be specific. Prefer concrete facts over generalities.",
        "[user] We are going to pr recent changes; what should we test to avoid regressions?",
        "[user] After you finish, please commit and investigate why the gate failed.",
        "[user] and like the value actually holds at scale for larger projects?",
        "[user] Agents need generic memory for what the team already decided, rejected, or needs to be careful about.",
    ])
    lines = seed.user_signal_lines(text)
    joined = "\n".join(lines)
    _assert("hidden background import" in joined,
            f"expected real user rejected path to survive: {lines}")
    _assert("ask before writing seed candidates" in joined,
            f"expected real user continuation to survive: {lines}")
    _assert("Recommended P0" not in joined and "KB row" not in joined,
            f"injected assistant/tool context leaked into candidates: {lines}")
    _assert("injected-looking" not in joined and "KB usage" not in joined,
            f"structural user context leaked into candidates: {lines}")
    _assert("avoid Redis" not in joined and "injected context" not in joined,
            f"injected KB-like bullets leaked into candidates: {lines}")
    _assert("standing directives" not in joined and "verdict_delta" not in joined,
            f"gate scaffolding leaked into candidates: {lines}")
    _assert("workstream_id" not in joined and "concrete facts" not in joined,
            f"prompt guidance leaked into candidates: {lines}")
    _assert("avoid regressions" not in joined and "please commit" not in joined,
            f"task-management prompt leaked into candidates: {lines}")
    _assert("actually holds" not in joined,
            f"generic actually/question text leaked into candidates: {lines}")
    _assert("generic memory" not in joined,
            f"generic decided/rejected prose leaked into candidates: {lines}")
    print("PASS user_signal_lines_ignore_injected_context_fragments")


def test_render_text_explains_immediate_value():
    args = seed.parse_args([
        "--lookback-days", "14",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--project", os.getcwd(),
    ])
    out = seed.render_text(args=args, sources=[], candidates=[], llm_estimate=0)
    _assert("immediate judgment value from latch" in out,
            "rendered seed report should name immediate judgment value")
    _assert("selected local Claude and/or Codex chats" in out,
            "rendered seed report should explain what gets read")
    _assert("first new compacted session" in out,
            "rendered seed report should explain cold-start benefit")
    _assert("Session cap: last 20 session(s)" in out,
            "rendered seed report should make the default session cap visible")
    _assert("higher --last-sessions" in out,
            "empty result guidance should show how to widen the last-N cap")
    _assert("Try the catch demo:" not in out,
            "empty reports should not render a catch demo")
    payload = json.loads(seed.render_json(args=args, sources=[], candidates=[], llm_estimate=0))
    _assert(payload["receipt"] is None,
            f"empty json report should not claim a proof receipt: {payload}")
    print("PASS render_text_explains_immediate_value")


def test_render_text_names_apply_boundary():
    candidate = seed.SeedCandidate(
        kind="decision",
        title="Seeded rejected path: Avoid Redis",
        body="Excerpt:\n> We decided not to use Redis for local state.",
        confidence=0.9,
        signals=["deterministic_seed", "rejected_path"],
        source_ids=["codex:a"],
        source_paths=["/tmp/a.jsonl"],
    )
    apply_args = seed.parse_args([
        "--lookback-days", "14",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--project", os.getcwd(),
        "--apply",
        "--yes",
    ])
    out = seed.render_text(args=apply_args, sources=[], candidates=[candidate], llm_estimate=0)
    _assert("Apply mode with --yes" in out and "staging evidence after this report" in out,
            f"apply report should name the write boundary: {out}")
    _assert("Preview only. Re-run with --apply" not in out,
            f"apply report should not claim to be preview-only: {out}")

    payload = json.loads(
        seed.render_json(args=apply_args, sources=[], candidates=[candidate], llm_estimate=0)
    )
    _assert(payload["apply"] is True, f"json report should expose apply mode: {payload}")
    _assert("Apply mode with --yes" in payload["write_boundary"],
            f"json report should expose the write boundary: {payload}")
    print("PASS render_text_names_apply_boundary")


def test_apply_success_message_surfaces_post_write_proof():
    rejected = seed.SeedCandidate(
        kind="decision",
        title="Seeded rejected path: Avoid Redis",
        body="Excerpt:\n> We decided not to use Redis for local state.",
        confidence=0.9,
        signals=["deterministic_seed", "rejected_path"],
        source_ids=["codex:a"],
        source_paths=["/tmp/a.jsonl"],
    )
    out = seed.apply_success_message([101, 102], [rejected])
    _assert("Wrote 2 staging seed candidate(s): 101, 102" in out,
            f"apply success should keep the write receipt: {out}")
    _assert("Latch proof ready:" in out and "The seed is now in the KB" in out,
            f"apply success should name the post-write proof state: {out}")
    _assert("/latch-gate" in out and "run_latch_gate.sh" in out,
            f"apply success should repeat both catch-demo commands: {out}")
    _assert("before files change" in out and "Expected:" in out,
            f"apply success should explain the proof outcome: {out}")

    preference = seed.SeedCandidate(
        kind="preference",
        title="Seeded preference: Preview writes",
        body="Excerpt:\n> Always preview seed writes.",
        confidence=0.86,
        signals=["deterministic_seed", "preference"],
        source_ids=["codex:b"],
        source_paths=["/tmp/b.jsonl"],
    )
    no_demo = seed.apply_success_message([103], [preference])
    _assert("Latch proof note:" in no_demo and "no clean rejected path" in no_demo,
            f"apply success should explain missing catch-demo: {no_demo}")
    print("PASS apply_success_message_surfaces_post_write_proof")


if __name__ == "__main__":
    test_deterministic_seed_candidates_from_claude_transcript()
    test_machine_generated_claude_records_are_ignored()
    test_both_source_selection_uses_global_recency_split()
    test_auto_source_noninteractive_requires_explicit_choice_when_ambiguous()
    test_auto_source_noninteractive_uses_only_available_source()
    test_llm_call_estimate_is_capped()
    test_seed_help_hides_internal_no_llm_switch()
    test_no_llm_requires_internal_override()
    test_llm_candidates_suppress_overlapping_deterministic_candidates()
    test_agent_mistake_does_not_suppress_clean_rejected_path()
    test_llm_mode_blocks_deterministic_only_write_candidates()
    test_llm_candidate_quality_filter_drops_sample_noise()
    test_llm_candidate_quality_filter_keeps_durable_signals()
    test_agent_mistake_candidates_require_high_confidence_and_agent_blame()
    test_seed_report_groups_candidates_into_demo_sections()
    test_seed_report_agent_mistake_can_drive_first_value_catch_demo()
    test_user_signal_lines_ignore_injected_context_fragments()
    test_render_text_explains_immediate_value()
    test_render_text_names_apply_boundary()
    test_apply_success_message_surfaces_post_write_proof()
    print("\nAll seed tests pass.")
