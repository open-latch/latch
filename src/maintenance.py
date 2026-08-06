"""Weekly maintenance — ref_count decay + staging-to-canonical promotion.

Separate from compaction: compaction is per-session turnover; maintenance is
the slow-clock hygiene pass that keeps the ref_count signal meaningful and
lets earned knowledge graduate from staging to canonical.

Entry point: `run_weekly_maintenance(project_path)`. Invoked manually via the
`/latch-decay` slash command; future step will put this on a schedule.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import heal  # noqa: E402
import lifecycle_receipts  # noqa: E402
import lockfile  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402
import project_config  # noqa: E402
import tree  # noqa: E402
import workstream_automation  # noqa: E402
import workstream_detector  # noqa: E402
import workstreams  # noqa: E402


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


def run_weekly_maintenance(
    project_path: str | None = None,
    *,
    expected_binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> dict:
    """Apply decay + promotion to a project's KB. Safe to call ad-hoc; both ops
    are idempotent in the sense that running them twice in the same week just
    continues the decay curve and re-promotes nothing."""
    if paths.is_unlatched_mode(project_path):
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled(project_path):
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(
        project_path,
        expected_binding_revision=expected_binding_revision,
        expected_kb_dir=expected_kb_dir,
    )
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
        _log(
            project_path,
            result,
            expected_revision=expected_binding_revision,
        )
        return result
    finally:
        conn.close()


def run_nightly_heal(
    project_path: str | None = None,
    *,
    use_llm: bool = True,
    already_locked: bool = False,
    expected_binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> dict:
    """Nightly sweep: integrity + 0.70+ similarity contradiction pass with
    three-pass arbitration. LLM calls (when invoked) consume the daily budget."""
    if paths.is_unlatched_mode(project_path):
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled(project_path):
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(
        project_path,
        expected_binding_revision=expected_binding_revision,
        expected_kb_dir=expected_kb_dir,
    )
    try:
        result = heal.nightly_heal(
            conn,
            project_path=project_path,
            use_llm=use_llm,
            expected_binding_revision=expected_binding_revision,
            expected_kb_dir=expected_kb_dir,
        )
        try:
            result["workstream_integrity"] = workstreams.reconcile_lifecycle_integrity(
                conn, project_path=project_path, already_locked=already_locked,
            )
        except Exception:
            # Contradiction healing remains useful if lifecycle reconciliation
            # itself encounters an unexpected local failure.  Keep the error
            # structural; raw exception text can include local data.
            result["workstream_integrity"] = {
                "ok": False, "error": "internal", "repair_count": 0,
            }
        result["retrieval_events_pruned"] = db.prune_retrieval_events(
            conn, retention_days=90,
        )
        _log(
            project_path,
            {"op": "nightly_heal", **result},
            expected_revision=expected_binding_revision,
        )
        return result
    finally:
        conn.close()


def run_tree_rebuild(
    project_path: str | None = None,
    *,
    use_llm: bool = True,
    expected_binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> dict:
    """Full hierarchical rebuild — clusters leaves, promotes landmarks, builds
    one level of summary nodes. LLM calls (one per cluster) consume the budget."""
    if paths.is_unlatched_mode(project_path):
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled(project_path):
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(
        project_path,
        expected_binding_revision=expected_binding_revision,
        expected_kb_dir=expected_kb_dir,
    )
    try:
        result = tree.build_tree(conn, project_path=project_path, use_llm=use_llm)
        _log(
            project_path,
            {"op": "tree_rebuild", **result},
            expected_revision=expected_binding_revision,
        )
        return result
    finally:
        conn.close()


def run_workstream_shadow(
    project_path: str | None = None,
    *,
    already_locked: bool = False,
    expected_binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> dict:
    """Run the independently-cadenced, deterministic lifecycle detector.

    Manual calls own the shared project writer lock for baseline/restore
    reconciliation, derivation, and persistence. ``run_selfheal`` passes
    ``already_locked=True`` as an explicit fast path; same-thread lock
    ownership is also recognized safely if a composed caller omits it.
    """
    if paths.is_unlatched_mode(project_path):
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled(project_path):
        return {"ok": False, "reason": "disabled"}
    lock_context = (
        contextlib.nullcontext()
        if already_locked
        else lockfile.writer_lock(project_path)
    )
    with lock_context:
        conn = db.connect(
            project_path,
            expected_binding_revision=expected_binding_revision,
            expected_kb_dir=expected_kb_dir,
        )
        try:
            baseline_reconciliation = (
                lifecycle_receipts.reconcile_legacy_workstream_baselines(conn)
            )
            restore_reconciliation = (
                lifecycle_receipts.reconcile_orphaned_restore_ops(
                    conn, project_path=project_path,
                )
            )
            snapshot = workstream_detector.run_shadow_derivation(
                conn, project_path=project_path,
            )
            stale_tree = int(
                snapshot.get("counters", {}).get("stale_tree_signal", 0)
            )
            conn.execute(
                "INSERT INTO latch_meta(key, value) VALUES "
                "('stale_tree_signal', ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value",
                (str(stale_tree),),
            )
            conn.commit()
            result = {
                "ok": True,
                "mode": "shadow",
                "substrate_version": snapshot["substrate_version"],
                "derivation_key": snapshot["derivation_key"],
                "eligible_session_count": snapshot["eligible_session_count"],
                "candidate_count": len(snapshot.get("candidates") or []),
                "baseline_count": int(
                    baseline_reconciliation.get("baseline_count", 0)
                ),
                "orphaned_by_restore_count": int(
                    restore_reconciliation.get("orphaned_by_restore_count", 0)
                ),
                "candidate_keys": [
                    row["candidate_key"]
                    for row in snapshot.get("candidates") or []
                ],
                "orphan_pressure": snapshot.get("orphan_pressure") or {},
                "counters": snapshot.get("counters") or {},
            }
            log_utils.emit_event(
                "workstream_detector",
                result,
                project_path=project_path,
                session_id=None,
            )
            _log(
                project_path,
                {"op": "workstream_shadow", **result},
                expected_revision=expected_binding_revision,
            )
            return result
        finally:
            conn.close()


def run_workstream_governed(
    project_path: str | None = None,
    *,
    expected_binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> dict:
    """Apply only trust-ladder-eligible actions from the latest derivation.

    This entry point intentionally owns no outer compactor lock.  Each atomic
    lifecycle operation takes the shared writer lock itself, so callers that
    perform shadow derivation while holding the maintenance lock must invoke
    this function only after releasing that lock.
    """
    if paths.is_unlatched_mode(project_path):
        return {
            "ok": False,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
        }
    if paths.is_disabled(project_path):
        return {"ok": False, "reason": "disabled"}
    conn = db.connect(
        project_path,
        expected_binding_revision=expected_binding_revision,
        expected_kb_dir=expected_kb_dir,
    )
    try:
        governed = workstream_automation.run_governed(
            conn, project_path=project_path,
        )
        result = {
            "ok": not bool(governed.get("failed")),
            **governed,
        }
        structural = _governed_structural_summary(result)
        log_utils.emit_event(
            "workstream_automation",
            structural,
            project_path=project_path,
            session_id=None,
        )
        _log(
            project_path,
            structural,
            expected_revision=expected_binding_revision,
        )
        return result
    finally:
        conn.close()


def _governed_structural_summary(result: dict) -> dict:
    """Allowlist privacy-safe lifecycle telemetry.

    Planner requests/evidence and operation results can contain charters,
    reasons, node bodies, and backup paths.  Only stable ledger keys, closed
    operation/error codes, and aggregate counts may cross a logging boundary.
    """
    plans = [row for row in (result.get("plans") or []) if isinstance(row, dict)]
    applied = [
        row for row in (result.get("applied") or []) if isinstance(row, dict)
    ]
    failed = [
        row for row in (result.get("failed") or []) if isinstance(row, dict)
    ]

    def _keys(rows: list[dict], name: str) -> list[str]:
        return sorted({str(row[name]) for row in rows if row.get(name)})

    operation_codes = sorted({
        str(row["op"]).upper()
        for row in [*plans, *applied, *failed]
        if str(row.get("op") or "").upper() in db.WORKSTREAM_OPS
    })
    error_codes: set[str] = set()
    for row in failed:
        nested = row.get("result") if isinstance(row.get("result"), dict) else {}
        raw_code = row.get("error_code") or nested.get("error_code") or row.get("error")
        code = str(raw_code or "internal")
        error_codes.add(code if code in db.WORKSTREAM_OP_ERROR_CODES else "internal")

    return {
        "op": "workstream_governed",
        "ok": not bool(failed),
        "mode": "governed",
        "plan_count": len(plans),
        "eligible_count": sum(bool(row.get("eligible")) for row in plans),
        "applied_count": len(applied),
        "failed_count": len(failed),
        "suggestion_count": int(result.get("suggestion_count") or 0),
        "candidate_keys": _keys([*plans, *applied, *failed], "candidate_key"),
        "applied_op_keys": _keys(applied, "op_key"),
        "failed_op_keys": _keys(failed, "op_key"),
        "operation_codes": operation_codes,
        "error_codes": sorted(error_codes),
    }


def run_workstream_cycle(
    project_path: str | None = None,
    *,
    expected_binding_revision: str | None = None,
    expected_kb_dir: str | None = None,
) -> dict:
    """Derive current candidates, then run the governed trust ladder."""
    shadow = run_workstream_shadow(
        project_path,
        expected_binding_revision=expected_binding_revision,
        expected_kb_dir=expected_kb_dir,
    )
    if not shadow.get("ok"):
        return {"ok": False, "shadow": shadow, "governed": None}
    governed = run_workstream_governed(
        project_path,
        expected_binding_revision=expected_binding_revision,
        expected_kb_dir=expected_kb_dir,
    )
    return {
        "ok": bool(governed.get("ok")),
        "shadow": shadow,
        "governed": governed,
    }


def _log(
    project_path: str | None,
    result: dict,
    *,
    expected_revision: str | None = None,
) -> None:
    try:
        lockfile.append_project_log(
            project_path,
            "maintenance.log",
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"{json.dumps(result)}\n",
            expected_revision=expected_revision,
        )
    except Exception:
        pass


def _cli_binding_snapshot(
    project_path: str | None,
    *,
    session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Turn a shell caller's current-session receipt into exact DB authority."""
    raw_session = (
        session_id
        if session_id is not None
        else project_config.current_agent_session_id()
    )
    sid = (raw_session or "").strip()
    if not sid:
        if project_config.is_agent_context():
            raise project_config.ProjectConfigError(
                "maintenance requires the exact current agent session id"
            )
        return None, None
    project = str(project_path or os.getcwd())
    target = project_config.resolve(project)
    revision = project_config.current_session_revision(
        project,
        sid,
        resolved_target=target,
    )
    kb_dir = project_config.validated_bound_kb_dir(target)
    if revision is None or kb_dir is None:
        raise project_config.ProjectConfigError(
            "maintenance session belongs to another or older Latch scope"
        )
    return revision, str(kb_dir)


