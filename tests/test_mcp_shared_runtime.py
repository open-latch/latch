"""Concurrency, ownership, attribution, and recovery tests for shared MCP."""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "src" / "mcp_server.py"


def _assert(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class McpClient:
    def __init__(
        self,
        kb_dir: Path,
        session_id: str,
        *,
        idle_ttl: float = 60.0,
        force_legacy: bool = False,
        proxy_cap: int | None = None,
        proxy_retire_idle: float | None = None,
        proxy_heartbeat: float | None = None,
        project_cwd: Path | None = None,
    ):
        env = os.environ.copy()
        env.update(
            {
                "LATCH_KB_DIR": str(kb_dir),
                "LATCH_SESSION_ID": session_id,
                "LATCH_MCP_DAEMON_IDLE_TTL_SEC": str(idle_ttl),
                "LATCH_MCP_NO_LEGACY_FALLBACK": "1",
                # A disposable test vault must not start the unrelated nightly
                # maintenance subprocess.
                "CLAUDE_KB_IN_MAINTENANCE": "1",
            }
        )
        if force_legacy:
            env["LATCH_MCP_FORCE_LEGACY"] = "1"
        if proxy_cap is not None:
            env["LATCH_MCP_PROXY_CAP"] = str(proxy_cap)
        if proxy_retire_idle is not None:
            env["LATCH_MCP_PROXY_RETIRE_IDLE_SEC"] = str(proxy_retire_idle)
        if proxy_heartbeat is not None:
            env["LATCH_MCP_PROXY_HEARTBEAT_SEC"] = str(proxy_heartbeat)
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            cwd=str(project_cwd or ROOT),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._responses: queue.Queue[bytes | None] = queue.Queue()
        self._request_id = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self.initialize()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                self._responses.put(None)
                return
            self._responses.put(line)

    def send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        line = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        self.process.stdin.write(line)
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 45.0) -> dict:
        self._request_id += 1
        request_id = self._request_id
        self.send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._responses.get(timeout=min(0.5, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if line is None:
                stderr = self.stderr()
                raise AssertionError(f"proxy exited waiting for {method}: {stderr}")
            message = json.loads(line.decode("utf-8"))
            if message.get("id") == request_id:
                if "error" in message:
                    raise AssertionError(f"{method} failed: {message['error']}")
                return message
        raise AssertionError(f"timeout waiting for {method}; stderr={self.stderr()}")

    def initialize(self) -> None:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "latch-test", "version": "1"},
            },
        )
        _assert(result["result"]["serverInfo"]["name"] == "latch", str(result))
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        response = self.request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=60.0
        )
        result = response["result"]
        _assert(not result.get("isError"), f"tool {name} returned error: {result}")
        texts = [item["text"] for item in result["content"] if item.get("type") == "text"]
        _assert(bool(texts), f"tool {name} returned no text content: {result}")
        values = [json.loads(text) for text in texts]
        return values[0] if len(values) == 1 else values

    def status(self) -> dict[str, Any]:
        return self.call_tool("latch_runtime_status", {})

    def stderr(self) -> str:
        if self.process.stderr is None:
            return ""
        try:
            return self.process.stderr.peek(10000).decode("utf-8", errors="replace")
        except (AttributeError, OSError):
            return ""

    def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3.0)


def _daemon_pid(kb_dir: Path) -> int | None:
    try:
        return int(json.loads((kb_dir / "mcp-daemon.json").read_text())["pid"])
    except (OSError, ValueError, KeyError):
        return None


def _stop_daemon(kb_dir: Path) -> None:
    pid = _daemon_pid(kb_dir)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return


def _temp_vault() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch_shared_mcp_"))


