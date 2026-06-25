"""Artifact (repo + file) provenance for KB nodes — Slice 1: storage substrate + capture.

An *artifact* is a COORDINATE, not knowledge — the repo and (optionally) file a
node's work touched. It has no body, no embedding, no lifecycle of its own; it
labels *where* knowledge was OBSERVED or produced — provenance EVIDENCE, NOT an
assertion of where the claim applies (artifacts are evidence, not law). It
therefore lives in a normalized side
structure (db._migrate_artifacts), NOT as columns on `nodes` and NOT as graph
nodes/edges (KB id=1515 / id=1516):

  * `artifact(id, repo, path, status, missing_since, successor_id)` — the shared
    coordinate dimension, keyed UNIQUE(repo, path). Does NOT cascade on node
    delete (coordinates are historical, shared, outlive any single node).
  * `node_artifact(node_id, artifact_id)` — append-only provenance junction;
    DOES cascade on node delete (provenance dies with its node).

Repos and files are SEPARATE dimensions, both multi-valued (id=1504): repo is the
coarse scope-EVIDENCE key (stable, enumerable) — used to CAUTION heal across
disjoint provenance, never to hard-partition the KB; file is the fine retrieval seed
(many, churny). A file row carries its repo; a repo can be recorded with no file.
Store the finest LEAF set, never a rolled-up ancestor (id=1515) — an ancestor
monotonically collapses precision and behaves like a hub (the thing id=1474
down-weights); hierarchy is a read-time expansion ladder, not stored broadening.
Repo strings are canonicalized so `C:/x`, `/c/x`, `c:/x`, `C:\\x` collapse to one
coordinate (the sanitize_cwd drive-letter lesson, id=307).

Slice 1 is storage + capture ONLY. The consumers read these tables in later
slices and change nothing until they do (minimal forward-compatible interface,
id=510): scope-partitioned heal, artifact-first retrieval seeding (id=1507),
rarity-weighted clustering affinity (id=1522), workstream auto-detection
(id=1506). The lifecycle columns (status / missing_since / successor_id) are
present from Slice 1 so the liveness + rename slice (id=1517) needs no migration;
they are unused until then.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable

import paths

LIVE = "live"
STALE = "stale"

# repo-level coordinate (no specific file) is stored as '' — NOT NULL — because
# SQLite treats NULLs as distinct in a UNIQUE index, so a nullable path would let
# duplicate repo-level coordinates slip past UNIQUE(repo, path). '' is the
# repo-level sentinel; the API exposes it back to callers as None.
_REPO_LEVEL = ""


def canonicalize_repo(repo: str) -> str:
    """Canonical form of a repo path so equivalent spellings collapse to one
    coordinate: MINGW `/c/x` -> `C:/x`, backslashes -> `/`, repeated slashes
    collapsed, trailing slash stripped, Windows drive letter upper-cased.

    Lexical and filesystem-independent (deterministic; safe when the path is not
    present on this machine, and in tests). POSIX paths pass through unchanged
    apart from separator/trailing-slash tidy-up.
    """
    s = paths._normalize_input_path(str(repo).strip())
    s = s.replace("\\", "/")
    s = re.sub(r"/{2,}", "/", s)
    if len(s) > 1:
        s = s.rstrip("/")
    s = re.sub(r"^([a-zA-Z]):", lambda m: m.group(1).upper() + ":", s)
    return s


def _canonical_path(path: str | None) -> str:
    """Tidy a file path to the repo-relative-or-absolute leaf form we store.
    Returns the repo-level sentinel ('') for None/empty. We do NOT roll up to an
    ancestor (id=1515) — the leaf is stored verbatim apart from separator tidy."""
    if path is None:
        return _REPO_LEVEL
    p = str(path).strip().replace("\\", "/")
    p = re.sub(r"/{2,}", "/", p)
    return p or _REPO_LEVEL


def _coerce(a) -> tuple[str | None, str | None]:
    """Accept an artifact spec as a {'repo','path'} dict, a (repo, path) /
    (repo,) tuple/list, or a bare repo string. Returns (repo, path|None)."""
    if isinstance(a, dict):
        return a.get("repo"), a.get("path")
    if isinstance(a, (list, tuple)):
        repo = a[0] if len(a) >= 1 else None
        path = a[1] if len(a) >= 2 else None
        return repo, path
    return a, None


def upsert_artifact(conn: sqlite3.Connection, repo: str, path: str | None = None) -> int:
    """Get-or-create the artifact coordinate (canonicalized repo + leaf path).
    Returns its id. Idempotent on UNIQUE(repo, path)."""
    repo = canonicalize_repo(repo)
    if not repo:
        raise ValueError("artifact requires a non-empty repo")
    path = _canonical_path(path)
    row = conn.execute(
        "SELECT id FROM artifact WHERE repo = ? AND path = ?", (repo, path),
    ).fetchone()
    if row is not None:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO artifact (repo, path) VALUES (?, ?)", (repo, path),
    )
    conn.commit()
    return cur.lastrowid


def link_node_artifacts(
    conn: sqlite3.Connection, node_id: int, artifacts: Iterable,
) -> list[int]:
    """Attach artifact coordinates to a node (append-only provenance). `artifacts`
    is an iterable of dicts / tuples / repo-strings (see _coerce). Idempotent —
    re-linking the same coordinate is a no-op. Returns the linked artifact ids."""
    ids: list[int] = []
    for a in artifacts or []:
        repo, path = _coerce(a)
        if not repo or not str(repo).strip():
            continue
        aid = upsert_artifact(conn, repo, path)
        conn.execute(
            "INSERT OR IGNORE INTO node_artifact (node_id, artifact_id) VALUES (?, ?)",
            (node_id, aid),
        )
        ids.append(aid)
    conn.commit()
    return ids


def get_node_artifacts(
    conn: sqlite3.Connection, node_id: int, *, include_stale: bool = True,
) -> list[dict]:
    """The artifact coordinates attached to a node. `path` is returned as None for
    repo-level coordinates (the '' sentinel is an internal storage detail)."""
    q = (
        "SELECT a.id, a.repo, a.path, a.status, a.missing_since, a.successor_id "
        "FROM node_artifact na JOIN artifact a ON a.id = na.artifact_id "
        "WHERE na.node_id = ?"
    )
    params: list = [node_id]
    if not include_stale:
        q += " AND a.status = ?"
        params.append(LIVE)
    q += " ORDER BY a.repo, a.path"
    out = []
    for r in conn.execute(q, params).fetchall():
        d = dict(r)
        if d.get("path") == _REPO_LEVEL:
            d["path"] = None
        out.append(d)
    return out


# ---------- scope evidence (heal cross-scope guard) ----------
#
# Artifacts are EVIDENCE, not law (Artifact Evidence Contract). These helpers
# expose a node's repo "scope" — the set of repos its artifacts touch — so heal
# can CAUTION against destructive deterministic supersede across disjoint worlds,
# never to hard-partition the KB. An empty scope (no artifact evidence) always
# means "no opinion" → current heal behavior. The relation is deliberately coarse
# (repo-level only); file paths do not narrow scope.

def node_repo_scope(
    conn: sqlite3.Connection, node_id: int, *, cache: dict | None = None,
) -> frozenset[str]:
    """The set of canonical repos a node's artifacts touch. Empty frozenset =
    scopeless (no artifact evidence). `cache` (a plain dict) memoizes per-node
    lookups across a nightly candidate loop so the guard stays cheap."""
    if cache is not None and node_id in cache:
        return cache[node_id]
    repos = frozenset(
        a["repo"] for a in get_node_artifacts(conn, node_id) if a.get("repo")
    )
    if cache is not None:
        cache[node_id] = repos
    return repos


def scope_relation(a_repos: frozenset[str], b_repos: frozenset[str]) -> str:
    """Classify two repo-scope sets: 'either_empty' | 'overlap' | 'disjoint'.
    Only 'disjoint' (both non-empty, no shared repo) is treated specially by heal."""
    if not a_repos or not b_repos:
        return "either_empty"
    return "overlap" if (a_repos & b_repos) else "disjoint"


def is_cross_scope_disjoint(
    conn: sqlite3.Connection, a_id: int, b_id: int, *, cache: dict | None = None,
) -> bool:
    """True iff BOTH nodes have non-empty repo scopes AND those sets are disjoint
    — the only case heal treats specially (the evidence contract). Same /
    overlapping / either-scopeless all return False = unchanged heal behavior."""
    return scope_relation(
        node_repo_scope(conn, a_id, cache=cache),
        node_repo_scope(conn, b_id, cache=cache),
    ) == "disjoint"


def capture_for_node(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    artifacts: Iterable | None = None,
    project_cwd: str | None = None,
) -> list[int]:
    """Slice-1 capture-on-write. If explicit `artifacts` are supplied, link them
    (the accurate path — the agent names the repo(s)/file(s) the work actually
    touched). Otherwise fall back to a single coarse `repo = project_cwd` stamp.

    Limitation, by design for Slice 1: `project_cwd` is the KB's project dir. For
    a single-repo install that == the repo, so the fallback is correct and gives
    zero-effort capture out of the box. For MULTI-REPO work (e.g. developing repo
    A from repo B's folder) the fallback is only a coarse "created in project X"
    tag — pass explicit `artifacts` for accurate scope/file provenance.
    Auto-observing the session's actually-touched files is a later slice.
    """
    if artifacts:
        return link_node_artifacts(conn, node_id, artifacts)
    if project_cwd and str(project_cwd).strip():
        return link_node_artifacts(conn, node_id, [(project_cwd, None)])
    return []


# ---------- Slice 2: auto-observe touched files from the session transcript ----------

# Claude Code tool calls that edit a file on disk; their tool_use `input` carries
# the path (file_path, or notebook_path for NotebookEdit) — confirmed against a
# real transcript. compactor.read_transcript iterates the same tool_use items but
# flattens them to "[tool_use <name>]", dropping the path — so we parse the raw
# JSONL here rather than its flattened text.
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})


def _repo_root_for(abs_path: str) -> str | None:
    """Nearest ancestor directory containing a `.git` entry, canonicalized — the
    natural repo boundary. A portable filesystem check (does `.git` exist), NOT a
    git command: works offline and identically on Windows/macOS/Linux (priority
    id=1330), and returns None when there is no repo to find (non-git trees)."""
    try:
        p = Path(abs_path)
    except Exception:
        return None
    for parent in p.parents:
        try:
            if (parent / ".git").exists():
                return canonicalize_repo(str(parent))
        except OSError:
            continue
    return None


def _split_repo_path(file_path: str, project_cwd: str | None) -> tuple[str, str]:
    """Map an edited file to its (repo, repo-relative-path) coordinate. repo =
    nearest-ancestor `.git` dir, else canonicalize_repo(project_cwd) — so work done
    in another repo from this project's folder is attributed to the RIGHT repo, not
    project_cwd. The path is made repo-relative when the file sits under the repo
    (portable across machines); otherwise the canonical path is kept as the leaf."""
    f = canonicalize_repo(file_path)
    root = _repo_root_for(file_path) or (canonicalize_repo(project_cwd) if project_cwd else "")
    rel = f
    if root and (f == root or f.startswith(root + "/")):
        rel = f[len(root):].lstrip("/")
    return root, rel


def observe_session_artifacts(
    transcript_path: str | None, project_cwd: str | None,
) -> list[dict]:
    """Parse a Claude Code transcript (JSONL) for the files this session edited and
    return their {repo, path} coordinates (deduped, leaf paths). Repo is derived
    per-file via `_split_repo_path`. Returns [] for a missing/empty transcript."""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        msg = msg if isinstance(msg, dict) else obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not (isinstance(item, dict) and item.get("type") == "tool_use"):
                continue
            if item.get("name") not in _EDIT_TOOLS:
                continue
            inp = item.get("input") or {}
            fp = inp.get("file_path") or inp.get("notebook_path")
            if not fp or not str(fp).strip():
                continue
            repo, rel = _split_repo_path(str(fp), project_cwd)
            if not repo:
                continue
            key = (repo, rel)
            if key not in seen:
                seen.add(key)
                out.append({"repo": repo, "path": rel})
    return out


def attach_observed_artifacts(
    conn: sqlite3.Connection,
    session_id: str,
    transcript_path: str | None,
    project_cwd: str | None,
) -> int:
    """Compaction-time enrichment (Slice 2): attach the files this session actually
    edited (observed from the transcript) to every node created in the session.

    Purely ADDITIVE — never removes or clobbers existing coordinates, so the
    explicit-`artifacts` tier from kb_insert is preserved (the precedence the gate
    flagged). The coarse repo-level `project_cwd` fallback from Slice 1 coexists as
    a low-signal tag; a consumer slice prefers the file-level (path != '')
    coordinates at read time. Idempotent — safe to re-run on rolling compactions.
    Returns the number of session nodes enriched."""
    observed = observe_session_artifacts(transcript_path, project_cwd)
    if not observed:
        return 0
    rows = conn.execute(
        "SELECT id FROM nodes WHERE session_id = ?", (session_id,),
    ).fetchall()
    node_ids = [r["id"] for r in rows]
    for nid in node_ids:
        link_node_artifacts(conn, nid, observed)
    conn.commit()
    return len(node_ids)
