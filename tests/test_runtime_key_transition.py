"""Runtime-key transition regressions for shared MCP and embed discovery.

A blue/green upgrade must publish matching MCP and embed aliases for a retained
runtime key. These tests cover that companion-discovery contract, stale-owner
rejection, alias cleanup, and the broker's real MCP connection prelude.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from unittest import mock
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from latch.retrieval import embeddings  # noqa: E402
from latch.mcp import mcp_broker  # noqa: E402
from latch.mcp import mcp_daemon  # noqa: E402
from latch.mcp import mcp_server  # noqa: E402
from latch.store import paths  # noqa: E402

# Reuse the hermetic in-process embed harness from the existing embed test.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_embed_daemon import _assert, _pinned_vault, _wait_for_disc  # noqa: E402


def _dead_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    _, port = s.getsockname()
    s.close()
    return port


def test_upgrade_alias_publishes_embed_alias_for_retained_key():
    """An MCP upgrade alias must include the owner's live embed endpoint."""
    tmp = tempfile.mkdtemp(prefix="kb_embed_alias_")
    with _pinned_vault(tmp):
        # Owner == the real current runtime key; bring up its embed listener.
        mcp_server._start_embed_listener(tmp)
        owner_key = mcp_broker.RUNTIME_KEY
        owner_embed = _wait_for_disc(mcp_broker.embed_discovery_path())

        # A retained (old) runtime key with a STALE/DEAD embed.sock.json.
        retained_key = "aaaaaaaaaaaaaaaaaaaa"
        _assert(retained_key != owner_key, "retained key must differ from owner")
        retained_embed = mcp_broker.embed_discovery_path(retained_key)
        retained_embed.write_text(
            json.dumps({
                "runtime_key": retained_key, "host": "127.0.0.1",
                "port": _dead_port(), "token": "x" * 32, "pid": 0,
                "started_at": "0",
            }),
            encoding="utf-8",
        )

        # Owner publishes the compatible MCP upgrade alias for the retained key
        # (this is the code path that must also publish the embed alias).
        mcp_payload = {
            "port": 1, "token": "m" * 32, "pid": owner_embed["pid"],
            "started_at": "0",
        }
        mcp_daemon._publish_upgrade_alias(retained_key, mcp_payload, capable=True)

        # The retained key's embed discovery must now point at the owner's LIVE
        # embed endpoint and record owner_runtime_key.
        alias = json.loads(retained_embed.read_text(encoding="utf-8"))
        _assert(
            alias.get("port") == owner_embed["port"],
            f"embed alias not repointed to owner (port {alias.get('port')} != {owner_embed['port']})",
        )
        _assert(
            alias.get("owner_runtime_key") == owner_key,
            "embed alias must record owner_runtime_key",
        )

        # End-to-end: a process running under the retained key can now embed.
        saved = mcp_broker.RUNTIME_KEY
        try:
            mcp_broker.RUNTIME_KEY = retained_key
            out = embeddings.embed_remote("retained-key embed", project_cwd=tmp, timeout=5.0)
        finally:
            mcp_broker.RUNTIME_KEY = saved
        _assert(out is not None, "retained-key embed_remote failed after alias publish")


def test_publish_embed_alias_rejects_discovery_not_owned_by_live_mcp_owner():
    """Never republish a stale current-key embed endpoint as a retained alias."""
    tmp = tempfile.mkdtemp(prefix="kb_embed_owner_mismatch_")
    with _pinned_vault(tmp):
        paths.ensure_project_dir(tmp)
        current = mcp_broker.embed_discovery_path()
        current.write_text(
            json.dumps({
                "runtime_key": mcp_broker.RUNTIME_KEY,
                "host": "127.0.0.1",
                "port": _dead_port(),
                "token": "stale-owner-token",
                "pid": 2147483646,
                "started_at": "0",
            }),
            encoding="utf-8",
        )
        retained_key = "bbbbbbbbbbbbbbbbbbbb"
        result = mcp_broker.publish_embed_alias(
            retained_key,
            owner_payload={
                "runtime_key": mcp_broker.RUNTIME_KEY,
                "owner_runtime_key": mcp_broker.RUNTIME_KEY,
                "pid": os.getpid(),
            },
        )
        _assert(result is None, "stale owner discovery must not be aliased")
        _assert(
            not mcp_broker.embed_discovery_path(retained_key).exists(),
            "rejected discovery must not create a retained-key alias",
        )


def test_upgrade_alias_stages_embed_before_mcp_and_reports_degraded_state():
    """MCP publication is the commit point; embed failure is never silent."""
    calls: list[str] = []
    events: list[tuple[str, dict]] = []
    payload = {
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "owner_runtime_key": mcp_broker.RUNTIME_KEY,
        "port": 1,
        "token": "m" * 32,
        "pid": os.getpid(),
        "started_at": "0",
    }

    def no_embed(*_args, **_kwargs):
        calls.append("embed")
        return None

    def publish_mcp(**_kwargs):
        calls.append("mcp")
        return Path("unused")

    with (
        mock.patch.object(mcp_broker, "publish_embed_alias", side_effect=no_embed),
        mock.patch.object(mcp_broker, "publish_discovery", side_effect=publish_mcp),
        mock.patch.object(
            mcp_broker,
            "emit_lifecycle",
            side_effect=lambda event, **fields: events.append((event, fields)),
        ),
    ):
        published = mcp_daemon._publish_upgrade_alias(
            "cccccccccccccccccccc", payload, capable=True
        )

    _assert(calls == ["embed", "mcp"], f"unexpected publication order: {calls}")
    _assert(not published, "failed embed alias must be reported to the caller")
    _assert(
        any(event == "embed_alias_unavailable" for event, _ in events),
        "failed embed alias must emit explicit lifecycle telemetry",
    )


