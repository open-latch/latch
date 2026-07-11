"""Concurrency, ownership, attribution, and recovery tests for shared MCP."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
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
PROMPT_HOOK = ROOT / "src" / "hooks" / "user_prompt_submit.py"


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
        env_overrides: dict[str, str] | None = None,
    ):
        env = os.environ.copy()
        env.update(
            {
                "LATCH_KB_DIR": str(kb_dir),
                "LATCH_SESSION_ID": session_id,
                "LATCH_MCP_DAEMON_IDLE_TTL_SEC": str(idle_ttl),
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
        if env_overrides:
            env.update(env_overrides)
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
        files = list((kb_dir / "runtime" / "mcp-runtimes").glob("*/mcp-daemon.json"))
        return int(json.loads(files[0].read_text())["pid"])
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _stop_daemon(kb_dir: Path) -> None:
    for path in (kb_dir / "runtime" / "mcp-runtimes").glob("*/mcp-daemon.json"):
        try:
            pid = int(json.loads(path.read_text())["pid"])
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError, KeyError):
            continue


def _temp_vault() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch_shared_mcp_"))


def _lifecycle_rows(kb_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in kb_dir.glob("mcp_lifecycle-*.log"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


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
        _assert(os.path.samefile(first["project_cwd"], project_a), str(first))
        _assert(os.path.samefile(second["project_cwd"], project_b), str(second))
        _assert(first["embedding"]["heavy_model_owner_count"] == 1, str(first))
        _assert(second["embedding"]["listener"]["pid"] == first["process_pid"], str(second))
        _assert(first["daemon"]["peak_connections"] == 2, str(first))
        started = [row for row in _lifecycle_rows(kb_dir) if row.get("event") == "daemon_started"]
        _assert(bool(started), "daemon_started lifecycle event missing")
        _assert(started[-1].get("reason") == "proxy_start", str(started[-1]))
        _assert(float(started[-1].get("cold_start_duration_ms")) >= 0, str(started[-1]))

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
        rows = _lifecycle_rows(kb_dir)
        reasons = [row.get("reason") for row in rows if row.get("event") == "daemon_started"]
        _assert("daemon_reconnect" in reasons, str(reasons))
        _assert(
            any(row.get("event") == "daemon_reconnect_succeeded" for row in rows),
            "reconnect success lifecycle event missing",
        )
        print("PASS owner_crash_restarts_on_next_call_without_replaying_inflight_work")
    finally:
        if client is not None:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_committed_mutation_with_lost_response_is_not_replayed_or_called_retryable() -> None:
    kb_dir = _temp_vault()
    client: McpClient | None = None
    title = "post-commit response-loss sentinel"
    try:
        client = McpClient(
            kb_dir,
            "unknown-outcome-session",
            env_overrides={"LATCH_MCP_TEST_DROP_RESPONSE_ID_ONCE": "2"},
        )
        try:
            client.call_tool(
                "latch_insert",
                {
                    "kind": "fact",
                    "title": title,
                    "body": "The mutation committed before its response channel was dropped.",
                },
            )
            raise AssertionError("fault injection did not drop the committed response")
        except AssertionError as exc:
            message = str(exc).lower()
            _assert("outcome is unknown" in message, message)
            _assert("inspect current latch state" in message, message)
            _assert("retry" not in message, message)

        def count_rows() -> int:
            conn = sqlite3.connect(kb_dir / "kb.db")
            try:
                return int(conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE title = ?", (title,)
                ).fetchone()[0])
            finally:
                conn.close()

        _assert(count_rows() == 1, "committed mutation missing or duplicated")
        client.status()
        _assert(count_rows() == 1, "proxy replayed an unknown mutation")
        print("PASS committed_mutation_with_lost_response_is_not_replayed_or_retryable")
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
        idle_events = [
            row for row in _lifecycle_rows(kb_dir)
            if row.get("event") == "daemon_idle_exit"
        ]
        _assert(bool(idle_events), "daemon_idle_exit lifecycle event missing")
        _assert(idle_events[-1].get("reason") == "idle_ttl", str(idle_events[-1]))
        _assert(float(idle_events[-1].get("idle_duration_s")) >= 1.0, str(idle_events[-1]))
        _assert(int(idle_events[-1].get("peak_connections")) >= 1, str(idle_events[-1]))
        print("PASS idle_owner_is_reclaimed_and_lazily_recreated")
    finally:
        if client is not None:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_prompt_embed_activity_keeps_owner_warm() -> None:
    kb_dir = _temp_vault()
    client: McpClient | None = None
    try:
        client = McpClient(kb_dir, "embed-activity-session", idle_ttl=1.0)
        old_pid = client.status()["process_pid"]
        env = os.environ.copy()
        env.update({"LATCH_KB_DIR": str(kb_dir), "PYTHONPATH": str(ROOT / "src")})
        code = (
            "import embeddings,time\n"
            "for _ in range(4):\n"
            " assert embeddings.embed_remote('keep warm', '.', timeout=1) is not None\n"
            " time.sleep(.35)\n"
        )
        subprocess.run([sys.executable, "-c", code], env=env, check=True, timeout=5.0)
        _assert(_daemon_pid(kb_dir) == old_pid, "prompt embedding did not refresh owner activity")
        print("PASS prompt_embed_activity_keeps_owner_warm")
    finally:
        if client is not None:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_prompt_after_idle_exit_wakes_owner_and_emits_truthful_bounded_receipt() -> None:
    kb_dir = _temp_vault()
    client: McpClient | None = None
    try:
        client = McpClient(kb_dir, "prompt-idle-session", idle_ttl=1.0)
        old_pid = client.status()["process_pid"]
        client.close()
        client = None

        registry = kb_dir / "runtime" / "mcp-runtimes"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and list(registry.glob("*/mcp-daemon.json")):
            time.sleep(0.05)
        _assert(not list(registry.glob("*/mcp-daemon.json")), "idle owner did not exit")

        env = os.environ.copy()
        env.update({
            "LATCH_KB_DIR": str(kb_dir),
            "LATCH_MCP_DAEMON_IDLE_TTL_SEC": "60",
            "CLAUDE_KB_IN_MAINTENANCE": "1",
            "PYTHONPATH": str(ROOT / "src"),
        })
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, str(PROMPT_HOOK)],
            input=json.dumps({
                "session_id": "prompt-idle-session",
                "cwd": str(ROOT),
                "prompt": "what durable decisions apply to this change",
            }).encode("utf-8"),
            capture_output=True,
            timeout=5.0,
            env=env,
        )
        wall_ms = (time.perf_counter() - started) * 1000
        _assert(proc.returncode == 0, proc.stderr.decode(errors="replace"))
        output = json.loads(proc.stdout.decode("utf-8"))
        context = output["hookSpecificOutput"]["additionalContext"]
        _assert("temporarily unavailable" in context, context)
        _assert("not similarity-scored" in context, context)
        _assert("none auto-retrieved (sim below floor)" not in context.lower(), context)
        _assert(wall_ms < 250, f"idle prompt hook blocked for {wall_ms:.1f} ms")

        deadline = time.monotonic() + 35.0
        new_pid = None
        while time.monotonic() < deadline:
            new_pid = _daemon_pid(kb_dir)
            if new_pid is not None and new_pid != old_pid:
                break
            time.sleep(0.05)
        _assert(new_pid is not None and new_pid != old_pid, "hook wake did not start a new owner")
        rows = _lifecycle_rows(kb_dir)
        _assert(
            any(row.get("event") == "prompt_retrieval_degraded" for row in rows),
            "degraded prompt lifecycle event missing",
        )
        _assert(
            any(
                row.get("event") == "daemon_started"
                and row.get("reason") == "prompt_hook"
                for row in rows
            ),
            "prompt-hook startup reason missing",
        )
        print("PASS prompt_after_idle_exit_wakes_owner_and_emits_truthful_bounded_receipt")
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
        retired = [
            row for row in _lifecycle_rows(kb_dir)
            if row.get("event") == "proxy_retired"
        ]
        _assert(bool(retired), "proxy retirement lifecycle event missing")
        _assert(retired[-1].get("over_cap_duration_s") is not None, str(retired[-1]))
        print("PASS over_cap_idle_proxy_retires_itself_without_killing_peers")
    finally:
        for client in clients:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


if __name__ == "__main__":
    test_parallel_clients_share_one_heavy_owner_and_keep_context_isolated()
    test_owner_crash_restarts_on_next_call_without_replaying_inflight_work()
    test_committed_mutation_with_lost_response_is_not_replayed_or_called_retryable()
    test_idle_owner_is_reclaimed_and_lazily_recreated()
    test_prompt_embed_activity_keeps_owner_warm()
    test_prompt_after_idle_exit_wakes_owner_and_emits_truthful_bounded_receipt()
    test_over_cap_idle_proxy_retires_itself_without_killing_peers()
    print("\nAll shared MCP runtime tests pass.")
