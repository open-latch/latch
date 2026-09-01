"""Current-protocol canonical writer -> runner -> report baseline."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest
from filelock import FileLock

from latch.store import db
from latch.gate import gate
from latch.common import log_utils
from latch.evals import outcome_measurement as om
from latch.evals import outcome_measurement_cli as cli
from latch.evals import outcome_measurement_runner as runner
from latch.store import paths
from latch.proof import project_proof


UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent
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


def test_lineage_checkpoint_round_trips_structural_authority_exactly(
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
    mac_key = b"\x33" * 32
    runner.write_lineage_checkpoint(
        checkpoint,
        state,
        coordinate_sha256=coordinate,
        mac_key=mac_key,
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
        checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
    )
    assert loaded == (provisional, finalized)
    # The checkpoint is a vault-local private artifact (ruling 4562), so the
    # exact source coordinate persists: the evidence resolver joins prior rows
    # against snapshot maps keyed by real absolute paths (Latch 4528).
    assert "/private/structural-coordinate/gate.log" in encoded
    with pytest.raises(ValueError, match="schema or coordinate"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256="c" * 64, mac_key=mac_key
        )

    # A malformed row is only reachable once the file authenticates, so the
    # tampered copy has to be re-signed to exercise row validation at all.
    malformed_checkpoint = json.loads(encoded)
    malformed_checkpoint["receipts"][0]["lineage_order_key"] = "not-a-time"
    malformed_checkpoint["mac"] = runner._checkpoint_mac(
        malformed_checkpoint, mac_key
    )
    _write_json(checkpoint, malformed_checkpoint)
    with pytest.raises(ValueError, match="checkpoint row is invalid"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
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
    checkpoint_mac_key = runner.lineage_checkpoint_mac_key(
        str(project), key_epoch=EPOCH
    )
    prior = runner.load_lineage_checkpoint(
        lineage,
        coordinate_sha256=coordinate,
        mac_key=checkpoint_mac_key,
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
        mac_key=checkpoint_mac_key,
        contract_sha256=contract_hash,
    )
    assert len(retained) == 1
    assert retained[0].admitted and retained[0].prefix_member
    assert retained[0].outcome == "CENSORED"
    assert retained[0].finalized is False
    assert retained[0].boundary_evidence
    assert lineage.read_bytes() != checkpoint_before


def _corpus_baseline(
    tmp_path: Path,
    monkeypatch,
    *,
    contract_hash: str,
    implementation_commit: str,
    event_age: timedelta = timedelta(minutes=10),
    file_valued_s1: bool = False,
) -> dict[str, Path]:
    """Build one corpus-derived current-protocol audit envelope.

    Uses the real ``gate._log_invocation`` writer for S1 and a structural
    mutation of the pinned real Codex rollout for S2 (Latch 4114: external-format
    code is exercised only against corpus-derived fixtures).

    ``event_age`` places the evidence relative to the wall clock: the default
    keeps it inside ``FRESHNESS_SECONDS`` so receipts stay provisional, while
    an age past that bound lets run-time snapshots finalize them.
    """

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
    event_ts = now - event_age
    t0 = event_ts - timedelta(hours=1)
    cap = t0 + timedelta(days=21)
    host_root = tmp_path / "host"
    host_root.mkdir()
    measurement, host_bytes = _current_protocol_host(
        project=project,
        proof=proof,
        timestamp=event_ts,
    )
    event_iso = event_ts.isoformat(timespec="milliseconds").replace("+00:00", "Z")
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
    (host_root / "rollout-corpus-current.jsonl").write_bytes(host_bytes)
    s1_root = gate_path if file_valued_s1 else gate_root
    roots = {"S1": [str(s1_root)], "S2": [str(host_root)]}

    fixture_bytes = _fixture_bytes()
    fixture_hashes = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in fixture_bytes.items()
    }
    manifest = om.MeasurementManifest(
        contract_sha256=contract_hash,
        ratification_node_ids=om.RATIFICATION_NODE_IDS,
        implementation_commit=implementation_commit,
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        source_roots=roots,
        fixture_hashes=fixture_hashes,
        composite_sha256="",
    )
    manifest = replace(manifest, composite_sha256=manifest.expected_composite())
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
                "implementation_commit": implementation_commit,
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
                "measurement_protocol_version": (
                    manifest.measurement_protocol_version
                ),
                "source_roots": roots,
                "fixture_hashes": fixture_hashes,
                "composite_sha256": manifest.composite_sha256,
            },
            "fixture_paths": {name: str(FIXTURES / name) for name in fixture_bytes},
            # Deterministic test receipts over corpus-derived current shapes.
            # They are not represented as completed live canaries.
            "canaries": [
                {
                    "host": host,
                    "nonce": f"corpus-derived-{host}",
                    "tool_result_seen": True,
                    "gate_log_seen": True,
                    "host_record_seen": True,
                    "dual_source_joined": True,
                    "runtime_version": RUNTIME,
                    "measurement_protocol_version": (
                        om.MEASUREMENT_PROTOCOL_VERSION
                    ),
                    "key_epoch": EPOCH,
                    "project_proof": proof,
                }
                for host in ("codex", "claude")
            ],
        },
    )
    return {"project": project, "envelope": envelope, "gate_path": gate_path}


def _clean_git_checkout(tmp_path: Path) -> tuple[Path, str]:
    """Materialize the runtime in a fresh, clean git checkout.

    ``runner._deployed_implementation_commit`` deliberately fails closed unless
    it can tie HEAD to an exact, clean source tree, so reachability can only be
    proven from a real checkout rather than by patching the function out.
    """

    root = tmp_path / "deployed"
    root.mkdir()
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPO_ROOT / "src", root / "src", ignore=ignore)
    shutil.copytree(REPO_ROOT / "artifacts", root / "artifacts", ignore=ignore)
    shutil.copytree(REPO_ROOT / "bin", root / "bin", ignore=ignore)
    for name in ("VERSION", "KB_SCHEMA_VERSION", "WIRING_VERSION"):
        shutil.copy2(REPO_ROOT / name, root / name)
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "latch-test",
        "GIT_AUTHOR_EMAIL": "latch-test@example.invalid",
        "GIT_COMMITTER_NAME": "latch-test",
        "GIT_COMMITTER_EMAIL": "latch-test@example.invalid",
    }
    for command in (
        ["git", "init", "--quiet"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "pinned runtime"],
    ):
        subprocess.run(command, cwd=root, check=True, env=env, capture_output=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()
    assert status == "", f"deployed checkout must be clean, saw: {status!r}"
    return root, head


def test_packaged_contract_artifact_is_the_frozen_capture() -> None:
    """The frozen contract ships in-tree and hashes to the pinned capture.

    Latch 4427 finding 2: no file in the tree hashed to ``CONTRACT_SHA256``, so
    ``contract_hash_mismatch`` was unavoidable and no canonical report could
    ever be valid.
    """

    packaged = REPO_ROOT / runner.PACKAGED_CONTRACT_RELPATH
    assert packaged.is_file(), f"frozen contract must ship at {packaged}"
    assert hashlib.sha256(packaged.read_bytes()).hexdigest() == om.CONTRACT_SHA256
    assert runner.packaged_contract_bytes() == packaged.read_bytes()


def test_canonical_report_is_reachable_without_patching_pinned_constants(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A non-invalidated canonical report from an unpatched clean checkout.

    Neither ``om.CONTRACT_SHA256`` nor ``runner._deployed_implementation_commit``
    is patched: the contract bytes are the shipped artifact and the commit is a
    real ``git rev-parse HEAD`` over a real clean tree.
    """

    deployed, head = _clean_git_checkout(tmp_path)
    contract = deployed / runner.PACKAGED_CONTRACT_RELPATH
    contract_hash = hashlib.sha256(contract.read_bytes()).hexdigest()
    assert contract_hash == om.CONTRACT_SHA256

    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=contract_hash,
        implementation_commit=head,
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(deployed / "src" / "latch" / "evals" / "outcome_measurement_cli.py"),
            "--project", str(built["project"]),
            "--envelope", str(built["envelope"]),
            "--contract", str(contract),
            "--lineage", str(tmp_path / "lineage.json"),
            "--report", str(tmp_path / "report.json"),
            "--initialize-empty-lineage",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    receipt = json.loads(completed.stdout)
    assert receipt["ok"] is True
    assert receipt["invalidated"] is False
    assert receipt["invalidation_reasons"] == []
    assert receipt["implementation_commit"] == head

    payload = json.loads((tmp_path / "report.json").read_text())
    assert payload["oracles"]["invalidated"] is False
    assert "contract_hash_mismatch" not in payload["oracles"]["invalidation_reasons"]
    assert (
        "deployed_implementation_commit_unavailable"
        not in payload["oracles"]["invalidation_reasons"]
    )
    assert payload["contract"]["sha256"] == om.CONTRACT_SHA256


