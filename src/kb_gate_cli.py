"""CLI backing the /latch-gate slash command.

Usage (called by `bin/run_latch_gate.sh`; legacy: `bin/run_kb_gate.sh`):

    python kb_gate_cli.py <project_cwd> <request...>

The request is the full user query, joined from argv[2:]. Always emits a
single JSON object with the run_gate() return shape (verdict, evidence,
chains, request). Exit 0 on success, 2 on argv error.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import paths
import project_config


def _emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    return 0


def _unlatched_verdict() -> dict:
    message = paths.UNLATCHED_MESSAGE
    reason = message.strip().rstrip(".")
    return {
        "recommendation": None,
        "summary": f"Gate did not produce a recommendation: {reason}.",
        "decision_chain": [],
        "abandoned_paths": [],
        "active_constraints": [],
        "current_direction": [],
        "risk_if_proceed": "",
        "better_next_action": "",
        "evidence_nodes": [],
        "load_bearing_claims": [],
        "uncovered_claims": [],
        "error": reason,
        "reason": "unlatched",
        "message": message,
        "skipped": True,
    }


def _unlatched_findings(verdict: dict) -> dict:
    return {
        "label": "Latch gate findings",
        "must_display_to_user": True,
        "source": "latch_gate",
        "recommendation": None,
        "summary": verdict["summary"],
        "risk_if_proceed": "",
        "better_next_action": "",
        "decision_chain": [],
        "abandoned_paths": [],
        "active_constraints": [],
        "current_direction": [],
        "evidence_nodes": [],
        "load_bearing_claims": [],
        "uncovered_claims": [],
        "receipt": {
            "summary": (
                "Latch gate was skipped because Latch is currently UNLATCHED. "
                "Run /latch to re-latch. If LATCH_UNLATCHED is set, unset it too."
            ),
            "source": "latch_gate",
            "used": {
                "decision_chain": 0,
                "abandoned_paths": 0,
                "active_constraints": 0,
                "current_direction": 0,
                "evidence_nodes": 0,
                "load_bearing_claims": 0,
                "uncovered_claims": 0,
            },
            "authority": "No KB evidence was read while latch was unlatched.",
        },
        "why_it_matters": (
            "Latch gate was skipped because Latch is currently UNLATCHED. "
            "Run /latch to re-latch. If LATCH_UNLATCHED is set, unset it too."
        ),
    }


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        sys.stdout.write(json.dumps({
            "ok": False, "error": "usage: kb_gate_cli.py <cwd> <request...>"
        }))
        sys.stdout.write("\n")
        return 2
    cwd = argv[1]
    request = " ".join(argv[2:]).strip()
    if not request:
        return _emit({"ok": False, "error": "empty request"})
    if paths.is_unlatched_mode(cwd):
        verdict = _unlatched_verdict()
        return _emit({
            "ok": False,
            "request": request,
            "reason": "unlatched",
            "message": paths.UNLATCHED_MESSAGE,
            "verdict": verdict,
            "findings": _unlatched_findings(verdict),
            "evidence": [],
            "chain_summary": {
                "seed_count": 0,
                "seed_ids": [],
                "reachable_ids": [],
            },
        })

    import db
    import gate

    expected_revision = None
    expected_kb_dir = None
    sid = project_config.current_agent_session_id()
    if sid is not None:
        try:
            target = project_config.resolve(cwd)
            expected_revision = project_config.current_session_revision(
                cwd,
                sid,
                resolved_target=target,
            )
            selected = project_config.validated_bound_kb_dir(target)
        except project_config.ProjectConfigError as exc:
            return _emit({
                "ok": False,
                "reason": "stale_session_binding",
                "error": str(exc),
            })
        if expected_revision is None or selected is None:
            return _emit({
                "ok": False,
                "reason": "stale_session_binding",
                "error": "gate session belongs to another or older Latch scope",
            })
        expected_kb_dir = str(selected)
    elif project_config.is_agent_context():
        return _emit({
            "ok": False,
            "reason": "stale_session_binding",
            "error": "gate requires the exact current agent session id",
        })

    conn = db.connect(
        cwd,
        expected_binding_revision=expected_revision,
        expected_kb_dir=expected_kb_dir,
    )
    try:
        out = gate.run_gate(conn, request, project_path=cwd)
    finally:
        conn.close()

    return _emit({"ok": True, **out})


if __name__ == "__main__":
    sys.exit(main(sys.argv))
