"""Deterministic, manifest-bound retirement of proven Latch vault pollution.

This command intentionally handles only two exact, already-observed classes:

* ``tests/measure_write_path.py`` benchmark fixtures whose title/body encode
  the same integer and topic modulo.
* no-op compactor progress nodes tied to an ended, zero-turn session and
  carrying an explicit no-work phrase.

It never deletes nodes. Apply marks proven junk stale and tombstones every
active incident edge, preserving the complete audit trail. A verified protected
snapshot is mandatory and is created while the project write lock is held.

Usage:
    python src/pollution_cleanup.py metrics --project /path/to/project
    python src/pollution_cleanup.py plan --project /path/to/project \
        --manifest /path/to/cleanup.json
    python src/pollution_cleanup.py apply --project /path/to/project \
        --manifest /path/to/cleanup.json --plan-sha256 <digest>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np

import db
import heal
import lockfile
import log_utils
import paths
import vault_backup
import vault_identity


MANIFEST_FORMAT = 1
PREDICATE_VERSION = "known-pollution-v1"
FIXTURE_TITLE_RE = re.compile(r"^seed ([0-9]+)$")
SESSION_TITLE_RE = re.compile(r"^(?:session|empty session)\b", re.IGNORECASE)
NO_OP_SIGNALS = (
    "no substantive work",
    "no work performed",
    "no work yet",
    "no work captured",
    "nothing substantive",
    "context only",
    "no user request",
    "no user task",
)


class CleanupSafetyError(RuntimeError):
    """The planned target no longer matches the live vault exactly."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _plan_digest(manifest: Mapping[str, Any]) -> str:
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"plan_sha256", "application"}
    }
    return _sha256_text(_canonical_json(payload))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, path)


def _identity(conn) -> vault_identity.VaultIdentity:
    identity = getattr(conn, "_kb_vault_identity", None)
    if not isinstance(identity, vault_identity.VaultIdentity):
        raise CleanupSafetyError("connection has no validated vault identity")
    return identity


def _active_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT n.*, s.turn_count AS session_turn_count,
               s.ended_at AS session_ended_at,
               (SELECT COUNT(*) FROM node_artifact na
                WHERE na.node_id = n.id) AS artifact_count
        FROM nodes n
        LEFT JOIN sessions s ON s.id = n.session_id
        WHERE n.status != 'stale'
        ORDER BY n.id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def classify_known_pollution(row: Mapping[str, Any]) -> str | None:
    """Return the exact pollution class, or ``None`` for anything ambiguous."""
    title = str(row.get("title") or "")
    body = str(row.get("body") or "")

    fixture = FIXTURE_TITLE_RE.fullmatch(title)
    if fixture is not None:
        index = int(fixture.group(1))
        expected = (
            f"seed node {index} about topic {index % 23}; filler so the body is a "
            "plausible length and the vector is not degenerate."
        )
        if (
            row.get("kind") == "fact"
            and body == expected
            and row.get("session_id") is None
            and row.get("workstream_id") is None
            and int(row.get("artifact_count") or 0) == 0
        ):
            return "benchmark_fixture"

    combined = f"{title}\n{body}".lower()
    if (
        row.get("kind") == "progress"
        and row.get("session_id")
        and int(row.get("session_turn_count") or 0) == 0
        and row.get("session_ended_at")
        and row.get("workstream_id") is None
        and int(row.get("artifact_count") or 0) == 0
        and SESSION_TITLE_RE.match(title)
        and any(signal in combined for signal in NO_OP_SIGNALS)
    ):
        return "no_op_session"
    return None


def _node_fingerprint(row: Mapping[str, Any], category: str) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "category": category,
        "kind": row["kind"],
        "title": row["title"],
        "body_sha256": _sha256_text(str(row.get("body") or "")),
        "status": row["status"],
        "session_id": row.get("session_id"),
        "session_turn_count": row.get("session_turn_count"),
        "session_ended_at": row.get("session_ended_at"),
        "workstream_id": row.get("workstream_id"),
        "artifact_count": int(row.get("artifact_count") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "ref_count": int(row.get("ref_count") or 0),
    }


