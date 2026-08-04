"""RAPTOR-style hierarchical clustering with access-frequency-weighted landmarks.

Nightly rebuild. Leaf nodes (non-stale, depth=0) are clustered by cosine
similarity; each cluster of >= MIN_CLUSTER_SIZE gets an LLM-generated summary
node at depth=1 (kind='summary', canonical). Cluster members' `parent_id`
points to the summary.

Access-frequency twist: nodes with `ref_count >= LANDMARK_REF_COUNT` are
**landmarks**. They skip clustering and stand on their own at depth=0 with
parent_id=NULL — frequently-consulted knowledge sits alongside cluster
summaries rather than being absorbed into them.

Retrieval is unchanged: summaries appear in the same FTS+vector pool as
leaves (RAPTOR's "collapsed tree" — flat search over all non-stale nodes).
This gives two-hop-free retrieval and is comparable to tree traversal on
the literature's benchmarks.

Rebuild model: clustering and summary generation first prepare a complete
replacement without touching live parent links. Once ready, one SQLite
transaction rewires surviving leaves and marks superseded depth>0 nodes stale;
failure before or during that commit preserves the last known-good hierarchy.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np

import budget  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import lifecycle_signals  # noqa: E402
import log_utils  # noqa: E402
import mcp_runtime  # noqa: E402
import model_backends  # noqa: E402
import paths  # noqa: E402


# Landmark threshold. A node with ref_count >= this bypasses clustering.
LANDMARK_REF_COUNT = 5

# Agglomerative clustering: pairs with cosine similarity >= this are mergeable.
# History (KB id=1529/id=1560): single-link is connected-components of the
# "cosine >= threshold" graph; at ~1k leaves a fixed 0.55 crossed a percolation
# point that chained ~620 nodes into ONE cluster. Step 1 raised it to 0.70 as a
# mitigation; Step 2 adopted bounded average-linkage (the default below), which
# can't chain-collapse, so we lowered the operating point to 0.65 — a dry-run on
# live vectors showed average@0.65 gives more topics (71 vs 50) at higher
# coherence (0.73 vs 0.68) than single@0.70, with a largest cluster of 15.
CLUSTER_SIMILARITY_THRESHOLD = 0.65

# A cluster must have at least this many members to earn an LLM summary.
# Smaller clusters' members stay at depth=0 with parent_id=NULL.
MIN_CLUSTER_SIZE = 3

# Fail-safe collapse tripwire (Step 1 of the single-link fix, KB id=1560): a
# cluster larger than this defers the complete rebuild (prior hierarchy
# preserved, counted as `oversized_skipped`) rather than persisting a giant
# canonical summary, so a density-driven re-collapse can't write a bad summary.
MAX_CLUSTER_MEMBERS = 40

# Safety cap on total backend attempts per run, including explicitly approved
# fallbacks. Completed summaries are staged so a later retry can resume without
# repeating successful calls.
MAX_SUMMARIES_PER_RUN = 50

SUMMARY_TIMEOUT_S = 60
TREE_CODEX_MODEL_ENV = (
    "LATCH_TREE_CODEX_MODEL",
    "CODEX_TREE_MODEL",
    "LATCH_MAINTENANCE_CODEX_MODEL",
    "CODEX_MAINTENANCE_MODEL",
)

SUMMARY_PROMPT = """You are writing a one-paragraph summary of a group of related knowledge-base nodes.

Each member has a kind (fact/decision/progress/entity/preference/open_question/idea),
a title, and a body.

Produce ONE JSON object, and nothing else:

  {"title": "<short title, 6-10 words>", "body": "<markdown paragraph, no lists>"}

Guidelines:
- The summary should make the group searchable: a future query about any
  member should retrieve this summary.
