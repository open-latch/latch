"""Authority-safe, domain-complete projection for compiled rejection policy.

This is the DB-facing half of A2.  It reads every rejection that can belong to
one explicit policy domain in a single SQLite snapshot and classifies rows as
binding or advisory.  It deliberately does not search, rank, retrieve through
the Latch gate, compile predicates, or evaluate host actions.

The returned rows contain private option/reason/predicate text for the local
compiler.  ``reason_counts`` and ``freshness_token`` are the only aggregate
surfaces intended for structural reporting; callers must not log row objects.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import sqlite3
from types import MappingProxyType
from typing import Mapping

# Keep the deterministic projection path independent of the wider DB runtime.
# Importing db here pulls embedding and network-adjacent modules into the
# projection-to-verdict path even though only this closed authority set is
# needed.
JUDGMENT_KINDS = frozenset({"decision", "preference"})
PROJECTION_ENGINE = "predicate-policy-projection-v1"
_SAFE_POLICY_DOMAIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


@dataclass(frozen=True)
class ProjectedPolicyRow:
    """One private enforcement row plus its current authority proof."""

    rejected_path_id: int
    node_id: int
    option: str
    reason: str
    ratifier: str | None
    decided_at: str | None
    scope_predicate: str | None
    source: str
    policy_domain_id: str | None
    owner_kind: str
    owner_status: str
    owner_updated_at: str
    latest_ratification_id: int | None
    latest_ratification_ratifier: str | None
    latest_ratification_decided_at: str | None
    latest_ratification_action: str | None
    latest_ratification_source: str | None
    superseder_ids: tuple[int, ...]
    reconciler_ids: tuple[int, ...]
    authority_basis: str
    classification: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class PolicyProjection:
    """Complete current projection for exactly one explicit policy domain."""

    engine: str
    policy_domain_id: str
    binding_rows: tuple[ProjectedPolicyRow, ...]
    advisory_rows: tuple[ProjectedPolicyRow, ...]
    reason_counts: Mapping[str, int]
    freshness_token: str


def project_policy_domain(
    conn: sqlite3.Connection,
    policy_domain_id: str,
) -> PolicyProjection:
    """Return the complete binding/advisory set for ``policy_domain_id``.

    Rows explicitly bound to another domain are excluded.  Legacy unbound rows
    are included as advisory because they may need re-declaration, but can
    never block.  The function uses a savepoint so its base rows, authority
    outcomes, and graph edges all come from one SQLite snapshot.  It executes
    no mutations and leaves any caller-owned transaction intact.
    """
    domain = _require_policy_domain_id(policy_domain_id)
    _require_projection_schema(conn)

    savepoint = "predicate_policy_projection"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        base_rows = conn.execute(
            """
            SELECT
                r.id AS rejected_path_id,
                r.node_id,
                r.option,
                r.reason,
                r.ratifier,
                r.decided_at,
                r.scope_predicate,
                r.source,
                r.policy_domain_id,
                n.kind AS owner_kind,
                n.status AS owner_status,
                n.updated_at AS owner_updated_at,
                latest.id AS latest_ratification_id,
                latest.ratifier AS latest_ratification_ratifier,
                latest.decided_at AS latest_ratification_decided_at,
                latest.action AS latest_ratification_action,
                latest.source AS latest_ratification_source
            FROM rejected_path AS r
            JOIN nodes AS n ON n.id = r.node_id
            LEFT JOIN ratification AS latest
              ON latest.id = (
                    SELECT MAX(history.id)
                    FROM ratification AS history
                    WHERE history.node_id = r.node_id
              )
            WHERE r.policy_domain_id = ? OR r.policy_domain_id IS NULL
            ORDER BY r.id ASC
            """,
            (domain,),
        ).fetchall()

        # A stale would-be replacement does not erase a current owner.  Active
        # supersedes/replaces from every other non-stale source remain a
        # conservative authority stop, including a still-staging source.
        superseder_rows = conn.execute(
            """
            SELECT DISTINCT edge.dst AS owner_id, edge.src AS related_id
            FROM edges AS edge
            JOIN nodes AS source_node ON source_node.id = edge.src
            JOIN rejected_path AS rejected ON rejected.node_id = edge.dst
            WHERE edge.status = 'active'
              AND edge.relation IN ('supersedes', 'replaces')
              AND source_node.status != 'stale'
              AND (
                    rejected.policy_domain_id = ?
                    OR rejected.policy_domain_id IS NULL
              )
            ORDER BY edge.dst ASC, edge.src ASC
            """,
            (domain,),
        ).fetchall()
        reconciler_rows = conn.execute(
            """
            SELECT DISTINCT edge.src AS owner_id, edge.dst AS related_id
            FROM edges AS edge
            JOIN nodes AS target_node ON target_node.id = edge.dst
            JOIN rejected_path AS rejected ON rejected.node_id = edge.src
            WHERE edge.status = 'active'
              AND edge.relation = 'reconciled_by'
              AND target_node.status != 'stale'
              AND (
                    rejected.policy_domain_id = ?
                    OR rejected.policy_domain_id IS NULL
              )
            ORDER BY edge.src ASC, edge.dst ASC
            """,
            (domain,),
        ).fetchall()
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")

    superseders = _edge_map(superseder_rows)
    reconcilers = _edge_map(reconciler_rows)
    binding: list[ProjectedPolicyRow] = []
    advisory: list[ProjectedPolicyRow] = []
    reason_counts: Counter[str] = Counter()

    for raw in base_rows:
        row = dict(raw)
        node_id = int(row["node_id"])
        latest_action = row["latest_ratification_action"]
        reasons: list[str] = []

        if row["source"] != "declared":
            reasons.append("row_source_backfill")
        if row["policy_domain_id"] is None:
            reasons.append("row_domain_unbound")
        if not _nonblank(row["ratifier"]) or not _nonblank(row["decided_at"]):
            reasons.append("row_declaration_incomplete")
        if row["owner_kind"] not in JUDGMENT_KINDS:
            reasons.append("owner_not_judgment")
        if row["owner_status"] != "canonical":
            reasons.append("owner_not_canonical")
        if latest_action == "reject":
            reasons.append("owner_latest_ratification_rejected")
        if superseders[node_id]:
            reasons.append("owner_superseded")
        if reconcilers[node_id]:
            reasons.append("owner_unresolved_reconciliation")

        if latest_action == "ratify":
            authority_basis = "latest_ratification"
        elif latest_action == "reject":
            authority_basis = "latest_rejection"
        elif (
            row["owner_kind"] in JUDGMENT_KINDS
            and row["owner_status"] == "canonical"
        ):
            # V3 grandfathering: already-canonical judgments were not given
            # synthetic ratification rows when the append-only table shipped.
            authority_basis = "legacy_canonical"
        else:
            authority_basis = "none"

        reason_codes = tuple(reasons)
        classification = "binding" if not reason_codes else "advisory"
        projected = ProjectedPolicyRow(
            rejected_path_id=int(row["rejected_path_id"]),
            node_id=node_id,
            option=str(row["option"]),
            reason=str(row["reason"]),
            ratifier=row["ratifier"],
            decided_at=row["decided_at"],
            scope_predicate=row["scope_predicate"],
            source=str(row["source"]),
            policy_domain_id=row["policy_domain_id"],
            owner_kind=str(row["owner_kind"]),
            owner_status=str(row["owner_status"]),
            owner_updated_at=str(row["owner_updated_at"]),
            latest_ratification_id=(
                int(row["latest_ratification_id"])
                if row["latest_ratification_id"] is not None
                else None
            ),
            latest_ratification_ratifier=row["latest_ratification_ratifier"],
            latest_ratification_decided_at=row["latest_ratification_decided_at"],
            latest_ratification_action=latest_action,
            latest_ratification_source=row["latest_ratification_source"],
            superseder_ids=superseders[node_id],
            reconciler_ids=reconcilers[node_id],
            authority_basis=authority_basis,
            classification=classification,
            reason_codes=reason_codes,
        )
        if classification == "binding":
            binding.append(projected)
        else:
            advisory.append(projected)
            reason_counts.update(reason_codes)

    frozen_counts = MappingProxyType(dict(sorted(reason_counts.items())))
    token = _freshness_token(domain, binding, advisory, frozen_counts)
    return PolicyProjection(
        engine=PROJECTION_ENGINE,
        policy_domain_id=domain,
        binding_rows=tuple(binding),
        advisory_rows=tuple(advisory),
        reason_counts=frozen_counts,
        freshness_token=token,
    )


def _require_policy_domain_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_POLICY_DOMAIN_ID_RE.fullmatch(value) is None
    ):
        raise ValueError(
            "policy_domain_id must be a 1-256 character ASCII token using "
            "letters, digits, '.', '_', ':', or '-'"
        )
    return value


def _require_projection_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(rejected_path)").fetchall()
    }
    if "policy_domain_id" not in columns:
        raise RuntimeError(
            "predicate policy projection requires the additive "
            "rejected_path.policy_domain_id migration"
        )


def _edge_map(rows: list[sqlite3.Row]) -> defaultdict[int, tuple[int, ...]]:
    collected: defaultdict[int, list[int]] = defaultdict(list)
    for row in rows:
        collected[int(row["owner_id"])].append(int(row["related_id"]))
    result: defaultdict[int, tuple[int, ...]] = defaultdict(tuple)
    for node_id, related_ids in collected.items():
        result[node_id] = tuple(sorted(set(related_ids)))
    return result


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _freshness_token(
    policy_domain_id: str,
    binding_rows: list[ProjectedPolicyRow],
    advisory_rows: list[ProjectedPolicyRow],
    reason_counts: Mapping[str, int],
) -> str:
    payload = {
        "engine": PROJECTION_ENGINE,
        "policy_domain_id": policy_domain_id,
        "binding_rows": [asdict(row) for row in binding_rows],
        "advisory_rows": [asdict(row) for row in advisory_rows],
        "reason_counts": dict(reason_counts),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
