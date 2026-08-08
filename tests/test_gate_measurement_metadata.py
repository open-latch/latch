"""Focused v2.6 runtime-emission and compact-host-response tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db  # noqa: E402
import gate  # noqa: E402
import log_utils  # noqa: E402
import mcp_broker  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402
import project_proof  # noqa: E402


_ID_LIST_FIELDS = (
    "evidence_ids",
    "decision_chain",
    "abandoned_paths",
    "active_constraints",
    "current_direction",
    "seed_ids",
)


def _last_gate_row(project: Path) -> dict:
    lines = log_utils.today_log_path(gate.LOG_STREAM, project).read_text(
        encoding="utf-8"
    ).splitlines()
    return json.loads(lines[-1])


def test_gate_result_and_log_share_v26_opaque_metadata(tmp_path: Path) -> None:
    project = tmp_path / "private-customer-name" / "secret-repo"
    project.mkdir(parents=True)
    conn = db.connect(str(project))
    try:
        identity = conn._kb_vault_identity
        result = gate.run_gate(
            conn,
            "structural request",
            project_path=str(project),
            session_id="session-1",
            host_adapter="codex",
            use_llm=False,
        )
        row = _last_gate_row(project)
        expected = gate.measurement_metadata(
            conn, str(project), host_adapter="codex"
        )

        for field in gate.MEASUREMENT_METADATA_FIELDS:
            assert result[field] == expected[field]
            assert row[field] == expected[field]
        assert result["measurement_protocol_version"] == "outcome-v2.6.0"
        assert result["host_adapter"] == "codex"
        assert result["key_epoch"] == "outcome-v2.6-key-1"
        assert result["runtime_version"] == mcp_broker.RUNTIME_KEY
        assert result["runtime_attestation"] == mcp_broker.RUNTIME_KEY
        assert result["attestation"] == mcp_broker.RUNTIME_KEY
        assert result["project_proof"] == {
            "version": project_proof.PROJECT_PROOF_VERSION,
            "key_epoch": gate.PROJECT_PROOF_KEY_EPOCH,
            "key_id": result["project_proof"]["key_id"],
            "fingerprint": result["project_proof"]["fingerprint"],
        }
        assert len(result["project_proof"]["key_id"]) == 64
        assert len(result["project_proof"]["fingerprint"]) == 64

        for field in _ID_LIST_FIELDS:
            assert isinstance(row[field], list)
            assert all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in row[field]
            )

        encoded = json.dumps({"result": result, "row": row}, sort_keys=True)
        assert str(project) not in encoded
        assert identity.vault_uuid not in encoded
        assert identity.registry_fingerprint not in encoded
    finally:
        conn.close()


def test_measurement_metadata_fails_closed_without_explicit_project_path() -> None:
    identity = SimpleNamespace(
        vault_uuid="44444444-4444-4444-8444-444444444444",
        registry_fingerprint="42" * 32,
    )
    conn = SimpleNamespace(_kb_vault_identity=identity)

    absent_path = gate.measurement_metadata(conn, None)
    absent_identity = gate.measurement_metadata(
        None, "/private/customer-name/secret-repo"
    )

    assert absent_path["project_proof"] is None
    assert absent_identity["project_proof"] is None
    for envelope in (absent_path, absent_identity):
        assert envelope["measurement_protocol_version"] == "outcome-v2.6.0"
        assert envelope["runtime_version"] == mcp_broker.RUNTIME_KEY
        encoded = json.dumps(envelope, sort_keys=True)
        assert identity.vault_uuid not in encoded
        assert identity.registry_fingerprint not in encoded
        assert "/private/customer-name/secret-repo" not in encoded


@pytest.mark.parametrize(
    ("session_source", "expected"),
    [
        ("env:CLAUDE_CODE_SESSION_ID", "claude"),
        ("env:CODEX_THREAD_ID", "codex"),
        ("codex_session_start_marker", "codex"),
        ("env:LATCH_SESSION_ID", None),
        ("unavailable", None),
    ],
)
def test_host_adapter_comes_only_from_connection_session_provenance(
    monkeypatch: pytest.MonkeyPatch,
    session_source: str,
    expected: str | None,
) -> None:
    context = SimpleNamespace(
        session_id="opaque-session",
        session_source=session_source,
    )
    monkeypatch.setattr(
        mcp_server.mcp_runtime, "current_connection", lambda: context
    )
    assert mcp_server._project_host_adapter() == expected


def test_compact_mcp_gate_response_preserves_measurement_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = {
        "version": project_proof.PROJECT_PROOF_VERSION,
        "key_epoch": gate.PROJECT_PROOF_KEY_EPOCH,
        "fingerprint": "ab" * 32,
    }
    metadata = {
        "measurement_protocol_version": gate.MEASUREMENT_PROTOCOL_VERSION,
        "host_adapter": "codex",
        "attestation": "runtime-test",
        "runtime_attestation": "runtime-test",
        "runtime_version": "runtime-test",
        "project_proof_version": project_proof.PROJECT_PROOF_VERSION,
        "key_epoch": gate.PROJECT_PROOF_KEY_EPOCH,
        "project_proof": proof,
    }
    verdict = {
        "recommendation": "PROCEED",
        "summary": "stubbed",
        "decision_chain": [],
        "abandoned_paths": [],
        "active_constraints": [],
        "current_direction": [],
        "evidence_nodes": [],
        "load_bearing_claims": [],
        "uncovered_claims": [],
        "risk_if_proceed": "",
        "better_next_action": "",
        "error": None,
    }
    full = {
        "request": "request",
        "gate_call_id": "abcdef123456",
        **metadata,
        "verdict": verdict,
        "findings": {},
        "chains": {"seeds": [], "evidence_node_ids": []},
        "evidence": [],
    }

    class _ConnContext:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(mcp_server.paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(mcp_server, "_conn", lambda: _ConnContext())
    monkeypatch.setattr(mcp_server, "_project_host_adapter", lambda: "codex")
    monkeypatch.setattr(mcp_server.gate, "run_gate", lambda *_a, **_k: full)

    compact = mcp_server.kb_gate("request", verbose=False)
    assert compact["gate_call_id"] == "abcdef123456"
    for field, value in metadata.items():
        assert compact[field] == value


def test_unlatched_mcp_gate_uses_readonly_identity_for_opaque_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = "/private/customer-name/secret-repo"
    identity = SimpleNamespace(
        vault_uuid="44444444-4444-4444-8444-444444444444",
        registry_fingerprint="24" * 32,
    )

    class _ReadOnlyConn:
        _kb_vault_identity = identity

        def close(self):
            pass

    monkeypatch.setattr(mcp_server.paths, "is_unlatched_mode", lambda: True)
    monkeypatch.setattr(mcp_server, "_project_cwd", lambda: project)
    monkeypatch.setattr(
        mcp_server.db, "connect_readonly", lambda _project: _ReadOnlyConn()
    )

    result = mcp_server.kb_gate("request")
    expected = project_proof.ProjectProofContext.from_vault_identity(
        identity,
        key_epoch=gate.PROJECT_PROOF_KEY_EPOCH,
    ).prove(project)
    assert result["gate_call_id"]
    assert result["verdict"]["reason"] == "unlatched"
    assert result["project_proof"] == expected
    assert result["runtime_version"] == mcp_broker.RUNTIME_KEY
    encoded = json.dumps(result, sort_keys=True)
    assert project not in encoded
    assert identity.vault_uuid not in encoded
    assert identity.registry_fingerprint not in encoded
