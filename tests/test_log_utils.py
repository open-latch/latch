"""Tests for src/log_utils.py — shared JSONL emission + daily rotation
+ 30d-hot / 1y-warm retention. Spec: KB id=1091."""
from __future__ import annotations

import gzip
import io
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import log_utils  # noqa: E402
import paths      # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_project():
    tmp = tempfile.mkdtemp(prefix="kb_logutils_test_")
    return tmp


def _cleanup(tmp):
    proj_dir = paths.project_dir(tmp)
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- today_log_path ----------

def test_today_log_path_format():
    """Path is <project_dir>/<stream>-<YYYY-MM-DD>.log."""
    tmp = _fresh_project()
    try:
        path = log_utils.today_log_path("heal", tmp)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _assert(path.name == f"heal-{today}.log",
                f"unexpected name: {path.name}")
        _assert(path.parent == paths.project_dir(tmp),
                f"unexpected parent: {path.parent}")
        print("PASS today_log_path_format")
    finally:
        _cleanup(tmp)


# ---------- emit_event ----------

def test_emit_event_writes_single_jsonl_line():
    tmp = _fresh_project()
    try:
        log_utils.emit_event(
            "heal",
            {"inserted_node_id": 1, "similarity": 0.92},
            project_path=tmp,
            session_id="sess-abc",
        )
        path = log_utils.today_log_path("heal", tmp)
        _assert(path.exists(), f"log file not created at {path}")
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        _assert(len(lines) == 1, f"expected 1 line, got {len(lines)}")
        row = json.loads(lines[0])
        _assert(row["inserted_node_id"] == 1, row)
        _assert(row["similarity"] == 0.92, row)
        print("PASS emit_event_writes_single_jsonl_line")
    finally:
        _cleanup(tmp)


def test_emit_event_common_header_fields_present():
    """ts, project, session_id, event_type — KB id=1091 §2."""
    tmp = _fresh_project()
    try:
        log_utils.emit_event(
            "heal", {"x": 1}, project_path=tmp, session_id="sess-xyz",
        )
        path = log_utils.today_log_path("heal", tmp)
        row = json.loads(path.read_text(encoding="utf-8").strip())
        for key in ("ts", "project", "session_id", "event_type"):
            _assert(key in row, f"missing header field {key!r}: {row}")
        _assert(row["event_type"] == "heal", row)
        _assert(row["session_id"] == "sess-xyz", row)
        _assert(row["project"] == paths.sanitize_cwd(tmp),
                f"project basename mismatch: {row['project']}")
        # ts is ISO8601 with Z suffix
        _assert(row["ts"].endswith("Z") and "T" in row["ts"],
                f"unexpected ts format: {row['ts']}")
        print("PASS emit_event_common_header_fields_present")
    finally:
        _cleanup(tmp)


def test_emit_event_project_none_falls_back_to_cwd():
    """When project_path is None, the header `project` must match the cwd-
    derived log dir (not the literal "unknown"). Regression for the on-insert
    heal `project="unknown"` bug — _project_basename must mirror
    _project_log_dir's os.getcwd() fallback. KB id=1108 §2."""
    tmp = _fresh_project()
    prev_cwd = os.getcwd()
    try:
        os.chdir(tmp)
        log_utils.emit_event("heal", {"x": 1}, project_path=None,
                             session_id="sess-none")
        # Row lands in the cwd-derived project dir...
        path = log_utils.today_log_path("heal", None)
        row = json.loads(path.read_text(encoding="utf-8").strip())
        # ...and the header `project` agrees with that dir, not "unknown".
        _assert(row["project"] != "unknown",
                f'project should not be literal "unknown": {row}')
        _assert(row["project"] == paths.sanitize_cwd(tmp),
                f"project basename should match cwd: {row['project']}")
        print("PASS emit_event_project_none_falls_back_to_cwd")
    finally:
        os.chdir(prev_cwd)
        _cleanup(tmp)


