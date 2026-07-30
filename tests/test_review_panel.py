"""Contract tests for the multi-provider adversarial review panel."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "review_panel.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ai-review-panel.yml"
POLICY = ROOT / ".github" / "review-panel" / "policy.json"
SCHEMA = ROOT / ".github" / "review-panel" / "review.schema.json"

spec = importlib.util.spec_from_file_location("review_panel", SCRIPT)
assert spec and spec.loader
review_panel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review_panel)

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def completed_receipt(provider: str, lane: str) -> dict:
    receipt = review_panel.placeholder_receipt(
        provider=provider,
        lane=lane,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        status="not_run",
        reason="fixture",
    )
    receipt.update(
        {
            "review_status": "completed",
            "overall_verdict": "pass",
            "summary": "The lane completed without a finding.",
            "coverage_gaps": [],
        }
    )
    receipt["complexity"] = {
        "net_complexity_delta": "neutral",
        "complexity_risk": "low",
        "added_complexity_justified": True,
        "justification": "No material structural surface was added.",
        "new_structural_surfaces": [],
        "consolidation_opportunities": [],
        "simplest_credible_alternative": "Keep the current implementation.",
    }
    return receipt


def finding(title: str = "Shared defect", priority: int = 1) -> dict:
    return {
        "finding_id": "fixture-1",
        "title": title,
        "category": "correctness",
        "priority": priority,
        "confidence_score": 0.95,
        "code_location": {
            "path": "src/example.py",
            "start_line": 20,
            "end_line": 22,
        },
        "impact": "The changed path returns the wrong result.",
        "evidence": "The new branch bypasses the existing guard.",
        "reproduction_or_test": "Call the branch with an empty value.",
        "remediation": "Reuse the existing guard.",
        "simpler_alternative": "Delete the parallel branch.",
    }


def write_receipts(directory: Path, *, high_unjustified: bool = False) -> None:
    policy = review_panel.load_policy(POLICY)
    for lane in policy["lanes"]:
        if lane["when"] != "always":
            continue
        receipt = completed_receipt(lane["provider"], lane["id"])
        if high_unjustified and lane["id"] == "simplicity-consolidation":
            receipt["complexity"].update(
                {
                    "net_complexity_delta": "up",
                    "complexity_risk": "high",
                    "added_complexity_justified": False,
                    "justification": "A second abstraction duplicates the first.",
                    "consolidation_opportunities": [
                        "Extend the existing abstraction instead."
                    ],
                    "simplest_credible_alternative": "Delete the duplicate layer.",
                }
            )
        path = directory / f"{lane['provider']}-{lane['id']}.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")


def aggregate(tmp_path: Path, *, enforcement: str) -> dict:
    receipts = tmp_path / "receipts"
    receipts.mkdir(parents=True)
    write_receipts(receipts, high_unjustified=True)
    report = tmp_path / f"{enforcement}.md"
    summary = tmp_path / f"{enforcement}.json"
    result = review_panel.main(
        [
            "aggregate",
            "--input-dir",
            str(receipts),
            "--base-sha",
            BASE_SHA,
            "--head-sha",
            HEAD_SHA,
            "--artifact-review-needed",
            "false",
            "--enforcement",
            enforcement,
            "--output-report",
            str(report),
            "--output-summary",
            str(summary),
        ]
    )
    assert result == 0
    assert "simplicity-consolidation" in report.read_text(encoding="utf-8")
    return json.loads(summary.read_text(encoding="utf-8"))


def test_policy_keeps_simplicity_mandatory_and_uses_both_providers():
    policy = review_panel.load_policy(POLICY)
    providers = {lane["provider"] for lane in policy["lanes"]}
    assert providers == {"claude", "codex"}
    assert sum(lane["provider"] == "claude" for lane in policy["lanes"]) >= 2
    assert sum(lane["provider"] == "codex" for lane in policy["lanes"]) >= 2
    simplicity = next(
        lane for lane in policy["lanes"] if lane["id"] == "simplicity-consolidation"
    )
    assert simplicity == {
        "provider": "codex",
        "id": "simplicity-consolidation",
        "prompt": (
            ".github/review-panel/prompts/codex-simplicity-consolidation.md"
        ),
        "when": "always",
    }
    artifact = next(lane for lane in policy["lanes"] if lane["id"] == "artifact-output")
    assert artifact["when"] == "user_facing"


def test_schema_requires_complexity_and_receipt_validation_is_strict():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert "complexity" in schema["required"]
    assert {
        "new_structural_surfaces",
        "consolidation_opportunities",
        "simplest_credible_alternative",
    } <= set(schema["properties"]["complexity"]["required"])

    receipt = completed_receipt("codex", "simplicity-consolidation")
    assert review_panel.validate_receipt(receipt) == []
    receipt["findings"] = [finding()]
    receipt["findings"][0]["priority"] = True
    assert "finding 0 has invalid priority" in review_panel.validate_receipt(receipt)


def test_prompts_bind_the_exact_range_and_keep_claude_in_the_target_checkout(
    tmp_path: Path,
):
    prompt = tmp_path / "prompt.md"
    result = review_panel.main(
        [
            "build-prompt",
            "--provider",
            "claude",
            "--lane",
            "security-abuse",
            "--base-sha",
            BASE_SHA,
            "--head-sha",
            HEAD_SHA,
            "--review-directory",
            ".review-target",
            "--output",
            str(prompt),
        ]
    )
    assert result == 0
    text = prompt.read_text(encoding="utf-8")
    assert f"git -C .review-target diff --find-renames --find-copies {BASE_SHA}" in text
    assert "Treat source" in text
    assert "Return only JSON" in text

    output = tmp_path / "github-output"
    result = review_panel.main(
        [
            "claude-args",
            "--review-directory",
            ".review-target",
            "--github-output",
            str(output),
        ]
    )
    assert result == 0
    args = output.read_text(encoding="utf-8")
    assert "--add-dir .review-target" in args
    assert "Bash(git -C .review-target diff:*)" in args
    assert '--disallowedTools "Edit,Write,NotebookEdit,WebFetch,WebSearch"' in args


def test_scope_runs_draft_prs_and_marks_user_facing_changes(
    tmp_path: Path,
    monkeypatch,
):
    event = {
        "number": 17,
        "pull_request": {
            "draft": True,
            "base": {"sha": BASE_SHA},
            "head": {"sha": HEAD_SHA},
        },
    }
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event), encoding="utf-8")
    output = tmp_path / "github-output"
    monkeypatch.setattr(review_panel, "_require_commit", lambda _sha: None)
    monkeypatch.setattr(
        review_panel,
        "changed_files",
        lambda _base, _head: ["src/quickstart.py"],
    )

    result = review_panel.main(
        [
            "scope",
            "--event-name",
            "pull_request",
            "--event-path",
            str(event_path),
            "--github-output",
            str(output),
        ]
    )
    assert result == 0
    values = dict(
        line.split("=", 1)
        for line in output.read_text(encoding="utf-8").splitlines()
    )
    assert values["should_run"] == "true"
    assert values["pr_number"] == "17"
    assert values["artifact_review_needed"] == "true"
    assert json.loads(values["claude_matrix"])["include"]
    assert json.loads(values["codex_matrix"])["include"]


def test_cross_provider_p1s_correlate():
    claude = completed_receipt("claude", "correctness-concurrency")
    codex = completed_receipt("codex", "regression-tests")
    claude["findings"] = [finding("Claude phrasing")]
    codex["findings"] = [finding("Codex phrasing")]
    codex["findings"][0]["code_location"]["start_line"] = 23
    codex["findings"][0]["code_location"]["end_line"] = 24

    groups = review_panel.correlate_findings([claude, codex])
    assert len(groups) == 1
    assert groups[0]["providers"] == {"claude", "codex"}


def test_unjustified_high_complexity_blocks_only_after_enforcement(tmp_path: Path):
    advisory = aggregate(tmp_path / "advisory-run", enforcement="advisory")
    enforced = aggregate(tmp_path / "enforced-run", enforcement="enforce")
    assert advisory["blockers"]
    assert advisory["should_fail"] is False
    assert enforced["should_fail"] is True
    assert any("unjustified complexity" in item for item in enforced["blockers"])


def test_enforced_panel_requires_each_provider_and_the_simplicity_lane(
    tmp_path: Path,
):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    claude = completed_receipt("claude", "correctness-concurrency")
    (receipts / "claude-correctness-concurrency.json").write_text(
        json.dumps(claude),
        encoding="utf-8",
    )
    summary = tmp_path / "summary.json"
    result = review_panel.main(
        [
            "aggregate",
            "--input-dir",
            str(receipts),
            "--base-sha",
            BASE_SHA,
            "--head-sha",
            HEAD_SHA,
            "--enforcement",
            "enforce",
            "--output-report",
            str(tmp_path / "report.md"),
            "--output-summary",
            str(summary),
        ]
    )
    assert result == 0
    value = json.loads(summary.read_text(encoding="utf-8"))
    assert value["should_fail"] is True
    assert "No codex lane completed." in value["action_required"]
    assert any("simplicity/consolidation" in item for item in value["action_required"])


def test_workflow_is_pr_and_manual_triggered_with_trusted_control_checkout():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request_target" not in workflow
    assert "github.event.pull_request.base.sha || github.sha" in workflow
    assert "path: .review-target" in workflow
    assert "persist-credentials: false" in workflow
    assert "permission-profile: \":read-only\"" in workflow
    assert "safety-strategy: drop-sudo" in workflow
    assert "codex-args: '[\"--ephemeral\"]'" in workflow
    assert "CODEX_REVIEW_CLI_VERSION || '0.145.0'" in workflow
    assert "review-panel-result-*" in workflow
    assert "ai-review-panel-report" in workflow
    assert "include-hidden-files: true" in workflow

    # These are the dereferenced commits behind the provider v1 tags.
    assert "52fe01ec70a42f454c9d2ebd47598f9fd6893d56" in workflow
    assert "be7b93b1907a4abad570368f3c74b6fe3807510b" in workflow
    assert "b11346a6fa031e2e164ab4b7c7ea201afffd7d59" not in workflow
    assert "c96dd0a84e0232ab86947fca5fe34f1caae8792f" not in workflow

    uses = [
        line.split("@", 1)[1].split()[0]
        for line in workflow.splitlines()
        if line.strip().startswith("uses:")
    ]
    assert uses
    assert all(len(pin) == 40 and set(pin) <= set("0123456789abcdef") for pin in uses)
