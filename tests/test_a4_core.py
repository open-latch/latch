"""A4-C2 shared policy-check core acceptance contract."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import subprocess
import sys


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_CLI = _ROOT / "bin" / "latch_policy_check.py"


def _module(name: str):
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(_SRC))


@dataclass(frozen=True)
class SyntheticProjection:
    engine: str
    policy_domain_id: str
    binding_rows: tuple[dict[str, object], ...]
    advisory_rows: tuple[dict[str, object], ...]
    reason_counts: dict[str, int]
    freshness_token: str


def _row(predicate: str = "file:src/private-policy-target.py") -> dict[str, object]:
    return {
        "rejected_path_id": 41,
        "node_id": 741,
        "option": "PRIVATE_POLICY_OPTION_SENTINEL",
        "reason": "PRIVATE_POLICY_REASON_SENTINEL",
        "ratifier": "synthetic-founder",
        "decided_at": "2026-09-02T00:00:00Z",
        "scope_predicate": predicate,
        "source": "declared",
        "policy_domain_id": "synthetic-a4-domain",
        "owner_kind": "decision",
        "owner_status": "canonical",
        "owner_updated_at": "2026-09-02T00:00:00Z",
        "latest_ratification_id": 941,
        "latest_ratification_action": "ratify",
        "latest_ratification_ratifier": "synthetic-founder",
        "latest_ratification_decided_at": "2026-09-02T00:00:00Z",
        "latest_ratification_source": "declared",
        "superseder_ids": (),
        "reconciler_ids": (),
        "authority_basis": "synthetic-ratified-declared",
        "classification": "binding",
        "reason_codes": (),
    }


def _projection() -> SyntheticProjection:
    return SyntheticProjection(
        engine="predicate-policy-projection-v1",
        policy_domain_id="synthetic-a4-domain",
        binding_rows=(_row(),),
        advisory_rows=(),
        reason_counts={},
        freshness_token="synthetic-a4-generation",
    )


def _publish(tmp_path: Path):
    snapshot = _module("latch.gate.predicate_snapshot")
    token = tmp_path / "source-generation.txt"
    token.write_text("generation-one\n", encoding="utf-8")
    private_dir = tmp_path / "private-runtime"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    document = snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-a4-domain",
        projector=_projection,
        freshness_token_path=token,
    )
    project_root = tmp_path / "private-project-root"
    project_root.mkdir()
    return snapshot, target, token, document, project_root


def _action(
    project_root: Path,
    *,
    path: str,
    complete: bool = True,
    tool_name: str = "synthetic.write",
):
    return {
        "policy_domain_id": "synthetic-a4-domain",
        "project_root": str(project_root),
        "cwd": str(project_root),
        "tool_name": tool_name,
        "command_text": "PRIVATE_ACTION_TEXT_SENTINEL",
        "proposed_file_paths": [path],
        "diff_paths": [],
        "staged_paths": [],
        "import_names": [],
        "api_names": [],
        "evidence_complete": complete,
        "evidence_provenance": ["synthetic-a4-core"],
    }


def _host_subject(**updates):
    subject = {
        "kind": "host-action",
        "repository_id": "synthetic-repository",
        "host_id": "codex",
        "session_id": "session-a",
        "action_id": "action-a",
    }
    subject.update(updates)
    return subject


def _request(action, **updates):
    request = {
        "action": action,
        "subject": _host_subject(),
        "adapter": {"id": "shared-test-adapter", "version": "1.0.0"},
        "mode": "enforce",
        "effect": "side-effecting",
    }
    request.update(updates)
    return request


def _run_cli(snapshot_path: Path, request: object):
    return subprocess.run(
        [sys.executable, "-I", str(_CLI), str(snapshot_path)],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )


def test_exit_outcomes_distinct_pass_block_flag_invalid(tmp_path):
    _, target, _, _, project_root = _publish(tmp_path)

    pass_proc = _run_cli(
        target,
        _request(_action(project_root, path="src/safe.py")),
    )
    block_proc = _run_cli(
        target,
        _request(_action(project_root, path="src/private-policy-target.py")),
    )
    flag_proc = _run_cli(
        target,
        _request(_action(project_root, path="src/safe.py", complete=False)),
    )
    invalid_proc = _run_cli(target, _request("not-an-action-object"))

    receipts = [
        json.loads(proc.stdout)
        for proc in (pass_proc, block_proc, flag_proc, invalid_proc)
    ]
    assert [receipt["outcome"] for receipt in receipts] == [
        "pass",
        "block",
        "flag",
        "invalid",
    ]
    exit_codes = [proc.returncode for proc in (pass_proc, block_proc, flag_proc, invalid_proc)]
    assert len(set(exit_codes)) == 4
    assert exit_codes[0] == 0
    assert all(code != 0 for code in exit_codes[1:])
    assert receipts[-1]["decision"] == receipts[-2]["decision"] == "flag"
    assert receipts[-1]["denied"] is receipts[-2]["denied"] is True
    assert all(proc.stderr == "" for proc in (pass_proc, block_proc, flag_proc, invalid_proc))


def test_flag_denies_side_effects_and_is_nonsuccess_in_ci(tmp_path):
    core = _module("latch.enforcement.core")
    _, target, _, _, project_root = _publish(tmp_path)
    action = _action(project_root, path="src/safe.py", complete=False)
    common = {
        "subject": _host_subject(),
        "adapter_id": "shared-test-adapter",
        "adapter_version": "1.0.0",
        "effect": "side-effecting",
    }

    enforced = core.check_policy(target, action, mode="enforce", **common)
    ci = core.check_policy(target, action, mode="ci-only", **common)
    observed = core.check_policy(target, action, mode="observe", **common)
    observed_invalid = core.check_policy(
        target,
        "not-an-action-object",
        mode="observe",
        **common,
    )

    assert enforced.outcome == ci.outcome == observed.outcome == "flag"
    assert enforced.denied is ci.denied is True
    assert enforced.exit_code != 0
    assert ci.exit_code != 0
    assert observed.denied is False
    assert observed.exit_code == 0
    assert observed_invalid.outcome == "invalid"
    assert observed_invalid.decision == "flag"
    assert observed_invalid.denied is False
    assert observed_invalid.exit_code == core.INVALID_EXIT


def test_receipt_binds_immutable_subject_and_stays_redacted(tmp_path):
    core = _module("latch.enforcement.core")
    _, target, _, document, project_root = _publish(tmp_path)
    action = _action(project_root, path="src/private-policy-target.py")
    common = {
        "adapter_id": "shared-test-adapter",
        "adapter_version": "1.0.0",
        "mode": "enforce",
        "effect": "side-effecting",
    }

    first = core.check_policy(target, action, subject=_host_subject(), **common)
    repeated = core.check_policy(target, action, subject=_host_subject(), **common)
    changed_action = core.check_policy(
        target,
        action,
        subject=_host_subject(action_id="action-b"),
        **common,
    )
    ci_subject = {
        "kind": "git-range",
        "repository_id": "synthetic-repository",
        "base_sha": "1" * 40,
        "head_sha": "2" * 40,
    }
    ci_bound = core.check_policy(
        target,
        action,
        subject=ci_subject,
        **{**common, "mode": "ci-only"},
    )

    assert first.receipt == repeated.receipt
    assert first.receipt["contract"] == "latch-policy-check-receipt-v1"
    assert first.receipt["engine"] == "predicate-v1"
    assert first.receipt["adapter"] == {
        "id": "shared-test-adapter",
        "version": "1.0.0",
    }
    assert first.receipt["policy"] == {
        "domain_id": "synthetic-a4-domain",
        "snapshot_digest": document["digest"],
        "freshness_token": "synthetic-a4-generation",
    }
    assert first.receipt["subject"] == _host_subject()
    assert first.receipt["subject_digest"] != changed_action.receipt["subject_digest"]
    assert ci_bound.receipt["subject"] == ci_subject
    assert ci_bound.receipt["subject_digest"] != first.receipt["subject_digest"]
    assert len(first.receipt["receipt_digest"]) == 64

    serialized = json.dumps(
        [first.receipt, ci_bound.receipt], sort_keys=True, separators=(",", ":")
    )
    for private_value in (
        "PRIVATE_POLICY_OPTION_SENTINEL",
        "PRIVATE_POLICY_REASON_SENTINEL",
        "file:src/private-policy-target.py",
        "PRIVATE_ACTION_TEXT_SENTINEL",
        "src/private-policy-target.py",
        str(project_root),
        str(target),
    ):
        assert private_value not in serialized


def test_read_only_inspection_exempt_and_post_denial_refresh_path(
    tmp_path,
    monkeypatch,
):
    core = _module("latch.enforcement.core")
    snapshot, target, token, _, project_root = _publish(tmp_path)
    action = _action(project_root, path="src/safe.py")
    inspection = {
        "policy_domain_id": "synthetic-a4-domain",
        "tool_name": "latch.policy.inspect",
    }
    common = {
        "subject": _host_subject(),
        "adapter_id": "shared-test-adapter",
        "adapter_version": "1.0.0",
        "mode": "enforce",
    }

    def must_not_evaluate(*_args, **_kwargs):
        raise AssertionError("read-only inspection reached the policy evaluator")

    original_evaluate = core.predicate_consumer.evaluate_policy
    monkeypatch.setattr(core.predicate_consumer, "evaluate_policy", must_not_evaluate)
    exempt = core.check_policy(
        target,
        inspection,
        effect="read-only",
        **common,
    )
    assert exempt.outcome == "pass"
    assert exempt.exempt is True
    assert exempt.denied is False
    assert exempt.exit_code == 0

    mislabeled = core.check_policy(
        target,
        action,
        effect="read-only",
        **common,
    )
    assert mislabeled.outcome == "invalid"
    assert mislabeled.denied is True
    assert mislabeled.exit_code == core.INVALID_EXIT
    assert mislabeled.receipt["reason_codes"] == ["read_only_effect_mismatch"]
    monkeypatch.setattr(core.predicate_consumer, "evaluate_policy", original_evaluate)

    token.write_text("generation-two\n", encoding="utf-8")
    denied = core.check_policy(
        target,
        action,
        effect="side-effecting",
        **common,
    )
    assert denied.outcome == "flag"
    assert denied.denied is True
    assert "source_changed" in denied.receipt["reason_codes"]

    snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-a4-domain",
        projector=_projection,
        freshness_token_path=token,
    )
    refreshed = core.recheck_after_snapshot_refresh(
        denied.receipt,
        target,
        action,
        effect="side-effecting",
        **common,
    )
    assert refreshed.outcome == "pass"
    assert refreshed.receipt["retry_of"] == denied.receipt["receipt_digest"]

    wrong_subject = core.recheck_after_snapshot_refresh(
        denied.receipt,
        target,
        action,
        subject=_host_subject(action_id="different-action"),
        adapter_id="shared-test-adapter",
        adapter_version="1.0.0",
        mode="enforce",
        effect="side-effecting",
    )
    assert wrong_subject.outcome == "invalid"
    assert wrong_subject.denied is True
    assert "refresh_subject_mismatch" in wrong_subject.receipt["reason_codes"]
