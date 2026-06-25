"""Unit tests for the kb_update claim-change guard (src/verify.py).

Spec: KB id=1175 (enforces policy id=1174). The guard is a NUDGE, not a block:
``kb_update`` on a canonical fact/decision whose body edit materially shifts the
embedding (and does not merely append) surfaces a ``claim_change_hint`` steering
the agent toward ``kb_correct_plan`` so the decision-change transition stays
auditable.

NOTE (spec caveat): unlike test_verify.py, these tests must NOT reuse the no-op
``to_blob=None`` embedder stub — the guard needs REAL cosines. ``compute_claim_
change_hint`` is a pure predicate, so we construct small normalized vectors
directly (no model cold-load, no embedder monkeypatch) and pass them in. The
embeddings module is touched only for to_blob/from_blob round-tripping, which is
pure numpy.

Coverage:
- claim reversal on canonical fact → hint fires;
- typo edit (cosine ~1) → no hint;
- banner append (old body substring-preserved) → no hint even if cosine dips;
- workstream / progress kind → exempt;
- staging fact → exempt;
- title-only / status-only edit (body unchanged or None) → exempt;
- dimension mismatch → no hint, no raise;
- record_claim_change emits a structural-only claim_change.log row;
- hint_fired flag is True on a firing edit, False (but cosine recorded) on a
  non-claim kind.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

import embeddings  # noqa: E402
import log_utils  # noqa: E402
import paths  # noqa: E402
import verify  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _vec(*xs):
    return np.asarray(xs, dtype=np.float32)


def _blob(*xs):
    """A stored-embedding blob from a (normalized) vector, via the real
    to_blob/from_blob round-trip used by the production path."""
    return embeddings.to_blob(_vec(*xs))


# Two orthogonal unit vectors → cosine 0 (well below threshold).
_OLD_BLOB_FAR = _blob(1.0, 0.0)
_NEW_VEC_FAR = _vec(0.0, 1.0)
# Near-identical unit vectors → cosine ~0.995 (well above threshold).
_OLD_BLOB_NEAR = _blob(1.0, 0.0)
_NEW_VEC_NEAR = _vec(0.995, 0.0998)  # ~unit, dot with (1,0) = 0.995


# ---------- compute_claim_change_hint: firing ----------

def test_hint_fires_on_claim_reversal_canonical_fact():
    hint = verify.compute_claim_change_hint(
        node_id=42, kind="fact", status="canonical",
        old_embedding_blob=_OLD_BLOB_FAR,
        old_body="The window is 20 days.",
        new_body="The window is minutes-to-hours.",
        new_vec=_NEW_VEC_FAR,
    )
    _assert(hint is not None, "claim reversal on canonical fact must fire")
    _assert(hint["node_id"] == 42, hint)
    _assert(hint["suggestion"] == "kb_correct_plan", hint)
    _assert(hint["cosine"] < verify.CLAIM_CHANGE_COSINE_THRESHOLD, hint)
    print("PASS hint_fires_on_claim_reversal_canonical_fact")


def test_hint_fires_on_canonical_decision():
    hint = verify.compute_claim_change_hint(
        node_id=7, kind="decision", status="canonical",
        old_embedding_blob=_OLD_BLOB_FAR,
        old_body="We ship independent-rho per expiry.",
        new_body="We ship shared-rho across product expiries.",
        new_vec=_NEW_VEC_FAR,
    )
    _assert(hint is not None and hint["kind"] == "decision", hint)
    print("PASS hint_fires_on_canonical_decision")


# ---------- compute_claim_change_hint: exemptions ----------

def test_no_hint_on_typo_high_cosine():
    """A typo fix changes the body (not a substring of the old) but barely moves
    the embedding → cosine above threshold → no hint."""
    hint = verify.compute_claim_change_hint(
        node_id=42, kind="fact", status="canonical",
        old_embedding_blob=_OLD_BLOB_NEAR,
        old_body="The quikc brown fox.",
        new_body="The quick brown fox.",
        new_vec=_NEW_VEC_NEAR,
    )
    _assert(hint is None, f"high-cosine typo edit must NOT fire: {hint}")
    print("PASS no_hint_on_typo_high_cosine")


def test_no_hint_on_banner_append_substring_preserved():
    """Appending a reconciliation banner keeps the old claim intact (old body is
    a substring of the new) → exempt even when the embedding shift is large."""
    old = "X is true."
    hint = verify.compute_claim_change_hint(
        node_id=42, kind="fact", status="canonical",
        old_embedding_blob=_OLD_BLOB_FAR,
        old_body=old,
        new_body=old + "\n\n**Update:** reconciled by id=999.",
        new_vec=_NEW_VEC_FAR,  # deliberately far — cond 5 must short-circuit first
    )
    _assert(hint is None, f"banner append must be exempt (cond 5): {hint}")
    print("PASS no_hint_on_banner_append_substring_preserved")


def test_no_hint_on_non_claim_kinds():
    for kind in ("workstream", "progress", "entity", "preference", "open_question"):
        hint = verify.compute_claim_change_hint(
            node_id=1, kind=kind, status="canonical",
            old_embedding_blob=_OLD_BLOB_FAR,
            old_body="old summary", new_body="totally different summary",
            new_vec=_NEW_VEC_FAR,
        )
        _assert(hint is None, f"kind={kind} must be exempt: {hint}")
    print("PASS no_hint_on_non_claim_kinds")


def test_no_hint_on_staging_fact():
    hint = verify.compute_claim_change_hint(
        node_id=1, kind="fact", status="staging",
        old_embedding_blob=_OLD_BLOB_FAR,
        old_body="old", new_body="completely different",
        new_vec=_NEW_VEC_FAR,
    )
    _assert(hint is None, f"staging fact must be exempt (v1 scope): {hint}")
    print("PASS no_hint_on_staging_fact")


def test_no_hint_on_body_unchanged_or_none():
    # body unchanged (title-only / status-only edit reaches compute with
    # new_body == old_body)
    h1 = verify.compute_claim_change_hint(
        node_id=1, kind="fact", status="canonical",
        old_embedding_blob=_OLD_BLOB_FAR,
        old_body="same", new_body="same", new_vec=_NEW_VEC_FAR,
    )
    _assert(h1 is None, f"unchanged body must not fire: {h1}")
    # new_body None
    h2 = verify.compute_claim_change_hint(
        node_id=1, kind="fact", status="canonical",
        old_embedding_blob=_OLD_BLOB_FAR,
        old_body="x", new_body=None, new_vec=_NEW_VEC_FAR,
    )
    _assert(h2 is None, f"None new_body must not fire: {h2}")
    print("PASS no_hint_on_body_unchanged_or_none")


def test_no_hint_on_dimension_mismatch_no_raise():
    """A stored embedding of a different dimensionality than the new vector must
    yield None (cosine undefined) without raising in the hot path."""
    hint = verify.compute_claim_change_hint(
        node_id=1, kind="fact", status="canonical",
        old_embedding_blob=_blob(1.0, 0.0, 0.0),  # 3-dim
        old_body="old", new_body="different",
        new_vec=_vec(0.0, 1.0),  # 2-dim
    )
    _assert(hint is None, f"dimension mismatch must yield None: {hint}")
    print("PASS no_hint_on_dimension_mismatch_no_raise")


def test_missing_old_embedding_no_raise():
    hint = verify.compute_claim_change_hint(
        node_id=1, kind="fact", status="canonical",
        old_embedding_blob=None,
        old_body="old", new_body="different", new_vec=_NEW_VEC_FAR,
    )
    _assert(hint is None, f"missing old embedding must yield None: {hint}")
    print("PASS missing_old_embedding_no_raise")


# ---------- record_claim_change: telemetry ----------

def _read_claim_change_rows(tmp):
    path = log_utils.today_log_path(verify.LOG_STREAM_CLAIM_CHANGE, tmp)
    if not path.exists():
        return []
    return [
        json.loads(l)
        for l in path.read_text(encoding="utf-8").splitlines() if l.strip()
    ]


def _wipe_project_dir(tmp):
    proj_dir = paths.project_dir(tmp)
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)


def test_record_claim_change_emits_structural_only_log():
    tmp = tempfile.mkdtemp(prefix="kb_claim_change_test_")
    try:
        hint = verify.record_claim_change(
            node_id=42, kind="fact", status="canonical",
            old_embedding_blob=_OLD_BLOB_FAR,
            old_body="secret old claim body",
            new_body="secret new contradicting body",
            new_vec=_NEW_VEC_FAR,
            project_path=tmp, session_id="sess-cc",
        )
        _assert(hint is not None, "firing edit should return a hint")
        rows = _read_claim_change_rows(tmp)
        _assert(len(rows) == 1, rows)
        r = rows[0]
        # common header
        for key in ("ts", "project", "session_id", "event_type"):
            _assert(key in r, f"missing header field {key!r}: {r}")
        _assert(r["event_type"] == verify.LOG_STREAM_CLAIM_CHANGE, r)
        _assert(r["session_id"] == "sess-cc", r)
        # structural fields present
        for key in ("node_id", "kind", "status", "cosine", "body_len_before",
                    "body_len_after", "old_text_preserved", "hint_fired"):
            _assert(key in r, f"missing structural field {key!r}: {r}")
        _assert(r["hint_fired"] is True, r)
        _assert(r["old_text_preserved"] is False, r)
        _assert(r["body_len_before"] == len("secret old claim body"), r)
        # structural-only: no titles, bodies, or raw text anywhere
        forbidden = {"title", "body", "old_body", "new_body", "node_title",
                     "node_body", "description", "reason", "prompt", "raw_prompt"}
        leaked = set(r.keys()) & forbidden
        _assert(leaked == set(), f"forbidden fields leaked: {leaked} in {r}")
        blob = json.dumps(r)
        _assert("secret" not in blob, f"raw text leaked into log row: {r}")
        print("PASS record_claim_change_emits_structural_only_log")
    finally:
        _wipe_project_dir(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def test_record_claim_change_baseline_row_for_non_claim_kind():
    """A non-claim kind still emits a telemetry row (cosine recorded for the
    baseline distribution) but hint_fired is False and no hint is returned."""
    tmp = tempfile.mkdtemp(prefix="kb_claim_change_test_")
    try:
        hint = verify.record_claim_change(
            node_id=338, kind="workstream", status="canonical",
            old_embedding_blob=_OLD_BLOB_FAR,
            old_body="old workstream summary",
            new_body="freshened workstream summary",
            new_vec=_NEW_VEC_FAR,
            project_path=tmp, session_id="sess-ws",
        )
        _assert(hint is None, "workstream freshening must not return a hint")
        rows = _read_claim_change_rows(tmp)
        _assert(len(rows) == 1, rows)
        r = rows[0]
        _assert(r["hint_fired"] is False, r)
        _assert(r["kind"] == "workstream", r)
        _assert(r["cosine"] is not None, "baseline cosine must still be recorded")
        print("PASS record_claim_change_baseline_row_for_non_claim_kind")
    finally:
        _wipe_project_dir(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} claim-change-guard tests passed.")


if __name__ == "__main__":
    _run_all()
