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
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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


def _parse_transcript_ts(value) -> datetime | None:
    """Parse a transcript line's ISO-8601 `timestamp` to an aware UTC datetime.
    Returns None for anything unparseable, so a malformed line is skipped rather
    than silently counted as in-window."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


_CODEX_EDIT_TOOLS = frozenset({"apply_patch"})
_CODEX_OUTER_EXEC_TOOLS = frozenset({"exec", "functions.exec"})
_CODEX_PATCH_MARKERS = ("*** Add File:", "*** Update File:", "*** Delete File:")


@dataclass(frozen=True)
class _JsToken:
    kind: str
    text: str
    value: str | None = None


def _decode_js_string(raw: str) -> str | None:
    """Decode one static JS string literal; reject template interpolation."""
    if len(raw) < 2 or raw[0] not in {"'", '"', "`"} or raw[-1] != raw[0]:
        return None
    if raw[0] == '"':
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, str) else None
    body = raw[1:-1]
    if raw[0] == "`" and "${" in body:
        return None
    out: list[str] = []
    index = 0
    escapes = {
        "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
        "v": "\v", "0": "\0", "\\": "\\", "'": "'", '"': '"',
        "`": "`",
    }
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            return None
        escaped = body[index]
        if escaped in escapes:
            out.append(escapes[escaped])
            index += 1
            continue
        if escaped in {"\n", "\r"}:
            index += 1
            if escaped == "\r" and index < len(body) and body[index] == "\n":
                index += 1
            continue
        if escaped == "x" and index + 2 < len(body):
            digits = body[index + 1:index + 3]
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                return None
            index += 3
            continue
        if escaped == "u" and index + 4 < len(body):
            digits = body[index + 1:index + 5]
            try:
                out.append(chr(int(digits, 16)))
            except ValueError:
                return None
            index += 5
            continue
        return None
    return "".join(out)


def _js_tokens(script: str) -> tuple[_JsToken, ...]:
    """Tokenize only the JS surface needed for structural tool-call parsing."""
    tokens: list[_JsToken] = []
    index = 0
    while index < len(script):
        char = script[index]
        if char.isspace():
            index += 1
            continue
        if script.startswith("//", index):
            end = script.find("\n", index + 2)
            index = len(script) if end < 0 else end + 1
            continue
        if script.startswith("/*", index):
            end = script.find("*/", index + 2)
            if end < 0:
                tokens.append(_JsToken("error", script[index:]))
                break
            index = end + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            start = index
            index += 1
            template_depth = 0
            while index < len(script):
                current = script[index]
                if current == "\\":
                    index += 2
                    continue
                if quote == "`" and script.startswith("${", index):
                    template_depth += 1
                    index += 2
                    continue
                if quote == "`" and current == "}" and template_depth:
                    template_depth -= 1
                    index += 1
                    continue
                if current == quote and template_depth == 0:
                    index += 1
                    raw = script[start:index]
                    tokens.append(_JsToken(
                        "string", raw, _decode_js_string(raw),
                    ))
                    break
                index += 1
            else:
                tokens.append(_JsToken("error", script[start:]))
            continue
        if char.isalpha() or char in {"_", "$"}:
            start = index
            index += 1
            while index < len(script) and (
                script[index].isalnum() or script[index] in {"_", "$"}
            ):
                index += 1
            tokens.append(_JsToken("identifier", script[start:index]))
            continue
        tokens.append(_JsToken("punctuation", char))
        index += 1
    return tuple(tokens)


def _structural_apply_patch_arguments(script: str) -> tuple[str | None, ...]:
    """Return every real ``tools.apply_patch`` argument in execution order.

    A ``None`` entry means the invocation is real but its exact argument cannot
    be proven static. Callers defer that failure until after receipt-window and
    outer-call success checks.
    """
    tokens = _js_tokens(script)
    bindings: dict[str, str | None] = {}
    arguments: list[str | None] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.kind == "identifier"
            and token.text in {"const", "let", "var"}
            and index + 3 < len(tokens)
            and tokens[index + 1].kind == "identifier"
            and tokens[index + 2].text == "="
        ):
            name = tokens[index + 1].text
            value_token = tokens[index + 3]
            terminator = tokens[index + 4] if index + 4 < len(tokens) else None
            bindings[name] = (
                value_token.value
                if (
                    value_token.kind == "string"
                    and (terminator is None or terminator.text == ";")
                )
                else None
            )
            index += 4
            continue
        if (
            token.kind == "identifier"
            and index + 1 < len(tokens)
            and tokens[index + 1].text == "="
        ):
            # Reassignment makes a prior simple binding ambiguous. Declarations
            # are consumed above, so only later mutation reaches this branch.
            bindings[token.text] = None
        if not (
            token.kind == "identifier"
            and token.text == "tools"
            and index + 3 < len(tokens)
            and tokens[index + 1].text == "."
            and tokens[index + 2].kind == "identifier"
            and tokens[index + 2].text == "apply_patch"
            and tokens[index + 3].text == "("
        ):
            index += 1
            continue

        close = index + 4
        depth = 1
        while close < len(tokens) and depth:
            if tokens[close].text == "(":
                depth += 1
            elif tokens[close].text == ")":
                depth -= 1
            if depth:
                close += 1
        if depth or close != index + 5:
            arguments.append(None)
            index += 4
            continue
        argument = tokens[index + 4]
        if argument.kind == "string":
            arguments.append(argument.value)
        elif argument.kind == "identifier":
            arguments.append(bindings.get(argument.text))
        else:
            arguments.append(None)
        index = close + 1
    return tuple(arguments)


def _outer_exec_script(payload: Mapping[str, Any]) -> str | None:
    if payload.get("type") != "custom_tool_call":
        return None
    if payload.get("name") not in _CODEX_OUTER_EXEC_TOOLS:
        return None
    script: Any = payload.get("input")
    if script is None:
        script = payload.get("arguments")
    if isinstance(script, Mapping):
        script = script.get("input") or script.get("code")
    return script if isinstance(script, str) else None


def _outer_exec_has_apply_patch(payload: Mapping[str, Any]) -> bool:
    script = _outer_exec_script(payload)
    return bool(script and _structural_apply_patch_arguments(script))


def _codex_patch_paths(patch_text: str) -> list[str]:
    """Extract the file paths an `apply_patch` envelope touches.

    Codex writes edits as a single custom_tool_call whose `input` is a
    ``*** Begin Patch`` envelope naming each file on an Add/Update/Delete
    header line. Parsing those headers is how a Codex-hosted session's shipped
    code becomes visible at all — without it the shipped-diff signal is blind on
    the majority of this project's own gate traffic.
    """
    paths: list[str] = []
    for line in patch_text.splitlines():
        stripped = line.strip()
        for marker in _CODEX_PATCH_MARKERS:
            if stripped.startswith(marker):
                candidate = stripped[len(marker):].strip()
                if candidate:
                    paths.append(candidate)
                break
    return paths


def _failed_call_ids(objs: list[dict]) -> set[str]:
    """Ids of tool calls whose result reported failure.

    An attempted edit that errored moved no code, so counting it as a shipped
    diff would manufacture evidence — the difference between "the ruling was
    ignored" and "the agent tried something and it failed" is exactly what the
    outcome label is supposed to capture.

    Claude marks this on the `tool_result` item (`is_error`, joined by
    `tool_use_id`). Codex carries a `status` on the call itself; anything other
    than an explicit success is treated as failed, and an absent status is
    treated as success so a shape change cannot silently zero the signal.
    """
    failed: set[str] = set()
    for obj in objs:
        msg = obj.get("message")
        msg = msg if isinstance(msg, dict) else obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") != "tool_result":
                    continue
                if not item.get("is_error"):
                    continue
                call_id = item.get("tool_use_id")
                if isinstance(call_id, str) and call_id:
                    failed.add(call_id)
        payload = obj.get("payload")
        if isinstance(payload, dict):
            status = payload.get("status")
            if isinstance(status, str) and status.strip().lower() not in (
                "", "completed", "success", "succeeded",
            ):
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id:
                    failed.add(call_id)
    return failed


class ArtifactEvidenceError(ValueError):
    """A stable transcript snapshot cannot prove artifact evidence."""


def _strict_transcript_objects(data: bytes) -> list[dict[str, Any]]:
    if not isinstance(data, bytes):
        raise ArtifactEvidenceError("artifact evidence requires snapshotted bytes")
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactEvidenceError("artifact evidence is not valid UTF-8") from exc
    objects: list[dict[str, Any]] = []
    for line_number, line in enumerate(decoded.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ArtifactEvidenceError(
                f"malformed transcript JSON at line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise ArtifactEvidenceError(
                f"transcript row {line_number} is not an object"
            )
        objects.append(value)
    return objects


def _nested_result_failed(value: Any) -> bool:
    """Conservatively recognize structured tool-result failure markers."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return False
        return _nested_result_failed(decoded)
    if isinstance(value, list):
        return any(_nested_result_failed(item) for item in value)
    if not isinstance(value, dict):
        return False
    if (
        value.get("is_error") is True
        or value.get("isError") is True
        or value.get("success") is False
    ):
        return True
    exit_code = value.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return True
    status = value.get("status")
    if isinstance(status, str) and status.strip().lower() in {
        "error", "failed", "failure", "cancelled", "canceled",
    }:
        return True
    return any(
        _nested_result_failed(value.get(key))
        for key in ("content", "output", "result")
        if key in value
    )