def test_omitting_contract_uses_packaged_default_end_to_end(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Latch 4562 item 4: the packaged contract default is production-real.

    A run that omits ``--contract`` must resolve the shipped
    ``artifacts/outcome-measurement/contract-v2.6.md``, hash it to the pinned
    capture, and commit a non-invalidated canonical report.
    """

    deployed, head = _clean_git_checkout(tmp_path)
    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=head,
    )
    report = tmp_path / "report.json"
    completed = subprocess.run(
        [
            "bash",
            str(deployed / "bin" / "run_latch_outcome_audit.sh"),
            "--project", str(built["project"]),
            "--envelope", str(built["envelope"]),
            "--lineage", str(tmp_path / "lineage.json"),
            "--report", str(report),
            "--initialize-empty-lineage",
        ],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "LATCH_HOME": str(deployed),
            "LATCH_PYTHON": sys.executable,
        },
    )
    assert completed.returncode == 0, (
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    payload = json.loads(report.read_text())
    assert payload["oracles"]["invalidated"] is False
    assert payload["contract"]["sha256"] == om.CONTRACT_SHA256


def test_three_consecutive_real_entrypoint_runs_with_finalized_prior_stay_clean(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A finalized prior over byte-identical evidence must replay clean.

    Latch 4562 item 1 acceptance (4528 blocker 1): three consecutive runs of
    the real ``bin/run_latch_outcome_audit.sh`` entrypoint over byte-identical
    evidence with a FINALIZED prior all produce a clean canonical report.
    ``_snapshot_qualifies`` finalizes anything older than ``FRESHNESS_SECONDS``,
    so a production rerun is essentially always in this class.
    """

    deployed, head = _clean_git_checkout(tmp_path)
    contract = deployed / runner.PACKAGED_CONTRACT_RELPATH
    contract_hash = hashlib.sha256(contract.read_bytes()).hexdigest()
    assert contract_hash == om.CONTRACT_SHA256

    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=contract_hash,
        implementation_commit=head,
        event_age=timedelta(seconds=om.FRESHNESS_SECONDS * 4),
    )
    project = built["project"]
    lineage = tmp_path / "lineage.json"
    report = tmp_path / "report.json"

    gate_root = paths.project_dir(str(project))
    evidence_files = sorted(
        [*gate_root.glob("gate-*.log"), *(tmp_path / "host").glob("*.jsonl")]
    )
    assert len(evidence_files) == 2, "corpus baseline must yield S1+S2 evidence"

    def evidence_digest() -> list[str]:
        return [
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in evidence_files
        ]

    frozen_evidence = evidence_digest()
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "LATCH_HOME": str(deployed),
        "LATCH_PYTHON": sys.executable,
    }
    base_argv = [
        "bash",
        str(deployed / "bin" / "run_latch_outcome_audit.sh"),
        "--project", str(project),
        "--envelope", str(built["envelope"]),
        "--contract", str(contract),
        "--lineage", str(lineage),
        "--report", str(report),
    ]

    loaded_envelope = cli.load_envelope(built["envelope"])
    coordinate = runner.lineage_checkpoint_coordinate(
        loaded_envelope["config"], loaded_envelope["manifest"]
    )
    mac_key = runner.lineage_checkpoint_mac_key(str(project), key_epoch=EPOCH)

    for attempt in range(3):
        argv = list(base_argv)
        if attempt == 0:
            argv.append("--initialize-empty-lineage")
        completed = subprocess.run(argv, capture_output=True, text=True, env=env)
        assert completed.returncode == 0, (
            f"run {attempt + 1}: stdout={completed.stdout!r} "
            f"stderr={completed.stderr!r}"
        )
        payload = json.loads(report.read_text())
        assert payload["oracles"]["invalidated"] is False, f"run {attempt + 1}"
        assert payload["loss_markers"] == [], (
            f"run {attempt + 1} must be clean, saw {payload['loss_markers']!r}"
        )
        assert payload["oracles"]["o2"] == "pass", (
            f"run {attempt + 1}: {payload['oracles']!r}"
        )
        prior = runner.load_lineage_checkpoint(
            lineage,
            coordinate_sha256=coordinate,
            mac_key=mac_key,
            contract_sha256=contract_hash,
        )
        assert prior and prior[0].finalized is True, (
            f"run {attempt + 1} must persist the finalized class this "
            "regression exists for"
        )
        assert evidence_digest() == frozen_evidence, (
            f"run {attempt + 1} mutated the measured evidence"
        )


