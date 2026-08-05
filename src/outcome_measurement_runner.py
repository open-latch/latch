"""Pinned, read-only production runner for outcome-measurement v2.6.

This module deliberately has no CLI, scheduler, MCP registration, installer
hook, or import-time execution.  It is the single integration seam for a
post-merge caller that already has the frozen manifest and both live canary
receipts.  Until that separate T0 activation, importing this module changes no
state and cannot start a measurement window.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable, Mapping, Sequence

import db
import outcome_evidence
import outcome_measurement
import project_proof


@dataclass(frozen=True)
class PinnedAuditRun:
    """One canonical state and its deterministic, envelope-verified report."""

    state: outcome_measurement.AuditState
    report: str


def _deployed_implementation_commit() -> str | None:
    """Return HEAD only for an exact, clean source checkout.

    The manifest commit must be independently tied to the running source, not
    merely repeated by two caller-controlled objects. Installed layouts without
    Git metadata deliberately fail closed until their installer supplies an
    equivalently immutable build identity in a future, separately reviewed
    activation change.
    """

    root = Path(__file__).resolve().parent.parent
    try:
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.stdout.strip():
            return None
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return commit if re.fullmatch(r"[0-9a-f]{40}", commit) else None


def run_pinned_audit(
    *,
    project_path: str | os.PathLike[str],
    source_roots: Mapping[str, Sequence[str | os.PathLike[str]]],
    config: outcome_measurement.MeasurementConfig,
    contract_bytes: bytes,
    capture: outcome_measurement.CapturePin,
    manifest: outcome_measurement.MeasurementManifest,
    fixture_bytes: Mapping[str, bytes],
    canaries: Iterable[outcome_measurement.CanaryEvidence],
    prior_capture: outcome_measurement.CapturePin | None = None,
    prior_receipts: Iterable[
        outcome_measurement.InvocationReceipt | outcome_measurement.ReceiptLineage
    ] = (),
) -> PinnedAuditRun:
    """Measure stable pinned roots and render only through the full audit gate.

    The vault identity is read from the selected project's existing database;
    raw key material and recoverable project paths never enter a receipt.  All
    evidence queries share one read-only SQLite snapshot.  A mismatch between
    that derived project proof and the configured target hard-invalidates the
    report rather than selecting another project or silently continuing.
    """

    project = os.fspath(project_path)
    if not project.strip():
        raise ValueError("an explicit project path is required")
    if config.require_fresh_snapshots is not True:
        raise ValueError("canonical audits require fresh post-drain snapshots")
    canary_rows = tuple(canaries)
    prior_rows = tuple(prior_receipts)
    deployed_commit = _deployed_implementation_commit()

    conn = db.connect_readonly(project)
    try:
        conn.execute("BEGIN")
        proof_context = project_proof.ProjectProofContext.from_vault_identity(
            conn._kb_vault_identity,
            key_epoch=config.key_epoch,
        )
        derived_target = proof_context.prove(project)
        target_status = project_proof.compare_project_proofs(
            derived_target,
            config.target_project_proof,
        )
        resolver = outcome_evidence.make_receipt_evidence_resolver(conn, project)
        state = outcome_measurement.measure(
            source_roots,
            config,
            prior_receipts=prior_rows,
            project_proof_context=proof_context,
            evidence_resolver=resolver,
        )
        runner_invalidations: set[str] = set()
        if target_status != project_proof.PROJECT_MATCH:
            runner_invalidations.add(
                f"configured_target_project_proof:{target_status}"
            )
        if deployed_commit is None:
            runner_invalidations.add("deployed_implementation_commit_unavailable")
        elif config.implementation_commit != deployed_commit:
            runner_invalidations.add("deployed_implementation_commit_mismatch")
        if runner_invalidations:
            state = replace(
                state,
                hard_invalidations=tuple(sorted({
                    *state.hard_invalidations,
                    *runner_invalidations,
                })),
            )
        report = outcome_measurement.audit_report(
            state,
            contract_bytes=contract_bytes,
            capture=capture,
            manifest=manifest,
            fixture_bytes=fixture_bytes,
            prior_capture=prior_capture,
            canaries=canary_rows,
        )
        return PinnedAuditRun(state=state, report=report)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
