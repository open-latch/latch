from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import seed_report_evals  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _walk_strings(obj):
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_strings(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_strings(item)
    elif isinstance(obj, str):
        yield obj


def _assert_no_pathlike_coordinates(obj: dict, *, root: Path, project: Path) -> None:
    forbidden = [
        str(root),
        str(project),
        "/Users/",
        "/private/",
        "/var/",
        ".claude",
        ".codex",
        "/sessions/",
        "/projects/",
        ".jsonl",
        "rollout-",
        "seed-report-eval",
        "seed-report-agent-mistake",
    ]
    leaked = [
        text for text in _walk_strings(obj)
        if any(fragment in text for fragment in forbidden)
    ]
    _assert(not leaked, f"JSON output leaked local coordinates: {leaked}")


def test_seed_report_eval_passes_default_bundle():
    result = seed_report_evals.run_seed_report_eval()
    _assert(result["ok"] is True, json.dumps(result, indent=2))
    summary = result["summary"]
    _assert(summary["checks"] >= 16, summary)
    _assert(summary["source_counts"]["claude"] == 3, summary)
    _assert(summary["source_counts"]["codex"] == 2, summary)
    _assert(summary["synthetic_llm_candidate_count"] == 2, summary)
    _assert(summary["synthetic_llm_filtered_count"] == 4, summary)
    _assert(summary["catch_demo"] is True, summary)
    _assert(summary["section_counts"]["decisions_and_rejected_paths"] >= 3, summary)
    _assert(summary["section_counts"]["patterns_and_preferences"] >= 2, summary)
    _assert(summary["section_counts"]["agent_alignment_check"] == 2, summary)
    check_ids = {row["id"] for row in result["checks"] if row["passed"]}
    for required in {
        "internal_workstream_handoff",
        "next_step_followup",
        "redis_rejected_path",
        "background_queue_rejected_path",
        "oauth_popup_rejected_path",
        "preview_preference",
        "config_merge_preference",
        "agent_revived_rejected_path",
        "agent_added_queue_after_rule",
        "low_confidence_agent_mistake_filtered",
        "user_blaming_agent_mistake_filtered",
        "retroactive_agent_mistake_filtered",
        "transient_branch_noise_filtered",
        "injected_context_filtered",
        "old_out_of_scope_transcript_filtered",
        "unrelated_project_transcript_filtered",
    }:
        _assert(required in check_ids, f"missing passing check {required}: {result['checks']}")
    manifest = result["transcripts"]
    _assert(len([row for row in manifest if row.get("selected") == "yes"]) == 5, manifest)
    _assert(any(row.get("selected") == "no-lookback" for row in manifest), manifest)
    _assert(any(row.get("selected") == "no-project" for row in manifest), manifest)
    _assert(result["receipt"]["used"]["catch_demo"] is True, result["receipt"])
    _assert(result["catch_demo"]["requires_apply"] is True, result["catch_demo"])
    _assert("run_latch_gate.sh" in result["catch_demo"]["shell_command"], result["catch_demo"])
    report = seed_report_evals.render_markdown(result)
    _assert("Seed Report Eval" in report, report)
    _assert("ongoing_workstream" in report, report)
    _assert("continuity notes" in report, report)
    _assert("ongoing workstreams" not in report, report)
    _assert("agent alignment check" in report, report)
    _assert("Synthetic LLM-shaped candidates filtered: 4" in report, report)
    _assert("Catch demo: yes" in report, report)
    print("PASS seed_report_eval_passes_default_bundle")


def test_seed_report_eval_cli_writes_json():
    out = Path(tempfile.mkdtemp(prefix="seed-report-eval-json-")) / "report.json"
    rc = seed_report_evals.main(["--format", "json", "--output", str(out)])
    _assert(rc == 0, f"expected success rc, got {rc}")
    payload = json.loads(out.read_text(encoding="utf-8"))
    _assert(payload["ok"] is True, payload)
    _assert(payload["summary"]["synthetic_llm_candidate_count"] == 2, payload["summary"])
    _assert("real_smoke" not in payload, payload)
    _assert("transcripts" in payload, payload)
    print("PASS seed_report_eval_cli_writes_json")


def test_real_conversation_smoke_is_preview_only_and_redacted():
    root = Path(tempfile.mkdtemp(prefix="seed-report-real-smoke-"))
    project = root / "project" / "latch-fixture"
    project.mkdir(parents=True)
    claude_home = root / ".claude"
    codex_home = root / ".codex"
    seed_report_evals.write_transcript_bundle(
        project=project,
        claude_home=claude_home,
        codex_home=codex_home,
    )

    smoke = seed_report_evals.run_real_conversation_smoke(
        seed_report_evals.RealSmokeOptions(
            project=str(project),
            source="both",
            lookback_days=30,
            max_sessions=10,
            claude_home=str(claude_home),
            codex_home=str(codex_home),
        )
    )
    _assert(smoke["preview_only"] is True, smoke)
    _assert(smoke["writes_enabled"] is False, smoke)
    _assert(smoke["llm_calls"] == 0, smoke)
    _assert(smoke["project_scope"] == "selected_project", smoke)
    _assert("project" not in smoke, smoke)
    _assert(smoke["sources_scanned"] == 5, smoke)
    _assert(smoke["source_counts"] == {"claude": 3, "codex": 2, "cursor": 0}, smoke)
    _assert(smoke["source_indices"][0] == {"index": 1, "agent": "codex"}, smoke)
    _assert(smoke["candidate_count"] >= 6, smoke)
    _assert(smoke["section_counts"]["decisions_and_rejected_paths"] >= 3, smoke)
    _assert(smoke["receipt"]["must_display_to_user"] is True, smoke)
    _assert(smoke["catch_demo"]["requires_apply"] is True, smoke)
    _assert("candidate" not in smoke["catch_demo"], smoke["catch_demo"])
    _assert("request" not in smoke["catch_demo"], smoke["catch_demo"])
    _assert("source paths are omitted" in smoke["catch_demo"]["redaction"], smoke["catch_demo"])
    _assert("source_refs" not in smoke, smoke)
    _assert("notes" in smoke and any("project paths" in note for note in smoke["notes"]), smoke)
    _assert_no_pathlike_coordinates(smoke, root=root, project=project)
    print("PASS real_conversation_smoke_is_preview_only_and_redacted")


def test_seed_report_eval_cli_real_smoke_requires_explicit_source():
    out = Path(tempfile.mkdtemp(prefix="seed-report-real-smoke-cli-")) / "report.json"
    rc = seed_report_evals.main(["--real-smoke", "--format", "json", "--output", str(out)])
    _assert(rc == 2, f"--real-smoke without explicit source should fail, got {rc}")
    _assert(not out.exists(), "failed real-smoke invocation should not write output")
    print("PASS seed_report_eval_cli_real_smoke_requires_explicit_source")


def test_seed_report_eval_cli_can_include_fixture_real_smoke():
    root = Path(tempfile.mkdtemp(prefix="seed-report-real-smoke-cli-ok-"))
    project = root / "project" / "latch-fixture"
    project.mkdir(parents=True)
    claude_home = root / ".claude"
    codex_home = root / ".codex"
    seed_report_evals.write_transcript_bundle(
        project=project,
        claude_home=claude_home,
        codex_home=codex_home,
    )
    out = root / "report.json"
    rc = seed_report_evals.main([
        "--format", "json",
        "--output", str(out),
        "--real-smoke",
        "--real-source", "both",
        "--real-project", str(project),
        "--real-lookback-days", "30",
        "--real-last-sessions", "10",
        "--real-claude-home", str(claude_home),
        "--real-codex-home", str(codex_home),
    ])
    _assert(rc == 0, f"expected real-smoke CLI success, got {rc}")
    payload = json.loads(out.read_text(encoding="utf-8"))
    _assert(payload["ok"] is True, payload)
    _assert(payload["redaction"]["fixture_eval"] == "summary_only", payload)
    for omitted in {
        "catch_demo",
        "checks",
        "receipt",
        "sections",
        "synthetic_llm_filtered",
        "transcripts",
    }:
        _assert(omitted not in payload, f"{omitted} should not be in real-smoke JSON: {payload}")
    _assert(payload["real_smoke"]["preview_only"] is True, payload["real_smoke"])
    _assert(payload["real_smoke"]["writes_enabled"] is False, payload["real_smoke"])
    _assert(payload["real_smoke"]["sources_scanned"] == 5, payload["real_smoke"])
    _assert_no_pathlike_coordinates(payload, root=root, project=project)
    print("PASS seed_report_eval_cli_can_include_fixture_real_smoke")


if __name__ == "__main__":
    test_seed_report_eval_passes_default_bundle()
    test_seed_report_eval_cli_writes_json()
    test_real_conversation_smoke_is_preview_only_and_redacted()
    test_seed_report_eval_cli_real_smoke_requires_explicit_source()
    test_seed_report_eval_cli_can_include_fixture_real_smoke()
    print("\nAll seed report eval tests pass.")