def _mac_state(tmp_path: Path):
    """One admitted receipt whose checkpoint authority is worth forging."""

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
        nonce="forgeable",
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
    receipt = om.InvocationReceipt(
        nonce="forgeable",
        measurement_protocol_version=om.MEASUREMENT_PROTOCOL_VERSION,
        observations=(observation,),
        disposition="confirmatory",
        admitted=True,
        lineage_order_key=order,
        fresh_ts=order,
        session_id="opaque-session-coordinate",
        verdict="PROCEED",
        outcome="AMBIGUOUS",
        window_start=order,
        window_end=order + timedelta(minutes=30),
        prefix_member=True,
        boundary_evidence=(observation,),
    )
    return om.AuditState(
        config=config,
        receipts=(receipt,),
        loss_markers=(),
        source_health=(),
        snapshots=(),
        gate_rows=(),
    )


def test_lineage_checkpoint_is_authenticated_against_on_disk_forgery(
    tmp_path: Path,
) -> None:
    """Latch 4427 finding 3(a): N, O1 and O3 were forgeable from disk.

    The checkpoint's only "binding" values were plaintext fields copied from the
    file being checked, so editing an outcome or cloning receipts under fake
    nonces was accepted and re-persisted forward.
    """

    state = _mac_state(tmp_path)
    checkpoint = tmp_path / "lineage.json"
    coordinate = "b" * 64
    mac_key = b"\x11" * 32
    other_key = b"\x22" * 32

    runner.write_lineage_checkpoint(
        checkpoint, state, coordinate_sha256=coordinate, mac_key=mac_key
    )
    loaded = runner.load_lineage_checkpoint(
        checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
    )
    assert loaded == tuple(state.receipts)
    assert loaded[0].outcome == "AMBIGUOUS"

    honest = json.loads(checkpoint.read_text())
    assert honest["mac"], "checkpoint must carry a MAC"

    # (a) flip one outcome string: the reproduced ambiguous_rate 100.0 -> 0.0
    flipped = json.loads(json.dumps(honest))
    flipped["receipts"][0]["outcome"] = "ACCEPTED"
    _write_json(checkpoint, flipped)
    with pytest.raises(ValueError, match="authentication"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
        )

    # (b) clone one real receipt under fake nonces to forge N / O1 / O3
    forged = json.loads(json.dumps(honest))
    template = forged["receipts"][0]
    forged["receipts"] = [
        {**json.loads(json.dumps(template)), "nonce": f"forged-{index}"}
        for index in range(30)
    ]
    _write_json(checkpoint, forged)
    with pytest.raises(ValueError, match="authentication"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
        )

    # (c) stripping the MAC must fail closed, never degrade to unauthenticated
    stripped = json.loads(json.dumps(honest))
    stripped.pop("mac")
    _write_json(checkpoint, stripped)
    with pytest.raises(ValueError, match="authentication|fields are invalid"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
        )

    # (d) a checkpoint authenticated under a different vault key is not ours
    _write_json(checkpoint, honest)
    with pytest.raises(ValueError, match="authentication"):
        runner.load_lineage_checkpoint(
            checkpoint, coordinate_sha256=coordinate, mac_key=other_key
        )

    # the honest checkpoint still loads under the right key
    assert runner.load_lineage_checkpoint(
        checkpoint, coordinate_sha256=coordinate, mac_key=mac_key
    ) == tuple(state.receipts)


