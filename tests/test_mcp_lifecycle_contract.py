"""Focused regressions for MCP lifecycle ownership and failure receipts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mcp_broker  # noqa: E402
import mcp_daemon  # noqa: E402
import mcp_proxy  # noqa: E402


def test_blue_green_registry_is_keyed_for_v1_v2_v1(monkeypatch, tmp_path):
    vault = tmp_path / "registry"
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


def test_daemon_owner_fence_survives_broker_death_and_releases_with_owner(
    monkeypatch, tmp_path
):
    vault = tmp_path / "owner-fence"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    env = os.environ.copy()
    env.update({"LATCH_KB_DIR": str(vault), "PYTHONPATH": str(ROOT / "src")})
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import mcp_broker,time; "
                "h=mcp_broker.acquire_owner_fence(); "
                "print('held' if h else 'failed', flush=True); time.sleep(30)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        mcp_broker.start_lock_path().write_text(
            json.dumps({"pid": 2**30, "runtime_key": mcp_broker.RUNTIME_KEY}),
            encoding="utf-8",
        )
        assert mcp_broker._acquire_start_lock() is True
        contender = subprocess.run(
            [sys.executable, str(ROOT / "src" / "mcp_daemon.py")],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert contender.returncode == 0
        assert not mcp_broker.discovery_path().exists()
        assert mcp_broker.acquire_owner_fence() is None
        mcp_broker._release_start_lock()
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    fence = mcp_broker.acquire_owner_fence()
    assert fence is not None
    fence.close()


def test_live_pid_with_stale_heartbeat_does_not_hold_proxy_capacity(
    monkeypatch, tmp_path
):
    vault = tmp_path / "stale-lease"
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
    state = mcp_broker.proxy_lease_state()
    assert state["live"] == []
    assert state["stale_count"] == 1
    assert lease.exists(), "a live owner must repair its own lease"
    summary = mcp_broker.lifecycle_summary(hours=1, lease_state=state)
    assert summary["current_stale_leases"] == 1
    assert summary["max_stale_lease_age_s"] >= 99

    payload = json.loads(lease.read_text(encoding="utf-8"))
    payload["heartbeat_epoch"] = time.time()
    mcp_broker.write_proxy_lease("stale-live-pid", payload)
    assert len(mcp_broker.proxy_inventory()) == 1

    mcp_broker.emit_lifecycle("proxy_over_cap", live_leases=41, cap=32)
    mcp_broker.emit_lifecycle(
        "proxy_retired", cap=32, over_cap_duration_s=301.5, reason="idle_over_cap"
    )
    summary = mcp_broker.lifecycle_summary(hours=1)
    assert summary["proxy_high_water"] == 41
    assert summary["max_over_cap_duration_s"] == 301.5


def test_proxy_cap_scope_is_per_runtime_key(monkeypatch, tmp_path):
    vault = tmp_path / "runtime-lease-scope"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    original = mcp_broker.RUNTIME_KEY
    now = time.time()
    try:
        for key in ("runtime-v1", "runtime-v2"):
            mcp_broker.RUNTIME_KEY = key
            mcp_broker.write_proxy_lease(
                f"lease-{key}",
                {
                    "connection_id": f"lease-{key}",
                    "pid": os.getpid(),
                    "runtime_key": key,
                    "started_epoch": now,
                    "last_activity_epoch": now,
                    "heartbeat_epoch": now,
                },
            )
            mcp_broker.emit_lifecycle(
                "proxy_started", live_leases=3 if key == "runtime-v1" else 40
            )
        mcp_broker.RUNTIME_KEY = "runtime-v1"
        assert [row["runtime_key"] for row in mcp_broker.proxy_inventory()] == [
            "runtime-v1"
        ]
        mcp_broker.RUNTIME_KEY = "runtime-v2"
        assert [row["runtime_key"] for row in mcp_broker.proxy_inventory()] == [
            "runtime-v2"
        ]
        assert mcp_broker.proxy_lease_dir("runtime-v1") != mcp_broker.proxy_lease_dir(
            "runtime-v2"
        )
        mcp_broker.RUNTIME_KEY = "runtime-v1"
        assert mcp_broker.lifecycle_summary(hours=1)["proxy_high_water"] == 3
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_recent_lifecycle_warnings_are_chronological_across_day_files(
    monkeypatch, tmp_path
):
    vault = tmp_path / "lifecycle-order"
    vault.mkdir()
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    now = datetime.now(timezone.utc)
    today_rows = [
        {
            "ts": (now - timedelta(minutes=index)).isoformat(),
            "event": "daemon_failed",
            "runtime_key": mcp_broker.RUNTIME_KEY,
            "reason": f"today-{index}",
        }
        for index in range(11)
    ]
    yesterday_row = {
        "ts": (now - timedelta(hours=25)).isoformat(),
        "event": "daemon_failed",
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "reason": "yesterday",
    }
    mcp_broker._lifecycle_path(now).write_text(
        "\n".join(json.dumps(row) for row in today_rows) + "\n", encoding="utf-8"
    )
    mcp_broker._lifecycle_path(now - timedelta(days=1)).write_text(
        json.dumps(yesterday_row) + "\n", encoding="utf-8"
    )
    recent = mcp_broker.lifecycle_summary(hours=48)["recent_warnings"]
    reasons = {row["reason"] for row in recent}
    assert len(recent) == 10
    assert "yesterday" not in reasons
    assert "today-0" in reasons and "today-9" in reasons


def test_sustained_over_cap_duration_is_visible_from_live_leases(
    monkeypatch, tmp_path
):
    vault = tmp_path / "over-cap"
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


def test_partial_replay_flush_fails_pending_and_deferred_tail(monkeypatch):
    monkeypatch.setattr(mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)

    class FailingSocket:
        def __init__(self):
            self.calls = 0

        def sendall(self, _line):
            self.calls += 1
            if self.calls == 3:
                raise OSError("synthetic partial flush failure")

        def close(self):
            pass

    bridge = mcp_proxy.ProxyBridge({
        "connection_id": "partial-flush",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    })
    bridge._sock = FailingSocket()
    bridge._initialized_line = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    bridge._deferred = [
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}).encode()
        + b"\n"
        for request_id in (1, 2, 3)
    ]
    emitted: list[tuple[object, str]] = []
    bridge._emit_request_error = lambda request_id, message: emitted.append(
        (request_id, message)
    )
    try:
        assert bridge._finish_replay() is False
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert {request_id for request_id, _message in emitted} == {1, 2, 3}
    assert "not sent" in next(message for request_id, message in emitted if request_id == 3)


def test_reconnect_success_is_emitted_only_after_initialize_reply(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )

    class Socket:
        def sendall(self, _line):
            pass

        def close(self):
            pass

    bridge = mcp_proxy.ProxyBridge({
        "connection_id": "reconnect-timing",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    })
    bridge._sock = Socket()
    bridge._replaying = True
    bridge._replay_id = 17
    try:
        assert "daemon_reconnect_succeeded" not in events
        bridge._handle_daemon_line(b'{"jsonrpc":"2.0","id":17,"result":{}}\n')
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert events == ["daemon_reconnect_succeeded"]


def test_idle_reclaim_revalidates_activity_generation():
    state = mcp_daemon.DaemonState(started_at="now", idle_ttl_s=0.01)
    connection_id = state.register({"connection_id": "generation-race"})
    time.sleep(0.02)
    generation = state.idle_candidate()
    assert generation is not None
    state.request_started(connection_id, 7)

    class CancelScope:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    cancel_scope = CancelScope()
    assert state.cancel_reclaim_if_unchanged(generation, cancel_scope) is None
    assert cancel_scope.cancelled is False
    assert state.snapshot()["inflight_requests"] == 1

    state.request_finished(connection_id, 7)
    time.sleep(0.02)
    generation = state.idle_candidate()
    assert generation is not None
    assert state.cancel_reclaim_if_unchanged(generation, cancel_scope) is not None
    assert cancel_scope.cancelled is True


def test_request_stays_pending_until_response_delivery():
    state = mcp_daemon.DaemonState(started_at="now", idle_ttl_s=60)
    connection_id = state.register({"connection_id": "delivery-window"})
    state.request_started(connection_id, 9)

    class Message:
        def model_dump(self, **_kwargs):
            return {"jsonrpc": "2.0", "id": 9, "result": {}}

        def model_dump_json(self, **_kwargs):
            return '{"jsonrpc":"2.0","id":9,"result":{}}'

    class Session:
        message = Message()

    class Stream:
        async def send(self, _line):
            assert state.snapshot()["inflight_requests"] == 1

    async def run():
        await mcp_daemon._send_session_message(
            Stream(), state, connection_id, Session()
        )

    mcp_daemon.anyio.run(run)
    assert state.snapshot()["inflight_requests"] == 0


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
