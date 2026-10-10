"""Legacy per-cwd vaults survive the Windows resolved-spelling key.

Before v2.0.0 an unpinned install keyed a Windows drive cwd LEXICALLY
(``C:\\latch-junction\\alias`` -> ``c--latch-junction-alias``). v2.0.0 keys it
by its resolved spelling (junctions, symlinks, subst/mapped drives and 8.3
names expanded), so an existing populated vault must still be selected rather
than silently replaced by a new, empty one.

Production legacy mode is simulated: no test root, no pin, and a source
checkout under ``tmp_path`` whose ``projects/`` holds the per-cwd vaults.
Registry, backup and production roots are redirected under ``tmp_path``.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from latch.store import db  # noqa: E402
from latch.store import paths  # noqa: E402
from latch.store import vault_identity  # noqa: E402

import _win_pathspell  # noqa: E402

SCHEMA = ROOT / "src" / "latch" / "store" / "schema.sql"
ALIASES = {
    _win_pathspell.JUNCTION: _win_pathspell.JUNCTION_TARGET,
    _win_pathspell.SHORT_PROFILE: _win_pathspell.LONG_PROFILE,
}
# (cwd as a hook reports it, pre-v2.0.0 lexical key, resolved key)
SPELLINGS = [
    pytest.param(
        _win_pathspell.JUNCTION,
        "c--latch-junction-alias",
        "d--latch-target-repo",
        id="junction",
    ),
    pytest.param(
        _win_pathspell.SHORT_PROFILE + "\\repo",
        "c--Users-LATCHT~1-repo",
        "c--Users-latchtester-repo",
        id="short-name",
    ),
]


@pytest.fixture
def legacy_projects(monkeypatch, tmp_path) -> Path:
    for name in (
        paths.TEST_ROOT_ENV,
        paths.TEST_CAPABILITY_ENV,
        "LATCH_KB_DIR",
        "CLAUDE_KB_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(paths.PRODUCTION_DATA_ROOT_ENV, str(tmp_path / "production"))
    monkeypatch.setenv(paths.VAULT_REGISTRY_ROOT_ENV, str(tmp_path / "registry"))
    monkeypatch.setenv(paths.DURABILITY_ROOT_ENV, str(tmp_path / "backups"))
    source = tmp_path / "source"
    monkeypatch.setattr(paths, "KB_ROOT", source)
    monkeypatch.setattr(paths, "PROJECTS_ROOT", source / "projects")
    monkeypatch.setattr(paths, "_PINNED_DIR", None)
    return source / "projects"


def _legacy_vault(vault: Path, title: str) -> None:
    """A populated per-cwd vault as an older release left it."""
    vault.mkdir(parents=True)
    conn = sqlite3.connect(str(vault / "kb.db"))
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO nodes(kind,title,body,status) "
            "VALUES('decision',?,'must survive','canonical')",
            (title,),
        )
        conn.commit()
    finally:
        conn.close()


def _open(cwd: str) -> tuple[str, list[str]]:
    conn = db.connect(cwd)
    try:
        titles = [
            row[0]
            for row in conn.execute("SELECT title FROM nodes WHERE kind='decision'")
        ]
        return conn._kb_vault_identity.vault_uuid, titles
    finally:
        conn.close()


@pytest.mark.parametrize("cwd,lexical_key,resolved_key", SPELLINGS)
def test_populated_lexical_vault_keeps_identity_and_decisions(
    legacy_projects, cwd, lexical_key, resolved_key
):
    lexical = legacy_projects / lexical_key
    _legacy_vault(lexical, "legacy decision")
    with _win_pathspell.windows_realpath_host(ALIASES):
        # Tags (logs, request text) keep the canonical v2.0.0 key.
        assert paths.sanitize_cwd(cwd) == resolved_key
        assert paths.project_dir(cwd) == lexical
        seen = [_open(cwd) for _ in range(3)]
    assert len({vault_uuid for vault_uuid, _ in seen}) == 1
    assert all(titles == ["legacy decision"] for _, titles in seen)
    assert not (legacy_projects / resolved_key / "kb.db").exists()


@pytest.mark.parametrize("cwd,lexical_key,resolved_key", SPELLINGS)
def test_both_spellings_populated_refuses_with_pin_advice(
    legacy_projects, cwd, lexical_key, resolved_key
):
    lexical = legacy_projects / lexical_key
    resolved = legacy_projects / resolved_key
    _legacy_vault(lexical, "lexical decision")
    _legacy_vault(resolved, "resolved decision")
    before = {vault: (vault / "kb.db").read_bytes() for vault in (lexical, resolved)}
    with _win_pathspell.windows_realpath_host(ALIASES):
        with pytest.raises(RuntimeError, match="--kb-dir") as excinfo:
            db.connect(cwd)
        with pytest.raises(paths.LegacyVaultConflictError):
            paths.project_dir(cwd)
    message = str(excinfo.value)
    assert str(lexical) in message and str(resolved) in message
    assert {
        vault: (vault / "kb.db").read_bytes() for vault in (lexical, resolved)
    } == before


@pytest.mark.parametrize("cwd,lexical_key,resolved_key", SPELLINGS)
def test_unpopulated_lexical_spelling_uses_resolved_key(
    legacy_projects, cwd, lexical_key, resolved_key
):
    resolved = legacy_projects / resolved_key
    with _win_pathspell.windows_realpath_host(ALIASES):
        assert paths.project_dir(cwd) == resolved
        # Side files (logs, budget, markers) without a kb.db do not pin a key.
        (legacy_projects / lexical_key).mkdir(parents=True)
        assert paths.project_dir(cwd) == resolved
        _legacy_vault(resolved, "resolved decision")
        assert paths.project_dir(cwd) == resolved


@pytest.mark.parametrize("preexisting", [False, True], ids=["missing", "empty-file"])
def test_refused_checkout_vault_leaves_nothing_to_adopt(
    legacy_projects, tmp_path, preexisting
):
    cwd = tmp_path / "work" / "new-project"
    cwd.mkdir(parents=True)
    vault = paths.project_dir(str(cwd))
    assert vault.parent == legacy_projects
    db_file = vault / "kb.db"
    if preexisting:
        vault.mkdir(parents=True)
        db_file.write_bytes(b"")
    for _ in range(2):
        with pytest.raises(vault_identity.VaultSafetyError, match="source checkout"):
            db.connect(str(cwd))
    if preexisting:
        assert db_file.stat().st_size == 0
    else:
        assert not db_file.exists()
    assert not list((tmp_path / "registry").glob("*.json"))
