"""Standing-directive ("top of mind") store for latch.

Priorities are short standing guidelines the user wants weighed on build/plan
work. Overall priorities apply across the project; workstream priorities are
additive guidance only when that workstream is in scope. They are the complement
of retrieval: the graph answers "what's relevant to this prompt?"; priorities
answer "what must always be considered for this scope?". A CEO's "always think
about security" is the canonical overall example — it must colour work that has
nothing to do with security.

Storage (deliberately migration-free on `nodes`):
  * `kind='priority'` nodes in the existing `nodes` table — unifies with the
    planned personal-layer / todo store (id=1057), reusing insert/list/update.
  * NO embedding — keeps priorities out of vector_search, kb_gate similarity
    seeds, and nightly-heal's similarity contradiction scan (the "surface-only
    / no-traversal" property id=1057 wanted). The FTS trigger still indexes
    them, so gate seed collection filters `kind='priority'` explicitly
    (see gate.EXCLUDED_SEED_KINDS) and the per-prompt hook excludes the kind.
  * Scope rides the existing `nodes.workstream_id` field: NULL = overall,
    non-NULL = scoped to that workstream.
  * Lifecycle rides the existing status field: 'canonical' = active,
    'stale' = retired (drops out of default reads, survives as audit trail).
  * Ordering + graveyard date live in a side table `priority_order`
    (db._migrate_priorities_order), mirroring the `focus` table pattern.

Ranking model (locked vs. floating):
  * A priority is **locked** when its `priority_order.rank` is a number — the
    user explicitly placed it at that absolute slot. Locked priorities are
    immutable to automatic adds; only an explicit user reorder/retire moves them.
  * A priority is **floating** when `rank` is NULL — added generically, with no
    explicit rank. Floating priorities order by recency (newest-first).
  * Effective P1..PN order (computed at read time): locked priorities claim
    their absolute slot; floating priorities fill the remaining slots top-down,
    newest first. A generic add therefore lands in the topmost slot no locked
    priority claims — never displacing a locked one.
  * Genuinely ambiguous user actions (an explicit rank colliding with an
    already-locked priority) return a `conflict` result rather than inventing a
    placement rule — the agent surfaces it and asks the user how to reorder.

Project-wide by default — NOT user-filtered (unlike todos). The CEO's directive
applies to every dev. Workstream-scoped rows apply to every dev working inside
that workstream. `created_by` is still stamped for audit.

Injected at two already-paid-for surfaces (no per-prompt LLM cost):
  * kb_gate classifier prompt — the primary surface (gate.py).
  * SessionStart brief — a "Top of mind" section (hooks/session_start.py).

Capture is offered via a deterministic regex nudge in the UserPromptSubmit
hook (no LLM, no DB query) — see hooks/user_prompt_submit.py.
"""
from __future__ import annotations

import os
import sqlite3

import db


PRIORITY_KIND = "priority"
ACTIVE_STATUS = "canonical"   # active priority
RETIRED_STATUS = "stale"      # retired — out of default reads, kept for audit

# Hard cap on active priorities. Forces curation and bounds the gate-prompt /
# brief injection token cost. "Top of mind" loses meaning past a handful —
# keep it low on purpose. Adding past the cap is refused (no silent eviction).
# Configurable via CLAUDE_KB_PRIORITY_CAP (mirrors CLAUDE_KB_HEAL_CAP); default 5.
try:
    MAX_ACTIVE = int(os.environ.get("CLAUDE_KB_PRIORITY_CAP") or 5)
except ValueError:
    MAX_ACTIVE = 5
if MAX_ACTIVE < 1:
    MAX_ACTIVE = 5

# Display cap for the node title (the directive itself lives in full in body).
_TITLE_CHARS = 120


# ---------- ordering helpers (priority_order side table) ----------

def _clamp_rank(rank, n: int) -> int | None:
    """Clamp a requested 1-based rank into [1, max(n, 1)]. Returns None if the
    input isn't an int-like value."""
    try:
        r = int(rank)
    except (TypeError, ValueError):
        return None
    return min(max(r, 1), max(n, 1))


def _order_row(conn: sqlite3.Connection, node_id: int):
    return conn.execute(
        "SELECT node_id, rank, retired_at FROM priority_order WHERE node_id = ?",
        (node_id,),
    ).fetchone()