def test_emit_event_header_overrides_row_keys():
    """If the caller's row tries to set ts/project/session_id/event_type,
    the header wins so schema invariants hold."""
    tmp = _fresh_project()
    try:
        log_utils.emit_event(
            "heal",
            {
                "ts": "FAKE-TS",
                "project": "FAKE-PROJECT",
                "session_id": "FAKE-SID",
                "event_type": "FAKE-EVENT",
                "real_field": 42,
            },
            project_path=tmp,
            session_id="real-sid",
        )
        row = json.loads(
            log_utils.today_log_path("heal", tmp)
            .read_text(encoding="utf-8").strip()
        )
        _assert(row["ts"] != "FAKE-TS", row)
        _assert(row["project"] != "FAKE-PROJECT", row)
        _assert(row["session_id"] == "real-sid", row)
        _assert(row["event_type"] == "heal", row)
        _assert(row["real_field"] == 42, row)
        print("PASS emit_event_header_overrides_row_keys")
    finally:
        _cleanup(tmp)


def test_emit_event_appends_across_calls():
    tmp = _fresh_project()
    try:
        for i in range(3):
            log_utils.emit_event(
                "heal", {"idx": i}, project_path=tmp, session_id="s",
            )
        path = log_utils.today_log_path("heal", tmp)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        _assert(len(lines) == 3, f"expected 3 lines, got {len(lines)}")
        idxs = [json.loads(l)["idx"] for l in lines]
        _assert(idxs == [0, 1, 2], idxs)
        print("PASS emit_event_appends_across_calls")
    finally:
        _cleanup(tmp)


def test_emit_event_swallows_open_failure():
    """If the underlying file write blows up, the caller MUST NOT see an
    exception — log failure cannot break the verdict path."""
    tmp = _fresh_project()
    try:
        # Replace Path.open globally to always raise, then emit.
        original_open = Path.open

        def _raise(self, *a, **kw):
            raise IOError("simulated disk failure")

        Path.open = _raise
        try:
            # Must NOT raise.
            log_utils.emit_event(
                "heal", {"x": 1}, project_path=tmp, session_id=None,
            )
        finally:
            Path.open = original_open
        print("PASS emit_event_swallows_open_failure")
    finally:
        _cleanup(tmp)


def test_emit_event_session_id_null_is_serialized():
    """session_id=None must serialize as JSON null, not be dropped."""
    tmp = _fresh_project()
    try:
        log_utils.emit_event("heal", {"x": 1}, project_path=tmp, session_id=None)
        row = json.loads(
            log_utils.today_log_path("heal", tmp)
            .read_text(encoding="utf-8").strip()
        )
        _assert("session_id" in row, f"session_id missing: {row}")
        _assert(row["session_id"] is None, f"expected null: {row['session_id']}")
        print("PASS emit_event_session_id_null_is_serialized")
    finally:
        _cleanup(tmp)


def test_emit_event_creates_project_dir_lazily():
    """If the project dir doesn't exist yet, emit_event creates it."""
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        # Ensure the dir is missing at the start.
        if proj_dir.exists():
            shutil.rmtree(proj_dir, ignore_errors=True)
        log_utils.emit_event("heal", {"x": 1}, project_path=tmp, session_id=None)
        _assert(proj_dir.is_dir(), f"project_dir not created: {proj_dir}")
        _assert(log_utils.today_log_path("heal", tmp).exists(), "log not created")
        print("PASS emit_event_creates_project_dir_lazily")
    finally:
        _cleanup(tmp)


# ---------- maintain_log_retention ----------

def _write_dated_log(proj_dir: Path, stream: str, date_str: str,
                    *, gz: bool = False) -> Path:
    proj_dir.mkdir(parents=True, exist_ok=True)
    name = f"{stream}-{date_str}.log"
    if gz:
        name += ".gz"
    path = proj_dir / name
    if gz:
        with gzip.open(path, "wb") as f:
            f.write(b'{"x": 1}\n')
    else:
        path.write_text('{"x": 1}\n', encoding="utf-8")
    return path


def _date_str_n_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def test_retention_gzips_files_older_than_30_days():
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        old = _write_dated_log(proj_dir, "heal", _date_str_n_days_ago(40))
        recent = _write_dated_log(proj_dir, "heal", _date_str_n_days_ago(5))
        out = log_utils.maintain_log_retention(tmp)
        _assert(out["gzipped"] == 1, out)
        _assert(not old.exists(), f"old uncompressed should be removed: {old}")
        gz = old.with_name(old.name + ".gz")
        _assert(gz.exists(), f"gzipped file missing: {gz}")
        _assert(recent.exists(), f"recent file should be untouched: {recent}")
        # Gzipped contents are readable.
        with gzip.open(gz, "rb") as f:
            content = f.read()
        _assert(b'"x": 1' in content, content)
        print("PASS retention_gzips_files_older_than_30_days")
    finally:
        _cleanup(tmp)


