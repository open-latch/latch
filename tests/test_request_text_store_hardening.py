"""Hardening suite for the vault-local request-text store (Latch 5300).

The store shipped at 5141/5216/5241 with four recorded gaps, each authorized
for repair by handoff 5300 and pinned here BEFORE its repair (4543):

- item 2 — retention: the store deliberately sits outside
  ``log_utils.maintain_log_retention`` because that sweep gzips with the
  ambient umask, which would republish cleartext prompts world-readable. The
  consequence was unbounded growth. The repair applies the same 30-day /
  1-year clock with a mode-preserving sweep, wired into nightly heal, and
  today's file is never touched.
- item 3 — daemon-safe opt-out: ``LATCH_REQUEST_TEXT_CAPTURE`` is env-only
  and never reaches a long-lived shared MCP runtime. A ``request_text_capture``
  key in the vault ``runtime_settings.json`` is honoured with the same
  precedence and fail-closed-on-invalid semantics as
  ``capture_streams.outcome_events_enabled``.
- item 4 — suppression observability: a refused write previously vanished;
  now it emits one structural, text-free row (closed-set reason code, count,
  join keys — never a path or a prompt) on its own daily stream, leaving the
  pinned gate.log schema untouched.
- item 5 — the retired ``CLAUDE_KB_LOG_RAW_QUERY`` flag was silently inert; a
  set flag now produces a one-time, non-fatal ``[latch]`` notice on stderr
  (never stdout — that is the MCP JSON-RPC channel).

Item 1 (the founder's Windows-parity ruling) is documentation-verified; this
suite pins only its testable edges (the README's documented controls).

Review round 1 (Latch 5845) then failed criteria 1, 2, and 5 on boundaries
none of the above reached. The round-2 repairs are pinned at the bottom of
this file:

- criterion 1 — the append path followed a symlink at the store name to a
  regular 0600 file OUTSIDE the vault, which every mode check happily
  approved; and the README described the two opt-out controls as peers when
  the environment variable in fact overrides the vault key absolutely.
- criterion 2 — compression forced every archive to 0600 without reading the
  source, so a 0400 store came back with owner-write ADDED. The item-2 test
  above only ever started from 0600, which is why it passed.
- criterion 5 — the notice read ``os.environ`` inside the shared daemon, whose
  environment ``mcp_broker`` builds from an allowlist that has never carried
  the flag, and whose stderr is redirected to a log file. It could not reach a
  standard MCP operator at all.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
sys.path.insert(0, str(_SRC))

from latch.gate import capture_streams      # noqa: E402
from latch.store import db                   # noqa: E402
from latch.retrieval import embeddings           # noqa: E402
from latch.gate import gate                 # noqa: E402
from latch.pipeline import heal                 # noqa: E402
from latch.common import log_utils            # noqa: E402
from latch.store import paths                # noqa: E402
from latch.gate import request_text_store   # noqa: E402

# Classifier path only — the adversary layer would fire a second live call.
gate.ADVERSARY_ENABLED = False

SENTINEL = (
    "zqxjvbnm-sentinel-request-text please cache Redis sessions for the "
    "checkout service and explain which standing rulings govern that choice "
    + ("padding " * 40)
).strip()

CAPTURE_ENV = "LATCH_REQUEST_TEXT_CAPTURE"
OUTCOME_ENV = "LATCH_OUTCOME_EVENTS"
RETIRED_ENV = "CLAUDE_KB_LOG_RAW_QUERY"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_reqtext_hard_")
    conn = db.connect(tmp)
    return tmp, conn


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _ins(conn, kind, title, body, *, status="staging"):
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec),
    )


def _records(tmp) -> list[dict]:
    path = request_text_store.store_path(tmp)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _gate_rows(tmp) -> list[dict]:
    path = log_utils.today_log_path(gate.LOG_STREAM, tmp)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _suppression_path(tmp) -> Path:
    return log_utils.today_log_path(request_text_store.SUPPRESSION_STREAM, tmp)


def _suppression_rows(tmp) -> list[dict]:
    path = _suppression_path(tmp)
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(conn, tmp, request, **kwargs):
    return gate.run_gate(
        conn, request, project_path=tmp, use_llm=False, **kwargs,
    )


def _settings_path(tmp) -> Path:
    return paths.project_dir(tmp) / paths.VAULT_RUNTIME_SETTINGS_FILENAME


def _write_settings(tmp, payload) -> None:
    path = _settings_path(tmp)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")


def _clear_policy_caches() -> None:
    request_text_store._CAPTURE_SETTINGS_CACHE.clear()
    capture_streams._OUTCOME_SETTINGS_CACHE.clear()


def _store_day(tmp, day, text) -> Path:
    """Write one raw store line for ``day`` through the private append path."""
    path = request_text_store.store_path(tmp, day)
    request_text_store._append_private(
        path, json.dumps({"request_text": text, "ts": f"{day.isoformat()}T00:00:00.000Z"})
    )
    return path


# ---------- item 2: privacy-preserving retention ----------

def test_store_filename_never_matches_the_structural_log_sweep():
    """Pin the load-bearing negative: the `.jsonl` suffix and the hyphenated
    stream name each independently fail `maintain_log_retention`'s pattern,
    whose gzip archives take the ambient umask. If the names ever drift into
    that pattern, prompts get republished world-readable."""
    for name in (
        "gate-request-text-2026-01-01.jsonl",
        "gate-request-text-2026-01-01.jsonl.gz",
    ):
        _assert(log_utils._DAILY_LOG_RE.match(name) is None,
                f"{name!r} must never match the structural-log sweep pattern")
    # And live: an aged store file survives the structural sweep byte-for-byte.
    tmp, conn = _fresh_db()
    try:
        old_day = (datetime.now(timezone.utc) - timedelta(days=90)).date()
        path = _store_day(tmp, old_day, SENTINEL)
        before = path.read_bytes()
        log_utils.maintain_log_retention(tmp)
        _assert(path.exists() and path.read_bytes() == before,
                "the structural-log sweep must leave the store alone")
        print("PASS store_filename_never_matches_the_structural_log_sweep")
    finally:
        _cleanup(tmp, conn)


def test_retention_expires_aged_store_files_without_widening_their_mode():
    """Item-2 acceptance: aged files are compressed (then expired) on the same
    30-day / 1-year clock as the structural logs, but every artifact the sweep
    produces is owner-only — asserted, not assumed. Today's file is never
    touched, even when it parses as aged."""
    tmp, conn = _fresh_db()
    try:
        now = datetime.now(timezone.utc)
        aged_day = (now - timedelta(days=60)).date()
        ancient_day = (now - timedelta(days=400)).date()
        today = now.date()

        aged = _store_day(tmp, aged_day, SENTINEL)
        todays = _store_day(tmp, today, SENTINEL + " today")
        todays_bytes = todays.read_bytes()

        # A hand-made ancient archive, past cold retention.
        ancient_gz = Path(str(request_text_store.store_path(tmp, ancient_day)) + ".gz")
        descriptor = os.open(ancient_gz, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb") as dst:
                dst.write(b"{}\n")

        result = request_text_store.maintain_retention(tmp)

        _assert(result.get("gzipped") == 1, f"one aged file to compress: {result}")
        _assert(result.get("deleted") == 1, f"one ancient archive to delete: {result}")
        _assert(not aged.exists(), "the aged plaintext file must be gone")
        aged_gz = Path(str(aged) + ".gz")
        _assert(aged_gz.exists(), "the aged file must have been compressed in place")
        with gzip.open(aged_gz, "rt", encoding="utf-8") as f:
            _assert(SENTINEL in f.read(), "compression must preserve the content")
        _assert(not ancient_gz.exists(), "the ancient archive must be expired")
        _assert(todays.exists() and todays.read_bytes() == todays_bytes,
                "today's file is never touched")

        if os.name == "posix":
            vault = paths.project_dir(tmp)
            produced = [
                p for p in vault.iterdir()
                if p.name.startswith(request_text_store.STREAM) and p.is_file()
            ]
            _assert(produced, "sweep artifacts must exist to be checked")
            for artifact in produced:
                mode = stat.S_IMODE(artifact.stat().st_mode)
                _assert(mode == 0o600,
                        f"{artifact.name} must stay owner-only, got {oct(mode)}")
        print("PASS retention_expires_aged_store_files_without_widening_their_mode")
    finally:
        _cleanup(tmp, conn)


def test_retention_artifacts_and_temps_stay_gitignored():
    """Verifier finding (5300 validation round): the sweep must not convert a
    gitignored cleartext file into an unignored artifact. The ignore pattern
    has to cover the plain file, the archive, AND the in-flight temp name."""
    ignored = [
        line.strip()
        for line in (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    pattern = request_text_store.GITIGNORE_PATTERN
    _assert(pattern in ignored, f"{pattern!r} must be a .gitignore line")
    import fnmatch
    plain = "gate-request-text-2026-01-01.jsonl"
    for name in (plain, plain + ".gz", f"{plain}.12345.{'a' * 32}.gz.tmp"):
        _assert(fnmatch.fnmatch(name, pattern.removeprefix("**/")),
                f"ignore pattern {pattern!r} must cover {name!r}")
    print("PASS retention_artifacts_and_temps_stay_gitignored")


def test_retention_cleans_stale_temps_and_is_not_jammed_by_them():
    """Verifier finding (5300 validation round): a crash-orphaned temp used to
    (a) hold compressed cleartext forever — outside both sweep patterns — and
    (b) jam that day's compression permanently wherever the pid is stable,
    because the temp name was pid-derived and opened with O_EXCL. The sweep
    must clean stale temps and never be blocked by them; a fresh temp (a
    concurrent compressor's in-flight file) must be left alone."""
    tmp, conn = _fresh_db()
    try:
        now = datetime.now(timezone.utc)
        aged_day = (now - timedelta(days=60)).date()
        aged = _store_day(tmp, aged_day, SENTINEL)

        # A stale orphan for the SAME day (any pid), two days old.
        stale = aged.with_name(f"{aged.name}.99999.{'b' * 32}.gz.tmp")
        stale.write_bytes(b"orphaned")
        old_ns = int((now - timedelta(days=2)).timestamp() * 1e9)
        os.utime(stale, ns=(old_ns, old_ns))

        # A fresh temp — someone else's in-flight compression.
        fresh = aged.with_name(f"{aged.name}.88888.{'c' * 32}.gz.tmp")
        fresh.write_bytes(b"in-flight")

        result = request_text_store.maintain_retention(tmp)
        _assert(result.get("gzipped") == 1,
                f"a stale temp must not jam compression: {result}")
        _assert(not stale.exists(), "the stale orphan must be cleaned")
        _assert(fresh.exists(), "a fresh temp must be left alone")
        _assert(Path(str(aged) + ".gz").exists(), "the day must still compress")
        print("PASS retention_cleans_stale_temps_and_is_not_jammed_by_them")
    finally:
        _cleanup(tmp, conn)


def test_read_day_tolerates_a_corrupt_archive():
    """Verifier finding (5300 validation round): the read side promises
    best-effort tolerance, but a truncated archive raised EOFError through the
    OSError guard. Corruption must yield an empty read, not a crash."""
    tmp, conn = _fresh_db()
    try:
        aged_day = (datetime.now(timezone.utc) - timedelta(days=60)).date()
        _store_day(tmp, aged_day, SENTINEL)
        result = request_text_store.maintain_retention(tmp)
        _assert(result.get("gzipped") == 1, f"fixture must age out: {result}")
        archive = Path(str(request_text_store.store_path(tmp, aged_day)) + ".gz")
        whole = archive.read_bytes()
        archive.write_bytes(whole[: len(whole) // 2])   # truncate mid-stream
        rows = request_text_store.read_day(aged_day, tmp)
        _assert(rows == [], f"a corrupt archive must read as empty: {rows}")
        print("PASS read_day_tolerates_a_corrupt_archive")
    finally:
        _cleanup(tmp, conn)


def test_retention_produces_nothing_when_privacy_cannot_be_asserted():
    """If the archive descriptor cannot be verified private, the sweep must
    leave the original in place and produce no artifact at all — a cleartext
    prompt log is never traded for an unverified archive."""
    tmp, conn = _fresh_db()
    real_assert = request_text_store._assert_private
    try:
        aged_day = (datetime.now(timezone.utc) - timedelta(days=60)).date()
        aged = _store_day(tmp, aged_day, SENTINEL)
        before = aged.read_bytes()

        def _refuse(_descriptor):
            raise OSError("simulated: archive cannot be made private")
        request_text_store._assert_private = _refuse

        result = request_text_store.maintain_retention(tmp)
        _assert(result.get("gzipped") == 0, f"nothing may be compressed: {result}")
        _assert(result.get("skipped", 0) >= 1, f"the refusal must be counted: {result}")
        _assert(aged.exists() and aged.read_bytes() == before,
                "the original must survive a refused compression")
        leftovers = [
            p.name for p in paths.project_dir(tmp).iterdir()
            if p.name.endswith(".gz") or ".tmp" in p.name
        ]
        _assert(not leftovers, f"no partial artifact may remain: {leftovers}")
        print("PASS retention_produces_nothing_when_privacy_cannot_be_asserted")
    finally:
        request_text_store._assert_private = real_assert
        _cleanup(tmp, conn)


def test_read_day_reads_compressed_archives():
    """The documented consumer path must keep working after the sweep
    compresses a day — otherwise retention silently breaks the V5 join."""
    tmp, conn = _fresh_db()
    try:
        aged_day = (datetime.now(timezone.utc) - timedelta(days=60)).date()
        _store_day(tmp, aged_day, SENTINEL)
        result = request_text_store.maintain_retention(tmp)
        _assert(result.get("gzipped") == 1, f"fixture must age out: {result}")
        rows = request_text_store.read_day(aged_day, tmp)
        _assert(len(rows) == 1 and rows[0]["request_text"] == SENTINEL,
                f"read_day must read through the archive: {rows}")
        print("PASS read_day_reads_compressed_archives")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_runs_request_text_retention():
    """Item-2 acceptance: the sweep is wired into nightly heal beside the
    structural-log sweep, with the same counts-summary shape."""
    tmp, conn = _fresh_db()
    try:
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        _assert("request_text_retention" in result,
                f"expected request_text_retention key in summary: {result}")
        retention = result["request_text_retention"]
        _assert(isinstance(retention, dict), f"expected dict, got {type(retention)}")
        for key in ("gzipped", "deleted", "skipped"):
            _assert(key in retention, f"expected {key} in {retention}")
        print("PASS nightly_heal_runs_request_text_retention")
    finally:
        _cleanup(tmp, conn)


def test_nightly_heal_request_text_retention_failure_isolated():
    """A broken store sweep must not take nightly heal down with it."""
    tmp, conn = _fresh_db()
    saved = request_text_store.maintain_retention
    try:
        def _boom(*_args, **_kwargs):
            raise RuntimeError("retention exploded")
        request_text_store.maintain_retention = _boom
        result = heal.nightly_heal(conn, project_path=tmp, use_llm=False)
        _assert(result.get("ok") is True,
                f"nightly_heal should still succeed: {result}")
        _assert("error" in result.get("request_text_retention", {}),
                f"expected error key: {result.get('request_text_retention')}")
        print("PASS nightly_heal_request_text_retention_failure_isolated")
    finally:
        request_text_store.maintain_retention = saved
        _cleanup(tmp, conn)


# ---------- item 3: daemon-safe opt-out via runtime_settings.json ----------

def test_vault_key_matches_the_outcome_events_control_exactly():
    """Item-3 acceptance: `request_text_capture` is honoured with the SAME
    precedence and fail-closed-on-invalid semantics as
    `capture_streams.outcome_events_enabled` — proven by running one scenario
    matrix through BOTH controls and requiring identical outcomes."""
    tmp, conn = _fresh_db()
    prev_capture = os.environ.pop(CAPTURE_ENV, None)
    prev_outcome = os.environ.pop(OUTCOME_ENV, None)
    try:
        paths.ensure_project_dir(tmp)
        scenarios = [
            # (env value or None, settings payload or None-for-missing, expected)
            (None, None, True),                                   # clean install
            (None, {}, True),                                     # key absent
            (None, {"KEY": True}, True),                          # key-set on
            (None, {"KEY": False}, False),                        # key-set off
            ("0", {"KEY": True}, False),                          # both-set: env wins
            ("1", {"KEY": False}, True),                          # both-set: env wins
            ("0", None, False),                                   # env-only off
            (None, "not-json", False),                            # invalid: fail closed
            (None, "[]", False),                                  # invalid: fail closed
            (None, {"KEY": "false"}, False),                      # invalid type: fail closed
            (None, {"KEY": 1}, False),                            # invalid type: fail closed
        ]
        for env_value, payload, expected in scenarios:
            for env_name, key, probe in (
                (CAPTURE_ENV, "request_text_capture",
                 request_text_store.capture_enabled),
                (OUTCOME_ENV, "outcome_events",
                 capture_streams.outcome_events_enabled),
            ):
                os.environ.pop(CAPTURE_ENV, None)
                os.environ.pop(OUTCOME_ENV, None)
                settings = _settings_path(tmp)
                if settings.exists() or settings.is_symlink():
                    settings.unlink()
                if payload is not None:
                    if isinstance(payload, dict):
                        concrete = {
                            (key if k == "KEY" else k): v
                            for k, v in payload.items()
                        }
                        _write_settings(tmp, concrete)
                    else:
                        _write_settings(tmp, payload)
                if env_value is not None:
                    os.environ[env_name] = env_value
                _clear_policy_caches()
                got = probe(tmp)
                _assert(got is expected,
                        f"{probe.__module__}.{probe.__name__} under "
                        f"env={env_value!r} payload={payload!r}: "
                        f"expected {expected}, got {got}")
        print("PASS vault_key_matches_the_outcome_events_control_exactly")
    finally:
        os.environ.pop(CAPTURE_ENV, None)
        os.environ.pop(OUTCOME_ENV, None)
        if prev_capture is not None:
            os.environ[CAPTURE_ENV] = prev_capture
        if prev_outcome is not None:
            os.environ[OUTCOME_ENV] = prev_outcome
        _clear_policy_caches()
        _cleanup(tmp, conn)


def test_symlinked_vault_policy_fails_closed():
    """A symlinked settings file is refused outright, exactly like the
    outcome-events control."""
    if os.name != "posix":
        print("SKIP symlinked_vault_policy_fails_closed (POSIX symlinks)")
        return
    tmp, conn = _fresh_db()
    prev = os.environ.pop(CAPTURE_ENV, None)
    try:
        paths.ensure_project_dir(tmp)
        real = paths.project_dir(tmp) / "elsewhere.json"
        real.write_text(json.dumps({"request_text_capture": True}), encoding="utf-8")
        settings = _settings_path(tmp)
        if settings.exists():
            settings.unlink()
        os.symlink(real, settings)
        _clear_policy_caches()
        _assert(request_text_store.capture_enabled(tmp) is False,
                "a symlinked policy file must fail closed")
        print("PASS symlinked_vault_policy_fails_closed")
    finally:
        if prev is not None:
            os.environ[CAPTURE_ENV] = prev
        _clear_policy_caches()
        _cleanup(tmp, conn)


def test_vault_key_policy_changes_are_seen_without_reimport():
    """The stat-signature cache must notice an edited policy file — a daemon
    honours the opt-out without a restart."""
    tmp, conn = _fresh_db()
    prev = os.environ.pop(CAPTURE_ENV, None)
    try:
        paths.ensure_project_dir(tmp)
        _write_settings(tmp, {"request_text_capture": True})
        _clear_policy_caches()
        _assert(request_text_store.capture_enabled(tmp) is True, "baseline on")
        _write_settings(tmp, {"request_text_capture": False})
        bump = _settings_path(tmp).stat()
        os.utime(_settings_path(tmp), ns=(bump.st_atime_ns, bump.st_mtime_ns + 1_000_000))
        _assert(request_text_store.capture_enabled(tmp) is False,
                "an edited policy file must be honoured without re-import")
        print("PASS vault_key_policy_changes_are_seen_without_reimport")
    finally:
        if prev is not None:
            os.environ[CAPTURE_ENV] = prev
        _clear_policy_caches()
        _cleanup(tmp, conn)


def test_vault_key_opt_out_reaches_the_gate_write_path():
    """The daemon-safe scenario end to end: no env var anywhere, the vault key
    alone must stop the text — and only the text. Opt-out is silence, not
    suppression: no store file, no suppression row, structural log intact."""
    tmp, conn = _fresh_db()
    prev = os.environ.pop(CAPTURE_ENV, None)
    try:
        paths.ensure_project_dir(tmp)
        _write_settings(tmp, {"request_text_capture": False})
        _clear_policy_caches()
        _run(conn, tmp, SENTINEL)
        _assert(not request_text_store.store_path(tmp).exists(),
                "the vault key alone must suppress the store file")
        _assert(_suppression_rows(tmp) == [],
                "a deliberate opt-out must not emit a suppression signal")
        rows = _gate_rows(tmp)
        _assert(len(rows) == 1, f"structural log must still fire: {rows}")
        _assert(rows[0]["query_hash"] == gate._query_hash(SENTINEL), rows[0])
        print("PASS vault_key_opt_out_reaches_the_gate_write_path")
    finally:
        if prev is not None:
            os.environ[CAPTURE_ENV] = prev
        _clear_policy_caches()
        _cleanup(tmp, conn)


# ---------- item 4: suppression observability ----------

def test_refused_write_emits_a_text_free_suppression_signal():
    """Item-4 acceptance: when the mode check refuses a write, one structural
    row records why — closed-set reason, count, and the exact join keys the
    gate row already carries. Never a path, never a prompt."""
    if not hasattr(os, "fchmod") or os.name != "posix":
        print("SKIP refused_write_emits_a_text_free_suppression_signal "
              "(POSIX mode semantics only)")
        return
    tmp, conn = _fresh_db()
    real_fchmod = os.fchmod
    try:
        path = request_text_store.store_path(tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.chmod(path, 0o644)

        def _refuse(*_args, **_kwargs):
            raise OSError("simulated: filesystem does not support fchmod")
        os.fchmod = _refuse

        result = _run(conn, tmp, SENTINEL)
        _assert(result["verdict"] is not None,
                "the verdict must survive a refused write")
        rows = _suppression_rows(tmp)
        _assert(len(rows) == 1, f"exactly one suppression row: {rows}")
        row = rows[0]
        gate_row = _gate_rows(tmp)[-1]
        expected_keys = {
            "ts", "project", "session_id", "event_type",
            "reason", "count", "gate_call_id", "query_hash",
        }
        _assert(set(row) == expected_keys,
                f"suppression row must carry exactly the structural keys:\n"
                f"  got {sorted(row)}")
        _assert(row["reason"] == "non_private_mode",
                f"reason must name the refusal: {row}")
        _assert(row["reason"] in request_text_store.SUPPRESSION_REASONS, row)
        _assert(row["count"] == 1, row)
        _assert(row["gate_call_id"] == gate_row["gate_call_id"], row)
        _assert(row["query_hash"] == gate_row["query_hash"], row)
        _assert(row["ts"] == gate_row["ts"],
                "the suppression row must share the gate row's timestamp")
        blob = _suppression_path(tmp).read_bytes()
        _assert(b"zqxjvbnm" not in blob, "no request text in the signal")
        _assert(str(path).encode() not in blob, "no filesystem path in the signal")
        _assert(b"simulated" not in blob, "no exception prose in the signal")
        _assert(path.read_bytes() == b"", "the refused store must stay empty")
        print("PASS refused_write_emits_a_text_free_suppression_signal")
    finally:
        os.fchmod = real_fchmod
        _cleanup(tmp, conn)


def test_suppression_reports_a_non_regular_target():
    """The other refusal class: a store path that is not a regular file."""
    if os.name != "posix":
        print("SKIP suppression_reports_a_non_regular_target (POSIX only)")
        return
    tmp, conn = _fresh_db()
    real_store_path = request_text_store.store_path
    try:
        request_text_store.store_path = lambda *_a, **_k: Path("/dev/null")
        _run(conn, tmp, SENTINEL)
        rows = _suppression_rows(tmp)
        _assert(len(rows) == 1, f"exactly one suppression row: {rows}")
        _assert(rows[0]["reason"] == "non_regular_target", rows[0])
        print("PASS suppression_reports_a_non_regular_target")
    finally:
        request_text_store.store_path = real_store_path
        _cleanup(tmp, conn)


def test_suppression_stream_stays_off_the_gate_log():
    """Item-4 constraint: the signal must not widen gate.log. The pinned-schema
    test in the 5141 suite is the primary guard; this pins the stream identity
    so the two files can never merge."""
    _assert(request_text_store.SUPPRESSION_STREAM != gate.LOG_STREAM,
            "the suppression signal must ride its own stream")
    _assert(log_utils._DAILY_LOG_RE.match(
        f"{request_text_store.SUPPRESSION_STREAM}-2026-01-01.log"),
        "the suppression stream must follow the structural daily-log "
        "convention so standard retention applies to it")
    print("PASS suppression_stream_stays_off_the_gate_log")


# ---------- item 5: the retired flag notices instead of silence ----------

def test_setting_the_retired_raw_query_flag_notices_once_on_stderr():
    """Item-5 acceptance: an operator who sets the retired flag gets one
    non-fatal `[latch]` line on stderr (never stdout — MCP JSON-RPC lives
    there), and the gate keeps working exactly as before."""
    tmp, conn = _fresh_db()
    prev_env = os.environ.pop(RETIRED_ENV, None)
    real_stderr = sys.stderr
    try:
        os.environ[RETIRED_ENV] = "1"
        request_text_store._retired_notice_emitted = False
        buffer = io.StringIO()
        sys.stderr = buffer
        _run(conn, tmp, SENTINEL)
        _run(conn, tmp, SENTINEL + " second call")
        sys.stderr = real_stderr
        notice = buffer.getvalue()
        _assert(notice.count(RETIRED_ENV) == 1,
                f"the notice must fire exactly once across calls: {notice!r}")
        _assert(notice.startswith("[latch] "),
                f"stderr diagnostics carry the [latch] prefix: {notice!r}")
        _assert("retired" in notice, f"the notice must say the flag is retired: {notice!r}")
        _assert(len(_gate_rows(tmp)) == 2,
                "the notice must not cost a single structural row")

        # Unset, the notice never fires.
        os.environ.pop(RETIRED_ENV, None)
        request_text_store._retired_notice_emitted = False
        buffer = io.StringIO()
        sys.stderr = buffer
        _run(conn, tmp, SENTINEL + " third call")
        sys.stderr = real_stderr
        _assert(RETIRED_ENV not in buffer.getvalue(),
                f"no notice when the flag is unset: {buffer.getvalue()!r}")
        print("PASS setting_the_retired_raw_query_flag_notices_once_on_stderr")
    finally:
        sys.stderr = real_stderr
        os.environ.pop(RETIRED_ENV, None)
        if prev_env is not None:
            os.environ[RETIRED_ENV] = prev_env
        request_text_store._retired_notice_emitted = False
        _cleanup(tmp, conn)


# ---------- item 1 edge + README coherence ----------

def test_readme_documents_both_controls_and_the_retention_policy():
    """Item-3 acceptance names README coverage of both opt-out controls, and
    item-2 requires the retention policy stated beside the capture paragraph.
    The stale 'not compressed or expired' claim must be gone with it."""
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    _assert("request_text_capture" in readme,
            "README must document the vault-policy key")
    _assert("LATCH_REQUEST_TEXT_CAPTURE" in readme,
            "README must document the env-var control")
    _assert("not compressed or expired" not in readme,
            "the superseded no-retention claim must not survive")
    anchor = readme.find("gate-request-text-")
    _assert(anchor != -1, "the capture paragraph must name the store file")
    window = readme[anchor:anchor + 2000]
    _assert("30 days" in window and "one year" in window,
            "the retention policy must be stated next to the capture paragraph")
    print("PASS readme_documents_both_controls_and_the_retention_policy")


# ---------- review round 2 (Latch 5845): criteria 1, 2, and 5 ----------

def test_append_refuses_a_symlink_pointing_outside_the_vault():
    """Criterion-1 repair. A symlink planted at the store name redirects the
    prompt to whatever it points at, and pointing it at a private regular file
    the attacker owns satisfies every check the store had: regular file, 0600,
    owner-readable. Containment therefore cannot rest on the mode assertion —
    the open itself must refuse to follow. The refusal is a real refusal, so
    it also has to show up on the suppression stream."""
    if os.name != "posix":
        print("SKIP append_refuses_a_symlink_pointing_outside_the_vault (POSIX symlinks)")
        return
    tmp, conn = _fresh_db()
    outside_root = tempfile.mkdtemp(prefix="kb_reqtext_outside_")
    try:
        paths.ensure_project_dir(tmp)
        # A perfectly private regular file — just not in the vault.
        outside = Path(outside_root) / "harvested.jsonl"
        descriptor = os.open(outside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)

        target = request_text_store.store_path(tmp)
        if target.exists():
            target.unlink()
        os.symlink(outside, target)

        _run(conn, tmp, SENTINEL)

        _assert(outside.read_bytes() == b"",
                "the prompt must never be written through the symlink")
        _assert(target.is_symlink(),
                "the store path must be left as found, not replaced")
        rows = _suppression_rows(tmp)
        _assert(len(rows) == 1, f"one refusal must be reported: {rows}")
        _assert(rows[0].get("reason") == "symlinked_target",
                f"the refusal reason must name the symlink: {rows[0]}")
        _assert(rows[0]["reason"] in request_text_store.SUPPRESSION_REASONS,
                "the reason must stay inside the closed set")
        blob = json.dumps(rows[0])
        _assert(SENTINEL[:40] not in blob, "no prompt text on the suppression row")
        _assert(str(outside) not in blob and outside.name not in blob,
                "no filesystem path on the suppression row")
        _assert(len(_gate_rows(tmp)) == 1,
                "the structural gate row still lands")
        print("PASS append_refuses_a_symlink_pointing_outside_the_vault")
    finally:
        shutil.rmtree(outside_root, ignore_errors=True)
        _cleanup(tmp, conn)


def test_retention_never_widens_a_tightened_source_mode():
    """Criterion-2 repair. 'Never widen' is stricter than 'always 0600': an
    operator who tightened the store to 0400 must not get owner-write back
    from the sweep. Every artifact is compared against the mode of the file it
    replaced, not against a constant — which is the assertion the round-1
    suite was missing."""
    if os.name != "posix":
        print("SKIP retention_never_widens_a_tightened_source_mode (POSIX modes)")
        return
    tmp, conn = _fresh_db()
    try:
        now = datetime.now(timezone.utc)
        aged_day = (now - timedelta(days=60)).date()
        aged = _store_day(tmp, aged_day, SENTINEL)
        os.chmod(aged, 0o400)
        source_mode = stat.S_IMODE(aged.stat().st_mode)
        _assert(source_mode == 0o400, f"fixture must be 0400, got {oct(source_mode)}")

        result = request_text_store.maintain_retention(tmp)
        _assert(result.get("gzipped") == 1, f"the aged file must compress: {result}")

        archive = Path(str(aged) + ".gz")
        _assert(archive.exists(), "the archive must exist")
        archive_mode = stat.S_IMODE(archive.stat().st_mode)
        _assert(archive_mode == source_mode,
                f"archive must inherit 0400, got {oct(archive_mode)}")
        _assert(archive_mode & ~source_mode == 0,
                f"the sweep must never ADD a bit: {oct(archive_mode)} vs {oct(source_mode)}")
        with gzip.open(archive, "rt", encoding="utf-8") as f:
            _assert(SENTINEL in f.read(), "compression must still preserve content")

        # Nothing else the sweep left behind may be wider than owner-only.
        vault = paths.project_dir(tmp)
        for produced in vault.iterdir():
            if not produced.name.startswith(request_text_store.STREAM):
                continue
            if not produced.is_file():
                continue
            mode = stat.S_IMODE(produced.stat().st_mode)
            _assert(mode & 0o077 == 0,
                    f"{produced.name} must not be group/other readable: {oct(mode)}")
        print("PASS retention_never_widens_a_tightened_source_mode")
    finally:
        _cleanup(tmp, conn)


def test_retention_narrows_an_already_wide_source():
    """The other direction of the same rule: inheriting the source's bits must
    not turn into COPYING them. A store that is somehow 0644 gets a 0600
    archive — narrowing is always allowed, widening never is."""
    if os.name != "posix":
        print("SKIP retention_narrows_an_already_wide_source (POSIX modes)")
        return
    tmp, conn = _fresh_db()
    try:
        aged_day = (datetime.now(timezone.utc) - timedelta(days=60)).date()
        aged = _store_day(tmp, aged_day, SENTINEL)
        os.chmod(aged, 0o644)

        result = request_text_store.maintain_retention(tmp)
        _assert(result.get("gzipped") == 1, f"the aged file must compress: {result}")
        archive = Path(str(aged) + ".gz")
        mode = stat.S_IMODE(archive.stat().st_mode)
        _assert(mode == 0o600,
                f"a wide source must yield an owner-only archive, got {oct(mode)}")
        print("PASS retention_narrows_an_already_wide_source")
    finally:
        _cleanup(tmp, conn)


def test_the_shared_daemon_can_never_see_the_retired_flag():
    """Criterion-5 repair, first boundary. The reason the old notice could not
    work is structural, not incidental: `mcp_broker` builds the long-lived
    daemon's environment from an allowlist, so the flag an operator exports
    never arrives. Pinning that here keeps a future refactor from quietly
    moving the notice back inside the daemon."""
    from latch.mcp import mcp_broker      # noqa: PLC0415
    from latch.mcp import mcp_runtime     # noqa: PLC0415

    allowlisted = (
        mcp_broker.DAEMON_OS_ENV_VARS
        + mcp_broker.DAEMON_OWNER_ENV_VARS
        + mcp_broker.DAEMON_HELPER_ENV_VARS
        + mcp_runtime.PROCESS_OS_ENV_VARS
    )
    _assert(RETIRED_ENV not in allowlisted,
            "the retired flag must not be donated to the daemon")

    built = mcp_broker._daemon_environment(
        source={RETIRED_ENV: "1", "PATH": os.environ.get("PATH", "")},
        for_helper=True,
    )
    _assert(RETIRED_ENV not in built,
            f"the daemon environment must not carry the flag: {sorted(built)}")
    print("PASS the_shared_daemon_can_never_see_the_retired_flag")


def test_the_mcp_proxy_emits_the_retired_flag_notice():
    """Criterion-5 repair, second boundary. The proxy is the last process on
    the standard MCP path that still holds the operator's environment, so the
    notice fires there — before any branch that could exit early, and on
    stderr only. `connection_metadata` is made to fail so `main` returns
    immediately without starting a daemon."""
    from latch.mcp import mcp_proxy       # noqa: PLC0415

    prev_env = os.environ.pop(RETIRED_ENV, None)
    prev_legacy = os.environ.pop("LATCH_MCP_FORCE_LEGACY", None)
    real_metadata = mcp_proxy.connection_metadata
    real_stderr, real_stdout = sys.stderr, sys.stdout
    try:
        os.environ[RETIRED_ENV] = "1"
        request_text_store._retired_notice_emitted = False

        def _refuse():
            raise ValueError("probe: no daemon in this test")

        mcp_proxy.connection_metadata = _refuse
        err, out = io.StringIO(), io.StringIO()
        sys.stderr, sys.stdout = err, out
        code = mcp_proxy.main()
        sys.stderr, sys.stdout = real_stderr, real_stdout

        _assert(code == 2, f"the probe must short-circuit main(), got {code}")
        notice = err.getvalue()
        _assert(RETIRED_ENV in notice,
                f"the proxy must warn about the retired flag: {notice!r}")
        _assert("retired" in notice, f"the notice must say retired: {notice!r}")
        _assert(request_text_store.CAPTURE_ENV in notice,
                "the notice must point at the live opt-out")
        _assert(RETIRED_ENV not in out.getvalue(),
                "stdout is the JSON-RPC channel and must stay clean")
        print("PASS the_mcp_proxy_emits_the_retired_flag_notice")
    finally:
        sys.stderr, sys.stdout = real_stderr, real_stdout
        mcp_proxy.connection_metadata = real_metadata
        os.environ.pop(RETIRED_ENV, None)
        if prev_env is not None:
            os.environ[RETIRED_ENV] = prev_env
        if prev_legacy is not None:
            os.environ["LATCH_MCP_FORCE_LEGACY"] = prev_legacy
        request_text_store._retired_notice_emitted = False


def test_doctor_reports_the_retired_flag():
    """Criterion-5 repair, third boundary. Stderr is only operator-visible if
    the host shows it; doctor is the surface an operator opens on purpose, and
    it runs in their own environment rather than the daemon's."""
    from latch.install import doctor          # noqa: PLC0415

    prev_env = os.environ.pop(RETIRED_ENV, None)
    try:
        name, level, detail = doctor.check_retired_env_flags()
        _assert(level == doctor.OK, f"unset flag must be clean: {level} {detail}")

        os.environ[RETIRED_ENV] = "1"
        name, level, detail = doctor.check_retired_env_flags()
        _assert(level == doctor.WARN,
                f"a set retired flag must WARN, not FAIL: {level}")
        _assert(RETIRED_ENV in detail and "retired" in detail,
                f"the row must name the flag and its status: {detail!r}")
        _assert(request_text_store.CAPTURE_ENV in detail
                and request_text_store.SETTINGS_KEY in detail,
                f"the row must point at both live controls: {detail!r}")
        print("PASS doctor_reports_the_retired_flag")
    finally:
        os.environ.pop(RETIRED_ENV, None)
        if prev_env is not None:
            os.environ[RETIRED_ENV] = prev_env


def test_readme_states_env_precedence_over_the_vault_key():
    """Criterion-1 repair, documentation half. The README called the two
    controls interchangeable; they are not. A user who sets the vault key to
    false and leaves LATCH_REQUEST_TEXT_CAPTURE=1 exported is still captured,
    and the README has to say so."""
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    anchor = readme.find("gate-request-text-")
    _assert(anchor != -1, "the capture paragraph must name the store file")
    window = readme[anchor:anchor + 2600]
    _assert("Either control suppresses the text while\nleaving the structural "
            "records untouched." not in window,
            "the bare 'either control' claim must not stand alone")
    _assert("precedence" in window or "not consulted" in window,
            f"the README must state the precedence: {window!r}")
    _assert("LATCH_REQUEST_TEXT_CAPTURE=1" in window,
            "the README must name the case where the env var overrides the key")
    _assert("symbolic links" in window or "symlink" in window,
            "the README must state that the store is opened without following links")
    print("PASS readme_states_env_precedence_over_the_vault_key")