def _scope_label(workstream_id: int | None) -> str:
    return "overall" if workstream_id is None else f"workstream {workstream_id}"


def _scope_title(conn: sqlite3.Connection, workstream_id: int | None) -> str | None:
    if workstream_id is None:
        return None
    row = conn.execute(
        "SELECT title FROM nodes WHERE id = ? AND kind = 'workstream'",
        (workstream_id,),
    ).fetchone()
    return row["title"] if row else None


def _scope_predicate(workstream_id: int | None, params: list) -> str:
    if workstream_id is None:
        return " AND n.workstream_id IS NULL"
    params.append(workstream_id)
    return " AND n.workstream_id = ?"


def _validate_workstream_scope(
    conn: sqlite3.Connection, workstream_id: int | None,
) -> dict | None:
    if workstream_id is None:
        return None
    try:
        wid = int(workstream_id)
    except (TypeError, ValueError):
        return {"error": f"workstream_id must be an integer, got {workstream_id!r}"}
    node = conn.execute(
        "SELECT kind, status FROM nodes WHERE id = ?", (wid,),
    ).fetchone()
    if node is None:
        return {"error": f"workstream {wid} not found"}
    if node["kind"] != "workstream":
        return {"error": f"node {wid} is kind={node['kind']!r}, not a workstream"}
    if node["status"] == RETIRED_STATUS:
        return {"error": f"workstream {wid} is stale"}
    return None


def _locked_at(
    conn: sqlite3.Connection,
    rank: int,
    *,
    workstream_id: int | None = None,
    exclude: int | None = None,
):
    """Return the node_id of an ACTIVE priority locked at `rank`, or None.
    `exclude` skips a given node (used by reorder so a node doesn't clash with
    itself)."""
    sql = (
        "SELECT po.node_id FROM priority_order po "
        "JOIN nodes n ON n.id = po.node_id "
        "WHERE n.kind = ? AND n.status = ? AND po.retired_at IS NULL "
        "AND po.rank = ?"
    )
    params = [PRIORITY_KIND, ACTIVE_STATUS, rank]
    sql += _scope_predicate(workstream_id, params)
    if exclude is not None:
        sql += " AND po.node_id != ?"
        params.append(exclude)
    row = conn.execute(sql, params).fetchone()
    return row["node_id"] if row else None


def _effective_order(
    conn: sqlite3.Connection, *, workstream_id: int | None = None,
) -> list[int]:
    """Active priority node_ids in effective P1..PN order.

    Locked (rank non-NULL) claim absolute slots; floating (rank NULL) fill the
    rest newest-first. With N active priorities there are exactly N slots, so
    every priority is placed. Locked collisions (two locked at the same slot)
    are resolved deterministically to the next free slot — but add/reorder
    surface such collisions as a `conflict` before they can happen."""
    params = [PRIORITY_KIND, ACTIVE_STATUS]
    sql = (
        """
        SELECT n.id AS id, po.rank AS rank, n.created_at AS created_at
        FROM nodes n
        JOIN priority_order po ON po.node_id = n.id
        WHERE n.kind = ? AND n.status = ? AND po.retired_at IS NULL
        """
    )
    sql += _scope_predicate(workstream_id, params)
    rows = conn.execute(sql, params).fetchall()
    n = len(rows)
    if n == 0:
        return []
    locked = sorted(
        (r for r in rows if r["rank"] is not None),
        key=lambda r: (r["rank"], r["created_at"] or "", r["id"]),
    )
    floating = sorted(
        (r for r in rows if r["rank"] is None),
        key=lambda r: (r["created_at"] or "", r["id"]),
        reverse=True,  # newest first
    )
    slots: list[int | None] = [None] * n
    for r in locked:
        want = _clamp_rank(r["rank"], n) - 1  # 0-based
        if slots[want] is None:
            slots[want] = r["id"]
        else:
            for i in range(n):
                if slots[i] is None:
                    slots[i] = r["id"]
                    break
    fi = 0
    for i in range(n):
        if slots[i] is None and fi < len(floating):
            slots[i] = floating[fi]["id"]
            fi += 1
    return [x for x in slots if x is not None]


