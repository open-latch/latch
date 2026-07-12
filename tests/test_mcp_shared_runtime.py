"""Concurrency, ownership, attribution, and recovery tests for shared MCP."""
from __future__ import annotations

import json
import os
import queue
import sqlite3
import shutil
import signal
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest


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
        server_path: Path | None = None,
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
            [sys.executable, str(server_path or SERVER)],
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


def _discovery(kb_dir: Path) -> tuple[Path, dict[str, Any]]:
    paths = list((kb_dir / "runtime" / "mcp-runtimes").glob("*/mcp-daemon.json"))
    _assert(len(paths) == 1, f"expected one discovery document, got {paths}")
    return paths[0], json.loads(paths[0].read_text(encoding="utf-8"))


class ResponseDropRelay:
    """Test-only TCP relay that drops one completed response at the transport."""

    def __init__(self, upstream: dict[str, Any], *, drop_response_id: int):
        self.upstream = dict(upstream)
        self.drop_response_id = drop_response_id
        self.dropped = threading.Event()
        self._stop = threading.Event()
        self._connections: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.2)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._connections.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._lock:
            self._connections.discard(sock)

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                downstream, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._handle(downstream)

    def _handle(self, downstream: socket.socket) -> None:
        try:
            upstream = socket.create_connection(
                (self.upstream["host"], int(self.upstream["port"])), timeout=5
            )
            upstream.settimeout(None)
        except OSError:
            self._close(downstream)
            return
        self._track(downstream)
        self._track(upstream)

        buffer = bytearray()
        try:
            while True:
                readable, _, _ = select.select([downstream, upstream], [], [], 0.2)
                if downstream in readable:
                    chunk = downstream.recv(65536)
                    if not chunk:
                        return
                    upstream.sendall(chunk)
                if upstream in readable:
                    chunk = upstream.recv(65536)
                    if not chunk:
                        return
                    buffer.extend(chunk)
                    while b"\n" in buffer:
                        raw, _, rest = buffer.partition(b"\n")
                        buffer = bytearray(rest)
                        try:
                            message = json.loads(raw)
                        except ValueError:
                            message = {}
                        if (
                            not self.dropped.is_set()
                            and message.get("id") == self.drop_response_id
                            and ("result" in message or "error" in message)
                        ):
                            self.dropped.set()
                            return
                        downstream.sendall(raw + b"\n")
        except OSError:
            return
        finally:
            self._close(downstream)
            self._close(upstream)
            self._untrack(downstream)
            self._untrack(upstream)

    def close(self) -> None:
        self._stop.set()
        self._close(self._listener)
        with self._lock:
            connections = list(self._connections)
        for sock in connections:
            self._close(sock)
        self._thread.join(timeout=2)


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
        _assert(first["proxy_pool"]["scope"] == "owner_runtime_key", str(first))
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


