"""Vault-local verbatim request-text store (Latch 5141, consumer contract 4676 A4 v5).

The shipped gate persists only ``query_hash`` and ``query_chars``, so V5's
qualifying window could never accumulate a task prompt (Latch 4982). This
suite pins the remedy and, just as importantly, pins what the remedy must NOT
disturb:

- item 1 — every gate call writes one 0600 vault-local record whose text
  satisfies the A4 v5 verification rule ``sha1(text)[:12] == query_hash`` and
  ``len(text) == query_chars``, joinable back to its gate.log row;
- item 2 — gate.log and every artifact a run produces stay structural-only.
  The ``query_excerpt`` affordance is superseded: raw request text now has one
  home, and it is not the structural log (fact 3091 stays true).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
sys.path.insert(0, str(_SRC))

import db                   # noqa: E402
import embeddings           # noqa: E402
import gate                 # noqa: E402
import log_utils            # noqa: E402
import paths                # noqa: E402
import request_text_store   # noqa: E402

# Classifier path only — the adversary layer would fire a second live call.
gate.ADVERSARY_ENABLED = False

# A request that is unmistakable in a byte sweep and long enough that the old
# 200-char excerpt would have truncated it.
SENTINEL = (
    "zqxjvbnm-sentinel-request-text please cache Redis sessions for the "
    "checkout service and explain which standing rulings govern that choice "
    + ("padding " * 40)
).strip()


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_reqtext_")
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


def _run(conn, tmp, request, **kwargs):
    return gate.run_gate(
        conn, request, project_path=tmp, use_llm=False, **kwargs,
    )


# ---------- item 1: the store ----------

def test_gate_call_persists_hash_verified_verbatim_text():
    """Item-1 acceptance: a live-style gate call produces a record whose text
    passes 4676 A4 v5's verification rule in both directions."""
    tmp, conn = _fresh_db()
    try:
        _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        _run(conn, tmp, SENTINEL, host_adapter="claude")
        records = _records(tmp)
        _assert(len(records) == 1, f"exactly one record per gate call: {records}")
        row = records[0]
        text = row["request_text"]
        _assert(text == SENTINEL, "text must be stored verbatim, never edited")
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        _assert(digest == row["query_hash"],
                f"sha1(text)[:12] must equal query_hash: {digest} vs {row['query_hash']}")
        _assert(len(text) == row["query_chars"],
                f"len(text) must equal query_chars: {len(text)} vs {row['query_chars']}")
        print("PASS gate_call_persists_hash_verified_verbatim_text")
    finally:
        _cleanup(tmp, conn)


def test_stored_text_is_untruncated_past_the_old_excerpt_cap():
    """The superseded affordance capped text at 200 chars and would have lost
    54% of the real corpus (4982). Verbatim means verbatim."""
    tmp, conn = _fresh_db()
    try:
        long_request = "x" * 5000
        _run(conn, tmp, long_request)
        row = _records(tmp)[-1]
        _assert(row["request_text"] == long_request,
                f"5000-char request must round-trip whole: got {len(row['request_text'])}")
        _assert(row["query_chars"] == 5000, row["query_chars"])
        print("PASS stored_text_is_untruncated_past_the_old_excerpt_cap")
    finally:
        _cleanup(tmp, conn)


def test_non_ascii_text_satisfies_the_verification_rule():
    """``query_chars`` counts characters while ``query_hash`` digests UTF-8
    bytes. A consumer applying A4 v5 literally must still verify, so pin the
    two units against a request where they differ."""
    tmp, conn = _fresh_db()
    try:
        request = "cache les sessions Redis — naïve façade, 日本語テキスト, emoji 🚀 included"
        _run(conn, tmp, request)
        row = _records(tmp)[-1]
        text = row["request_text"]
        _assert(text == request, "non-ASCII text must round-trip exactly")
        _assert(len(text.encode("utf-8")) != len(text),
                "fixture must actually exercise the chars-vs-bytes difference")
        _assert(hashlib.sha1(text.encode("utf-8")).hexdigest()[:12] == row["query_hash"],
                f"hash must verify over UTF-8 bytes: {row}")
        _assert(len(text) == row["query_chars"],
                f"query_chars must count characters, not bytes: {row}")
        print("PASS non_ascii_text_satisfies_the_verification_rule")
    finally:
        _cleanup(tmp, conn)


