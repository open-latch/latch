"""Focused regressions for MCP lifecycle ownership and failure receipts."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mcp_broker  # noqa: E402
import mcp_proxy  # noqa: E402


def test_blue_green_registry_is_keyed_for_v1_v2_v1(monkeypatch):
    vault = Path(tempfile.mkdtemp(prefix="latch_registry_"))
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    original = mcp_broker.RUNTIME_KEY
    try:
        mcp_broker.RUNTIME_KEY = "runtime-v1"
        v1_path = mcp_broker.publish_discovery(
            port=1111, token="v1-token", pid=os.getpid(), started_at="v1"
        )
        mcp_broker.RUNTIME_KEY = "runtime-v2"
        v2_path = mcp_broker.publish_discovery(
            port=2222, token="v2-token", pid=os.getpid(), started_at="v2"
        )
        assert v1_path != v2_path
        assert mcp_broker.start_lock_path("runtime-v1") != mcp_broker.start_lock_path("runtime-v2")

        mcp_broker.RUNTIME_KEY = "runtime-v1"
        assert mcp_broker.read_discovery()["port"] == 1111
        mcp_broker.RUNTIME_KEY = "runtime-v2"
        assert mcp_broker.read_discovery()["port"] == 2222
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_live_pid_with_stale_heartbeat_does_not_hold_proxy_capacity(monkeypatch):
    vault = Path(tempfile.mkdtemp(prefix="latch_stale_lease_"))
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setenv("LATCH_MCP_PROXY_STALE_SEC", "1")
    lease = mcp_broker.write_proxy_lease(
        "stale-live-pid",
        {
            "connection_id": "stale-live-pid",
            "pid": os.getpid(),
            "runtime_key": mcp_broker.RUNTIME_KEY,
            "started_epoch": time.time() - 100,
            "last_activity_epoch": time.time() - 100,
            "heartbeat_epoch": time.time() - 100,
        },
    )
    assert mcp_broker.proxy_inventory() == []
    assert not lease.exists()
    mcp_broker.emit_lifecycle("proxy_over_cap", live_leases=41, cap=32)
    mcp_broker.emit_lifecycle(
        "proxy_retired", cap=32, over_cap_duration_s=301.5, reason="idle_over_cap"
    )
    summary = mcp_broker.lifecycle_summary(hours=1)
    assert summary["counts"].get("proxy_lease_stale") == 1
    assert summary["proxy_high_water"] == 41
    assert summary["max_over_cap_duration_s"] == 301.5


def test_sustained_over_cap_duration_is_visible_from_live_leases(monkeypatch):
    vault = Path(tempfile.mkdtemp(prefix="latch_over_cap_"))
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setenv("LATCH_MCP_PROXY_CAP", "2")
    monkeypatch.setenv("LATCH_MCP_PROXY_STALE_SEC", "300")
    now = time.time()
    for index in range(3):
        mcp_broker.write_proxy_lease(
            f"live-{index}",
            {
                "connection_id": f"live-{index}",
                "pid": os.getpid(),
                "runtime_key": mcp_broker.RUNTIME_KEY,
                "started_epoch": now - 100 + index,
                "last_activity_epoch": now,
                "heartbeat_epoch": now,
                "over_cap_since_epoch": now - 45,
            },
        )
    mcp_broker.emit_lifecycle("proxy_over_cap", live_leases=3, cap=2)
    summary = mcp_broker.lifecycle_summary(hours=1)
    assert summary["current_live_leases"] == 3
    assert summary["currently_over_cap"] is True
    assert summary["current_over_cap_duration_s"] >= 44
    assert summary["over_cap_duration_is_lower_bound"] is True


def test_disconnect_reports_unknown_mutation_outcome_without_retry_advice(monkeypatch):
    monkeypatch.setattr(mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)
    metadata = {
        "connection_id": "receipt-test",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    }
    bridge = mcp_proxy.ProxyBridge(metadata)
    emitted: list[tuple[object, str]] = []
    bridge._emit_request_error = lambda request_id, message: emitted.append((request_id, message))
    bridge._pending = {7: "tools/call latch_insert"}
    try:
        bridge._daemon_lost("response channel closed")
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert emitted and emitted[0][0] == 7
    message = emitted[0][1].lower()
    assert "outcome is unknown" in message
    assert "inspect current latch state" in message
    assert "retry" not in message


def test_reconnect_failure_emits_lifecycle_signal(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        mcp_broker,
        "connect_mcp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            mcp_broker.BrokerError("synthetic reconnect failure")
        ),
    )
    metadata = {
        "connection_id": "reconnect-receipt",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    }
    bridge = mcp_proxy.ProxyBridge(metadata)
    bridge._init_line = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    bridge._init_id = 1
    emitted: list[tuple[object, str]] = []
    bridge._emit_request_error = lambda request_id, message: emitted.append((request_id, message))
    try:
        bridge._handle_host_line(
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert emitted and "synthetic reconnect failure" in emitted[0][1]
    assert any(event == "daemon_reconnect_failed" for event, _fields in events)


def test_shared_start_failure_is_visible_and_legacy_is_opt_in(monkeypatch, capsys):
    events: list[str] = []
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )
    monkeypatch.delenv("LATCH_MCP_ALLOW_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("LATCH_MCP_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(
        mcp_broker,
        "ensure_daemon",
        lambda _cwd, **_kwargs: (_ for _ in ()).throw(
            mcp_broker.BrokerError("synthetic failure")
        ),
    )
    legacy_called = []
    monkeypatch.setattr(mcp_proxy, "_exec_legacy_server", lambda: legacy_called.append(True))
    assert mcp_proxy.main() == 1
    assert legacy_called == []
    stderr = capsys.readouterr().err
    assert "shared MCP daemon unavailable" in stderr
    assert "LATCH_MCP_ALLOW_LEGACY_FALLBACK=1" in stderr
    assert "daemon_start_failed" in events


def test_explicit_legacy_fallback_emits_lifecycle_signal(monkeypatch):
    events: list[str] = []
    monkeypatch.setenv("LATCH_MCP_ALLOW_LEGACY_FALLBACK", "1")
    monkeypatch.delenv("LATCH_MCP_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )
    monkeypatch.setattr(
        mcp_broker,
        "ensure_daemon",
        lambda _cwd, **_kwargs: (_ for _ in ()).throw(
            mcp_broker.BrokerError("synthetic failure")
        ),
    )

    class LegacyExec(RuntimeError):
        pass

    monkeypatch.setattr(
        mcp_proxy,
        "_exec_legacy_server",
        lambda: (_ for _ in ()).throw(LegacyExec()),
    )
    try:
        mcp_proxy.main()
        raise AssertionError("legacy exec sentinel did not fire")
    except LegacyExec:
        pass
    assert "legacy_fallback" in events


def test_fastmcp_private_boundary_is_pinned_and_available():
    import mcp_server

    server = getattr(mcp_server.mcp, "_mcp_server", None)
    assert callable(getattr(server, "run", None))
    assert callable(getattr(server, "create_initialization_options", None))
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "mcp>=1.28.1,<1.29" in requirements