def test_retained_proxy_recovers_after_in_place_compatible_upgrade() -> None:
    kb_dir = _temp_vault()
    install = Path(tempfile.mkdtemp(prefix="latch-upgrade-install-"))
    install_src = install / "src"
    bootstrap: McpClient | None = None
    client: McpClient | None = None
    try:
        shutil.copytree(
            ROOT / "src",
            install_src,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        env = os.environ.copy()
        env.update({
            "LATCH_HOME": str(ROOT),
            "PYTHONPATH": str(install_src),
            "PYTHONDONTWRITEBYTECODE": "1",
        })

        def runtime_key() -> str:
            return subprocess.check_output(
                [sys.executable, "-c", "import mcp_broker; print(mcp_broker.RUNTIME_KEY)"],
                env=env,
                text=True,
                timeout=10.0,
            ).strip()

        current_key = runtime_key()
        runtime_module = install_src / "mcp_runtime.py"
        stat = runtime_module.stat()
        os.utime(runtime_module, (stat.st_atime + 60, stat.st_mtime + 60))
        _assert(runtime_key() == current_key, "runtime key changed from mtime only")

        # Produce an older content key while retaining the explicit capability
        # epoch. This is the supported future in-place-upgrade path.
        broker_path = install_src / "mcp_broker.py"
        broker_source = broker_path.read_text(encoding="utf-8")
        broker_path.write_text(
            broker_source + "\n# compatible-upgrade-test-fingerprint\n",
            encoding="utf-8",
        )
        old_key = runtime_key()
        _assert(old_key != current_key, "test install did not produce an old runtime key")

        bootstrap = McpClient(
            kb_dir,
            "in-place-upgrade-bootstrap",
            server_path=install_src / "mcp_server.py",
            env_overrides={
                "LATCH_HOME": str(ROOT),
                "PYTHONPATH": str(install_src),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        client = McpClient(
            kb_dir,
            "in-place-upgrade-session",
            server_path=install_src / "mcp_server.py",
            env_overrides={
                "LATCH_HOME": str(ROOT),
                "PYTHONPATH": str(install_src),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        before = client.status()
        old_pid = before["process_pid"]
        _assert(before["daemon"]["runtime_key"] == old_key, str(before))

        # Replace the same live install path, then kill the old owner. The
        # retained proxy still has old_key cached in memory and must be aliased
        # to one owner started from the updated source.
        shutil.copy2(ROOT / "src" / "mcp_broker.py", broker_path)
        _assert(runtime_key() == current_key, "updated install key is not content-stable")
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)

        after = client.status()
        _assert(after["process_pid"] != old_pid, str(after))
        _assert(after["daemon"]["runtime_key"] == current_key, str(after))
        _assert(after["connection"]["runtime_key"] == old_key, str(after))
        _assert(after["proxy_pool"]["scope"] == "owner_runtime_key", str(after))
        _assert(after["proxy_pool"]["live_leases"] == 2, str(after))
        discoveries = list(
            (kb_dir / "runtime" / "mcp-runtimes").glob("*/mcp-daemon.json")
        )
        owner_pids = {
            int(json.loads(path.read_text(encoding="utf-8"))["pid"])
            for path in discoveries
        }
        _assert(owner_pids == {after["process_pid"]}, str(owner_pids))
        client_connection_id = before["connection"]["connection_id"]
        migrated_lease = (
            kb_dir
            / "runtime"
            / "mcp-runtimes"
            / current_key
            / "mcp-proxies"
            / f"{client_connection_id}.json"
        )
        old_lease = (
            kb_dir
            / "runtime"
            / "mcp-runtimes"
            / old_key
            / "mcp-proxies"
            / f"{client_connection_id}.json"
        )
        _assert(migrated_lease.exists(), "capable proxy did not migrate its lease")
        _assert(not old_lease.exists(), "historical lease remained after migration")
        _assert(client.process.poll() is None, "retained proxy exited after upgrade")
        print("PASS retained_proxy_recovers_after_in_place_compatible_upgrade")
    finally:
        if client is not None:
            client.close()
        if bootstrap is not None:
            bootstrap.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)
        shutil.rmtree(install, ignore_errors=True)


def _copy_git_src(commit: str, target: Path) -> None:
    files = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", commit, "src"],
        cwd=ROOT,
        text=True,
        timeout=15.0,
    ).splitlines()
    for relative in files:
        destination = target / Path(relative).relative_to("src")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            subprocess.check_output(
                ["git", "show", f"{commit}:{relative}"],
                cwd=ROOT,
                timeout=15.0,
            )
        )


def _require_historical_commit(commit: str) -> None:
    available = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if available.returncode != 0:
        pytest.skip(f"historical commit {commit} is not present in this checkout")


def _assert_historical_proxy_requires_fresh_task(commit: str) -> None:
    kb_dir = _temp_vault()
    install = Path(tempfile.mkdtemp(prefix=f"latch-{commit}-install-"))
    install_src = install / "src"
    client: McpClient | None = None
    try:
        _copy_git_src(commit, install_src)
        overrides = {
            "LATCH_HOME": str(ROOT),
            "PYTHONPATH": str(install_src),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        client = McpClient(
            kb_dir,
            f"historical-{commit}",
            server_path=install_src / "mcp_server.py",
            env_overrides=overrides,
        )
        old_pid = client.status()["process_pid"]
        shutil.rmtree(install_src)
        shutil.copytree(
            ROOT / "src",
            install_src,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        os.kill(old_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)

        started = time.monotonic()
        try:
            client.status()
        except AssertionError as exc:
            message = str(exc).lower()
            _assert("start a fresh task" in message, message)
            _assert("request was not executed" in message, message)
        else:
            raise AssertionError("pre-capability proxy joined the current runtime")
        _assert(time.monotonic() - started < 10.0, "fresh-task rejection was not bounded")
        _assert(client.process.poll() is None, "historical proxy exited before surfacing error")
        owner_pids = {
            int(json.loads(path.read_text(encoding="utf-8"))["pid"])
            for path in (kb_dir / "runtime" / "mcp-runtimes").glob(
                "*/mcp-daemon.json"
            )
        }
        _assert(len(owner_pids) == 1, str(owner_pids))
    finally:
        if client is not None:
            client.close()
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)
        shutil.rmtree(install, ignore_errors=True)


def test_pre_capability_registry_proxy_requires_fresh_task_after_upgrade() -> None:
    _require_historical_commit("7bcb86d")
    _assert_historical_proxy_requires_fresh_task("7bcb86d")


@pytest.mark.skipif(
    os.name == "nt",
    reason="fa162bd's own os.kill(pid, 0) probe is destructive on Windows",
)
def test_fa162bd_pre_registry_proxy_requires_fresh_task_after_upgrade() -> None:
    _require_historical_commit("fa162bd")
    _assert_historical_proxy_requires_fresh_task("fa162bd")


@pytest.mark.parametrize(
    ("pre_registry", "capability_epoch"),
    ((False, None), (False, 1), (True, None)),
    ids=("keyed-pre-capability", "keyed-epoch-1", "fa162bd-pre-registry"),
)
def test_historical_transport_receives_fresh_task_error_cross_platform(
    pre_registry: bool,
    capability_epoch: int | None,
) -> None:
    """Exercise both historical discovery layouts and epoch-less wire contract."""
    kb_dir = _temp_vault()
    env = os.environ.copy()
    env.update({
        "LATCH_HOME": str(ROOT),
        "LATCH_KB_DIR": str(kb_dir),
        "LATCH_MCP_DAEMON_PROCESS": "1",
        "LATCH_MCP_RUNTIME_KEY": "historical-epochless",
        "LATCH_MCP_INITIAL_PROJECT_CWD": str(ROOT),
        "LATCH_MCP_DAEMON_IDLE_TTL_SEC": "60",
        "CLAUDE_KB_IN_MAINTENANCE": "1",
        "PYTHONPATH": str(ROOT / "src"),
    })
    if capability_epoch is None:
        env.pop("LATCH_MCP_PROXY_CAPABILITY_EPOCH", None)
    else:
        env["LATCH_MCP_PROXY_CAPABILITY_EPOCH"] = str(capability_epoch)
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "src" / "mcp_daemon.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        requested_key = "historical-epochless"
        legacy_discovery = (
            kb_dir / "mcp-daemon.json"
            if pre_registry
            else kb_dir
            / "runtime"
            / "mcp-runtimes"
            / requested_key
            / "mcp-daemon.json"
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and not legacy_discovery.exists():
            if process.poll() is not None:
                raise AssertionError(
                    f"compatibility owner exited: {process.stderr.read().decode(errors='replace')}"
                )
            time.sleep(0.05)
        _assert(legacy_discovery.exists(), "pre-registry discovery was not published")
        payload = json.loads(legacy_discovery.read_text(encoding="utf-8"))
        with socket.create_connection(
            (payload["host"], int(payload["port"])), timeout=5.0
        ) as sock:
            stream = sock.makefile("rwb")
            prelude = {
                "op": "mcp",
                "protocol": 1,
                "token": payload["token"],
                "runtime_key": requested_key,
                "connection_id": "fa162bd-wire-contract",
                "project_cwd": str(ROOT),
                "proxy_pid": os.getpid(),
            }
            if capability_epoch is not None:
                prelude["proxy_capability_epoch"] = capability_epoch
            stream.write(json.dumps(prelude).encode() + b"\n")
            stream.write(json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "fa162bd", "version": "1"},
                },
            }).encode() + b"\n")
            stream.flush()
            initialized = json.loads(stream.readline())
            _assert("result" in initialized, str(initialized))
            stream.write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
            )
            stream.write(
                b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
            )
            stream.flush()
            rejected = json.loads(stream.readline())
            message = str(rejected.get("error", {}).get("message", "")).lower()
            _assert("start a fresh task" in message, str(rejected))
            _assert("request was not executed" in message, str(rejected))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        _stop_daemon(kb_dir)
        shutil.rmtree(kb_dir, ignore_errors=True)