def test_store_is_vault_local_and_mode_0600():
    """Item-1 acceptance: path under the vault, 0600, private parents."""
    tmp, conn = _fresh_db()
    try:
        _run(conn, tmp, SENTINEL)
        path = request_text_store.store_path(tmp)
        vault = paths.project_dir(tmp).resolve()
        _assert(path.exists(), f"store should exist at {path}")
        _assert(path.resolve().parent == vault,
                f"store must live under the vault dir {vault}: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        _assert(mode == 0o600, f"store must be 0600, got {oct(mode)}")
        print("PASS store_is_vault_local_and_mode_0600")
    finally:
        _cleanup(tmp, conn)


def test_store_filename_is_gitignored():
    """Item-1 acceptance: cleartext prompts are acceptable *because* the
    artifact never leaves the vault (ruling 4562). Pin the ignore rule the way
    the outcome-lineage checkpoint pins its own."""
    ignored = [
        line.strip()
        for line in (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    ]
    pattern = request_text_store.GITIGNORE_PATTERN
    _assert(pattern in ignored, f"{pattern!r} must be a .gitignore line")
    name = request_text_store.store_path(os.getcwd()).name
    _assert(fnmatch.fnmatch(name, pattern.removeprefix("**/")),
            f"ignore pattern {pattern!r} must cover the emitted filename {name!r}")
    print("PASS store_filename_is_gitignored")


def test_record_carries_host_and_session_identity():
    """The record must be joinable to its gate.log row and attributable to the
    host/session the gate already identified."""
    tmp, conn = _fresh_db()
    session = "22222222-2222-2222-2222-222222222222"
    try:
        result = _run(conn, tmp, SENTINEL, session_id=session, host_adapter="codex")
        row = _records(tmp)[-1]
        gate_row = _gate_rows(tmp)[-1]
        _assert(row["session_id"] == session, row)
        _assert(row["host_adapter"] == "codex", row)
        _assert(row["gate_call_id"] == result["gate_call_id"], row)
        _assert(row["runtime_version"] == gate_row["runtime_version"], row)
        _assert(row["project"] == gate_row["project"], row)
        _assert(row["event_type"] == request_text_store.STREAM, row)
        print("PASS record_carries_host_and_session_identity")
    finally:
        _cleanup(tmp, conn)


def test_record_joins_to_its_gate_log_row_on_query_hash_and_timestamp():
    """Item-1 acceptance: joinable by (query_hash, timestamp). The two writes
    share one timestamp rather than stamping themselves independently, so the
    composite key is exact and not merely near."""
    tmp, conn = _fresh_db()
    try:
        _run(conn, tmp, SENTINEL)
        _run(conn, tmp, SENTINEL + " second call")
        rows = _gate_rows(tmp)
        records = _records(tmp)
        _assert(len(rows) == 2 and len(records) == 2,
                f"one record per gate row: {len(rows)} vs {len(records)}")
        for gate_row, record in zip(rows, records):
            key = (gate_row["query_hash"], gate_row["ts"])
            _assert((record["query_hash"], record["ts"]) == key,
                    f"(query_hash, ts) must match exactly: {key} vs "
                    f"{(record['query_hash'], record['ts'])}")
            _assert(record["query_chars"] == gate_row["query_chars"], record)
        print("PASS record_joins_to_its_gate_log_row_on_query_hash_and_timestamp")
    finally:
        _cleanup(tmp, conn)


def test_capture_is_default_on():
    """No configuration required: the qualifying window starts accumulating at
    deploy, not at the first person who remembers to set a flag."""
    _prev = os.environ.pop(request_text_store.CAPTURE_ENV, None)
    try:
        _assert(request_text_store.capture_enabled() is True,
                "capture must default ON with no env var set")
    finally:
        if _prev is not None:
            os.environ[request_text_store.CAPTURE_ENV] = _prev
    print("PASS capture_is_default_on")


def test_opt_out_suppresses_text_but_leaves_structural_log_intact():
    """Item-1 acceptance: the opt-out removes the text and nothing else."""
    tmp, conn = _fresh_db()
    _prev = os.environ.get(request_text_store.CAPTURE_ENV)
    try:
        os.environ[request_text_store.CAPTURE_ENV] = "0"
        _assert(request_text_store.capture_enabled() is False,
                "explicit non-'1' value must disable capture")
        _run(conn, tmp, SENTINEL)
        _assert(_records(tmp) == [], "opt-out must write no request text")
        _assert(not request_text_store.store_path(tmp).exists(),
                "opt-out must not even create the store file")
        rows = _gate_rows(tmp)
        _assert(len(rows) == 1, f"structural log must still fire: {rows}")
        for field in ("query_hash", "query_chars", "recommendation", "seed_ids",
                      "evidence_ids", "elapsed_ms", "ts", "project"):
            _assert(field in rows[0], f"{field} missing under opt-out: {rows[0]}")
        print("PASS opt_out_suppresses_text_but_leaves_structural_log_intact")
    finally:
        if _prev is None:
            os.environ.pop(request_text_store.CAPTURE_ENV, None)
        else:
            os.environ[request_text_store.CAPTURE_ENV] = _prev
        _cleanup(tmp, conn)


def test_blank_request_hash_normalization_is_recorded_faithfully():
    """``_query_hash`` folds whitespace-only requests to the empty string, so
    such a record cannot satisfy the A4 v5 hash check. That is correct — A4(c)
    excludes it anyway — but the text must still be stored verbatim rather than
    silently normalized to match."""
    tmp, conn = _fresh_db()
    try:
        _run(conn, tmp, "   \n  ")
        row = _records(tmp)[-1]
        _assert(row["request_text"] == "   \n  ",
                f"whitespace must be preserved byte-for-byte: {row}")
        _assert(row["query_chars"] == 6, row)
        _assert(row["query_hash"] == gate._query_hash("   \n  "), row)
        _assert(hashlib.sha1(row["request_text"].encode("utf-8")).hexdigest()[:12]
                != row["query_hash"],
                "the normalization gap must stay visible, not be papered over")
        print("PASS blank_request_hash_normalization_is_recorded_faithfully")
    finally:
        _cleanup(tmp, conn)


def test_store_write_failure_cannot_break_the_gate():
    """Best-effort, exactly like the structural log: a broken store must not
    change a verdict or drop a gate.log row."""
    tmp, conn = _fresh_db()
    _prev = request_text_store.store_path
    try:
        def _boom(*_args, **_kwargs):
            raise OSError("simulated store failure")
        request_text_store.store_path = _boom
        result = _run(conn, tmp, SENTINEL)
        _assert(result["verdict"] is not None, "verdict must survive a store failure")
        _assert(len(_gate_rows(tmp)) == 1,
                "gate.log row must survive a store failure")
        print("PASS store_write_failure_cannot_break_the_gate")
    finally:
        request_text_store.store_path = _prev
        _cleanup(tmp, conn)


# ---------- item 2: structural surfaces unchanged ----------

# The gate.log row shape for a default (use_llm=False) call, including the
# approved structural ``model`` observability field. A change here is a change
# to the correlator's input format and must stay explicit.
GATE_LOG_KEYS = frozenset({
    "ts", "project", "session_id", "event_type",
    "gate_call_id",
    "measurement_protocol_version", "host_adapter", "attestation",
    "runtime_attestation", "runtime_version", "project_proof_version",
    "key_epoch", "project_proof",
    "query_hash", "query_chars",
    "recommendation", "skipped", "error",
    "evidence_ids", "decision_chain", "abandoned_paths", "active_constraints",
    "current_direction", "surfaced_rejected_paths", "cited_rejected_paths",
    "seed_count", "seed_ids", "seeds", "chain_lane_contacts",
    "reachable_count", "prompt_chars", "backend", "model", "timed_out", "elapsed_ms",
    "budget_count", "load_bearing_claim_count", "uncovered_claim_count",
    "evidence_type_counts", "gap_type_counts",
})


def test_gate_log_schema_is_documented_and_structural_only():
    """The record schema is exact and contains no request-text field."""
    tmp, conn = _fresh_db()
    try:
        _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        _run(conn, tmp, SENTINEL, host_adapter="claude")
        keys = set(_gate_rows(tmp)[-1])
        _assert(keys == set(GATE_LOG_KEYS),
                f"gate.log schema drifted:\n  added {sorted(keys - GATE_LOG_KEYS)}\n"
                f"  removed {sorted(GATE_LOG_KEYS - keys)}")
        print("PASS gate_log_schema_is_unchanged")
    finally:
        _cleanup(tmp, conn)


def test_gate_log_never_carries_request_text_and_has_no_opt_in_left():
    """The whole ``CLAUDE_KB_LOG_RAW_QUERY`` affordance is retired (5141 item 1,
    review round 1 / 5216). There is no setting that puts free text in gate.log,
    so fact 3091 holds unconditionally rather than by default — pinned by the
    absence of the flag itself, not merely by its default value."""
    tmp, conn = _fresh_db()
    try:
        os.environ["CLAUDE_KB_LOG_RAW_QUERY"] = "1"   # must be inert now
        _assert(not hasattr(gate, "LOG_RAW_QUERY"),
                "the opt-in flag must not exist; a flag that exists can be set")
        _assert(not hasattr(gate, "LOG_QUERY_EXCERPT_CHARS"),
                "the excerpt cap must not exist; nothing excerpts text any more")
        _run(conn, tmp, SENTINEL)
        raw = json.dumps(_gate_rows(tmp)[-1])
        for banned in ("query_excerpt", "uncovered_claim_texts",
                       "zqxjvbnm-sentinel-request-text"):
            _assert(banned not in raw, f"{banned} must not reach gate.log: {raw[:400]}")
        print("PASS gate_log_never_carries_request_text_and_has_no_opt_in_left")
    finally:
        os.environ.pop("CLAUDE_KB_LOG_RAW_QUERY", None)
        _cleanup(tmp, conn)


def test_sweep_finds_request_text_only_in_the_private_store():
    """Item-2 acceptance: an automated sweep of every artifact a run produces
    finds zero raw request text outside the 0600 store."""
    tmp, conn = _fresh_db()
    original = gate.classify_gate
    try:
        # Adversarial on two axes: the retired opt-in forced on in the
        # environment, and a classifier that echoes the whole request back as a
        # claim — the exact pair that leaked in review round 1.
        os.environ["CLAUDE_KB_LOG_RAW_QUERY"] = "1"
        gate.classify_gate = (
            lambda chain_assembly, **kw: _verdict_echoing_the_request(SENTINEL)
        )
        _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        gate.run_gate(conn, SENTINEL, project_path=tmp, use_llm=True,
                      session_id="33333333-3333-3333-3333-333333333333",
                      host_adapter="claude")
        conn.commit()
        store = request_text_store.store_path(tmp).resolve()
        vault = paths.project_dir(tmp).resolve()
        needle = b"zqxjvbnm-sentinel-request-text"
        swept, leaked = 0, []
        for path in sorted(vault.rglob("*")):
            if not path.is_file() or path.resolve() == store:
                continue
            swept += 1
            if needle in path.read_bytes():
                leaked.append(str(path.relative_to(vault)))
        _assert(swept > 0, f"sweep must actually inspect artifacts under {vault}")
        _assert(not leaked, f"raw request text leaked into: {leaked}")
        _assert(needle in store.read_bytes(),
                "control: the sweep needle must be present in the store itself")
        print(f"PASS sweep_finds_request_text_only_in_the_private_store "
              f"({swept} artifacts swept)")
    finally:
        os.environ.pop("CLAUDE_KB_LOG_RAW_QUERY", None)
        gate.classify_gate = original
        _cleanup(tmp, conn)


def test_write_repairs_a_permissive_store_before_writing():
    """A store left world-readable — created before this rule, or by a stray
    umask — is tightened to 0600 *before* the first prompt byte reaches it,
    not after."""
    tmp, conn = _fresh_db()
    try:
        path = request_text_store.store_path(tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.chmod(path, 0o644)
        _run(conn, tmp, SENTINEL)
        _assert(stat.S_IMODE(path.stat().st_mode) == 0o600,
                f"permissive store must be repaired: {oct(stat.S_IMODE(path.stat().st_mode))}")
        _assert(_records(tmp)[-1]["request_text"] == SENTINEL,
                "and the record is written once it is private")
        print("PASS write_repairs_a_permissive_store_before_writing")
    finally:
        _cleanup(tmp, conn)


def test_write_fails_closed_when_the_mode_cannot_be_made_private():
    """Review round 1, item 1(a): asserting the mode is not the same as
    achieving it. `fchmod` is Unix-only and can fail outright — unsupported
    filesystem, foreign ownership — and the old code swallowed that and wrote
    anyway, putting cleartext prompts in a world-readable file. The mode is now
    verified on the open descriptor and the write abandoned when it does not
    hold. Losing one episode's text is the safe failure; leaking the prompt is
    not."""
    if not hasattr(os, "fchmod") or os.name != "posix":
        print("SKIP write_fails_closed_when_the_mode_cannot_be_made_private "
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

        _run(conn, tmp, SENTINEL)
        _assert(path.read_bytes() == b"",
                f"nothing may be written to a non-private store: "
                f"{path.read_bytes()[:200]!r}")
        _assert(stat.S_IMODE(path.stat().st_mode) == 0o644,
                "and we do not silently claim a mode we could not set")
        _assert(len(_gate_rows(tmp)) == 1,
                "the structural log must still fire when the text write is refused")
        print("PASS write_fails_closed_when_the_mode_cannot_be_made_private")
    finally:
        os.fchmod = real_fchmod
        _cleanup(tmp, conn)


def test_write_succeeds_when_an_existing_store_is_already_private():
    """The fail-closed check must not reject the file this module itself
    created on a previous call — appends to an existing 0600 store still work."""
    tmp, conn = _fresh_db()
    try:
        _run(conn, tmp, SENTINEL)
        _run(conn, tmp, SENTINEL + " second")
        records = _records(tmp)
        _assert(len(records) == 2, f"append to an existing 0600 store: {records}")
        mode = stat.S_IMODE(request_text_store.store_path(tmp).stat().st_mode)
        _assert(mode == 0o600, f"store must stay 0600 across appends: {oct(mode)}")
        print("PASS write_succeeds_when_an_existing_store_is_already_private")
    finally:
        _cleanup(tmp, conn)


def test_capture_still_works_where_mode_bits_are_not_a_permission_model():
    """Review round 2, item 1 (5241): on Windows CPython reports a writable
    regular file as 0o666 and `chmod` moves only the read-only bit, so no
    S_IMODE value can mean "owner only". An unconditional 0600 assertion is
    therefore unsatisfiable there and silently suppressed EVERY capture —
    V5 text availability would never have started on Windows.

    Simulated on POSIX by turning off the platform flag, so the regression runs
    on ordinary CI rather than only on a Windows host."""
    tmp, conn = _fresh_db()
    _prev = request_text_store._POSIX_MODE_SEMANTICS
    try:
        request_text_store._POSIX_MODE_SEMANTICS = False
        path = request_text_store.store_path(tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.chmod(path, 0o666)          # what Windows reports for a normal file
        _run(conn, tmp, SENTINEL)
        records = _records(tmp)
        _assert(len(records) == 1,
                f"capture must not be suppressed by an unenforceable mode: {records}")
        _assert(records[0]["request_text"] == SENTINEL, records[0])
        print("PASS capture_still_works_where_mode_bits_are_not_a_permission_model")
    finally:
        request_text_store._POSIX_MODE_SEMANTICS = _prev
        _cleanup(tmp, conn)


def test_non_regular_target_is_refused_on_every_platform():
    """The one guarantee that survives without POSIX modes: never append
    cleartext to something that is not a regular file. Checked directly,
    because it must hold on the platform where the mode check does not."""
    _prev = request_text_store._POSIX_MODE_SEMANTICS
    read_fd, write_fd = os.pipe()
    try:
        request_text_store._POSIX_MODE_SEMANTICS = False   # weakest platform
        try:
            request_text_store._assert_private(write_fd)
        except OSError as exc:
            _assert("not a regular file" in str(exc), str(exc))
        else:
            raise AssertionError("a non-regular target must be refused")
        print("PASS non_regular_target_is_refused_on_every_platform")
    finally:
        request_text_store._POSIX_MODE_SEMANTICS = _prev
        os.close(read_fd)
        os.close(write_fd)


def test_a_fifo_store_path_cannot_hang_the_gate():
    """`open(fifo, O_WRONLY)` blocks until a reader arrives. This runs inside
    the gate's response path, so a blocking open would stall the verdict — a
    logging concern escalating into a product outage. O_NONBLOCK makes it fail
    fast instead, and the failure is swallowed like any other."""
    if not hasattr(os, "mkfifo"):
        print("SKIP a_fifo_store_path_cannot_hang_the_gate (no mkfifo)")
        return
    tmp, conn = _fresh_db()
    try:
        path = request_text_store.store_path(tmp)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(path)
        started = time.monotonic()
        _run(conn, tmp, SENTINEL)          # must return, not block
        elapsed = time.monotonic() - started
        _assert(elapsed < 10, f"gate must not stall on a fifo store: {elapsed:.1f}s")
        _assert(len(_gate_rows(tmp)) == 1, "the structural log must still fire")
        print("PASS a_fifo_store_path_cannot_hang_the_gate")
    finally:
        try:
            request_text_store.store_path(tmp).unlink()
        except OSError:
            pass
        _cleanup(tmp, conn)


def test_paired_records_share_a_daily_file_across_the_utc_midnight_boundary():
    """Review round 1, item 1(b): both writers must derive their daily file
    from the ONE shared timestamp. Sampling the date independently lets a call
    that straddles midnight drop its two halves into different days, which
    breaks the one-day join the store exists to support.

    Simulated by freezing the shared stamp just before midnight while the
    ambient clock has already rolled over."""
    tmp, conn = _fresh_db()
    frozen_ts = "2026-01-01T23:59:59.999Z"
    _now, _today = log_utils.now_iso, log_utils._today_utc_date
    try:
        log_utils.now_iso = lambda: frozen_ts
        log_utils._today_utc_date = lambda: "2026-01-02"   # clock already rolled
        _run(conn, tmp, SENTINEL)
        vault = paths.project_dir(tmp)
        gate_day1 = vault / f"{gate.LOG_STREAM}-2026-01-01.log"
        gate_day2 = vault / f"{gate.LOG_STREAM}-2026-01-02.log"
        text_day1 = vault / f"{request_text_store.STREAM}-2026-01-01.jsonl"
        text_day2 = vault / f"{request_text_store.STREAM}-2026-01-02.jsonl"
        _assert(gate_day1.exists() and not gate_day2.exists(),
                "gate row must land on the timestamp's day, not the clock's")
        _assert(text_day1.exists() and not text_day2.exists(),
                "text record must land on the timestamp's day, not the clock's")
        row = json.loads(gate_day1.read_text(encoding="utf-8").strip())
        record = json.loads(text_day1.read_text(encoding="utf-8").strip())
        _assert(row["ts"] == frozen_ts == record["ts"], (row["ts"], record["ts"]))
        _assert(row["query_hash"] == record["query_hash"],
                f"pair must still join: {row['query_hash']} vs {record['query_hash']}")
        print("PASS paired_records_share_a_daily_file_across_the_utc_midnight_boundary")
    finally:
        log_utils.now_iso, log_utils._today_utc_date = _now, _today
        _cleanup(tmp, conn)


def _verdict_echoing_the_request(request):
    """A verdict whose uncovered claim repeats the request verbatim — the exact
    shape review round 1 flagged as a leak path."""
    return {
        "recommendation": "MODIFY",
        "summary": "s",
        "decision_chain": [],
        "evidence_nodes": [],
        "load_bearing_claims": [
            {"claim": request, "evidence_type": "none", "evidence_ref": None},
        ],
        "uncovered_claims": [{"claim": request, "gap_type": "unknowable"}],
        "backend": "stub",
        "prompt_chars": 10,
    }


def test_gate_log_omits_request_text_echoed_back_as_a_claim():
    """Review round 1, item 2: claim text is not a separate privacy class from
    request text — a classifier that repeats the request as an uncovered claim
    would have carried the whole prompt into gate.log. The earlier sweep ran
    with use_llm=False, so no claim ever existed to leak; this drives the
    classifier path through a stub instead."""
    tmp, conn = _fresh_db()
    original = gate.classify_gate
    try:
        gate.classify_gate = (
            lambda chain_assembly, **kw: _verdict_echoing_the_request(SENTINEL)
        )
        gate.run_gate(conn, SENTINEL, project_path=tmp, use_llm=True)
        line = (
            log_utils.today_log_path(gate.LOG_STREAM, tmp)
            .read_text(encoding="utf-8").strip().splitlines()[-1]
        )
        entry = json.loads(line)
        _assert(entry["uncovered_claim_count"] == 1,
                f"the structural count must survive: {entry}")
        _assert("uncovered_claim_texts" not in entry,
                f"claim text must not be emitted: {entry}")
        _assert("zqxjvbnm-sentinel-request-text" not in line,
                f"request text reached gate.log via a claim: {line[:400]}")
        # And the verbatim text is still captured where it belongs.
        _assert(_records(tmp)[-1]["request_text"] == SENTINEL,
                "the private store must still hold the verbatim request")
        print("PASS gate_log_omits_request_text_echoed_back_as_a_claim")
    finally:
        gate.classify_gate = original
        _cleanup(tmp, conn)


def test_committed_public_artifacts_carry_no_request_text_field():
    """The proof packet and shipped receipts are the public-safe surface. They
    must not gain a request-text field from this mission."""
    def iter_keys(value):
        if isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from iter_keys(item)
        elif isinstance(value, list):
            for item in value:
                yield from iter_keys(item)

    banned = {"request_text", "query_excerpt", "raw_request", "query_text"}
    for relative in ("proof/live_gate_receipt.json", "proof/results.json"):
        payload = json.loads((_ROOT / relative).read_text(encoding="utf-8"))
        leaked_fields = sorted(banned.intersection(iter_keys(payload)))
        _assert(not leaked_fields,
                f"{relative} must not carry fields {leaked_fields!r}")
    print("PASS committed_public_artifacts_carry_no_request_text_field")
