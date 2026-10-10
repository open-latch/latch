#!/usr/bin/env python3
"""Build the public, reproducible latch proof packet."""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from latch.evals import evals
from latch.proof import no_history_demo
from latch.evals import seed_report_evals


ROOT = next(p for p in Path(__file__).resolve().parents if p.name == "src").parent
PROOF_DIR = ROOT / "proof"
LIVE_RECEIPT_PATH = PROOF_DIR / "live_gate_receipt.json"
SUCCESSFUL_GATE_RECOMMENDATIONS = {
    "MODIFY",
    "DO_NOT_PROCEED",
    "NEEDS_HUMAN_JUDGMENT",
}
RUNTIME_BUNDLE_ROOTS = (
    "src",
    "benchmarks/fixtures",
    "vendor",
)
RUNTIME_BUNDLE_FILES = (
    ".gitattributes",
    "VERSION",
    "KB_SCHEMA_VERSION",
    "WIRING_VERSION",
    "requirements.txt",
    "bin/latch_eval.sh",
    "bin/latch_proof_packet.sh",
    "bin/latch_proof_packet.ps1",
    "bin/latch_seed_report_eval.sh",
    "bin/run_latch_outcome_audit.sh",
    # The audit CLI's packaged default: proof currency must bind the shipped
    # contract bytes, not just the code that reads them (Latch 4562 item 4).
    "artifacts/outcome-measurement/contract-v2.6.md",
)
NO_EDIT_PROOF_FIELDS = (
    "command",
    "status_before_clean",
    "status_after_clean",
    "status_entry_count_before",
    "status_entry_count_after",
    "unchanged",
)
PROOF_ARTIFACT_FILES = (
    "proof/live_gate_receipt.json",
    "proof/results.json",
    "proof/README.md",
)


class ProofPacketError(RuntimeError):
    """Raised when proof evidence is missing, unsafe, or internally inconsistent."""


def git_output(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT,
    ).strip()


def git_status(root: Path = ROOT) -> list[str]:
    output = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        text=True,
        stderr=subprocess.STDOUT,
    )
    return output.splitlines()


def source_commit(root: Path = ROOT) -> str:
    return git_output("rev-parse", "HEAD", root=root)


def tested_runtime_paths(*, root: Path = ROOT) -> tuple[str, ...]:
    """Return the tracked source/fixture bundle exercised by this packet."""
    try:
        output = subprocess.check_output(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                *RUNTIME_BUNDLE_ROOTS,
                *RUNTIME_BUNDLE_FILES,
            ],
            cwd=root,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as exc:
        raise ProofPacketError("could not enumerate tracked runtime paths") from exc
    paths = {
        item.decode("utf-8", errors="surrogateescape")
        for item in output.split(b"\0")
        if item
    }
    missing_files = set(RUNTIME_BUNDLE_FILES) - paths
    missing_roots = [
        relative_root
        for relative_root in RUNTIME_BUNDLE_ROOTS
        if not any(path.startswith(f"{relative_root}/") for path in paths)
    ]
    if missing_files or missing_roots:
        missing = sorted(missing_files) + sorted(missing_roots)
        raise ProofPacketError(
            f"tested runtime bundle is missing tracked paths: {', '.join(missing)}"
        )
    return tuple(sorted(paths))


def runtime_manifest(*, root: Path = ROOT) -> dict[str, str]:
    """Hash working files as Git canonical blobs, honoring clean filters."""
    manifest = {}
    for relative_path in tested_runtime_paths(root=root):
        try:
            manifest[relative_path] = git_output(
                "hash-object", f"--path={relative_path}", relative_path, root=root,
            )
        except subprocess.CalledProcessError as exc:
            raise ProofPacketError(
                f"could not hash tested runtime path {relative_path}"
            ) from exc
    return manifest


def manifest_digest(manifest: dict[str, str]) -> str:
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def assert_tested_runtime_matches(
    receipt: dict[str, Any], *, root: Path = ROOT,
) -> None:
    expected = receipt.get("tested_runtime_git_blobs")
    expected_paths = set(tested_runtime_paths(root=root))
    if not isinstance(expected, dict) or set(expected) != expected_paths:
        raise ProofPacketError("live receipt has an incomplete tested runtime manifest")
    assert_source_commit_matches_manifest(receipt, root=root)
    actual = runtime_manifest(root=root)
    if expected != actual:
        changed = sorted(path for path in actual if expected.get(path) != actual[path])
        raise ProofPacketError(
            "proof runtime changed since the live receipt; recapture the live proof "
            f"({', '.join(changed)})"
        )