def test_retention_deletes_gz_older_than_1_year():
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        ancient = _write_dated_log(
            proj_dir, "heal", _date_str_n_days_ago(400), gz=True,
        )
        recent_gz = _write_dated_log(
            proj_dir, "heal", _date_str_n_days_ago(60), gz=True,
        )
        out = log_utils.maintain_log_retention(tmp)
        _assert(out["deleted"] == 1, out)
        _assert(not ancient.exists(),
                f"year-old gz should be deleted: {ancient}")
        _assert(recent_gz.exists(),
                f"60-day-old gz should be kept: {recent_gz}")
        print("PASS retention_deletes_gz_older_than_1_year")
    finally:
        _cleanup(tmp)


def test_retention_never_touches_today():
    """Today's file must be untouched even if a stream-name regex accidentally
    matches a future-dated file."""
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        today = _write_dated_log(
            proj_dir, "heal", _date_str_n_days_ago(0),
        )
        out = log_utils.maintain_log_retention(tmp)
        _assert(out["gzipped"] == 0 and out["deleted"] == 0, out)
        _assert(today.exists(), f"today's file removed: {today}")
        print("PASS retention_never_touches_today")
    finally:
        _cleanup(tmp)


def test_retention_skips_non_matching_files():
    """Files that don't match the daily-rotation regex are ignored — keeps
    the legacy single-file retrieve.log / gate.log archives in place."""
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        proj_dir.mkdir(parents=True, exist_ok=True)
        legacy = proj_dir / "retrieve.log"
        legacy.write_text('{"legacy": true}\n', encoding="utf-8")
        other = proj_dir / "kb.db"
        other.write_text("not a log", encoding="utf-8")
        out = log_utils.maintain_log_retention(tmp)
        _assert(out["gzipped"] == 0 and out["deleted"] == 0, out)
        _assert(legacy.exists(), "legacy single-file log removed")
        _assert(other.exists(), "non-log file removed")
        print("PASS retention_skips_non_matching_files")
    finally:
        _cleanup(tmp)


def test_retention_handles_missing_project_dir():
    """Calling on a project that has no log dir is a no-op, not an error."""
    tmp = _fresh_project()
    try:
        # Don't create the project dir.
        proj_dir = paths.project_dir(tmp)
        if proj_dir.exists():
            shutil.rmtree(proj_dir, ignore_errors=True)
        out = log_utils.maintain_log_retention(tmp)
        _assert(out == {"gzipped": 0, "deleted": 0, "skipped": 0}, out)
        print("PASS retention_handles_missing_project_dir")
    finally:
        _cleanup(tmp)


# ---------- read_log_range ----------

def _write_dated_log_with_rows(
    proj_dir: Path, stream: str, date_str: str, rows: list[dict],
    *, gz: bool = False,
) -> Path:
    proj_dir.mkdir(parents=True, exist_ok=True)
    name = f"{stream}-{date_str}.log" + (".gz" if gz else "")
    path = proj_dir / name
    payload = "".join(json.dumps(r) + "\n" for r in rows)
    if gz:
        with gzip.open(path, "wb") as f:
            f.write(payload.encode("utf-8"))
    else:
        path.write_text(payload, encoding="utf-8")
    return path


def test_read_log_range_iterates_dates_in_order():
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-25", [{"day": 25, "i": 0}, {"day": 25, "i": 1}],
        )
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-26", [{"day": 26, "i": 0}],
        )
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-27", [{"day": 27, "i": 0}, {"day": 27, "i": 1}],
        )
        rows = list(log_utils.read_log_range(
            "gate", date(2026, 5, 25), date(2026, 5, 27), tmp,
        ))
        _assert(len(rows) == 5, f"expected 5 rows, got {len(rows)}")
        _assert([r["day"] for r in rows] == [25, 25, 26, 27, 27],
                f"unexpected day order: {[r['day'] for r in rows]}")
        print("PASS read_log_range_iterates_dates_in_order")
    finally:
        _cleanup(tmp)


