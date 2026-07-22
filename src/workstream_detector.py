"""Deterministic shadow detector for workstream lifecycle candidates.

The detector is deliberately split in two:

* :func:`collect_inputs` performs read-only projection from the event and tree
  substrates.
* :func:`derive_shadow_snapshot` is pure: identical inputs yield identical
  candidates, rankings, counters, and derivation identity.

Persisting a run writes only an immutable ``workstream_derivations`` snapshot.
It never invokes an LLM and never mutates nodes, edges, focus, or priorities.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from array import array
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any

import db
import feeders
import lifecycle_signals
import log_utils


SUBSTRATE_VERSION = f"{lifecycle_signals.SUBSTRATE_VERSION}:detector-v1"

ELIGIBLE_SESSION_LIMIT = 40
ELIGIBLE_WINDOW_FLOOR_DAYS = 14
ELIGIBLE_WINDOW_CAP_DAYS = 90
MERGE_MIN_CO_CONTACT = 4
MERGE_MIN_JACCARD = 0.40
TIER2_MIN_SHARED_TARGETS = 2
TIER2_MIN_CROSS_PATH_SESSIONS = 2
OPEN_MIN_UNITS = 4
OPEN_MIN_CONTACT_SESSIONS = 2
OPEN_MIN_CONTACT_DAYS = 3
OPEN_MIN_SPAN_HOURS = 48.0
OPEN_DEDUPE_COSINE = 0.92
CLOSE_MIN_OBSERVATION_DAYS = 14
TREE_MAX_AGE_DAYS = 14
RECENT_ACTIVE_LANE_DAYS = 30
RECENT_ACTIVE_LANE_CAP = 12
HEAL_SIGNALS_PER_PAIR_LIMIT = 20
WEAK_ANNOTATION_ID_LIMIT = 20

_CONTACT_SOURCES = frozenset({"prompt", "graph", "tool", "gate"})
_STRUCTURAL_TARGET_RELATIONS = frozenset({
    "advances", "motivates", "depends_on", "constrains", "tested_against",
})
_STRUCTURAL_TARGET_KINDS = frozenset({
    "decision", "open_question", "idea", "progress", "workstream",
})
_FORWARD_KINDS = frozenset({"open_question", "idea", "progress"})


def _parse_ts(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _canonical_hash(prefix: str, value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _embedding_values(blob: bytes | None) -> list[float] | None:
    if blob is None:
        return None
    try:
        values = array("f")
        values.frombytes(blob)
        return [float(value) for value in values]
    except (TypeError, ValueError):
        return None


def _cosine(left: Sequence[float], right: Sequence[float]) -> float | None:
    if not left or len(left) != len(right):
        return None
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return None
    return sum(float(a) * float(b) for a, b in zip(left, right)) / (
        left_norm * right_norm
    )


def _dedupe_open_units(group: Mapping[str, Any]) -> list[dict]:
    """Collapse exact content hashes, then cosine-near-duplicate components."""
    member_ids = [int(value) for value in group.get("member_ids") or []]
    kinds = list(group.get("member_kinds") or [])
    created = list(group.get("member_created_at") or [])
    hashes = list(group.get("member_content_hashes") or [])
    embeddings = group.get("member_embeddings") or {}
    artifacts = group.get("member_artifacts") or {}
    raw: list[dict] = []
    for index, node_id in enumerate(member_ids):
        kind = str(kinds[index]) if index < len(kinds) else ""
        if kind == "priority":
            continue
        raw.append({
            "node_ids": [node_id],
            "kinds": {kind},
            "created_at": [created[index] if index < len(created) else None],
            "content_hash": hashes[index] if index < len(hashes) else None,
            "content_hashes": {
                str(hashes[index])
                if index < len(hashes) and hashes[index] is not None else ""
            },
            "embedding": embeddings.get(str(node_id), embeddings.get(node_id)),
            "artifact_ids": set(artifacts.get(str(node_id), artifacts.get(node_id, []))),
        })

    # Exact non-empty hashes are one unit before the more permissive cosine pass.
    exact: list[dict] = []
    by_hash: dict[str, dict] = {}
    for unit in raw:
        content_hash = str(unit["content_hash"] or "").strip()
        if not content_hash:
            exact.append(unit)
            continue
        prior = by_hash.get(content_hash)
        if prior is None:
            by_hash[content_hash] = unit
            exact.append(unit)
            continue
        prior["node_ids"].extend(unit["node_ids"])
        prior["kinds"].update(unit["kinds"])
        prior["created_at"].extend(unit["created_at"])
        prior["artifact_ids"].update(unit["artifact_ids"])
        prior["content_hashes"].update(unit["content_hashes"])
        if prior.get("embedding") is None and unit.get("embedding") is not None:
            prior["embedding"] = unit["embedding"]

    parent = list(range(len(exact)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for left in range(len(exact)):
        for right in range(left + 1, len(exact)):
            similarity = _cosine(
                exact[left].get("embedding") or [], exact[right].get("embedding") or [],
            )
            if similarity is not None and similarity >= OPEN_DEDUPE_COSINE:
                root_left, root_right = find(left), find(right)
                if root_left != root_right:
                    parent[root_right] = root_left
    components: dict[int, dict] = {}
    for index, unit in enumerate(exact):
        root = find(index)
        merged = components.setdefault(root, {
            "node_ids": [], "kinds": set(), "created_at": [], "artifact_ids": set(),
            "content_hashes": set(),
        })
        merged["node_ids"].extend(unit["node_ids"])
        merged["kinds"].update(unit["kinds"])
        merged["created_at"].extend(unit["created_at"])
        merged["artifact_ids"].update(unit["artifact_ids"])
        merged["content_hashes"].update(unit["content_hashes"])
    units = list(components.values())
    for unit in units:
        unit["node_ids"] = sorted({int(value) for value in unit["node_ids"]})
        unit["kinds"] = sorted({value for value in unit["kinds"] if value})
        unit["artifact_ids"] = sorted({int(value) for value in unit["artifact_ids"]})
        unit["content_hashes"] = sorted(
            value for value in unit["content_hashes"] if value
        )
        unit["created_at"] = sorted(
            str(value) for value in unit["created_at"] if value is not None
        )
    units.sort(key=lambda unit: unit["node_ids"])
    return units


def _is_contact_event(row: Mapping[str, Any]) -> bool:
    source = row.get("source")
    if source == "write":
        return row.get("session_id") is not None
    turn = row.get("turn")
    return bool(
        row.get("session_id") is not None
        and source in _CONTACT_SOURCES
        and turn is not None
        and int(turn) > 0
    )


def _node_lane(row: Mapping[str, Any], prefix: str) -> int | None:
    if row.get(f"{prefix}_kind") == "workstream":
        return int(row[f"{prefix}_id"])
    value = row.get(f"{prefix}_workstream_id")
    return int(value) if value is not None else None


def _done_when(body: str | None) -> str | None:
    for line in str(body or "").splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip().casefold() in {"done when", "done-when"}:
            normalized = " ".join(value.split())
            return normalized or None
    return None


def _latest_tree_signal_times(
    project_path: str | None,
    *,
    now: datetime,
) -> dict[tuple[int, ...], str]:
    """Map logged orphan member sets to their latest tree derivation time."""
    if project_path is None:
        return {}
    start = (now - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS)).date()
    found: dict[tuple[int, ...], tuple[datetime, str]] = {}
    for row in log_utils.read_log_range(
        "lifecycle", start, now.date(), project_path=project_path,
    ):
        if row.get("event") != "orphan_cluster":
            continue
        try:
            members = tuple(sorted({int(value) for value in row.get("member_ids") or []}))
        except (TypeError, ValueError):
            continue
        derived = _parse_ts(row.get("tree_derived_at") or row.get("ts"))
        if not members or derived is None:
            continue
        previous = found.get(members)
        if previous is None or derived > previous[0]:
            found[members] = (derived, _stamp(derived))
    return {members: value for members, (_ts, value) in found.items()}


def _latest_heal_pair_signals(
    project_path: str | None,
    *,
    now: datetime,
) -> dict[tuple[int, int], list[dict]]:
    """Read bounded, structural-only cross-lane signals emitted by heal."""
    if project_path is None:
        return {}
    start = (now - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS)).date()
    by_pair: dict[tuple[int, int], list[tuple[datetime, dict]]] = defaultdict(list)
    allowed = {"cross_lane_duplicate", "cross_lane_contradiction"}
    for row in log_utils.read_log_range(
        "lifecycle", start, now.date(), project_path=project_path,
    ):
        event = str(row.get("event") or "")
        if event not in allowed:
            continue
        try:
            left, right = sorted((int(row["ws_a"]), int(row["ws_b"])))
            node_a, node_b = int(row["node_a"]), int(row["node_b"])
        except (KeyError, TypeError, ValueError):
            continue
        observed = _parse_ts(row.get("ts"))
        if left == right or observed is None or observed > now:
            continue
        # Deliberately omit titles, bodies, arbitrary reason strings, and session
        # metadata.  This substrate carries only durable structural identities.
        by_pair[(left, right)].append((observed, {
            "event": event,
            "node_a": node_a,
            "node_b": node_b,
            "workstream_ids": [left, right],
        }))
    result: dict[tuple[int, int], list[dict]] = {}
    for pair, values in sorted(by_pair.items()):
        values.sort(key=lambda item: (
            item[0], item[1]["event"], item[1]["node_a"], item[1]["node_b"],
        ))
        result[pair] = [
            signal for _observed, signal in values[-HEAL_SIGNALS_PER_PAIR_LIMIT:]
        ]
    return result


def _fisher_z(value: float | None) -> float | None:
    if value is None:
        return None
    bounded = max(-0.999999, min(0.999999, float(value)))
    return round(0.5 * math.log((1.0 + bounded) / (1.0 - bounded)), 6)


def _select_eligible_sessions(
    rows: Sequence[Mapping[str, Any]],
    session_started: Mapping[str, str],
    *,
    now: datetime,
) -> list[dict]:
    by_session: dict[str, list[datetime]] = defaultdict(list)
    for row in rows:
        if not _is_contact_event(row):
            continue
        ts = _parse_ts(row.get("ts"))
        if ts is not None:
            by_session[str(row["session_id"])].append(ts)
    cap_cutoff = now - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS)
    floor_cutoff = now - timedelta(days=ELIGIBLE_WINDOW_FLOOR_DAYS)
    candidates: list[tuple[datetime, str]] = []
    for session_id, event_times in by_session.items():
        started = _parse_ts(session_started.get(session_id)) or min(event_times)
        if started >= cap_cutoff:
            candidates.append((started, session_id))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = candidates[:ELIGIBLE_SESSION_LIMIT]
    selected_ids = {session_id for _started, session_id in selected}
    for started, session_id in candidates[ELIGIBLE_SESSION_LIMIT:]:
        if started >= floor_cutoff and session_id not in selected_ids:
            selected.append((started, session_id))
            selected_ids.add(session_id)
    selected.sort(key=lambda item: (item[0], item[1]))
    return [
        {"session_id": session_id, "started_at": _stamp(started)}
        for started, session_id in selected
    ]


def _structural_tier2(
    conn: sqlite3.Connection,
    event_rows: Sequence[Mapping[str, Any]],
    selected_sessions: set[str],
) -> dict[tuple[int, int], dict[str, list[int] | list[str]]]:
    targets: dict[int, set[int]] = defaultdict(set)
    edges = conn.execute(
        "SELECT e.id, e.relation, e.src AS src_id, e.dst AS dst_id, "
        "s.kind AS src_kind, s.workstream_id AS src_workstream_id, "
        "d.kind AS dst_kind, d.workstream_id AS dst_workstream_id "
        "FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
        "WHERE e.status='active' AND s.status!='stale' AND d.status!='stale'"
    ).fetchall()
    for raw in edges:
        row = dict(raw)
        src_lane = _node_lane(row, "src")
        dst_lane = _node_lane(row, "dst")
        strong_relation = row["relation"] in _STRUCTURAL_TARGET_RELATIONS
        if src_lane is not None and dst_lane != src_lane and (
            strong_relation or row["dst_kind"] in _STRUCTURAL_TARGET_KINDS
        ):
            targets[src_lane].add(int(row["dst_id"]))
        if dst_lane is not None and src_lane != dst_lane and (
            strong_relation or row["src_kind"] in _STRUCTURAL_TARGET_KINDS
        ):
            targets[dst_lane].add(int(row["src_id"]))

    cross_groups: dict[tuple[str, int, int], set[int]] = defaultdict(set)
    for row in event_rows:
        session_id = row.get("session_id")
        if (
            session_id not in selected_sessions
            or row.get("source") not in {"graph", "gate"}
            or row.get("seed_node_id") is None
            or row.get("turn") is None
            or int(row["turn"]) <= 0
        ):
            continue
        lane = row.get("workstream_id_at_event")
        if lane is None:
            continue
        key = (str(session_id), int(row["turn"]), int(row["seed_node_id"]))
        cross_groups[key].add(int(lane))
    cross_sessions: dict[tuple[int, int], set[str]] = defaultdict(set)
    for (session_id, _turn, _seed), lanes in cross_groups.items():
        for left, right in combinations(sorted(lanes), 2):
            cross_sessions[(left, right)].add(session_id)

    lane_ids = sorted(targets)
    result: dict[tuple[int, int], dict[str, list[int] | list[str]]] = {}
    pairs = set(combinations(lane_ids, 2)) | set(cross_sessions)
    for pair in sorted(pairs):
        left, right = pair
        result[pair] = {
            "shared_target_ids": sorted(targets[left].intersection(targets[right])),
            "cross_path_sessions": sorted(cross_sessions.get(pair, set())),
        }
    return result


def _weak_tier3_annotations(
    conn: sqlite3.Connection,
    groups: Sequence[Mapping[str, Any]],
    lane_ids: Iterable[int],
) -> dict[tuple[int, int], dict]:
    """Project weak annotations which are never used as qualification gates."""
    lanes = sorted({int(value) for value in lane_ids})
    artifacts_by_lane: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute(
        "SELECT n.workstream_id,na.artifact_id FROM node_artifact na "
        "JOIN nodes n ON n.id=na.node_id WHERE n.status!='stale' "
        "AND n.workstream_id IS NOT NULL ORDER BY n.workstream_id,na.artifact_id"
    ).fetchall():
        artifacts_by_lane[int(row["workstream_id"])].add(int(row["artifact_id"]))

    vectors_by_lane: dict[int, list[list[float]]] = defaultdict(list)
    for row in conn.execute(
        "SELECT workstream_id,embedding FROM nodes WHERE status!='stale' "
        "AND workstream_id IS NOT NULL AND embedding IS NOT NULL "
        "ORDER BY workstream_id,id"
    ).fetchall():
        values = _embedding_values(row["embedding"])
        if values:
            vectors_by_lane[int(row["workstream_id"])].append(values)

    centroids: dict[int, list[float]] = {}
    for lane_id, vectors in vectors_by_lane.items():
        dimension = len(vectors[0])
        compatible = [values for values in vectors if len(values) == dimension]
        if compatible:
            centroids[lane_id] = [
                sum(values[index] for values in compatible) / len(compatible)
                for index in range(dimension)
            ]

    tree_ids_by_pair: dict[tuple[int, int], set[int]] = defaultdict(set)
    for group in groups:
        group_lanes = sorted({
            int(lane_id) for lane_id, members
            in (group.get("lane_member_ids") or {}).items() if members
        })
        for pair in combinations(group_lanes, 2):
            tree_ids_by_pair[pair].add(int(group["tree_id"]))

    annotations: dict[tuple[int, int], dict] = {}
    for pair in combinations(lanes, 2):
        left, right = pair
        artifact_ids = sorted(
            artifacts_by_lane[left].intersection(artifacts_by_lane[right])
        )
        tree_ids = sorted(tree_ids_by_pair.get(pair, set()))
        similarity = _cosine(
            centroids.get(left, []), centroids.get(right, []),
        )
        annotations[pair] = {
            "qualification_role": "annotation_only",
            "artifact_overlap_count": len(artifact_ids),
            "artifact_overlap_ids": artifact_ids[:WEAK_ANNOTATION_ID_LIMIT],
            "artifact_overlap_truncated": len(artifact_ids) > WEAK_ANNOTATION_ID_LIMIT,
            "tree_co_membership": bool(tree_ids),
            "tree_ids": tree_ids[:WEAK_ANNOTATION_ID_LIMIT],
            "tree_ids_truncated": len(tree_ids) > WEAK_ANNOTATION_ID_LIMIT,
            "centroid_cosine": (
                round(float(similarity), 6) if similarity is not None else None
            ),
            "centroid_fisher_z": _fisher_z(similarity),
            "centroid_member_counts": {
                str(left): len(vectors_by_lane.get(left, [])),
                str(right): len(vectors_by_lane.get(right, [])),
            },
        }
    return annotations


def collect_inputs(
    conn: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
    project_path: str | None = None,
) -> dict:
    """Build a JSON-like, read-only input bundle for pure derivation."""
    anchor = _parse_ts(now) or datetime.now(timezone.utc)
    cap_cutoff = _stamp(anchor - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS))
    event_rows = [
        dict(row) for row in conn.execute(
            "SELECT * FROM retrieval_events WHERE ts >= ? ORDER BY ts, id",
            (cap_cutoff,),
        ).fetchall()
    ]
    session_started = {
        str(row["id"]): row["started_at"]
        for row in conn.execute("SELECT id, started_at FROM sessions").fetchall()
    }
    eligible_sessions = _select_eligible_sessions(
        event_rows, session_started, now=anchor,
    )
    selected_ids = {row["session_id"] for row in eligible_sessions}

    contacts: dict[int, set[str]] = defaultdict(set)
    last_contact: dict[int, str] = {}
    orphan_events: dict[int, list[dict]] = defaultdict(list)
    for row in event_rows:
        if not _is_contact_event(row):
            continue
        lane = row.get("workstream_id_at_event")
        if lane is not None:
            lane_id = int(lane)
            if row["ts"] > last_contact.get(lane_id, ""):
                last_contact[lane_id] = row["ts"]
            if row["session_id"] in selected_ids:
                contacts[lane_id].add(str(row["session_id"]))
        else:
            if row["session_id"] not in selected_ids:
                continue
            orphan_events[int(row["node_id"])].append({
                "session_id": str(row["session_id"]),
                "ts": row["ts"],
                "source": row["source"],
            })

    lanes: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT n.id, n.title, n.body, n.status, n.created_at, n.updated_at, "
        "COALESCE(f.pinned,0) AS pinned "
        "FROM nodes n LEFT JOIN focus f ON f.workstream_id=n.id "
        "WHERE n.kind='workstream' AND n.status!='stale' ORDER BY n.id"
    ).fetchall():
        lane_id = int(row["id"])
        member_activity = conn.execute(
            "SELECT MAX(updated_at) FROM nodes WHERE status!='stale' AND workstream_id=?",
            (lane_id,),
        ).fetchone()[0]
        activity_times = [
            parsed for parsed in (
                _parse_ts(member_activity), _parse_ts(last_contact.get(lane_id)),
            ) if parsed is not None
        ]
        latest_activity = max(activity_times) if activity_times else None
        member_rows = conn.execute(
            "SELECT id,kind FROM nodes WHERE status!='stale' AND workstream_id=? "
            "AND kind NOT IN ('workstream','priority') ORDER BY id",
            (lane_id,),
        ).fetchall()
        forward_member_ids = [
            int(member["id"]) for member in member_rows
            if member["kind"] in _FORWARD_KINDS
        ]
        resolution_rows = conn.execute(
            "SELECT e.id,e.dst,e.created_by FROM edges e "
            "WHERE e.status='active' AND e.relation IN "
            "('resolves','supersedes','replaces') AND e.dst IN "
            "(SELECT id FROM nodes WHERE status!='stale' AND workstream_id=? "
            "AND kind IN ('open_question','idea','progress')) "
            "AND e.created_by IS NOT NULL AND TRIM(e.created_by)!='' ORDER BY e.id",
            (lane_id,),
        ).fetchall()
        resolved_forward_ids = sorted({int(edge["dst"]) for edge in resolution_rows})
        resolution_edge_ids = [int(edge["id"]) for edge in resolution_rows]
        unknown_inbound = conn.execute(
            "SELECT e.id FROM edges e JOIN nodes n ON n.id=e.src "
            "WHERE e.dst=? AND e.status='active' AND n.kind!='workstream' "
            "AND e.relation NOT IN ('advances','motivates','depends_on') ORDER BY e.id",
            (lane_id,),
        ).fetchall()
        feeder_snapshot = feeders.open_feeder_snapshot(conn, lane_id)
        lanes[lane_id] = {
            **dict(row),
            "id": lane_id,
            "pinned": bool(row["pinned"]),
            "open_feeder_count": len(feeder_snapshot),
            "open_feeder_ids": sorted({int(item["id"]) for item in feeder_snapshot}),
            "active_priority_count": int(conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind='priority' "
                "AND status!='stale' AND workstream_id=?", (lane_id,),
            ).fetchone()[0]),
            "last_contact_at": last_contact.get(lane_id),
            "member_activity_at": member_activity,
            "active_member_count": len(member_rows),
            "member_ids": [int(member["id"]) for member in member_rows],
            "unknown_inbound_edge_ids": [int(edge["id"]) for edge in unknown_inbound],
            "done_when": _done_when(row["body"]),
            "forward_member_ids": forward_member_ids,
            "resolved_forward_member_ids": resolved_forward_ids,
            "resolution_edge_ids": resolution_edge_ids,
            "resolution_evidence": [
                {
                    "edge_id": int(edge["id"]),
                    "forward_member_id": int(edge["dst"]),
                    "created_by": str(edge["created_by"]),
                }
                for edge in resolution_rows
            ],
            "resolution_density": (
                len(set(resolved_forward_ids).intersection(forward_member_ids))
                / len(forward_member_ids)
                if forward_member_ids else 0.0
            ),
            "recently_active": bool(
                latest_activity is not None
                and latest_activity >= anchor - timedelta(days=RECENT_ACTIVE_LANE_DAYS)
            ),
        }

    tree_times = _latest_tree_signal_times(project_path, now=anchor)
    grouped: dict[int, dict] = {}
    for row in conn.execute(
        "SELECT p.id AS tree_id, p.updated_at AS tree_updated_at, "
        "c.id, c.kind, c.created_at, c.content_hash, c.embedding, c.workstream_id "
        "FROM nodes p JOIN nodes c ON c.parent_id=p.id "
        "WHERE p.kind='summary' AND p.status!='stale' "
        "AND c.status!='stale' "
        "AND c.kind NOT IN ('summary','workstream','priority') ORDER BY p.id,c.id"
    ).fetchall():
        tree_id = int(row["tree_id"])
        group = grouped.setdefault(tree_id, {
            "tree_id": tree_id,
            "tree_derived_at": row["tree_updated_at"],
            "tree_time_source": "summary_updated_at",
            "member_ids": [],
            "member_kinds": [],
            "member_created_at": [],
            "member_content_hashes": [],
            "member_embeddings": {},
            "member_artifacts": {},
            "fact_linked_to_nonfact_ids": [],
            "shared_target_ids": [],
            "target_member_ids": {},
            "lane_member_ids": {},
            "adopt_shared_targets": {},
            "member_feeder_relations": {},
        })
        if row["workstream_id"] is not None:
            lane_key = str(int(row["workstream_id"]))
            group["lane_member_ids"].setdefault(lane_key, []).append(int(row["id"]))
            continue
        group["member_ids"].append(int(row["id"]))
        group["member_kinds"].append(row["kind"])
        group["member_created_at"].append(row["created_at"])
        group["member_content_hashes"].append(row["content_hash"])
        embedding = _embedding_values(row["embedding"])
        if embedding is not None:
            group["member_embeddings"][str(int(row["id"]))] = embedding
    for group in grouped.values():
        members = tuple(sorted(group["member_ids"]))
        if not members:
            continue
        all_members = tuple(sorted({
            *members,
            *(
                int(node_id)
                for lane_members in group["lane_member_ids"].values()
                for node_id in lane_members
            ),
        }))
        placeholders = ",".join("?" for _ in members)
        artifact_rows = conn.execute(
            f"SELECT node_id, artifact_id FROM node_artifact "
            f"WHERE node_id IN ({placeholders}) ORDER BY node_id,artifact_id",
            members,
        ).fetchall()
        artifact_map: dict[str, list[int]] = defaultdict(list)
        for artifact in artifact_rows:
            artifact_map[str(int(artifact["node_id"]))].append(int(artifact["artifact_id"]))
        group["member_artifacts"] = dict(artifact_map)
        member_set = set(members)
        all_member_set = set(all_members)
        lane_by_member = {
            int(node_id): int(lane_id)
            for lane_id, lane_members in group["lane_member_ids"].items()
            for node_id in lane_members
        }
        kind_by_id = {
            int(node_id): str(kind)
            for node_id, kind in zip(group["member_ids"], group["member_kinds"])
        }
        target_members: dict[int, set[int]] = defaultdict(set)
        feeder_relations_by_lane: dict[int, dict[int, str]] = defaultdict(dict)
        fact_linked: set[int] = set()
        edge_rows = conn.execute(
            f"SELECT e.src,e.dst,e.relation,s.kind AS src_kind,d.kind AS dst_kind "
            f"FROM edges e JOIN nodes s ON s.id=e.src JOIN nodes d ON d.id=e.dst "
            f"WHERE e.status='active' AND (e.src IN "
            f"({','.join('?' for _ in all_members)}) OR e.dst IN "
            f"({','.join('?' for _ in all_members)})) ORDER BY e.id",
            [*all_members, *all_members],
        ).fetchall()
        for edge in edge_rows:
            src, dst = int(edge["src"]), int(edge["dst"])
            if src in member_set and dst in member_set:
                if kind_by_id.get(src) == "fact" and kind_by_id.get(dst) != "fact":
                    fact_linked.add(src)
                if kind_by_id.get(dst) == "fact" and kind_by_id.get(src) != "fact":
                    fact_linked.add(dst)
                continue
            if src in all_member_set and dst not in all_member_set:
                member_id, target_id, target_kind = src, dst, edge["dst_kind"]
            elif dst in all_member_set and src not in all_member_set:
                member_id, target_id, target_kind = dst, src, edge["src_kind"]
            else:  # pragma: no cover - constrained by SQL
                continue
            if (
                edge["relation"] in _STRUCTURAL_TARGET_RELATIONS
                or target_kind in _STRUCTURAL_TARGET_KINDS
            ):
                target_members[target_id].add(member_id)
            if (
                src in member_set
                and target_id in {int(value) for value in group["lane_member_ids"]}
                and edge["relation"] in feeders.FEEDER_RELATIONS
            ):
                feeder_relations_by_lane[target_id][src] = str(edge["relation"])
        group["fact_linked_to_nonfact_ids"] = sorted(fact_linked)
        group["shared_target_ids"] = sorted(
            target_id for target_id, linked_members in target_members.items()
            if len(linked_members) >= 2
        )
        group["target_member_ids"] = {
            str(target_id): sorted(linked_members)
            for target_id, linked_members in sorted(target_members.items())
        }
        group["adopt_shared_targets"] = {
            str(lane_id): sorted(
                target_id for target_id, linked_members in target_members.items()
                if any(member in member_set for member in linked_members)
                and any(lane_by_member.get(member) == lane_id for member in linked_members)
            )
            for lane_id in sorted(set(lane_by_member.values()))
        }
        group["member_feeder_relations"] = {
            str(lane_id): {
                str(node_id): relation
                for node_id, relation in sorted(member_relations.items())
            }
            for lane_id, member_relations in sorted(feeder_relations_by_lane.items())
        }
        logged = tree_times.get(members)
        if logged is not None:
            group["tree_derived_at"] = logged
            group["tree_time_source"] = "lifecycle_log"

    orphan_count = int(conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE status!='stale' "
        "AND workstream_id IS NULL AND kind NOT IN ('summary','workstream','priority')"
    ).fetchone()[0])
    observation_row = conn.execute(
        "SELECT MIN(ts) FROM retrieval_events WHERE session_id IS NOT NULL "
        "AND (source='write' OR (turn>0 AND source IN "
        "('prompt','graph','tool','gate')))"
    ).fetchone()
    observation_start = observation_row[0] if observation_row else None
    probation_lanes: dict[int, dict] = {}
    seen_open_lanes: set[int] = set()
    for open_row in conn.execute(
        "SELECT op_key,dst_workstream_id,payload_json,applied_at FROM workstream_ops "
        "WHERE op='OPEN' AND state='applied' AND origin='auto' "
        "AND dst_workstream_id IS NOT NULL ORDER BY id DESC"
    ).fetchall():
        lane_id = int(open_row["dst_workstream_id"])
        if lane_id in seen_open_lanes:
            continue
        seen_open_lanes.add(lane_id)
        if lane_id not in lanes:
            continue
        try:
            payload = json.loads(open_row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        probation = payload.get("probation")
        if not isinstance(probation, Mapping) or probation.get("active") is not True:
            continue
        opened_at = _parse_ts(probation.get("opened_at") or open_row["applied_at"])
        if opened_at is None:
            continue
        try:
            target = max(1, int(
                probation.get("eligible_session_target")
                or probation.get("eligible_sessions_target")
                or 10
            ))
        except (TypeError, ValueError):
            continue
        after_sessions = [
            session["session_id"] for session in eligible_sessions
            if (_parse_ts(session["started_at"]) or opened_at) > opened_at
        ]
        contact_sessions_after = sorted({
            str(event["session_id"])
            for event in event_rows
            if _is_contact_event(event)
            and event.get("workstream_id_at_event") == lane_id
            and (_parse_ts(event.get("ts")) or opened_at) > opened_at
        })
        eligible_session_count = len(set(after_sessions))
        if eligible_session_count >= target and contact_sessions_after:
            # Successful probation graduates dynamically.  The immutable OPEN
            # receipt remains active for audit, but no longer overrides ordinary
            # lifecycle handling once both the target and a real contact exist.
            continue
        lane = lanes[lane_id]
        probation_lanes[lane_id] = {
            "active": True,
            "open_op_key": open_row["op_key"],
            "opened_at": _stamp(opened_at),
            "eligible_session_target": target,
            "eligible_sessions_after_open": sorted(after_sessions),
            "eligible_session_count": eligible_session_count,
            "contact_sessions_after_open": contact_sessions_after,
            "contact_session_count": len(contact_sessions_after),
            "window_within_retention": bool(
                opened_at >= anchor - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS)
            ),
            "retained_since": _stamp(
                anchor - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS)
            ),
            "target_sessions_observable": eligible_session_count >= target,
            "member_ids": sorted({int(value) for value in lane["member_ids"]}),
            "open_feeder_ids": sorted({int(value) for value in lane["open_feeder_ids"]}),
            "probation_payload": dict(probation),
        }
    orphan_groups = [
        grouped[key] for key in sorted(grouped) if grouped[key]["member_ids"]
    ]
    tier2 = _structural_tier2(conn, event_rows, selected_ids)
    for pair, signals in _latest_heal_pair_signals(
        project_path, now=anchor,
    ).items():
        tier2.setdefault(pair, {
            "shared_target_ids": [], "cross_path_sessions": [],
        })["heal_signals"] = signals
    tier3 = _weak_tier3_annotations(conn, orphan_groups, lanes)
    return {
        "now": _stamp(anchor),
        "eligible_sessions": eligible_sessions,
        "contacts": {lane: sorted(sessions) for lane, sessions in sorted(contacts.items())},
        "lanes": lanes,
        "tier2": {
            f"{left}:{right}": value for (left, right), value in sorted(tier2.items())
        },
        "tier3": {
            f"{left}:{right}": value for (left, right), value in sorted(tier3.items())
        },
        "orphan_groups": orphan_groups,
        "orphan_events": {
            node_id: sorted(rows, key=lambda row: (row["ts"], row["session_id"]))
            for node_id, rows in sorted(orphan_events.items())
        },
        "orphan_count": orphan_count,
        "observation_start": observation_start,
        "recent_active_lane_count": sum(
            1 for lane in lanes.values() if lane["recently_active"]
        ),
        "recent_active_lane_cap": RECENT_ACTIVE_LANE_CAP,
        "probation_lanes": probation_lanes,
    }


def _contact_matrix(
    lane_ids: Iterable[int], contacts: Mapping[int, Sequence[str]],
) -> list[dict]:
    rows: list[dict] = []
    sets = {int(lane): set(values) for lane, values in contacts.items()}
    for left, right in combinations(sorted({int(value) for value in lane_ids}), 2):
        left_sessions = sets.get(left, set())
        right_sessions = sets.get(right, set())
        union = left_sessions | right_sessions
        co = left_sessions & right_sessions
        rows.append({
            "left": left,
            "right": right,
            "left_sessions": len(left_sessions),
            "right_sessions": len(right_sessions),
            "co_contact_sessions": len(co),
            "union_sessions": len(union),
            "jaccard": round(len(co) / len(union), 6) if union else 0.0,
        })
    return rows


def _merge_candidates(inputs: Mapping[str, Any], matrix: Sequence[dict]) -> list[dict]:
    tier2 = inputs.get("tier2") or {}
    tier3 = inputs.get("tier3") or {}
    lanes = {int(key): value for key, value in (inputs.get("lanes") or {}).items()}
    candidates: list[dict] = []
    for contact in matrix:
        if contact["co_contact_sessions"] < MERGE_MIN_CO_CONTACT:
            continue
        if contact["jaccard"] < MERGE_MIN_JACCARD:
            continue
        left, right = int(contact["left"]), int(contact["right"])
        corroboration = tier2.get(f"{left}:{right}") or tier2.get(f"{right}:{left}") or {}
        shared = sorted({int(value) for value in corroboration.get("shared_target_ids", [])})
        crossing = sorted({str(value) for value in corroboration.get("cross_path_sessions", [])})
        heal_signals: list[dict] = []
        for raw_signal in (corroboration.get("heal_signals") or [
        ])[:HEAL_SIGNALS_PER_PAIR_LIMIT]:
            if not isinstance(raw_signal, Mapping):
                continue
            event = str(raw_signal.get("event") or "")
            if event not in {"cross_lane_duplicate", "cross_lane_contradiction"}:
                continue
            try:
                node_a = int(raw_signal["node_a"])
                node_b = int(raw_signal["node_b"])
            except (KeyError, TypeError, ValueError):
                continue
            heal_signals.append({
                "event": event,
                "node_a": node_a,
                "node_b": node_b,
                "workstream_ids": [left, right],
            })
        tier2_inputs: list[str] = []
        if len(shared) >= TIER2_MIN_SHARED_TARGETS:
            tier2_inputs.append("shared_targets")
        if len(crossing) >= TIER2_MIN_CROSS_PATH_SESSIONS:
            tier2_inputs.append("repeated_cross_lane_paths")
        for event in ("cross_lane_duplicate", "cross_lane_contradiction"):
            if any(signal["event"] == event for signal in heal_signals):
                tier2_inputs.append(event)
        if not tier2_inputs:
            continue
        candidate_key = lifecycle_signals.make_candidate_key("MERGE", [left, right])
        left_lane = lanes.get(left, {})
        right_lane = lanes.get(right, {})
        direction_basis: str | None = None
        source_id: int | None = None
        absorber_id: int | None = None
        if contact["left_sessions"] != contact["right_sessions"]:
            direction_basis = "contact_sessions"
            source_id, absorber_id = (
                (left, right)
                if contact["left_sessions"] < contact["right_sessions"]
                else (right, left)
            )
        elif int(left_lane.get("active_member_count") or 0) \
                != int(right_lane.get("active_member_count") or 0):
            direction_basis = "active_member_count"
            source_id, absorber_id = (
                (left, right)
                if int(left_lane.get("active_member_count") or 0)
                < int(right_lane.get("active_member_count") or 0)
                else (right, left)
            )
        else:
            left_activity = _parse_ts(left_lane.get("member_activity_at"))
            right_activity = _parse_ts(right_lane.get("member_activity_at"))
            if left_activity != right_activity:
                direction_basis = "member_activity_at"
                floor = datetime.min.replace(tzinfo=timezone.utc)
                source_id, absorber_id = (
                    (left, right)
                    if (left_activity or floor) < (right_activity or floor)
                    else (right, left)
                )
        ambiguity_reason = None
        apply_request = None
        if source_id is None or absorber_id is None:
            ambiguity_reason = "merge_direction_tied"
        else:
            unknown_edges = sorted({
                int(value)
                for value in lanes.get(source_id, {}).get("unknown_inbound_edge_ids", [])
            })
            if unknown_edges:
                ambiguity_reason = "unknown_inbound_relations"
            else:
                apply_request = {
                    "source_workstream_id": source_id,
                    "absorber_workstream_id": absorber_id,
                    "dispositions": {},
                    "evidence": {
                        "candidate_key": candidate_key,
                        "co_contact_sessions": contact["co_contact_sessions"],
                        "eligible_session_count": len(inputs.get("eligible_sessions") or []),
                        "jaccard": contact["jaccard"],
                        "tier2_inputs": tier2_inputs,
                        "shared_target_ids": shared,
                        "cross_path_sessions": crossing,
                        "heal_signals": heal_signals,
                        "direction_basis": direction_basis,
                    },
                    "priority_policy_complete": True,
                }
        signal = {
            **contact,
            "tier1": "co_contact",
            "tier2_inputs": tier2_inputs,
            "tier2_qualified_by": tier2_inputs,
            "shared_target_ids": shared,
            "cross_path_sessions": crossing,
            "heal_signals": heal_signals,
            "tier3_annotations": dict(
                tier3.get(f"{left}:{right}")
                or tier3.get(f"{right}:{left}")
                or {}
            ),
            "direction": {
                "source_workstream_id": source_id,
                "absorber_workstream_id": absorber_id,
                "basis": direction_basis,
                "unambiguous": apply_request is not None,
            },
            "ambiguity_reason": ambiguity_reason,
            "qualified": True,
            "mode": "shadow",
            "substrate_version": SUBSTRATE_VERSION,
        }
        if int(inputs.get("recent_active_lane_count") or 0) >= int(
            inputs.get("recent_active_lane_cap") or RECENT_ACTIVE_LANE_CAP
        ):
            signal["cap_pressure"] = True
            signal["governor_reason"] = "cap_pressure"
        if apply_request is not None:
            signal["apply_request"] = apply_request
        candidates.append({
            "candidate_key": candidate_key,
            "op": "MERGE",
            "signal": signal,
        })
    candidates.sort(key=lambda row: (
        -row["signal"]["co_contact_sessions"],
        -row["signal"]["jaccard"],
        row["candidate_key"],
    ))
    return candidates


def _open_groups(inputs: Mapping[str, Any], *, now: datetime) -> tuple[list[dict], list[dict], int]:
    event_map = {
        int(node_id): rows for node_id, rows in (inputs.get("orphan_events") or {}).items()
    }
    diagnostics: list[dict] = []
    candidates: list[dict] = []
    stale_count = 0
    active_lane_count = int(inputs.get("recent_active_lane_count") or 0)
    active_lane_cap = int(
        inputs.get("recent_active_lane_cap") or RECENT_ACTIVE_LANE_CAP
    )
    cap_reached = active_lane_count >= active_lane_cap
    for group in inputs.get("orphan_groups") or []:
        derived = _parse_ts(group.get("tree_derived_at"))
        stale = derived is None or now - derived > timedelta(days=TREE_MAX_AGE_DAYS)
        mixed_lane_ids = sorted({
            int(lane_id) for lane_id, members
            in (group.get("lane_member_ids") or {}).items() if members
        })
        units = _dedupe_open_units(group)
        member_ids = sorted({
            node_id for unit in units for node_id in unit["node_ids"]
        })
        fact_linked = {
            int(value) for value in group.get("fact_linked_to_nonfact_ids") or []
        }
        eligible_units = [
            unit for unit in units
            if any(kind != "fact" for kind in unit["kinds"])
            or bool(set(unit["node_ids"]).intersection(fact_linked))
        ]
        counted_member_ids = sorted({
            node_id for unit in eligible_units for node_id in unit["node_ids"]
        })
        # Tier-1 contact must come from the same bounded evidence units that
        # count toward K.  An excluded standalone fact cannot lend behavioral
        # recurrence to otherwise untouched units in its tree cluster.
        events = [
            event
            for node_id in counted_member_ids
            for event in event_map.get(node_id, [])
        ]
        sessions = sorted({str(event["session_id"]) for event in events})
        unit_times: list[datetime] = []
        unit_days: list[str] = []
        for unit in eligible_units:
            times = sorted(
                parsed for parsed in (
                    _parse_ts(value) for value in unit.get("created_at") or []
                ) if parsed is not None
            )
            if times:
                unit_times.append(times[0])
                unit_days.append(times[0].date().isoformat())
        days = sorted(set(unit_days))
        span_hours = (
            (max(unit_times) - min(unit_times)).total_seconds() / 3600.0
            if len(unit_times) >= 2 else 0.0
        )
        kinds = sorted({kind for unit in eligible_units for kind in unit["kinds"]})
        artifacts = sorted({
            artifact for unit in eligible_units for artifact in unit["artifact_ids"]
        })
        target_members = group.get("target_member_ids") or {}
        node_to_unit = {
            node_id: index
            for index, unit in enumerate(eligible_units)
            for node_id in unit["node_ids"]
        }
        shared_targets: list[int] = []
        for target_id, linked_members in target_members.items():
            linked_units = {
                node_to_unit[int(node_id)] for node_id in linked_members
                if int(node_id) in node_to_unit
            }
            if len(linked_units) >= 2:
                shared_targets.append(int(target_id))
        shared_targets.sort()
        contact_tier1 = len(sessions) >= OPEN_MIN_CONTACT_SESSIONS
        shared_target_tier1 = bool(shared_targets)
        tier1_sources = []
        if contact_tier1:
            tier1_sources.append("multi_session_orphan_contact")
        if shared_target_tier1:
            tier1_sources.append("shared_feeder_or_decision_target")
        day_counts = {
            day: unit_days.count(day) for day in sorted(set(unit_days))
        }
        half_day_guard = bool(
            eligible_units
            and (max(day_counts.values(), default=0) <= len(eligible_units) / 2)
        )
        diversity_guard = len(kinds) >= 2 or len(artifacts) >= 2
        evidence_qualified = bool(
            not stale
            and not mixed_lane_ids
            and len(eligible_units) >= OPEN_MIN_UNITS
            and tier1_sources
            and len(days) >= OPEN_MIN_CONTACT_DAYS
            and span_hours >= OPEN_MIN_SPAN_HOURS
            and bool(set(kinds).intersection(_FORWARD_KINDS))
            and diversity_guard
            and half_day_guard
        )
        qualified = evidence_qualified and not cap_reached
        diagnostic = {
            "tree_id": int(group["tree_id"]),
            "tree_derived_at": group.get("tree_derived_at"),
            "tree_time_source": group.get("tree_time_source"),
            "tree_stale": stale,
            "member_ids": member_ids,
            "mixed_lane_ids": mixed_lane_ids,
            "counted_member_ids": counted_member_ids,
            "member_kinds": kinds,
            "raw_member_count": len(member_ids),
            "unit_count": len(eligible_units),
            "deduped_units": [unit["node_ids"] for unit in eligible_units],
            "fact_units_excluded": len(units) - len(eligible_units),
            "artifact_ids": artifacts,
            "diversity_guard": diversity_guard,
            "contact_sessions": sessions,
            "contact_session_count": len(sessions),
            "unit_days": days,
            "unit_day_counts": day_counts,
            "half_day_guard": half_day_guard,
            "span_hours": round(span_hours, 3),
            "shared_target_ids": shared_targets,
            "tier1_sources": tier1_sources,
            "tier1_present": bool(tier1_sources),
            "recent_active_lane_count": active_lane_count,
            "recent_active_lane_cap": active_lane_cap,
            "cap_pressure": bool(evidence_qualified and cap_reached),
            "evidence_qualified": evidence_qualified,
            "qualified": qualified,
            "reason": (
                "stale_tree_signal" if stale
                else "mixed_lane_adopt_candidate" if mixed_lane_ids
                else "cap_pressure" if evidence_qualified and cap_reached
                else "qualified" if qualified
                else "insufficient_tier1_or_recurrence"
            ),
        }
        diagnostics.append(diagnostic)
        if stale:
            stale_count += 1
        if not evidence_qualified:
            continue
        unit_hashes = [
            _canonical_hash(
                "unit",
                {
                    "content_hashes": unit["content_hashes"],
                    "representative_node_id": min(unit["node_ids"]),
                },
            )
            for unit in eligible_units
        ]
        candidates.append({
            "candidate_key": lifecycle_signals.make_candidate_key(
                "OPEN", [], unit_hashes,
            ),
            "op": "OPEN",
            "signal": {
                **diagnostic,
                # Excluded facts remain visible in the diagnostic snapshot but
                # cannot be proposed as seed members for the resulting lane.
                "member_ids": counted_member_ids,
                "tier1": tier1_sources[0],
                "tree_role": "grouping_only",
                "mode": "shadow",
                "substrate_version": SUBSTRATE_VERSION,
            },
        })
    candidates.sort(key=lambda row: (
        -row["signal"]["contact_session_count"],
        -row["signal"]["unit_count"],
        row["candidate_key"],
    ))
    diagnostics.sort(key=lambda row: row["tree_id"])
    return diagnostics, candidates, stale_count


def _adopt_candidates(
    inputs: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    """Derive ADOPT handoffs only from fresh, corroborated mixed tree groups."""
    lanes = {int(key): value for key, value in (inputs.get("lanes") or {}).items()}
    event_map = {
        int(node_id): rows for node_id, rows in (inputs.get("orphan_events") or {}).items()
    }
    diagnostics: list[dict] = []
    aggregate: dict[int, dict] = {}
    for group in inputs.get("orphan_groups") or []:
        lane_members = {
            int(lane_id): sorted({int(value) for value in members})
            for lane_id, members in (group.get("lane_member_ids") or {}).items()
            if members
        }
        if not lane_members:
            continue
        derived = _parse_ts(group.get("tree_derived_at"))
        stale = derived is None or now - derived > timedelta(days=TREE_MAX_AGE_DAYS)
        units = _dedupe_open_units(group)
        fact_linked = {
            int(value) for value in group.get("fact_linked_to_nonfact_ids") or []
        }
        eligible_units = [
            unit for unit in units
            if any(kind != "fact" for kind in unit["kinds"])
            or bool(set(unit["node_ids"]).intersection(fact_linked))
        ]
        eligible_member_ids = sorted({
            node_id for unit in eligible_units for node_id in unit["node_ids"]
        })
        unit_session_ids: list[list[str]] = []
        for unit in eligible_units:
            unit_session_ids.append(sorted({
                str(event["session_id"])
                for node_id in unit["node_ids"]
                for event in event_map.get(node_id, [])
                if event.get("session_id") is not None
            }))
        sessions = sorted({
            session_id for values in unit_session_ids for session_id in values
        })
        all_units_contacted = bool(eligible_units) and all(unit_session_ids)
        has_forward_unit = any(
            kind in _FORWARD_KINDS
            for unit in eligible_units for kind in unit["kinds"]
        )
        target_members = group.get("target_member_ids") or {}
        eligible_member_set = set(eligible_member_ids)
        for lane_id, in_tree_lane_members in sorted(lane_members.items()):
            declared_targets = sorted({
                int(value) for value in (
                    (group.get("adopt_shared_targets") or {}).get(str(lane_id))
                    or (group.get("adopt_shared_targets") or {}).get(lane_id)
                    or []
                )
            })
            shared_targets = [
                target_id for target_id in declared_targets
                if not target_members
                or bool(eligible_member_set.intersection({
                    int(value) for value in (
                        target_members.get(str(target_id))
                        or target_members.get(target_id)
                        or []
                    )
                }))
            ]
            lane = lanes.get(lane_id)
            active_lane = bool(lane is not None and lane.get("status") != "stale")
            tier1 = bool(all_units_contacted and len(sessions) >= 2)
            tier2 = bool(shared_targets)
            qualified = bool(
                not stale
                and active_lane
                and eligible_units
                and has_forward_unit
                and tier1
                and tier2
            )
            diagnostic = {
                "tree_id": int(group["tree_id"]),
                "tree_derived_at": group.get("tree_derived_at"),
                "tree_stale": stale,
                "workstream_id": lane_id,
                "lane_member_ids": in_tree_lane_members,
                "member_ids": eligible_member_ids,
                "unit_count": len(eligible_units),
                "unit_contact_sessions": unit_session_ids,
                "contact_session_ids": sessions,
                "all_units_contacted": all_units_contacted,
                "tier1": "multi_session_orphan_contact" if tier1 else None,
                "tier2_inputs": ["shared_target"] if tier2 else [],
                "shared_target_ids": shared_targets,
                "active_lane": active_lane,
                "has_forward_unit": has_forward_unit,
                "qualified": qualified,
                "reason": (
                    "stale_tree_signal" if stale
                    else "inactive_target_lane" if not active_lane
                    else "tree_only_or_partial_contact" if not tier1
                    else "shared_target_missing" if not tier2
                    else "forward_looking_evidence_missing" if not has_forward_unit
                    else "qualified"
                ),
            }
            diagnostics.append(diagnostic)
            if not qualified:
                continue
            row = aggregate.setdefault(lane_id, {
                "member_ids": set(),
                "contact_session_ids": set(),
                "shared_target_ids": set(),
                "tree_ids": set(),
                "bounded_evidence": [],
                "member_feeder_relations": defaultdict(set),
            })
            row["member_ids"].update(eligible_member_ids)
            row["contact_session_ids"].update(sessions)
            row["shared_target_ids"].update(shared_targets)
            row["tree_ids"].add(int(group["tree_id"]))
            stored_relations = (
                (group.get("member_feeder_relations") or {}).get(str(lane_id))
                or (group.get("member_feeder_relations") or {}).get(lane_id)
                or {}
            )
            for node_id in eligible_member_ids:
                raw_relation = (
                    stored_relations.get(str(node_id))
                    or stored_relations.get(node_id)
                )
                if raw_relation is not None:
                    relation = db.canonicalize_relation(str(raw_relation))
                    if relation in feeders.FEEDER_RELATIONS:
                        row["member_feeder_relations"][node_id].add(relation)
            row["bounded_evidence"].append({
                "tree_id": int(group["tree_id"]),
                "member_ids": eligible_member_ids[:WEAK_ANNOTATION_ID_LIMIT],
                "contact_session_ids": sessions[:WEAK_ANNOTATION_ID_LIMIT],
                "shared_target_ids": shared_targets[:WEAK_ANNOTATION_ID_LIMIT],
            })

    candidates: list[dict] = []
    for lane_id, row in sorted(aggregate.items()):
        member_ids = sorted(row["member_ids"])
        sessions = sorted(row["contact_session_ids"])
        shared_targets = sorted(row["shared_target_ids"])
        tree_ids = sorted(row["tree_ids"])
        relation_precedence = {
            relation: index for index, relation in enumerate(feeders.FEEDER_RELATIONS)
        }
        relations = {
            str(node_id): min(
                row["member_feeder_relations"].get(node_id) or {"advances"},
                key=lambda relation: (relation_precedence.get(relation, 999), relation),
            )
            for node_id in member_ids
        }
        bounded_evidence = sorted(
            row["bounded_evidence"], key=lambda value: value["tree_id"],
        )[:WEAK_ANNOTATION_ID_LIMIT]
        lane = lanes[lane_id]
        apply_request = None
        if not bool(lane.get("pinned")):
            apply_request = {
                "workstream_id": lane_id,
                "node_ids": member_ids,
                "relations": relations,
                "evidence": {
                    "trigger": "shared_target",
                    "forward_looking": True,
                    "session_ids": sessions,
                    "shared_target_ids": shared_targets,
                    "tree_ids": tree_ids,
                    "bounded_evidence": bounded_evidence,
                },
                "allow_auto_apply": True,
            }
        signal = {
            "qualified": True,
            "workstream_id": lane_id,
            "member_ids": member_ids,
            "tier1": "multi_session_orphan_contact",
            "tier1_session_ids": sessions,
            "tier2_inputs": ["shared_target"],
            "shared_target_ids": shared_targets,
            "member_feeder_relations": relations,
            "tree_ids": tree_ids,
            "tree_stale": False,
            "bounded_evidence": bounded_evidence,
            "candidate_only": apply_request is None,
            "ambiguity_reason": "pinned_target" if apply_request is None else None,
            "mode": "shadow",
            "substrate_version": SUBSTRATE_VERSION,
        }
        if apply_request is not None:
            signal["apply_request"] = apply_request
        candidates.append({
            "candidate_key": lifecycle_signals.make_candidate_key("ADOPT", [lane_id]),
            "op": "ADOPT",
            "signal": signal,
        })
    return diagnostics, candidates


def _close_candidates(
    inputs: Mapping[str, Any],
    *,
    now: datetime,
) -> tuple[list[dict], list[dict]]:
    observation_start = _parse_ts(inputs.get("observation_start"))
    observation_days = (
        (now - observation_start).total_seconds() / 86400.0
        if observation_start is not None else 0.0
    )
    observation_ready = bool(
        observation_start is not None
        and observation_days >= CLOSE_MIN_OBSERVATION_DAYS
    )
    contacts = {
        int(lane): set(sessions) for lane, sessions in (inputs.get("contacts") or {}).items()
    }
    probation_lanes: dict[int, Mapping[str, Any]] = {}
    for lane, value in (inputs.get("probation_lanes") or {}).items():
        if not isinstance(value, Mapping) or value.get("active") is not True:
            continue
        try:
            observed = int(value.get("eligible_session_count") or 0)
            target = max(1, int(value.get("eligible_session_target") or 10))
            contacts_after = int(value.get("contact_session_count") or 0)
        except (TypeError, ValueError):
            continue
        if observed >= target and contacts_after > 0:
            continue
        probation_lanes[int(lane)] = value
    candidates: list[dict] = []
    probation_diagnostics: list[dict] = []
    for lane_id_raw, lane in sorted((inputs.get("lanes") or {}).items()):
        lane_id = int(lane_id_raw)
        probation = probation_lanes.get(lane_id)
        if bool(lane.get("pinned")):
            if probation is not None:
                probation_diagnostics.append({
                    "workstream_id": lane_id,
                    "opening_op_key": probation.get("open_op_key"),
                    "qualified": False,
                    "reason": "pinned_target",
                })
            continue
        suppress_inactivity = False
        if probation is not None:
            observed = int(probation.get("eligible_session_count") or 0)
            target = int(probation.get("eligible_session_target") or 10)
            contact_count = int(probation.get("contact_session_count") or 0)
            opened_at = _parse_ts(probation.get("opened_at"))
            retention_start = now - timedelta(days=ELIGIBLE_WINDOW_CAP_DAYS)
            window_within_retention = bool(
                opened_at is not None and opened_at >= retention_start
            )
            target_sessions_observable = bool(
                observed >= target
                and probation.get("target_sessions_observable", True)
            )
            priority_clear = int(lane.get("active_priority_count") or 0) == 0
            ready = bool(
                contact_count == 0
                and window_within_retention
                and target_sessions_observable
                and priority_clear
            )
            diagnostic = {
                "workstream_id": lane_id,
                "opening_op_key": probation.get("open_op_key"),
                "opened_at": probation.get("opened_at"),
                "retained_since": _stamp(retention_start),
                "eligible_session_count": observed,
                "eligible_session_target": target,
                "contact_session_count": contact_count,
                "window_within_retention": window_within_retention,
                "target_sessions_observable": target_sessions_observable,
                "qualified": ready,
                "reason": (
                    "contact_observed" if contact_count > 0
                    else "probation_evidence_expired" if not window_within_retention
                    else "awaiting_target_sessions" if observed < target
                    else "target_sessions_incomplete" if not target_sessions_observable
                    else "active_priorities_block_release" if not priority_clear
                    else "qualified"
                ),
            }
            probation_diagnostics.append(diagnostic)
            incomplete_absence_evidence = bool(
                contact_count > 0
                or not window_within_retention
                or (observed >= target and not target_sessions_observable)
            )
            if incomplete_absence_evidence:
                # The immutable OPEN receipt is not a proof of zero contact once
                # any part of its evidence window has fallen out of retention.
                suppress_inactivity = True
            if incomplete_absence_evidence:
                # Structurally completed lanes remain eligible below; only the
                # inference from absence is suppressed.
                pass
            else:
                release_ids = sorted({
                    int(value)
                    for value in [
                        *(probation.get("member_ids") or []),
                        *(probation.get("open_feeder_ids") or []),
                    ]
                })
                signal = {
                    "workstream_id": lane_id,
                    "outcome": "abandoned",
                    "reason": "auto_open_probation_no_contacts",
                    "opening_op_key": probation.get("open_op_key"),
                    "probation_evidence": dict(probation),
                    "eligible_session_count": observed,
                    "eligible_session_target": target,
                    "release_member_or_feeder_ids": release_ids,
                    "open_feeder_count": int(lane.get("open_feeder_count") or 0),
                    "active_priority_count": int(lane.get("active_priority_count") or 0),
                    "pinned": False,
                    "probation_ready": ready,
                    "qualified": ready,
                    "auto_apply_eligible": ready,
                    "candidate_only": not ready,
                    "mode": "shadow",
                    "substrate_version": SUBSTRATE_VERSION,
                }
                if ready:
                    signal["apply_request"] = {
                        "workstream_id": lane_id,
                        "outcome": "abandoned",
                        "reason": (
                            f"zero contamination-free contacts across {observed} "
                            "eligible post-open sessions"
                        ),
                        "dispositions": {
                            str(node_id): {"action": "release"}
                            for node_id in release_ids
                        },
                    }
                candidates.append({
                    "candidate_key": lifecycle_signals.make_candidate_key(
                        "CLOSE", [lane_id],
                    ),
                    "op": "CLOSE",
                    "signal": signal,
                })
                continue
        if not observation_ready:
            continue
        if int(lane.get("open_feeder_count") or 0) > 0:
            continue
        if int(lane.get("active_priority_count") or 0) > 0:
            continue
        created = _parse_ts(lane.get("created_at"))
        if created is not None and (now - created).total_seconds() / 86400.0 \
                < CLOSE_MIN_OBSERVATION_DAYS:
            continue
        forward_ids = sorted({int(value) for value in lane.get("forward_member_ids", [])})
        resolved_ids = sorted({
            int(value) for value in lane.get("resolved_forward_member_ids", [])
        })
        resolution_edge_ids = sorted({
            int(value) for value in lane.get("resolution_edge_ids", [])
        })
        resolution_evidence = [
            dict(value) for value in lane.get("resolution_evidence", [])
            if isinstance(value, Mapping)
            and str(value.get("created_by") or "").strip()
        ]
        done_when = str(lane.get("done_when") or "").strip()
        density = float(lane.get("resolution_density") or 0.0)
        completed = bool(
            done_when
            and forward_ids
            and resolution_edge_ids
            and resolution_evidence
            and set(forward_ids).issubset(resolved_ids)
            and density >= 1.0
        )
        if completed:
            reason = "Done-when satisfied with complete resolution coverage"
            signal = {
                "workstream_id": lane_id,
                "outcome": "completed",
                "reason": "structurally_completed",
                "completion_evidence": {
                    "done_when": done_when,
                    "forward_member_ids": forward_ids,
                    "resolved_forward_member_ids": resolved_ids,
                    "resolution_edge_ids": resolution_edge_ids,
                    "resolution_evidence": resolution_evidence,
                    "resolution_density": round(density, 6),
                },
                "apply_request": {
                    "workstream_id": lane_id,
                    "outcome": "completed",
                    "reason": reason,
                    "dispositions": {},
                },
                "observation_days": round(observation_days, 3),
                "open_feeder_count": 0,
                "active_priority_count": 0,
                "pinned": False,
                "qualified": True,
                "attestation_required": True,
                "mode": "shadow",
                "substrate_version": SUBSTRATE_VERSION,
            }
        else:
            if suppress_inactivity or contacts.get(lane_id):
                continue
            signal = {
                "workstream_id": lane_id,
                "outcome": "abandoned",
                "reason": "no_contamination_free_contact",
                "observation_days": round(observation_days, 3),
                "open_feeder_count": 0,
                "active_priority_count": 0,
                "pinned": False,
                "qualified": True,
                "auto_apply_eligible": False,
                "candidate_only": True,
                "mode": "shadow",
                "substrate_version": SUBSTRATE_VERSION,
            }
        if int(inputs.get("recent_active_lane_count") or 0) >= int(
            inputs.get("recent_active_lane_cap") or RECENT_ACTIVE_LANE_CAP
        ):
            signal["cap_pressure"] = True
            signal["governor_reason"] = "cap_pressure"
        candidates.append({
            "candidate_key": lifecycle_signals.make_candidate_key("CLOSE", [lane_id]),
            "op": "CLOSE",
            "signal": signal,
        })
    return candidates, probation_diagnostics


def derive_shadow_snapshot(inputs: Mapping[str, Any]) -> dict:
    """Pure detector: derive ranked candidates and calibration diagnostics."""
    now = _parse_ts(inputs.get("now"))
    if now is None:
        raise ValueError("inputs.now must be a valid timestamp")
    lanes = {int(value) for value in (inputs.get("lanes") or {}).keys()}
    contacts = {
        int(lane): values for lane, values in (inputs.get("contacts") or {}).items()
    }
    matrix = _contact_matrix(lanes, contacts)
    merge = _merge_candidates(inputs, matrix)
    open_diagnostics, opened, stale_tree_count = _open_groups(inputs, now=now)
    adopt_diagnostics, adopted = _adopt_candidates(inputs, now=now)
    closed, probation_diagnostics = _close_candidates(inputs, now=now)
    candidates = merge + closed + adopted + opened
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank
    eligible = list(inputs.get("eligible_sessions") or [])
    window_start = eligible[0]["started_at"] if eligible else None
    window_end = _stamp(now)
    orphan_count = int(inputs.get("orphan_count") or 0)
    contacted_orphans = len(inputs.get("orphan_events") or {})
    orphan_pressure = {
        "orphan_nodes": orphan_count,
        "contacted_orphan_nodes": contacted_orphans,
        "contacted_fraction": (
            round(contacted_orphans / orphan_count, 6) if orphan_count else 0.0
        ),
        "tree_group_count": len(open_diagnostics),
    }
    counters = {"stale_tree_signal": stale_tree_count}
    tier3_annotations = dict(inputs.get("tier3") or {})
    snapshot_core = {
        "substrate_version": SUBSTRATE_VERSION,
        "window_start": window_start,
        "window_end": window_end,
        "orphan_pressure": orphan_pressure,
        "contact_matrix": matrix,
        "open_groups": open_diagnostics,
        "adopt_groups": adopt_diagnostics,
        "probation_diagnostics": probation_diagnostics,
        "tier3_annotations": tier3_annotations,
        "counters": counters,
        "candidate_keys": [candidate["candidate_key"] for candidate in candidates],
        "signals": [candidate["signal"] for candidate in candidates],
    }
    return {
        "mode": "shadow",
        "substrate_version": SUBSTRATE_VERSION,
        "derivation_key": _canonical_hash("wsd1", snapshot_core),
        "window_start": window_start,
        "window_end": window_end,
        "eligible_session_count": len(eligible),
        "orphan_pressure": orphan_pressure,
        "contact_matrix": matrix,
        "open_groups": open_diagnostics,
        "adopt_groups": adopt_diagnostics,
        "probation_diagnostics": probation_diagnostics,
        "tier3_annotations": tier3_annotations,
        "counters": counters,
        "candidates": candidates,
    }


def persist_shadow_snapshot(
    conn: sqlite3.Connection,
    snapshot: Mapping[str, Any],
) -> dict:
    """Persist only the immutable derivation/candidate snapshot."""
    if snapshot.get("mode") != "shadow":
        raise ValueError("only shadow snapshots may be persisted by this detector")
    return db.record_workstream_derivation(
        conn,
        derivation_key=str(snapshot["derivation_key"]),
        substrate_version=str(snapshot["substrate_version"]),
        window_start=snapshot.get("window_start"),
        window_end=snapshot.get("window_end"),
        candidates=list(snapshot.get("candidates") or []),
    )


def run_shadow_derivation(
    conn: sqlite3.Connection,
    *,
    now: datetime | str | None = None,
    project_path: str | None = None,
) -> dict:
    inputs = collect_inputs(conn, now=now, project_path=project_path)
    snapshot = derive_shadow_snapshot(inputs)
    snapshot["ledger"] = persist_shadow_snapshot(conn, snapshot)
    return snapshot


def prequential_state(
    candidate_key: str,
    snapshots: Sequence[Iterable[str]],
    *,
    verdicts: Iterable[str] = (),
) -> dict:
    """Score persistence over chronological derivation snapshots."""
    presence = [candidate_key in set(snapshot) for snapshot in snapshots]
    seen = sum(presence)
    consecutive = 0
    for present in reversed(presence):
        if not present:
            break
        consecutive += 1
    normalized_verdicts = [str(verdict) for verdict in verdicts]
    return {
        "candidate_key": candidate_key,
        "windows": len(presence),
        "windows_present": seen,
        "persistence": round(seen / len(presence), 6) if presence else 0.0,
        "consecutive_present": consecutive,
        "latest_present": bool(presence and presence[-1]),
        "agree": normalized_verdicts.count("agree"),
        "disagree": normalized_verdicts.count("disagree"),
        "unsure": normalized_verdicts.count("unsure"),
    }


def graduation_eligible(
    state: Mapping[str, Any],
    *,
    min_consecutive: int = 2,
    min_persistence: float = 0.70,
) -> bool:
    return bool(
        state.get("latest_present")
        and int(state.get("consecutive_present") or 0) >= min_consecutive
        and float(state.get("persistence") or 0.0) >= min_persistence
        and int(state.get("disagree") or 0) == 0
    )


def load_prequential_state(
    conn: sqlite3.Connection,
    candidate_key: str,
    *,
    lookback: int = 10,
    substrate_version: str = SUBSTRATE_VERSION,
) -> dict:
    rows = conn.execute(
        "SELECT id FROM workstream_derivations WHERE substrate_version=? "
        "ORDER BY id DESC LIMIT ?",
        (substrate_version, max(1, int(lookback))),
    ).fetchall()
    derivation_ids = [int(row["id"]) for row in reversed(rows)]
    snapshots: list[set[str]] = []
    for derivation_id in derivation_ids:
        snapshots.append({
            str(row["candidate_key"])
            for row in conn.execute(
                "SELECT candidate_key FROM workstream_derivation_candidates "
                "WHERE derivation_id=?", (derivation_id,),
            ).fetchall()
        })
    verdicts: list[str] = []
    if derivation_ids:
        placeholders = ",".join("?" for _ in derivation_ids)
        verdicts = [
            str(row["verdict"])
            for row in conn.execute(
                "SELECT verdict FROM workstream_op_events "
                f"WHERE candidate_key=? AND verdict IS NOT NULL "
                f"AND derivation_id IN ({placeholders}) ORDER BY id",
                [candidate_key, *derivation_ids],
            ).fetchall()
        ]
    state = prequential_state(candidate_key, snapshots, verdicts=verdicts)
    state["substrate_version"] = substrate_version
    return state