def discover_known_pollution(conn) -> list[dict[str, Any]]:
    candidates = []
    for row in _active_rows(conn):
        category = classify_known_pollution(row)
        if category is not None:
            candidates.append(_node_fingerprint(row, category))
    return candidates


def _incident_edges(conn, node_ids: set[int]) -> list[dict[str, Any]]:
    if not node_ids:
        return []
    placeholders = ",".join("?" for _ in node_ids)
    rows = conn.execute(
        f"""
        SELECT id, src, dst, relation, created_at, created_by, status
        FROM edges
        WHERE status = 'active'
          AND (src IN ({placeholders}) OR dst IN ({placeholders}))
        ORDER BY id
        """,
        [*sorted(node_ids), *sorted(node_ids)],
    ).fetchall()
    return [dict(row) for row in rows]


def queue_metrics(conn) -> dict[str, Any]:
    """Pure nightly-candidate census; no edge, node, or budget mutation."""
    rows = _active_rows(conn)
    by_id = {int(row["id"]): row for row in rows}
    pollution = {
        node_id: category
        for node_id, row in by_id.items()
        if (category := classify_known_pollution(row)) is not None
    }
    candidates = conn.execute(
        """
        SELECT id, embedding
        FROM nodes
        WHERE status != 'stale' AND embedding IS NOT NULL
          AND kind NOT IN ('summary', 'workstream')
        ORDER BY id
        """
    ).fetchall()
    seen: set[tuple[int, int]] = set()
    queued: list[dict[str, Any]] = []
    summary = {
        "examined": 0,
        "collisions": 0,
        "skipped_stale": 0,
        "skipped_edge_exists": 0,
        "skipped_summary_or_workstream": 0,
    }
    for row in candidates:
        a_id = int(row["id"])
        a = by_id.get(a_id)
        if a is None:
            continue
        summary["examined"] += 1
        vector = np.frombuffer(row["embedding"], dtype=np.float32)
        for candidate in heal.find_near_duplicates(
            conn,
            vector,
            exclude_id=a_id,
            threshold=heal.LOW_TIER_SIMILARITY_THRESHOLD,
            top_k=heal.NIGHTLY_TOP_K,
        ):
            b_id = int(candidate["id"])
            pair = (min(a_id, b_id), max(a_id, b_id))
            if pair in seen:
                continue
            seen.add(pair)
            summary["collisions"] += 1
            b = by_id.get(b_id)
            if b is None or b.get("status") == "stale":
                summary["skipped_stale"] += 1
                continue
            if a.get("kind") in {"summary", "workstream"} or b.get("kind") in {
                "summary",
                "workstream",
            }:
                summary["skipped_summary_or_workstream"] += 1
                continue
            if heal.edge_exists_between(conn, a_id, b_id):
                summary["skipped_edge_exists"] += 1
                continue
            similarity = float(candidate["similarity"])
            tier = (
                "high"
                if similarity >= heal.NIGHTLY_SIMILARITY_THRESHOLD
                else "low"
            )
            queued.append(
                {
                    "a": pair[0],
                    "b": pair[1],
                    "similarity": similarity,
                    "tier": tier,
                    "pollution": bool(
                        pair[0] in pollution or pair[1] in pollution
                    ),
                }
            )

    tiers = Counter(pair["tier"] for pair in queued)
    pollution_tiers = Counter(
        pair["tier"] for pair in queued if pair["pollution"]
    )
    similarities = [pair["similarity"] for pair in queued]
    return {
        **summary,
        "queued": len(queued),
        "queued_by_tier": {"high": tiers["high"], "low": tiers["low"]},
        "known_pollution_nodes": len(pollution),
        "known_pollution_pairs": sum(pair["pollution"] for pair in queued),
        "known_pollution_pairs_by_tier": {
            "high": pollution_tiers["high"],
            "low": pollution_tiers["low"],
        },
        "similarity": {
            "min": min(similarities) if similarities else None,
            "max": max(similarities) if similarities else None,
            "gte_0_95": sum(value >= 0.95 for value in similarities),
        },
    }