def assert_source_commit_matches_manifest(
    receipt: dict[str, Any], *, root: Path = ROOT,
    require_direct_parent: bool = False,
) -> None:
    commit = str(receipt.get("source_commit") or "")
    manifest = receipt.get("tested_runtime_git_blobs") or {}
    try:
        resolved = git_output("rev-parse", "--verify", f"{commit}^{{commit}}", root=root)
    except subprocess.CalledProcessError as exc:
        try:
            shallow = git_output(
                "rev-parse", "--is-shallow-repository", root=root,
            ) == "true"
        except subprocess.CalledProcessError:
            shallow = False
        if shallow:
            raise ProofPacketError(
                "live receipt source_commit is unavailable in this shallow clone; "
                "run `git fetch --deepen=2` and rerun --check (repeat if needed), "
                "or run `git fetch --unshallow`"
            ) from exc
        raise ProofPacketError("live receipt source_commit does not exist") from exc
    if resolved != commit:
        raise ProofPacketError("live receipt source_commit is not an exact commit coordinate")

    if require_direct_parent:
        try:
            head_artifacts = {
                relative_path: git_output(
                    "rev-parse", f"HEAD:{relative_path}", root=root,
                )
                for relative_path in PROOF_ARTIFACT_FILES
            }
            history = git_output("rev-list", "--parents", "HEAD", root=root)
        except subprocess.CalledProcessError as exc:
            raise ProofPacketError(
                "could not resolve the generated artifact history"
            ) from exc
        artifact_commit = None
        for line in history.splitlines():
            coordinates = line.split()
            if len(coordinates) != 2 or coordinates[1] != commit:
                continue
            candidate = coordinates[0]
            try:
                candidate_artifacts = {
                    relative_path: git_output(
                        "rev-parse", f"{candidate}:{relative_path}", root=root,
                    )
                    for relative_path in PROOF_ARTIFACT_FILES
                }
            except subprocess.CalledProcessError:
                continue
            if candidate_artifacts == head_artifacts:
                artifact_commit = candidate
                break
        if artifact_commit is None:
            raise ProofPacketError(
                "live receipt source_commit must be the direct parent of the "
                "generated artifact commit"
            )

    changed = []
    for relative_path, expected_oid in manifest.items():
        try:
            actual_oid = git_output(
                "rev-parse", f"{commit}:{relative_path}", root=root,
            )
        except subprocess.CalledProcessError:
            changed.append(relative_path)
            continue
        if actual_oid != expected_oid:
            changed.append(relative_path)
    if changed:
        raise ProofPacketError(
            "live receipt manifest does not match its source commit "
            f"({', '.join(sorted(changed))})"
        )


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_strings(item)


def assert_public_safe(value: Any, *, label: str) -> None:
    forbidden = (
        "/" + "Users" + "/",
        "/" + "home" + "/",
        "/" + "tmp" + "/",
        "/" + "var" + "/" + "tmp" + "/",
        "/private/" + "tmp/",
        "\\" + "Users" + "\\",
        "session" + "_id",
        "transcript" + "_path",
        "source" + "_paths",
    )
    for text in iter_strings(value):
        if any(token in text for token in forbidden):
            raise ProofPacketError(f"{label} contains a private/runtime coordinate")


def canonical_fixture_evidence(receipt: dict[str, Any]) -> dict[str, Any]:
    fixture = receipt.get("fixture") or {}
    seeded_id = fixture.get("seeded_decision_id")
    seeded_title = fixture.get("seeded_decision_title")
    matches = [
        item
        for item in receipt.get("evidence_nodes") or []
        if item.get("id") == seeded_id
        and item.get("kind") == "decision"
        and item.get("status") == "canonical"
        and item.get("title") == seeded_title
    ]
    if len(matches) != 1:
        raise ProofPacketError(
            "live receipt must cite exactly one canonical seeded fixture decision"
        )
    return matches[0]


