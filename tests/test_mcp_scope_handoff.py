"""Canonical scope identity must survive every MCP process boundary."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import pytest

import db
import mcp_broker
import mcp_daemon
import mcp_proxy
import mcp_runtime
import mcp_server
import paths
import project_config


def _private_scope(tmp_path: Path) -> tuple[Path, Path, project_config.ResolvedScope]:
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    project = tmp_path / f"project-{uuid.uuid4()}"
    project.mkdir()
    test_root = paths.validated_test_root()
    assert test_root is not None
    vault = test_root / "vaults" / f"mcp-scope-{uuid.uuid4()}"
    vault.mkdir(parents=True)
    project_config.create_scope(project, policy=project_config.POLICY_PRIVATE)
    target = project_config.authorize_scope(project, kb_dir=vault)
    assert target.state == project_config.MODE_LATCHED
    return project, vault.resolve(), target


def _metadata(
    project: Path,
    descriptor: mcp_runtime.ScopeDescriptor,
) -> dict[str, object]:
    return {
        "project_cwd": str(project),
        "proxy_pid": 123,
        "project_binding_revision": descriptor.revision,
        "project_kb_dir": descriptor.kb_dir,
        "scope": descriptor.payload(),
        "in_compact": False,
        "unlatched": False,
        "disabled": False,
        "write_disabled": False,
        "in_maintenance": False,
        "gate_backend": "codex",
        "maintenance_backend": "codex",
        "gate_classifier_timeout_s": 44,
        "gate_adversary_timeout_s": 22,
        "gate_adversary_enabled": True,
        "proxy_policy": {
            "cap": 7,
            "retire_idle_s": 11.0,
            "heartbeat_s": 3.0,
            "stale_s": 19.0,
        },
    }


def _compatibility_scope(tmp_path: Path) -> tuple[Path, mcp_runtime.ScopeDescriptor]:
    project = tmp_path / f"compat-project-{uuid.uuid4()}"
    vault = tmp_path / f"compat-vault-{uuid.uuid4()}"
    project.mkdir()
    vault.mkdir()
    return project, mcp_runtime.validate_scope_descriptor({
        "root": str(project.resolve()),
        "state": project_config.MODE_LATCHED,
        "policy": project_config.POLICY_SHARED,
        "scope_id": None,
        "revision": "1" * 32,
        "kb_dir": str(vault.resolve()),
        "target_fingerprint": "2" * 64,
        "lock_key": "shared-global",
    })


def test_descriptor_rejects_denied_states_and_noncanonical_targets(tmp_path: Path):
    project, vault, target = _private_scope(tmp_path)
    descriptor = mcp_runtime.scope_descriptor_from_resolved(target)
    assert descriptor.root == str(project.resolve())
    assert descriptor.kb_dir == str(vault)

    denied = descriptor.payload()
    denied["state"] = "unlatched"
    with pytest.raises(ValueError, match="LATCHED"):
        mcp_runtime.validate_scope_descriptor(denied)

    noncanonical = descriptor.payload()
    noncanonical["kb_dir"] = str(vault / ".." / vault.name)
    with pytest.raises(ValueError, match="canonical"):
        mcp_runtime.validate_scope_descriptor(noncanonical)


def test_proxy_metadata_carries_one_exact_scope_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project, vault, target = _private_scope(tmp_path)
    monkeypatch.delenv("LATCH_UNLATCHED", raising=False)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("LATCH_ADAPTER", raising=False)

    metadata = mcp_proxy.connection_metadata(str(project))
    descriptor = mcp_runtime.validate_scope_descriptor(metadata["scope"])

    assert descriptor == mcp_runtime.scope_descriptor_from_resolved(target)
    assert metadata["project_binding_revision"] == descriptor.revision
    assert metadata["project_kb_dir"] == str(vault)
    assert metadata["unlatched"] is False


def test_broker_never_synthesizes_scope_for_authenticated_test_harness(
    tmp_path: Path,
):
    project = tmp_path / "outside-explicit-scope"
    project.mkdir()
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)

    target = project_config.resolve(project)
    assert target.state == project_config.MODE_LOCKED
    assert target.reason_code == project_config.LOCK_OUTSIDE_SCOPE
    with pytest.raises(ValueError, match="LATCHED scope"):
        mcp_broker.resolve_connection_scope(str(project))


def test_proxy_compatibility_scope_requires_verified_session_before_data_plane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project, descriptor = _compatibility_scope(tmp_path)
    touched: list[str] = []
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda _cwd: False)
    monkeypatch.setattr(
        mcp_broker, "resolve_connection_scope", lambda _cwd: descriptor
    )
    monkeypatch.setattr(mcp_proxy, "_resolve_session", lambda _cwd: (None, "missing"))
    monkeypatch.setattr(mcp_proxy, "_requires_verified_session", lambda: True)
    monkeypatch.setattr(
        mcp_broker,
        "vault_context_digest",
        lambda: touched.append("data-plane"),
    )

    with pytest.raises(ValueError, match="no verified agent session"):
        mcp_proxy.connection_metadata(str(project))
    assert touched == []


def test_proxy_compatibility_scope_rejects_missing_session_receipt_before_data_plane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project, descriptor = _compatibility_scope(tmp_path)
    touched: list[str] = []
    monkeypatch.setattr(paths, "is_unlatched_mode", lambda _cwd: False)
    monkeypatch.setattr(
        mcp_broker, "resolve_connection_scope", lambda _cwd: descriptor
    )
    monkeypatch.setattr(
        mcp_proxy, "_resolve_session", lambda _cwd: ("compat-task", "test")
    )
    monkeypatch.setattr(
        project_config, "current_session_revision", lambda _cwd, _session_id: None
    )
    monkeypatch.setattr(
        mcp_broker,
        "vault_context_digest",
        lambda: touched.append("data-plane"),
    )

    with pytest.raises(ValueError, match="older project scope"):
        mcp_proxy.connection_metadata(str(project))
    assert touched == []


@pytest.mark.parametrize("session_id", [None, "compat-task"])
def test_server_compatibility_scope_rejects_missing_session_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str | None,
):
    project, descriptor = _compatibility_scope(tmp_path)
    monkeypatch.setattr(
        mcp_broker, "resolve_connection_scope", lambda _cwd: descriptor
    )
    monkeypatch.setattr(
        project_config, "current_session_revision", lambda _cwd, _session_id: None
    )

    assert mcp_server.project_binding_snapshot(
        str(project), session_id=session_id, require_session=True
    ) == ("stale-session", None)


def test_broker_rejects_stale_descriptor_before_runtime_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project, _vault, target = _private_scope(tmp_path)
    descriptor = mcp_runtime.scope_descriptor_from_resolved(target)
    project_config.set_scope_mode(project, project_config.MODE_UNLATCHED)
    touched: list[str] = []
    monkeypatch.setattr(
        mcp_broker,
        "_checked_discovery",
        lambda: touched.append("discovery"),
    )

    with pytest.raises(mcp_broker.BrokerError, match="not available|changed"):
        mcp_broker.ensure_daemon(str(project), scope=descriptor)
    assert touched == []


def test_daemon_context_rejects_missing_or_disagreeing_scope(
    tmp_path: Path,
):
    project, _vault, target = _private_scope(tmp_path)
    descriptor = mcp_runtime.scope_descriptor_from_resolved(target)
    metadata = _metadata(project, descriptor)

    context = mcp_daemon._context_from(dict(metadata), "accepted")
    assert context.scope_descriptor == descriptor

    missing = dict(metadata)
    missing.pop("scope")
    with pytest.raises(ValueError, match="scope descriptor"):
        mcp_daemon._context_from(missing, "missing")

    mismatched = dict(metadata)
    mismatched["project_kb_dir"] = str(tmp_path)
    with pytest.raises(ValueError, match="disagrees"):
        mcp_daemon._context_from(mismatched, "mismatch")

    denied = dict(metadata)
    denied["unlatched"] = True
    with pytest.raises(ValueError, match="no MCP data plane"):
        mcp_daemon._context_from(denied, "denied")


def test_daemon_environment_hands_off_exact_owner_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    _project, vault, target = _private_scope(tmp_path)
    descriptor = mcp_runtime.scope_descriptor_from_resolved(target)

    control_root = paths.validated_test_root() / "custom-scope-control"
    control_root.mkdir()
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control_root))
    with mcp_runtime.bind_runtime_scope(descriptor):
        environment = mcp_broker._daemon_environment()

    assert environment["LATCH_KB_DIR"] == str(vault)
    assert environment[project_config.CONTROL_ROOT_ENV] == str(control_root)
    assert (
        mcp_runtime.validate_scope_descriptor(
            json.loads(environment[mcp_broker.OWNER_SCOPE_ENV])
        )
        == descriptor
    )


def test_server_rechecks_descriptor_after_scope_transition(tmp_path: Path):
    project, _vault, target = _private_scope(tmp_path)
    descriptor = mcp_runtime.scope_descriptor_from_resolved(target)
    context = mcp_runtime.ConnectionContext(
        connection_id="scope-transition",
        project_cwd=str(project),
        session_id=None,
        session_source="test",
        proxy_pid=os.getpid(),
        proxy_started_at="now",
        runtime_key="test",
        project_binding_revision=descriptor.revision,
        project_kb_dir=descriptor.kb_dir,
        scope_descriptor=descriptor,
    )
    project_config.set_scope_mode(project, project_config.MODE_UNLATCHED)

    with mcp_runtime.bind_connection(context):
        with pytest.raises(db.ProjectTargetChangedError, match="no longer|older"):
            mcp_server._assert_connection_target()


def test_server_rechecks_session_receipt_when_scope_is_unchanged(tmp_path: Path):
    project, vault, target = _private_scope(tmp_path)
    session_id = "revoked-mcp-session"
    assert project_config.record_session_binding(project, session_id) == (
        target.revision
    )
    descriptor = mcp_runtime.scope_descriptor_from_resolved(target)
    context = mcp_runtime.ConnectionContext(
        connection_id="session-receipt-revalidation",
        project_cwd=str(project),
        session_id=session_id,
        session_source="test",
        proxy_pid=os.getpid(),
        proxy_started_at="now",
        runtime_key="test",
        project_binding_revision=descriptor.revision,
        project_kb_dir=descriptor.kb_dir,
        scope_descriptor=descriptor,
    )

    with mcp_runtime.bind_connection(context):
        mcp_server._assert_connection_target(vault)
        project_config.record_session_boundary(project, session_id)
        assert mcp_runtime.scope_descriptor_from_resolved(
            project_config.resolve(project)
        ) == descriptor
        assert project_config.current_session_revision(project, session_id) is None
        with pytest.raises(db.ProjectTargetChangedError, match="older agent task"):
            mcp_server._assert_connection_target(vault)

    cursor_context = mcp_runtime.ConnectionContext(
        **{
            **context.__dict__,
            "connection_id": "unattributed-cursor-connection",
            "session_id": None,
            "session_source": "unavailable",
        }
    )
    with mcp_runtime.bind_connection(cursor_context):
        mcp_server._assert_connection_target(vault)


@pytest.mark.parametrize("mode", ["locked", "unlatched"])
def test_runtime_initialization_cleanly_skips_denied_project(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
):
    project = tmp_path / mode
    project.mkdir()
    if mode == "locked":
        monkeypatch.setattr(paths, "is_unlatched_mode", lambda _cwd: False)
        monkeypatch.setattr(
            mcp_broker,
            "resolve_connection_scope",
            lambda _cwd: (_ for _ in ()).throw(
                project_config.ProjectConfigError("synthetic LOCKED scope")
            ),
        )
    else:
        _project, _vault, _target = _private_scope(tmp_path)
        project = _project
        project_config.set_scope_mode(project, project_config.MODE_UNLATCHED)
    touched: list[str] = []
    monkeypatch.setattr(mcp_server, "_RUNTIME_INITIALIZED", False)
    monkeypatch.setattr(
        mcp_server, "_start_embed_listener", lambda _cwd: touched.append("listener")
    )
    monkeypatch.setattr(
        mcp_server.embeddings,
        "embed",
        lambda _text: touched.append("model"),
    )

    assert mcp_server.initialize_runtime(
        str(project), start_embed_listener=True
    ) is False
    assert touched == []


def test_forced_legacy_rejects_unlatched_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    project, _vault, _target = _private_scope(tmp_path)
    project_config.set_scope_mode(project, project_config.MODE_UNLATCHED)
    monkeypatch.chdir(project)
    monkeypatch.setenv("LATCH_MCP_FORCE_LEGACY", "1")
    called: list[str] = []
    monkeypatch.setattr(
        mcp_proxy, "_resolve_session", lambda _cwd: called.append("session")
    )
    monkeypatch.setattr(
        mcp_proxy, "_exec_legacy_server", lambda: called.append("legacy")
    )

    assert mcp_proxy.main() == 2
    assert called == []
