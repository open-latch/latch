"""Smoke tests for the MCP-server-hosted embed listener.

Validates the daemon path used by the UserPromptSubmit hook to avoid the
~15s torch cold-load per subprocess. Spawns the listener in-process (no
subprocess, no real MCP stdio) and round-trips an embed call over loopback
TCP, then compares to a local embed of the same text.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from latch.retrieval import embeddings  # noqa: E402
from latch.mcp import mcp_broker  # noqa: E402
from latch.mcp import mcp_server  # noqa: E402
from latch.store import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _wait_for_disc(disc_path: Path, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if disc_path.exists():
            try:
                return json.loads(disc_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    raise TimeoutError(f"discovery file not written: {disc_path}")


@contextmanager
def _pinned_vault(path: str):
    saved_env = os.environ.get("LATCH_KB_DIR")
    saved_pin = paths._PINNED_DIR
    mcp_server.shutdown_runtime()
    # Resolve the logical scope through pytest's authenticated test root before
    # exercising explicit pin behavior.
    os.environ["LATCH_KB_DIR"] = str(paths.project_dir(path))
    paths._PINNED_DIR = False
    try:
        yield
    finally:
        mcp_server.shutdown_runtime()
        paths._PINNED_DIR = saved_pin
        if saved_env is None:
            os.environ.pop("LATCH_KB_DIR", None)
        else:
            os.environ["LATCH_KB_DIR"] = saved_env


def test_embed_remote_round_trips_against_local_embed():
    tmp = tempfile.mkdtemp(prefix="kb_embed_daemon_")
    with _pinned_vault(tmp):
        mcp_server._start_embed_listener(tmp)

        disc_path = mcp_broker.embed_discovery_path()
        meta = _wait_for_disc(disc_path)
        _assert(isinstance(meta.get("port"), int), "discovery missing port")
        _assert(isinstance(meta.get("token"), str) and len(meta["token"]) >= 16, "discovery missing token")

        text = "phase 1 latency fix smoke test"
        remote = embeddings.embed_remote(text, project_cwd=tmp, timeout=60.0)
        _assert(remote is not None, "embed_remote returned None against live daemon")
        _assert(remote.shape == (embeddings.DIM,), f"unexpected shape: {remote.shape}")

        local = embeddings.embed(text)
        _assert(np.allclose(remote, local, atol=1e-5), "remote vec diverged from local vec")


def test_embed_remote_returns_none_when_no_discovery_file():
    tmp = tempfile.mkdtemp(prefix="kb_embed_no_daemon_")
    with _pinned_vault(tmp):
        paths.ensure_project_dir(tmp)
        out = embeddings.embed_remote("some prompt", project_cwd=tmp, timeout=0.5)
        _assert(out is None, "expected None when discovery file missing")


def test_embed_remote_returns_none_when_daemon_dead():
    tmp = tempfile.mkdtemp(prefix="kb_embed_dead_daemon_")
    with _pinned_vault(tmp):
        paths.ensure_project_dir(tmp)
        disc_path = mcp_broker.embed_discovery_path()
    # Bind a port, immediately close — discovery file points at a port
    # nobody is listening on. Hook should fall through to None within
    # the connect timeout, not raise.
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        _, dead_port = s.getsockname()
        s.close()
        disc_path.write_text(json.dumps({
            "runtime_key": mcp_broker.RUNTIME_KEY,
            "host": "127.0.0.1", "port": dead_port, "token": "x" * 32,
            "pid": 0, "started_at": "0",
        }), encoding="utf-8")
        out = embeddings.embed_remote("anything", project_cwd=tmp, timeout=0.5)
        _assert(out is None, "expected None when daemon is dead")


def test_embed_remote_rejects_bad_token():
    tmp = tempfile.mkdtemp(prefix="kb_embed_bad_token_")
    with _pinned_vault(tmp):
        mcp_server._start_embed_listener(tmp)
        disc_path = mcp_broker.embed_discovery_path()
        meta = _wait_for_disc(disc_path)

    # Tamper with the token, write back, attempt a remote embed: server
    # should reject and embed_remote() should return None.
        meta["token"] = "0" * 32
        disc_path.write_text(json.dumps(meta), encoding="utf-8")
        out = embeddings.embed_remote("blocked", project_cwd=tmp, timeout=5.0)
        _assert(out is None, "bad token must not yield a vector")


if __name__ == "__main__":
    test_embed_remote_returns_none_when_no_discovery_file()
    print("OK: no-discovery returns None")
    test_embed_remote_returns_none_when_daemon_dead()
    print("OK: dead-daemon returns None")
    test_embed_remote_rejects_bad_token()
    print("OK: bad-token rejected")
    test_embed_remote_round_trips_against_local_embed()
    print("OK: round-trip matches local embed")