def test_root_containment_rejects_case_variant_spelling(tmp_path: Path) -> None:
    """Latch 4427 finding 3(c): the audit wrote its report into the corpus.

    ``resolve()`` does not case-fold, so on APFS/NTFS a report under
    ``.../HOST`` passed a byte-exact prefix check against the S2 root
    ``.../host`` and the next run parsed it back as S2 evidence.
    """

    root = tmp_path / "host"
    root.mkdir()
    assert cli._inside_root(root / "rollout-audit.jsonl", root) is True

    variant = tmp_path / "HOST" / "rollout-audit.jsonl"
    case_insensitive = (tmp_path / "HOST").is_dir()
    if case_insensitive:
        assert cli._inside_root(variant, root) is True
    else:
        # On a case-sensitive filesystem .../HOST genuinely is a different
        # directory, so containment is correctly False.
        assert cli._inside_root(variant, root) is False

    outside = tmp_path / "elsewhere" / "report.json"
    assert cli._inside_root(outside, root) is False


def test_file_valued_source_root_contains_its_aliases(tmp_path: Path) -> None:
    """Latch 4562 item 5: a file-valued root contains exactly itself.

    ``_inside_root`` short-circuited False for any regular-file root before
    identity was ever consulted, so an aliased spelling of a measured gate log
    passed output validation and the audit destroyed the evidence it had just
    measured (Latch 4528 defect i). Hardlinks give the identity check
    deterministic coverage on case-sensitive filesystems too.
    """

    root = tmp_path / "gate-2026-08-03.log"
    root.write_bytes(b"s1 evidence\n")
    assert cli._inside_root(root, root) is True

    alias = tmp_path / "report-alias.json"
    os.link(root, alias)
    assert cli._inside_root(alias, root) is True

    sibling = tmp_path / "report.json"
    sibling.write_bytes(b"{}")
    assert cli._inside_root(sibling, root) is False
    assert cli._inside_root(tmp_path / "unwritten.json", root) is False

    variant = tmp_path / "GATE-2026-08-03.LOG"
    if variant.is_file():
        # The filesystem case-folds: the variant names the same entry.
        assert cli._inside_root(variant, root) is True
    else:
        assert cli._inside_root(variant, root) is False


