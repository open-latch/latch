"""Deterministic client deadline and observed-discovery failure regressions."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from latch.mcp import mcp_broker
from latch.retrieval import embeddings


class _Clock:
    now = 100.0

    def monotonic(self):
        return self.now


class _Socket:
    def __init__(self, response, clock, *, receive_delay=0):
        self.response = response
        self.clock = clock
        self.receive_delay = receive_delay
        self.timeouts = []
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def sendall(self, payload):
        self.sent.append(payload)

    def recv(self, _):
        self.clock.now += self.receive_delay
        return self.response


@pytest.fixture
def client(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(embeddings._time, "monotonic", clock.monotonic)
    discovery = mcp_broker.EmbedDiscoveryResult(
        "ready", {"host": "127.0.0.1", "port": 12345, "token": "private"}
    )
    monkeypatch.setattr(mcp_broker, "inspect_live_embed_discovery", lambda: discovery)
    return clock, discovery


def test_healthy_owner_with_exhausted_deadline_does_not_attempt_rpc(client, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("exhausted local budget must not inspect or call the healthy owner")

    monkeypatch.setattr(mcp_broker, "inspect_live_embed_discovery", unexpected)
    monkeypatch.setattr(embeddings._socket, "create_connection", unexpected)
    result = embeddings.embed_remote_result("prompt", ".", timeout=0)
    assert result.status == "local_budget_exhausted"
    assert result.vector is None


def test_discovery_time_counts_toward_same_deadline(client, monkeypatch):
    clock, discovery = client

    def inspect():
        clock.now += 0.2
        return discovery

    monkeypatch.setattr(mcp_broker, "inspect_live_embed_discovery", inspect)
    monkeypatch.setattr(
        embeddings._socket, "create_connection",
        lambda *a, **k: pytest.fail("discovery consumed the RPC allowance"),
    )
    assert embeddings.embed_remote_result("prompt", ".", timeout=0.1).status == "local_budget_exhausted"


def test_connect_send_and_receive_share_one_deadline(client, monkeypatch):
    clock, _ = client
    sock = _Socket(json.dumps({"vec": [1.0] * embeddings.DIM}).encode() + b"\n", clock)
    connect_timeouts = []

    def connect(address, timeout):
        connect_timeouts.append(timeout)
        clock.now += 0.02
        return sock

    def sendall(payload):
        clock.now += 0.03

    sock.sendall = sendall
    monkeypatch.setattr(embeddings._socket, "create_connection", connect)
    result = embeddings.embed_remote_result("prompt", ".", timeout=0.1)
    assert result.status == "ready"
    assert result.vector.shape == (embeddings.DIM,)
    assert connect_timeouts == pytest.approx([0.1])
    assert sock.timeouts == pytest.approx([0.08, 0.05])


def test_valid_vector_after_deadline_is_rejected(client, monkeypatch):
    clock, _ = client
    sock = _Socket(
        json.dumps({"vec": [1.0] * embeddings.DIM}).encode() + b"\n",
        clock, receive_delay=0.2,
    )
    monkeypatch.setattr(embeddings._socket, "create_connection", lambda *a, **k: sock)
    result = embeddings.embed_remote_result("prompt", ".", timeout=0.1)
    assert result.status == "local_budget_exhausted"
    assert result.vector is None


@pytest.mark.parametrize("response, status", [
    (b'{"error":"bad_token"}\n', "authentication_rejected"),
    (b'{"error":"model failed"}\n', "rpc_failed"),
    (b'[]\n', "rpc_failed"),
    (b'not json\n', "rpc_failed"),
    (b'{"vec":[1]}\n', "rpc_failed"),
    (b'', "rpc_failed"),
])
def test_protocol_outcome_classification(client, monkeypatch, response, status):
    clock, _ = client
    sock = _Socket(response, clock)
    monkeypatch.setattr(embeddings._socket, "create_connection", lambda *a, **k: sock)
    assert embeddings.embed_remote_result("prompt", ".").status == status
    assert embeddings.embed_remote("prompt", ".") is None


@pytest.mark.parametrize("error, status", [
    (ConnectionRefusedError(), "rpc_failed"),
    (TimeoutError(), "local_budget_exhausted"),
])
def test_transport_outcome_classification(client, monkeypatch, error, status):
    def connect(*args, **kwargs):
        raise error

    monkeypatch.setattr(embeddings._socket, "create_connection", connect)
    assert embeddings.embed_remote_result("prompt", ".").status == status


@pytest.mark.parametrize("timeout", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_timeout_is_not_an_unbounded_rpc(client, timeout):
    with pytest.raises(ValueError, match="finite"):
        embeddings.embed_remote_result("prompt", ".", timeout=timeout)
    assert embeddings.embed_remote("prompt", ".", timeout=timeout) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), [1.0]])
def test_invalid_vector_is_rejected(client, monkeypatch, value):
    clock, _ = client
    sock = _Socket(json.dumps({"vec": [value] * embeddings.DIM}).encode() + b"\n", clock)
    monkeypatch.setattr(embeddings._socket, "create_connection", lambda *a, **k: sock)
    assert embeddings.embed_remote_result("prompt", ".").status == "rpc_failed"


def test_preflight_discovery_can_be_reused(client, monkeypatch):
    clock, discovery = client
    sock = _Socket(json.dumps({"vec": [1.0] * embeddings.DIM}).encode() + b"\n", clock)
    monkeypatch.setattr(embeddings._socket, "create_connection", lambda *a, **k: sock)
    monkeypatch.setattr(
        mcp_broker, "inspect_live_embed_discovery",
        lambda: pytest.fail("validated preflight should not repeat discovery"),
    )
    result = embeddings.embed_remote_result("prompt", ".", discovery=discovery)
    np.testing.assert_array_equal(result.vector, np.ones(embeddings.DIM, dtype=np.float32))


@pytest.fixture
def discovery_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_broker, "embed_discovery_path", lambda key=None: tmp_path / "embed.json")
    monkeypatch.setattr(mcp_broker, "discovery_path", lambda key=None: tmp_path / "mcp.json")
    monkeypatch.setattr(mcp_broker, "start_lock_path", lambda key=None: tmp_path / "start.lock")
    monkeypatch.setattr(mcp_broker, "vault_context_digest", lambda: "a" * 64)
    return tmp_path


def test_missing_discovery_is_not_assumed_to_be_starting(discovery_files):
    assert mcp_broker.inspect_live_embed_discovery().status == "discovery_missing"
    assert mcp_broker.read_live_embed_discovery() is None


@pytest.mark.parametrize("pid, key, age, status", [
    (os.getpid(), mcp_broker.RUNTIME_KEY, 0, "owner_starting"),
    (0, mcp_broker.RUNTIME_KEY, 0, "discovery_missing"),
    (os.getpid(), "other-runtime", 0, "discovery_missing"),
    (os.getpid(), mcp_broker.RUNTIME_KEY, 1000, "discovery_missing"),
])
def test_only_current_live_start_lock_proves_startup(discovery_files, monkeypatch, pid, key, age, status):
    lock = discovery_files / "start.lock"
    lock.write_text(json.dumps({"pid": pid, "runtime_key": key}))
    os.utime(lock, (time.time() - age, time.time() - age))
    monkeypatch.setattr(mcp_broker, "_pid_alive", lambda candidate: candidate == os.getpid())
    assert mcp_broker.inspect_live_embed_discovery().status == status


def test_published_start_failure_is_not_pending_startup(discovery_files):
    (discovery_files / "mcp.json").write_text(json.dumps({
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "created_epoch": time.time(), "error": "startup failed",
    }))
    assert mcp_broker.inspect_live_embed_discovery().status == "owner_start_failed"


def test_invalid_discovery_is_distinct_from_missing(discovery_files):
    (discovery_files / "embed.json").write_text("not json")
    assert mcp_broker.inspect_live_embed_discovery().status == "discovery_rejected"


def test_invalid_local_authority_is_context_rejection(discovery_files, monkeypatch):
    def invalid():
        raise mcp_broker.BrokerError("invalid authority")

    monkeypatch.setattr(mcp_broker, "vault_context_digest", invalid)
    assert mcp_broker.inspect_live_embed_discovery().status == "context_rejected"


def test_recorded_owner_context_mismatch_is_context_rejection(discovery_files):
    (discovery_files / "mcp.json").write_text(json.dumps({
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "protocol": mcp_broker.PROTOCOL_VERSION,
        "vault_context_digest": "b" * 64,
        "host": "127.0.0.1", "port": 12345, "token": "private", "pid": os.getpid(),
    }))
    assert mcp_broker.inspect_live_embed_discovery().status == "context_rejected"


def test_valid_discovery_preserves_legacy_api(discovery_files):
    metadata = {
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "host": "127.0.0.1", "port": 12345, "token": "private", "pid": os.getpid(),
    }
    (discovery_files / "embed.json").write_text(json.dumps(metadata))
    result = mcp_broker.inspect_live_embed_discovery()
    assert result.status == "ready"
    assert result.metadata == metadata
    assert mcp_broker.read_live_embed_discovery() == metadata


def _write_embed_discovery(directory, *, pid=None):
    (directory / "embed.json").write_text(json.dumps({
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "host": "127.0.0.1", "port": 12345, "token": "private",
        "pid": os.getpid() if pid is None else pid,
    }))


def test_hook_waits_for_mcp_ready_even_when_embed_socket_is_published(discovery_files):
    _write_embed_discovery(discovery_files)
    (discovery_files / "start.lock").write_text(json.dumps({
        "pid": os.getpid(), "runtime_key": mcp_broker.RUNTIME_KEY,
    }))
    result = mcp_broker.inspect_live_embed_discovery(require_owner_ready=True)
    assert result.status == "owner_starting"
    assert result.metadata is None
    assert mcp_broker.inspect_live_embed_discovery().status == "ready"


def test_hook_without_ready_owner_does_not_use_standalone_embed_socket(discovery_files):
    _write_embed_discovery(discovery_files)
    result = mcp_broker.inspect_live_embed_discovery(require_owner_ready=True)
    assert result.status == "discovery_missing"
    assert result.metadata is None


@pytest.mark.parametrize("payload", ["not json", "[]", '{"runtime_key":"wrong"}'])
def test_hook_rejects_invalid_mcp_readiness_instead_of_treating_it_as_missing(discovery_files, payload):
    _write_embed_discovery(discovery_files)
    (discovery_files / "mcp.json").write_text(payload)
    result = mcp_broker.inspect_live_embed_discovery(require_owner_ready=True)
    assert result.status == "discovery_rejected"
    assert result.metadata is None


def test_expired_start_failure_can_retry_as_missing(discovery_files):
    (discovery_files / "mcp.json").write_text(json.dumps({
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "created_epoch": time.time() - 2 * mcp_broker.START_FAILURE_MAX_AGE_S,
        "error": "startup failed",
    }))
    result = mcp_broker.inspect_live_embed_discovery(require_owner_ready=True)
    assert result.status == "discovery_missing"
    assert not (discovery_files / "mcp.json").exists()


def test_hook_start_failure_takes_priority_over_leftover_embed_socket(discovery_files):
    _write_embed_discovery(discovery_files)
    (discovery_files / "mcp.json").write_text(json.dumps({
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "created_epoch": time.time(), "error": "startup failed",
    }))
    result = mcp_broker.inspect_live_embed_discovery(require_owner_ready=True)
    assert result.status == "owner_start_failed"
    assert result.metadata is None


def test_dead_owner_record_allows_missing_owner_recovery(discovery_files, monkeypatch):
    _write_embed_discovery(discovery_files, pid=123456)
    monkeypatch.setattr(mcp_broker, "_pid_alive", lambda pid: False)
    result = mcp_broker.inspect_live_embed_discovery()
    assert result.status == "discovery_missing"
    assert result.metadata is None


def test_dead_owner_record_with_live_startup_is_starting(discovery_files, monkeypatch):
    _write_embed_discovery(discovery_files, pid=123456)
    (discovery_files / "start.lock").write_text(json.dumps({
        "pid": os.getpid(), "runtime_key": mcp_broker.RUNTIME_KEY,
    }))
    monkeypatch.setattr(mcp_broker, "_pid_alive", lambda pid: pid == os.getpid())
    result = mcp_broker.inspect_live_embed_discovery()
    assert result.status == "owner_starting"
    assert result.metadata is None