def test_embed_remote_rejects_dead_owner_pid():
    """Reject discovery whose owner PID is dead, even if its port is bound."""
    tmp = tempfile.mkdtemp(prefix="kb_embed_deadpid_")
    with _pinned_vault(tmp):
        mcp_server._start_embed_listener(tmp)
        disc = mcp_broker.embed_discovery_path()
        meta = _wait_for_disc(disc)
        # Precondition: with the real (alive) owner pid, the live socket embeds.
        _assert(
            embeddings.embed_remote("x", project_cwd=tmp, timeout=5.0) is not None,
            "precondition: live owner should embed",
        )
        # Same live socket, but tamper the owner pid to a dead one.
        meta["pid"] = 2147483646  # not a live pid
        disc.write_text(json.dumps(meta), encoding="utf-8")
        out = embeddings.embed_remote("x", project_cwd=tmp, timeout=5.0)
        _assert(out is None, "dead-owner discovery must be rejected even with a live socket")


def test_shutdown_removes_all_embed_aliases_for_owner():
    """Cleanup retracts every exact-owner alias and preserves foreign files."""
    tmp = tempfile.mkdtemp(prefix="kb_embed_cleanup_")
    with _pinned_vault(tmp):
        mcp_server._start_embed_listener(tmp)
        owner = _wait_for_disc(mcp_broker.embed_discovery_path())
        pid, token = owner["pid"], owner["token"]

        retained_alias = mcp_broker.publish_embed_alias(
            "eeeeeeeeeeeeeeeeeeee",
            owner_payload={
                "runtime_key": mcp_broker.RUNTIME_KEY,
                "owner_runtime_key": mcp_broker.RUNTIME_KEY,
                "pid": pid,
            },
        )
        _assert(retained_alias is not None and retained_alias.exists(), "alias not published")

        foreign = mcp_broker.embed_discovery_path("ffffffffffffffffffff")
        foreign.write_text(
            json.dumps({
                "runtime_key": "ffffffffffffffffffff", "host": "127.0.0.1",
                "port": 1, "token": "other-owner", "pid": 1, "started_at": "0",
            }),
            encoding="utf-8",
        )

        mcp_broker.remove_embed_discovery_if_owner(pid=pid, token=token)
        _assert(not retained_alias.exists(), "retained-key embed alias not removed on shutdown")
        _assert(not mcp_broker.embed_discovery_path().exists(), "current-key embed not removed")
        _assert(foreign.exists(), "another owner's embed must survive cleanup")


def _stub_probe_server(pid: int):
    """Minimal daemon stand-in answering the probe prelude like mcp_daemon:542."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    stop = threading.Event()
    observed_ops: list[str] = []

    def serve():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.settimeout(1.0)
                line = conn.makefile("rb").readline(4096)
                prelude = json.loads(line.decode("utf-8"))
                observed_ops.append(str(prelude.get("op") or ""))
                if prelude.get("op") == "probe":
                    conn.sendall(
                        json.dumps({
                            "ok": True,
                            "pid": pid,
                            "vault_context_digest": prelude.get(
                                "vault_context_digest"
                            ),
                        }).encode() + b"\n"
                    )
            except (OSError, ValueError):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv, srv.getsockname()[1], stop, t, observed_ops


def test_retained_key_connect_mcp_uses_live_alias():
    """Exercise broker readiness plus the real MCP prelude through an alias.

    This intentionally does not claim to prove Cursor's full stdio proxy or an
    in-flight owner transition; those remain physical/multi-process acceptance
    boundaries.  It does prove the unchanged ``connect_mcp`` path consumes a
    live retained-key alias without a connection-closed error.
    """
    tmp = tempfile.mkdtemp(prefix="kb_mcp_transition_")
    with _pinned_vault(tmp):
        paths.ensure_project_dir(tmp)
        owner_pid = os.getpid()
        srv, port, stop, t, observed_ops = _stub_probe_server(owner_pid)
        try:
            retained_key = "dddddddddddddddddddd"
            # New owner publishes the compatible MCP alias for the retained key.
            mcp_broker.publish_discovery(
                port=port, token="t" * 32, pid=owner_pid, started_at="0",
                runtime_key=retained_key, owner_runtime_key="owner-key-xxxxxxxxx",
                compatibility="migrate",
            )
            saved = mcp_broker.RUNTIME_KEY
            try:
                mcp_broker.RUNTIME_KEY = retained_key
                sock, payload = mcp_broker.connect_mcp({
                    "project_cwd": tmp,
                    "connection_id": "retained-key-test",
                    "proxy_pid": os.getpid(),
                })
                sock.close()
            finally:
                mcp_broker.RUNTIME_KEY = saved
            deadline = time.time() + 2.0
            while "mcp" not in observed_ops and time.time() < deadline:
                time.sleep(0.01)
            _assert(payload.get("runtime_key") == retained_key, "wrong alias payload")
            _assert(
                "probe" in observed_ops and "mcp" in observed_ops,
                f"connect_mcp did not complete probe + MCP prelude: {observed_ops}",
            )
        finally:
            stop.set()
            try:
                srv.close()
            except OSError:
                pass
            t.join(timeout=2.0)


if __name__ == "__main__":
    test_upgrade_alias_publishes_embed_alias_for_retained_key()
    print("OK: upgrade alias publishes embed alias for retained key")
    test_embed_remote_rejects_dead_owner_pid()
    print("OK: embed_remote rejects dead-owner pid")
    test_shutdown_removes_all_embed_aliases_for_owner()
    print("OK: shutdown removes all embed aliases for owner")
    test_retained_key_connect_mcp_uses_live_alias()
    print("OK: retained key connect_mcp uses live alias")
