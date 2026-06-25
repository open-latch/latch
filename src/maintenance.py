"""Weekly maintenance — ref_count decay + staging-to-canonical promotion.

Separate from compaction: compaction is per-session turnover; maintenance is
the slow-clock hygiene pass that keeps the ref_count signal meaningful and
lets earned knowledge graduate from staging to canonical.

Entry point: `run_weekly_maintenance(project_path)`. Invoked manually via the
`/kb-decay` slash command; future step will put this on a schedule.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import heal  # noqa: E402
import paths  # noqa: E402
import tree  # noqa: E402


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


DECAY_FACTOR = 0.9       # weekly multiplicative decay on ref_count
DECAY_FLOOR = 1          # once referenced, a node survives decay indefinitely
PROMOTION_THRESHOLD = 3  # ref_count at which staging promotes to canonical


def run_weekly_maintenance(project_path: str | None = None) -> dict:
    """Apply decay + promotion to a project's KB. Safe to call ad-hoc; both ops
    are idempotent in the sense that running them twice in the same week just
    continues the decay curve and re-promotes nothing."""
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(project_path)
    try:
        if os.environ.get("CLAUDE_KB_DEBUG_LOG"):
            before = conn.execute(
                "SELECT id, kind, title, ref_count, status FROM nodes "
                "WHERE ref_count > 0 OR status = 'staging'"
            ).fetchall()
            before_map = {r["id"]: dict(r) for r in before}
            _debug(f"weekly start: {len(before_map)} candidate rows "
                   f"(ref_count>0 or status=staging), "
                   f"factor={DECAY_FACTOR} floor={DECAY_FLOOR} "
                   f"promo_threshold={PROMOTION_THRESHOLD}")
        else:
            before_map = None

        decayed = db.apply_ref_count_decay(conn, factor=DECAY_FACTOR, floor=DECAY_FLOOR)
        promoted = db.promote_by_ref_count(conn, min_ref_count=PROMOTION_THRESHOLD)

        if before_map is not None:
            after = conn.execute(
                "SELECT id, ref_count, status FROM nodes WHERE id IN ({})".format(
                    ",".join("?" for _ in before_map)
                ),
                list(before_map.keys()),
            ).fetchall() if before_map else []
            after_map = {r["id"]: dict(r) for r in after}
            for nid, b in before_map.items():
                a = after_map.get(nid, {})
                rc_before = b.get("ref_count", 0)
                rc_after = a.get("ref_count", rc_before)
                st_before = b.get("status", "")
                st_after = a.get("status", st_before)
                if rc_before != rc_after or st_before != st_after:
                    _debug(f"  id={nid} kind={b['kind']!r} title={b['title']!r}: "
                           f"ref_count {rc_before}->{rc_after}, "
                           f"status {st_before}->{st_after}")
            _debug(f"weekly complete: decayed_rows={decayed} "
                   f"promoted_ids={promoted}")

        result = {
            "ok": True,
            "decayed_rows": decayed,
            "promoted_ids": promoted,
            "promoted_count": len(promoted),
            "factor": DECAY_FACTOR,
            "floor": DECAY_FLOOR,
            "threshold": PROMOTION_THRESHOLD,
        }
        _log(project_path, result)
        return result
    finally:
        conn.close()


def run_nightly_heal(project_path: str | None = None, *, use_llm: bool = True) -> dict:
    """Nightly sweep: integrity + 0.70+ similarity contradiction pass with
    three-pass arbitration. LLM calls (when invoked) consume the daily budget."""
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(project_path)
    try:
        result = heal.nightly_heal(conn, project_path=project_path, use_llm=use_llm)
        _log(project_path, {"op": "nightly_heal", **result})
        return result
    finally:
        conn.close()


def run_tree_rebuild(project_path: str | None = None, *, use_llm: bool = True) -> dict:
    """Full hierarchical rebuild — clusters leaves, promotes landmarks, builds
    one level of summary nodes. LLM calls (one per cluster) consume the budget."""
    if paths.is_disabled():
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(project_path)
    try:
        result = tree.build_tree(conn, project_path=project_path, use_llm=use_llm)
        _log(project_path, {"op": "tree_rebuild", **result})
        return result
    finally:
        conn.close()


def _log(project_path: str | None, result: dict) -> None:
    log_path = paths.KB_ROOT / "maintenance.log"
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(
                f"[{datetime.now().isoformat(timespec='seconds')}] "
                f"project={project_path} {json.dumps(result)}\n"
            )
    except Exception:
        pass


if __name__ == "__main__":
    # python maintenance.py [weekly|nightly|tree] [project_path]
    argv = sys.argv[1:]
    op = argv[0] if argv else "weekly"
    project = argv[1] if len(argv) > 1 else None
    if op == "weekly":
        print(json.dumps(run_weekly_maintenance(project), indent=2))
    elif op == "nightly":
        print(json.dumps(run_nightly_heal(project), indent=2))
    elif op == "tree":
        print(json.dumps(run_tree_rebuild(project), indent=2))
    else:
        print(f"unknown op {op!r} — use 'weekly' | 'nightly' | 'tree'", file=sys.stderr)
        sys.exit(2)