def validate_live_receipt(receipt: dict[str, Any]) -> None:
    if receipt.get("schema_version") != 2:
        raise ProofPacketError("live receipt schema_version must be 2")
    if receipt.get("observed") is not True:
        raise ProofPacketError("live receipt must be observed, not expected output")
    if receipt.get("demo_kind") != "synthetic_no_history_fixture":
        raise ProofPacketError("V1 live receipt must identify the public fixture honestly")
    recommendation = receipt.get("recommendation")
    if recommendation not in SUCCESSFUL_GATE_RECOMMENDATIONS:
        raise ProofPacketError(
            f"live receipt recommendation is not proof: {recommendation!r}"
        )
    for field in ("summary", "risk_if_proceed", "better_next_action"):
        if not str(receipt.get(field) or "").strip():
            raise ProofPacketError(f"live receipt is missing {field}")
    evidence = receipt.get("evidence_nodes") or []
    if not evidence:
        raise ProofPacketError("live receipt has no cited evidence")
    fixture = receipt.get("fixture") or {}
    if fixture.get("used_personal_history") is not False:
        raise ProofPacketError("fixture receipt must state that personal history was not used")
    canonical_fixture_evidence(receipt)
    proof = receipt.get("no_edit_proof") or {}
    if set(proof) != set(NO_EDIT_PROOF_FIELDS):
        raise ProofPacketError("live receipt has unexpected worktree-proof fields")
    if (
        proof.get("unchanged") is not True
        or proof.get("status_before_clean") is not True
        or proof.get("status_after_clean") is not True
        or proof.get("status_entry_count_before") != 0
        or proof.get("status_entry_count_after") != 0
    ):
        raise ProofPacketError("live receipt requires a clean worktree before and after")
    commit = str(receipt.get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ProofPacketError("live receipt source_commit is not a full Git SHA")
    manifest = receipt.get("tested_runtime_git_blobs")
    if not isinstance(manifest, dict) or not manifest:
        raise ProofPacketError("live receipt tested runtime manifest is incomplete")
    if not all(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(value))
        for value in manifest.values()
    ):
        raise ProofPacketError("live receipt tested runtime manifest is invalid")
    assert_public_safe(receipt, label="live receipt")


def capture_live_receipt(*, backend: str, root: Path = ROOT) -> dict[str, Any]:
    before = git_status(root)
    if before:
        raise ProofPacketError("live capture requires a clean worktree")
    fixture_root = Path(tempfile.mkdtemp(prefix="latch-public-proof-"))
    try:
        fixture = no_history_demo.create_fixture(fixture_root)
        out = no_history_demo.run_gate(
            fixture,
            request=no_history_demo.DEMO_REQUEST,
            use_llm=True,
            max_chains=5,
            backend=backend,
        )
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)
    after = git_status(root)
    if after:
        raise ProofPacketError("live capture changed the worktree")
    verdict = out.get("verdict") or {}
    findings = out.get("findings") or {}
    evidence = out.get("evidence") or []
    receipt = {
        "schema_version": 2,
        "observed": True,
        "captured_at_utc": datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z"),
        "source_commit": source_commit(root),
        "tested_runtime_git_blobs": runtime_manifest(root=root),
        "capture_command": (
            f"bash bin/latch_proof_packet.sh --capture-live --backend {backend}"
        ),
        "demo_kind": "synthetic_no_history_fixture",
        "backend": verdict.get("backend") or backend,
        "request": no_history_demo.DEMO_REQUEST,
        "recommendation": findings.get("recommendation") or verdict.get("recommendation"),
        "summary": findings.get("summary") or verdict.get("summary"),
        "risk_if_proceed": (
            findings.get("risk_if_proceed") or verdict.get("risk_if_proceed")
        ),
        "better_next_action": (
            findings.get("better_next_action") or verdict.get("better_next_action")
        ),
        "evidence_nodes": [
            {key: item.get(key) for key in ("id", "kind", "title", "status")}
            for item in evidence
        ],
        "fixture": {
            "label": "Synthetic no-history fixture",
            "used_personal_history": False,
            "seeded_decision_id": fixture.decision_id,
            "seeded_decision_title": no_history_demo.DEMO_DECISION_TITLE,
            "source": "generated public fixture file GOVERNANCE.md",
        },
        "no_edit_proof": {
            "command": "git status --porcelain=v1 --untracked-files=all",
            "status_before_clean": True,
            "status_after_clean": True,
            "status_entry_count_before": 0,
            "status_entry_count_after": 0,
            "unchanged": True,
        },
    }
    validate_live_receipt(receipt)
    return receipt


