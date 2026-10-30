"""Non-contamination guards for the S1a adherence harness (charter 5134 item 5).

The mission's one unrecoverable failure is a harness run writing into the live
vault: synthetic rows there pollute the V4/T1 denominator and corrupt a
measurement with no automatic extension (id=4963).

The load-bearing test drives the real CLI as a subprocess against a fabricated
production install — its own ``LATCH_HOME`` and its own ``kb_location.json``
pointing at a decoy vault — with the pytest test-root capability removed, so
``paths.project_dir()`` resolves exactly the way it does in production. Under
pytest the resolution is sandboxed and a passing in-process test would prove
nothing about the real thing.

Non-vacuity is proved the other way round: the same fabricated install, driven
by a script that skips the harness's repin, *does* mutate the decoy vault. The
decoy is genuinely reachable, so the harness leaving it byte-identical is a
result rather than an artifact of an unreachable target.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from _win_dirlink import link_dir
from latch.evals import adherence_vault
from latch.store import db
from latch.store import paths


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


# ---------- helpers ----------

def _fabricate_install(tmp_path: Path, decoy_vault: Path) -> Path:
    """A production-shaped install root pinned at the decoy vault."""
    install = tmp_path / "install"
    install.mkdir()
    for name in ("src", "vendor", "benchmarks", "docs"):
        link_dir(install / name, REPO_ROOT / name)
    (install / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(decoy_vault)}) + "\n", encoding="utf-8"
    )
    return install


def _production_env(install: Path, scratch: Path) -> dict:
    """Environment with the pytest sandbox removed, as production sees it.

    The vault-identity registry and platform state root are redirected into the
    test's own tmp tree. Without that, exercising production-mode vault
    creation would mint durable records in the developer's real
    ``<platform production root>/registry``.
    """
    env = dict(os.environ)
    for name in (
        "LATCH_TEST_ROOT", "LATCH_TEST_CAPABILITY", "LATCH_KB_DIR", "CLAUDE_KB_DIR",
    ):
        env.pop(name, None)
    env["LATCH_HOME"] = str(install)
    env["LATCH_ADHERENCE_SCRATCH"] = str(scratch)
    env["LATCH_PRODUCTION_DATA_ROOT"] = str(install.parent / "platform-state")
    env["LATCH_VAULT_REGISTRY_ROOT"] = str(install.parent / "platform-state" / "registry")
    return env


def _make_decoy_vault(tmp_path: Path) -> tuple[Path, Path]:
    """A populated stand-in for the production vault, plus its install root.

    Created by a production-mode subprocess rather than in-process: a vault
    built under the pytest test root is stamped with the ``test``
    classification, and ``vault_identity`` then refuses to open it from a
    production-mode process. The decoy has to be a real production vault for
    the guard test to mean anything.
    """
    decoy = tmp_path / "live-vault"
    decoy.mkdir()
    install = _fabricate_install(tmp_path, decoy)
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(install / 'src')!r})\n"
        "from latch.store import db, paths\n"
        "conn = db.connect(str(paths.project_dir()))\n"
        "db.insert_node(conn, kind='decision', title='decoy decision',\n"
        "               body='a row that must survive a harness run untouched',\n"
        "               status='canonical')\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(bootstrap)], capture_output=True, text=True, timeout=300,
        env=_production_env(install, tmp_path / "scratch"), cwd=str(install),
    )
    assert proc.returncode == 0, proc.stderr
    # A gate log, so the charter's literal "gate-log row count" criterion has
    # something to be byte-identical about.
    (decoy / "gate-2026-08-10.log").write_text(
        json.dumps({"gate_call_id": "decoy", "recommendation": "PROCEED"}) + "\n",
        encoding="utf-8",
    )
    return decoy, install


def _run_cli(install: Path, scratch: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        # These tests exercise isolation, not capacity. Pinning the headroom
        # floor to 0 keeps them from failing on a developer box or a CI runner
        # that happens to be below the charter's 10 GiB — the floor itself has
        # its own dedicated tests.
        [sys.executable, str(install / "src" / "latch" / "evals" / "adherence_cli.py"), *args,
         "--min-free-gib", "0"],
        capture_output=True, text=True, timeout=900,
        env=_production_env(install, scratch),
        cwd=str(install),
    )


# ---------- charter item 5(a): the live vault is untouched ----------

@pytest.mark.s1a_smoke
def test_live_vault_is_byte_identical_across_a_full_harness_run(tmp_path):
    decoy, install = _make_decoy_vault(tmp_path)
    scratch = tmp_path / "scratch"

    before = adherence_vault.vault_digest(decoy)
    proc = _run_cli(install, scratch, "run", "--tier", "smoke")
    assert proc.returncode == 0, proc.stderr
    after = adherence_vault.vault_digest(decoy)

    assert adherence_vault.digest_delta(before, after) == []
    assert before["files"] == after["files"]
    assert before["tables"] == after["tables"]
    # The charter's literal criterion, asserted on the bytes themselves.
    gate_log = decoy / "gate-2026-08-10.log"
    assert before["files"][gate_log.name]["sha256"] == after["files"][gate_log.name]["sha256"]
    assert len(gate_log.read_text(encoding="utf-8").splitlines()) == 1

    result = json.loads(proc.stdout)
    # The digest is corroborating evidence; this is the primary evidence,
    # because it cannot be confounded by concurrent activity elsewhere.
    opened = result["guards"]["databases_opened"]
    assert opened, "the run must actually open databases, or the guard is vacuous"
    for entry in opened:
        assert not Path(entry["path"]).is_relative_to(decoy), entry
    assert result["guards"]["install_fingerprint_unchanged"] is True
    assert result["parameters"]["protocol"]["matches_pin"] is True


@pytest.mark.s1a_smoke
def test_the_decoy_vault_is_reachable_so_the_guard_is_not_vacuous(tmp_path):
    """Non-vacuity: without the harness's repin, the same install DOES write.

    Proves the decoy sits exactly where an unguarded run would land, so the
    byte-identity above is a property of the harness and not of an unreachable
    target.
    """
    decoy, install = _make_decoy_vault(tmp_path)
    script = tmp_path / "unguarded.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(install / 'src')!r})\n"
        "from latch.store import db, paths\n"
        "conn = db.connect(str(paths.project_dir()))\n"
        "db.insert_node(conn, kind='fact', title='unguarded', body='wrote to the pinned vault')\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    before = adherence_vault.vault_digest(decoy)
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=300,
        env=_production_env(install, tmp_path / "scratch"), cwd=str(install),
    )
    assert proc.returncode == 0, proc.stderr
    after = adherence_vault.vault_digest(decoy)

    deltas = adherence_vault.digest_delta(before, after)
    assert deltas, "an unguarded write must be visible to the digest"
    assert any("table changed: nodes" in d for d in deltas), deltas


@pytest.mark.s1a_smoke
def test_a_run_leaves_no_records_in_the_platform_vault_registry(tmp_path):
    """Regression: creating a vault mints a durable identity record.

    Left at its default, every ephemeral vault leaked one orphan record into
    the developer's real ``<platform production root>/registry`` — six per run,
    outliving the vault by design. That is the throwaway-artefact growth
    3865's execution-order note put ahead of S1a, so ``ephemeral_vault``
    redirects the registry into the scratch root, where teardown reclaims it.
    """
    decoy, install = _make_decoy_vault(tmp_path)
    platform_state = Path(_production_env(install, tmp_path / "scratch")["LATCH_PRODUCTION_DATA_ROOT"])
    registry = platform_state / "registry"
    before = set(registry.glob("*.json")) if registry.exists() else set()

    proc = _run_cli(install, tmp_path / "scratch", "run", "--tier", "smoke")
    assert proc.returncode == 0, proc.stderr

    after = set(registry.glob("*.json")) if registry.exists() else set()
    assert after == before, (
        f"the run minted {len(after - before)} vault-identity records outside "
        f"its own scratch root"
    )
    # And the scratch root is gone too, so the redirected records went with it.
    assert not (tmp_path / "scratch").exists() or not any(
        (tmp_path / "scratch").rglob("*.json")
    )


# ---------- the tripwire itself ----------

def test_tripwire_refuses_a_database_outside_the_scratch_root(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "elsewhere.db"
    with adherence_vault.db_open_tripwire(scratch_root=scratch):
        with pytest.raises(adherence_vault.ContaminationError) as excinfo:
            sqlite3.connect(str(outside))
    assert "outside its scratch root" in str(excinfo.value)


def test_tripwire_allows_a_database_inside_the_scratch_root(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with adherence_vault.db_open_tripwire(scratch_root=scratch) as opened:
        sqlite3.connect(str(scratch / "ok.db")).close()
    assert [Path(e["path"]).name for e in opened] == ["ok.db"]


def test_tripwire_allows_only_readonly_opens_of_the_live_vault(tmp_path):
    """Charter hard edge 3: one bounded read-only extractor, nothing else."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    sqlite3.connect(str(live / "kb.db")).close()

    with adherence_vault.db_open_tripwire(scratch_root=scratch, readonly_roots=(live,)):
        conn = sqlite3.connect(f"{(live / 'kb.db').as_uri()}?mode=ro", uri=True)
        conn.close()
        with pytest.raises(adherence_vault.ContaminationError):
            sqlite3.connect(str(live / "kb.db"))


