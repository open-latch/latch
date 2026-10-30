"""Read-only extractor for the private scored tier (charter item 2b).

Two properties are the whole point and both are proved rather than documented:
the live vault is opened read-only, and the output cannot land inside the repo
tree where one ``git add -A`` would publish private KB bodies (id=4781).

The row-shaping half is tested against synthetic row mappings, never live text
— priority id=4114, and the gate ruling on this mission said the same.
"""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from latch.evals import adherence
from latch.evals import adherence_extract
from latch.store import db
from latch.store import paths


@pytest.fixture
def vault(tmp_path):
    """A populated fixture vault with a typed rejection, shaped like the real one."""
    root = paths.validated_test_root() / "vaults" / f"extract-{tmp_path.name}"
    root.mkdir(parents=True, exist_ok=True)
    import os

    old = os.environ.get("LATCH_KB_DIR")
    os.environ["LATCH_KB_DIR"] = str(root)
    paths.refresh_pinned_dir()
    try:
        conn = db.connect(str(root))
        try:
            node_id = db.insert_node(
                conn, kind="decision", title="No background writes",
                body="We rejected deferring writes past the response.",
                status="canonical",
            )
            conn.execute(
                "INSERT INTO rejected_path "
                "(node_id, option, reason, ratifier, decided_at, scope_predicate, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (node_id, "background writer thread",
                 "a write landing after the response cannot be reported to the caller",
                 "founder", "2026-01-02", "src/ledger/**", "declared"),
            )
            conn.commit()
        finally:
            conn.close()
    finally:
        if old is None:
            os.environ.pop("LATCH_KB_DIR", None)
        else:
            os.environ["LATCH_KB_DIR"] = old
        paths.refresh_pinned_dir()
    return root


# ---------- read-only is a property of the connection ----------

def test_the_extractor_connection_cannot_write(vault):
    conn = adherence_extract.open_readonly(vault)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO nodes (kind, title, body) VALUES ('fact', 't', 'b')")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM rejected_path")
    finally:
        conn.close()


def test_a_full_extract_leaves_the_source_vault_byte_identical(vault, tmp_path):
    from latch.evals import adherence_vault

    before = adherence_vault.vault_digest(vault)
    adherence_extract.extract(vault, tmp_path / "out" / "scored.jsonl", tree=Path("/nonexistent"))
    after = adherence_vault.vault_digest(vault)
    assert adherence_vault.digest_delta(before, after) == []


def test_extract_reads_the_rejected_path_bank(vault, tmp_path):
    summary = adherence_extract.extract(
        vault, tmp_path / "scored.jsonl", tree=Path("/nonexistent")
    )
    assert summary["rows"] == 1
    lines = (tmp_path / "scored.jsonl").read_text(encoding="utf-8").splitlines()
    draft = json.loads(lines[0])
    assert draft["draft"] is True
    assert draft["rejected_option"]["option"] == "background writer thread"
    assert draft["provenance"]["scope_predicate"] == "src/ledger/**"


def test_missing_database_is_refused(tmp_path):
    with pytest.raises(adherence_extract.ExtractError):
        adherence_extract.open_readonly(tmp_path)


# ---------- the destination guard (id=4781) ----------

def test_destination_inside_the_repo_tree_is_refused(tmp_path):
    tree = tmp_path / "repo"
    (tree / "benchmarks").mkdir(parents=True)
    with pytest.raises(adherence_extract.ExtractError) as excinfo:
        adherence_extract.validate_destination(tree / "benchmarks" / "scored.jsonl", tree=tree)
    assert "inside the repo tree" in str(excinfo.value)


def test_destination_equal_to_the_repo_root_is_refused(tmp_path):
    tree = tmp_path / "repo"
    tree.mkdir()
    with pytest.raises(adherence_extract.ExtractError):
        adherence_extract.validate_destination(tree, tree=tree)


def test_destination_outside_the_repo_tree_is_accepted(tmp_path):
    tree = tmp_path / "repo"
    tree.mkdir()
    outside = tmp_path / "elsewhere" / "scored.jsonl"
    assert adherence_extract.validate_destination(outside, tree=tree) == outside.resolve()


def test_there_is_no_default_destination(tmp_path):
    with pytest.raises(adherence_extract.ExtractError) as excinfo:
        adherence_extract.validate_destination(None)
    assert "deliberately no default" in str(excinfo.value)


def test_the_real_repo_root_is_what_the_default_guard_protects():
    """The guard's default tree is the checkout an `add -A` would sweep."""
    assert adherence_extract.repo_root() == Path(paths.KB_ROOT).resolve()


@pytest.mark.skipif(os.name == "nt", reason="mode bits are not a permission model on Windows")
def test_written_drafts_are_owner_readable_only(tmp_path):
    path = adherence_extract.write_drafts(
        [{"set_id": "x"}], tmp_path / "scored.jsonl", tree=Path("/nonexistent")
    )
    assert path.stat().st_mode & 0o077 == 0, "private KB residue must not be world-readable"


# ---------- row shaping, over synthetic rows only (id=4114) ----------

SYNTHETIC_ROW = {
    "rejected_path_id": 42,
    "node_id": 7,
    "option": "vendored parser",
    "reason": "it silently rounded large amounts",
    "ratifier": "founder",
    "decided_at": "2026-01-02",
    "scope_predicate": "src/ledger/**",
    "source": "declared",
    "node_title": "Payloads are parsed with the standard library",
    "node_body": "We rejected the vendored parser after two amounts were corrupted.",
    "node_kind": "decision",
    "node_status": "canonical",
}


def test_row_to_draft_carries_provenance_and_the_decision_text():
    draft = adherence_extract.row_to_draft(SYNTHETIC_ROW)
    assert draft["set_id"] == "mined-42"
    assert draft["decision"]["text"] == SYNTHETIC_ROW["node_body"]
    assert draft["provenance"]["rejected_path_id"] == 42
    assert draft["provenance"]["node_id"] == 7
    assert draft["provenance"]["ratifier"] == "founder"


def test_row_to_draft_leaves_the_probes_unauthored():
    draft = adherence_extract.row_to_draft(SYNTHETIC_ROW)
    assert draft["e3"]["request"].startswith(adherence_extract.DRAFT_REQUEST_PLACEHOLDER)
    assert draft["e4"]["request"].startswith(adherence_extract.DRAFT_REQUEST_PLACEHOLDER)
    assert draft["e3"]["forbidden_paths"] == []


def test_a_mined_draft_is_refused_by_the_runner():
    """End to end: nothing the extractor emits can be scored as-is."""
    draft = adherence_extract.row_to_draft(SYNTHETIC_ROW)
    with pytest.raises(adherence.AdherenceError) as excinfo:
        adherence.validate_probe_set(draft, "mined")
    assert "unauthored draft" in str(excinfo.value)
