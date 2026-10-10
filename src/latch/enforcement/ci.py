"""Generic local/self-hosted A4 PR/CI consumer over immutable Git trees."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Mapping, Sequence

from latch.enforcement import core
from latch.gate import predicate_consumer
from latch.gate import predicate_snapshot


CI_ADAPTER_ID = "generic-pr-ci"
CI_ELIGIBILITY_VERSION = "a4-ci-eligibility-v1"
CI_ADAPTER_VERSION = f"a4-ci-v1.{CI_ELIGIBILITY_VERSION}"
CI_ELIGIBLE_PREFIXES = frozenset({"file", "glob"})
CI_UNSUPPORTED_PREFIXES = frozenset({"package", "import", "api"})
RUNNER_SCOPES = frozenset({"local", "self-hosted", "hosted"})
SNAPSHOT_KINDS = frozenset({"private", "synthetic"})
_SNAPSHOT_SOURCE_KINDS = {
    "sqlite-fileset-v1": "private",
    "token-file-v1": "synthetic",
}
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MAX_DIFF_BYTES = 2 * 1024 * 1024
_MAX_DIFF_PATHS = 100_000
_GIT_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class GitDiffEvidence:
    paths: tuple[str, ...]
    raw_digest: str
    raw_bytes: int
    candidate_count: int
    complete: bool
    reason_codes: tuple[str, ...]


class GitEvidenceError(RuntimeError):
    """Committed Git evidence was unavailable; details remain local."""


class _ArgumentParserError(ValueError):
    """CLI shape was invalid; raw arguments must not reach stderr."""


class _ReceiptArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _ArgumentParserError("invalid arguments")


def parse_git_diff_paths(raw: bytes | object) -> GitDiffEvidence:
    """Parse exact ``git diff-tree --name-only -z`` bytes without partial use."""
    if not isinstance(raw, bytes):
        return GitDiffEvidence((), hashlib.sha256(b"").hexdigest(), 0, 0, False, (
            "ci_evidence_invalid",
        ))
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) > _MAX_DIFF_BYTES:
        return GitDiffEvidence(
            (), digest, len(raw), 0, False, ("ci_evidence_too_large",)
        )
    if not raw:
        return GitDiffEvidence((), digest, 0, 0, True, ())
    if not raw.endswith(b"\0"):
        candidates = raw.count(b"\0") + 1
        return GitDiffEvidence(
            (), digest, len(raw), candidates, False, ("ci_evidence_truncated",)
        )
    encoded_paths = raw[:-1].split(b"\0")
    candidate_count = len(encoded_paths)
    if candidate_count > _MAX_DIFF_PATHS:
        return GitDiffEvidence(
            (),
            digest,
            len(raw),
            candidate_count,
            False,
            ("ci_evidence_too_many_paths",),
        )
    if any(not item for item in encoded_paths):
        return GitDiffEvidence(
            (), digest, len(raw), candidate_count, False, ("ci_evidence_malformed",)
        )
    try:
        decoded = [item.decode("utf-8", errors="strict") for item in encoded_paths]
    except UnicodeDecodeError:
        return GitDiffEvidence(
            (), digest, len(raw), candidate_count, False, ("ci_evidence_encoding",)
        )
    if any(_unsafe_project_path(path) for path in decoded):
        return GitDiffEvidence(
            (), digest, len(raw), candidate_count, False, ("ci_evidence_malformed",)
        )
    return GitDiffEvidence(
        tuple(sorted(set(decoded))),
        digest,
        len(raw),
        candidate_count,
        True,
        (),
    )


def check_ci_policy(
    *,
    snapshot_path: str | Path,
    repository_root: str | Path,
    repository_id: str,
    base_sha: str,
    head_sha: str,
    policy_domain_id: str,
    runner_scope: str,
    snapshot_kind: str,
) -> core.PolicyCheckResult:
    """Evaluate one immutable base..head subject with a fixed CI partition.

    ``runner_scope`` is an explicit support/config declaration, not remote-host
    attestation.  The privacy boundary is structural: this module has no fetch,
    upload, network, or check-publication path, and hosted/private is rejected
    before either filesystem coordinate is resolved.
    """
    action_stub = {"policy_domain_id": policy_domain_id}
    subject = {
        "kind": "git-range",
        "repository_id": repository_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    invalid_common = {
        "action": action_stub,
        "subject": subject,
        "adapter_id": CI_ADAPTER_ID,
        "adapter_version": CI_ADAPTER_VERSION,
        "mode": "ci-only",
        "effect": "side-effecting",
    }
    if runner_scope not in RUNNER_SCOPES:
        return core.invalid_policy_check("runner_scope_invalid", **invalid_common)
    if snapshot_kind not in SNAPSHOT_KINDS:
        return core.invalid_policy_check("snapshot_kind_invalid", **invalid_common)
    if runner_scope == "hosted" and snapshot_kind == "private":
        return core.invalid_policy_check(
            "private_snapshot_on_hosted_runner", **invalid_common
        )
    if not _immutable_sha(base_sha) or not _immutable_sha(head_sha):
        return core.invalid_policy_check(
            "immutable_git_sha_required", **invalid_common
        )

    loaded = predicate_snapshot.load_policy_snapshot(
        snapshot_path,
        policy_domain_id=policy_domain_id,
    )
    if loaded.snapshot is None:
        evaluation = predicate_consumer.failure_evaluation(
            policy_domain_id, loaded.reason_codes
        )
        return core.finalize_policy_evaluation(
            evaluation,
            **invalid_common,
        )
    snapshot = loaded.snapshot
    authenticated_snapshot_kind = _SNAPSHOT_SOURCE_KINDS.get(
        snapshot.freshness_source.get("kind")
    )
    if authenticated_snapshot_kind != snapshot_kind:
        evaluation = predicate_consumer.failure_evaluation(
            policy_domain_id,
            ("snapshot_kind_mismatch",),
            snapshot=snapshot,
        )
        return core.finalize_policy_evaluation(evaluation, **invalid_common)

    try:
        repo = Path(repository_root).resolve(strict=True)
        if not repo.is_dir():
            raise GitEvidenceError("repository root is not a directory")
        raw = _git_diff_tree_bytes(repo, base_sha, head_sha)
    except (GitEvidenceError, OSError, subprocess.SubprocessError):
        evidence = GitDiffEvidence(
            (),
            hashlib.sha256(b"").hexdigest(),
            0,
            0,
            False,
            ("ci_evidence_unavailable",),
        )
    else:
        evidence = parse_git_diff_paths(raw)

    eligible, residual_counts = _partition_checks(snapshot.binding_checks)
    filtered = replace(
        snapshot,
        binding_checks=eligible,
        binding_rows=len(eligible),
        binding_compiled=len(eligible),
        advisory_rows=snapshot.advisory_rows + sum(residual_counts.values()),
    )
    evidence_counts = {
        "ci_evidence:candidates": evidence.candidate_count,
        "ci_evidence:unique": len(evidence.paths),
        "ci_eligible:file": sum(check.prefix == "file" for check in eligible),
        "ci_eligible:glob": sum(check.prefix == "glob" for check in eligible),
        **{
            f"ci_unsupported:{prefix}": count
            for prefix, count in residual_counts.items()
        },
    }
    action = {
        "policy_domain_id": policy_domain_id,
        "project_root": str(repo) if "repo" in locals() else None,
        "cwd": str(repo) if "repo" in locals() else None,
        "tool_name": "ci.change-set",
        "proposed_file_paths": [],
        "diff_paths": list(evidence.paths),
        "staged_paths": [],
        "import_names": [],
        "api_names": [],
        "evidence_complete": evidence.complete,
        "evidence_provenance": ["git-diff-tree-z-v1"],
    }
    if not evidence.complete:
        evaluation = predicate_consumer.failure_evaluation(
            policy_domain_id,
            ("ci_evidence_incomplete",),
            snapshot=filtered,
        )
    elif not evidence.paths:
        evaluation = _no_change_evaluation(filtered)
    else:
        evaluation = predicate_consumer.evaluate_loaded_policy(filtered, action)
    return core.finalize_policy_evaluation(
        evaluation,
        action=action,
        subject=subject,
        adapter_id=CI_ADAPTER_ID,
        adapter_version=CI_ADAPTER_VERSION,
        mode="ci-only",
        effect="side-effecting",
        supplemental_advisory_counts=evidence_counts,
    )


def reference_main(argv: Sequence[str] | None = None) -> int:
    parser = _ReceiptArgumentParser(
        description="evaluate a private policy snapshot over an immutable Git range"
    )
    parser.add_argument("snapshot_path")
    parser.add_argument("repository_root")
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--policy-domain-id", required=True)
    parser.add_argument("--runner-scope", choices=sorted(RUNNER_SCOPES), required=True)
    parser.add_argument("--snapshot-kind", choices=sorted(SNAPSHOT_KINDS), required=True)
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except _ArgumentParserError:
        result = core.invalid_policy_check(
            "invalid_arguments",
            adapter_id=CI_ADAPTER_ID,
            adapter_version=CI_ADAPTER_VERSION,
            mode="ci-only",
            effect="side-effecting",
        )
    else:
        result = check_ci_policy(
            snapshot_path=args.snapshot_path,
            repository_root=args.repository_root,
            repository_id=args.repository_id,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            policy_domain_id=args.policy_domain_id,
            runner_scope=args.runner_scope,
            snapshot_kind=args.snapshot_kind,
        )
    json.dump(result.receipt, sys.stdout, ensure_ascii=False, sort_keys=True)
    sys.stdout.write("\n")
    return result.exit_code


def _partition_checks(checks):
    eligible = []
    residual: dict[str, int] = {}
    for check in checks:
        if check.prefix in CI_ELIGIBLE_PREFIXES:
            eligible.append(check)
        else:
            prefix = (
                check.prefix
                if check.prefix in CI_UNSUPPORTED_PREFIXES
                else "unsupported"
            )
            residual[prefix] = residual.get(prefix, 0) + 1
    return tuple(eligible), dict(sorted(residual.items()))


def _no_change_evaluation(
    snapshot: predicate_snapshot.LoadedPolicySnapshot,
) -> predicate_consumer.PolicyEvaluation:
    before = predicate_snapshot.check_loaded_snapshot_freshness(snapshot)
    if before:
        return predicate_consumer.failure_evaluation(
            snapshot.policy_domain_id, before, snapshot=snapshot
        )
    after = predicate_snapshot.check_loaded_snapshot_freshness(snapshot)
    if after:
        return predicate_consumer.failure_evaluation(
            snapshot.policy_domain_id, after, snapshot=snapshot
        )
    verdict = {
        "engine": "predicate-v1",
        "decision": "pass",
        "llm_calls": 0,
        "matches": [],
    }
    receipt = {
        "contract": predicate_consumer.RECEIPT_CONTRACT,
        "engine": "predicate-v1",
        "decision": "pass",
        "llm_calls": 0,
        "policy_domain_id": snapshot.policy_domain_id,
        "policy_digest": snapshot.digest,
        "freshness_token": snapshot.freshness_token,
        "binding_rows": snapshot.binding_rows,
        "binding_compiled": snapshot.binding_compiled,
        "advisory_rows": snapshot.advisory_rows,
        "uncompilable_rows": snapshot.uncompilable_rows,
        "matched_rejected_path_ids": [],
        "matched_node_ids": [],
        "reason_codes": [],
        "advisory_reason_counts": dict(snapshot.advisory_reason_counts),
    }
    return predicate_consumer.PolicyEvaluation(verdict=verdict, receipt=receipt)


def _git_diff_tree_bytes(repo: Path, base_sha: str, head_sha: str) -> bytes:
    environment = dict(os.environ)
    environment.update(GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
    for sha in (base_sha, head_sha):
        resolved = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{sha}^{{commit}}"],
            env=environment,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if resolved.returncode != 0:
            raise GitEvidenceError("commit object is unavailable")
        try:
            resolved_sha = resolved.stdout.decode("ascii", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise GitEvidenceError("commit resolution was malformed") from exc
        if resolved_sha != sha:
            raise GitEvidenceError("object does not resolve to the supplied commit id")
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff-tree",
            "-r",
            "--no-commit-id",
            "--name-only",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            base_sha,
            head_sha,
            "--",
        ],
        env=environment,
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        raise GitEvidenceError("git diff-tree failed")
    return proc.stdout


def _immutable_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA_RE.fullmatch(value) is not None


def _unsafe_project_path(value: str) -> bool:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        return True
    if "\x00" in value:
        return True
    return any(segment in {"", ".", ".."} for segment in value.split("/"))


__all__ = [
    "CI_ADAPTER_ID",
    "CI_ADAPTER_VERSION",
    "CI_ELIGIBILITY_VERSION",
    "CI_ELIGIBLE_PREFIXES",
    "GitDiffEvidence",
    "check_ci_policy",
    "parse_git_diff_paths",
    "reference_main",
]