def test_read_log_range_reads_gz_transparently():
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-04-01", [{"day": 1, "g": True}], gz=True,
        )
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-04-02", [{"day": 2, "g": False}],
        )
        rows = list(log_utils.read_log_range(
            "gate", date(2026, 4, 1), date(2026, 4, 2), tmp,
        ))
        _assert(len(rows) == 2, rows)
        _assert(rows[0]["day"] == 1 and rows[0]["g"] is True, rows[0])
        _assert(rows[1]["day"] == 2 and rows[1]["g"] is False, rows[1])
        print("PASS read_log_range_reads_gz_transparently")
    finally:
        _cleanup(tmp)


def test_read_log_range_skips_missing_dates():
    """Gaps in the date range are silently skipped — no error, no
    placeholder rows. Mirrors emit_event's lazy create."""
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-25", [{"day": 25}],
        )
        # Skip 26.
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-27", [{"day": 27}],
        )
        rows = list(log_utils.read_log_range(
            "gate", date(2026, 5, 25), date(2026, 5, 27), tmp,
        ))
        _assert([r["day"] for r in rows] == [25, 27],
                f"unexpected days: {[r['day'] for r in rows]}")
        print("PASS read_log_range_skips_missing_dates")
    finally:
        _cleanup(tmp)


def test_read_log_range_skips_malformed_json_lines():
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        proj_dir.mkdir(parents=True, exist_ok=True)
        path = proj_dir / "gate-2026-05-25.log"
        path.write_text(
            '{"good": 1}\n'
            'not valid json\n'
            '\n'
            '{"good": 2}\n',
            encoding="utf-8",
        )
        rows = list(log_utils.read_log_range(
            "gate", date(2026, 5, 25), date(2026, 5, 25), tmp,
        ))
        _assert([r["good"] for r in rows] == [1, 2],
                f"unexpected rows: {rows}")
        print("PASS read_log_range_skips_malformed_json_lines")
    finally:
        _cleanup(tmp)


def test_read_log_range_empty_when_no_files():
    """Range with no files yields nothing; no error."""
    tmp = _fresh_project()
    try:
        rows = list(log_utils.read_log_range(
            "gate", date(2026, 5, 25), date(2026, 5, 27), tmp,
        ))
        _assert(rows == [], f"expected empty, got {rows}")
        print("PASS read_log_range_empty_when_no_files")
    finally:
        _cleanup(tmp)


def test_read_log_range_single_day_inclusive():
    """start == end reads exactly one day's file."""
    tmp = _fresh_project()
    try:
        proj_dir = paths.project_dir(tmp)
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-25", [{"i": 0}, {"i": 1}, {"i": 2}],
        )
        _write_dated_log_with_rows(
            proj_dir, "gate", "2026-05-26", [{"i": 99}],
        )
        rows = list(log_utils.read_log_range(
            "gate", date(2026, 5, 25), date(2026, 5, 25), tmp,
        ))
        _assert([r["i"] for r in rows] == [0, 1, 2],
                f"unexpected rows: {rows}")
        print("PASS read_log_range_single_day_inclusive")
    finally:
        _cleanup(tmp)


if __name__ == "__main__":
    test_today_log_path_format()
    test_emit_event_writes_single_jsonl_line()
    test_emit_event_common_header_fields_present()
    test_emit_event_header_overrides_row_keys()
    test_emit_event_appends_across_calls()
    test_emit_event_swallows_open_failure()
    test_emit_event_session_id_null_is_serialized()
    test_emit_event_creates_project_dir_lazily()
    test_retention_gzips_files_older_than_30_days()
    test_retention_deletes_gz_older_than_1_year()
    test_retention_never_touches_today()
    test_retention_skips_non_matching_files()
    test_retention_handles_missing_project_dir()
    test_read_log_range_iterates_dates_in_order()
    test_read_log_range_reads_gz_transparently()
    test_read_log_range_skips_missing_dates()
    test_read_log_range_skips_malformed_json_lines()
    test_read_log_range_empty_when_no_files()
    test_read_log_range_single_day_inclusive()
    print("\nAll log_utils tests pass.")