def main(argv: list[str] | None = None) -> int:
    # python maintenance.py [weekly|nightly|tree|workstreams|workstream-shadow]
    #                       [project_path] [--session-id <current-id>]
    args = list(sys.argv[1:] if argv is None else argv)
    explicit_session = None
    if "--session-id" in args:
        index = args.index("--session-id")
        if index + 1 >= len(args):
            print(json.dumps({
                "ok": False,
                "reason": "invalid_arguments",
                "error": "--session-id requires a value",
            }))
            return 2
        explicit_session = args[index + 1]
        del args[index:index + 2]
    op = args[0] if args else "weekly"
    project = args[1] if len(args) > 1 else None
    if len(args) > 2:
        print(json.dumps({
            "ok": False,
            "reason": "invalid_arguments",
            "error": "too many arguments",
        }))
        return 2
    supported = {
        "weekly", "nightly", "tree", "workstreams", "workstream-shadow",
    }
    if op not in supported:
        print(
            f"unknown op {op!r} — use 'weekly' | 'nightly' | 'tree' | "
            "'workstreams' | 'workstream-shadow'",
            file=sys.stderr,
        )
        return 2
    if paths.is_unlatched_mode(project) or paths.is_disabled(project):
        revision, kb_dir = None, None
    else:
        try:
            revision, kb_dir = _cli_binding_snapshot(
                project,
                session_id=explicit_session,
            )
        except project_config.ProjectConfigError as exc:
            print(json.dumps({
                "ok": False,
                "reason": "stale_session_binding",
                "error": str(exc),
            }))
            return 1
    kwargs = {
        "expected_binding_revision": revision,
        "expected_kb_dir": kb_dir,
    }
    if op == "weekly":
        result = run_weekly_maintenance(project, **kwargs)
    elif op == "nightly":
        result = run_nightly_heal(project, **kwargs)
    elif op == "tree":
        result = run_tree_rebuild(project, **kwargs)
    elif op == "workstreams":
        result = run_workstream_cycle(project, **kwargs)
    elif op == "workstream-shadow":
        result = run_workstream_shadow(project, **kwargs)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