- Prefer concrete nouns and named entities from the members.
- Do not invent content — only combine what is already in the members.
- Output JSON only. No markdown fences, no commentary.
"""


# ---------- clustering ----------

def _cluster_by_threshold(
    vectors: np.ndarray, threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
) -> list[list[int]]:
    """Single-link agglomerative clustering with a similarity floor.

    Merges any two clusters sharing at least one pair of nodes with cosine
    similarity >= threshold. Returns a list of clusters, each a list of row
    indices into `vectors`. Vectors are assumed L2-normalized (sentence-
    transformers normalizes by default).
    """
    n = vectors.shape[0]
    if n == 0:
        return []
    sim = vectors @ vectors.T
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            s = float(sim[i, j])
            if s >= threshold:
                pairs.append((s, i, j))
    # Merge in descending similarity so the strongest links form first.
    pairs.sort(reverse=True)
    for _, i, j in pairs:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return list(groups.values())


def _cluster_average_linkage(
    vectors: np.ndarray,
    threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
    max_cluster_members: int = MAX_CLUSTER_MEMBERS,
) -> list[list[int]]:
    """Bounded average-linkage (UPGMA) agglomerative clustering.

    Repeatedly merges the two clusters with the highest MEAN pairwise cosine,
    but only while that mean is >= threshold AND the merged size stays
    <= max_cluster_members. Unlike single-link (_cluster_by_threshold), a single
    bridge edge cannot fuse two regions, so it resists the percolation/chaining
    collapse (KB id=1529/id=1560). The size cap is enforced *during* merging, so
    no cluster can exceed it by construction.

    Vectors are assumed L2-normalized, so the mean pairwise cosine between two
    clusters equals the dot product of their (unnormalized) centroid means. That
    lets the cluster-cluster mean-similarity matrix be maintained incrementally
    via the size-weighted Lance-Williams update (O(n) per merge) rather than
    recomputing pairwise means. Memory is O(n^2) (the similarity matrix). Best-
    pair selection rescans the n x n matrix each merge, so worst-case TIME is
    ~O(n^3) — fine at the current ~1k-node scale (id=320); swap to a heap with
    lazy invalidation if the KB grows much larger.

    Returns a list of clusters, each a list of row indices into `vectors`.
    """
    n = vectors.shape[0]
    if n == 0:
        return []
    # S[i, j] = mean pairwise cosine between clusters i and j (singletons at start).
    S = (vectors @ vectors.T).astype(np.float64)
    np.fill_diagonal(S, -np.inf)  # never merge a cluster with itself
    sizes = np.ones(n, dtype=np.int64)
    members: list[list[int] | None] = [[i] for i in range(n)]
    active = np.ones(n, dtype=bool)

    while True:
        # Eligible merges: active-active pairs at/above threshold whose combined
        # size fits the cap. Pick the globally most-similar eligible pair.
        size_sum = sizes[:, None] + sizes[None, :]
        eligible = (
            active[:, None] & active[None, :]
            & (S >= threshold)
            & (size_sum <= max_cluster_members)
        )
        if not eligible.any():
            break
        masked = np.where(eligible, S, -np.inf)
        a, b = (int(x) for x in np.unravel_index(np.argmax(masked), masked.shape))
        if not np.isfinite(masked[a, b]):
            break
        # Merge b into a; Lance-Williams size-weighted update of a's row/col so the
        # new mean-similarity to every other cluster stays exact.
        na, nb = int(sizes[a]), int(sizes[b])
        new_row = (na * S[a, :] + nb * S[b, :]) / (na + nb)
        S[a, :] = new_row
        S[:, a] = new_row
        S[a, a] = -np.inf
        sizes[a] = na + nb
        members[a].extend(members[b])  # type: ignore[union-attr]
        members[b] = None
        active[b] = False
        S[b, :] = -np.inf
        S[:, b] = -np.inf

    return [m for m in members if m is not None]


# ---------- LLM summary ----------

def _summary_backend() -> str:
    try:
        return model_backends.resolve_backend(
            env_names=model_backends.MAINTENANCE_BACKEND_ENV,
            default="claude",
        )
    except ValueError:
        return "claude"


def _invoke_summary_attempt(
    members: list[dict],
    *,
    runner: tuple[str, str, str, str] | None = None,
) -> tuple[dict | None, model_backends.ModelCallResult]:
    """Ask the selected model backend for a {title, body} JSON summary.

    Returns the dict or None on any parse/subprocess failure (caller falls back)."""
    if paths.is_disabled() or paths.is_in_compact():
        failure = model_backends.ModelCallResult(
            None, "tree summary invocation is disabled", False, "disabled",
            failure_kind="configuration", terminal=True,
        )
        return None, failure
    payload_parts = [SUMMARY_PROMPT, "\n\n--- CLUSTER MEMBERS ---\n"]
    for m in members:
        payload_parts.append(
            f"- ({m['kind']}) **{m['title']}**\n  {_one_line(m['body'], 300)}\n"
        )
    payload = "".join(payload_parts)

    invoke_kwargs: dict = {
        "env_names": model_backends.MAINTENANCE_BACKEND_ENV,
        "timeout_s": SUMMARY_TIMEOUT_S,
        "purpose": "tree_summary",
        "codex_model_env": TREE_CODEX_MODEL_ENV,
    }
    if runner is not None:
        backend, executable, home, search_path = runner
        env = mcp_runtime.autonomous_subprocess_environment()
        env["HOME"] = home
        env["PATH"] = search_path
        if sys.platform == "win32":
            env["USERPROFILE"] = home
        else:
            env.pop("USERPROFILE", None)
        env.pop("HOMEDRIVE", None)
        env.pop("HOMEPATH", None)
        invoke_kwargs.update({
            "backend": backend,
            "subprocess_env": env,
            {
                "claude": "claude_bin",
                "codex": "codex_bin",
                "cursor": "cursor_bin",
            }[backend]: executable,
        })
    result = model_backends.invoke_prompt(payload, **invoke_kwargs)
    if result.error is not None or result.text is None:
        _log(f"summary {result.backend} subprocess failed: {result.error}")
        return None, result

    parsed = _parse_summary_output(result.text)
    if parsed is None:
        return None, model_backends.ModelCallResult(
            None,
            "tree summary backend returned an invalid response",
            False,
            result.backend,
            failure_kind="invalid_response",
            terminal=False,
        )
    return parsed, result


def _invoke_summary(members: list[dict]) -> dict | None:
    """Compatibility wrapper for manual/backend unit tests."""
    summary, _result = _invoke_summary_attempt(members)
    return summary


def _parse_summary_output(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        env = json.loads(raw)
        text = env.get("result") or env.get("response") or raw
    except json.JSONDecodeError:
        text = raw
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    title = obj.get("title")
    body = obj.get("body")
    if not title or not body:
        return None
    return {"title": str(title), "body": str(body)}


def _one_line(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------- content hash (skip regen when cluster + prompt + model unchanged) ----------

# Identifier folded into each summary's content_hash so prompt-template or
# model/backend swaps invalidate cached summaries. Override via env when pinning
# a specific model for summary work.
_SUMMARY_MODEL_TAG_OVERRIDE = (
    os.environ.get("LATCH_TREE_SUMMARY_MODEL_TAG")
    or os.environ.get("CLAUDE_KB_SUMMARY_MODEL_TAG")
)
SUMMARY_MODEL_TAG = _SUMMARY_MODEL_TAG_OVERRIDE or f"{_summary_backend()}-default"


def _summary_model_tag(backend: str) -> str:
    return _SUMMARY_MODEL_TAG_OVERRIDE or f"{backend}-default"

# Body truncation used both when feeding members to the LLM and when computing
# the hash — keeps the hash stable across edits to a member's body that lie
# below the truncation cutoff.
HASH_BODY_TRUNC = 300


def _cluster_content_hash(
    members: list[dict],
    prompt_template: str = SUMMARY_PROMPT,
    model_tag: str = SUMMARY_MODEL_TAG,
) -> str:
    """SHA-256 over canonicalized (sorted) cluster content + prompt + model.

    Hashes everything that would change the LLM's output: which members are in
    the cluster (by id), each member's kind/title/body-truncation, the prompt
    template, and a model identifier. Equal hash => safe to reuse the prior
    summary; different hash => regenerate.
    """
    sorted_members = sorted(members, key=lambda m: int(m["id"]))
    canonical = [
        [int(m["id"]), str(m["kind"]), str(m["title"]),
         _one_line(str(m["body"] or ""), HASH_BODY_TRUNC)]
        for m in sorted_members
    ]
    payload = json.dumps(
        {"members": canonical, "prompt": prompt_template, "model": model_tag},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _backfill_summary_hashes(conn) -> int:
    """Compute content_hash for any non-stale summary that lacks one, using
    its current children. Idempotent. Returns the number backfilled.

    First post-migration run hits this path; subsequent runs are no-ops.
    Best-effort — if cluster membership later shifts, the backfilled hash
    won't match the new cluster and the summary will be regenerated, which
    is the correct behavior."""
    rows = conn.execute(
        "SELECT id FROM nodes WHERE depth > 0 AND status != 'stale' "
        "AND kind = 'summary' AND content_hash IS NULL"
    ).fetchall()
    backfilled = 0
    for r in rows:
        sid = r["id"]
        children = conn.execute(
            "SELECT id, kind, title, body FROM nodes "
            "WHERE parent_id = ? AND status != 'stale' ORDER BY id",
            (sid,),
        ).fetchall()
        if not children:
            continue
        h = _cluster_content_hash([dict(c) for c in children])
        conn.execute("UPDATE nodes SET content_hash = ? WHERE id = ?", (h, sid))
        backfilled += 1
    if backfilled:
        conn.commit()
    return backfilled


TREE_STAGE_FILENAME = "tree_rebuild_stage.json"
TREE_STAGE_VERSION = 1


def _tree_stage_path(project_path: str | None) -> Path:
    return paths.project_dir(project_path) / TREE_STAGE_FILENAME


def _load_tree_stage(project_path: str | None) -> dict[str, dict]:
    try:
        payload = json.loads(_tree_stage_path(project_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != TREE_STAGE_VERSION
        or not isinstance(payload.get("entries"), dict)
    ):
        return {}
    return {
        str(key): dict(value)
        for key, value in payload["entries"].items()
        if isinstance(value, dict)
    }


def _save_tree_stage(project_path: str | None, entries: dict[str, dict]) -> None:
    path = _tree_stage_path(project_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                {"version": TREE_STAGE_VERSION, "entries": entries},
                handle,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _clear_tree_stage(project_path: str | None) -> None:
    try:
        _tree_stage_path(project_path).unlink()
    except FileNotFoundError:
        pass


# ---------- build_tree ----------

def _existing_summary_index(conn) -> tuple[dict[str, int], list[tuple[str, int]]]:
    """Index reusable summaries without mutating the live hierarchy."""
    existing: dict[str, int] = {}
    backfills: list[tuple[str, int]] = []
    rows = conn.execute(
        "SELECT id, content_hash FROM nodes "
        "WHERE depth > 0 AND status != 'stale' AND kind = 'summary'"
    ).fetchall()
    for row in rows:
        summary_id = int(row["id"])
        content_hash = row["content_hash"]
        if content_hash is None:
            children = conn.execute(
                "SELECT id, kind, title, body FROM nodes "
                "WHERE parent_id = ? AND status != 'stale' ORDER BY id",
                (summary_id,),
            ).fetchall()
            if children:
                content_hash = _cluster_content_hash([dict(item) for item in children])
                backfills.append((content_hash, summary_id))
        if content_hash:
            existing[str(content_hash)] = summary_id
    return existing, backfills


def _normalized_backend_policy(
    policy: list[tuple[str, str, str, str]] | tuple[tuple[str, str, str, str], ...] | None,
) -> list[tuple[str, str, str, str] | None]:
    if policy is None:
        return [None]
    normalized: list[tuple[str, str, str, str] | None] = []
    seen: set[str] = set()
    for runner in policy:
        backend = str(runner[0]).strip().lower()
        if backend not in model_backends.SUPPORTED_BACKENDS:
            raise ValueError(f"unsupported tree backend {backend!r}")
        if backend in seen:
            raise ValueError("tree backend policy contains duplicates")
        seen.add(backend)
        normalized.append((backend, str(runner[1]), str(runner[2]), str(runner[3])))
    return normalized


def _runner_backend(runner: tuple[str, str, str, str] | None) -> str:
    return runner[0] if runner is not None else _summary_backend()

def build_tree(
    conn,
    project_path: str | None = None,
    *,
    use_llm: bool = True,
    landmark_ref_count: int = LANDMARK_REF_COUNT,
    cluster_threshold: float = CLUSTER_SIMILARITY_THRESHOLD,
    min_cluster_size: int = MIN_CLUSTER_SIZE,
    max_summaries: int = MAX_SUMMARIES_PER_RUN,
    max_cluster_members: int = MAX_CLUSTER_MEMBERS,
    linkage: str = "average",
    backend_policy: list[tuple[str, str, str, str]] | None = None,
) -> dict:
    """Full rebuild with hash-based skip — clusters whose content_hash matches
    a still-canonical prior summary are reused (no LLM call). Cost scales with
    drift, not total cluster count.

    Returns a summary dict:

    {
      "ok": bool,
      "leaves": N total leaf nodes considered,
      "landmarks": L landmarks preserved as-is,
      "clusters": total clusters found,
      "largest_cluster": size of the biggest cluster (collapse indicator),
      "p95_cluster_size": 95th-percentile cluster size,
      "summaries_generated": S NEW summaries written this run,
      "summaries_reused": R prior summaries kept (cache hit),
      "summaries_backfilled": B prior summaries that got their first content_hash,
      "budget_blocked": C clusters skipped due to daily cap,
      "llm_failed": F clusters where LLM returned unparseable output,
      "oversized_skipped": clusters refused a summary for exceeding
                           max_cluster_members (fail-safe collapse guard),
      "singletons": singleton clusters (<min_cluster_size, no summary),
      "prior_summaries_staled": number of prior summaries no longer matched
                                by any new cluster (genuine churn),
    }
    """
    if linkage not in ("average", "single"):
        raise ValueError(
            f"build_tree: unknown linkage {linkage!r}; expected 'average' or 'single'")
    if paths.is_unlatched_mode():
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}

    runners = _normalized_backend_policy(backend_policy)
    runner_backends = [_runner_backend(runner) for runner in runners]
    result: dict = {
        "ok": True, "leaves": 0, "landmarks": 0, "clusters": 0,
        "linkage": linkage,
        "largest_cluster": 0, "p95_cluster_size": 0,
        "summaries_generated": 0, "summaries_reused": 0,
        "summaries_backfilled": 0,
        "budget_blocked": 0, "llm_failed": 0, "oversized_skipped": 0,
        "singletons": 0, "prior_summaries_staled": 0,
        "attempts": 0, "backend_attempts": [], "backends_used": [],
        "backend_used": None, "fallback_used": False,
        "summaries_staged": 0, "summaries_staged_reused": 0,
        "retry_pending": False,
    }

    # Phase 1 is read/model-only.  No parent link, summary status, or cached
    # hash changes until every replacement is ready and the commit starts.
    existing_summaries, backfills = _existing_summary_index(conn)
    result["summaries_backfilled"] = len(backfills)
    _debug(f"indexed {len(existing_summaries)} reusable prior summaries by hash")
    reused_summary_ids: set[int] = set()

    # 4. Collect leaf candidates: non-stale, depth=0, has embedding.
    leaf_rows = conn.execute(
        "SELECT id, kind, title, body, ref_count, embedding, workstream_id "
        "FROM nodes "
        "WHERE status != 'stale' AND depth = 0 AND embedding IS NOT NULL"
    ).fetchall()
    result["leaves"] = len(leaf_rows)
    _debug(f"collected {len(leaf_rows)} leaf candidates "
           f"(landmark_ref_count={landmark_ref_count}, "
           f"cluster_threshold={cluster_threshold}, min_cluster_size={min_cluster_size})")
    if not leaf_rows:
        clusters_idx = []
        clusterable = []
        landmarks = []
    else:
        landmarks = []
        clusterable = []
        for row in leaf_rows:
            if int(row["ref_count"] or 0) >= landmark_ref_count:
                landmarks.append(dict(row))
            else:
                clusterable.append(dict(row))
        result["landmarks"] = len(landmarks)
        _debug(f"split: {len(landmarks)} landmarks (ref>={landmark_ref_count}), "
               f"{len(clusterable)} clusterable")
        if clusterable:
            vecs = np.stack(
                [np.frombuffer(r["embedding"], dtype=np.float32) for r in clusterable]
            )
            if linkage == "average":
                clusters_idx = _cluster_average_linkage(
                    vecs,
                    threshold=cluster_threshold,
                    max_cluster_members=max_cluster_members,
                )
            else:
                clusters_idx = _cluster_by_threshold(vecs, threshold=cluster_threshold)
        else:
            clusters_idx = []

    for lm in landmarks:
        _debug(f"  landmark id={lm['id']} kind={lm['kind']!r} "
               f"ref_count={lm['ref_count']} title={lm['title']!r}")

    cluster_sizes = sorted((len(c) for c in clusters_idx), reverse=True)
    result["clusters"] = len(clusters_idx)
    result["largest_cluster"] = cluster_sizes[0] if cluster_sizes else 0
    result["p95_cluster_size"] = (
        int(np.percentile(np.array(cluster_sizes), 95)) if cluster_sizes else 0
    )
    _debug(f"clustered into {len(clusters_idx)} groups (sizes: {cluster_sizes})")

    stage_entries = _load_tree_stage(project_path) if use_llm else {}
    assignments: list[dict] = []
    lifecycle_events: list[dict] = []
    terminal_failures: dict[str, model_backends.ModelCallResult] = {}

    def fail(reason: str, failure_kind: str | None = None) -> dict:
        result.update({
            "ok": False,
            "reason": reason,
            "failure_kind": failure_kind or reason,
            "retry_pending": True,
            "summaries_staged": len(stage_entries),
        })
        if use_llm and stage_entries:
            _save_tree_stage(project_path, stage_entries)
        _debug(f"build_tree deferred without hierarchy mutation: {result}")
        return result

    for cluster_i, cluster in enumerate(clusters_idx):
        members = [clusterable[i] for i in cluster]
        member_ids = [m["id"] for m in members]
        input_hash = _cluster_content_hash(members, model_tag="tree-input-v1")

        # Sub-threshold groups are not lifecycle clusters.  In particular,
        # emitting orphan_cluster for every singleton on every rebuild floods
        # the detector with repeated non-candidates.
        if len(members) < min_cluster_size:
            result["singletons"] += 1
            _debug(f"  cluster #{cluster_i} size={len(members)} ids={member_ids} "
                   f"SKIP: below min_cluster_size")
            continue

        # Structural lifecycle corroboration only: lane ids and node ids, never
        # bodies.  Text clustering may group candidates, but cannot itself
        # authorize membership or a lifecycle mutation.
        lane_counts: dict[int, int] = defaultdict(int)
        orphan_ids: list[int] = []
        for member in members:
            wid = member.get("workstream_id")
            if wid is None and member.get("kind") == "workstream":
                wid = member["id"]
            if wid is None:
                orphan_ids.append(int(member["id"]))
            else:
                lane_counts[int(wid)] += 1
        if len(lane_counts) >= 2:
            lifecycle_events.append({
                    "event": "cluster_span",
                    "substrate_version": lifecycle_signals.SUBSTRATE_VERSION,
                    "cluster_id": input_hash[:16],
                    "workstream_ids": sorted(lane_counts),
                    "fractions": {
                        str(wid): round(count / len(members), 6)
                        for wid, count in sorted(lane_counts.items())
                    },
                })
        if orphan_ids and len(orphan_ids) > len(members) / 2:
            lifecycle_events.append({
                    "event": "orphan_cluster",
                    "substrate_version": lifecycle_signals.SUBSTRATE_VERSION,
                    "cluster_id": input_hash[:16],
                    "member_ids": sorted(orphan_ids),
                    "tree_derived_at": datetime.now().isoformat(timespec="seconds"),
                })
        if orphan_ids and lane_counts:
            lifecycle_events.append({
                    "event": "mixed_lane_orphan_cluster",
                    "substrate_version": lifecycle_signals.SUBSTRATE_VERSION,
                    "cluster_id": input_hash[:16],
                    "orphan_ids": sorted(orphan_ids),
                    "workstream_ids": sorted(lane_counts),
                })

        if len(members) > max_cluster_members:
            # Fail-safe collapse guard: refuse to persist a summary for a
            # pathologically large cluster (single-link percolation). The whole
            # replacement is deferred so prior parent links remain intact,
            # rather than writing a giant canonical summary. (KB id=1560 Step 1;
            # bounded average-linkage + recursive re-split are Steps 2-3.)
            result["oversized_skipped"] += 1
            _log(f"WARNING: cluster of {len(members)} members > "
                 f"MAX_CLUSTER_MEMBERS={max_cluster_members} at "
                 f"threshold={cluster_threshold} — refusing to summarize "
                 f"(possible single-link collapse); prior hierarchy preserved")
            _debug(f"  cluster #{cluster_i} size={len(members)} "
                   f"SKIP: exceeds max_cluster_members={max_cluster_members}")
            return fail("oversized_cluster", "oversized_cluster")

        _debug(f"  cluster #{cluster_i} size={len(members)} ids={member_ids}")
        for m in members:
            _debug(f"    member id={m['id']} kind={m['kind']!r} "
                   f"title={m['title']!r}")

        candidate_hashes = {
            backend: _cluster_content_hash(
                members,
                model_tag=_summary_model_tag(backend),
            )
            for backend in runner_backends
        }
        cached_id = next(
            (
                existing_summaries[candidate_hashes[backend]]
                for backend in runner_backends
                if candidate_hashes[backend] in existing_summaries
            ),
            None,
        )
        if cached_id is not None:
            assignments.append({"member_ids": member_ids, "cached_id": cached_id})
            reused_summary_ids.add(cached_id)
            result["summaries_reused"] += 1
            _debug(f"    -> PLAN REUSE summary id={cached_id}")
            continue

        if not use_llm:
            continue

        staged = stage_entries.get(input_hash)
        if isinstance(staged, dict):
            staged_backend = str(staged.get("backend") or "")
            if (
                staged_backend in candidate_hashes
                and staged.get("content_hash") == candidate_hashes[staged_backend]
                and isinstance(staged.get("title"), str)
                and isinstance(staged.get("body"), str)
            ):
                assignments.append({
                    "member_ids": member_ids,
                    "summary": {
                        "title": staged["title"],
                        "body": staged["body"],
                    },
                    "content_hash": staged["content_hash"],
                    "backend": staged_backend,
                })
                result["summaries_staged_reused"] += 1
                if staged_backend not in result["backends_used"]:
                    result["backends_used"].append(staged_backend)
                result["fallback_used"] = (
                    bool(result["fallback_used"])
                    or runner_backends.index(staged_backend) > 0
                )
                continue

        summary = None
        successful_backend = None
        last_failure = None
        if not runners:
            return fail("backend_unavailable", "configuration")
        for runner_i, runner in enumerate(runners):
            backend = _runner_backend(runner)
            if backend in terminal_failures:
                last_failure = terminal_failures[backend]
                continue
            if result["attempts"] >= max_summaries:
                return fail("attempt_cap", "attempt_cap")
            try:
                allowed, _ = budget.check_and_record(project_path, category="nonheal")
            except OSError as exc:
                allowed = False
                _debug(f"    -> budget state unavailable: {exc}")
            if not allowed:
                result["budget_blocked"] += 1
                return fail("budget_blocked", "budget_blocked")
            result["attempts"] += 1
            summary, call_result = _invoke_summary_attempt(members, runner=runner)
            attempt = {
                "backend": backend,
                "outcome": "success" if summary is not None else "failed",
            }
            if summary is None:
                attempt["failure_kind"] = call_result.failure_kind or "backend_error"
            result["backend_attempts"].append(attempt)
            if summary is not None:
                successful_backend = backend
                result["fallback_used"] = (
                    bool(result["fallback_used"]) or runner_i > 0
                )
                break
            result["llm_failed"] += 1
            last_failure = call_result
            if not call_result.terminal:
                return fail("model_failed", call_result.failure_kind)
            # A terminal pre-invocation/configuration failure cannot recover
            # later in this process.  Do not spend another shared-budget slot
            # retrying the same backend for each remaining cluster.
            terminal_failures[backend] = call_result

        if summary is None or successful_backend is None:
            return fail(
                "backend_unavailable",
                getattr(last_failure, "failure_kind", None) or "backend_error",
            )

        content_hash = candidate_hashes[successful_backend]
        stage_entries[input_hash] = {
            "title": summary["title"],
            "body": summary["body"],
            "backend": successful_backend,
            "content_hash": content_hash,
        }
        assignments.append({
            "member_ids": member_ids,
            "summary": summary,
            "content_hash": content_hash,
            "backend": successful_backend,
        })
        if successful_backend not in result["backends_used"]:
            result["backends_used"].append(successful_backend)

    # Phase 2: the replacement is complete.  Apply every hierarchy change in
    # one SQLite transaction; any insert/embed/commit error restores the prior
    # tree automatically.
    try:
        conn.execute("BEGIN IMMEDIATE")
        for content_hash, summary_id in backfills:
            conn.execute(
                "UPDATE nodes SET content_hash = ? WHERE id = ?",
                (content_hash, summary_id),
            )
        conn.execute(
            "UPDATE nodes SET parent_id = NULL "
            "WHERE depth = 0 AND status != 'stale'"
        )
        for assignment in assignments:
            member_ids = assignment["member_ids"]
            cached_id = assignment.get("cached_id")
            if cached_id is None:
                summary = assignment["summary"]
                vec = embeddings.to_blob(
                    embeddings.embed(f"{summary['title']}\n\n{summary['body']}")
                )
                cached_id = db.insert_node_nc(
                    conn,
                    kind="summary",
                    title=summary["title"],
                    body=summary["body"],
                    status="canonical",
                    embedding=vec,
                )
                conn.execute(
                    "UPDATE nodes SET depth = 1, content_hash = ? WHERE id = ?",
                    (assignment["content_hash"], cached_id),
                )
                result["summaries_generated"] += 1
            placeholders = ",".join("?" for _ in member_ids)
            conn.execute(
                f"UPDATE nodes SET parent_id = ? WHERE id IN ({placeholders})",
                [cached_id, *member_ids],
            )
        _stale_orphans(conn, existing_summaries, reused_summary_ids, result)
        conn.commit()
    except Exception:
        conn.rollback()
        return fail("commit_failed", "commit_failed")

    if use_llm:
        _clear_tree_stage(project_path)
    result["summaries_staged"] = 0
    if result["backends_used"]:
        result["backend_used"] = result["backends_used"][-1]
    for payload in lifecycle_events:
        log_utils.emit_event(
            "lifecycle",
            payload,
            project_path=project_path,
            session_id=None,
        )

    _debug(f"build_tree complete: {result}")
    return result


def _stale_orphans(conn, existing_summaries: dict[str, int],
                   reused_summary_ids: set[int], result: dict) -> None:
    """Mark every prior summary in `existing_summaries` that wasn't reused
    this run as stale. Updates `result['prior_summaries_staled']`."""
    orphan_ids = [sid for sid in existing_summaries.values()
                  if sid not in reused_summary_ids]
    if not orphan_ids:
        return
    placeholders = ",".join("?" for _ in orphan_ids)
    conn.execute(
        f"UPDATE nodes SET status = 'stale', updated_at = ? "
        f"WHERE id IN ({placeholders}) AND status != 'stale'",
        [db._now(), *orphan_ids],
    )
    result["prior_summaries_staled"] += len(orphan_ids)
    _debug(f"staled {len(orphan_ids)} orphan summaries: ids={orphan_ids}")


# ---------- logging ----------

def _log(msg: str) -> None:
    log_path = paths.KB_ROOT / "tree.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass


def _debug(msg: str) -> None:
    """Per-decision debug log. No-op unless CLAUDE_KB_DEBUG_LOG points at a file."""
    path = os.environ.get("CLAUDE_KB_DEBUG_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n")
    except Exception:
        pass