def test_committed_mutation_with_lost_response_is_not_replayed_or_called_retryable() -> None:
    kb_dir = _temp_vault()
    bootstrap: McpClient | None = None
    client: McpClient | None = None
    relay: ResponseDropRelay | None = None
    title = "post-commit response-loss sentinel"
    try:
        bootstrap = McpClient(kb_dir, "response-drop-bootstrap")
        discovery_path, discovery = _discovery(kb_dir)
        relay = ResponseDropRelay(discovery, drop_response_id=2)
        discovery["port"] = relay.port
        discovery_path.write_text(json.dumps(discovery) + "\n", encoding="utf-8")
        client = McpClient(kb_dir, "unknown-outcome-session")
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
        _assert(relay.dropped.is_set(), "transport fixture did not drop response")

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
        if bootstrap is not None:
            bootstrap.close()
        if relay is not None:
            relay.close()
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
    test_retained_proxy_recovers_after_in_place_compatible_upgrade()
    test_committed_mutation_with_lost_response_is_not_replayed_or_called_retryable()
    test_idle_owner_is_reclaimed_and_lazily_recreated()
    test_prompt_embed_activity_keeps_owner_warm()
    test_prompt_after_idle_exit_wakes_owner_and_emits_truthful_bounded_receipt()
    test_over_cap_idle_proxy_retires_itself_without_killing_peers()
    print("\nAll shared MCP runtime tests pass.")
