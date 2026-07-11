"""Offline ONNX parity gate with an optional live reference lane.

The required default lane compares a checked-in public corpus with frozen
sentence-transformers reference vectors and needs no model download. Set
``CLAUDE_KB_PARITY_DB`` to an explicit populated ``kb.db`` to run the optional
live-corpus lane (which requires sentence-transformers to be installed).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import numpy as np  # noqa: E402

import embeddings  # noqa: E402
N_SAMPLES = 1000
TOL = 1e-5
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "embedder_parity_corpus.txt"
REFERENCE_PATH = Path(__file__).resolve().parent / "fixtures" / "embedder_parity_reference.npy"


def _fixture_bodies() -> list[str]:
    bodies = [
        line.strip()
        for line in FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not bodies:
        raise RuntimeError(f"public parity fixture is empty: {FIXTURE_PATH}")
    return bodies


def _database_bodies(path: Path) -> list[str]:
    if not path.is_file():
        raise RuntimeError(f"CLAUDE_KB_PARITY_DB is not a readable file: {path}")
    conn = sqlite3.connect(str(path))
    try:
        bodies = [
            row[0]
            for row in conn.execute(
                f"SELECT body FROM nodes "
                f"WHERE body IS NOT NULL AND length(body) > 50 LIMIT {N_SAMPLES}"
            )
        ]
    finally:
        conn.close()
    if not bodies:
        raise RuntimeError(f"explicit parity database contains no usable node bodies: {path}")
    return bodies


def parity_corpus() -> tuple[str, list[str]]:
    override = (os.environ.get("CLAUDE_KB_PARITY_DB") or "").strip()
    if override:
        path = Path(override).expanduser().resolve()
        return str(path), _database_bodies(path)
    return str(FIXTURE_PATH), _fixture_bodies()


def _reference_vectors(source: str, bodies: list[str]) -> np.ndarray:
    if source == str(FIXTURE_PATH):
        vectors = np.load(REFERENCE_PATH, allow_pickle=False).astype(np.float32)
        if vectors.shape != (len(bodies), 384):
            raise RuntimeError(
                f"frozen parity reference shape {vectors.shape} does not match "
                f"{len(bodies)} fixture rows"
            )
        return vectors
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:  # pragma: no cover - explicit optional lane
        raise RuntimeError(
            "CLAUDE_KB_PARITY_DB requires the optional sentence-transformers dependency"
        ) from exc
    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    return model.encode(
        bodies, normalize_embeddings=True, convert_to_numpy=True, batch_size=32
    ).astype(np.float32)


def test_onnx_matches_frozen_or_live_reference() -> None:
    source, bodies = parity_corpus()
    print(f"Loaded {len(bodies)} bodies from {source}")

    v_reference = _reference_vectors(source, bodies)
    print(f"reference:            {v_reference.shape} dtype={v_reference.dtype}")

    v_onnx = embeddings.embed_batch(bodies)
    print(f"onnx:                 {v_onnx.shape} dtype={v_onnx.dtype}")

    cosines = (v_reference * v_onnx).sum(axis=1)
    abs_diff = np.abs(v_reference - v_onnx)
    abs_max_diff = float(abs_diff.max())
    print(
        f"Cosine: min={cosines.min():.6f} mean={cosines.mean():.6f} "
        f"max={cosines.max():.6f}"
    )
    print(
        f"Element-wise abs diff: max={abs_max_diff:.2e} "
        f"mean={float(abs_diff.mean()):.2e}"
    )
    assert abs_max_diff < TOL, (
        f"PARITY FAILED: max element-wise abs diff {abs_max_diff:.2e} "
        f">= tol {TOL:.0e}"
    )


if __name__ == "__main__":
    test_onnx_matches_frozen_or_live_reference()
