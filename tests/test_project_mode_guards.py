"""Explicit-target operations must check the same project's Latch mode."""
from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path

import pytest

import compactor
import gate
import heal
import kb_gate_cli
import maintenance
import mcp_server
import paths
import seed
import selfheal
import tree


def test_unlatched_guards_receive_each_operation_target(monkeypatch, tmp_path):
    target = str(tmp_path / "target-project")
    Path(target).mkdir()
    seen: list[str | None] = []

    def unlatched(project_path=None):
        seen.append(project_path)
        if project_path != target:
            raise AssertionError(f"guard checked {project_path!r}, expected {target!r}")
        return True

    monkeypatch.setattr(paths, "is_unlatched_mode", unlatched)
    monkeypatch.setattr(paths, "is_disabled", lambda *_args: False)
    monkeypatch.setattr(mcp_server, "_RUNTIME_INITIALIZED", False)

    assert compactor.run_compaction("session", target, None)["reason"] == "unlatched"
    assert gate.classify_gate({}, project_path=target)["reason"] == "unlatched"
    assert gate.adversary_classify({}, {}, project_path=target)["reason"] == "unlatched"
    assert gate.run_gate(None, "request", project_path=target)["verdict"]["reason"] == "unlatched"
    assert maintenance.run_weekly_maintenance(target)["reason"] == "unlatched"
    assert maintenance.run_nightly_heal(target)["reason"] == "unlatched"
    assert maintenance.run_tree_rebuild(target)["reason"] == "unlatched"
    assert maintenance.run_workstream_shadow(target)["reason"] == "unlatched"
    assert maintenance.run_workstream_governed(target)["reason"] == "unlatched"
    assert selfheal._trigger_blocked(target) is True
    assert selfheal.run_selfheal(target)["reason"] == "unlatched"
    assert tree.build_tree(None, project_path=target)["reason"] == "unlatched"
    assert heal.nightly_heal(None, project_path=target)["reason"] == "unlatched"
    with pytest.raises(seed.SeedWriteBlocked):
        seed.apply_candidates([], project_path=target)
    assert kb_gate_cli.main(["kb_gate_cli.py", target, "request"]) == 0
    mcp_server.initialize_runtime(target, start_embed_listener=False)

    assert seen and set(seen) == {target}


def test_disabled_nested_model_guards_receive_operation_target(
    monkeypatch, tmp_path,
):
    target = str(tmp_path / "target-project")
    Path(target).mkdir()
    seen: list[str | None] = []

    def enabled(project_path=None):
        if project_path != target:
            raise AssertionError(f"guard checked {project_path!r}, expected {target!r}")
        return False

    def disabled(project_path=None):
        seen.append(project_path)
        if project_path != target:
            raise AssertionError(f"guard checked {project_path!r}, expected {target!r}")
        return True

    monkeypatch.setattr(paths, "is_unlatched_mode", enabled)
    monkeypatch.setattr(paths, "is_disabled", disabled)
    monkeypatch.setattr(paths, "is_in_compact", lambda: False)

    assert compactor.run_compaction("session", target, None)["reason"] == "disabled"
    assert gate.classify_gate({}, project_path=target)["skipped"] is True
    assert gate.adversary_classify({}, {}, project_path=target)["skipped"] is True
    assert maintenance.run_weekly_maintenance(target)["reason"] == "disabled"
    assert maintenance.run_nightly_heal(target)["reason"] == "disabled"
    assert maintenance.run_tree_rebuild(target)["reason"] == "disabled"
    assert maintenance.run_workstream_shadow(target)["reason"] == "disabled"
    assert maintenance.run_workstream_governed(target)["reason"] == "disabled"
    assert selfheal._trigger_blocked(target) is True
    assert selfheal.run_selfheal(target)["reason"] == "disabled"
    assert tree.build_tree(None, project_path=target)["reason"] == "disabled"
    assert tree._invoke_summary([], project_path=target) is None
    assert heal.nightly_heal(None, project_path=target)["reason"] == "disabled"
    assert heal.arbitrate({}, {}, 0.9, project_path=target)["decision"] == "keep_both"
    assert heal._arbitrate_nightly(
        {}, {}, 0.9, project_path=target,
    )["decision"] == "keep_both"
    assert heal.three_pass_arbitrate(
        {"id": 1}, {"id": 2}, similarity=0.7, tier="low",
        project_path=target,
    )["decision"] == "keep_both"

    assert seen and set(seen) == {target}


def test_every_nightly_heal_verdict_application_keeps_explicit_target():
    tree_node = ast.parse(textwrap.dedent(inspect.getsource(heal.nightly_heal)))
    calls = [
        node
        for node in ast.walk(tree_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_apply_verdict"
    ]

    assert calls
    assert all(
        any(keyword.arg == "project_path" for keyword in call.keywords)
        for call in calls
    )
