"""Aggregate-only acceptance test for the predicate coverage entry.

The fixture is deliberately a minimal synthetic SQLite vault.  Its row text is
unique to this test and must never appear in the report.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"


def _coverage_module():
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module("predicate_coverage")
    finally:
        sys.path.remove(str(_SRC))


def _synthetic_vault(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE rejected_path (
                id INTEGER PRIMARY KEY,
                node_id INTEGER NOT NULL,
                option TEXT NOT NULL,
                reason TEXT NOT NULL,
                scope_predicate TEXT,
                source TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO rejected_path
                (id, node_id, option, reason, scope_predicate, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    1,
                    101,
                    "PRIVATE_OPTION_ALPHA",
                    "PRIVATE_REASON_ALPHA",
                    "file:src/PRIVATE_PATH_ALPHA.py",
                    "declared",
                ),
                (
                    2,
                    102,
                    "PRIVATE_OPTION_BRAVO",
                    "PRIVATE_REASON_BRAVO",
                    "package:synthetic.widgets",
                    "backfill",
                ),
                (
                    3,
                    103,
                    "PRIVATE_OPTION_CHARLIE",
                    "PRIVATE_REASON_CHARLIE",
                    "feature:PRIVATE_FEATURE_CHARLIE",
                    "backfill",
                ),
                (
                    4,
                    104,
                    "PRIVATE_OPTION_DELTA",
                    "PRIVATE_REASON_DELTA",
                    "positioning",
                    "declared",
                ),
                (
                    5,
                    105,
                    "PRIVATE_OPTION_ECHO",
                    "PRIVATE_REASON_ECHO",
                    None,
                    "backfill",
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_aggregates_only_and_total_accounting(tmp_path):
    predicate_coverage = _coverage_module()
    vault_path = tmp_path / "synthetic-vault.sqlite3"
    _synthetic_vault(vault_path)

    read_only = predicate_coverage.open_read_only(vault_path)
    try:
        assert predicate_coverage.verify_read_only(read_only) is True
        with pytest.raises(sqlite3.OperationalError, match="read-only|readonly"):
            read_only.execute(
                """
                INSERT INTO rejected_path
                    (node_id, option, reason, scope_predicate, source)
                VALUES (999, 'write-probe', 'write-probe', 'file:probe', 'declared')
                """
            )
    finally:
        read_only.close()

    report = predicate_coverage.coverage_report(vault_path)
    assert set(report) == {
        "engine",
        "total",
        "per_prefix",
        "source_counts",
        "compiled",
        "uncompilable",
        "compiled_fraction",
        "read_only_verified",
    }
    assert report["engine"] == "predicate-v1"
    assert report["total"] == 5
    assert report["compiled"] == 2
    assert report["uncompilable"] == 3
    assert report["compiled"] + report["uncompilable"] == report["total"]
    assert report["per_prefix"] == {
        "<null>": 1,
        "feature": 1,
        "file": 1,
        "package": 1,
        "positioning": 1,
    }
    assert sum(report["per_prefix"].values()) == report["total"]
    assert report["source_counts"] == {"backfill": 3, "declared": 2}
    assert sum(report["source_counts"].values()) == report["total"]
    assert report["compiled_fraction"] == pytest.approx(2 / 5)
    assert report["read_only_verified"] is True

    proc = subprocess.run(
        ["bash", str(_ROOT / "bin" / "predicate_coverage.sh"), str(vault_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(proc.stdout) == report
    assert proc.stderr == ""

    serialized = json.dumps(report, sort_keys=True)
    for private_text in (
        "PRIVATE_OPTION",
        "PRIVATE_REASON",
        "PRIVATE_PATH",
        "PRIVATE_FEATURE",
    ):
        assert private_text not in serialized
    for forbidden_row_field in (
        '"scope_predicate":',
        '"option":',
        '"reason":',
        '"predicate":',
    ):
        assert forbidden_row_field not in serialized
