"""One-shot generator for tests/fixtures/v4_gate_log_corpus.jsonl — the
corpus-derived fixture behind the V4 counter tests (id=4626 item 4, fixture
discipline per priority 4114: never authored from belief).

Two strata, both derived from the real writer, never hand-typed:

1. LEGACY stratum — 12 real rows copied from the live gate.log corpus
   (gate-*.log files in the pinned vault) and deterministically sanitized:
   every node/workstream id, hash, session id, call id, and project slug is
   remapped through a first-seen table; shapes, key sets, enums, bools,
   counters, and list lengths are byte-preserved. These rows predate the
   citation capability (no surfaced_rejected_paths key) and pin the
   capability_missing path against the REAL historical format. Selection is
   deterministic: every row carrying the newest-schema keys, every
   skipped/errored row, then earliest-by-ts until 12.

2. CAPABILITY stratum — 9 rows emitted by the REAL pipeline: run_gate over
   throwaway fixture vaults with the model faked at the binary seam (the
   test_gate_backends pattern), covering cited×{MODIFY×2, DO_NOT_PROCEED,
   PROCEED, NEEDS_HUMAN_JUDGMENT}, surfaced-but-uncited, nothing-surfaced,
   skipped (use_llm=False), and parse-error calls. The live corpus cannot
   supply these rows: no installed runtime writes the citation keys yet
   (install gap, id=4626), so real-writer output is the nearest
   4114-compliant derivation. Sanitized with the same remap.

Run from the repo root on a machine with the live corpus:
    python3 tests/fixtures/gen_v4_gate_log_corpus.py
Regeneration is content-deterministic except ts/elapsed_ms/budget_count on
the capability stratum (wall-clock values from the real writer; the counter
ignores all three). The committed fixture is the review artifact.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
ROOT = TESTS.parent
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(ROOT / "src"))

import _isolation  # noqa: E402,F401  (must precede runtime modules)
import db          # noqa: E402
import embeddings  # noqa: E402
import gate        # noqa: E402
import log_utils   # noqa: E402

gate.ADVERSARY_ENABLED = False

LIVE_DIR = Path("/Users/nicomey/repos/latch-vault")
OUT = Path(__file__).resolve().parent / "v4_gate_log_corpus.jsonl"
LEGACY_COUNT = 12

# ---------------------------------------------------------------- sanitizer

_INT_MAP: dict[int, int] = {}
_STR_MAP: dict[tuple[str, str], str] = {}

_ID_LIST_KEYS = {
    "evidence_ids", "decision_chain", "abandoned_paths", "active_constraints",
    "current_direction", "seed_ids", "reached_workstream_ids",
    "surfaced_rejected_paths", "cited_rejected_paths",
}
_ID_SCALAR_KEYS = {
    "id", "workstream_id", "seed_id", "seed_workstream_id", "lane_group_id",
}


def _map_int(v):
    if v is None:
        return None
    v = int(v)
    if v not in _INT_MAP:
        _INT_MAP[v] = 9000 + len(_INT_MAP)
    return _INT_MAP[v]


def _map_str(kind: str, v):
    if v is None:
        return None
    key = (kind, str(v))
    if key not in _STR_MAP:
        k = sum(1 for existing in _STR_MAP if existing[0] == kind)
        if kind == "query_hash":
            _STR_MAP[key] = f"{k:012x}"
        elif kind == "session_id":
            _STR_MAP[key] = f"00000000-0000-4000-8000-{k:012d}"
        elif kind == "runtime_key":
            # 20-char shape like the real attestation key, clearly fake.
            _STR_MAP[key] = f"fx{k:018x}"
        elif kind == "proof_hex":
            # 64-char shape like the vault HMAC fingerprint/key_id.
            _STR_MAP[key] = f"fx{k:062x}"
        elif kind == "key_epoch":
            _STR_MAP[key] = f"fx-key-epoch-{k}"
        else:  # gate_call_id
            _STR_MAP[key] = f"fixturecall{k:04d}"
    return _STR_MAP[key]


def sanitize(entry: dict) -> dict:
    out = {}
    for k, v in entry.items():
        if k in _ID_LIST_KEYS and isinstance(v, list):
            out[k] = [_map_int(x) for x in v]
        elif k == "query_hash":
            out[k] = _map_str("query_hash", v)
        elif k == "session_id":
            out[k] = _map_str("session_id", v)
        elif k == "gate_call_id":
            out[k] = _map_str("gate_call_id", v)
        elif k == "project":
            out[k] = "-fixture-project"
        elif k in ("attestation", "runtime_attestation", "runtime_version"):
            # V1-runtime attestation material (adversarial-panel finding over
            # dd5f6f1): never let the live runtime key into the fixture.
            out[k] = _map_str("runtime_key", v)
        elif k == "key_epoch":
            out[k] = _map_str("key_epoch", v)
        elif k == "project_proof" and isinstance(v, dict):
            out[k] = {
                **v,
                "fingerprint": _map_str("proof_hex", v.get("fingerprint")),
                "key_id": _map_str("proof_hex", v.get("key_id")),
                "key_epoch": _map_str("key_epoch", v.get("key_epoch")),
            }
        elif k == "seeds" and isinstance(v, list):
            out[k] = [
                {**s, "id": _map_int(s.get("id")),
                 "workstream_id": _map_int(s.get("workstream_id"))}
                for s in v
            ]
        elif k == "chain_lane_contacts" and isinstance(v, list):
            out[k] = [
                {**c,
                 "seed_id": _map_int(c.get("seed_id")),
                 "seed_workstream_id": _map_int(c.get("seed_workstream_id")),
                 "lane_group_id": _map_int(c.get("lane_group_id")),
                 "reached_workstream_ids": [
                     _map_int(x) for x in c.get("reached_workstream_ids") or []
                 ]}
                for c in v
            ]
        else:
            out[k] = v
    return out


# ------------------------------------------------------------ legacy stratum

def legacy_rows() -> list[dict]:
    if not LIVE_DIR.is_dir():
        print(f"WARNING: live corpus dir missing ({LIVE_DIR}); "
              f"legacy stratum skipped", file=sys.stderr)
        return []
    rows = []
    for path in sorted(glob.glob(str(LIVE_DIR / "gate-*.log"))):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r.get("ts") or "")
    newest_schema = [r for r in rows if "abandoned_paths" in r]
    degraded = [
        r for r in rows
        if r not in newest_schema and (r.get("skipped") or r.get("error"))
    ]
    rest = [r for r in rows if r not in newest_schema and r not in degraded]
    picked = newest_schema + degraded + rest
    return [sanitize(r) for r in picked[:LEGACY_COUNT]]


# -------------------------------------------------------- capability stratum

def _ins(conn, kind, title, body, *, status="staging"):
    vec = embeddings.embed(f"{title}\n\n{body}")
    return db.insert_node(
        conn, kind=kind, title=title, body=body, status=status,
        embedding=embeddings.to_blob(vec),
    )


def _force_seed(conn, node_id):
    row = conn.execute(
        "SELECT id, kind, title, body, status, workstream_id FROM nodes "
        "WHERE id = ?", (node_id,),
    ).fetchone()
    hit = dict(row)
    hit["score"] = 1.0
    return lambda *a, **kw: [hit]


def _fake_claude(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env bash\ncat >/dev/null\n"
        "printf '%s\\n' \"$FAKE_GATE_RESPONSE\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _verdict(recommendation, cited, nid):
    return json.dumps({
        "recommendation": recommendation,
        "summary": "fixture verdict",
        "decision_chain": [nid], "abandoned_paths": [],
        "active_constraints": [], "current_direction": [nid],
        "risk_if_proceed": "", "better_next_action": "",
        "evidence_nodes": [nid], "cited_rejected_paths": cited,
        "load_bearing_claims": [],
    })


def _one_call(*, with_rejection: bool, response, use_llm: bool = True) -> dict:
    tmp = tempfile.mkdtemp(prefix="v4fix_")
    fake_dir = Path(tempfile.mkdtemp(prefix="v4fix_bin_"))
    conn = db.connect(tmp)
    orig_search = gate.search.hybrid_search
    old_claude = gate.CLAUDE_BIN
    old_resp = os.environ.get("FAKE_GATE_RESPONSE")
    try:
        nid = _ins(conn, "decision", "Queue engine chosen",
                   "Redis Streams adopted.", status="canonical")
        rid = None
        if with_rejection:
            rid = db.insert_rejected_path(
                conn, nid, option="in-process queue",
                reason="loses state across restarts",
            )
        gate.search.hybrid_search = _force_seed(conn, nid)
        gate.CLAUDE_BIN = str(_fake_claude(fake_dir / "claude"))
        os.environ["FAKE_GATE_RESPONSE"] = (
            response(rid, nid) if callable(response) else response
        )
        gate.run_gate(conn, "re-try the in-process queue",
                      project_path=tmp, use_llm=use_llm)
        line = (log_utils.today_log_path(gate.LOG_STREAM, tmp)
                .read_text(encoding="utf-8").strip().splitlines()[-1])
        return sanitize(json.loads(line))
    finally:
        gate.search.hybrid_search = orig_search
        gate.CLAUDE_BIN = old_claude
        if old_resp is None:
            os.environ.pop("FAKE_GATE_RESPONSE", None)
        else:
            os.environ["FAKE_GATE_RESPONSE"] = old_resp
        try:
            conn.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(fake_dir, ignore_errors=True)


def capability_rows() -> list[dict]:
    return [
        # cited + changed verdict (the V4 numerator)
        _one_call(with_rejection=True,
                  response=lambda rid, nid: _verdict("MODIFY", [rid], nid)),
        _one_call(with_rejection=True,
                  response=lambda rid, nid: _verdict("MODIFY", [rid], nid)),
        _one_call(with_rejection=True,
                  response=lambda rid, nid: _verdict("DO_NOT_PROCEED", [rid], nid)),
        # cited, verdict not changed under the declared rubric
        _one_call(with_rejection=True,
                  response=lambda rid, nid: _verdict("PROCEED", [rid], nid)),
        _one_call(with_rejection=True,
                  response=lambda rid, nid: _verdict(
                      "NEEDS_HUMAN_JUDGMENT", [rid], nid)),
        # surfaced but not cited
        _one_call(with_rejection=True,
                  response=lambda rid, nid: _verdict("MODIFY", [], nid)),
        # nothing surfaced
        _one_call(with_rejection=False,
                  response=lambda rid, nid: _verdict("PROCEED", [], nid)),
        # skipped (use_llm=False) — capability key present, row ineligible
        _one_call(with_rejection=True, response="unused", use_llm=False),
        # classifier parse error — surfaced logged, row ineligible
        _one_call(with_rejection=True, response="not json at all"),
    ]


def main() -> None:
    rows = legacy_rows() + capability_rows()
    with OUT.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == "__main__":
    main()