ArtifactIdentity = tuple[str, str, str]
ArtifactIdentityResolver = Callable[
    [str, str, str | None, Mapping[str, Any] | None],
    ArtifactIdentity | None,
]


@dataclass(frozen=True)
class _IndexedArtifactCall:
    identity: ArtifactIdentity
    timestamp: datetime | None
    call_id: Any
    adapter: str
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ArtifactEvidenceIndex:
    """One immutable parse of the stable S2 segment union.

    Raw paths and transcript contents remain in memory only. The public lookup
    key contains an adapter, an opaque project-proof fingerprint, and a session
    id, so a same-session record from another host or project cannot join the
    target session's edit calls/results.
    """

    calls_by_identity: Mapping[ArtifactIdentity, tuple[_IndexedArtifactCall, ...]]
    call_candidates: Mapping[
        tuple[ArtifactIdentity, str], tuple[_IndexedArtifactCall, ...]
    ]
    results: Mapping[tuple[ArtifactIdentity, str], tuple[bool, ...]]
    result_conflicts: frozenset[tuple[ArtifactIdentity, str]]
    identities_by_file: Mapping[str, frozenset[ArtifactIdentity]]
    file_errors: frozenset[str]
    identity_errors: frozenset[ArtifactIdentity]


def build_artifact_evidence_index(
    segments: Sequence[tuple[str, bytes]],
    *,
    resolve_identity: ArtifactIdentityResolver,
    invalid_files: Iterable[str] = (),
    conflicting_files: Iterable[str] = (),
) -> ArtifactEvidenceIndex:
    """Parse stable transcript segments once into exact identity buckets.

    ``resolve_identity`` is the trust boundary: it returns a key only when the
    row's project proof matches the configured target. Foreign and unknown rows
    are therefore neither counted nor allowed to poison a target identity.
    Malformed bytes remain attached to their file and every accepted identity
    observed in that file, preserving fail-closed behavior for exact evidence.
    """

    calls: dict[ArtifactIdentity, list[_IndexedArtifactCall]] = defaultdict(list)
    call_candidates: dict[
        tuple[ArtifactIdentity, str], list[_IndexedArtifactCall]
    ] = defaultdict(list)
    results: dict[tuple[ArtifactIdentity, str], list[bool]] = defaultdict(list)
    result_fingerprints: dict[
        tuple[ArtifactIdentity, str], set[str]
    ] = defaultdict(set)
    identities_by_file: dict[str, set[ArtifactIdentity]] = defaultdict(set)
    file_errors = {str(file) for file in invalid_files if str(file)}
    conflict_files = {str(file) for file in conflicting_files if str(file)}

    for file_token, data in segments:
        if not isinstance(file_token, str) or not file_token:
            continue
        if not isinstance(data, bytes):
            file_errors.add(file_token)
            continue
        try:
            decoded = data.decode("utf-8")
        except UnicodeDecodeError:
            file_errors.add(file_token)
            continue

        codex_session: str | None = None
        codex_cwd: str | None = None
        for line in decoded.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                file_errors.add(file_token)
                continue
            if not isinstance(obj, dict):
                file_errors.add(file_token)
                continue

            payload = obj.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            record_type = str(obj.get("type") or "")
            payload_type = str(payload.get("type") or "")
            if record_type == "session_meta" or payload_type == "session_meta":
                meta = payload if payload else obj
                codex_session = str(
                    meta.get("id") or meta.get("session_id") or ""
                ) or None
                codex_cwd = str(meta.get("cwd") or "") or None
                if codex_session:
                    identity = resolve_identity(
                        "codex", codex_session, codex_cwd, None
                    )
                    if identity is not None:
                        identities_by_file[file_token].add(identity)
                continue

            identity: ArtifactIdentity | None = None
            adapter: str | None = None
            if obj.get("event_type") == "gate_host_record":
                adapter = str(obj.get("adapter") or "host")
                direct_session = str(obj.get("session_id") or "") or None
                explicit_proof = obj.get("project_proof")
                proof = explicit_proof if isinstance(explicit_proof, Mapping) else None
                if direct_session:
                    identity = resolve_identity(
                        adapter,
                        direct_session,
                        str(obj.get("cwd") or "") or None,
                        proof,
                    )
            else:
                claude_session = str(obj.get("sessionId") or "") or None
                if claude_session:
                    adapter = "claude"
                    identity = resolve_identity(
                        adapter,
                        claude_session,
                        str(obj.get("cwd") or "") or None,
                        None,
                    )
                elif codex_session:
                    adapter = "codex"
                    identity = resolve_identity(
                        adapter, codex_session, codex_cwd, None
                    )
            if identity is None or adapter is None:
                # Session/project/adapter filtering happens before any edit
                # identity or result requirement. Unknown and foreign records
                # cannot censor an exact target receipt.
                continue
            identities_by_file[file_token].add(identity)

            message = obj.get("message")
            message = message if isinstance(message, dict) else obj
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_result":
                        call_id = item.get("tool_use_id")
                        if isinstance(call_id, str) and call_id:
                            result_key = (identity, call_id)
                            results[result_key].append(
                                bool(item.get("is_error"))
                                or _nested_result_failed(item.get("content"))
                            )
                            result_fingerprints[result_key].add(
                                json.dumps(
                                    item,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                            )
                        continue
                    if item.get("type") == "tool_use":
                        call_id = item.get("id")
                        candidate = _IndexedArtifactCall(
                            identity=identity,
                            timestamp=_parse_transcript_ts(obj.get("timestamp")),
                            call_id=call_id,
                            adapter="claude",
                            payload=item,
                        )
                        if isinstance(call_id, str) and call_id:
                            call_candidates[(identity, call_id)].append(candidate)
                        if item.get("name") in _EDIT_TOOLS:
                            calls[identity].append(candidate)

            if payload_type in {
                "function_call_output", "custom_tool_call_output",
            }:
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id:
                    result_key = (identity, call_id)
                    results[result_key].append(
                        _nested_result_failed(payload.get("output"))
                    )
                    result_fingerprints[result_key].add(
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
            else:
                is_edit = (
                    payload.get("name") in _CODEX_EDIT_TOOLS
                    or _outer_exec_has_apply_patch(payload)
                )
                if payload_type in {"function_call", "custom_tool_call"} or is_edit:
                    call_id = payload.get("call_id")
                    if call_id in (None, ""):
                        call_id = payload.get("id")
                    candidate = _IndexedArtifactCall(
                        identity=identity,
                        timestamp=_parse_transcript_ts(obj.get("timestamp")),
                        call_id=call_id,
                        adapter="codex",
                        payload=payload,
                    )
                    if isinstance(call_id, str) and call_id:
                        call_candidates[(identity, call_id)].append(candidate)
                    if is_edit:
                        calls[identity].append(candidate)

    file_errors.update(conflict_files)
    identity_errors: set[ArtifactIdentity] = set()
    for file_token in file_errors:
        identity_errors.update(identities_by_file.get(file_token, ()))
    return ArtifactEvidenceIndex(
        calls_by_identity={
            identity: tuple(rows) for identity, rows in calls.items()
        },
        call_candidates={
            key: tuple(rows) for key, rows in call_candidates.items()
        },
        results={key: tuple(values) for key, values in results.items()},
        result_conflicts=frozenset(
            key
            for key, fingerprints in result_fingerprints.items()
            if len(fingerprints) > 1
        ),
        identities_by_file={
            file: frozenset(identities)
            for file, identities in identities_by_file.items()
        },
        file_errors=frozenset(file_errors),
        identity_errors=frozenset(identity_errors),
    )


def observe_indexed_session_artifacts(
    index: ArtifactEvidenceIndex,
    identity: ArtifactIdentity,
    project_cwd: str | None,
    t0: datetime,
    t_end: datetime,
) -> list[dict]:
    """Resolve distinct successful in-window edits from one parsed index."""

    if not isinstance(t0, datetime) or not isinstance(t_end, datetime):
        raise ArtifactEvidenceError("artifact evidence requires a receipt window")
    if t0.tzinfo is None or t_end.tzinfo is None or t_end < t0:
        raise ArtifactEvidenceError("artifact evidence window is invalid")
    if identity in index.identity_errors:
        raise ArtifactEvidenceError("exact-session transcript bytes are malformed")
    t0 = t0.astimezone(timezone.utc)
    t_end = t_end.astimezone(timezone.utc)
    seen: set[tuple[str, str]] = set()

    def record(file_path: Any) -> None:
        if not isinstance(file_path, str) or not file_path.strip():
            raise ArtifactEvidenceError("edit call is missing its file path")
        repo, rel = _split_repo_path(file_path, project_cwd)
        if not repo:
            raise ArtifactEvidenceError("edit path has no resolvable project scope")
        seen.add((repo, rel))

    in_window_calls: dict[str, tuple[str, _IndexedArtifactCall]] = {}
    unkeyed_calls: list[_IndexedArtifactCall] = []

    def call_fingerprint(call: _IndexedArtifactCall) -> str:
        return json.dumps(
            {
                "adapter": call.adapter,
                "timestamp": (
                    call.timestamp.isoformat() if call.timestamp else None
                ),
                "payload": call.payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    for call in index.calls_by_identity.get(identity, ()):
        ts = call.timestamp
        if ts is None:
            raise ArtifactEvidenceError("edit call has no valid timestamp")
        # Window admission precedes call-id, result, and payload validation.
        # A broken historical/future edit is not evidence for this receipt and
        # therefore cannot censor its otherwise observable window.
        if not (t0 <= ts <= t_end):
            continue
        call_id = call.call_id
        if not isinstance(call_id, str) or not call_id:
            unkeyed_calls.append(call)
            continue
        fingerprint = call_fingerprint(call)
        existing = in_window_calls.get(call_id)
        if existing is not None and existing[0] != fingerprint:
            raise ArtifactEvidenceError(
                "edit call has conflicting duplicate payloads"
            )
        in_window_calls.setdefault(call_id, (fingerprint, call))

    for call in [
        *unkeyed_calls,
        *(entry[1] for entry in in_window_calls.values()),
    ]:
        call_id = call.call_id
        if not isinstance(call_id, str) or not call_id:
            raise ArtifactEvidenceError("edit call has no tool identity")
        candidates = index.call_candidates.get((identity, call_id), ())
        if any(candidate.timestamp is None for candidate in candidates):
            raise ArtifactEvidenceError(
                "edit call identity has an unplaceable tool-call candidate"
            )
        candidate_fingerprints = {
            call_fingerprint(candidate)
            for candidate in candidates
            if candidate.timestamp is not None
            and t0 <= candidate.timestamp <= t_end
        }
        if len(candidate_fingerprints) > 1:
            raise ArtifactEvidenceError(
                "edit call identity is shared by conflicting tool calls"
            )
        statuses = index.results.get((identity, call_id))
        if (identity, call_id) in index.result_conflicts:
            raise ArtifactEvidenceError("edit call has conflicting result payloads")

        if call.adapter == "claude":
            if not statuses:
                raise ArtifactEvidenceError("edit call has no tool result")
            if len(set(statuses)) != 1:
                raise ArtifactEvidenceError("edit call has conflicting tool results")
            if statuses[0]:
                continue
            inp = call.payload.get("input")
            if not isinstance(inp, dict):
                raise ArtifactEvidenceError("edit call input is malformed")
            record(inp.get("file_path") or inp.get("notebook_path"))
            continue

        script = _outer_exec_script(call.payload)
        if script is not None:
            # The outer exec is only a transport. Its own result, joined by the
            # outer call id, is the success/failure authority for every nested
            # apply_patch invocation.
            if not statuses:
                raise ArtifactEvidenceError("outer exec has no tool result")
            if len(set(statuses)) != 1:
                raise ArtifactEvidenceError("outer exec has conflicting results")
            if statuses[0]:
                continue
        else:
            status = call.payload.get("status")
            explicit_success = (
                isinstance(status, str)
                and status.strip().lower() in {
                    "completed", "success", "succeeded",
                }
            )
            if isinstance(status, str) and status.strip() and not explicit_success:
                continue
            if statuses and len(set(statuses)) != 1:
                raise ArtifactEvidenceError("edit call has conflicting tool results")
            if not explicit_success and not statuses:
                raise ArtifactEvidenceError("edit call has no success evidence")
            if statuses and statuses[0]:
                continue
        if script is not None:
            patch_texts = _structural_apply_patch_arguments(script)
            if not patch_texts or any(text is None for text in patch_texts):
                raise ArtifactEvidenceError(
                    "outer exec apply_patch argument is not exactly resolvable"
                )
        else:
            raw = call.payload.get("input")
            if not isinstance(raw, str):
                raw = call.payload.get("arguments")
            if not isinstance(raw, str) or not raw:
                raise ArtifactEvidenceError("apply_patch input is malformed")
            patch_text = raw
            if not raw.lstrip().startswith("***"):
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ArtifactEvidenceError(
                        "apply_patch arguments are malformed"
                    ) from exc
                if not isinstance(decoded, dict):
                    raise ArtifactEvidenceError(
                        "apply_patch arguments are malformed"
                    )
                patch_text = str(
                    decoded.get("input") or decoded.get("patch") or ""
                )
            patch_texts = (patch_text,)
        for patch_text in patch_texts:
            if patch_text is None:
                raise ArtifactEvidenceError("apply_patch argument is unavailable")
            paths_in_patch = _codex_patch_paths(patch_text)
            if not paths_in_patch:
                raise ArtifactEvidenceError(
                    "apply_patch contains no parseable file path"
                )
            for file_path in paths_in_patch:
                record(file_path)

    return [
        {"repo": repo, "path": path}
        for repo, path in sorted(seen)
    ]


def observe_session_artifacts_in_window_bytes(
    data: bytes,
    project_cwd: str | None,
    t0: datetime,
    t_end: datetime,
    *,
    session_id: str | None = None,
) -> list[dict]:
    """Strict artifact evidence from one already-snapshotted S2 byte stream.

    Unlike the legacy path wrapper below, this production measurement helper
    never rereads a path and never turns malformed/unavailable evidence into a
    clean zero. It returns distinct successful edit coordinates whose tool-use
    timestamp is inside the exact canonical receipt window.
    """
    def resolve_identity(
        adapter: str,
        candidate_session: str,
        _cwd: str | None,
        _proof: Mapping[str, Any] | None,
    ) -> ArtifactIdentity | None:
        if session_id is not None and candidate_session != session_id:
            return None
        return (adapter, "unscoped", candidate_session)

    index = build_artifact_evidence_index(
        (("<snapshot>", data),),
        resolve_identity=resolve_identity,
    )
    if index.file_errors:
        raise ArtifactEvidenceError("artifact snapshot is malformed")
    identities = {
        identity
        for file_identities in index.identities_by_file.values()
        for identity in file_identities
        if session_id is None or identity[2] == session_id
    }
    seen: dict[tuple[str, str], dict] = {}
    for identity in sorted(identities):
        for item in observe_indexed_session_artifacts(
            index, identity, project_cwd, t0, t_end,
        ):
            seen[(item["repo"], item["path"])] = item
    return [seen[key] for key in sorted(seen)]


def observe_session_artifacts_in_window(
    transcript_path: str | None,
    project_cwd: str | None,
    t0: datetime,
    t_end: datetime,
) -> list[dict]:
    """Like `observe_session_artifacts`, but keeps only edits that (a) are
    timestamped within [t0, t_end] and (b) actually succeeded.

    This is the "shipped diff" signal for the outcome correlator: evidence that
    code actually moved after a gate verdict, rather than proximity in time to a
    KB write. Both host shapes are read — Claude's `tool_use` items and Codex's
    `apply_patch` envelopes — because a signal that sees only one host would
    under-report exactly the sessions that do the most work.

    Deliberately reuses the transcript rather than shelling out to git:
    `project_path` is the vault, not a worktree, and the no-git-command
    constraint (priority id=1330) holds here as it does in `_repo_root_for`.
    """
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    objs: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objs.append(obj)

    failed = _failed_call_ids(objs)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    def _record(file_path: str) -> None:
        if not file_path or not str(file_path).strip():
            return
        repo, rel = _split_repo_path(str(file_path), project_cwd)
        if not repo:
            return
        key = (repo, rel)
        if key not in seen:
            seen.add(key)
            out.append({"repo": repo, "path": rel})

    for obj in objs:
        ts = _parse_transcript_ts(obj.get("timestamp"))
        if ts is None or ts < t0 or ts > t_end:
            continue

        # Codex: response_item -> custom_tool_call/function_call apply_patch.
        payload = obj.get("payload")
        if isinstance(payload, dict):
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id in failed:
                continue
            if payload.get("name") in _CODEX_EDIT_TOOLS:
                raw = payload.get("input")
                if not isinstance(raw, str):
                    raw = payload.get("arguments")
                if isinstance(raw, str) and raw:
                    text = raw
                    if not text.lstrip().startswith("***"):
                        # function_call form: the envelope arrives JSON-encoded.
                        try:
                            decoded = json.loads(raw)
                        except json.JSONDecodeError:
                            decoded = None
                        if isinstance(decoded, dict):
                            text = str(
                                decoded.get("input")
                                or decoded.get("patch")
                                or "",
                            )
                    for candidate in _codex_patch_paths(text):
                        _record(candidate)

        # Claude Code: message content -> tool_use with an edit tool.
        msg = obj.get("message")
        msg = msg if isinstance(msg, dict) else obj
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not (isinstance(item, dict) and item.get("type") == "tool_use"):
                continue
            if item.get("name") not in _EDIT_TOOLS:
                continue
            if item.get("id") in failed:
                continue
            inp = item.get("input") or {}
            _record(inp.get("file_path") or inp.get("notebook_path") or "")
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
