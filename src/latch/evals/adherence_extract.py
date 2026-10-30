"""Read-only extractor for the adherence harness's private scored tier.

Charter 5134 item 2(b). The scored tier is mined, not authored: it comes from
the typed ``rejected_path`` bank V2 built in the live vault — real decisions
recorded before this benchmark existed, which is the legitimacy bar id=3789
sets and the thing that kills "you wrote the test to pass."

Two hard rules, both enforced here rather than documented:

* The live vault is opened **read-only**. ``mode=ro`` plus ``query_only``, no
  schema repair, no identity stamping, no migration — none of the setup writes
  ``db.connect`` performs. Charter hard edge 3.
* The output **never defaults inside the repo tree**. One ``git add -A`` must
  not be able to leak private KB bodies into a public repo (id=4781), so an
  explicit destination is required and a destination inside the repo is
  refused.

What comes out is a **draft**, not a runnable probe set. Mining supplies the
decision and its recorded reason; the E3 revival probe and the E4 negative
control still have to be authored per decision by a human (id=3789 budgets
hours per repo for exactly this). ``adherence.load_probe_sets`` refuses a
draft, so a draft can never be silently scored.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from latch.store import paths


DRAFT_REQUEST_PLACEHOLDER = "TODO(author): "

E3_AUTHORING_HINT = (
    "TODO(author): a plausible request whose natural implementation revives "
    "this rejected option"
)
E4_AUTHORING_HINT = (
    "TODO(author): an adjacent, legitimate request that must proceed"
)


class ExtractError(Exception):
    """The extractor refused to run, or refused a destination."""


def open_readonly(vault_dir: Path) -> sqlite3.Connection:
    """Open a vault's kb.db strictly read-only.

    Read-only is a property of the connection, not a convention: ``mode=ro``
    means SQLite itself rejects a write, and ``query_only`` rejects one earlier
    still. Neither takes a write lock, so a running daemon is undisturbed.
    """
    db_file = Path(vault_dir) / "kb.db"
    if not db_file.exists():
        raise ExtractError(f"no kb.db under {vault_dir}")
    conn = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def read_rejected_paths(
    conn: sqlite3.Connection, *, limit: int | None = None, source: str | None = None
) -> list[dict[str, Any]]:
    """Every typed rejection, joined to the decision node that carries it."""
    sql = (
        "SELECT rp.id AS rejected_path_id, rp.node_id, rp.option, rp.reason, "
        "       rp.ratifier, rp.decided_at, rp.scope_predicate, rp.source, "
        "       n.title AS node_title, n.body AS node_body, n.kind AS node_kind, "
        "       n.status AS node_status "
        "FROM rejected_path rp JOIN nodes n ON n.id = rp.node_id "
    )
    params: list[Any] = []
    if source:
        sql += "WHERE rp.source = ? "
        params.append(source)
    sql += "ORDER BY rp.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [dict(row) for row in conn.execute(sql, params)]


def row_to_draft(row: dict[str, Any]) -> dict[str, Any]:
    """Shape one ``rejected_path`` row into a probe-set draft.

    Pure over a row mapping, so the committed synthetic fixtures cover this
    parsing rather than live text (priority id=4114, and the gate ruling on
    this mission).
    """
    set_id = f"mined-{row['rejected_path_id']}"
    return {
        "set_id": set_id,
        "draft": True,
        "provenance": {
            "rejected_path_id": row["rejected_path_id"],
            "node_id": row["node_id"],
            "source": row.get("source"),
            "ratifier": row.get("ratifier"),
            "decided_at": row.get("decided_at"),
            "scope_predicate": row.get("scope_predicate"),
            "node_status": row.get("node_status"),
        },
        "decision": {
            "kind": row.get("node_kind") or "decision",
            "status": row.get("node_status") or "canonical",
            "title": row["node_title"],
            "text": row["node_body"],
        },
        "rejected_option": {
            "option": row["option"],
            "reason": row["reason"],
        },
        "e3": {
            "probe_id": f"{set_id}-e3",
            "request": E3_AUTHORING_HINT,
            "forbidden_paths": [],
            "forbidden_markers": [],
        },
        "e4": {
            "probe_id": f"{set_id}-e4",
            "request": E4_AUTHORING_HINT,
        },
    }


def rows_to_drafts(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row_to_draft(row) for row in rows]


def repo_root() -> Path:
    """The latch install/repo root — the tree an ``git add -A`` would sweep."""
    return Path(paths.KB_ROOT).resolve()


def validate_destination(out_path: Path, *, tree: Path | None = None) -> Path:
    """Refuse any destination inside the repo tree (id=4781)."""
    if out_path is None:
        raise ExtractError(
            "scored-tier output path is required; there is deliberately no default"
        )
    resolved = Path(out_path).expanduser().resolve()
    forbidden = (tree or repo_root()).resolve()
    if resolved == forbidden or resolved.is_relative_to(forbidden):
        raise ExtractError(
            f"refusing to write the private scored tier inside the repo tree: "
            f"{resolved} is under {forbidden}. One `git add -A` must never be "
            f"able to leak live-vault bodies (KB id=4781)."
        )
    return resolved


def write_drafts(drafts: list[dict[str, Any]], out_path: Path, *, tree: Path | None = None) -> Path:
    destination = validate_destination(out_path, tree=tree)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        for draft in drafts:
            handle.write(json.dumps(draft, sort_keys=True) + "\n")
    destination.chmod(0o600)
    return destination


def extract(
    vault_dir: Path,
    out_path: Path,
    *,
    limit: int | None = None,
    source: str | None = None,
    tree: Path | None = None,
) -> dict[str, Any]:
    """Read-only mine of ``vault_dir`` into a private draft file."""
    destination = validate_destination(out_path, tree=tree)
    conn = open_readonly(vault_dir)
    try:
        rows = read_rejected_paths(conn, limit=limit, source=source)
    finally:
        conn.close()
    drafts = rows_to_drafts(rows)
    write_drafts(drafts, destination, tree=tree)
    return {
        "vault_dir": str(vault_dir),
        "out_path": str(destination),
        "rows": len(rows),
        "drafts": len(drafts),
        "note": (
            "drafts only — E3/E4 requests and the forbidden surface still need "
            "human authoring before any scored run (protocol §3)"
        ),
    }
