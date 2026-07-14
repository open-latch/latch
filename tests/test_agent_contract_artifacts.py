from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import agents_md_sync  # noqa: E402
import claude_md_sync  # noqa: E402
import cursor_rules_sync  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _block(begin: str, rendered: str, end: str) -> str:
    return f"{begin}\n{rendered}\n{end}\n"


def _metric(text: str) -> dict[str, int]:
    return {"lines": len(text.splitlines()), "words": len(text.split())}


def _evidence_metric(text: str) -> dict[str, int | str]:
    data = text.encode("utf-8")
    return {
        "lines": len(text.splitlines()),
        "words": len(text.split()),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_machine_reviewable_obligations_and_footprint_budgets() -> None:
    matrix = _load("agent_contract_obligations.json")
    claude = _block(
        claude_md_sync.BEGIN_MARK,
        claude_md_sync.render_contract(kb_home="/opt/latch"),
        claude_md_sync.END_MARK,
    )
    agents = _block(
        agents_md_sync.BEGIN_MARK,
        agents_md_sync.render_contract(kb_home="/opt/latch"),
        agents_md_sync.END_MARK,
    )
    cursor_rule = cursor_rules_sync.render_rule(kb_home="/opt/latch")
    surfaces = {
        "shared_contract": (claude, agents),
        "claude_contract": (claude,),
        "agents_contract": (agents,),
        "cursor_rule": (cursor_rule,),
    }

    for obligation in matrix["obligations"]:
        assert obligation["id"] and obligation["description"]
        for surface, markers in obligation["markers"].items():
            assert surface in surfaces, (obligation["id"], surface)
            for text in surfaces[surface]:
                for marker in markers:
                    assert marker in text, (obligation["id"], surface, marker)

    measured = {
        "claude_managed": _metric(claude),
        "agents_managed": _metric(agents),
        "cursor_rule": _metric(cursor_rule),
        "cursor_always_loaded": _metric(agents + cursor_rule),
    }
    for surface, budget in matrix["budgets"].items():
        assert measured[surface]["lines"] <= budget["max_lines"], (surface, measured[surface])
        assert measured[surface]["words"] <= budget["max_words"], (surface, measured[surface])


def test_scenario_corpus_covers_every_obligation_and_host() -> None:
    matrix = _load("agent_contract_obligations.json")
    corpus = _load("agent_contract_scenarios.json")
    hosts = set(corpus["hosts"])
    scenarios = {row["id"]: row for row in corpus["scenarios"]}

    assert hosts == {"claude", "codex", "cursor"}
    for row in scenarios.values():
        assert row["tier"] in {"deterministic", "live_host", "deep_followup"}
        assert set(row["hosts"]) <= hosts
        assert row["stimulus"] and row["expected"]
        if row["tier"] == "deterministic":
            assert row.get("evidence_tests")
            for nodeid in row["evidence_tests"]:
                path, test_name = nodeid.split("::", 1)
                text = (ROOT / path).read_text(encoding="utf-8")
                assert f"def {test_name}(" in text, nodeid

    for obligation in matrix["obligations"]:
        assert obligation["scenario_ids"]
        assert set(obligation["scenario_ids"]) <= set(scenarios), obligation["id"]


def test_reference_preserves_offloaded_rules_and_host_boundaries() -> None:
    text = (ROOT / "docs" / "agent-contract-reference.md").read_text(encoding="utf-8")
    required = [
        "older --reconciled_by--> newer",
        "newer --supersedes--> older",
        "reconciliation_banner",
        "No silent",
        "Claude Code",
        "Codex",
        "Cursor",
        "WIRING_VERSION" if "WIRING_VERSION" in text else "latch-wiring-version",
    ]
    for marker in required:
        assert marker in text, marker


def test_evidence_candidate_footprints_match_rendered_surfaces() -> None:
    evidence = json.loads(
        (ROOT / "artifacts" / "agent-contract-trim" / "evidence.json").read_text(
            encoding="utf-8"
        )
    )
    claude = _block(
        claude_md_sync.BEGIN_MARK,
        claude_md_sync.render_contract(kb_home="/opt/latch"),
        claude_md_sync.END_MARK,
    )
    agents = _block(
        agents_md_sync.BEGIN_MARK,
        agents_md_sync.render_contract(kb_home="/opt/latch"),
        agents_md_sync.END_MARK,
    )
    cursor_rule = cursor_rules_sync.render_rule(kb_home="/opt/latch")
    measured = {
        "source_snippet": _evidence_metric(
            (ROOT / "claude_md_snippet.md").read_text(encoding="utf-8")
        ),
        "claude_managed_block": _evidence_metric(claude),
        "agents_managed_block": _evidence_metric(agents),
        "cursor_rule": _evidence_metric(cursor_rule),
        "cursor_always_loaded": _evidence_metric(agents + "\n" + cursor_rule),
    }
    assert evidence["measurement"]["candidate"] == measured


def test_evidence_baseline_footprints_match_base_commit() -> None:
    evidence = json.loads(
        (ROOT / "artifacts" / "agent-contract-trim" / "evidence.json").read_text(
            encoding="utf-8"
        )
    )
    commit = evidence["base_commit"]

    def show(path: str) -> str:
        proc = subprocess.run(
            ["git", "show", f"{commit}:{path}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            pytest.skip(f"baseline commit {commit} is unavailable in this checkout")
        return proc.stdout

    version = show("WIRING_VERSION").strip()
    source = show("claude_md_snippet.md")

    def contract(target_name: str, installer_name: str) -> str:
        text = source.replace("{{KB_HOME}}", "/opt/latch")
        if target_name != "CLAUDE.md":
            text = text.replace("CLAUDE.md", target_name)
        if installer_name != "install_claude_md":
            text = text.replace("install_claude_md", installer_name)
        rendered = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        return f"<!-- latch-wiring-version: {version} -->\n{rendered}"

    claude = _block(
        claude_md_sync.BEGIN_MARK,
        contract("CLAUDE.md", "install_claude_md"),
        claude_md_sync.END_MARK,
    )
    agents = _block(
        agents_md_sync.BEGIN_MARK,
        contract("AGENTS.md", "install_agents_md"),
        agents_md_sync.END_MARK,
    )
    cursor_rule = (
        show("cursor_rule_snippet.mdc")
        .replace("{{KB_HOME}}", "/opt/latch")
        .replace("{{WIRING_VERSION}}", version)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip("\n")
        + "\n"
    )
    measured = {
        "source_snippet": _evidence_metric(source),
        "claude_managed_block": _evidence_metric(claude),
        "agents_managed_block": _evidence_metric(agents),
        "cursor_rule": _evidence_metric(cursor_rule),
        "cursor_always_loaded": _evidence_metric(agents + "\n" + cursor_rule),
    }
    assert evidence["mainline_integration"]["origin_main_commit"] == commit
    assert evidence["measurement"]["baseline"] == measured


def test_generated_root_instruction_files_are_not_tracked() -> None:
    proc = subprocess.run(
        ["git", "ls-files", "--", "CLAUDE.md", "AGENTS.md"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert proc.stdout.strip() == ""
