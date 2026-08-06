"""An OFF -> LATCHED subtree cycle permanently stales delayed work."""
from __future__ import annotations

import shutil
from pathlib import Path
import uuid

import pytest

import mcp_broker
import mcp_runtime
import paths
import project_config
import selfheal


@pytest.fixture
def inherited_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "continuity-tests" / uuid.uuid4().hex
    home = tmp_path / "latch-home"
    home.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)

    root = tmp_path / "consulting"
    child = root / "client"
    nested = child / "service"
    sibling = root / "internal"
    nested.mkdir(parents=True)
    sibling.mkdir()
    vault = test_root / "vaults" / f"continuity-{uuid.uuid4()}"
    vault.mkdir(parents=True)
    project_config.create_scope(root, policy=project_config.POLICY_PRIVATE)
    project_config.authorize_scope(root, kb_dir=vault)

    yield root, child, nested, sibling, vault

    shutil.rmtree(vault, ignore_errors=True)
    shutil.rmtree(control, ignore_errors=True)


def _cycle_off_then_latched(child: Path) -> None:
    assert project_config.create_off_boundary(child).state == (
        project_config.MODE_UNLATCHED
    )
    assert project_config.remove_off_boundary(child).state == (
        project_config.MODE_LATCHED
    )


def test_child_relatch_changes_only_its_effective_continuity_revision(
    inherited_scope: tuple[Path, Path, Path, Path, Path],
) -> None:
    root, child, nested, sibling, _vault = inherited_scope
    before_root = project_config.resolve(root)
    before_child = project_config.resolve(child)
    before_nested = project_config.resolve(nested)
    before_sibling = project_config.resolve(sibling)

    assert before_child.target_revision == before_root.target_revision
    assert before_child.revision == before_root.revision
    assert before_nested.revision == before_child.revision
    assert before_sibling.revision == before_root.revision

    _cycle_off_then_latched(child)

    after_root = project_config.resolve(root)
    after_child = project_config.resolve(child)
    after_nested = project_config.resolve(nested)
    after_sibling = project_config.resolve(sibling)
    assert after_child.target_revision == before_child.target_revision
    assert after_child.revision != before_child.revision
    assert after_nested.revision == after_child.revision
    assert after_root.revision == before_root.revision
    assert after_sibling.revision == before_sibling.revision


def test_child_relatch_rejects_old_session_but_allows_a_fresh_one(
    inherited_scope: tuple[Path, Path, Path, Path, Path],
) -> None:
    _root, child, _nested, sibling, _vault = inherited_scope
    old_revision = project_config.record_session_binding(child, "old-child-task")
    sibling_revision = project_config.record_session_binding(
        sibling,
        "sibling-task",
    )
    assert old_revision == project_config.resolve(child).revision

    _cycle_off_then_latched(child)

    assert project_config.current_session_revision(child, "old-child-task") is None
    assert project_config.current_session_revision(
        sibling,
        "sibling-task",
    ) == sibling_revision
    fresh_revision = project_config.record_session_binding(
        child,
        "fresh-child-task",
    )
    assert fresh_revision == project_config.resolve(child).revision
    assert fresh_revision != old_revision


def test_child_relatch_rejects_old_mcp_scope_before_runtime_lookup(
    inherited_scope: tuple[Path, Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, child, _nested, _sibling, _vault = inherited_scope
    descriptor = mcp_runtime.scope_descriptor_from_resolved(
        project_config.resolve(child)
    )
    _cycle_off_then_latched(child)
    touched: list[str] = []
    monkeypatch.setattr(
        mcp_broker,
        "_checked_discovery",
        lambda: touched.append("runtime") or None,
    )

    with pytest.raises(mcp_broker.BrokerError, match="scope changed"):
        mcp_broker.ensure_daemon(str(child), scope=descriptor)
    assert touched == []


def test_delayed_selfheal_rejects_revision_captured_before_child_relatch(
    inherited_scope: tuple[Path, Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, child, _nested, _sibling, vault = inherited_scope
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 4242

    def fake_popen(args, **_kwargs):
        captured["args"] = list(args)
        return FakeProcess()

    monkeypatch.setattr(selfheal.subprocess, "Popen", fake_popen)
    selfheal.spawn_detached(str(child))
    args = captured["args"]
    assert isinstance(args, list)
    old_revision = args[3]
    assert old_revision == project_config.resolve(child).revision
    assert args[4] == str(vault.resolve())

    _cycle_off_then_latched(child)
    touched: list[str] = []
    monkeypatch.setattr(
        selfheal,
        "_run_selfheal_locked",
        lambda _project: touched.append("data-plane") or {"ok": True},
    )

    result = selfheal.run_selfheal(
        args[2],
        expected_binding_revision=args[3],
        expected_kb_dir=args[4],
    )
    assert result == {"ok": False, "reason": "target_changed"}
    assert touched == []


def test_crash_after_epoch_write_leaves_child_off_boundary_intact(
    inherited_scope: tuple[Path, Path, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _root, child, _nested, sibling, _vault = inherited_scope
    sibling_revision = project_config.resolve(sibling).revision
    project_config.create_off_boundary(child)

    def crash_before_unlink(path: Path) -> None:
        assert path == project_config.local_binding_path(child)
        raise OSError("simulated crash before OFF unlink")

    monkeypatch.setattr(project_config, "durable_unlink", crash_before_unlink)
    with pytest.raises(OSError, match="simulated crash"):
        project_config.remove_off_boundary(child)

    assert project_config.continuity_epoch_path(child).is_file()
    after_crash = project_config.resolve(child)
    assert after_crash.state == project_config.MODE_UNLATCHED
    assert after_crash.kb_dir is None
    assert project_config.resolve(sibling).revision == sibling_revision
