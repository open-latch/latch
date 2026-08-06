"""Focused safety tests for root-local native instruction overrides."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import project_config
import unlatch


@pytest.fixture
def local_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    parent = tmp_path / "consulting"
    root = parent / "client-a"
    child = root / "service" / "api"
    sibling = parent / "client-b"
    child.mkdir(parents=True)
    sibling.mkdir(parents=True)
    state_dir = tmp_path / "machine-state" / "scope-a"
    state_file = state_dir / "unlatch-state.json"

    monkeypatch.setattr(project_config, "project_root", lambda _path: root)
    monkeypatch.setattr(
        project_config,
        "unlatch_state_path",
        lambda _root: state_file,
        raising=False,
    )

    def ensure_state(_root):
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir

    monkeypatch.setattr(
        project_config, "ensure_state_dir", ensure_state, raising=False
    )
    return parent, root, child, sibling, state_file


def test_root_only_round_trip_preserves_bom_crlf_and_every_other_file(
    local_scope,
):
    parent, root, child, sibling, state_file = local_scope
    root_claude = b"\xef\xbb\xbfroot rule\r\nsecond rule\r\n"
    root_agents = b"root agents\n"
    untouched = {
        parent / "CLAUDE.md": b"shared consulting parent\r\n",
        parent / "AGENTS.md": b"shared parent agents\n",
        child / "CLAUDE.md": b"nested child stays byte exact\r\n",
        child / "AGENTS.md": b"nested agents stay byte exact\n",
        sibling / "CLAUDE.md": b"other client secret\r\n",
        sibling / "AGENTS.md": b"other client agents\n",
    }
    (root / "CLAUDE.md").write_bytes(root_claude)
    (root / "AGENTS.md").write_bytes(root_agents)
    for path, body in untouched.items():
        path.write_bytes(body)

    unlatch.disable(child)

    assert not (root / ".git").exists(), "the behavior must not depend on Git"
    assert state_file.is_file()
    assert not state_file.is_relative_to(root)
    masked_claude = (root / "CLAUDE.md").read_bytes()
    masked_agents = (root / "AGENTS.md").read_bytes()
    assert masked_claude.startswith(root_claude)
    assert unlatch._override_bytes(b"\r\n") in masked_claude
    assert masked_agents.startswith(root_agents)
    assert unlatch._override_bytes(b"\n") in masked_agents
    for path, body in untouched.items():
        assert path.read_bytes() == body

    unlatch.enable(child)

    assert (root / "CLAUDE.md").read_bytes() == root_claude
    assert (root / "AGENTS.md").read_bytes() == root_agents
    assert not state_file.exists()
    for path, body in untouched.items():
        assert path.read_bytes() == body


def test_missing_root_files_are_owned_then_removed(local_scope):
    _parent, root, child, _sibling, state_file = local_scope

    unlatch.disable(child)

    for name in ("CLAUDE.md", "AGENTS.md"):
        body = (root / name).read_bytes()
        assert body == unlatch._override_bytes(b"\n")
    assert state_file.is_file()

    unlatch.enable(child)

    assert not (root / "CLAUDE.md").exists()
    assert not (root / "AGENTS.md").exists()
    assert not state_file.exists()


def test_relatch_preserves_user_edits_outside_exact_override(local_scope):
    _parent, root, child, _sibling, _state_file = local_scope
    original = b"client rule\r\n"
    (root / "CLAUDE.md").write_bytes(original)
    (root / "AGENTS.md").write_bytes(b"agents rule\n")
    unlatch.disable(child)
    with (root / "CLAUDE.md").open("ab") as handle:
        handle.write(b"new user rule added while unlatched\r\n")

    unlatch.enable(child)

    assert (root / "CLAUDE.md").read_bytes() == (
        original + b"new user rule added while unlatched\r\n"
    )
    assert (root / "AGENTS.md").read_bytes() == b"agents rule\n"


def test_tampered_override_fails_before_restoring_any_file(local_scope):
    _parent, root, child, _sibling, state_file = local_scope
    (root / "CLAUDE.md").write_bytes(b"claude\n")
    (root / "AGENTS.md").write_bytes(b"agents\n")
    unlatch.disable(child)
    agents_masked = (root / "AGENTS.md").read_bytes()
    claude = (root / "CLAUDE.md").read_bytes().replace(
        b"Do not search, gate, capture", b"Do search, gate, capture", 1
    )
    (root / "CLAUDE.md").write_bytes(claude)

    with pytest.raises(project_config.ProjectConfigError, match="tampered"):
        unlatch.enable(child)

    assert (root / "CLAUDE.md").read_bytes() == claude
    assert (root / "AGENTS.md").read_bytes() == agents_masked
    assert state_file.is_file()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_linked_instruction_file_is_rejected_without_touching_target(
    local_scope, link_kind: str,
):
    _parent, root, child, sibling, state_file = local_scope
    foreign = sibling / "foreign.md"
    foreign.write_bytes(b"foreign client data\n")
    target = root / "CLAUDE.md"
    try:
        if link_kind == "symlink":
            target.symlink_to(foreign)
        else:
            os.link(foreign, target)
    except OSError as exc:
        pytest.skip(f"{link_kind} unavailable: {exc}")
    (root / "AGENTS.md").write_bytes(b"local agents\n")

    with pytest.raises(project_config.ProjectConfigError, match="linked"):
        unlatch.disable(child)

    assert foreign.read_bytes() == b"foreign client data\n"
    assert (root / "AGENTS.md").read_bytes() == b"local agents\n"
    assert not state_file.exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_linked_machine_receipt_is_rejected_before_instruction_edits(
    local_scope, link_kind: str,
):
    _parent, root, child, sibling, state_file = local_scope
    original = {
        root / "CLAUDE.md": b"local claude\n",
        root / "AGENTS.md": b"local agents\n",
    }
    for path, body in original.items():
        path.write_bytes(body)
    state_file.parent.mkdir(parents=True)
    foreign = sibling / "foreign-state.json"
    foreign.write_bytes(b"foreign machine state\n")
    try:
        if link_kind == "symlink":
            state_file.symlink_to(foreign)
        else:
            os.link(foreign, state_file)
    except OSError as exc:
        pytest.skip(f"{link_kind} unavailable: {exc}")

    with pytest.raises(project_config.ProjectConfigError, match="linked"):
        unlatch.disable(child)

    assert foreign.read_bytes() == b"foreign machine state\n"
    for path, body in original.items():
        assert path.read_bytes() == body


def test_tampered_state_cannot_target_parent_or_sibling(local_scope):
    parent, root, child, sibling, state_file = local_scope
    (root / "CLAUDE.md").write_bytes(b"root claude\n")
    (root / "AGENTS.md").write_bytes(b"root agents\n")
    outside = sibling / "AGENTS.md"
    outside.write_bytes(b"other client\n")
    parent_before = b"shared parent\n"
    (parent / "CLAUDE.md").write_bytes(parent_before)
    unlatch.disable(child)
    root_snapshots = {
        name: (root / name).read_bytes() for name in ("CLAUDE.md", "AGENTS.md")
    }
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["instruction_files"][0]["path"] = "../client-b/AGENTS.md"
    state_file.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(project_config.ProjectConfigError, match="unsafe path"):
        unlatch.enable(child)

    assert outside.read_bytes() == b"other client\n"
    assert (parent / "CLAUDE.md").read_bytes() == parent_before
    for name, body in root_snapshots.items():
        assert (root / name).read_bytes() == body


def test_disable_is_idempotent_and_status_validates_receipt(local_scope):
    _parent, root, child, _sibling, state_file = local_scope
    (root / "CLAUDE.md").write_bytes(b"claude\n")
    (root / "AGENTS.md").write_bytes(b"agents\n")
    first = unlatch.disable(child)
    snapshots = {
        name: (root / name).read_bytes() for name in ("CLAUDE.md", "AGENTS.md")
    }

    second = unlatch.disable(child)
    report = unlatch.status(child)

    assert first
    assert second == []
    assert state_file.is_file()
    assert sum("override present" in line for line in report) == 2
    for name, body in snapshots.items():
        assert (root / name).read_bytes() == body


def test_interrupted_disable_keeps_receipt_and_relatch_recovers(
    local_scope, monkeypatch: pytest.MonkeyPatch,
):
    _parent, root, child, _sibling, state_file = local_scope
    originals = {
        root / "CLAUDE.md": b"claude\r\n",
        root / "AGENTS.md": b"agents\n",
    }
    for path, body in originals.items():
        path.write_bytes(body)
    real_atomic = unlatch._atomic_bytes

    def fail_agents(path, body, *, mode, expected):
        if path == root / "AGENTS.md":
            raise OSError("injected instruction write failure")
        return real_atomic(path, body, mode=mode, expected=expected)

    monkeypatch.setattr(unlatch, "_atomic_bytes", fail_agents)
    with pytest.raises(OSError, match="injected instruction write failure"):
        unlatch.disable(child)

    assert state_file.is_file()
    assert unlatch._BEGIN_BYTES in (root / "CLAUDE.md").read_bytes()
    assert (root / "AGENTS.md").read_bytes() == originals[root / "AGENTS.md"]

    monkeypatch.setattr(unlatch, "_atomic_bytes", real_atomic)
    unlatch.enable(child)
    assert not state_file.exists()
    for path, body in originals.items():
        assert path.read_bytes() == body


def test_interrupted_relatch_is_retryable_without_remasking_restored_file(
    local_scope, monkeypatch: pytest.MonkeyPatch,
):
    _parent, root, child, _sibling, state_file = local_scope
    originals = {
        root / "CLAUDE.md": b"claude\n",
        root / "AGENTS.md": b"agents\n",
    }
    for path, body in originals.items():
        path.write_bytes(body)
    unlatch.disable(child)
    real_atomic = unlatch._atomic_bytes

    def fail_agents(path, body, *, mode, expected):
        if path == root / "AGENTS.md":
            raise OSError("injected restore failure")
        return real_atomic(path, body, mode=mode, expected=expected)

    monkeypatch.setattr(unlatch, "_atomic_bytes", fail_agents)
    with pytest.raises(OSError, match="injected restore failure"):
        unlatch.enable(child)

    assert state_file.is_file()
    assert (root / "CLAUDE.md").read_bytes() == originals[root / "CLAUDE.md"]
    assert unlatch._BEGIN_BYTES in (root / "AGENTS.md").read_bytes()

    monkeypatch.setattr(unlatch, "_atomic_bytes", real_atomic)
    unlatch.enable(child)
    assert not state_file.exists()
    for path, body in originals.items():
        assert path.read_bytes() == body
