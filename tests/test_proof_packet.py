from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import proof_packet  # noqa: E402


ROOT = Path(__file__).resolve().parent.parent


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _init_manifest_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "config", "user.email", "proof-packet@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Proof Packet Test"], cwd=root, check=True,
    )
    for relative_path in proof_packet.RUNTIME_BUNDLE_FILES:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            "*.sh text eol=lf\n"
            if relative_path == ".gitattributes"
            else f"tracked fixture for {relative_path}\n"
        )
        path.write_text(body, encoding="utf-8")
    source = root / "src" / "tracked.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    fixture = root / "benchmarks" / "fixtures" / "wedge_v1.jsonl"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text('{"id":"fixture"}\n', encoding="utf-8")
    vendor = root / "vendor" / "model.onnx"
    vendor.parent.mkdir(parents=True, exist_ok=True)
    vendor.write_bytes(b"synthetic model fixture\n")
    (root / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


def test_live_receipt_is_observed_and_complete():
    receipt = proof_packet.load_live_receipt()
    _assert(receipt["schema_version"] == 2, receipt)
    _assert(receipt["observed"] is True, receipt)
    _assert(receipt["recommendation"] in proof_packet.SUCCESSFUL_GATE_RECOMMENDATIONS,
            receipt)
    _assert(receipt["evidence_nodes"], receipt)
    _assert(receipt["better_next_action"], receipt)
    _assert(receipt["no_edit_proof"]["unchanged"] is True, receipt)
    _assert(receipt["no_edit_proof"]["status_before_clean"] is True, receipt)
    _assert(receipt["no_edit_proof"]["status_after_clean"] is True, receipt)
    print("PASS live_receipt_is_observed_and_complete")


def test_live_receipt_rejects_non_proof_states():
    receipt = proof_packet.load_live_receipt()
    for recommendation in (None, "SKIPPED", "PROCEED"):
        candidate = copy.deepcopy(receipt)
        candidate["recommendation"] = recommendation
        try:
            proof_packet.validate_live_receipt(candidate)
        except proof_packet.ProofPacketError:
            pass
        else:
            raise AssertionError(f"accepted non-proof recommendation {recommendation!r}")
    changed = copy.deepcopy(receipt)
    changed["no_edit_proof"]["status_after_clean"] = False
    changed["no_edit_proof"]["status_entry_count_after"] = 1
    try:
        proof_packet.validate_live_receipt(changed)
    except proof_packet.ProofPacketError:
        pass
    else:
        raise AssertionError("accepted a changed worktree as proof")
    print("PASS live_receipt_rejects_non_proof_states")


def test_live_receipt_rejects_raw_dirty_paths():
    receipt = copy.deepcopy(proof_packet.load_live_receipt())
    receipt["no_edit_proof"]["dirty_paths"] = [
        "?? customers/acme-secret-plan.md"
    ]
    try:
        proof_packet.validate_live_receipt(receipt)
    except proof_packet.ProofPacketError:
        pass
    else:
        raise AssertionError("accepted raw dirty paths in a public receipt")
    print("PASS live_receipt_rejects_raw_dirty_paths")


def test_live_receipt_rejects_private_coordinates():
    for coordinate in (
        "/" + "Users" + "/example/private-project",
        "/" + "home" + "/alice/private-project",
        "/" + "tmp" + "/latch-private-abc/project",
        "/" + "var" + "/" + "tmp" + "/latch-private-abc/project",
    ):
        receipt = copy.deepcopy(proof_packet.load_live_receipt())
        receipt["summary"] = coordinate
        try:
            proof_packet.validate_live_receipt(receipt)
        except proof_packet.ProofPacketError:
            pass
        else:
            raise AssertionError(
                f"accepted a personal filesystem coordinate: {coordinate}"
            )
    print("PASS live_receipt_rejects_private_coordinates")


def test_runtime_manifest_rejects_drift_without_git_history():
    receipt = copy.deepcopy(proof_packet.load_live_receipt())
    first_path = proof_packet.tested_runtime_paths()[0]
    current_oid = receipt["tested_runtime_git_blobs"][first_path]
    receipt["tested_runtime_git_blobs"][first_path] = "0" * len(current_oid)
    try:
        proof_packet.assert_tested_runtime_matches(receipt)
    except proof_packet.ProofPacketError:
        pass
    else:
        raise AssertionError("accepted a changed tested runtime manifest")
    print("PASS runtime_manifest_rejects_drift_without_git_history")


def test_runtime_manifest_covers_authoritative_bundle():
    paths = set(proof_packet.tested_runtime_paths())
    required = {
        "VERSION",
        "KB_SCHEMA_VERSION",
        "WIRING_VERSION",
        "bin/latch_eval.sh",
        "bin/latch_seed_report_eval.sh",
        "src/codex_transcript.py",
        "src/budget.py",
        "src/priorities.py",
        "src/profiles.py",
        "src/schema.sql",
        "src/proof_packet.py",
        "vendor/config.json",
        "vendor/model.onnx",
        "vendor/special_tokens_map.json",
        "vendor/tokenizer.json",
        "vendor/tokenizer_config.json",
        "vendor/vocab.txt",
        "bin/latch_proof_packet.sh",
        "bin/latch_proof_packet.ps1",
    }
    _assert(required <= paths, sorted(required - paths))
    _assert(not any("__pycache__" in path or path.endswith(".pyc") for path in paths),
            sorted(paths))
    print("PASS runtime_manifest_covers_authoritative_bundle")


def test_runtime_manifest_excludes_ignored_files():
    with tempfile.TemporaryDirectory(prefix="latch-proof-tracked-test-") as raw_dir:
        root = Path(raw_dir)
        _init_manifest_repo(root)
        ignored = root / "src" / ".DS_Store"
        ignored.write_bytes(b"ignored metadata")
        status = subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            text=True,
        )
        _assert(status == "", status)
        paths = proof_packet.tested_runtime_paths(root=root)
        _assert("src/.DS_Store" not in paths, paths)
        _assert(set(proof_packet.runtime_manifest(root=root)) == set(paths), paths)
    print("PASS runtime_manifest_excludes_ignored_files")