def load_live_receipt(path: Path = LIVE_RECEIPT_PATH) -> dict[str, Any]:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProofPacketError(
            "no observed live receipt exists; rerun with --capture-live"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ProofPacketError(f"invalid live receipt JSON: {exc}") from exc
    validate_live_receipt(receipt)
    return receipt


def compact_mode(mode: dict[str, Any]) -> dict[str, Any]:
    return {
        key: mode[key]
        for key in (
            "cases",
            "passed",
            "failed",
            "pass_rate",
            "required_retrieval_rate",
            "supporting_phrase_rate",
        )
    }


def build_public_results(
    live_receipt: dict[str, Any], *, root: Path = ROOT, verify_runtime: bool = True,
) -> dict[str, Any]:
    """Derive the public results payload from an observed live receipt.

    ``verify_runtime`` re-binds the receipt to the current working tree.  It
    stays on for every production caller — ``generate_packet`` and through it
    ``publish_packet`` and ``check_generated`` — which is what makes the
    release-time packet-currency guarantee real.  Tests that exercise
    derivation or rendering pass ``False``, so a routine runtime-bundle edit (a
    VERSION bump, a ``bin/`` wrapper change) does not turn everyday CI red and
    force a live gate recapture.
    """
    validate_live_receipt(live_receipt)
    tested_commit = live_receipt["source_commit"]
    if verify_runtime:
        assert_tested_runtime_matches(live_receipt, root=root)
    wedge = evals.run_cases(evals.load_cases([evals.DEFAULT_FIXTURE]))
    seed_report = seed_report_evals.run_seed_report_eval()
    comparison = wedge["summary"]["comparisons"]["latch_evidence_vs_memory_like"]
    results = {
        "schema_version": 2,
        "source_commit": tested_commit,
        "tested_runtime_manifest_sha256": manifest_digest(
            live_receipt["tested_runtime_git_blobs"]
        ),
        "thesis": (
            "Latch preserves project judgment and can surface a rejected path "
            "before an agent changes files."
        ),
        "live_demo": {
            key: copy.deepcopy(live_receipt[key])
            for key in (
                "observed",
                "captured_at_utc",
                "source_commit",
                "demo_kind",
                "backend",
                "request",
                "recommendation",
                "summary",
                "risk_if_proceed",
                "better_next_action",
                "evidence_nodes",
                "fixture",
            )
        },
        "wedge_v1": {
            "ok": wedge["ok"],
            "cases": wedge["summary"]["cases"],
            "passed": wedge["summary"]["passed"],
            "failed": wedge["summary"]["failed"],
            "modes": {
                name: compact_mode(wedge["modes"][name])
                for name in evals.DEFAULT_MODES
            },
            "latch_evidence_vs_memory_like": comparison,
            "memory_like_definition": evals.MODE_DESCRIPTIONS["memory_like"],
            "baseline_boundary": (
                "memory_like is an internal active-search-only ablation. It is "
                "not a benchmark of any third-party memory product."
            ),
        },
        "seed_report_eval": {
            "ok": seed_report["ok"],
            "checks": seed_report["summary"]["checks"],
            "passed": seed_report["summary"]["passed"],
            "failed": seed_report["summary"]["failed"],
            "pass_rate": seed_report["summary"]["pass_rate"],
            "candidate_count": seed_report["summary"]["candidate_count"],
            "section_counts": seed_report["summary"]["section_counts"],
            "synthetic_candidate_probe": {
                "considered": (
                    seed_report["summary"]["synthetic_llm_candidate_count"]
                    + seed_report["summary"]["synthetic_llm_filtered_count"]
                ),
                "accepted": seed_report["summary"][
                    "synthetic_llm_candidate_count"
                ],
                "filtered": seed_report["summary"][
                    "synthetic_llm_filtered_count"
                ],
            },
            "catch_demo": seed_report["summary"]["catch_demo"],
            "model_calls": 0,
            "boundary": (
                "This deterministic fixture eval grades seed-report capture and "
                "filtering; it is not a live transcript-quality benchmark."
            ),
        },
        "reproduction": {
            "check_packet": "bash bin/latch_proof_packet.sh --check",
            "wedge_eval": "bash bin/latch_eval.sh",
            "seed_report_eval": "bash bin/latch_seed_report_eval.sh",
            "recapture_live_demo": live_receipt["capture_command"],
        },
        "what_this_proves": [
            "A live model-backed gate can cite a canonical rejected path and redirect a violating request before repository edits.",
            "The deterministic wedge suite distinguishes full decision-evidence assembly from its active-search-only ablation.",
            "The deterministic seed-report fixture suite exercises structured capture, filtering, and catch-demo selection.",
        ],
        "what_this_does_not_prove": [
            "The synthetic no-history demo is not evidence about a particular user's project history.",
            "The internal memory_like ablation does not measure any third-party memory product.",
            "The small fixture suites are proof instruments, not broad claims about every repository or model.",
        ],
    }
    results["live_demo"]["no_edit_proof"] = {
        key: copy.deepcopy(live_receipt["no_edit_proof"][key])
        for key in NO_EDIT_PROOF_FIELDS
    }
    assert_public_safe(results, label="public results")
    return results


def pct(value: float) -> str:
    return f"{value:.0%}"


def render_readme(results: dict[str, Any]) -> str:
    live = results["live_demo"]
    wedge = results["wedge_v1"]
    seed = results["seed_report_eval"]
    comparison = wedge["latch_evidence_vs_memory_like"]
    evidence = canonical_fixture_evidence(live)
    lines = [
        "# Latch V1 public proof packet",
        "",
        results["thesis"],
        "",
        "This packet combines one observed live gate receipt with two small, "
        "deterministic fixture suites. It is designed to be skimmed in two minutes.",
        "",
        "## Results at a glance",
        "",
        "| Evidence | Result | Meaning |",
        "| --- | ---: | --- |",
        (
            f"| Live pre-edit gate | `{live['recommendation']}` | "
            f"Cited canonical decision id={evidence['id']}; worktree unchanged |"
        ),
        (
            f"| `wedge_v1` | {wedge['passed']}/{wedge['cases']} | "
            f"`memory_like` passed {wedge['modes']['memory_like']['passed']}/"
            f"{wedge['modes']['memory_like']['cases']}; "
            f"{comparison['primary_only_wins']} latch-only wins |"
        ),
        (
            f"| Seed-report eval | {seed['passed']}/{seed['checks']} | "
            "Deterministic capture/filtering checks; zero model calls |"
        ),
        "",
        "## Observed live gate",
        "",
        f"Captured with the `{live['backend']}` backend on commit "
        f"`{live['source_commit']}`. This is a synthetic no-history fixture and "
        "used no personal conversation history.",
        "",
        "```text",
        f"Request: {live['request']}",
        f"Recommendation: {live['recommendation']}",
        f"Summary: {live['summary']}",
        f"Risk if proceed: {live['risk_if_proceed']}",
        f"Better next action: {live['better_next_action']}",
        "Cited evidence:",
        (
            f"- id={evidence['id']} {evidence['kind']} status={evidence['status']}: "
            f"{evidence['title']}"
        ),
        "Worktree changed before/after gate: no",
        "```",
        "",
        "The gate used an actual model call. `SKIPPED`, `PROCEED`, empty evidence, "
        "or a changed worktree would fail packet validation.",
        "",
        "## Decision-evidence comparison",
        "",
        "| Mode | Passed | Required retrieval | Supporting rationale |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in evals.DEFAULT_MODES:
        mode = wedge["modes"][name]
        lines.append(
            f"| `{name}` | {mode['passed']}/{mode['cases']} | "
            f"{pct(mode['required_retrieval_rate'])} | "
            f"{pct(mode['supporting_phrase_rate'])} |"
        )
    lines.extend([
        "",
        f"`memory_like` means: {wedge['memory_like_definition']}.",
        "",
        wedge["baseline_boundary"],
        "The defensible reading is that decision relations and status-aware evidence "
        f"assembly add value on these {wedge['cases']} fixtures. The table is not a claim that "
        "Latch outperforms a named memory product.",
        "",
        "## Seed-report capture checks",
        "",
        f"The deterministic seed-report suite passed {seed['passed']}/{seed['checks']} "
        f"checks and produced {seed['candidate_count']} fixture candidates. It exercises "
        "decisions and rejected paths, where-left-off state, preferences, continuity, "
        "strict agent-alignment filtering, source scoping, and catch-demo selection.",
        "",
        seed["boundary"],
        "",
        "## What this proves",
        "",
    ])
    lines.extend(f"- {item}" for item in results["what_this_proves"])
    lines.extend(["", "## What this does not prove", ""])
    lines.extend(f"- {item}" for item in results["what_this_does_not_prove"])
    lines.extend([
        "",
        "## Reproduce",
        "",
        f"The deterministic results were generated from commit `{results['source_commit']}`.",
        "",
        "```bash",
        results["reproduction"]["wedge_eval"],
        results["reproduction"]["seed_report_eval"],
        results["reproduction"]["check_packet"],
        "```",
        "",
        "Verification checks the tooling commit immediately before the generated "
        "artifacts. A GitHub merge ref adds another history level, so if a depth-1 "
        "clone reports that the receipt commit is unavailable, deepen by two and "
        "retry. Repeat if needed, or fetch the full history:",
        "",
        "```bash",
        "git fetch --deepen=2  # repeat if needed; or: git fetch --unshallow",
        results["reproduction"]["check_packet"],
        "```",
        "",
        "Recapturing the live receipt spends a model call and replaces the observed "
        "receipt only after it passes the proof checks:",
        "",
        "```bash",
        results["reproduction"]["recapture_live_demo"],
        "```",
        "",
        "The machine-readable summary is in [`results.json`](./results.json), and the "
        "observed receipt is in "
        "[`live_gate_receipt.json`](./live_gate_receipt.json).",
        "",
    ])
    rendered = "\n".join(lines)
    assert_public_safe(rendered, label="proof README")
    return rendered


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def generate_packet(
    live_receipt: dict[str, Any], *, output_dir: Path = PROOF_DIR, root: Path = ROOT,
    verify_runtime: bool = True,
) -> tuple[dict[str, Any], str]:
    results = build_public_results(
        live_receipt, root=root, verify_runtime=verify_runtime,
    )
    readme = render_readme(results)
    write_json(output_dir / "results.json", results)
    (output_dir / "README.md").write_text(readme, encoding="utf-8")
    return results, readme


def publish_packet(
    live_receipt: dict[str, Any], *, output_dir: Path = PROOF_DIR, root: Path = ROOT,
    verify_runtime: bool = True,
) -> None:
    """Build and validate the complete packet before swapping artifact directories.

    ``verify_runtime`` forwards to :func:`build_public_results`; tests covering
    the atomic swap and restore semantics pass ``False`` so they exercise
    directory handling rather than packet currency.
    """
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".latch-proof-publish-", dir=output_dir.parent))
    backup_root = Path(tempfile.mkdtemp(prefix=".latch-proof-backup-", dir=output_dir.parent))
    previous_dir = backup_root / "previous"
    expected_names = {"live_gate_receipt.json", "results.json", "README.md"}
    try:
        write_json(temp_dir / "live_gate_receipt.json", live_receipt)
        generate_packet(
            live_receipt, output_dir=temp_dir, root=root,
            verify_runtime=verify_runtime,
        )
        load_live_receipt(temp_dir / "live_gate_receipt.json")
        json.loads((temp_dir / "results.json").read_text(encoding="utf-8"))
        rendered = (temp_dir / "README.md").read_text(encoding="utf-8")
        assert_public_safe(rendered, label="proof README")

        had_previous = output_dir.exists()
        if had_previous:
            unexpected = {path.name for path in output_dir.iterdir()} - expected_names
            if unexpected:
                raise ProofPacketError(
                    "proof directory contains unmanaged files: "
                    f"{', '.join(sorted(unexpected))}"
                )
            os.replace(output_dir, previous_dir)
        try:
            os.replace(temp_dir, output_dir)
        except BaseException:
            if had_previous and previous_dir.exists():
                os.replace(previous_dir, output_dir)
            raise
        if previous_dir.exists():
            shutil.rmtree(previous_dir)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        shutil.rmtree(backup_root, ignore_errors=True)


