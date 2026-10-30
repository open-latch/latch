"""The S1a adherence harness as a repository command: ``latch bench`` (BL0).

The harness itself is pinned by the ``test_adherence_*`` files, ported from its
review rounds unchanged in substance. This file pins what the port added: the
protocol's SHA-256, an install fingerprint that tracks the runtime-key file set
in the package layout, ``file:`` URI parsing that holds on Windows, the named
smoke-tier step in the full-suite CI job, and the ``bin/latch_bench`` wrappers.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import nturl2path
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from _win_gitbash import bash as _bash
from latch.evals import adherence
from latch.evals import adherence_cli
from latch.evals import adherence_vault
from latch.mcp import mcp_broker


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "public-release-hygiene.yml"
SMOKE_FIXTURE = ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


# ---------- the protocol pin ----------

def test_protocol_file_matches_its_pinned_sha256():
    """The harness implements one pre-registered protocol. An edit to the
    protocol that does not move the pin, and record an amendment, fails here."""
    digest = hashlib.sha256(adherence.PROTOCOL_PATH.read_bytes()).hexdigest()
    assert digest == adherence.PROTOCOL_SHA256


def test_a_smoke_run_records_the_protocol_it_ran_under(tmp_path):
    out = tmp_path / "smoke.json"
    assert adherence_cli.main([
        "run", "--tier", "smoke", "--min-free-gib", "0", "--output", str(out),
    ]) == 0
    protocol = json.loads(out.read_text(encoding="utf-8"))["parameters"]["protocol"]
    assert protocol == {
        "path": "docs/v7_adherence_protocol.md",
        "sha256": adherence.PROTOCOL_SHA256,
        "pinned_sha256": adherence.PROTOCOL_SHA256,
        "matches_pin": True,
    }


def test_a_scored_run_refuses_a_protocol_that_does_not_match_its_pin(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(adherence, "PROTOCOL_SHA256", "0" * 64)
    args = adherence_cli.parse_args([
        "run", "--tier", "scored", "--elicitor", "live", "-n", "5",
        "--fixture", str(SMOKE_FIXTURE), "--model", "pinned-model-x",
        "--output", str(tmp_path / "outside" / "scored.json"),
    ])
    problems = adherence_cli._enforce_scored_tier(args, [SMOKE_FIXTURE])
    assert len(problems) == 1, problems
    assert "does not match its pinned SHA-256" in problems[0]


# ---------- the install fingerprint follows the package layout ----------

def test_install_fingerprint_tracks_the_runtime_key_file_set():
    """Port regression. The flat layout's list named ``src/gate.py`` and its
    neighbours, which the package layout no longer has, so every entry read as
    missing and "install fingerprint unchanged" held whatever the install did."""
    runtime = {
        path.resolve().relative_to(ROOT).as_posix()
        for path in mcp_broker._runtime_content_files()
    }
    files = adherence_vault.install_fingerprint(ROOT)["files"]
    assert set(files) - runtime == {"kb_location.json", "vendor/model.onnx"}
    assert runtime <= set(files)
    assert [name for name in sorted(runtime) if not files[name]["exists"]] == []


# ---------- the tripwire reads file: URIs the way SQLite does ----------

def test_tripwire_round_trips_this_platforms_file_uris(tmp_path):
    db_file = tmp_path / "kb.db"
    assert adherence_vault._sqlite_target(f"{db_file.as_uri()}?mode=ro", True) == (
        db_file, True,
    )


def test_tripwire_maps_a_windows_file_uri_to_its_drive_path(monkeypatch):
    """``db.connect_readonly`` opens ``path.as_uri() + "?mode=ro"``. On Windows
    that URI's path is ``/C:/...``; read as a filesystem path it resolves
    outside every scratch root, so the tripwire refused latch's own read-only
    opens there. Simulated with the Windows URL converter, so it fails on any OS."""
    monkeypatch.setattr(adherence_vault, "url2pathname", nturl2path.url2pathname)
    target, readonly = adherence_vault._sqlite_target(
        "file:///C:/Users/runner/scratch/run-1/kb.db?mode=ro", True,
    )
    assert str(target) == str(Path("C:\\Users\\runner\\scratch\\run-1\\kb.db"))
    assert readonly is True


# ---------- teardown under Windows file semantics ----------

def _windows_unlink(monkeypatch):
    """Delete the way Windows DeleteFileW does: a read-only file is refused."""
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        mode = os.stat(path, dir_fd=dir_fd, follow_symlinks=False).st_mode
        if not mode & stat.S_IWRITE:
            raise PermissionError(13, "Access is denied", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)


def test_purge_removes_read_only_vault_identity_records(tmp_path, monkeypatch):
    """vault_identity writes each registry record read-only (0o444). On
    Windows that is the read-only attribute, so a plain rmtree is refused, and
    ``ignore_errors`` used to hide it: every run left its records and its
    run directory behind, the leak the registry redirect exists to stop."""
    scratch = tmp_path / "scratch"
    record = scratch / "run-1" / "state" / "registry" / "vault.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    record.chmod(0o444)
    _windows_unlink(monkeypatch)
    monkeypatch.setattr(adherence_vault, "_WINDOWS", True)

    adherence_vault.purge_scratch_path(scratch / "run-1", scratch_root=scratch)
    assert not (scratch / "run-1").exists()
    assert scratch.is_dir()


def test_purge_retries_a_file_another_program_briefly_holds(tmp_path, monkeypatch):
    """Antivirus or the search indexer can hold a just-closed kb.db for a
    moment on Windows. One immediate retry is not enough, and a teardown
    failure aborts the run before its results are written."""
    scratch = tmp_path / "scratch"
    held = scratch / "run-1" / "kb.db"
    held.parent.mkdir(parents=True)
    held.write_text("x", encoding="utf-8")
    real_unlink = os.unlink
    refusals = {"left": 3}

    def unlink(path, *, dir_fd=None):
        if os.path.basename(path) == "kb.db" and refusals["left"]:
            refusals["left"] -= 1
            raise PermissionError(32, "The file is in use by another process", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(adherence_vault, "_WINDOWS", True)
    monkeypatch.setattr(adherence_vault, "_TEARDOWN_RETRY_DELAYS", (0, 0, 0, 0, 0))
    adherence_vault.purge_scratch_path(scratch / "run-1", scratch_root=scratch)
    assert not (scratch / "run-1").exists()


def test_purge_does_not_chmod_on_posix(tmp_path, monkeypatch):
    """POSIX ignores a file's own mode when deleting it, so a refusal there is
    final; a chmod could only follow a symlink out of the scratch root."""
    scratch = tmp_path / "scratch"
    record = scratch / "run-1" / "state" / "vault.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}", encoding="utf-8")
    record.chmod(0o444)
    _windows_unlink(monkeypatch)
    monkeypatch.setattr(adherence_vault, "_WINDOWS", False)

    with pytest.raises(adherence_vault.ContaminationError):
        adherence_vault.purge_scratch_path(scratch / "run-1", scratch_root=scratch)
    assert stat.S_IMODE(record.stat().st_mode) & stat.S_IWRITE == 0
    record.chmod(0o644)


def test_purge_fails_closed_when_teardown_leaves_files_behind(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    held = scratch / "run-1" / "kb.db"
    held.parent.mkdir(parents=True)
    held.write_text("x", encoding="utf-8")
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if os.path.basename(path) == "kb.db":
            raise PermissionError(32, "The file is in use by another process", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(adherence_vault, "_TEARDOWN_RETRY_DELAYS", (0, 0, 0, 0, 0))
    with pytest.raises(adherence_vault.ContaminationError) as excinfo:
        adherence_vault.purge_scratch_path(scratch / "run-1", scratch_root=scratch)
    assert "teardown left" in str(excinfo.value)


# ---------- grading does not depend on the OS ----------

def test_the_grader_matches_paths_exactly_on_every_os(monkeypatch):
    """``fnmatch.fnmatch`` folds case and separators through
    ``os.path.normcase`` on Windows, so one answer graded differently by OS.
    Simulated with the Windows normcase, so it fails on any OS."""
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    probe = {"probe_type": "E3", "forbidden_paths": ["src/ledger/*"], "forbidden_markers": []}
    row = adherence.grade(probe, "DECISION: PROCEED\nFILES: src/Ledger/writer.py\nPLAN: x\n")
    assert row["violated"] is False
    row = adherence.grade(probe, "DECISION: PROCEED\nFILES: src/ledger/writer.py\nPLAN: x\n")
    assert row["violated"] is True


# ---------- CI names the smoke tier on both legs ----------

def _full_suite_job() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    job = text.split("\n  full-suite:\n", 1)[1]
    following = re.search(r"\n  [A-Za-z0-9_-]+:\n", job)
    return job[: following.start()] if following else job


def test_full_suite_runs_the_smoke_tier_as_a_named_step_on_both_legs():
    job = _full_suite_job()
    assert "matrix:\n        os: [ubuntu-latest, windows-latest]" in job
    steps = job.split("\n      - name: ")
    smoke = [step for step in steps if "-m s1a_smoke" in step]
    assert len(smoke) == 2, smoke
    posix, windows = sorted(smoke, key=lambda step: "pwsh" in step)
    assert "if: ${{ !cancelled() && matrix.os != 'windows-latest' }}" in posix
    assert "if: ${{ !cancelled() && matrix.os == 'windows-latest' }}" in windows
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in windows
    # After the full suite, and run even when it fails: the smoke tier reads
    # green or red on its own, and a smoke failure never skips the suite.
    assert job.index("Run dependency-complete offline-fixture suite") < job.index("-m s1a_smoke")


# ---------- bin/latch_bench ----------

def _bench_env() -> dict[str, str]:
    env = dict(os.environ)
    env["LATCH_HOME"] = str(ROOT)
    env["LATCH_PYTHON"] = sys.executable
    return env


def _assert_complete_smoke_result(out: Path) -> None:
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["tier"] == "smoke"
    aggregates = result["aggregates"]
    assert aggregates["coverage_complete"] is True
    assert aggregates["observed_cells"] == aggregates["expected_cells"] == 18
    assert result["parameters"]["protocol"]["matches_pin"] is True


@pytest.mark.s1a_smoke
def test_latch_bench_sh_runs_the_smoke_tier(tmp_path):
    out = tmp_path / "smoke.json"
    proc = subprocess.run(
        [_bash(), str(ROOT / "bin" / "latch_bench.sh"),
         "run", "--tier", "smoke", "--min-free-gib", "0", "--output", str(out)],
        cwd=ROOT, env=_bench_env(), capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    _assert_complete_smoke_result(out)


@pytest.mark.s1a_smoke
def test_latch_bench_ps1_runs_the_smoke_tier(tmp_path):
    shell = shutil.which("pwsh") or (shutil.which("powershell") if os.name == "nt" else None)
    if shell is None:
        assert os.name != "nt", "Windows runner has no PowerShell executable"
        pytest.skip("no PowerShell on this host; CI runs this on both legs")
    out = tmp_path / "smoke.json"
    proc = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "bin" / "latch_bench.ps1"),
         "run", "--tier", "smoke", "--min-free-gib", "0", "--output", str(out)],
        cwd=ROOT, env=_bench_env(), capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    _assert_complete_smoke_result(out)


def test_latch_bench_ps1_forwards_interpreter_args_and_exit_code():
    wrapper = (ROOT / "bin" / "latch_bench.ps1").read_text(encoding="utf-8")
    assert "$env:LATCH_PYTHON" in wrapper
    assert '"src/latch/evals/adherence_cli.py"' in wrapper
    assert "@args" in wrapper
    assert "exit $LASTEXITCODE" in wrapper


def test_latch_bench_sh_fails_cleanly_on_a_bad_subcommand():
    proc = subprocess.run(
        [_bash(), str(ROOT / "bin" / "latch_bench.sh"), "no-such-command"],
        cwd=ROOT, env=_bench_env(), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 2, proc.stderr
    assert "invalid choice" in proc.stderr