def test_runtime_manifest_rejects_vendor_asset_drift():
    with tempfile.TemporaryDirectory(prefix="latch-proof-vendor-test-") as raw_dir:
        root = Path(raw_dir)
        _init_manifest_repo(root)
        receipt = {
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            ).strip(),
            "tested_runtime_git_blobs": proof_packet.runtime_manifest(root=root),
        }
        (root / "vendor" / "model.onnx").write_bytes(b"changed model fixture\n")
        try:
            proof_packet.assert_tested_runtime_matches(receipt, root=root)
        except proof_packet.ProofPacketError as exc:
            _assert("vendor/model.onnx" in str(exc), str(exc))
        else:
            raise AssertionError("accepted changed vendor/model.onnx")
    print("PASS runtime_manifest_rejects_vendor_asset_drift")


def test_runtime_manifest_canonicalizes_crlf_checkout():
    with tempfile.TemporaryDirectory(prefix="latch-proof-crlf-test-") as raw_dir:
        source = Path(raw_dir) / "source"
        checkout = Path(raw_dir) / "checkout"
        _init_manifest_repo(source)
        subprocess.run(
            [
                "git", "clone", "-q", "--no-local", "-c", "core.autocrlf=true",
                str(source), str(checkout),
            ],
            check=True,
        )
        _assert(
            b"\r\n" in (checkout / "src" / "tracked.py").read_bytes(), "not CRLF"
        )
        for relative_path in proof_packet.RUNTIME_BUNDLE_FILES:
            if relative_path.endswith(".sh"):
                _assert(
                    b"\r\n" not in (checkout / relative_path).read_bytes(),
                    f"shell script is CRLF: {relative_path}",
                )
        manifest = proof_packet.runtime_manifest(root=checkout)
        for relative_path, actual_oid in manifest.items():
            expected_oid = subprocess.check_output(
                ["git", "rev-parse", f"HEAD:{relative_path}"],
                cwd=checkout,
                text=True,
            ).strip()
            _assert(actual_oid == expected_oid, relative_path)
    print("PASS runtime_manifest_canonicalizes_crlf_checkout")


def test_source_commit_must_contain_manifest_blobs():
    receipt = copy.deepcopy(proof_packet.load_live_receipt())
    receipt["source_commit"] = subprocess.check_output(
        ["git", "rev-list", "--max-parents=0", "HEAD"], cwd=ROOT, text=True
    ).splitlines()[0]
    try:
        proof_packet.assert_tested_runtime_matches(receipt)
    except proof_packet.ProofPacketError:
        pass
    else:
        raise AssertionError("accepted a source commit that does not contain the manifest")
    print("PASS source_commit_must_contain_manifest_blobs")