def test_report_aliasing_a_file_valued_source_root_is_refused(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Latch 4562 item 5 acceptance: the audit must not destroy its evidence.

    File-valued source roots are first-class in discovery. A report path that
    aliases the measured gate log (hardlink everywhere; case-variant where the
    filesystem folds) previously passed validation, and ``os.replace``
    committed the report over the S1 evidence with exit 0 (Latch 4528: the
    real run overwrote the S1 gate log).
    """

    contract = runner.packaged_contract_path()
    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=COMMIT,
        file_valued_s1=True,
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: COMMIT)
    gate_path = built["gate_path"]
    evidence = gate_path.read_bytes()

    aliases = [tmp_path / "report-hardlink.json"]
    os.link(gate_path, aliases[0])
    variant = gate_path.with_name(gate_path.name.upper())
    if variant.is_file():
        aliases.append(variant)

    for alias in aliases:
        argv = [
            "--project", str(built["project"]),
            "--envelope", str(built["envelope"]),
            "--contract", str(contract),
            "--lineage", str(tmp_path / "lineage.json"),
            "--report", str(alias),
            "--initialize-empty-lineage",
        ]
        assert cli.main(argv) == 2, alias
        failure = json.loads(capsys.readouterr().err)
        assert "inside a measured root" in failure["error"], alias
        assert gate_path.read_bytes() == evidence, alias


def test_omitted_lineage_defaults_to_a_private_vault_local_checkpoint(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Ruling 4562: the checkpoint's default home is inside the vault.

    The checkpoint carries raw source coordinates, so its sanctioned location
    is a 0o600 file in the project's vault directory — already private and
    gitignored — rather than an operator-invented path. The vault is also the
    measured S1 root, so exactly this default (an inert name discovery can
    never read as evidence) is exempt from the inside-a-measured-root refusal.
    """

    contract = runner.packaged_contract_path()
    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=COMMIT,
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: COMMIT)
    report = tmp_path / "report.json"
    argv = [
        "--project", str(built["project"]),
        "--envelope", str(built["envelope"]),
        "--contract", str(contract),
        "--report", str(report),
        "--initialize-empty-lineage",
    ]
    assert cli.main(argv) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["lineage_updated"] is True
    default_lineage = cli.default_lineage_path(str(built["project"]))
    assert default_lineage.parent == paths.project_dir(str(built["project"]))
    assert default_lineage.is_file()
    assert default_lineage.stat().st_mode & 0o777 == 0o600

    # Any other vault-internal spelling keeps the blanket refusal.
    inside = paths.project_dir(str(built["project"])) / "elsewhere.json"
    assert cli.main(argv[:-1] + ["--lineage", str(inside)]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert "inside a measured root" in failure["error"]

    # The exemption is keyed on the sanctioned spelling, not file identity:
    # a hardlink of the checkpoint under an evidence-shaped name would let
    # the audit commit checkpoint JSON into its own S1 corpus.
    evidence_shaped = default_lineage.parent / "gate-2099-01-01.log"
    os.link(default_lineage, evidence_shaped)
    assert cli.main(argv[:-1] + ["--lineage", str(evidence_shaped)]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert "inside a measured root" in failure["error"]


def test_report_cannot_destroy_the_vault_lineage_checkpoint(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The default checkpoint is protected authority, not a writable sink.

    With a file-valued S1 root the vault directory is not itself a measured
    root, so containment alone never protected the checkpoint: a report aimed
    at the well-known default would destroy the admission history and leave
    every later default-lineage run failing closed.
    """

    contract = runner.packaged_contract_path()
    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=COMMIT,
        file_valued_s1=True,
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: COMMIT)
    base = [
        "--project", str(built["project"]),
        "--envelope", str(built["envelope"]),
        "--contract", str(contract),
    ]
    assert cli.main(base + [
        "--report", str(tmp_path / "report.json"),
        "--initialize-empty-lineage",
    ]) == 0
    capsys.readouterr()
    default_lineage = cli.default_lineage_path(str(built["project"]))
    checkpoint_bytes = default_lineage.read_bytes()

    assert cli.main(base + [
        "--report", str(default_lineage),
        "--lineage", str(tmp_path / "other-lineage.json"),
        "--initialize-empty-lineage",
    ]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert "aliases the vault lineage checkpoint" in failure["error"]
    assert default_lineage.read_bytes() == checkpoint_bytes


def test_same_path_does_not_fail_open_on_undecidable_errors(
    tmp_path: Path, monkeypatch
) -> None:
    """An undecidable aliasing check must never read as 'safe to write here'."""

    left = tmp_path / "a"
    right = tmp_path / "b"
    left.write_text("a")
    right.write_text("b")

    def deny(*_args, **_kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(os.path, "samefile", deny)
    with pytest.raises(ValueError, match="same file"):
        cli._same_path(left, right)


def test_initialize_empty_lineage_is_keyed_on_the_lineage_path(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Latch 4427 finding 3(b) and the half-committed-run deadlock.

    The flag guarded ``args.report.exists()``, a caller-supplied path, so it
    refused the one legitimate recovery (report committed, lineage write failed)
    while leaving the reset itself reachable through a second fresh path.
    """

    contract = runner.packaged_contract_path()
    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=COMMIT,
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: COMMIT)
    lineage = tmp_path / "lineage.json"
    report = tmp_path / "report.json"
    argv = [
        "--project", str(built["project"]),
        "--envelope", str(built["envelope"]),
        "--contract", str(contract),
        "--lineage", str(lineage),
        "--report", str(report),
        "--initialize-empty-lineage",
    ]

    assert cli.main(argv) == 0
    capsys.readouterr()
    assert lineage.is_file() and report.is_file()

    # An existing checkpoint may not be re-initialized away.
    assert cli.main(argv) == 2
    assert "lineage checkpoint exists" in json.loads(capsys.readouterr().err)["error"]

    # A committed report with no checkpoint is the recoverable half-committed
    # state, and must be runnable rather than permanently deadlocked.
    lineage.unlink()
    assert report.is_file()
    assert cli.main(argv) == 0
    capsys.readouterr()
    assert lineage.is_file()


def test_failed_lineage_write_rolls_back_the_committed_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A half-committed pair must not leave the window unrunnable."""

    contract = runner.packaged_contract_path()
    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=COMMIT,
    )
    monkeypatch.setattr(runner, "_deployed_implementation_commit", lambda: COMMIT)
    lineage = tmp_path / "lineage.json"
    report = tmp_path / "report.json"
    argv = [
        "--project", str(built["project"]),
        "--envelope", str(built["envelope"]),
        "--contract", str(contract),
        "--lineage", str(lineage),
        "--report", str(report),
        "--initialize-empty-lineage",
    ]

    def fail_lineage_write(*_args, **_kwargs):
        raise OSError("lineage sink unavailable")

    monkeypatch.setattr(runner, "write_lineage_checkpoint", fail_lineage_write)
    assert cli.main(argv) == 2
    capsys.readouterr()
    assert not lineage.exists()
    assert not report.exists(), "a report must not survive a failed lineage commit"


def test_sqlite_errors_are_reported_as_a_failed_audit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A database error must not surface as a raw traceback and exit 1.

    Exit 1 is the code for a validly invalidated audit, so an escaping
    ``sqlite3.Error`` was indistinguishable from a real measurement outcome.
    """

    built = _corpus_baseline(
        tmp_path,
        monkeypatch,
        contract_hash=om.CONTRACT_SHA256,
        implementation_commit=COMMIT,
    )

    def fail_mac_key(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runner, "lineage_checkpoint_mac_key", fail_mac_key)
    assert cli.main([
        "--project", str(built["project"]),
        "--envelope", str(built["envelope"]),
        "--contract", str(runner.packaged_contract_path()),
        "--lineage", str(tmp_path / "lineage.json"),
        "--report", str(tmp_path / "report.json"),
        "--initialize-empty-lineage",
    ]) == 2
    failure = json.loads(capsys.readouterr().err)
    assert failure["ok"] is False
    assert "database is locked" in failure["error"]


def test_checkpoint_persists_exact_coordinates_and_rewrites_stably(
    tmp_path: Path,
) -> None:
    """The vault-local checkpoint stores the real coordinate, stable on rewrite.

    Ruling 4562 reverted path tokenization: the checkpoint never leaves the
    vault, and the evidence resolver joins prior rows against snapshot maps
    keyed by real absolute paths (Latch 4528 blocker 1). A load -> write cycle
    must be byte-stable — the tokenized design re-derived tokens from
    already-tokenized values on every rewrite.
    """

    state = _mac_state(tmp_path)
    checkpoint = tmp_path / "lineage.json"
    mac_key = b"\x44" * 32
    runner.write_lineage_checkpoint(
        checkpoint, state, coordinate_sha256="b" * 64, mac_key=mac_key
    )
    encoded = checkpoint.read_bytes()
    loaded = runner.load_lineage_checkpoint(
        checkpoint, coordinate_sha256="b" * 64, mac_key=mac_key
    )
    assert loaded[0].observations[0].file == (
        "/private/structural-coordinate/gate.log"
    )
    rewritten = tmp_path / "lineage-rewritten.json"
    runner.write_lineage_checkpoint(
        rewritten,
        replace(state, receipts=loaded),
        coordinate_sha256="b" * 64,
        mac_key=mac_key,
    )
    assert rewritten.read_bytes() == encoded


def test_private_checkpoint_parents_are_created_at_0o700(tmp_path: Path) -> None:
    """``mkdir(parents=True, mode=...)`` applies the mode to the leaf only."""

    state = _mac_state(tmp_path)
    nested = tmp_path / "outer" / "inner" / "lineage.json"
    runner.write_lineage_checkpoint(
        nested, state, coordinate_sha256="b" * 64, mac_key=b"\x55" * 32
    )
    assert (tmp_path / "outer").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "outer" / "inner").stat().st_mode & 0o777 == 0o700
    assert nested.stat().st_mode & 0o777 == 0o600
