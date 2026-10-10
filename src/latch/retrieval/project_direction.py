#!/usr/bin/env python3
"""Read-only project-direction report.

This is the first minimal project-direction layer: assemble existing KB
primitives into a workstream-centered view without adding a storage rebuild.
"""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from latch.store import artifacts as artifact_store  # noqa: E402
from latch.retrieval import authority  # noqa: E402
from latch.store import db  # noqa: E402
from latch.retrieval import feeders  # noqa: E402
from latch.store import lifecycle_receipts  # noqa: E402
from latch.store import priorities as priority_store  # noqa: E402


DECISION_KINDS = {"decision"}
CONSTRAINT_KINDS = {"preference"}
PROGRESS_KINDS = {"progress"}
UNANCHORED_KINDS = {"decision", "progress", "open_question", "idea"}
ANCHOR_STOPWORDS = {
    "about", "action", "active", "after", "again", "against", "agent",
    "apply", "artifact", "artifacts", "before", "branch", "candidate",
    "change", "changes", "codex", "commit", "copy", "decision",
    "direction", "evidence", "first", "from", "future", "gate", "gates",
    "item", "items", "latch", "layer", "local", "make", "manual", "next",
    "node", "open", "project", "receipt", "report", "review", "should",
    "show", "smoke", "staging", "status", "their", "there", "these",
    "this", "user", "where", "whether", "with", "work", "workstream",
    "write", "writes",
}
UNANCHORED_SKIP_RE = re.compile(
    r"\b(session start|no substantive work|no work yet|context only)\b",
    re.IGNORECASE,
)
# Backwards-compatible module alias; the computation itself lives in the
# shared helper used by both project_direction and the gate renderer.
AUTHORITY_RELATIONS = authority.AUTHORITY_RELATIONS
OBJECTIVE_RE = re.compile(r"^\s*(?:objective|goal)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
NEXT_ACTION_RE = re.compile(
    r"^\s*(?:next action|next step)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
COMPACT_WORKSTREAM_LIMIT = 3
COMPACT_MEMBER_LIMIT = 8
COMPACT_UNANCHORED_LIMIT = 3
COMPACT_DECISION_LIMIT = 3
COMPACT_BACKLOG_LIMIT = 5
COMPACT_CONSTRAINT_LIMIT = 3
COMPACT_PROGRESS_LIMIT = 3
COMPACT_ARTIFACT_LIMIT = 3
COMPACT_RECEIPT_LIMIT = 3
COMPACT_PRIORITY_LIMIT = 5
COMPACT_TEXT_CHARS = 240
COMPACT_METADATA_CHARS = 80
# Reserve headroom for the MCP layer's visible kb_activity receipt.
COMPACT_REPORT_BODY_BYTES = 60_000
COMPACT_REPORT_MAX_BYTES = 72_000


@dataclass(frozen=True)
class DirectionNode:
    id: int
    kind: str
    title: str
    status: str
    authority_tier: str | None = None
    relation: str | None = None


@dataclass(frozen=True)
class DirectionArtifact:
    repo: str
    path: str | None
    node_ids: list[int]


@dataclass(frozen=True)
class DirectionPriority:
    id: int
    title: str
    status: str
    effective_rank: int | None
    locked: bool
    workstream_id: int | None
    scope: str


@dataclass(frozen=True)
class WorkstreamDirection:
    id: int
    title: str
    status: str
    objective: str
    focus_rank: int | None
    focus_score: float | None
    governing_decisions: list[DirectionNode]
    backlog_items: list[DirectionNode]
    constraints: list[DirectionNode]
    recent_progress: list[DirectionNode]
    artifacts: list[DirectionArtifact]
    priorities: list[DirectionPriority]
    next_action: str | None
    next_action_source: str | None
    next_action_node_id: int | None
    omitted: dict[str, int]


@dataclass(frozen=True)
class AnchorCandidate:
    id: int
    kind: str
    title: str
    status: str
    suggested_workstream_id: int | None
    suggested_workstream_title: str | None
    reason: str


def assemble_project_direction(
    conn,
    *,
    limit: int = 3,
    member_limit: int = 20,
    unanchored_limit: int = 5,
    compact: bool = False,
) -> dict[str, Any]:
    """Assemble a read-only project-direction report from existing KB rows.

    ``compact=True`` is the bounded, on-demand catch-up surface. It clamps
    caller-supplied scan limits and trims every repeated output section while
    preserving the same response shape as the expanded report.
    """
    if compact:
        limit = min(max(0, int(limit)), COMPACT_WORKSTREAM_LIMIT)
        member_limit = min(max(0, int(member_limit)), COMPACT_MEMBER_LIMIT)
        unanchored_limit = min(
            max(0, int(unanchored_limit)),
            COMPACT_UNANCHORED_LIMIT,
        )
    if compact:
        workstreams, workstream_total = _workstream_seed_inventory(
            conn,
            limit=limit,
        )
    else:
        workstreams = _workstream_seeds(conn, limit=limit)
        workstream_total = len(workstreams)
    rows = [
        _assemble_workstream(conn, ws, member_limit=member_limit)
        for ws in workstreams
    ]
    rows = [row for row in rows if row is not None]
    rendered_rows = (
        [_compact_workstream(row) for row in rows]
        if compact
        else [asdict(row) for row in rows]
    )
    backlog_total = sum(len(row["backlog_items"]) for row in rendered_rows)
    decision_total = sum(len(row["governing_decisions"]) for row in rendered_rows)
    artifact_total = sum(len(row["artifacts"]) for row in rendered_rows)
    unanchored_scan = _unanchored_candidates(
        conn,
        rows,
        limit=80 if compact else unanchored_limit,
    )
    unanchored = unanchored_scan[:unanchored_limit] if compact else unanchored_scan
    # This report is strictly read-only. Foreground search/get/recent reads own
    # the one-way receipt claim; direction reports may show recent durable
    # receipt history, but must not race that delivery channel or commit.
    try:
        receipt_scan = (
            lifecycle_receipts.recent_receipts(
                conn,
                limit=100 if compact else 10,
            )
            if lifecycle_receipts.RECEIPTS_CHANNEL_LIVE
            else []
        )
    except Exception:
        receipt_scan = []
    receipts = (
        receipt_scan[:COMPACT_RECEIPT_LIMIT]
        if compact
        else receipt_scan
    )
    overall_priorities = _direction_priorities(
        priority_store.list_priorities(conn)
    )
    rendered_overall_priorities = (
        [_compact_priority(row) for row in overall_priorities[:COMPACT_PRIORITY_LIMIT]]
        if compact
        else [asdict(row) for row in overall_priorities]
    )
    priority_total = len(rendered_overall_priorities) + sum(
        len(row["priorities"]) for row in rendered_rows
    )
    summary = (
        f"Latch assembled {len(rendered_rows)} workstream(s), "
        f"{decision_total} governing "
        f"decision(s), {backlog_total} backlog/open item(s), and "
        f"{artifact_total} artifact coordinate(s) from the local KB."
    )
    if compact:
        summary += " This is the bounded compact catch-up view."
    if unanchored:
        summary += (
            f" It also found {len(unanchored)} recent unanchored item(s) that "
            "may need a user-confirmed workstream."
        )
    report = {
        "label": "Latch project direction",
        "source": "project_direction",
        "mode": "compact" if compact else "expanded",
        "read_only": True,
        "must_display_to_user": True,
        "summary": summary,
        "why_it_matters": (
            "This keeps the next-step view anchored in workstreams, current "
            "decision authority, open work, and artifact evidence instead of a "
            "generic memory summary."
        ),
        "used": {
            "workstreams": len(rendered_rows),
            "governing_decisions": decision_total,
            "backlog_items": backlog_total,
            "artifacts": artifact_total,
            "unanchored_items": len(unanchored),
            "lifecycle_receipts": len(receipts),
            "priorities": priority_total,
        },
        "workstreams": rendered_rows,
        "overall_priorities": rendered_overall_priorities,
        "foregrounded_item": _foregrounded_item(rows, unanchored, compact=compact),
        "unanchored_evidence": (
            [_compact_unanchored(row) for row in unanchored]
            if compact
            else [asdict(row) for row in unanchored]
        ),
        "lifecycle_receipts": (
            [_compact_receipt(row) for row in receipts]
            if compact
            else receipts
        ),
    }
    if compact:
        report["omitted"] = {
            "workstreams": max(0, workstream_total - len(rendered_rows)),
            "overall_priorities": max(
                0,
                len(overall_priorities) - COMPACT_PRIORITY_LIMIT,
            ),
            "unanchored_evidence": max(
                0,
                len(unanchored_scan) - len(unanchored),
            ),
            "lifecycle_receipts": max(
                0,
                len(receipt_scan) - len(receipts),
            ),
        }
        report["compact_limits"] = {
            "workstreams": COMPACT_WORKSTREAM_LIMIT,
            "members_scanned_per_workstream": COMPACT_MEMBER_LIMIT,
            "governing_decisions_per_workstream": COMPACT_DECISION_LIMIT,
            "backlog_items_per_workstream": COMPACT_BACKLOG_LIMIT,
            "constraints_per_workstream": COMPACT_CONSTRAINT_LIMIT,
            "recent_progress_per_workstream": COMPACT_PROGRESS_LIMIT,
            "artifacts_per_workstream": COMPACT_ARTIFACT_LIMIT,
            "unanchored_items": COMPACT_UNANCHORED_LIMIT,
            "lifecycle_receipts": COMPACT_RECEIPT_LIMIT,
            "priorities_per_scope": COMPACT_PRIORITY_LIMIT,
            "text_chars": COMPACT_TEXT_CHARS,
            "metadata_chars": COMPACT_METADATA_CHARS,
            "max_bytes": COMPACT_REPORT_MAX_BYTES,
        }
        report = enforce_compact_report_bytes(
            report,
            max_bytes=COMPACT_REPORT_BODY_BYTES,
        )
    return report


def _bounded_text(value: Any, *, limit: int = COMPACT_TEXT_CHARS) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _compact_node(node: DirectionNode) -> dict:
    return {
        "id": node.id,
        "kind": _bounded_text(node.kind, limit=COMPACT_METADATA_CHARS),
        "title": _bounded_text(node.title),
        "status": _bounded_text(node.status, limit=COMPACT_METADATA_CHARS),
        "authority_tier": (
            _bounded_text(node.authority_tier, limit=COMPACT_METADATA_CHARS)
            if node.authority_tier is not None
            else None
        ),
        "relation": (
            _bounded_text(node.relation, limit=COMPACT_METADATA_CHARS)
            if node.relation is not None
            else None
        ),
    }


def _compact_artifact(artifact: DirectionArtifact) -> dict:
    item = asdict(artifact)
    item["repo"] = _bounded_text(item["repo"])
    if item["path"] is not None:
        item["path"] = _bounded_text(item["path"])
    return item


def _compact_priority(row: DirectionPriority) -> dict:
    return {
        "id": row.id,
        "title": _bounded_text(row.title),
        "status": _bounded_text(row.status, limit=COMPACT_METADATA_CHARS),
        "effective_rank": row.effective_rank,
        "locked": row.locked,
        "workstream_id": row.workstream_id,
        "scope": _bounded_text(row.scope, limit=COMPACT_METADATA_CHARS),
    }


def _compact_workstream(row: WorkstreamDirection) -> dict:
    omitted = dict(row.omitted)
    for key, values, limit in (
        ("governing_decisions", row.governing_decisions, COMPACT_DECISION_LIMIT),
        ("backlog_items", row.backlog_items, COMPACT_BACKLOG_LIMIT),
        ("constraints", row.constraints, COMPACT_CONSTRAINT_LIMIT),
        ("recent_progress", row.recent_progress, COMPACT_PROGRESS_LIMIT),
        ("artifacts", row.artifacts, COMPACT_ARTIFACT_LIMIT),
        ("priorities", row.priorities, COMPACT_PRIORITY_LIMIT),
    ):
        omitted[key] = omitted.get(key, 0) + max(0, len(values) - limit)
    return {
        "id": row.id,
        "title": _bounded_text(row.title),
        "status": _bounded_text(row.status, limit=COMPACT_METADATA_CHARS),
        "objective": _bounded_text(row.objective),
        "focus_rank": row.focus_rank,
        "focus_score": row.focus_score,
        "governing_decisions": [
            _compact_node(node)
            for node in row.governing_decisions[:COMPACT_DECISION_LIMIT]
        ],
        "backlog_items": [
            _compact_node(node)
            for node in row.backlog_items[:COMPACT_BACKLOG_LIMIT]
        ],
        "constraints": [
            _compact_node(node)
            for node in row.constraints[:COMPACT_CONSTRAINT_LIMIT]
        ],
        "recent_progress": [
            _compact_node(node)
            for node in row.recent_progress[:COMPACT_PROGRESS_LIMIT]
        ],
        "artifacts": [
            _compact_artifact(artifact)
            for artifact in row.artifacts[:COMPACT_ARTIFACT_LIMIT]
        ],
        "priorities": [
            _compact_priority(priority)
            for priority in row.priorities[:COMPACT_PRIORITY_LIMIT]
        ],
        "next_action": (
            _bounded_text(row.next_action)
            if row.next_action is not None
            else None
        ),
        "next_action_source": row.next_action_source,
        "next_action_node_id": row.next_action_node_id,
        "omitted": omitted,
    }


def _compact_unanchored(row: AnchorCandidate) -> dict:
    return {
        "id": row.id,
        "kind": _bounded_text(row.kind, limit=COMPACT_METADATA_CHARS),
        "title": _bounded_text(row.title),
        "status": _bounded_text(row.status, limit=COMPACT_METADATA_CHARS),
        "suggested_workstream_id": row.suggested_workstream_id,
        "suggested_workstream_title": (
            _bounded_text(row.suggested_workstream_title)
            if row.suggested_workstream_title is not None
            else None
        ),
        "reason": _bounded_text(row.reason),
    }


def _compact_receipt(row: dict) -> dict:
    return {
        key: (
            _bounded_text(value, limit=(
                COMPACT_TEXT_CHARS
                if key == "receipt"
                else COMPACT_METADATA_CHARS
            ))
            if isinstance(value, str)
            else value
        )
        for key in (
            "operation_id",
            "op_key",
            "op",
            "origin",
            "workstream_id",
            "receipt",
            "applied_at",
        )
        if (value := row.get(key)) is not None
    }


def _encoded_bytes(value: Any) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


def _refresh_compact_summary(report: dict) -> None:
    workstreams = report.get("workstreams") or []
    unanchored = report.get("unanchored_evidence") or []
    receipts = report.get("lifecycle_receipts") or []
    decisions = sum(len(row.get("governing_decisions") or []) for row in workstreams)
    backlog = sum(len(row.get("backlog_items") or []) for row in workstreams)
    artifact_count = sum(len(row.get("artifacts") or []) for row in workstreams)
    priority_count = len(report.get("overall_priorities") or []) + sum(
        len(row.get("priorities") or []) for row in workstreams
    )
    report["used"] = {
        "workstreams": len(workstreams),
        "governing_decisions": decisions,
        "backlog_items": backlog,
        "artifacts": artifact_count,
        "unanchored_items": len(unanchored),
        "lifecycle_receipts": len(receipts),
        "priorities": priority_count,
    }
    summary = (
        f"Latch assembled {len(workstreams)} workstream(s), "
        f"{decisions} governing decision(s), {backlog} backlog/open item(s), "
        f"and {artifact_count} artifact coordinate(s) from the local KB. "
        "This is the bounded compact catch-up view."
    )
    if unanchored:
        summary += (
            f" It also found {len(unanchored)} recent unanchored item(s) that "
            "may need a user-confirmed workstream."
        )
    if report.get("compact_truncated"):
        summary += " Oversized detail was trimmed to the response ceiling."
    report["summary"] = summary


def _drop_one_compact_detail(report: dict) -> bool:
    for key in ("lifecycle_receipts", "unanchored_evidence", "overall_priorities"):
        rows = report.get(key)
        if isinstance(rows, list) and rows:
            rows.pop()
            _increment_omitted(report, key)
            return True
    workstreams = report.get("workstreams")
    if not isinstance(workstreams, list):
        return False
    for row in reversed(workstreams):
        for key in (
            "artifacts",
            "priorities",
            "recent_progress",
            "constraints",
            "backlog_items",
            "governing_decisions",
        ):
            rows = row.get(key)
            if isinstance(rows, list) and rows:
                rows.pop()
                _increment_omitted(row, key)
                return True
    if len(workstreams) > 1:
        workstreams.pop()
        _increment_omitted(report, "workstreams")
        return True
    return False


def _increment_omitted(container: dict, key: str, count: int = 1) -> None:
    omitted = container.get("omitted")
    if not isinstance(omitted, dict):
        omitted = {}
        container["omitted"] = omitted
    try:
        previous = int(omitted.get(key) or 0)
    except (TypeError, ValueError):
        previous = 0
    omitted[key] = previous + max(0, int(count))


def _bounded_foreground(
    item: Any,
    *,
    text_limit: int = COMPACT_TEXT_CHARS,
) -> dict | None:
    if not isinstance(item, dict) or item.get("id") is None:
        return None
    return {
        "id": item.get("id"),
        "kind": _bounded_text(item.get("kind"), limit=COMPACT_METADATA_CHARS),
        "title": _bounded_text(item.get("title"), limit=text_limit),
        "status": _bounded_text(item.get("status"), limit=COMPACT_METADATA_CHARS),
        "workstream_id": item.get("workstream_id"),
        "reason": _bounded_text(item.get("reason"), limit=COMPACT_METADATA_CHARS),
    }


def _minimal_compact_report(report: dict) -> dict:
    source_workstreams = report.get("workstreams") or []
    top_omitted = dict(report.get("omitted") or {})
    top_omitted["workstreams"] = (
        int(top_omitted.get("workstreams") or 0)
        + max(0, len(source_workstreams) - 1)
    )
    for key in ("lifecycle_receipts", "unanchored_evidence", "overall_priorities"):
        top_omitted[key] = (
            int(top_omitted.get(key) or 0)
            + len(report.get(key) or [])
        )
    identities = []
    for row in source_workstreams[:1]:
        row_omitted = dict(row.get("omitted") or {})
        for key in (
            "artifacts",
            "priorities",
            "recent_progress",
            "constraints",
            "backlog_items",
            "governing_decisions",
        ):
            row_omitted[key] = (
                int(row_omitted.get(key) or 0)
                + len(row.get(key) or [])
            )
        identities.append({
            "id": row.get("id"),
            "title": _bounded_text(row.get("title"), limit=COMPACT_METADATA_CHARS),
            "status": _bounded_text(
                row.get("status"),
                limit=COMPACT_METADATA_CHARS,
            ),
            "objective": _bounded_text(
                row.get("objective"),
                limit=COMPACT_METADATA_CHARS,
            ),
            "focus_rank": row.get("focus_rank"),
            "focus_score": row.get("focus_score"),
            "governing_decisions": [],
            "backlog_items": [],
            "constraints": [],
            "recent_progress": [],
            "artifacts": [],
            "priorities": [],
            "next_action": (
                _bounded_text(
                    row.get("next_action"),
                    limit=COMPACT_METADATA_CHARS,
                )
                if row.get("next_action") is not None
                else None
            ),
            "next_action_source": row.get("next_action_source"),
            "next_action_node_id": row.get("next_action_node_id"),
            "omitted": row_omitted,
        })
    foreground = _bounded_foreground(
        report.get("foregrounded_item"),
        text_limit=COMPACT_METADATA_CHARS,
    )
    out = {
        "label": "Latch project direction",
        "source": "project_direction",
        "mode": "compact",
        "read_only": True,
        "must_display_to_user": True,
        "summary": "",
        "why_it_matters": (
            "This keeps the next-step view anchored in current project "
            "direction while respecting the response ceiling."
        ),
        "used": {},
        "workstreams": identities,
        "overall_priorities": [],
        "foregrounded_item": foreground or (
            {
                "id": identities[0]["id"],
                "kind": "workstream",
                "title": identities[0]["title"],
                "status": identities[0]["status"],
                "workstream_id": identities[0]["id"],
                "reason": "size_limited_focused_workstream",
            }
            if identities
            else None
        ),
        "unanchored_evidence": [],
        "lifecycle_receipts": [],
        "omitted": top_omitted,
        "compact_limits": report.get("compact_limits") or {},
        "compact_truncated": True,
    }
    if isinstance(report.get("kb_activity"), dict):
        out["kb_activity"] = {
            "label": "Latch KB activity",
            "must_display_to_user": True,
            "action": "read",
            "tool": "latch_project_direction",
            "summary": "Read a size-limited Latch project-direction report.",
            "nodes": [
                {
                    "id": row.get("id"),
                    "kind": "workstream",
                    "title": row.get("title"),
                    "status": row.get("status"),
                }
                for row in identities
            ],
            "hints": [],
        }
    _refresh_compact_summary(out)
    return out


def enforce_compact_report_bytes(
    report: dict,
    *,
    max_bytes: int = COMPACT_REPORT_MAX_BYTES,
) -> dict:
    """Return a compact-direction report below a hard serialized-byte ceiling."""
    if _encoded_bytes(report) <= max_bytes:
        return report
    report["compact_truncated"] = True
    while _encoded_bytes(report) > max_bytes and _drop_one_compact_detail(report):
        pass
    _refresh_compact_summary(report)
    if _encoded_bytes(report) <= max_bytes:
        return report
    return _minimal_compact_report(report)


def _workstream_seed_inventory(conn, *, limit: int) -> tuple[list[dict], int]:
    total = int(conn.execute(
        "SELECT COUNT(*) FROM nodes "
        "WHERE kind='workstream' AND status!='stale'"
    ).fetchone()[0])
    if limit <= 0:
        return [], total
    focus = db.get_focus(conn, limit=0)
    selected = [dict(row) for row in focus[:limit]]
    remaining = limit - len(selected)
    if remaining <= 0:
        return selected, total
    focused_ids = [int(row["id"]) for row in focus]
    params: list[Any] = []
    exclusion = ""
    if focused_ids:
        placeholders = ",".join("?" for _ in focused_ids)
        exclusion = f" AND id NOT IN ({placeholders})"
        params.extend(focused_ids)
    params.append(remaining)
    recent = conn.execute(
        "SELECT * FROM nodes "
        "WHERE kind='workstream' AND status!='stale'"
        f"{exclusion} "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    selected.extend(dict(row) for row in recent)
    return selected, total


def _direction_priorities(rows: list[dict]) -> list[DirectionPriority]:
    return [
        DirectionPriority(
            id=int(row["id"]),
            title=str(row["title"]),
            status=str(row["status"]),
            effective_rank=_int_or_none(row.get("effective_rank")),
            locked=bool(row.get("locked")),
            workstream_id=_int_or_none(row.get("workstream_id")),
            scope=str(row.get("scope") or "overall"),
        )
        for row in rows
    ]


def _foregrounded_item(
    workstreams: list[WorkstreamDirection],
    unanchored: list[AnchorCandidate],
    *,
    compact: bool,
) -> dict | None:
    if workstreams:
        row = workstreams[0]
        node_id = row.next_action_node_id or row.id
        candidates = [
            *row.governing_decisions,
            *row.backlog_items,
            *row.constraints,
            *row.recent_progress,
        ]
        node = next((item for item in candidates if item.id == node_id), None)
        item = {
            "id": node_id,
            "kind": node.kind if node is not None else "workstream",
            "title": node.title if node is not None else row.title,
            "status": node.status if node is not None else row.status,
            "workstream_id": row.id,
            "reason": row.next_action_source or "focused_workstream",
        }
    elif unanchored:
        node = unanchored[0]
        item = {
            "id": node.id,
            "kind": node.kind,
            "title": node.title,
            "status": node.status,
            "workstream_id": node.suggested_workstream_id,
            "reason": "recent_unanchored_evidence",
        }
    else:
        return None
    if compact:
        for key in ("kind", "status", "reason"):
            item[key] = _bounded_text(item[key], limit=COMPACT_METADATA_CHARS)
        item["title"] = _bounded_text(item["title"])
    return item


def _workstream_seeds(conn, *, limit: int) -> list[dict]:
    focus = db.get_focus(conn, limit=limit)
    if focus:
        return [dict(row) for row in focus]
    rows = db.recent_nodes(conn, kind="workstream", limit=limit)
    return [dict(row) for row in rows if row.get("status") != "stale"]


def _assemble_workstream(conn, ws: dict, *, member_limit: int) -> WorkstreamDirection | None:
    wid = int(ws["id"])
    if ws.get("kind") != "workstream" or ws.get("status") == "stale":
        return None
    member_total = int(conn.execute(
        "SELECT COUNT(*) FROM nodes "
        "WHERE workstream_id=? AND status!='stale'",
        (wid,),
    ).fetchone()[0])
    members = _workstream_members(conn, wid, limit=member_limit)
    connected = _connected_nodes(conn, wid, members)
    decisions = _governing_decisions(wid, members, connected)
    backlog, backlog_omitted = _feeder_backlog(
        conn,
        wid,
        member_limit=member_limit,
    )
    constraints = _nodes_for_kinds(members, CONSTRAINT_KINDS)
    progress = _nodes_for_kinds(members, PROGRESS_KINDS)
    artifacts = _artifacts_for_nodes(conn, [wid, *[int(n["id"]) for n in members]])
    workstream_priorities = _direction_priorities(
        priority_store.list_priorities(conn, workstream_id=wid)
    )
    next_action, next_action_source, next_action_node_id = _next_action(
        ws,
        backlog=backlog,
        progress=progress,
    )
    return WorkstreamDirection(
        id=wid,
        title=str(ws["title"]),
        status=str(ws["status"]),
        objective=_objective(ws),
        focus_rank=_int_or_none(ws.get("rank")),
        focus_score=_round_or_none(ws.get("effective_score") or ws.get("score")),
        governing_decisions=decisions,
        backlog_items=backlog,
        constraints=constraints,
        recent_progress=progress[:5],
        artifacts=artifacts,
        priorities=workstream_priorities,
        next_action=next_action,
        next_action_source=next_action_source,
        next_action_node_id=next_action_node_id,
        omitted={
            "members_not_scanned": max(0, member_total - len(members)),
            "backlog_items": backlog_omitted,
            "recent_progress": max(0, len(progress) - 5),
        },
    )


def _workstream_members(conn, workstream_id: int, *, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, kind, title, body, status, updated_at, workstream_id
        FROM nodes
        WHERE workstream_id = ?
          AND status != 'stale'
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (workstream_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _connected_nodes(conn, workstream_id: int, members: list[dict]) -> list[dict]:
    ids = [workstream_id, *[int(n["id"]) for n in members]]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT e.src, e.dst, e.relation,
               n.id, n.kind, n.title, n.status, n.workstream_id
        FROM edges e
        JOIN nodes n ON n.id = CASE WHEN e.src IN ({placeholders}) THEN e.dst ELSE e.src END
        WHERE e.status = 'active'
          AND (e.src IN ({placeholders}) OR e.dst IN ({placeholders}))
          AND n.status != 'stale'
        """,
        [*ids, *ids, *ids],
    ).fetchall()
    return [dict(row) for row in rows]


def _governing_decisions(
    workstream_id: int,
    members: list[dict],
    connected: list[dict],
) -> list[DirectionNode]:
    out: dict[int, DirectionNode] = {}
    for node in members:
        if node.get("kind") in DECISION_KINDS:
            nid = int(node["id"])
            out[nid] = DirectionNode(
                id=nid,
                kind=str(node["kind"]),
                title=str(node["title"]),
                status=str(node["status"]),
                authority_tier=authority.LANE_LOCAL,
            )
    for node in connected:
        if node.get("kind") not in DECISION_KINDS:
            continue
        rel = db.canonicalize_relation(str(node.get("relation") or "related_to"))
        if rel not in AUTHORITY_RELATIONS and _int_or_none(node.get("workstream_id")) != workstream_id:
            continue
        nid = int(node["id"])
        tier = authority.decision_authority_tier(
            relation=rel,
            decision_workstream_id=_int_or_none(node.get("workstream_id")),
            owning_workstream_id=workstream_id,
        )
        previous = out.get(nid)
        if (previous
                and authority.authority_sort_key(previous.authority_tier)
                <= authority.authority_sort_key(tier)):
            continue
        out[nid] = DirectionNode(
            id=nid,
            kind=str(node["kind"]),
            title=str(node["title"]),
            status=str(node["status"]),
            authority_tier=tier,
            relation=rel,
        )
    return sorted(
        out.values(),
        key=lambda n: (*authority.authority_sort_key(n.authority_tier), n.id),
    )


def _authority_tier(
    *,
    relation: str,
    decision_workstream_id: int | None,
    workstream_id: int,
) -> str:
    """Compatibility wrapper around the shared authority computation."""
    return authority.decision_authority_tier(
        relation=relation,
        decision_workstream_id=decision_workstream_id,
        owning_workstream_id=workstream_id,
    )


def _nodes_for_kinds(nodes: list[dict], kinds: set[str]) -> list[DirectionNode]:
    out = []
    for node in nodes:
        if node.get("kind") not in kinds:
            continue
        out.append(DirectionNode(
            id=int(node["id"]),
            kind=str(node["kind"]),
            title=str(node["title"]),
            status=str(node["status"]),
        ))
    return out


EDGE_FEEDER_LIMIT = 5


def _feeder_backlog(
    conn,
    workstream_id: int,
    *,
    member_limit: int,
    edge_limit: int = EDGE_FEEDER_LIMIT,
) -> tuple[list[DirectionNode], int]:
    """Backlog = the workstream's open feeders (KB 2299): unresolved
    forward-looking members plus declared-intent edge feeders, from the same
    `feeders.open_feeders` query used by other lifecycle views — one source of
    truth, so a resolution edge hides a row everywhere at once. Member and edge
    rows are capped independently so neither can crowd the other out; a node
    that is both member and edge feeder appears once, carrying its declared
    relation."""
    member_rows: list[DirectionNode] = []
    edge_rows: list[DirectionNode] = []
    omitted = 0
    for row in feeders.open_feeders(conn, workstream_id, limit=0):
        via = str(row["via"])
        node = DirectionNode(
            id=int(row["id"]),
            kind=str(row["kind"]),
            title=str(row["title"]),
            status=str(row["status"]),
            relation=None if via == "member" else via,
        )
        if via == "member":
            if len(member_rows) < member_limit:
                member_rows.append(node)
            else:
                omitted += 1
        elif len(edge_rows) < edge_limit:
            edge_rows.append(node)
        else:
            omitted += 1
    return [*member_rows, *edge_rows], omitted


def _artifacts_for_nodes(conn, node_ids: list[int]) -> list[DirectionArtifact]:
    by_coordinate: dict[tuple[str, str | None], set[int]] = {}
    for node_id in node_ids:
        # Artifact links are evidence, not authority; stale/status/reconciled_by
        # judgment remains on the governing node rows surfaced elsewhere.
        for artifact in artifact_store.get_node_artifacts(conn, node_id, include_stale=False):
            key = (str(artifact["repo"]), artifact.get("path"))
            by_coordinate.setdefault(key, set()).add(node_id)
    out = [
        DirectionArtifact(repo=repo, path=path, node_ids=sorted(ids))
        for (repo, path), ids in by_coordinate.items()
    ]
    return sorted(out, key=lambda a: (a.repo, a.path or ""))


def _unanchored_candidates(
    conn,
    workstreams: list[WorkstreamDirection],
    *,
    limit: int,
) -> list[AnchorCandidate]:
    if limit <= 0:
        return []
    anchored_ids = _anchored_node_ids(workstreams)
    recent = db.recent_nodes(conn, limit=80)
    out: list[AnchorCandidate] = []
    for node in recent:
        if len(out) >= limit:
            break
        if int(node["id"]) in anchored_ids:
            continue
        if _skip_unanchored_candidate(node):
            continue
        if node.get("kind") not in UNANCHORED_KINDS:
            continue
        if node.get("status") == "stale" or node.get("workstream_id") is not None:
            continue
        suggestion = _suggest_existing_workstream(node, workstreams)
        if suggestion is None:
            reason = (
                "No active workstream shares enough terms; if this lane recurs, "
                "create or choose a workstream before relying on it for direction."
            )
            wid = None
            title = None
        else:
            wid, title, terms = suggestion
            reason = "Shares anchor terms: " + ", ".join(sorted(terms)[:5])
        out.append(AnchorCandidate(
            id=int(node["id"]),
            kind=str(node["kind"]),
            title=str(node["title"]),
            status=str(node["status"]),
            suggested_workstream_id=wid,
            suggested_workstream_title=title,
            reason=reason,
        ))
    return out


def _skip_unanchored_candidate(node: dict) -> bool:
    text = " ".join([
        str(node.get("title") or ""),
        str(node.get("body") or ""),
    ])
    return bool(UNANCHORED_SKIP_RE.search(text))


def _anchored_node_ids(workstreams: list[WorkstreamDirection]) -> set[int]:
    ids: set[int] = set()
    for ws in workstreams:
        ids.add(ws.id)
        for group in (
            ws.governing_decisions,
            ws.backlog_items,
            ws.constraints,
            ws.recent_progress,
        ):
            ids.update(node.id for node in group)
        for artifact in ws.artifacts:
            ids.update(artifact.node_ids)
    return ids


def _suggest_existing_workstream(
    node: dict,
    workstreams: list[WorkstreamDirection],
) -> tuple[int, str, set[str]] | None:
    node_terms = _anchor_terms(" ".join([
        str(node.get("title") or ""),
        str(node.get("body") or ""),
    ]))
    best: tuple[int, str, set[str]] | None = None
    for ws in workstreams:
        ws_terms = _anchor_terms(" ".join([ws.title, ws.objective, ws.next_action or ""]))
        overlap = node_terms & ws_terms
        if len(overlap) < 3:
            continue
        if best is None or len(overlap) > len(best[2]):
            best = (ws.id, ws.title, overlap)
    return best


def _anchor_terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_-]{3,}", text.lower())
    return {word for word in words if word not in ANCHOR_STOPWORDS}


def _objective(ws: dict) -> str:
    body = str(ws.get("body") or "").strip()
    match = OBJECTIVE_RE.search(body)
    if match:
        return match.group(1).strip()
    for line in body.splitlines():
        line = line.strip(" -\t")
        if line:
            return line[:220]
    return str(ws.get("title") or "")


def _next_action(
    ws: dict,
    *,
    backlog: list[DirectionNode],
    progress: list[DirectionNode],
) -> tuple[str | None, str | None, int | None]:
    body = str(ws.get("body") or "")
    match = NEXT_ACTION_RE.search(body)
    if match:
        return match.group(1).strip(), "declared", int(ws["id"])
    if backlog:
        return (
            f"Resolve: {backlog[0].title}",
            "inferred_from_backlog",
            backlog[0].id,
        )
    if progress:
        return (
            f"Continue from: {progress[0].title}",
            "inferred_from_progress",
            progress[0].id,
        )
    return None, None, None


def format_text(report: dict[str, Any]) -> str:
    lines = [
        "# Latch Project Direction",
        "",
        report["summary"],
        "",
        f"Why this matters: {report['why_it_matters']}",
    ]
    foreground = report.get("foregrounded_item")
    if foreground:
        lines.extend([
            "",
            "Foregrounded item: "
            f"id={foreground['id']} [{foreground['kind']}/{foreground['status']}] "
            f"{foreground['title']} ({foreground['reason']})",
        ])
    _append_priorities(
        lines,
        "Overall priorities",
        report.get("overall_priorities") or [],
    )
    workstreams = report.get("workstreams") or []
    if not workstreams:
        lines.extend([
            "",
            "No active or recent workstreams found. Create a `kind='workstream'` "
            "node or set focus with `bin/run_kb_focus.sh set <workstream_id>`.",
        ])
    for ws in workstreams:
        focus = (
            f"rank {ws['focus_rank']}, score {ws['focus_score']}"
            if ws.get("focus_rank") is not None else "recent"
        )
        lines.extend([
            "",
            f"## {ws['title']} (id={ws['id']}, {ws['status']}, {focus})",
            f"Objective: {ws['objective']}",
        ])
        if ws.get("next_action"):
            source = ws.get("next_action_source") or "unlabeled"
            node_id = ws.get("next_action_node_id")
            anchor = f", node {node_id}" if node_id is not None else ""
            lines.append(f"Next action ({source}{anchor}): {ws['next_action']}")
        _append_priorities(
            lines,
            "Workstream priorities",
            ws.get("priorities") or [],
        )
        _append_nodes(lines, "Governing decisions", ws.get("governing_decisions") or [])
        _append_nodes(lines, "Backlog / open items", ws.get("backlog_items") or [])
        _append_nodes(lines, "Constraints", ws.get("constraints") or [])
        _append_nodes(lines, "Recent progress", ws.get("recent_progress") or [])
        artifacts = ws.get("artifacts") or []
        if artifacts:
            lines.append("Artifacts:")
            for artifact in artifacts:
                path = artifact.get("path") or "(repo)"
                ids = ", ".join(str(i) for i in artifact.get("node_ids") or [])
                lines.append(f"- {artifact['repo']} :: {path} (nodes: {ids})")
        omitted = {
            key: value
            for key, value in (ws.get("omitted") or {}).items()
            if value
        }
        if omitted:
            lines.append(
                "Omitted: "
                + ", ".join(f"{key}={value}" for key, value in sorted(omitted.items()))
            )
    unanchored = report.get("unanchored_evidence") or []
    if unanchored:
        lines.extend([
            "",
            "## Unanchored Recent Evidence",
            "These recent durable-looking rows are not attached to a workstream. "
            "Treat them as prompts for user-confirmed anchoring, not automatic backfill.",
        ])
        for item in unanchored:
            suggestion = ""
            if item.get("suggested_workstream_id"):
                suggestion = (
                    f" -> suggest id={item['suggested_workstream_id']} "
                    f"{item['suggested_workstream_title']}"
                )
            lines.append(
                f"- id={item['id']} [{item['kind']}/{item['status']}] "
                f"{item['title']}{suggestion}; {item['reason']}"
            )
    receipts = report.get("lifecycle_receipts") or []
    if receipts:
        lines.extend(["", "## Workstream Lifecycle Receipts"])
        lines.extend(f"- {item['receipt']}" for item in receipts)
    omitted = {
        key: value
        for key, value in (report.get("omitted") or {}).items()
        if value
    }
    if omitted:
        lines.extend([
            "",
            "Report omissions: "
            + ", ".join(f"{key}={value}" for key, value in sorted(omitted.items())),
        ])
    return "\n".join(lines) + "\n"


def _append_nodes(lines: list[str], label: str, nodes: list[dict]) -> None:
    if not nodes:
        return
    lines.append(f"{label}:")
    for node in nodes:
        tier = f"; {node['authority_tier']}" if node.get("authority_tier") else ""
        relation = f"; via {node['relation']}" if node.get("relation") else ""
        lines.append(
            f"- id={node['id']} [{node['kind']}/{node['status']}] "
            f"{node['title']}{tier}{relation}"
        )


def _append_priorities(lines: list[str], label: str, rows: list[dict]) -> None:
    if not rows:
        return
    lines.append(f"{label}:")
    for row in rows:
        lock = "locked" if row.get("locked") else "floating"
        lines.append(
            f"- P{row.get('effective_rank')} id={row['id']} ({lock}) "
            f"{row['title']}"
        )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Show latch's minimal project-direction view."
    )
    ap.add_argument("--project", default=os.getcwd(),
                    help="project directory whose KB should be read (default: cwd)")
    ap.add_argument("--limit", type=int, default=3,
                    help="number of workstreams to show (default: 3)")
    ap.add_argument("--member-limit", type=int, default=20,
                    help="member nodes scanned per workstream (default: 20)")
    ap.add_argument("--unanchored-limit", type=int, default=5,
                    help="recent unanchored evidence rows to show (default: 5)")
    ap.add_argument(
        "--compact",
        action="store_true",
        help="emit the bounded on-demand catch-up view",
    )
    ap.add_argument("--format", choices=("text", "json"), default="text")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conn = db.connect(args.project)
    try:
        report = assemble_project_direction(
            conn,
            limit=args.limit,
            member_limit=args.member_limit,
            unanchored_limit=args.unanchored_limit,
            compact=args.compact,
        )
    finally:
        conn.close()
    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(format_text(report), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
