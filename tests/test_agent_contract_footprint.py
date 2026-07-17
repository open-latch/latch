from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

import agent_contract_footprint as footprint  # noqa: E402
import agents_md_sync  # noqa: E402
import claude_md_sync  # noqa: E402
import cursor_rules_sync  # noqa: E402


def _measure(*, lines: int = 10, words: int = 20, bytes_: int = 100) -> dict:
    return {"surface": {"lines": lines, "words": words, "bytes": bytes_}}


def test_snapshot_measurement_matches_real_install_renderers() -> None:
    read = lambda path: (ROOT / path).read_text(encoding="utf-8")
    measured = footprint.measure_snapshot(footprint.load_snapshot(read))

    claude = (
        f"{claude_md_sync.BEGIN_MARK}\n"
        f"{claude_md_sync.render_contract(kb_home='/opt/latch')}\n"
        f"{claude_md_sync.END_MARK}\n"
    )
    agents = (
        f"{agents_md_sync.BEGIN_MARK}\n"
        f"{agents_md_sync.render_contract(kb_home='/opt/latch')}\n"
        f"{agents_md_sync.END_MARK}\n"
    )
    cursor_rule = cursor_rules_sync.render_rule(kb_home="/opt/latch")
    expected = {
        "source_snippet": footprint.metric(read("claude_md_snippet.md")),
        "claude_managed": footprint.metric(claude),
        "agents_managed": footprint.metric(agents),
        "cursor_rule": footprint.metric(cursor_rule),
        "cursor_always_loaded": footprint.metric(agents + "\n" + cursor_rule),
    }

    assert measured == expected


def test_utf8_byte_metric_catches_growth_hidden_from_line_and_word_counts() -> None:
    base = footprint.metric("one line")
    current = footprint.metric("one line" + "x" * 100)

    assert current["lines"] == base["lines"]
    assert current["words"] == base["words"]
    assert current["bytes"] > base["bytes"]


def test_any_relative_surface_growth_requires_review() -> None:
    base = _measure()
    assert footprint.growth_findings(base, _measure()) == []
    assert footprint.growth_findings(base, _measure(lines=11)) == [
        "surface.lines grew by 1"
    ]
    assert footprint.growth_findings(base, _measure(words=21)) == [
        "surface.words grew by 1"
    ]
    assert footprint.growth_findings(base, _measure(bytes_=101)) == [
        "surface.bytes grew by 1"
    ]
    assert footprint.growth_findings(base, _measure(lines=9, words=19, bytes_=99)) == []


def test_budget_loosening_is_separately_reviewable() -> None:
    base = {"surface": {"max_lines": 10, "max_words": 20, "max_bytes": 100}}
    current = {"surface": {"max_lines": 10, "max_words": 21, "max_bytes": 100}}

    assert footprint.budget_loosening_findings(base, current) == [
        "surface.max_words increased from 20 to 21"
    ]


def test_absolute_caps_are_not_removed_by_relative_growth_approval() -> None:
    current = _measure(lines=11)
    budgets = {
        "surface": {"max_lines": 10, "max_words": 20, "max_bytes": 100}
    }

    assert footprint.absolute_budget_findings(current, budgets) == [
        "surface.lines is 11, above max_lines=10"
    ]


def test_ci_workflow_exposes_dedicated_signal_and_review_label() -> None:
    workflow = (ROOT / ".github" / "workflows" / "agent-contract-footprint.yml").read_text(
        encoding="utf-8"
    )
    policy = json.loads(
        (ROOT / "tests" / "fixtures" / "agent_contract_obligations.json").read_text(
            encoding="utf-8"
        )
    )

    assert "agent-contract-footprint" in workflow
    assert policy["growth_review_label"] in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "tests/test_agent_contract_footprint.py" in workflow
