"""Smoke tests for the MCP-server-hosted embed listener.

Validates the daemon path used by the UserPromptSubmit hook to avoid the
~15s torch cold-load per subprocess. Spawns the listener in-process (no
subprocess, no real MCP stdio) and round-trips an embed call over loopback
TCP, then compares to a local embed of the same text.
"""
from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import embeddings  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402


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


def test_embed_remote_round_trips_against_local_embed():
    tmp = tempfile.mkdtemp(prefix="kb_embed_daemon_")
    mcp_server._start_embed_listener(tmp)

    disc_path = paths.project_dir(tmp) / embeddings.DISCOVERY_FILE
    meta = _wait_for_disc(disc_path)
    _assert(isinstance(meta.get("port"), int), "discovery missing port")
    _assert(isinstance(meta.get("token"), str) and len(meta["token"]) >= 16, "discovery missing token")

    text = "phase 1 latency fix smoke test"
    # First call may block on the daemon's lazy model load. Give it room;
    # the production hook path uses a 2s timeout but expects the pre-warm
    # thread to have finished long before any real prompt arrives.
    remote = embeddings.embed_remote(text, project_cwd=tmp, timeout=60.0)
    _assert(remote is not None, "embed_remote returned None against live daemon")
    _assert(remote.shape == (embeddings.DIM,), f"unexpected shape: {remote.shape}")

    local = embeddings.embed(text)
    _assert(np.allclose(remote, local, atol=1e-5), "remote vec diverged from local vec")


def test_embed_remote_returns_none_when_no_discovery_file():
    tmp = tempfile.mkdtemp(prefix="kb_embed_no_daemon_")
    # Project dir exists but no embed.sock.json — hook path should bail
    # cleanly without raising and without paying the cold-load.
    paths.ensure_project_dir(tmp)
    out = embeddings.embed_remote("some prompt", project_cwd=tmp, timeout=0.5)
    _assert(out is None, "expected None when discovery file missing")


def test_embed_remote_returns_none_when_daemon_dead():
    tmp = tempfile.mkdtemp(prefix="kb_embed_dead_daemon_")
    paths.ensure_project_dir(tmp)
    disc_path = paths.project_dir(tmp) / embeddings.DISCOVERY_FILE
    # Bind a port, immediately close — discovery file points at a port
    # nobody is listening on. Hook should fall through to None within
    # the connect timeout, not raise.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _, dead_port = s.getsockname()
    s.close()
    disc_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": dead_port, "token": "x" * 32,
        "pid": 0, "started_at": "0",
    }), encoding="utf-8")
    out = embeddings.embed_remote("anything", project_cwd=tmp, timeout=0.5)
    _assert(out is None, "expected None when daemon is dead")


def test_embed_remote_rejects_bad_token():
    tmp = tempfile.mkdtemp(prefix="kb_embed_bad_token_")
    mcp_server._start_embed_listener(tmp)
    disc_path = paths.project_dir(tmp) / embeddings.DISCOVERY_FILE
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
