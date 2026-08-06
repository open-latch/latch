"""Current-protocol canonical writer -> runner -> report baseline."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from filelock import FileLock

import db
import gate
import log_utils
import outcome_measurement as om
import outcome_measurement_cli as cli
import outcome_measurement_runner as runner
import paths
import project_proof


UTC = timezone.utc
FIXTURES = Path(__file__).parent / "fixtures" / "outcome_measurement"
EPOCH = "outcome-v2.6-key-test"
RUNTIME = "runtime-test"
COMMIT = "a" * 40


def _fixture_bytes() -> dict[str, bytes]:
    return {
        name: (FIXTURES / name).read_bytes()
        for name in om.FROZEN_FIXTURE_PACK_SHA256
    }


def _current_protocol_host(
    *,
    project: Path,
    proof: dict[str, str],
    timestamp: datetime,
) -> tuple[dict[str, object], bytes]:
    """Mutate the pinned real Codex S2 shape, never invent an envelope."""

    iso = timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    nonce = "corpus-current-baseline"
    session = "corpus-current-session"
    common = {
        "gate_call_id": nonce,
        "session_id": session,
        "attestation": RUNTIME,
        "runtime_attestation": RUNTIME,
        "measurement_protocol_version": om.MEASUREMENT_PROTOCOL_VERSION,
        "project_proof": proof,
        "project_proof_version": project_proof.PROJECT_PROOF_VERSION,
        "key_epoch": EPOCH,
        "runtime_version": RUNTIME,
        "host_adapter": "codex",
        "skipped": False,
    }
    host = [
        json.loads(line)
        for line in (
            FIXTURES / "codex-rollout-2026-07-29.sanitized.jsonl"
        ).read_text().splitlines()
    ]
    host[0]["timestamp"] = iso
    host[0]["payload"].update(
        {
            "id": session,
            "session_id": session,
            "timestamp": iso,
            "cwd": str(project),
        }
    )
    host_result = {
        **common,
        "verdict": {
            "recommendation": "PROCEED",
            **{name: [] for name in om.REQUIRED_ID_LIST_FIELDS},
            "skipped": False,
        },
    }
    host[1]["timestamp"] = iso
    host[2]["timestamp"] = iso
    host[2]["payload"]["output"] = json.dumps(host_result, sort_keys=True)
    return common, (
        "\n".join(json.dumps(row, separators=(",", ":")) for row in host)
        + "\n"
    ).encode()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def test_lineage_checkpoint_round_trips_structural_authority_privately(
    tmp_path: Path,
) -> None:
    config = om.MeasurementConfig(
        t0=datetime(2026, 8, 1, tzinfo=UTC),
        cap=datetime(2026, 8, 22, tzinfo=UTC),
        target_project_proof={
            "version": project_proof.PROJECT_PROOF_VERSION,
            "key_epoch": EPOCH,
            "key_id": "0" * 16,
            "fingerprint": "0" * 64,
        },
        key_epoch=EPOCH,
        pinned_runtime_version=RUNTIME,
        implementation_commit=COMMIT,
    )
    order = datetime(2026, 8, 1, tzinfo=UTC)
    observation = om.Observation(
        source=om.SOURCE_GATE,
        file="/private/structural-coordinate/gate.log",
        byte_offset=17,
        nonce="privacy-safe-lineage",
        ts=order,
        session_id="opaque-session-coordinate",
        adapter="codex",
        attestation=RUNTIME,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        project_proof=config.target_project_proof,
        key_epoch=EPOCH,
        runtime_version=RUNTIME,
        verdict="PROCEED",
        verdict_id_lists={name: [] for name in om.REQUIRED_ID_LIST_FIELDS},
        skipped=False,
        observable=True,
        evidence_available=True,
        progress_inserts=1,
        inserts=1,
        linked_cited_insert=True,
        cited_edge_activity=True,
        touches=1,
        raw_sha256="f" * 64,
    )
    provisional = om.InvocationReceipt(
        nonce="privacy-safe-lineage",
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        observations=(observation,),
        disposition="loss_signal",
        admitted=True,
        lineage_order_key=order,
        fresh_ts=order,
        session_id="opaque-session-coordinate",
        verdict="PROCEED",
        outcome="CENSORED",
        censored_reason="instrument_unavailable",
        loss_reasons=("host_only",),
        window_start=order,
        window_end=order + timedelta(minutes=30),
        prefix_member=True,
        boundary_evidence=(observation,),
    )
    finalized_observation = replace(
        observation,
        file="/private/structural-coordinate/finalized.log",
        byte_offset=23,
        nonce="finalized-authority",
        ts=order + timedelta(minutes=1),
    )
    finalized = replace(
        provisional,
        nonce="finalized-authority",
        observations=(finalized_observation,),
        disposition="confirmatory",
        lineage_order_key=order + timedelta(minutes=1),
        fresh_ts=order + timedelta(minutes=1),
        outcome="ACCEPTED",
        censored_reason=None,
        loss_reasons=(),
        finalized=True,
        window_start=order + timedelta(minutes=1),
        window_end=order + timedelta(minutes=31),
        boundary_evidence=(finalized_observation,),
    )
    state = om.AuditState(
        config=config,
        receipts=(provisional, finalized),
        loss_markers=(),
        source_health=(),
        snapshots=(),
        gate_rows=(),
    )
    checkpoint = tmp_path / "lineage.json"
    coordinate = "b" * 64
    runner.write_lineage_checkpoint(
        checkpoint,
        state,
        coordinate_sha256=coordinate,
    )
    encoded = checkpoint.read_text()
    assert "privacy-safe-lineage" in encoded
    assert "observations" in encoded
    assert "source_bytes" not in encoded
    assert "prompt" not in encoded
    assert "tool_output" not in encoded
    assert "database_rows" not in encoded
    assert "implementation_commit" not in encoded
    assert checkpoint.stat().st_mode & 0o777 == 0o600
    loaded = runner.load_lineage_checkpoint(
        checkpoint, coordinate_sha256=coordinate
    )
    assert loaded == (provisional, finalized)
    with pytest.raises(ValueError, match="schema or coordinate"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256="c" * 64
        )

    malformed_checkpoint = json.loads(encoded)
    malformed_checkpoint["receipts"][0]["lineage_order_key"] = "not-a-time"
    _write_json(checkpoint, malformed_checkpoint)
    with pytest.raises(ValueError, match="checkpoint row is invalid"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256=coordinate
        )

    malformed = tmp_path / "malformed-envelope.json"
    _write_json(
        malformed,
        {
            "schema": cli.ENVELOPE_SCHEMA,
            "config": {
                "t0": "2026-08-01T00:00:00Z",
                "cap": "2026-08-22T00:00:00Z",
                "source_roots": {"S1": "/not/a/list", "S2": ["/host"]},
            },
        },
    )
    try:
        cli.load_envelope(malformed)
    except ValueError as exc:
        assert "non-empty string lists" in str(exc)
    else:
        raise AssertionError("accepted a string as a source-root list")


def test_cli_runs_corpus_derived_current_protocol_and_persists_lineage(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    conn = db.connect(str(project))
    try:
        context = project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch=EPOCH,
        )
        proof = context.prove(str(project))
    finally:
        conn.close()

    now = datetime.now(UTC)
    event_ts = now - timedelta(minutes=10)
    t0 = event_ts - timedelta(hours=1)
    cap = t0 + timedelta(days=21)
    host_root = tmp_path / "host"
    host_root.mkdir()
    host_path = host_root / "rollout-corpus-current.jsonl"
    measurement, host_bytes = _current_protocol_host(
        project=project,
        proof=proof,
        timestamp=event_ts,
    )
    event_iso = event_ts.isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    monkeypatch.setattr(
        log_utils, "_today_utc_date", lambda: event_ts.date().isoformat()
    )
    monkeypatch.setattr(log_utils, "_now_iso", lambda: event_iso)
    gate._log_invocation(
        project_path=str(project),
        session_id="corpus-current-session",
        request="SANITIZED CURRENT-PROTOCOL BASELINE",
        verdict={
            "recommendation": "PROCEED",
            "skipped": False,
            "decision_chain": [],
            "abandoned_paths": [],
            "active_constraints": [],
            "current_direction": [],
        },
        chain_assembly={"seeds": [], "evidence_node_ids": []},
        evidence=[],
        elapsed_ms=1.0,
        gate_call_id="corpus-current-baseline",
        measurement=measurement,
    )
    gate_root = paths.project_dir(str(project))
    gate_path = gate_root / f"gate-{event_ts.date().isoformat()}.log"
    assert gate_path.is_file(), "the real gate writer must produce S1"
    host_path.write_bytes(host_bytes)
    roots = {"S1": [str(gate_root)], "S2": [str(host_root)]}

    contract = tmp_path / "contract.md"
    contract.write_bytes(b"corpus current-protocol baseline contract")
    contract_hash = hashlib.sha256(contract.read_bytes()).hexdigest()
    monkeypatch.setattr(om, "CONTRACT_SHA256", contract_hash)
    fixture_bytes = _fixture_bytes()
    fixture_hashes = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in fixture_bytes.items()
    }
    manifest = om.MeasurementManifest(
        contract_sha256=contract_hash,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit=COMMIT,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots=roots,
        fixture_hashes=fixture_hashes,
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
    canaries = [
        {
            "host": host,
            "nonce": f"corpus-derived-{host}",
            "tool_result_seen": True,
            "gate_log_seen": True,
            "host_record_seen": True,
            "dual_source_joined": True,
            "runtime_version": RUNTIME,
            "measurement_protocol_version": om.MEASUREMENT_PROTOCOL_VERSION,
            "key_epoch": EPOCH,
            "project_proof": proof,
        }
        for host in ("codex", "claude")
    ]
    envelope = tmp_path / "envelope.json"
    _write_json(
        envelope,
        {
            "schema": cli.ENVELOPE_SCHEMA,
            "config": {
                "t0": t0.isoformat(),
                "cap": cap.isoformat(),
                "target_project_proof": proof,
                "key_epoch": EPOCH,
                "pinned_runtime_version": RUNTIME,
                "measurement_protocol_version": om.MEASUREMENT_PROTOCOL_VERSION,
                "implementation_commit": COMMIT,
                "source_roots": roots,
                "require_fresh_snapshots": True,
            },
            "capture": {
                "node_id": om.CAPTURE_NODE_ID,
                "contract_sha256": contract_hash,
                "contract_version": om.CONTRACT_VERSION,
                "supersedes_node_id": om.SUPERSEDED_CAPTURE_NODE_ID,
            },
            "manifest": {
                "contract_sha256": manifest.contract_sha256,
                "ratification_node_ids": list(manifest.ratification_node_ids),
                "implementation_commit": manifest.implementation_commit,
                "measurement_protocol_version": manifest.measurement_protocol_version,
                "source_roots": roots,
                "fixture_hashes": fixture_hashes,
                "composite_sha256": manifest.composite_sha256,
            },
            "fixture_paths": {
                name: str(FIXTURES / name) for name in fixture_bytes
            },
            # These are deterministic test receipts over corpus-derived current
            # shapes. They are not represented as completed live canaries.
            "canaries": canaries,
        },
    )
    lineage = tmp_path / "lineage.json"
    report = tmp_path / "report.json"
    argv = [
        "--project", str(project),
        "--envelope", str(envelope),
        "--contract", str(contract),
        "--lineage", str(lineage),
        "--report", str(report),
        "--initialize-empty-lineage",
    ]

    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: COMMIT)
    lock_path = str(lineage.resolve()) + ".lock"
    with FileLock(lock_path, timeout=0):
        assert cli.main(argv) == 2
    lock_error = json.loads(capsys.readouterr().err)
    assert lock_error["ok"] is False
    assert not lineage.exists()
    assert not report.exists()

    original_report_writer = runner.write_canonical_report

    def fail_report_write(path, report_text):
        raise OSError("report sink unavailable")

    monkeypatch.setattr(runner, "write_canonical_report", fail_report_write)
    assert cli.main(argv) == 2
    assert not lineage.exists(), "lineage must not advance before report commit"
    assert not report.exists()
    capsys.readouterr()
    monkeypatch.setattr(runner, "write_canonical_report", original_report_writer)

    assert cli.main(argv) == 0
    receipt = json.loads(capsys.readouterr().out)
    payload = json.loads(report.read_text())
    assert receipt["report_kind"] == "canonical_outcome_audit"
    assert receipt["lineage_updated"] is True
    assert payload["oracles"]["invalidated"] is False
    assert payload["oracles"]["v1_green"] is False
    assert payload["oracles"]["o2"] == "indeterminate"
    assert payload["oracles"]["o2_reasons"] == ["unfinalized_receipts"]
    assert all(
        not any(
            row[field]
            for field in (
                "missing_files",
                "unreadable_files",
                "malformed_regions",
                "unstable_files",
            )
        )
        for row in payload["accounting"]["source_health"]
    )
    assert all(
        row["complete"]
        for row in payload["accounting"]["candidate_completeness"]
    )
    checkpoint_before = lineage.read_bytes()
    loaded_envelope = cli.load_envelope(envelope)
    coordinate = runner.lineage_checkpoint_coordinate(
        loaded_envelope["config"], loaded_envelope["manifest"]
    )
    prior = runner.load_lineage_checkpoint(
        lineage,
        coordinate_sha256=coordinate,
        contract_sha256=contract_hash,
    )
    assert [(row.nonce, row.admitted) for row in prior] == [
        ("corpus-current-baseline", True)
    ]
    assert prior[0].observations
    assert prior[0].finalized is False

    alias_argv = list(argv)
    alias_argv[alias_argv.index("--report") + 1] = str(host_root / "audit.jsonl")
    assert cli.main(alias_argv) == 2
    alias_error = json.loads(capsys.readouterr().err)
    assert "inside a measured root" in alias_error["error"]
    assert lineage.read_bytes() == checkpoint_before

    # Removing both current source rows cannot reset the persisted admitted
    # lineage. Full structural authority makes both deletions explicit and
    # retains a censored, unfinalized prefix receipt without invalidating.
    gate_path.write_bytes(b"")
    host_path.write_bytes(b"")
    argv.remove("--initialize-empty-lineage")
    assert cli.main(argv) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["lineage_updated"] is True
    payload = json.loads(report.read_text())
    assert payload["oracles"]["invalidated"] is False
    assert "admitted_lineage_authority_missing" not in payload["oracles"][
        "invalidation_reasons"
    ]
    assert {
        (row["reason"], row["source"], row["nonce"])
        for row in payload["loss_markers"]
    } == {
        ("admitted_source_deleted", om.SOURCE_GATE, "corpus-current-baseline"),
        ("admitted_source_deleted", om.SOURCE_HOST, "corpus-current-baseline"),
    }
    assert len(payload["receipts"]) == 1
    missing = payload["receipts"][0]
    assert missing["admitted"] is True
    assert missing["prefix_member"] is True
    assert missing["finalized"] is False
    assert missing["disposition"] == "loss_signal"
    assert missing["outcome"] == "CENSORED"
    retained = runner.load_lineage_checkpoint(
        lineage,
        coordinate_sha256=coordinate,
        contract_sha256=contract_hash,
    )
    assert len(retained) == 1
    assert retained[0].admitted and retained[0].prefix_member
    assert retained[0].outcome == "CENSORED"
    assert retained[0].finalized is False
    assert retained[0].boundary_evidence
    assert lineage.read_bytes() != checkpoint_before