def _active_summary(
    conn: sqlite3.Connection, *, workstream_id: int | None = None,
) -> list[dict]:
    """Compact view of the active set (id, effective position, locked, text) —
    attached to `conflict` results so the agent can show the user the current
    ordering and ask how to reorder."""
    out = []
    for p in list_priorities(conn, workstream_id=workstream_id):
        out.append({
            "id": p["id"],
            "position": p.get("effective_rank"),
            "locked_rank": p.get("rank"),
            "locked": p.get("locked", False),
            "scope": p.get("scope"),
            "workstream_id": p.get("workstream_id"),
            "text": p["title"],
        })
    return out


# ---------- add / list / reorder / retire ----------

def add_priority(
    conn: sqlite3.Connection,
    text: str,
    *,
    note: str | None = None,
    rank: int | None = None,
    workstream_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Add a standing priority.

    Enforces MAX_ACTIVE per scope — refuses (no write) when the overall or
    workstream-local cap is reached so each set stays small and the user is
    forced to curate. Inserted WITHOUT an embedding so it never pollutes
    retrieval / gate similarity seeds / heal.

    `rank` omitted (the common case) → the priority is **floating**: it stacks
    onto the top of the unlocked region (newest-first) and never displaces a
    locked priority. `rank` provided → the priority is **locked** at that
    absolute slot. If the requested slot is already locked by another priority,
    no write happens and a `conflict` is returned for the agent to surface."""
    text = (text or "").strip()
    if not text:
        return {"error": "empty priority text"}
    scope_error = _validate_workstream_scope(conn, workstream_id)
    if scope_error:
        return scope_error
    if workstream_id is not None:
        workstream_id = int(workstream_id)
    active = list_priorities(conn, workstream_id=workstream_id)
    if len(active) >= MAX_ACTIVE:
        return {
            "error": (
                f"active priority cap ({MAX_ACTIVE}) reached for "
                f"{_scope_label(workstream_id)}"
            ),
            "scope": "overall" if workstream_id is None else "workstream",
            "workstream_id": workstream_id,
            "active": [{"id": p["id"], "text": p["title"]} for p in active],
            "hint": "retire one in this scope first: kb_priority_retire(node_id)",
        }

    locked_rank = None
    if rank is not None:
        # New total after this insert = len(active) + 1.
        locked_rank = _clamp_rank(rank, len(active) + 1)
        clash = _locked_at(conn, locked_rank, workstream_id=workstream_id)
        if clash is not None:
            return {
                "conflict": "rank_locked",
                "requested_rank": locked_rank,
                "held_by": clash,
                "scope": "overall" if workstream_id is None else "workstream",
                "workstream_id": workstream_id,
                "active": _active_summary(conn, workstream_id=workstream_id),
                "hint": (
                    f"rank {locked_rank} is locked by id={clash}; pick another "
                    "rank, move/unlock it first via kb_priority_reorder, or "
                    "retire it — ask the user which."
                ),
            }

    title = text if len(text) <= _TITLE_CHARS else text[: _TITLE_CHARS - 1] + "…"
    body = text if not note else f"{text}\n\n{note.strip()}"
    nid = db.insert_node(
        conn,
        kind=PRIORITY_KIND,
        title=title,
        body=body,
        status=ACTIVE_STATUS,
        session_id=session_id,
        embedding=None,  # surface-only: never embedded
        workstream_id=workstream_id,
    )
    conn.execute(
        "INSERT INTO priority_order (node_id, rank, retired_at) VALUES (?, ?, NULL)",
        (nid, locked_rank),
    )
    conn.commit()
    return {
        "id": nid,
        "ok": True,
        "active_count": len(active) + 1,
        "rank": locked_rank,
        "locked": locked_rank is not None,
        "scope": "overall" if workstream_id is None else "workstream",
        "workstream_id": workstream_id,
    }


def list_priorities(
    conn: sqlite3.Connection, *, include_retired: bool = False,
    workstream_id: int | None = None,
) -> list[dict]:
    """Active priorities in effective P1..PN order (locked at their slot,
    floating newest-first). Each row carries `effective_rank` (1-based display
    position), `rank` (the locked slot, None when floating), and `locked`.

    Pass include_retired=True for the audit/graveyard view: the active set
    followed by retired priorities, most-recently-graveyarded first, each
    carrying its `retired_at` date."""
    order = _effective_order(conn, workstream_id=workstream_id)
    active: list[dict] = []
    workstream_title = _scope_title(conn, workstream_id)
    if order:
        placeholders = ",".join("?" * len(order))
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE id IN ({placeholders})", order,
        ).fetchall()
        by_id = {r["id"]: dict(r) for r in rows}
        for i, nid in enumerate(order):
            node = by_id.get(nid)
            if node is None:
                continue
            po = _order_row(conn, nid)
            node["rank"] = po["rank"] if po else None
            node["locked"] = bool(po and po["rank"] is not None)
            node["effective_rank"] = i + 1
            node["retired_at"] = None
            node["scope"] = "overall" if node.get("workstream_id") is None else "workstream"
            node["workstream_title"] = workstream_title
            active.append(node)
    if not include_retired:
        return active

    params = [PRIORITY_KIND, RETIRED_STATUS]
    sql = (
        """
        SELECT n.*, po.retired_at AS retired_at
        FROM nodes n
        LEFT JOIN priority_order po ON po.node_id = n.id
        WHERE n.kind = ? AND n.status = ?
        """
    )
    sql += _scope_predicate(workstream_id, params)
    sql += " ORDER BY COALESCE(po.retired_at, n.updated_at) DESC, n.id DESC"
    grave_rows = conn.execute(sql, params).fetchall()
    graveyard: list[dict] = []
    for r in grave_rows:
        d = dict(r)
        d["rank"] = None
        d["locked"] = False
        d["effective_rank"] = None
        d["scope"] = "overall" if d.get("workstream_id") is None else "workstream"
        d["workstream_title"] = workstream_title
        graveyard.append(d)
    return active + graveyard


def list_for_context(
    conn: sqlite3.Connection, workstream_ids: list[int] | tuple[int, ...] | set[int],
) -> list[dict]:
    """Overall priorities plus priorities for the in-scope workstreams.

    Used by gate/session surfaces after they have already resolved which
    workstreams are active for the current context. Invalid/missing ids simply
    produce no scoped rows; focus/workstream resolution owns validation.
    """
    out = list_priorities(conn)
    seen: set[int] = set()
    for raw in workstream_ids or []:
        if raw is None:
            continue
        try:
            wid = int(raw)
        except (TypeError, ValueError):
            continue
        if wid in seen:
            continue
        seen.add(wid)
        out.extend(list_priorities(conn, workstream_id=wid))
    return out


def reorder_priority(
    conn: sqlite3.Connection, node_id: int, new_rank: int | None,
) -> dict:
    """Move an active priority to an explicit slot (locking it there), or pass
    new_rank=None to unlock it back to floating.

    Locking onto a slot held by a *floating* priority is fine (floating reflows
    around it). Locking onto a slot held by another *locked* priority returns a
    `conflict` (no write) — the agent surfaces it and asks the user."""
    node = db.get_node(conn, node_id)
    if node is None:
        return {"error": f"node {node_id} not found"}
    if node["kind"] != PRIORITY_KIND:
        return {"error": f"node {node_id} is kind={node['kind']!r}, not a priority"}
    if node["status"] != ACTIVE_STATUS:
        return {"error": f"priority {node_id} is not active (status={node['status']!r})"}

    workstream_id = node["workstream_id"]

    if _order_row(conn, node_id) is None:
        conn.execute(
            "INSERT INTO priority_order (node_id, rank, retired_at) "
            "VALUES (?, NULL, NULL)",
            (node_id,),
        )

    if new_rank is None:
        conn.execute(
            "UPDATE priority_order SET rank = NULL WHERE node_id = ?", (node_id,),
        )
        conn.commit()
        return {
            "id": node_id,
            "ok": True,
            "rank": None,
            "locked": False,
            "scope": "overall" if workstream_id is None else "workstream",
            "workstream_id": workstream_id,
        }

    n = len(_effective_order(conn, workstream_id=workstream_id))
    r = _clamp_rank(new_rank, n)
    clash = _locked_at(conn, r, workstream_id=workstream_id, exclude=node_id)
    if clash is not None:
        return {
            "conflict": "rank_locked",
            "requested_rank": r,
            "held_by": clash,
            "scope": "overall" if workstream_id is None else "workstream",
            "workstream_id": workstream_id,
            "active": _active_summary(conn, workstream_id=workstream_id),
            "hint": (
                f"rank {r} is locked by id={clash}; unlock/move it first via "
                "kb_priority_reorder, or pick another rank — ask the user which."
            ),
        }
    conn.execute(
        "UPDATE priority_order SET rank = ? WHERE node_id = ?", (r, node_id),
    )
    conn.commit()
    return {
        "id": node_id,
        "ok": True,
        "rank": r,
        "locked": True,
        "scope": "overall" if workstream_id is None else "workstream",
        "workstream_id": workstream_id,
    }


def retire_priority(conn: sqlite3.Connection, node_id: int) -> dict:
    """Retire a priority — soft-delete to 'stale' and move it to the graveyard,
    stamping the date it was retired. Reversible (the row and its audit trail
    persist); never hard-deletes. Clears the rank so the remaining active
    priorities renumber to close the gap."""
    node = db.get_node(conn, node_id)
    if node is None:
        return {"error": f"node {node_id} not found"}
    if node["kind"] != PRIORITY_KIND:
        return {"error": f"node {node_id} is kind={node['kind']!r}, not a priority"}
    if node["status"] == RETIRED_STATUS:
        existing = _order_row(conn, node_id)
        return {
            "id": node_id, "retired": True, "already": True,
            "retired_at": existing["retired_at"] if existing else None,
        }
    db.update_node(conn, node_id, status=RETIRED_STATUS)
    now = db._now()
    if _order_row(conn, node_id) is None:
        conn.execute(
            "INSERT INTO priority_order (node_id, rank, retired_at) "
            "VALUES (?, NULL, ?)",
            (node_id, now),
        )
    else:
        conn.execute(
            "UPDATE priority_order SET rank = NULL, retired_at = ? "
            "WHERE node_id = ?",
            (now, node_id),
        )
    conn.commit()
    return {"id": node_id, "retired": True, "retired_at": now}


def render_for_gate(priorities: list[dict]) -> str:
    """Block injected into the kb_gate classifier prompt. Empty string when
    there are no active priorities (caller omits the section entirely)."""
    if not priorities:
        return ""
    lines = [
        "ACTIVE PROJECT PRIORITIES (overall directives apply on EVERY build; "
        "workstream directives are additive only when that workstream is in scope):"
    ]
    overall = [p for p in priorities if p.get("workstream_id") is None]
    scoped = [p for p in priorities if p.get("workstream_id") is not None]
    for i, p in enumerate(overall, start=1):
        lines.append(f"  Overall P{i} [id={p['id']}] {p['title']}")
    grouped: dict[int, list[dict]] = {}
    titles: dict[int, str] = {}
    for p in scoped:
        wid = int(p["workstream_id"])
        grouped.setdefault(wid, []).append(p)
        if p.get("workstream_title"):
            titles[wid] = p["workstream_title"]
    for wid, rows in grouped.items():
        title = titles.get(wid)
        suffix = f" — {title}" if title else ""
        lines.append(f"  Workstream {wid}{suffix}:")
        for i, p in enumerate(rows, start=1):
            lines.append(f"    P{i} [id={p['id']}] {p['title']}")
    return "\n".join(lines)


def render_for_brief(priorities: list[dict]) -> list[str]:
    """Lines for the SessionStart brief 'Top of mind' section. Empty list when
    there are no active priorities. Locked priorities are annotated `(pinned)`,
    mirroring the focus-table convention."""
    if not priorities:
        return []
    out = ["## Top of mind (priorities)\n"]
    for i, p in enumerate(priorities, start=1):
        pin = " (pinned)" if p.get("locked") else ""
        out.append(f"{i}.{pin} {p['title']}  (id={p['id']})")
    out.append(
        "\n_Overall standing directives — weighed in every `kb_gate`; "
        "workstream priorities appear under active workstreams. "
        "Manage with `kb_priority_add` / `kb_priority_reorder` / "
        "`kb_priority_retire`._"
    )
    return out


def render_workstream_for_brief(priorities: list[dict]) -> list[str]:
    """Indented brief lines for priorities scoped to one active workstream."""
    if not priorities:
        return []
    out = ["  Workstream priorities:"]
    for i, p in enumerate(priorities, start=1):
        pin = " (pinned)" if p.get("locked") else ""
        out.append(f"  - {i}.{pin} {p['title']}  (id={p['id']})")
    return out
