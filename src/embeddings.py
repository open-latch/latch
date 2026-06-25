"""Slim embedder: ONNX MiniLM via onnxruntime + HF tokenizers.

Lossless w.r.t. sentence-transformers/all-MiniLM-L6-v2 (~1e-5 cosine). No
torch, no sentence-transformers, no HuggingFace runtime calls. Public surface
preserved 1:1 with the legacy torch embedder (embed / embed_batch / to_blob /
from_blob / cosine_topk / embed_remote / DIM / DISCOVERY_FILE).
"""
from __future__ import annotations

import json as _json
import os as _os
import socket as _socket
import threading as _threading

import numpy as np
import onnxruntime as _ort
from tokenizers import Tokenizer as _Tokenizer

import paths as _paths

DIM = 384
DISCOVERY_FILE = "embed.sock.json"
DEFAULT_REMOTE_TIMEOUT = 2.0
MAX_SEQ_LEN = 256  # matches sentence-transformers' configured max_seq_length for all-MiniLM-L6-v2 (model card says 512 positional max, but the s-t pipeline caps at 256)

_VENDOR_DIR = _paths.KB_ROOT / "vendor"
_MODEL_PATH = _VENDOR_DIR / "model.onnx"
_TOKENIZER_PATH = _VENDOR_DIR / "tokenizer.json"

_LOAD_TIMEOUT = 60.0

_SESSION: "_ort.InferenceSession | None" = None
_TOKENIZER: "_Tokenizer | None" = None
_LOAD_LOCK = _threading.Lock()


def _ensure_loaded() -> None:
    """Lazy-init the ONNX session and tokenizer. Mirrors the torch loader's
    deadlock-guard: if the lock can't be acquired in _LOAD_TIMEOUT seconds,
    raise rather than block every caller indefinitely."""
    global _SESSION, _TOKENIZER
    if _SESSION is not None and _TOKENIZER is not None:
        return
    if not _LOAD_LOCK.acquire(timeout=_LOAD_TIMEOUT):
        raise TimeoutError(
            f"_LOAD_LOCK held > {_LOAD_TIMEOUT}s; loader likely deadlocked"
        )
    try:
        if _SESSION is None:
            _SESSION = _ort.InferenceSession(
                str(_MODEL_PATH), providers=["CPUExecutionProvider"]
            )
        if _TOKENIZER is None:
            tok = _Tokenizer.from_file(str(_TOKENIZER_PATH))
            # Optimum's export bakes a fixed-length padding strategy (e.g. 128)
            # into tokenizer.json. Disable it — we pad to batch-max ourselves
            # below so attention_mask correctly marks PAD positions as 0.
            tok.no_padding()
            tok.enable_truncation(max_length=MAX_SEQ_LEN)
            _TOKENIZER = tok
    finally:
        _LOAD_LOCK.release()


def embed(text: str) -> np.ndarray:
    return embed_batch([text])[0]


def embed_batch(texts: list[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    _ensure_loaded()
    assert _TOKENIZER is not None and _SESSION is not None  # for type checkers
    encodings = _TOKENIZER.encode_batch(texts)
    max_len = max(len(e.ids) for e in encodings)
    input_ids = np.array(
        [e.ids + [0] * (max_len - len(e.ids)) for e in encodings],
        dtype=np.int64,
    )
    attention_mask = np.array(
        [[1] * len(e.ids) + [0] * (max_len - len(e.ids)) for e in encodings],
        dtype=np.int64,
    )
    token_type_ids = np.zeros_like(input_ids)

    outputs = _SESSION.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )
    last_hidden = outputs[0]  # (batch, seq, hidden)

    # Mean-pool over attention mask, then L2-normalize. Mirrors
    # sentence-transformers' Pooling(mode='mean') + Normalize() head.
    mask = attention_mask[..., None].astype(np.float32)
    pooled = (last_hidden * mask).sum(axis=1) / mask.sum(axis=1).clip(min=1e-9)
    norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
    return (pooled / norms).astype(np.float32)


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def cosine_topk(
    query: np.ndarray, vectors: np.ndarray, k: int
) -> tuple[np.ndarray, np.ndarray]:
    """Vectors are assumed L2-normalized."""
    if vectors.shape[0] == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
    scores = vectors @ query
    k = min(k, vectors.shape[0])
    idx = np.argpartition(-scores, k - 1)[:k]
    idx = idx[np.argsort(-scores[idx])]
    return idx, scores[idx]


def embed_remote(
    text: str,
    project_cwd: "str | _os.PathLike",
    timeout: float = DEFAULT_REMOTE_TIMEOUT,
) -> "np.ndarray | None":
    """Call the per-project MCP server's embed listener over loopback TCP.

    Identical wire protocol to the torch embedder — the daemon is what owns
    the model now, callers just see vec lists. Returns None on any failure.
    """
    from paths import project_dir  # local import; avoid circular
    disc = project_dir(project_cwd) / DISCOVERY_FILE
    if not disc.exists():
        return None
    try:
        meta = _json.loads(disc.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    host, port, token = meta.get("host"), meta.get("port"), meta.get("token")
    if not host or not port or not token:
        return None
    try:
        with _socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            payload = _json.dumps({"op": "embed", "text": text, "token": token}).encode("utf-8")
            s.sendall(payload + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 4 * 1024 * 1024:
                    return None
        line = bytes(buf).split(b"\n", 1)[0]
        if not line:
            return None
        resp = _json.loads(line.decode("utf-8"))
        vec = resp.get("vec")
        if not isinstance(vec, list) or len(vec) != DIM:
            return None
        return np.asarray(vec, dtype=np.float32)
    except (OSError, _socket.timeout, ValueError):
        return None