def build_manifest(conn, *, project_path: str) -> dict[str, Any]:
    identity = _identity(conn)
    candidates = discover_known_pollution(conn)
    node_ids = {int(row["id"]) for row in candidates}
    edges = _incident_edges(conn, node_ids)
    counts = Counter(row["category"] for row in candidates)
    manifest: dict[str, Any] = {
        "format": MANIFEST_FORMAT,
        "predicate_version": PREDICATE_VERSION,
        "created_at": _utc_now(),
        "project_path": str(Path(project_path).expanduser().resolve()),
        "vault": {
            "uuid": identity.vault_uuid,
            "classification": identity.classification,
            "created_at": identity.created_at,
            "registry_fingerprint": identity.registry_fingerprint,
        },
        "candidates": candidates,
        "active_incident_edges": edges,
        "counts": {
            "candidates": len(candidates),
            "benchmark_fixture": counts["benchmark_fixture"],
            "no_op_session": counts["no_op_session"],
            "active_incident_edges": len(edges),
        },
        "queue_before": queue_metrics(conn),
        "application": None,
    }
    manifest["plan_sha256"] = _plan_digest(manifest)
    return manifest


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleanupSafetyError(f"could not read cleanup manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CleanupSafetyError("cleanup manifest must be a JSON object")
    if manifest.get("format") != MANIFEST_FORMAT:
        raise CleanupSafetyError("unsupported cleanup manifest format")
    if manifest.get("predicate_version") != PREDICATE_VERSION:
        raise CleanupSafetyError("cleanup predicate version mismatch")
    if manifest.get("application") is not None:
        raise CleanupSafetyError("cleanup manifest was already started or applied")
    expected = manifest.get("plan_sha256")
    if not isinstance(expected, str) or expected != _plan_digest(manifest):
        raise CleanupSafetyError("cleanup manifest digest mismatch")
    return manifest


def _verify_manifest_live(conn, manifest: Mapping[str, Any]) -> None:
    identity = _identity(conn)
    vault = manifest.get("vault") or {}
    if (
        vault.get("uuid") != identity.vault_uuid
        or vault.get("classification") != identity.classification
        or vault.get("registry_fingerprint") != identity.registry_fingerprint
    ):
        raise CleanupSafetyError("cleanup manifest targets a different vault")

    live_candidates = discover_known_pollution(conn)
    planned_candidates = list(manifest.get("candidates") or [])
    if live_candidates != planned_candidates:
        raise CleanupSafetyError(
            "live pollution candidates changed after planning; generate a new manifest"
        )
    node_ids = {int(row["id"]) for row in live_candidates}
    live_edges = _incident_edges(conn, node_ids)
    if live_edges != list(manifest.get("active_incident_edges") or []):
        raise CleanupSafetyError(
            "incident edge set changed after planning; generate a new manifest"
        )

    current_by_id = {
        int(row["id"]): row
        for row in _active_rows(conn)
        if int(row["id"]) in node_ids
    }
    for planned in planned_candidates:
        node_id = int(planned["id"])
        current = current_by_id.get(node_id)
        if current is None:
            raise CleanupSafetyError(f"planned node {node_id} is no longer active")
        category = classify_known_pollution(current)
        if category != planned["category"]:
            raise CleanupSafetyError(
                f"planned node {node_id} no longer matches its exact predicate"
            )
        if _sha256_text(str(current.get("body") or "")) != planned["body_sha256"]:
            raise CleanupSafetyError(f"planned node {node_id} body changed")


def apply_manifest(
    manifest_path: Path,
    *,
    project_path: str,
    expected_plan_sha256: str,
) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().absolute()
    manifest = _load_manifest(manifest_path)
    if manifest["plan_sha256"] != expected_plan_sha256:
        raise CleanupSafetyError("explicit plan digest does not match manifest")

    with lockfile.compactor_lock(project_path) as acquired:
        if not acquired:
            raise CleanupSafetyError("project write lock is held")

        conn = db.connect(project_path)
        try:
            _verify_manifest_live(conn, manifest)
        finally:
            conn.close()

        backup = vault_backup.create_snapshot(
            project_path,
            reason="known-pollution-cleanup",
        )
        manifest["application"] = {
            "state": "started",
            "started_at": _utc_now(),
            "backup": backup,
        }
        _atomic_write_json(manifest_path, manifest)

        conn = db.connect(project_path)
        try:
            # Revalidate after the online backup. The compactor lock blocks
            # normal MCP/compactor writers; this second check also catches any
            # unsupported direct SQLite writer.
            validation_copy = dict(manifest)
            validation_copy["application"] = None
            _verify_manifest_live(conn, validation_copy)
            conn.execute("BEGIN IMMEDIATE")
            for edge in manifest["active_incident_edges"]:
                changed = db.tombstone_edge_id_nc(conn, int(edge["id"]))
                if changed != 1:
                    raise CleanupSafetyError(
                        f"planned edge {edge['id']} was not active"
                    )
            for node in manifest["candidates"]:
                db.update_node_nc(conn, int(node["id"]), status="stale")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        verify = db.connect(project_path)
        try:
            remaining = discover_known_pollution(verify)
            if remaining:
                raise CleanupSafetyError(
                    f"{len(remaining)} known pollution nodes remain active"
                )
            integrity = verify.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = verify.execute("PRAGMA foreign_key_check").fetchall()
            queue_after_cleanup = queue_metrics(verify)
            active_nodes = verify.execute(
                "SELECT COUNT(*) FROM nodes WHERE status != 'stale'"
            ).fetchone()[0]
        finally:
            verify.close()

        receipt = {
            "state": "applied",
            "started_at": manifest["application"]["started_at"],
            "completed_at": _utc_now(),
            "backup": backup,
            "retired_nodes": len(manifest["candidates"]),
            "tombstoned_edges": len(manifest["active_incident_edges"]),
            "active_nodes_after": int(active_nodes),
            "integrity_check": integrity,
            "foreign_key_violations": len(foreign_keys),
            "queue_after_cleanup": queue_after_cleanup,
        }
        manifest["application"] = receipt
        _atomic_write_json(manifest_path, manifest)
        log_utils.emit_event(
            "pollution_cleanup",
            {
                "event": "known_pollution_retired",
                "predicate_version": PREDICATE_VERSION,
                "plan_sha256": manifest["plan_sha256"],
                "retired_nodes": receipt["retired_nodes"],
                "tombstoned_edges": receipt["tombstoned_edges"],
                "backup_manifest": backup["manifest"],
                "integrity_check": integrity,
                "foreign_key_violations": len(foreign_keys),
            },
            project_path=project_path,
            session_id=None,
        )
        return {
            "ok": True,
            "manifest": str(manifest_path),
            "plan_sha256": manifest["plan_sha256"],
            **receipt,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("metrics", "plan", "apply"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True)
        if name in {"plan", "apply"}:
            command.add_argument("--manifest", required=True, type=Path)
        if name == "apply":
            command.add_argument("--plan-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project = str(Path(args.project).expanduser().resolve())
    try:
        if args.command == "metrics":
            conn = db.connect(project)
            try:
                result = queue_metrics(conn)
            finally:
                conn.close()
        elif args.command == "plan":
            conn = db.connect(project)
            try:
                result = build_manifest(conn, project_path=project)
            finally:
                conn.close()
            _atomic_write_json(args.manifest, result)
            result = {
                "ok": True,
                "manifest": str(args.manifest.expanduser().absolute()),
                "plan_sha256": result["plan_sha256"],
                "counts": result["counts"],
                "queue_before": result["queue_before"],
            }
        else:
            result = apply_manifest(
                args.manifest,
                project_path=project,
                expected_plan_sha256=args.plan_sha256,
            )
    except CleanupSafetyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
