"""Runtime-key transition regressions (Windows-Cursor daemon reliability).

Phase A — embed-discovery transition defect
    When a blue/green upgrade publishes a *compatible MCP* discovery alias for a
    retained runtime key (old proxy keeps talking to the new owner), the matching
    *embed* discovery alias was NOT published. A process still running under the
    retained key would then read its own stale/dead ``embed.sock.json`` and fail
    every remote embed (``embed_daemon_unavailable``), even though the owner's
    embedder is alive. Observed live on 2026-07-15: key ``ccf4db…`` had an MCP
    alias to pid 22212 but a dead embed socket (pid 17716). See KB id=1912.

Phase B — MCP runtime-key transition (proof-first)
    Determine whether a retained proxy hitting a mid-transition owner actually
    produces connection-closed/timeout. Only if reproduced should the proxy/
    broker connection path change (codex directive).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import embeddings  # noqa: E402
import mcp_broker  # noqa: E402
import mcp_daemon  # noqa: E402
import mcp_server  # noqa: E402
import paths  # noqa: E402

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
    """Phase A regression: publishing the MCP upgrade alias for a retained key
    must also repoint that key's embed discovery at the owner's live endpoint,
    so a retained-key process can still embed. Fails before the fix (no embed
    alias is written, so the retained key keeps its stale/dead socket)."""
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


def test_embed_remote_rejects_dead_owner_pid():
    """Phase A item 3: embed_remote must reject discovery whose owner PID is
    dead, even when a socket is still bound at the advertised port — a retained
    key can read a stale embed.sock.json whose owner exited (KB id=1912)."""
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
    """Phase A item 4: shutdown cleanup retracts EVERY embed alias owned by the
    exact pid/token (all retained keys), while leaving another owner's file."""
    tmp = tempfile.mkdtemp(prefix="kb_embed_cleanup_")
    with _pinned_vault(tmp):
        mcp_server._start_embed_listener(tmp)
        owner = _wait_for_disc(mcp_broker.embed_discovery_path())
        pid, token = owner["pid"], owner["token"]

        retained_alias = mcp_broker.publish_embed_alias("eeeeeeeeeeeeeeeeeeee")
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
                conn.makefile("rb").readline(4096)  # consume the prelude
                conn.sendall(json.dumps({"ok": True, "pid": pid}).encode() + b"\n")
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv, srv.getsockname()[1], stop, t


def test_retained_key_resolves_via_mcp_alias_during_transition():
    """Phase B (proof-first, codex): a retained-key proxy connecting while a new
    owner is live resolves through the published MCP alias and probes OK — it
    does NOT reproduce connection-closed/timeout at the broker resolution layer.
    Because the defect is not reproduced here, the proxy/broker connection path
    is left unchanged (the embed-alias gap in Phase A was the real defect)."""
    tmp = tempfile.mkdtemp(prefix="kb_mcp_transition_")
    with _pinned_vault(tmp):
        paths.ensure_project_dir(tmp)
        owner_pid = os.getpid()
        srv, port, stop, t = _stub_probe_server(owner_pid)
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
                payload = mcp_broker.read_discovery()
                _assert(payload is not None, "retained key has no discovery (alias missing)")
                resolved = mcp_broker.probe_discovery(payload, timeout=2.0)
            finally:
                mcp_broker.RUNTIME_KEY = saved
            _assert(resolved, "retained-key MCP probe did NOT resolve via alias")
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
    test_retained_key_resolves_via_mcp_alias_during_transition()
    print("OK: retained key resolves via MCP alias (Phase B: no proxy change)")
