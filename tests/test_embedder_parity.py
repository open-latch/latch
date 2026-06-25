"""ONNX vs sentence-transformers parity gate.

Pulls up to N=1000 real bodies from the live kb.db, embeds both ways, asserts
cosine agreement at element-wise abs diff < 1e-5. The live kb.db IS the
parity corpus per [[onnx_minilm_parity_test]] id=837 / [[phase_5_prep]] id=1041.

Run from the claude_kb repo root:
    python tests/test_embedder_parity.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

import embeddings  # noqa: E402  (canonical ONNX embedder post-Phase-5 swap)
import paths  # noqa: E402
from sentence_transformers import SentenceTransformer  # noqa: E402


N_SAMPLES = 1000
TOL = 1e-5


def _find_live_kb_db() -> Path:
    """Use any project's kb.db as the parity corpus.

    Prefer the project pointed at by $CLAUDE_KB_PARITY_DB if set; otherwise
    pick the largest kb.db under projects/ (proxy for "real, populated").
    """
    import os as _os
    override = _os.environ.get("CLAUDE_KB_PARITY_DB")
    if override:
        return Path(override)
    candidates = sorted(
        (p for p in paths.PROJECTS_ROOT.glob("*/kb.db") if p.is_file()),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    if not candidates:
        raise RuntimeError(
            f"no kb.db found under {paths.PROJECTS_ROOT}; populate one project before running parity"
        )
    return candidates[0]


KB_DB = _find_live_kb_db()
conn = sqlite3.connect(str(KB_DB))
bodies = [
    row[0]
    for row in conn.execute(
        f"SELECT body FROM nodes WHERE body IS NOT NULL AND length(body) > 50 LIMIT {N_SAMPLES}"
    )
]
conn.close()
print(f"Loaded {len(bodies)} bodies from {KB_DB}")

st_model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
v_st = st_model.encode(
    bodies, normalize_embeddings=True, convert_to_numpy=True, batch_size=32
).astype(np.float32)
print(f"sentence-transformers: {v_st.shape} dtype={v_st.dtype}")

v_onnx = embeddings.embed_batch(bodies)
print(f"onnx:                 {v_onnx.shape} dtype={v_onnx.dtype}")

cosines = (v_st * v_onnx).sum(axis=1)
abs_diff = np.abs(v_st - v_onnx)
abs_max_diff = float(abs_diff.max())
print(
    f"Cosine: min={cosines.min():.6f} mean={cosines.mean():.6f} max={cosines.max():.6f}"
)
print(
    f"Element-wise abs diff: max={abs_max_diff:.2e} mean={float(abs_diff.mean()):.2e}"
)

if abs_max_diff >= TOL:
    raise AssertionError(
        f"PARITY FAILED: max element-wise abs diff {abs_max_diff:.2e} >= tol {TOL:.0e}"
    )
print(f"PARITY GATE PASSED (max diff {abs_max_diff:.2e} < {TOL:.0e})")
