"""Shared JSONL logging helpers for RL judgment-data streams.

Conventions are locked in KB id=1091:

* One file per concern, daily rotation:
  ``<project_dir>/<stream>-<YYYY-MM-DD>.log``
* Common header on every row: ``ts``, ``project``, ``session_id``, ``event_type``.
* Hot retention 30 days uncompressed, then gzipped in place for the remaining
  335 days, then deleted at 1 year. ``maintain_log_retention`` is the lazy
  rotator (called from nightly heal).
* Structural-only invariant: callers are responsible for keeping node titles,
  bodies, and raw prompt text OUT of `row`. This module does not redact.
* All emission failures are swallowed — log writes MUST NOT break the caller.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

import paths


HOT_RETENTION_DAYS = 30
COLD_RETENTION_DAYS = 365

_DAILY_LOG_RE = re.compile(
    r"^(?P<stream>[a-z_]+)-(?P<date>\d{4}-\d{2}-\d{2})\.log(?P<gz>\.gz)?$"
)


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    """ISO8601 UTC with millisecond precision and trailing Z."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _project_basename(project_path: str | os.PathLike | None) -> str:
    """Sanitized basename matching `project_dir`'s naming convention.

    Mirrors `_project_log_dir` / `paths.project_dir`: when project_path is
    None, fall back to the cwd so the header `project` field agrees with the
    directory the row is actually written into (id=1108 common-header
    invariant). The old literal "unknown" disagreed with the cwd-derived log
    dir — on-insert heal rows landed in the correct project dir but the row
    said project="unknown".
    """
    if project_path is None:
        return paths.sanitize_cwd(os.getcwd())
    return paths.sanitize_cwd(project_path)


def _project_log_dir(project_path: str | os.PathLike | None) -> Path:
    if project_path is None:
        return paths.project_dir(os.getcwd())
    return paths.project_dir(project_path)


def today_log_path(
    stream: str, project_path: str | os.PathLike | None = None,
) -> Path:
    """Return ``<project_dir>/<stream>-<today_utc>.log``."""
    return _project_log_dir(project_path) / f"{stream}-{_today_utc_date()}.log"


def read_log_range(
    stream: str,
    start: date,
    end: date,
    project_path: str | os.PathLike | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield JSON rows from daily files for ``stream`` over ``[start, end]``
    inclusive.

    Reads ``.log.gz`` transparently for days past hot retention. Missing files
    are skipped silently. Malformed JSON lines are skipped (best-effort
    offline replay; the live emitter swallows write failures, so we mirror
    that on the read side).
    """
    log_dir = _project_log_dir(project_path)
    cur = start
    while cur <= end:
        date_str = cur.strftime("%Y-%m-%d")
        plain = log_dir / f"{stream}-{date_str}.log"
        gz = log_dir / f"{stream}-{date_str}.log.gz"
        if plain.exists():
            f = plain.open("r", encoding="utf-8")
        elif gz.exists():
            f = gzip.open(gz, "rt", encoding="utf-8")
        else:
            cur += timedelta(days=1)
            continue
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        finally:
            f.close()
        cur += timedelta(days=1)


def emit_event(
    event_type: str,
    row: dict[str, Any],
    *,
    project_path: str | os.PathLike | None = None,
    session_id: str | None = None,
    log_date: date | None = None,
) -> None:
    """Append one JSONL row to the daily file for ``event_type``.

    Common header (ts, project, session_id, event_type) is prepended. Any
    matching keys in ``row`` are overwritten by the header to keep schema
    invariants. Wrapped in try/except — failures cannot break the caller.

    ``log_date`` overrides the daily-file date (default = today UTC).
    Used by the offline correlator (id=1098) so emitted gate_outcome rows
    land in the same daily file as the source gate.log row — required for
    cross-run dedup via ``read_log_range`` over the same date range.
    """
    try:
        if log_date is None:
            file_date = _today_utc_date()
        else:
            file_date = log_date.strftime("%Y-%m-%d")
        path = _project_log_dir(project_path) / f"{event_type}-{file_date}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "ts": _now_iso(),
            "project": _project_basename(project_path),
            "session_id": session_id,
            "event_type": event_type,
        }
        merged = {**row, **header}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(merged, default=str) + "\n")
    except Exception:
        pass


def maintain_log_retention(
    project_path: str | os.PathLike | None = None,
) -> dict:
    """Walk the project's log dir and apply the 30-day-hot / 1-year-warm policy.

    * Files matching ``<stream>-<YYYY-MM-DD>.log`` older than HOT_RETENTION_DAYS
      get gzipped in place; the uncompressed original is deleted.
    * Files matching ``<stream>-<YYYY-MM-DD>.log.gz`` older than
      COLD_RETENTION_DAYS are deleted.
    * Today's file is never touched, even if name parses as a past date
      (clock-skew defence).

    Returns a counts dict for nightly-heal summaries. Idempotent.
    """
    log_dir = _project_log_dir(project_path)
    result = {"gzipped": 0, "deleted": 0, "skipped": 0}
    if not log_dir.is_dir():
        return result
    now = datetime.now(timezone.utc)
    today_str = _today_utc_date()
    for entry in log_dir.iterdir():
        if not entry.is_file():
            continue
        match = _DAILY_LOG_RE.match(entry.name)
        if not match:
            continue
        date_str = match.group("date")
        if date_str == today_str:
            continue
        is_gz = match.group("gz") == ".gz"
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            result["skipped"] += 1
            continue
        age_days = (now - file_date).days
        if not is_gz and age_days > HOT_RETENTION_DAYS:
            gz_path = entry.with_name(entry.name + ".gz")
            try:
                with entry.open("rb") as src, gzip.open(gz_path, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                entry.unlink()
                result["gzipped"] += 1
            except Exception:
                result["skipped"] += 1
        elif is_gz and age_days > COLD_RETENTION_DAYS:
            try:
                entry.unlink()
                result["deleted"] += 1
            except Exception:
                result["skipped"] += 1
    return result
