from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _isolation  # noqa: F401,E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import db  # noqa: E402
import gate_report  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_gate_report_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        if conn is not None:
            conn.close()
    finally:
        shutil.rmtree(paths.project_dir(tmp), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _emit(stream: str, tmp: str, row: dict, *, log_date: date) -> None:
    log_utils.emit_event(
        stream,
        row,
        project_path=tmp,
        session_id="gate-report-test-session",
        log_date=log_date,
    )


def test_gate_report_summarizes_structural_logs_and_current_nodes():
    tmp, conn = _fresh_db()
    d = date(2026, 7, 1)
    try:
        priority = db.insert_node(
            conn,
            kind="priority",
            title="Keep first value sharp",
            body="Top of mind.",
            status="canonical",
        )
        decision = db.insert_node(
            conn,
            kind="decision",
            title="Use proof receipts",
            body="Receipts beat dashboards.",
            status="canonical",
        )
        _emit(
            "gate",
            tmp,
            {
                "query_hash": "aaa111bbb222",
                "recommendation": "MODIFY",
                "skipped": False,
                "evidence_ids": [priority, decision],
                "decision_chain": [decision],
                "load_bearing_claim_count": 3,
                "uncovered_claim_count": 1,
                "evidence_type_counts": {"kb_node": 2, "none": 1},
                "gap_type_counts": {"code_trace": 1},
            },
            log_date=d,
        )
        _emit(
            "gate",
            tmp,
            {
                "query_hash": "ccc333ddd444",
                "recommendation": "PROCEED",
                "skipped": False,
                "evidence_ids": [priority],
                "decision_chain": [priority],
                "load_bearing_claim_count": 2,
                "uncovered_claim_count": 0,
                "evidence_type_counts": {"kb_node": 2},
                "gap_type_counts": {},
            },
            log_date=d,
        )
        _emit(
            "adversary",
            tmp,
            {"verdict_before": "PROCEED", "verdict_delta": "MODIFY"},
            log_date=d,
        )
        _emit(
            "decision",
            tmp,
            {"human_action": "modify", "node_ids": [decision]},
            log_date=d,
        )
        _emit(
            "gate_outcome",
            tmp,
            {"verdict": "MODIFY", "outcome_category": "ACCEPTED"},
            log_date=d,
        )
        _emit(
            "gate_outcome",
            tmp,
            {"verdict": "PROCEED", "outcome_category": "ACCEPTED"},
            log_date=d,
        )

        report = gate_report.assemble_gate_report(
            conn,
            project_path=tmp,
            start=d,
            end=d,
            limit=5,
        )
        _assert(report["label"] == "Latch gate report", report)
        _assert(report["must_display_to_user"] is True, report)
        _assert(report["structural_only"] is True, report)
        _assert(report["used"]["gate_rows"] == 2, report["used"])
        _assert(report["used"]["adversary_rows"] == 1, report["used"])
        _assert(report["used"]["decision_rows"] == 1, report["used"])
        _assert(report["used"]["gate_outcome_rows"] == 2, report["used"])
        _assert(report["verdict_counts"] == {"MODIFY": 1, "PROCEED": 1}, report)
        _assert(report["outcome_counts"] == {"ACCEPTED": 2}, report)
        _assert(
            report["outcome_by_verdict_counts"] == {
                "MODIFY": {"ACCEPTED": 1},
                "PROCEED": {"ACCEPTED": 1},
            },
            report,
        )
        _assert(report["adversary_delta_counts"] == {"MODIFY": 1}, report)
        _assert(report["human_action_counts"] == {"modify": 1}, report)
        _assert(report["claim_signals"]["load_bearing_claims"] == 5, report)
        _assert(report["claim_signals"]["uncovered_claims"] == 1, report)
        _assert(report["claim_signals"]["evidence_type_counts"]["kb_node"] == 4, report)
        _assert(report["top_evidence_nodes"][0]["id"] == priority, report["top_evidence_nodes"])
        _assert(report["top_evidence_nodes"][0]["count"] == 2, report["top_evidence_nodes"])
        _assert(report["priority_evidence"][0]["title"] == "Keep first value sharp", report)

        text = gate_report.format_text(report)
        _assert("# Latch Gate Report" in text, text)
        _assert("Latch reviewed 2 implementation plans" in text, text)
        _assert("2 accepted outcomes, including 1 MODIFY course correction" in text, text)
        _assert("2 nudges accepted as course corrections" not in text, text)
        _assert("Latch checked 5 load-bearing claims" in text, text)
        _assert("Latch Evidence Leaderboard" in text, text)
        _assert("What Latch Kept You Focused On" in text, text)
        _assert("Why it mattered:" in text, text)
        _assert("Privacy boundary: no raw prompts" in text, text)
        _assert("Keep first value sharp" in text, text)
        _assert("Use proof receipts" in text, text)
        print("PASS gate_report_summarizes_structural_logs_and_current_nodes")
    finally:
        _cleanup(tmp, conn)


def test_gate_report_ignores_raw_query_debug_fields():
    tmp, conn = _fresh_db()
    d = date(2026, 7, 1)
    try:
        node = db.insert_node(
            conn,
            kind="decision",
            title="Safe visible title",
            body="Secret body should never appear through this report.",
            status="canonical",
        )
        _emit(
            "gate",
            tmp,
            {
                "query_hash": "aaa111bbb222",
                "query_excerpt": "SECRET RAW PROMPT",
                "uncovered_claim_texts": ["SECRET CLAIM"],
                "recommendation": "PROCEED",
                "evidence_ids": [node],
                "decision_chain": [node],
            },
            log_date=d,
        )
        report = gate_report.assemble_gate_report(conn, project_path=tmp, start=d, end=d)
        encoded = json.dumps(report)
        text = gate_report.format_text(report)
        _assert("SECRET RAW PROMPT" not in encoded + text, encoded + text)
        _assert("SECRET CLAIM" not in encoded + text, encoded + text)
        _assert("Secret body" not in encoded + text, encoded + text)
        _assert("Safe visible title" in text, text)
        print("PASS gate_report_ignores_raw_query_debug_fields")
    finally:
        _cleanup(tmp, conn)


def test_gate_report_cli_json_output():
    tmp, conn = _fresh_db()
    d = date(2026, 7, 1)
    try:
        node = db.insert_node(
            conn,
            kind="decision",
            title="CLI gate node",
            body="Body stays out of the report.",
            status="canonical",
        )
        _emit(
            "gate",
            tmp,
            {
                "query_hash": "aaa111bbb222",
                "recommendation": "PROCEED",
                "evidence_ids": [node],
                "decision_chain": [node],
            },
            log_date=d,
        )
        conn.close()
        conn = None
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rc = gate_report.main([
                "--project", tmp,
                "--start", "2026-07-01",
                "--end", "2026-07-01",
                "--format", "json",
            ])
        _assert(rc == 0, rc)
        payload = json.loads(stdout.getvalue())
        _assert(payload["used"]["gate_rows"] == 1, payload)
        _assert(payload["top_evidence_nodes"][0]["title"] == "CLI gate node", payload)
        print("PASS gate_report_cli_json_output")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    test_gate_report_summarizes_structural_logs_and_current_nodes()
    test_gate_report_ignores_raw_query_debug_fields()
    test_gate_report_cli_json_output()
    print("\nAll gate report tests pass.")
