"""Slim embedder: ONNX MiniLM via onnxruntime + HF tokenizers.

Lossless w.r.t. sentence-transformers/all-MiniLM-L6-v2 (~1e-5 cosine). No
torch, no sentence-transformers, no HuggingFace runtime calls. Public surface
preserved 1:1 with the legacy torch embedder (embed / embed_batch / to_blob /
from_blob / cosine_topk / embed_remote / DIM / DISCOVERY_FILE).
"""
from __future__ import annotations

import json as _json
import math as _math
import os as _os
import socket as _socket
import threading as _threading
import time as _time
from dataclasses import dataclass as _dataclass
from typing import TYPE_CHECKING as _TYPE_CHECKING

import numpy as np

from latch.store import paths as _paths

if _TYPE_CHECKING:
    from latch.mcp.mcp_broker import EmbedDiscoveryResult

DIM = 384
DISCOVERY_FILE = "embed.sock.json"
DEFAULT_REMOTE_TIMEOUT = 2.0
MAX_SEQ_LEN = 256  # matches sentence-transformers' configured max_seq_length for all-MiniLM-L6-v2 (model card says 512 positional max, but the s-t pipeline caps at 256)

_VENDOR_DIR = _paths.KB_ROOT / "vendor"
_MODEL_PATH = _VENDOR_DIR / "model.onnx"
_TOKENIZER_PATH = _VENDOR_DIR / "tokenizer.json"

_LOAD_TIMEOUT = 60.0

_ort = None
_Tokenizer = None
_SESSION = None
_TOKENIZER = None
_LOAD_LOCK = _threading.Lock()


def is_loaded() -> bool:
    """Whether this process currently owns a ready heavyweight embedder."""
    return _SESSION is not None and _TOKENIZER is not None


def _ensure_loaded() -> None:
    """Lazy-init the ONNX session and tokenizer. Mirrors the torch loader's
    deadlock-guard: if the lock can't be acquired in _LOAD_TIMEOUT seconds,
    raise rather than block every caller indefinitely."""
    global _ort, _Tokenizer, _SESSION, _TOKENIZER
    if _SESSION is not None and _TOKENIZER is not None:
        return
    if not _LOAD_LOCK.acquire(timeout=_LOAD_TIMEOUT):
        raise TimeoutError(
            f"_LOAD_LOCK held > {_LOAD_TIMEOUT}s; loader likely deadlocked"
        )
    try:
        if _ort is None:
            import onnxruntime as _onnxruntime
            _ort = _onnxruntime
        if _Tokenizer is None:
            from tokenizers import Tokenizer as _TokenizerClass
            _Tokenizer = _TokenizerClass
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


@_dataclass(frozen=True)
class RemoteEmbedResult:
    """Observed outcome; failures never assert that the owner is dead."""

    status: str
    vector: np.ndarray | None = None


def embed_remote_result(
    text: str,
    project_cwd: "str | _os.PathLike",
    timeout: float = DEFAULT_REMOTE_TIMEOUT,
    *,
    discovery: "EmbedDiscoveryResult | None" = None,
) -> RemoteEmbedResult:
    """Call the pinned vault's shared embed listener over loopback TCP.

    The shared MCP daemon owns the model; hook subprocesses only see vectors.
    ``timeout`` is one elapsed-time allowance covering discovery, connection,
    sending, receiving, and decoding. It must be finite. A response arriving
    after that deadline is rejected. Optional ``discovery`` is a validated
    broker inspection from the same call, allowing lightweight hook preflight
    to avoid repeating discovery after importing this NumPy-backed module.
    """
    # Discovery is runtime-keyed so blue/green daemons cannot overwrite each
    # other's embed endpoint.  Import locally to keep module initialization
    # ordering simple (mcp_broker itself remains stdlib-only).
    from latch.mcp import mcp_broker
    timeout = float(timeout)
    if not _math.isfinite(timeout):
        raise ValueError("remote embedding timeout must be finite")
    deadline = _time.monotonic() + timeout

    def remaining() -> float:
        seconds = deadline - _time.monotonic()
        if seconds <= 0:
            raise TimeoutError("remote embedding budget exhausted")
        return seconds

    try:
        remaining()
        inspected = discovery or mcp_broker.inspect_live_embed_discovery()
        remaining()
    except TimeoutError:
        return RemoteEmbedResult("local_budget_exhausted")
    if inspected.metadata is None:
        return RemoteEmbedResult(inspected.status)
    meta = inspected.metadata
    host, port, token = meta.get("host"), meta.get("port"), meta.get("token")
    try:
        with _socket.create_connection((host, int(port)), timeout=remaining()) as s:
            payload = _json.dumps({"op": "embed", "text": text, "token": token}).encode("utf-8")
            s.settimeout(remaining())
            s.sendall(payload + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                s.settimeout(remaining())
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 4 * 1024 * 1024:
                    return RemoteEmbedResult("rpc_failed")
        remaining()
        line = bytes(buf).split(b"\n", 1)[0]
        if not line:
            return RemoteEmbedResult("rpc_failed")
        resp = _json.loads(line.decode("utf-8"))
        if not isinstance(resp, dict):
            return RemoteEmbedResult("rpc_failed")
        if resp.get("error") == "bad_token":
            return RemoteEmbedResult("authentication_rejected")
        if "error" in resp:
            return RemoteEmbedResult("rpc_failed")
        vec = resp.get("vec")
        if not isinstance(vec, list) or len(vec) != DIM:
            return RemoteEmbedResult("rpc_failed")
        vector = np.asarray(vec, dtype=np.float32)
        if vector.shape != (DIM,) or not np.isfinite(vector).all():
            return RemoteEmbedResult("rpc_failed")
        remaining()
        return RemoteEmbedResult("ready", vector)
    except TimeoutError:
        return RemoteEmbedResult("local_budget_exhausted")
    except (OSError, TypeError, ValueError):
        return RemoteEmbedResult("rpc_failed")


def embed_remote(
    text: str,
    project_cwd: "str | _os.PathLike",
    timeout: float = DEFAULT_REMOTE_TIMEOUT,
) -> "np.ndarray | None":
    """Compatibility API: return the remote vector, or None on failure."""
    try:
        return embed_remote_result(text, project_cwd, timeout).vector
    except (TypeError, ValueError):
        return None
