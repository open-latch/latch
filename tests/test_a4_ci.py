"""A4-C3 local/self-hosted PR/CI policy consumer acceptance contract."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
_CLI = _ROOT / "bin" / "latch_ci_check.py"
_CORPUS_DIR = _ROOT / "tests" / "fixtures" / "a4_ci"
_CORPUS_MANIFEST = _CORPUS_DIR / "git-diff-tree-name-only-z.manifest.json"


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


def _row(row_id: int, predicate: str) -> dict[str, object]:
    return {
        "rejected_path_id": row_id,
        "node_id": 7000 + row_id,
        "option": f"PRIVATE_CI_OPTION_SENTINEL_{row_id}",
        "reason": f"PRIVATE_CI_REASON_SENTINEL_{row_id}",
        "ratifier": "synthetic-founder",
        "decided_at": "2026-09-02T00:00:00Z",
        "scope_predicate": predicate,
        "source": "declared",
        "policy_domain_id": "synthetic-a4-ci-domain",
        "owner_kind": "decision",
        "owner_status": "canonical",
        "owner_updated_at": "2026-09-02T00:00:00Z",
        "latest_ratification_id": 9000 + row_id,
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


def _publish(
    tmp_path: Path,
    predicates: tuple[str, ...],
    *,
    snapshot_kind: str = "private",
):
    snapshot = _module("latch.gate.predicate_snapshot")
    rows = tuple(_row(index, value) for index, value in enumerate(predicates, 1))
    projection = SyntheticProjection(
        engine="predicate-policy-projection-v1",
        policy_domain_id="synthetic-a4-ci-domain",
        binding_rows=rows,
        advisory_rows=(),
        reason_counts={},
        freshness_token="synthetic-a4-ci-generation",
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    if snapshot_kind == "private":
        source = tmp_path / "private-source.sqlite3"
        source.write_bytes(b"sanitized private source fixture\n")
        source_binding = {"source_vault_path": source}
    elif snapshot_kind == "synthetic":
        token = tmp_path / "ci-source-generation.txt"
        token.write_text("generation-one\n", encoding="utf-8")
        source_binding = {"freshness_token_path": token}
    else:
        raise ValueError("snapshot_kind must be private or synthetic")
    private_dir = tmp_path / "private-ci-runtime"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    document = snapshot.publish_policy_snapshot(
        target,
        policy_domain_id="synthetic-a4-ci-domain",
        projector=lambda: projection,
        **source_binding,
    )
    return target, document


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo_history(tmp_path: Path, *, corpus: bool = False):
    repo = tmp_path / "synthetic-ci-repository"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic Test")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    if corpus:
        (repo / "rename-source.txt").write_text("rename\n", encoding="utf-8")
        (repo / "delete-source.txt").write_text("delete\n", encoding="utf-8")
    base_sha = _commit(repo, "base")

    (repo / "src").mkdir()
    (repo / "src" / "safe.py").write_text("safe = True\n", encoding="utf-8")
    if corpus:
        _git(repo, "mv", "rename-source.txt", "renamed file.txt")
        (repo / "delete-source.txt").unlink()
        (repo / "src" / "private-policy-target.py").write_text(
            "blocked = True\n", encoding="utf-8"
        )
        (repo / "tab\tname.txt").write_text("tab\n", encoding="utf-8")
    head_sha = _commit(repo, "head")
    return repo, base_sha, head_sha


def _load_corpus_fixture():
    manifest = json.loads(_CORPUS_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["contract"] == "latch-parser-corpus-fixture-v1"
    assert manifest["encoding"] == "hex"
    provenance = manifest["provenance"]
    assert provenance["capture_kind"] == "sanitized-disposable-git-repository"
    assert provenance["producer"].startswith("git version ")
    assert provenance["command_argv"] == [
        "git",
        "diff-tree",
        "-r",
        "--no-commit-id",
        "--name-only",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        "BASE_SHA",
        "HEAD_SHA",
        "--",
    ]
    payload_path = _CORPUS_DIR / manifest["payload_file"]
    raw = bytes.fromhex(payload_path.read_text(encoding="ascii"))
    assert len(raw) == manifest["payload_bytes"]
    assert hashlib.sha256(raw).hexdigest() == manifest["payload_sha256"]
    expected_paths = tuple(manifest["expected_paths"])
    candidate_floor = manifest["candidate_floor"]
    assert isinstance(candidate_floor, int) and candidate_floor > 0
    return raw, expected_paths, candidate_floor


def _check(ci, target: Path, repo: Path, base_sha: str, head_sha: str, **updates):
    values = {
        "snapshot_path": target,
        "repository_root": repo,
        "repository_id": "synthetic-repository",
        "base_sha": base_sha,
        "head_sha": head_sha,
        "policy_domain_id": "synthetic-a4-ci-domain",
        "runner_scope": "self-hosted",
        "snapshot_kind": "private",
    }
    values.update(updates)
    return ci.check_ci_policy(**values)


def test_unsupported_evidence_class_is_advisory_never_red(tmp_path):
    ci = _module("latch.enforcement.ci")
    target, _ = _publish(
        tmp_path,
        (
            "file:src/private-policy-target.py",
            "glob:blocked/**",
            "package:requests",
            "import:numpy",
            "api:os.system",
        ),
    )
    repo, base_sha, head_sha = _repo_history(tmp_path)

    result = _check(ci, target, repo, base_sha, head_sha)

    assert result.outcome == "pass"
    assert result.exit_code == 0
    assert result.denied is False
    assert result.receipt["matched_rejected_path_ids"] == []
    residuals = result.receipt["advisory_reason_counts"]
    assert residuals["ci_unsupported:package"] == 1
    assert residuals["ci_unsupported:import"] == 1
    assert residuals["ci_unsupported:api"] == 1
    assert all(code not in result.receipt["reason_codes"] for code in residuals)
    assert result.receipt["binding_compiled"] == 2
    assert result.receipt["advisory_rows"] == 3

    serialized = json.dumps(result.receipt, sort_keys=True)
    for predicate in (
        "file:src/private-policy-target.py",
        "glob:blocked/**",
        "package:requests",
        "import:numpy",
        "api:os.system",
    ):
        assert predicate not in serialized


def test_supported_class_incomplete_evidence_still_flags(tmp_path, monkeypatch):
    ci = _module("latch.enforcement.ci")
    target, _ = _publish(tmp_path, ("file:src/private-policy-target.py",))
    repo, base_sha, head_sha = _repo_history(tmp_path, corpus=True)

    # The committed fixture is a sanitized capture from real Git output.  Its
    # manifest binds producer, argv, byte digest, exact paths, and sample floor.
    raw, expected_paths, candidate_floor = _load_corpus_fixture()
    assert raw.endswith(b"\0")
    assert raw.count(b"\0") >= candidate_floor
    evidence = ci.parse_git_diff_paths(raw)
    assert evidence.complete is True
    assert evidence.candidate_count >= candidate_floor
    assert evidence.paths == tuple(sorted(expected_paths))
    assert evidence.raw_digest == hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(
        ci,
        "_git_diff_tree_bytes",
        lambda *_args, **_kwargs: raw[:-1],
    )
    incomplete = _check(ci, target, repo, base_sha, head_sha)
    assert incomplete.outcome == "flag"
    assert incomplete.denied is True
    assert incomplete.exit_code != 0
    assert incomplete.receipt["reason_codes"] == ["ci_evidence_incomplete"]
    assert incomplete.receipt["matched_rejected_path_ids"] == []

    monkeypatch.setattr(
        ci,
        "_git_diff_tree_bytes",
        lambda *_args, **_kwargs: raw,
    )
    complete = _check(ci, target, repo, base_sha, head_sha)
    assert complete.outcome == "block"
    assert complete.denied is True


def test_private_snapshot_never_leaves_runner_and_hosted_ci_is_synthetic_only(
    tmp_path,
):
    ci = _module("latch.enforcement.ci")
    nonexistent = tmp_path / "PRIVATE_SNAPSHOT_PATH_SENTINEL.sqlite3"
    fake_repo = tmp_path / "PRIVATE_REPOSITORY_PATH_SENTINEL"
    hosted_private = _check(
        ci,
        nonexistent,
        fake_repo,
        "1" * 40,
        "2" * 40,
        runner_scope="hosted",
        snapshot_kind="private",
    )
    assert hosted_private.outcome == "invalid"
    assert hosted_private.receipt["reason_codes"] == [
        "private_snapshot_on_hosted_runner"
    ]

    target, _ = _publish(
        tmp_path / "private-snapshot-case",
        ("file:src/private-policy-target.py",),
    )
    repo, base_sha, head_sha = _repo_history(tmp_path)
    mislabelled_private = _check(
        ci,
        target,
        repo,
        base_sha,
        head_sha,
        runner_scope="hosted",
        snapshot_kind="synthetic",
    )
    assert mislabelled_private.outcome == "invalid"
    assert mislabelled_private.receipt["reason_codes"] == [
        "snapshot_kind_mismatch"
    ]

    synthetic_target, _ = _publish(
        tmp_path / "synthetic-snapshot-case",
        ("file:src/private-policy-target.py",),
        snapshot_kind="synthetic",
    )
    hosted_synthetic = _check(
        ci,
        synthetic_target,
        repo,
        base_sha,
        head_sha,
        runner_scope="hosted",
        snapshot_kind="synthetic",
    )
    assert hosted_synthetic.outcome == "pass"

    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            str(_CLI),
            str(target),
            str(repo),
            "--repository-id",
            "synthetic-repository",
            "--base-sha",
            base_sha,
            "--head-sha",
            head_sha,
            "--policy-domain-id",
            "synthetic-a4-ci-domain",
            "--runner-scope",
            "self-hosted",
            "--snapshot-kind",
            "private",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    invalid_proc = subprocess.run(
        [sys.executable, "-I", str(_CLI), "PRIVATE_ARGUMENT_SENTINEL"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid_proc.returncode != 0
    assert invalid_proc.stderr == ""
    invalid_receipt = json.loads(invalid_proc.stdout)
    assert invalid_receipt["outcome"] == "invalid"
    assert invalid_receipt["reason_codes"] == ["invalid_arguments"]

    serialized = json.dumps(
        [
            hosted_private.receipt,
            mislabelled_private.receipt,
            hosted_synthetic.receipt,
            receipt,
            invalid_receipt,
        ],
        sort_keys=True,
    )
    for private_value in (
        str(nonexistent),
        str(fake_repo),
        str(target),
        str(synthetic_target),
        str(repo),
        "PRIVATE_SNAPSHOT_PATH_SENTINEL",
        "PRIVATE_REPOSITORY_PATH_SENTINEL",
        "PRIVATE_CI_OPTION_SENTINEL",
        "PRIVATE_CI_REASON_SENTINEL",
        "PRIVATE_ARGUMENT_SENTINEL",
        "src/safe.py",
    ):
        assert private_value not in serialized

    source = (_SRC / "latch" / "enforcement" / "ci.py").read_text(
        encoding="utf-8"
    )
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imports.isdisjoint(
        {"aiohttp", "httpx", "requests", "socket", "urllib"}
    )


def test_verdict_reproducible_from_immutable_subject(tmp_path):
    ci = _module("latch.enforcement.ci")
    target, document = _publish(
        tmp_path, ("file:src/private-policy-target.py",)
    )
    repo, base_sha, head_sha = _repo_history(tmp_path)

    first = _check(ci, target, repo, base_sha, head_sha)
    second = _check(ci, target, repo, base_sha, head_sha)
    assert first.receipt == second.receipt
    assert first.receipt["subject"] == {
        "kind": "git-range",
        "repository_id": "synthetic-repository",
        "base_sha": base_sha,
        "head_sha": head_sha,
    }
    assert first.receipt["policy"]["snapshot_digest"] == document["digest"]

    # Dirty and branch-tip state cannot influence an immutable tree-to-tree run.
    (repo / "src" / "safe.py").write_text("dirty = True\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    after_dirty = _check(ci, target, repo, base_sha, head_sha)
    assert after_dirty.receipt == first.receipt

    (repo / "src" / "private-policy-target.py").write_text(
        "blocked = True\n", encoding="utf-8"
    )
    _git(repo, "add", "src/private-policy-target.py")
    _git(repo, "commit", "-m", "new immutable head")
    new_head = _git(repo, "rev-parse", "HEAD")
    changed = _check(ci, target, repo, base_sha, new_head)
    assert changed.outcome == "block"
    assert changed.receipt["subject_digest"] != first.receipt["subject_digest"]

    moving_ref = _check(ci, target, repo, "HEAD", new_head)
    assert moving_ref.outcome == "invalid"
    assert moving_ref.receipt["reason_codes"] == ["immutable_git_sha_required"]
