"""Tests for the artifact layer — Slice 1 (storage substrate + capture).

Covers the migration, repo canonicalization, upsert get-or-create + UNIQUE dedup
(including the repo-level '' sentinel), the provenance junction (multi/idempotent/
cascade), and capture_for_node's explicit-vs-fallback behaviour. Run directly:
    python tests/test_artifacts.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import _isolation  # noqa: F401,E402  (hermetic on a pinned KB for direct runs; see conftest)
import artifacts  # noqa: E402
import db          # noqa: E402
import search      # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_artifacts_test_")
    return tmp, db.connect(tmp)


def _cleanup(tmp, conn):
    conn.close()
    shutil.rmtree(tmp, ignore_errors=True)


def _node(conn, title="N", body="b"):
    return db.insert_node(conn, kind="fact", title=title, body=body, status="staging")


# ---------- migration ----------

def test_migration_creates_tables_idempotent():
    tmp, conn = _fresh_db()
    try:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        _assert("artifact" in names, "artifact table missing")
        _assert("node_artifact" in names, "node_artifact table missing")
        # connecting again must not error (CREATE ... IF NOT EXISTS)
        conn2 = db.connect(tmp)
        conn2.close()
        print("PASS test_migration_creates_tables_idempotent")
    finally:
        _cleanup(tmp, conn)


# ---------- canonicalization ----------

def test_canonicalize_repo_collapses_equivalents():
    forms = [
        "C:/Systems/latch",
        "/c/Systems/latch",
        "c:/Systems/latch",
        "C:\\Systems\\latch",
        "C:/Systems/latch/",
        "C:/Systems//latch",
    ]
    canon = {artifacts.canonicalize_repo(f) for f in forms}
    _assert(canon == {"C:/Systems/latch"}, f"forms did not collapse: {canon}")
    print("PASS test_canonicalize_repo_collapses_equivalents")


def test_canonicalize_repo_posix_passthrough():
    _assert(
        artifacts.canonicalize_repo("/Users/nico/repo/") == "/Users/nico/repo",
        "posix path mangled",
    )
    _assert(
        artifacts.canonicalize_repo("/home/x/proj") == "/home/x/proj",
        "posix path mangled",
    )
    print("PASS test_canonicalize_repo_posix_passthrough")


# ---------- upsert / UNIQUE ----------

def test_upsert_get_or_create():
    tmp, conn = _fresh_db()
    try:
        a1 = artifacts.upsert_artifact(conn, "C:/Systems/latch", "src/db.py")
        a2 = artifacts.upsert_artifact(conn, "/c/Systems/latch", "src/db.py")
        _assert(a1 == a2, "equivalent repo spellings must map to one coordinate")
        a3 = artifacts.upsert_artifact(conn, "C:/Systems/latch", "src/heal.py")
        _assert(a3 != a1, "different file must be a different coordinate")
        n = conn.execute("SELECT COUNT(*) c FROM artifact").fetchone()["c"]
        _assert(n == 2, f"expected 2 artifact rows, got {n}")
        print("PASS test_upsert_get_or_create")
    finally:
        _cleanup(tmp, conn)


def test_repo_only_dedups_via_empty_path_sentinel():
    tmp, conn = _fresh_db()
    try:
        a1 = artifacts.upsert_artifact(conn, "C:/Systems/latch")          # path None
        a2 = artifacts.upsert_artifact(conn, "C:/Systems/latch", None)
        _assert(a1 == a2, "repo-level coordinate must dedup (the '' sentinel)")
        n = conn.execute(
            "SELECT COUNT(*) c FROM artifact WHERE repo='C:/Systems/latch'"
        ).fetchone()["c"]
        _assert(n == 1, f"repo-level duplicated: {n} rows (NULL-uniqueness trap)")
        row = conn.execute("SELECT path FROM artifact WHERE id=?", (a1,)).fetchone()
        _assert(row["path"] == "", "repo-level path must be stored as ''")
        print("PASS test_repo_only_dedups_via_empty_path_sentinel")
    finally:
        _cleanup(tmp, conn)


# ---------- junction: multi / idempotent / read / cascade ----------

def test_link_and_get_multi_artifact():
    tmp, conn = _fresh_db()
    try:
        nid = _node(conn)
        artifacts.link_node_artifacts(conn, nid, [
            {"repo": "C:/Systems/latch", "path": "src/db.py"},
            {"repo": "C:/Systems/latch", "path": "src/heal.py"},
            ("C:/Systems/SurfaceAnalysis_v2", None),   # multi-repo + repo-level
            "C:/Systems/other",                         # bare repo string
        ])
        got = artifacts.get_node_artifacts(conn, nid)
        _assert(len(got) == 4, f"expected 4 coordinates, got {len(got)}")
        repos = {g["repo"] for g in got}
        _assert("C:/Systems/SurfaceAnalysis_v2" in repos, "multi-repo not captured")
        repo_level = [g for g in got if g["repo"] == "C:/Systems/other"][0]
        _assert(repo_level["path"] is None, "repo-level path must read back as None")
        print("PASS test_link_and_get_multi_artifact")
    finally:
        _cleanup(tmp, conn)


def test_link_idempotent():
    tmp, conn = _fresh_db()
    try:
        nid = _node(conn)
        artifacts.link_node_artifacts(conn, nid, [("C:/r", "f.py")])
        artifacts.link_node_artifacts(conn, nid, [("C:/r", "f.py")])  # again
        n = conn.execute(
            "SELECT COUNT(*) c FROM node_artifact WHERE node_id=?", (nid,)
        ).fetchone()["c"]
        _assert(n == 1, f"re-link must be idempotent, got {n} junction rows")
        print("PASS test_link_idempotent")
    finally:
        _cleanup(tmp, conn)


def test_junction_cascades_on_node_delete_artifact_survives():
    tmp, conn = _fresh_db()
    try:
        nid = _node(conn)
        aid = artifacts.upsert_artifact(conn, "C:/r", "f.py")
        artifacts.link_node_artifacts(conn, nid, [("C:/r", "f.py")])
        conn.execute("DELETE FROM nodes WHERE id=?", (nid,))
        conn.commit()
        j = conn.execute(
            "SELECT COUNT(*) c FROM node_artifact WHERE node_id=?", (nid,)
        ).fetchone()["c"]
        _assert(j == 0, "junction rows must cascade away with the node")
        a = conn.execute("SELECT COUNT(*) c FROM artifact WHERE id=?", (aid,)).fetchone()["c"]
        _assert(a == 1, "artifact coordinate must SURVIVE node delete (historical)")
        print("PASS test_junction_cascades_on_node_delete_artifact_survives")
    finally:
        _cleanup(tmp, conn)


# ---------- capture_for_node ----------

def test_capture_explicit_vs_fallback_vs_none():
    tmp, conn = _fresh_db()
    try:
        # explicit artifacts win over the project_cwd fallback
        n1 = _node(conn, title="explicit")
        artifacts.capture_for_node(
            conn, n1, artifacts=[("C:/Systems/latch", "src/gate.py")],
            project_cwd="C:/Systems/SurfaceAnalysis_v2",
        )
        g1 = artifacts.get_node_artifacts(conn, n1)
        _assert(len(g1) == 1 and g1[0]["repo"] == "C:/Systems/latch",
                "explicit artifacts must be used (not the cwd fallback)")

        # no explicit artifacts -> coarse repo=cwd fallback
        n2 = _node(conn, title="fallback")
        artifacts.capture_for_node(
            conn, n2, artifacts=None, project_cwd="C:/Systems/SurfaceAnalysis_v2",
        )
        g2 = artifacts.get_node_artifacts(conn, n2)
        _assert(len(g2) == 1 and g2[0]["repo"] == "C:/Systems/SurfaceAnalysis_v2"
                and g2[0]["path"] is None, "cwd fallback stamp missing/incorrect")

        # nothing to capture -> no rows
        n3 = _node(conn, title="empty")
        out = artifacts.capture_for_node(conn, n3, artifacts=None, project_cwd=None)
        _assert(out == [] and artifacts.get_node_artifacts(conn, n3) == [],
                "no artifacts + no cwd must capture nothing")
        print("PASS test_capture_explicit_vs_fallback_vs_none")
    finally:
        _cleanup(tmp, conn)


# ---------- Slice 2: auto-observe touched files ----------

def _write_transcript(dirpath: str, edits: list[dict]) -> str:
    """Minimal Claude Code JSONL transcript whose assistant turns carry tool_use
    edits. `edits`: list of {"name", "file_path"|"notebook_path"}. Appends a
    non-edit tool (Bash) and a malformed line — both must be ignored."""
    lines = []
    for e in edits:
        inp = {}
        if "file_path" in e:
            inp["file_path"] = e["file_path"]
        if "notebook_path" in e:
            inp["notebook_path"] = e["notebook_path"]
        lines.append(json.dumps({"type": "assistant", "message": {"role": "assistant",
            "content": [
                {"type": "text", "text": "working"},
                {"type": "tool_use", "name": e["name"], "input": inp},
            ]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}))
    lines.append("{ not valid json")
    path = os.path.join(dirpath, "transcript.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def _make_git_repo(dirpath: str, name: str) -> str:
    repo = os.path.join(dirpath, name)
    os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
    os.makedirs(os.path.join(repo, "src"), exist_ok=True)
    return repo


def test_observe_derives_repo_from_git_and_relpath():
    tmp, conn = _fresh_db()
    try:
        repo = _make_git_repo(tmp, "myrepo")
        f1 = os.path.join(repo, "src", "x.py")
        tpath = _write_transcript(tmp, [
            {"name": "Edit", "file_path": f1},
            {"name": "Edit", "file_path": f1},   # dup -> one coordinate
        ])
        obs = artifacts.observe_session_artifacts(tpath, project_cwd=tmp)
        _assert(len(obs) == 1, f"expected 1 deduped coordinate, got {obs}")
        _assert(obs[0]["repo"] == artifacts.canonicalize_repo(repo),
                f"repo should be the .git root, got {obs[0]['repo']}")
        _assert(obs[0]["path"] == "src/x.py",
                f"path should be repo-relative, got {obs[0]['path']}")
        print("PASS test_observe_derives_repo_from_git_and_relpath")
    finally:
        _cleanup(tmp, conn)


def test_observe_falls_back_to_project_cwd_without_git():
    tmp, conn = _fresh_db()
    try:
        sub = os.path.join(tmp, "plain")
        os.makedirs(sub, exist_ok=True)
        f1 = os.path.join(sub, "a.py")
        tpath = _write_transcript(tmp, [{"name": "Write", "file_path": f1}])
        obs = artifacts.observe_session_artifacts(tpath, project_cwd=tmp)
        _assert(len(obs) == 1, f"expected 1 coordinate, got {obs}")
        _assert(obs[0]["repo"] == artifacts.canonicalize_repo(tmp),
                f"repo should fall back to project_cwd, got {obs[0]['repo']}")
        _assert(obs[0]["path"] == "plain/a.py",
                f"path should be cwd-relative, got {obs[0]['path']}")
        print("PASS test_observe_falls_back_to_project_cwd_without_git")
    finally:
        _cleanup(tmp, conn)


def test_observe_missing_transcript_is_empty():
    _assert(artifacts.observe_session_artifacts(None, "C:/x") == [], "None transcript")
    _assert(artifacts.observe_session_artifacts("/no/such/file.jsonl", "C:/x") == [],
            "missing transcript file")
    print("PASS test_observe_missing_transcript_is_empty")


def test_attach_observed_to_session_nodes_idempotent():
    tmp, conn = _fresh_db()
    try:
        repo = _make_git_repo(tmp, "latchlike")
        f1 = os.path.join(repo, "src", "gate.py")
        tpath = _write_transcript(tmp, [{"name": "Edit", "file_path": f1}])
        n1 = db.insert_node(conn, kind="decision", title="d", body="b", session_id="S1")
        n2 = db.insert_node(conn, kind="fact", title="f", body="b", session_id="S1")
        other = db.insert_node(conn, kind="fact", title="o", body="b", session_id="S2")
        cnt = artifacts.attach_observed_artifacts(conn, "S1", tpath, project_cwd=tmp)
        _assert(cnt == 2, f"expected 2 session nodes enriched, got {cnt}")
        g1 = artifacts.get_node_artifacts(conn, n1)
        _assert(len(g1) == 1 and g1[0]["path"] == "src/gate.py"
                and g1[0]["repo"] == artifacts.canonicalize_repo(repo),
                f"n1 not enriched correctly: {g1}")
        _assert(len(artifacts.get_node_artifacts(conn, n2)) == 1, "n2 not enriched")
        _assert(artifacts.get_node_artifacts(conn, other) == [],
                "a different session's node must NOT be enriched")
        artifacts.attach_observed_artifacts(conn, "S1", tpath, project_cwd=tmp)  # re-run
        _assert(len(artifacts.get_node_artifacts(conn, n1)) == 1,
                "re-run must be idempotent")
        print("PASS test_attach_observed_to_session_nodes_idempotent")
    finally:
        _cleanup(tmp, conn)


# ---------- consumer: artifact-scoped retrieval boost ----------

def test_nodes_in_repo_subset():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="b")
        b = db.insert_node(conn, kind="fact", title="b", body="b")
        artifacts.link_node_artifacts(conn, a, [("C:/Systems/myrepo", "f.py")])
        artifacts.link_node_artifacts(conn, b, [("C:/Systems/other", "g.py")])
        got = search._nodes_in_repo(conn, [a, b], artifacts.canonicalize_repo("C:/Systems/myrepo"))
        _assert(got == {a}, f"expected only the same-repo node, got {got}")
        print("PASS test_nodes_in_repo_subset")
    finally:
        _cleanup(tmp, conn)


def test_scope_boost_lifts_same_repo():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="b")
        b = db.insert_node(conn, kind="fact", title="b", body="b")
        artifacts.link_node_artifacts(conn, a, [("C:/Systems/myrepo", "f.py")])
        rows = [{"id": b, "score": 0.10}, {"id": a, "score": 0.09}]  # b slightly ahead
        out = search.apply_scope_boost(conn, rows, "C:/Systems/myrepo")
        _assert(out[0]["id"] == a,
                f"same-repo node should rank first after boost, got {[r['id'] for r in out]}")
        _assert(abs(out[0]["score"] - 0.135) < 1e-9,
                f"score should be 0.09*(1+0.5)=0.135, got {out[0]['score']}")
        print("PASS test_scope_boost_lifts_same_repo")
    finally:
        _cleanup(tmp, conn)


def test_scope_boost_noop_when_none():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="b")
        artifacts.link_node_artifacts(conn, a, [("C:/Systems/myrepo", "f.py")])
        rows = [{"id": a, "score": 0.09}, {"id": 999, "score": 0.10}]
        before = [(r["id"], r["score"]) for r in rows]
        out = search.apply_scope_boost(conn, rows, None)
        _assert([(r["id"], r["score"]) for r in out] == before,
                "None scope_repo must be a byte-identical no-op")
        print("PASS test_scope_boost_noop_when_none")
    finally:
        _cleanup(tmp, conn)


def test_scope_boost_reach_through_preserved():
    tmp, conn = _fresh_db()
    try:
        a = db.insert_node(conn, kind="fact", title="a", body="b")   # same-repo, weak
        b = db.insert_node(conn, kind="fact", title="b", body="b")   # cross-repo, strong
        artifacts.link_node_artifacts(conn, a, [("C:/Systems/myrepo", "f.py")])
        artifacts.link_node_artifacts(conn, b, [("C:/Systems/other", "g.py")])
        rows = [{"id": b, "score": 0.50}, {"id": a, "score": 0.09}]
        out = search.apply_scope_boost(conn, rows, "C:/Systems/myrepo")
        _assert(out[0]["id"] == b,
                "a strongly-relevant cross-repo hit must stay above a boosted weak same-repo hit")
        print("PASS test_scope_boost_reach_through_preserved")
    finally:
        _cleanup(tmp, conn)


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