def check_generated(live_receipt: dict[str, Any]) -> None:
    assert_source_commit_matches_manifest(
        live_receipt, require_direct_parent=True,
    )
    temp_dir = Path(tempfile.mkdtemp(prefix="latch-proof-check-"))
    try:
        generate_packet(live_receipt, output_dir=temp_dir)
        for name in ("results.json", "README.md"):
            expected = (PROOF_DIR / name).read_text(encoding="utf-8")
            actual = (temp_dir / name).read_text(encoding="utf-8")
            if expected != actual:
                raise ProofPacketError(
                    f"proof/{name} is stale; run bin/latch_proof_packet.sh"
                )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="build the public latch proof packet")
    parser.add_argument("--capture-live", action="store_true",
                        help="spend one model call and replace the live receipt")
    parser.add_argument("--backend", choices=("codex", "claude"),
                        help="backend for --capture-live")
    parser.add_argument("--check", action="store_true",
                        help="verify committed packet files are current")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.capture_live and not args.backend:
        print("proof_packet: --capture-live requires --backend codex|claude")
        return 2
    try:
        if args.capture_live:
            receipt = capture_live_receipt(backend=args.backend)
        else:
            receipt = load_live_receipt()
        if args.check:
            check_generated(receipt)
            print("proof packet is current and public-safe")
        else:
            publish_packet(receipt)
            print("wrote validated proof packet artifacts")
    except ProofPacketError as exc:
        print(f"proof_packet: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