def test_parallel_clients_share_one_heavy_owner_and_keep_context_isolated() -> None:
    kb_dir = _temp_vault()
    clients: list[McpClient] = []
    try:
        project_a = kb_dir / "workspaces" / "a"
        project_b = kb_dir / "workspaces" / "b"
        project_a.mkdir(parents=True)
        project_b.mkdir(parents=True)
        clients = [
            McpClient(kb_dir, "session-a", project_cwd=project_a),
            McpClient(kb_dir, "session-b", project_cwd=project_b),
        ]
        first = clients[0].status()
        second = clients[1].status()
        _assert(first["mode"] == "shared_daemon", str(first))
        _assert(first["process_pid"] == second["process_pid"], "clients used different owners")
        _assert(first["process_pid"] != clients[0].process.pid, "proxy loaded heavy server")
        _assert(first["connection"]["session_id"] == "session-a", str(first))
        _assert(second["connection"]["session_id"] == "session-b", str(second))
        _assert(first["project_cwd"] == str(project_a.resolve()), str(first))
        _assert(second["project_cwd"] == str(project_b.resolve()), str(second))
        _assert(first["embedding"]["heavy_model_owner_count"] == 1, str(first))
        _assert(second["embedding"]["listener"]["pid"] == first["process_pid"], str(second))

        vector_a = clients[0].call_tool("latch_embed", {"text": "shared owner parity"})
        vector_b = clients[1].call_tool("latch_embed", {"text": "shared owner parity"})
        _assert(len(vector_a) == 384 and len(vector_b) == 384, "unexpected vector shape")
        _assert(max(abs(a - b) for a, b in zip(vector_a, vector_b)) < 1e-7,
                "shared owner changed embedding output")
        print("PASS parallel_clients_share_one_heavy_owner_and_keep_context_isolated")
    finally:
        for client in clients:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_owner_crash_restarts_on_next_call_without_replaying_inflight_work() -> None:
    kb_dir = _temp_vault()
    client: McpClient | None = None
    try:
        client = McpClient(kb_dir, "recovery-session")
        before = client.status()
        old_pid = before["process_pid"]
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)

        # The proxy sees EOF, stays alive, replays initialize internally, and
        # forwards this new request to a newly elected owner.
        after = client.status()
        _assert(after["process_pid"] != old_pid, f"owner did not change: {after}")
        _assert(after["connection"]["session_id"] == "recovery-session", str(after))
        _assert(client.process.poll() is None, "stdio proxy exited after owner crash")
        print("PASS owner_crash_restarts_on_next_call_without_replaying_inflight_work")
    finally:
        if client is not None:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_idle_owner_is_reclaimed_and_lazily_recreated() -> None:
    kb_dir = _temp_vault()
    client: McpClient | None = None
    try:
        client = McpClient(kb_dir, "idle-session", idle_ttl=1.0)
        old_pid = client.status()["process_pid"]
        time.sleep(2.0)
        after = client.status()
        _assert(after["process_pid"] != old_pid, f"idle owner was not reclaimed: {after}")
        _assert(client.process.poll() is None, "proxy did not survive idle reclamation")
        print("PASS idle_owner_is_reclaimed_and_lazily_recreated")
    finally:
        if client is not None:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_over_cap_idle_proxy_retires_itself_without_killing_peers() -> None:
    kb_dir = _temp_vault()
    clients: list[McpClient] = []
    try:
        for index in range(3):
            clients.append(
                McpClient(
                    kb_dir,
                    f"bounded-session-{index}",
                    proxy_cap=2,
                    proxy_retire_idle=0.5,
                    proxy_heartbeat=0.2,
                )
            )
            time.sleep(0.05)
        deadline = time.monotonic() + 4.0
        alive = clients
        while time.monotonic() < deadline:
            alive = [client for client in clients if client.process.poll() is None]
            if len(alive) <= 2:
                break
            time.sleep(0.1)
        _assert(len(alive) == 2, f"expected bounded proxy pool of 2, got {len(alive)}")
        _assert(
            any(client.process.poll() == 0 for client in clients),
            "over-cap proxy did not retire cleanly",
        )
        _assert(all(client.process.poll() is None for client in alive), "peer was killed")
        print("PASS over_cap_idle_proxy_retires_itself_without_killing_peers")
    finally:
        for client in clients:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


if __name__ == "__main__":
    test_parallel_clients_share_one_heavy_owner_and_keep_context_isolated()
    test_owner_crash_restarts_on_next_call_without_replaying_inflight_work()
    test_idle_owner_is_reclaimed_and_lazily_recreated()
    test_over_cap_idle_proxy_retires_itself_without_killing_peers()
    print("\nAll shared MCP runtime tests pass.")