def test_source_commit_must_directly_precede_generated_artifacts():
    with tempfile.TemporaryDirectory(prefix="latch-proof-parent-test-") as raw_dir:
        root = Path(raw_dir)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "proof-packet@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Proof Packet Test"],
            cwd=root,
            check=True,
        )
        tracked = root / "tracked.txt"
        tracked.write_text("tooling\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "tooling"], cwd=root, check=True)
        tooling_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        ).strip()
        tooling_oid = subprocess.check_output(
            ["git", "rev-parse", f"{tooling_commit}:tracked.txt"],
            cwd=root,
            text=True,
        ).strip()
        receipt = {
            "source_commit": tooling_commit,
            "tested_runtime_git_blobs": {"tracked.txt": tooling_oid},
        }
        (root / "docs.txt").write_text("interposed docs\n", encoding="utf-8")
        subprocess.run(["git", "add", "docs.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "docs"], cwd=root, check=True)
        for relative_path in proof_packet.PROOF_ARTIFACT_FILES:
            path = root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"generated {relative_path}\n", encoding="utf-8")
        subprocess.run(["git", "add", "proof"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "artifact"], cwd=root, check=True)
        try:
            proof_packet.assert_source_commit_matches_manifest(
                receipt, root=root, require_direct_parent=True,
            )
        except proof_packet.ProofPacketError as exc:
            _assert("direct parent" in str(exc), str(exc))
        else:
            raise AssertionError("accepted a non-parent tooling commit")
    print("PASS source_commit_must_directly_precede_generated_artifacts")


def test_source_and_artifact_pair_survives_a_merge_ref():
    with tempfile.TemporaryDirectory(prefix="latch-proof-merge-test-") as raw_dir:
        root = Path(raw_dir)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "proof-packet@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Proof Packet Test"],
            cwd=root,
            check=True,
        )
        (root / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        ).strip()
        subprocess.run(["git", "checkout", "-qb", "proof"], cwd=root, check=True)
        tracked = root / "tracked.txt"
        tracked.write_text("tooling\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "tooling"], cwd=root, check=True)
        tooling_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
        ).strip()
        tooling_oid = subprocess.check_output(
            ["git", "rev-parse", f"{tooling_commit}:tracked.txt"],
            cwd=root,
            text=True,
        ).strip()
        proof_dir = root / "proof"
        proof_dir.mkdir()
        for relative_path in proof_packet.PROOF_ARTIFACT_FILES:
            (root / relative_path).write_text(
                f"generated {relative_path}\n", encoding="utf-8",
            )
        subprocess.run(["git", "add", "proof"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "artifacts"], cwd=root, check=True)
        subprocess.run(
            ["git", "checkout", "-qb", "main", base_commit], cwd=root, check=True,
        )
        (root / "main.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "add", "main.txt"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "main"], cwd=root, check=True)
        subprocess.run(
            ["git", "merge", "--no-ff", "proof", "-m", "merge proof"],
            cwd=root,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        receipt = {
            "source_commit": tooling_commit,
            "tested_runtime_git_blobs": {"tracked.txt": tooling_oid},
        }
        proof_packet.assert_source_commit_matches_manifest(
            receipt, root=root, require_direct_parent=True,
        )
    print("PASS source_and_artifact_pair_survives_a_merge_ref")


def test_shallow_source_commit_error_is_actionable():
    with tempfile.TemporaryDirectory(prefix="latch-proof-shallow-test-") as raw_dir:
        source = Path(raw_dir) / "source"
        checkout = Path(raw_dir) / "checkout"
        source.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "proof-packet@example.invalid"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Proof Packet Test"],
            cwd=source,
            check=True,
        )
        tracked = source / "tracked.txt"
        tracked.write_text("tooling\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "tooling"], cwd=source, check=True)
        tooling_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True,
        ).strip()
        tooling_oid = subprocess.check_output(
            ["git", "rev-parse", f"{tooling_commit}:tracked.txt"],
            cwd=source,
            text=True,
        ).strip()
        (source / "artifact.txt").write_text("generated\n", encoding="utf-8")
        subprocess.run(["git", "add", "artifact.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "artifact"], cwd=source, check=True)
        subprocess.run(
            ["git", "clone", "-q", "--depth=1", source.as_uri(), str(checkout)],
            check=True,
        )
        receipt = {
            "source_commit": tooling_commit,
            "tested_runtime_git_blobs": {"tracked.txt": tooling_oid},
        }
        try:
            proof_packet.assert_source_commit_matches_manifest(receipt, root=checkout)
        except proof_packet.ProofPacketError as exc:
            message = str(exc)
            _assert("shallow clone" in message, message)
            _assert("git fetch --deepen=2" in message, message)
            _assert("repeat if needed" in message, message)
            _assert("git fetch --unshallow" in message, message)
        else:
            raise AssertionError("accepted an unavailable shallow source commit")
        subprocess.run(["git", "fetch", "--deepen=2"], cwd=checkout, check=True)
        proof_packet.assert_source_commit_matches_manifest(receipt, root=checkout)
    print("PASS shallow_source_commit_error_is_actionable")


def test_depth_one_merge_ref_requires_two_history_levels():
    with tempfile.TemporaryDirectory(prefix="latch-proof-merge-shallow-") as raw_dir:
        workspace = Path(raw_dir)
        source = workspace / "source"
        one_level = workspace / "one-level"
        two_levels = workspace / "two-levels"
        source.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=source, check=True)
        subprocess.run(
            ["git", "config", "user.email", "proof-packet@example.invalid"],
            cwd=source,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Proof Packet Test"],
            cwd=source,
            check=True,
        )
        (source / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=source, check=True)
        base_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True,
        ).strip()
        subprocess.run(["git", "checkout", "-qb", "proof"], cwd=source, check=True)
        tracked = source / "tracked.txt"
        tracked.write_text("tooling\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "tooling"], cwd=source, check=True)
        tooling_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True,
        ).strip()
        tooling_oid = subprocess.check_output(
            ["git", "rev-parse", f"{tooling_commit}:tracked.txt"],
            cwd=source,
            text=True,
        ).strip()
        for relative_path in proof_packet.PROOF_ARTIFACT_FILES:
            path = source / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"generated {relative_path}\n", encoding="utf-8")
        subprocess.run(["git", "add", "proof"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "artifacts"], cwd=source, check=True)
        subprocess.run(
            ["git", "checkout", "-qb", "merge-ref", base_commit],
            cwd=source,
            check=True,
        )
        (source / "main.txt").write_text("main\n", encoding="utf-8")
        subprocess.run(["git", "add", "main.txt"], cwd=source, check=True)
        subprocess.run(["git", "commit", "-qm", "main"], cwd=source, check=True)
        subprocess.run(
            ["git", "merge", "--no-ff", "proof", "-m", "merge proof"],
            cwd=source,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        receipt = {
            "source_commit": tooling_commit,
            "tested_runtime_git_blobs": {"tracked.txt": tooling_oid},
        }
        for checkout in (one_level, two_levels):
            subprocess.run(
                [
                    "git", "clone", "-q", "--depth=1", "--branch", "merge-ref",
                    source.as_uri(), str(checkout),
                ],
                check=True,
            )
        subprocess.run(["git", "fetch", "--deepen=1"], cwd=one_level, check=True)
        try:
            proof_packet.assert_source_commit_matches_manifest(
                receipt, root=one_level, require_direct_parent=True,
            )
        except proof_packet.ProofPacketError as exc:
            _assert("source_commit is unavailable" in str(exc), str(exc))
        else:
            raise AssertionError("one history level unexpectedly reached tooling")
        subprocess.run(["git", "fetch", "--deepen=2"], cwd=two_levels, check=True)
        proof_packet.assert_source_commit_matches_manifest(
            receipt, root=two_levels, require_direct_parent=True,
        )
    print("PASS depth_one_merge_ref_requires_two_history_levels")


def test_readme_selects_seeded_canonical_evidence():
    receipt = copy.deepcopy(proof_packet.load_live_receipt())
    receipt["evidence_nodes"].insert(0, {
        "id": 99,
        "kind": "fact",
        "title": "Supporting staging evidence",
        "status": "staging",
    })
    results = proof_packet.build_public_results(receipt)
    rendered = proof_packet.render_readme(results)
    expected = receipt["fixture"]["seeded_decision_id"]
    _assert(f"Cited canonical decision id={expected}" in rendered, rendered)
    _assert("id=99 fact status=staging" not in rendered, rendered)
    print("PASS readme_selects_seeded_canonical_evidence")


def test_readme_derives_fixture_count():
    receipt = proof_packet.load_live_receipt()
    results = proof_packet.build_public_results(receipt)
    results["wedge_v1"]["cases"] = 11
    rendered = proof_packet.render_readme(results)
    _assert("add value on these 11 fixtures" in rendered, rendered)
    _assert("add value on these eight fixtures" not in rendered, rendered)
    print("PASS readme_derives_fixture_count")


def test_root_readme_gate_verdict_matches_proof_packet():
    """Cross-surface guard (PR #37 review, node 2553).

    ``proof/`` is regenerated on every recapture, but the root ``README.md``
    proof table is hand-maintained.  Without this check a recapture that changes
    the live gate recommendation silently leaves ``README.md`` advertising a
    stale verdict.  This asserts the root README's live-gate row matches the
    generated packet.
    """
    import json
    import re

    results = json.loads((ROOT / "proof" / "results.json").read_text(encoding="utf-8"))
    packet_verdict = results["live_demo"]["recommendation"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(r"\|\s*Live pre-edit gate\s*\|\s*`([A-Z_]+)`", readme)
    _assert(match is not None, "root README has no 'Live pre-edit gate' proof row")
    readme_verdict = match.group(1)
    _assert(
        readme_verdict == packet_verdict,
        f"root README live-gate verdict {readme_verdict!r} != proof packet "
        f"{packet_verdict!r}; update README.md or recapture the packet",
    )
    print("PASS root_readme_gate_verdict_matches_proof_packet")


def test_failed_publication_preserves_last_good_packet():
    receipt = proof_packet.load_live_receipt()
    with tempfile.TemporaryDirectory(prefix="latch-proof-publish-test-") as raw_dir:
        output_dir = Path(raw_dir) / "proof"
        output_dir.mkdir()
        originals = {
            "live_gate_receipt.json": "old receipt\n",
            "results.json": "old results\n",
            "README.md": "old readme\n",
        }
        for name, body in originals.items():
            (output_dir / name).write_text(body, encoding="utf-8")
        with mock.patch.object(
            proof_packet, "generate_packet", side_effect=proof_packet.ProofPacketError("boom")
        ):
            try:
                proof_packet.publish_packet(receipt, output_dir=output_dir)
            except proof_packet.ProofPacketError:
                pass
            else:
                raise AssertionError("accepted a failed packet publication")
        actual = {
            name: (output_dir / name).read_text(encoding="utf-8")
            for name in originals
        }
        _assert(actual == originals, actual)
    print("PASS failed_publication_preserves_last_good_packet")


def test_failed_directory_swap_restores_last_good_packet():
    receipt = proof_packet.load_live_receipt()
    with tempfile.TemporaryDirectory(prefix="latch-proof-swap-test-") as raw_dir:
        output_dir = Path(raw_dir) / "proof"
        output_dir.mkdir()
        originals = {
            "live_gate_receipt.json": "old receipt\n",
            "results.json": "old results\n",
            "README.md": "old readme\n",
        }
        for name, body in originals.items():
            (output_dir / name).write_text(body, encoding="utf-8")
        real_replace = os.replace

        def fail_new_directory_swap(source, destination):
            source = Path(source)
            destination = Path(destination)
            if source.name.startswith(".latch-proof-publish-") and destination == output_dir:
                raise OSError("simulated directory swap failure")
            return real_replace(source, destination)

        with mock.patch.object(
            proof_packet.os, "replace", side_effect=fail_new_directory_swap
        ):
            try:
                proof_packet.publish_packet(receipt, output_dir=output_dir)
            except OSError:
                pass
            else:
                raise AssertionError("accepted a failed directory swap")
        actual = {
            name: (output_dir / name).read_text(encoding="utf-8")
            for name in originals
        }
        _assert(actual == originals, actual)
    print("PASS failed_directory_swap_restores_last_good_packet")


def test_generated_packet_matches_derived_eval_results():
    receipt = proof_packet.load_live_receipt()
    results = proof_packet.build_public_results(receipt)
    _assert(results["wedge_v1"]["ok"] is True, results["wedge_v1"])
    _assert(results["wedge_v1"]["modes"]["latch_evidence"]["passed"] ==
            results["wedge_v1"]["cases"], results["wedge_v1"])
    _assert(results["wedge_v1"]["modes"]["latch_evidence"]["passed"] >
            results["wedge_v1"]["modes"]["memory_like"]["passed"],
            results["wedge_v1"])
    _assert(results["seed_report_eval"]["passed"] ==
            results["seed_report_eval"]["checks"], results["seed_report_eval"])
    _assert("internal active-search-only ablation" in
            results["wedge_v1"]["baseline_boundary"], results["wedge_v1"])
    proof_packet.assert_public_safe(results, label="test results")
    print("PASS generated_packet_matches_derived_eval_results")


def test_packet_files_are_reproducible():
    proof_packet.check_generated(proof_packet.load_live_receipt())
    print("PASS packet_files_are_reproducible")


def test_wrapper_uses_configured_python():
    bash = shutil.which("bash")
    _assert(bash is not None, "bash is required for the shell wrapper test")
    env = dict(os.environ)
    env["LATCH_HOME"] = str(ROOT)
    env["LATCH_PYTHON"] = sys.executable
    # Prove the configured absolute interpreter is used: without it the
    # wrapper cannot discover a fallback Python from this deliberately empty
    # PATH.  This is portable to Git Bash on Windows, unlike relying on the
    # platform-specific availability of an external `echo` executable.
    env["PATH"] = ""
    result = subprocess.run(
        [bash, str(ROOT / "bin" / "latch_proof_packet.sh"), "--help"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _assert(result.returncode == 0, result.stderr)
    _assert("usage:" in result.stdout, result.stdout)
    _assert("--capture-live" in result.stdout, result.stdout)
    print("PASS wrapper_uses_configured_python")


def test_powershell_wrapper_forwards_interpreter_and_args():
    wrapper = (ROOT / "bin" / "latch_proof_packet.ps1").read_text(encoding="utf-8")
    _assert("$env:LATCH_PYTHON" in wrapper, wrapper)
    _assert('"src/proof_packet.py"' in wrapper, wrapper)
    _assert("@args" in wrapper, wrapper)
    print("PASS powershell_wrapper_forwards_interpreter_and_args")


def test_windows_workflow_propagates_proof_command_failures():
    workflow = (
        ROOT / ".github" / "workflows" / "public-release-hygiene.yml"
    ).read_text(encoding="utf-8")
    proof_job = workflow.split("  proof-packet-windows:", 1)[1].split(
        "\n  cursor-cumulative-full-suite:", 1,
    )[0]
    _assert(
        proof_job.count("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }") == 2,
        proof_job,
    )
    print("PASS windows_workflow_propagates_proof_command_failures")


if __name__ == "__main__":
    test_live_receipt_is_observed_and_complete()
    test_live_receipt_rejects_non_proof_states()
    test_live_receipt_rejects_raw_dirty_paths()
    test_live_receipt_rejects_private_coordinates()
    test_runtime_manifest_rejects_drift_without_git_history()
    test_runtime_manifest_covers_authoritative_bundle()
    test_runtime_manifest_excludes_ignored_files()
    test_runtime_manifest_rejects_vendor_asset_drift()
    test_runtime_manifest_canonicalizes_crlf_checkout()
    test_source_commit_must_contain_manifest_blobs()
    test_source_commit_must_directly_precede_generated_artifacts()
    test_source_and_artifact_pair_survives_a_merge_ref()
    test_shallow_source_commit_error_is_actionable()
    test_depth_one_merge_ref_requires_two_history_levels()
    test_readme_selects_seeded_canonical_evidence()
    test_readme_derives_fixture_count()
    test_root_readme_gate_verdict_matches_proof_packet()
    test_failed_publication_preserves_last_good_packet()
    test_failed_directory_swap_restores_last_good_packet()
    test_generated_packet_matches_derived_eval_results()
    test_packet_files_are_reproducible()
    test_wrapper_uses_configured_python()
    test_powershell_wrapper_forwards_interpreter_and_args()
    test_windows_workflow_propagates_proof_command_failures()
    print("\nAll proof packet tests pass.")