def test_tripwire_is_removed_on_exit(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    original = sqlite3.connect
    with adherence_vault.db_open_tripwire(scratch_root=scratch):
        assert sqlite3.connect is not original
    assert sqlite3.connect is original


# ---------- assert_isolated: the id=1556 trap ----------

def test_assert_isolated_rejects_a_pin_outside_the_scratch_root(tmp_path):
    with pytest.raises(adherence_vault.ContaminationError) as excinfo:
        adherence_vault.assert_isolated(tmp_path / "not-where-the-pin-points")
    assert "did not take effect" in str(excinfo.value)


def test_ephemeral_vault_repins_and_restores(tmp_path):
    scratch = paths.validated_test_root() / "vaults" / "adherence-unit"
    before_pin = paths.project_dir()
    before_env = os.environ.get("LATCH_KB_DIR")
    with adherence_vault.ephemeral_vault(scratch_root=scratch, label="unit") as vault:
        assert vault.is_dir()
        assert paths.project_dir() == vault
    assert os.environ.get("LATCH_KB_DIR") == before_env
    assert paths.project_dir() == before_pin
    assert not vault.exists(), "charter item 5(b): the vault is torn down"


# ---------- the recursive-delete boundary ----------

def test_purge_refuses_a_target_outside_the_scratch_root(tmp_path):
    """The harness creates and destroys vaults in a loop, which is the worst
    possible place for a path bug — so teardown fails closed."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")

    with pytest.raises(adherence_vault.ContaminationError):
        adherence_vault.purge_scratch_path(outside, scratch_root=scratch)
    assert (outside / "keep.txt").exists()


def test_purge_refuses_the_scratch_root_itself(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with pytest.raises(adherence_vault.ContaminationError):
        adherence_vault.purge_scratch_path(scratch, scratch_root=scratch)
    assert scratch.exists()


def test_purge_refuses_a_symlink_escaping_the_scratch_root(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    link_dir(scratch / "sneaky", outside)

    with pytest.raises(adherence_vault.ContaminationError):
        adherence_vault.purge_scratch_path(scratch / "sneaky", scratch_root=scratch)
    assert (outside / "keep.txt").exists()


def test_purge_removes_a_genuine_scratch_child(tmp_path):
    scratch = tmp_path / "scratch"
    child = scratch / "vault-abc"
    child.mkdir(parents=True)
    (child / "kb.db").write_text("x", encoding="utf-8")
    adherence_vault.purge_scratch_path(child, scratch_root=scratch)
    assert not child.exists() and scratch.exists()


# ---------- charter item 5(b): teardown ----------

def test_scratch_session_is_torn_down(tmp_path):
    with adherence_vault.scratch_session(scratch_root=tmp_path) as session:
        (session / "artifact").write_text("x", encoding="utf-8")
        assert session.is_dir()
    assert not session.exists()


def test_scratch_session_can_be_kept_for_debugging(tmp_path):
    with adherence_vault.scratch_session(scratch_root=tmp_path, keep=True) as session:
        pass
    assert session.is_dir()
    shutil.rmtree(session)


# ---------- charter item 5(c): disk headroom preflight ----------

def test_disk_headroom_preflight_aborts_below_the_floor(tmp_path):
    available = adherence_vault.free_bytes(tmp_path)
    with pytest.raises(adherence_vault.PreflightError) as excinfo:
        adherence_vault.require_disk_headroom(tmp_path, minimum=available + adherence_vault.GIB)
    assert "insufficient disk headroom" in str(excinfo.value)


def test_disk_headroom_preflight_passes_with_room(tmp_path):
    assert adherence_vault.require_disk_headroom(tmp_path, minimum=1) > 0


def test_default_floor_is_ten_gib():
    """Charter item 5(c) names the number; pin it so a drift is a test failure."""
    assert adherence_vault.MIN_FREE_BYTES == 10 * 1024 ** 3


# ---------- gate ruling (b): the install fingerprint ----------

def test_install_fingerprint_changes_when_a_fingerprinted_file_changes(tmp_path):
    install = tmp_path / "install"
    gate_py = install / "src" / "latch" / "gate" / "gate.py"
    gate_py.parent.mkdir(parents=True)
    (install / "kb_location.json").write_text('{"kb_dir": "/x"}', encoding="utf-8")
    gate_py.write_text("original", encoding="utf-8")

    before = adherence_vault.install_fingerprint(install)
    gate_py.write_text("modified", encoding="utf-8")
    assert adherence_vault.install_fingerprint(install) != before


def test_install_fingerprint_covers_the_pin_file(tmp_path):
    install = tmp_path / "install"
    (install / "src").mkdir(parents=True)
    (install / "kb_location.json").write_text('{"kb_dir": "/x"}', encoding="utf-8")

    before = adherence_vault.install_fingerprint(install)
    (install / "kb_location.json").write_text('{"kb_dir": "/repointed"}', encoding="utf-8")
    assert adherence_vault.install_fingerprint(install) != before


# ---------- the live vault is never resolved from a harness repin ----------

def test_live_vault_dir_is_none_under_pytest(tmp_path):
    """The suite must never be able to find a production vault to touch."""
    assert adherence_vault.live_vault_dir() is None
