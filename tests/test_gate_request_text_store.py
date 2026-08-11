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

# The gate.log row shape for a default (use_llm=False) call, pinned so this
# mission cannot widen it. Sourced from the shipped writer, not from the new
# code — a change here is a change to the correlator's input format.
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
    "reachable_count", "prompt_chars", "backend", "timed_out", "elapsed_ms",
    "budget_count", "load_bearing_claim_count", "uncovered_claim_count",
    "evidence_type_counts", "gap_type_counts",
})


def test_gate_log_schema_is_unchanged():
    """Item-2 acceptance: the structural record schema is exactly what it was.
    No request-text field, and no new field, leaks into the correlator's input."""
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


def test_gate_log_never_carries_request_text_under_any_setting():
    """The ``query_excerpt`` affordance is superseded (5141 item 1). Raw
    request text has exactly one home now, so fact 3091 holds unconditionally
    — including with the legacy opt-in forced on."""
    tmp, conn = _fresh_db()
    _prev = gate.LOG_RAW_QUERY
    try:
        for flag in (False, True):
            gate.LOG_RAW_QUERY = flag
            _run(conn, tmp, SENTINEL)
            raw = json.dumps(_gate_rows(tmp)[-1])
            _assert("query_excerpt" not in raw,
                    f"query_excerpt must be gone (LOG_RAW_QUERY={flag}): {raw[:400]}")
            _assert("zqxjvbnm-sentinel-request-text" not in raw,
                    f"raw request text must never reach gate.log "
                    f"(LOG_RAW_QUERY={flag}): {raw[:400]}")
        print("PASS gate_log_never_carries_request_text_under_any_setting")
    finally:
        gate.LOG_RAW_QUERY = _prev
        _cleanup(tmp, conn)


def test_sweep_finds_request_text_only_in_the_private_store():
    """Item-2 acceptance: an automated sweep of every artifact a run produces
    finds zero raw request text outside the 0600 store."""
    tmp, conn = _fresh_db()
    _prev = gate.LOG_RAW_QUERY
    try:
        gate.LOG_RAW_QUERY = True  # adversarial: legacy opt-in forced on
        _ins(conn, "decision", "Redis session cache", "Redis session cache body")
        _run(conn, tmp, SENTINEL, session_id="33333333-3333-3333-3333-333333333333",
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
        gate.LOG_RAW_QUERY = _prev
        _cleanup(tmp, conn)


def test_committed_public_artifacts_carry_no_request_text_field():
    """The proof packet and shipped receipts are the public-safe surface. They
    must not gain a request-text field from this mission."""
    for relative in ("proof/live_gate_receipt.json", "proof/results.json"):
        blob = (_ROOT / relative).read_text(encoding="utf-8")
        for banned in ("request_text", "query_excerpt", "raw_request", "query_text"):
            _assert(banned not in blob,
                    f"{relative} must not carry {banned!r}")
    print("PASS committed_public_artifacts_carry_no_request_text_field")
