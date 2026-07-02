#!/usr/bin/env python3
"""Read-only project-direction report.

This is the first minimal project-direction layer: assemble existing KB
primitives into a workstream-centered view without adding a storage rebuild.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

import artifacts as artifact_store  # noqa: E402
import db  # noqa: E402


BACKLOG_KINDS = {"open_question", "idea"}
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
AUTHORITY_RELATIONS = {
    "constrains",
    "depends_on",
    "motivates",
    "replaces",
    "supersedes",
    "reconciled_by",
}
OBJECTIVE_RE = re.compile(r"^\s*(?:objective|goal)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
NEXT_ACTION_RE = re.compile(
    r"^\s*(?:next action|next step)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


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
    next_action: str | None


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
) -> dict[str, Any]:
    """Assemble a compact project-direction report from existing KB rows."""
    workstreams = _workstream_seeds(conn, limit=limit)
    rows = [
        _assemble_workstream(conn, ws, member_limit=member_limit)
        for ws in workstreams
    ]
    rows = [row for row in rows if row is not None]
    backlog_total = sum(len(row.backlog_items) for row in rows)
    decision_total = sum(len(row.governing_decisions) for row in rows)
    artifact_total = sum(len(row.artifacts) for row in rows)
    unanchored = _unanchored_candidates(conn, rows, limit=unanchored_limit)
    summary = (
        f"Latch assembled {len(rows)} workstream(s), {decision_total} governing "
        f"decision(s), {backlog_total} backlog/open item(s), and "
        f"{artifact_total} artifact coordinate(s) from the local KB."
    )
    if unanchored:
        summary += (
            f" It also found {len(unanchored)} recent unanchored item(s) that "
            "may need a user-confirmed workstream."
        )
    return {
        "label": "Latch project direction",
        "source": "project_direction",
        "must_display_to_user": True,
        "summary": summary,
        "why_it_matters": (
            "This keeps the next-step view anchored in workstreams, current "
            "decision authority, open work, and artifact evidence instead of a "
            "generic memory summary."
        ),
        "used": {
            "workstreams": len(rows),
            "governing_decisions": decision_total,
            "backlog_items": backlog_total,
            "artifacts": artifact_total,
            "unanchored_items": len(unanchored),
        },
        "workstreams": [asdict(row) for row in rows],
        "unanchored_evidence": [asdict(row) for row in unanchored],
    }


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
    members = _workstream_members(conn, wid, limit=member_limit)
    connected = _connected_nodes(conn, wid, members)
    decisions = _governing_decisions(wid, members, connected)
    backlog = _nodes_for_kinds(members, BACKLOG_KINDS)
    constraints = _nodes_for_kinds(members, CONSTRAINT_KINDS)
    progress = _nodes_for_kinds(members, PROGRESS_KINDS)
    artifacts = _artifacts_for_nodes(conn, [wid, *[int(n["id"]) for n in members]])
    next_action = _next_action(ws, backlog=backlog, progress=progress)
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
        next_action=next_action,
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
                authority_tier="local_implementation_decision",
            )
    for node in connected:
        if node.get("kind") not in DECISION_KINDS:
            continue
        rel = db.canonicalize_relation(str(node.get("relation") or "related_to"))
        if rel not in AUTHORITY_RELATIONS and _int_or_none(node.get("workstream_id")) != workstream_id:
            continue
        nid = int(node["id"])
        tier = _authority_tier(
            relation=rel,
            decision_workstream_id=_int_or_none(node.get("workstream_id")),
            workstream_id=workstream_id,
        )
        previous = out.get(nid)
        if previous and previous.authority_tier != "local_implementation_decision":
            continue
        out[nid] = DirectionNode(
            id=nid,
            kind=str(node["kind"]),
            title=str(node["title"]),
            status=str(node["status"]),
            authority_tier=tier,
            relation=rel,
        )
    return sorted(out.values(), key=lambda n: (n.authority_tier or "", n.id))


def _authority_tier(
    *,
    relation: str,
    decision_workstream_id: int | None,
    workstream_id: int,
) -> str:
    if relation in {"constrains", "depends_on", "motivates"} and decision_workstream_id is None:
        return "foundational_project_decision"
    if relation in AUTHORITY_RELATIONS:
        return "governing_workstream_decision"
    if decision_workstream_id == workstream_id:
        return "local_implementation_decision"
    return "decision_evidence"


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
) -> str | None:
    body = str(ws.get("body") or "")
    match = NEXT_ACTION_RE.search(body)
    if match:
        return match.group(1).strip()
    if backlog:
        return f"Resolve: {backlog[0].title}"
    if progress:
        return f"Continue from: {progress[0].title}"
    return None


def format_text(report: dict[str, Any]) -> str:
    lines = [
        "# Latch Project Direction",
        "",
        report["summary"],
        "",
        f"Why this matters: {report['why_it_matters']}",
    ]
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
            lines.append(f"Next action: {ws['next_action']}")
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
