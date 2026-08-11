"""End-to-end MCP protocol/lifecycle test for the windowless Windows launcher.

Unlike the pipe-echo unit test, this drives a real MCP session through
``mcp_launcher_win.py`` exactly as an MCP host would:

    initialize -> notifications/initialized -> tools/list
        -> latch_insert (seed) -> latch_recent(limit=5) -> validate JSON-RPC
        -> close stdin -> assert the per-connection proxy exits while the
           shared daemon remains available to a second launcher.

Windows-only; all writes are pinned to pytest's temporary KB.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

if os.name != "nt":
    pytest.skip("windowless launcher is Windows-only", allow_module_level=True)

SRC = Path(__file__).resolve().parent.parent / "src"
LAUNCHER = SRC / "mcp_launcher_win.py"
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
sys.path.insert(0, str(SRC))

import paths  # noqa: E402


def _env(tmp_path: Path, kb_dir: Path) -> dict:
    env = os.environ.copy()
    # The autouse isolated_scope_control fixture points LATCH_HOME at a bare
    # tmp dir with no src/schema.sql; the launcher child needs the real
    # checkout as its install home while LATCH_KB_DIR keeps the data plane
    # in the disposable vault.
    env["LATCH_HOME"] = str(SRC.parent)
    env.pop("CLAUDE_KB_HOME", None)
    env["LATCH_ADAPTER"] = "cursor"
    env["LATCH_PYTHON"] = sys.executable
    env["LATCH_KB_DIR"] = str(kb_dir)
    env["LATCH_MCP_LAUNCHER_LOG"] = str(tmp_path / "launcher.log")
    return env


def _pid_alive(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        k32.CloseHandle(handle)


def _terminate_pid(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
    if not handle:
        return
    try:
        k32.TerminateProcess(handle, 0)
    finally:
        k32.CloseHandle(handle)


def _descendant_pids(root: int) -> set[int]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" |"
        "Select-Object ProcessId,ParentProcessId | ConvertTo-Json -Compress"
    )
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    if not out:
        return set()
    rows = json.loads(out)
    rows = rows if isinstance(rows, list) else [rows]
    by_parent: dict[int, list[int]] = {}
    for r in rows:
        by_parent.setdefault(int(r["ParentProcessId"]), []).append(int(r["ProcessId"]))
    seen, stack = set(), [root]
    while stack:
        for child in by_parent.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return seen


class _Reader(threading.Thread):
    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream, self.q = stream, queue.Queue()

    def run(self):
        try:
            for line in self.stream:
                self.q.put(line)
        finally:
            self.q.put(None)

    def next_json(self, timeout=60.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                line = self.q.get(timeout=max(0.01, end - time.time()))
            except queue.Empty:
                break
            if line is None:
                return None
            line = line.strip()
            if line:
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return None


def _send(proc, obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
    proc.stdin.flush()


def _result(msg):
    assert msg is not None, "no response"
    assert "error" not in msg, msg.get("error")
    return msg.get("result") or {}


def _start_launcher(tmp_path: Path, kb_dir: Path):
    proc = subprocess.Popen(
        [str(PYTHONW), str(LAUNCHER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(tmp_path), env=_env(tmp_path, kb_dir),
    )
    out = _Reader(proc.stdout)
    out.start()
    return proc, out


def _initialize(proc, out, *, request_id: int) -> None:
    _send(proc, {"jsonrpc": "2.0", "id": request_id, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "pytest", "version": "0"}}})
    init = _result(out.next_json())
    assert "serverInfo" in init or "capabilities" in init, init.keys()
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def _daemon_pid(kb_dir: Path) -> int:
    deadline = time.time() + 30
    while time.time() < deadline:
        for path in kb_dir.rglob("mcp-daemon.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                pid = payload.get("pid")
                if isinstance(pid, int) and _pid_alive(pid):
                    return pid
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    raise AssertionError("shared daemon discovery did not appear in temporary KB")


def _close_launcher(proc) -> int:
    try:
        proc.stdin.close()
    except OSError:
        pass
    return proc.wait(timeout=30)


def test_launcher_full_mcp_lifecycle_and_shared_daemon_survival(tmp_path):
    assert PYTHONW.is_file(), PYTHONW
    kb_dir = paths.project_dir(str(tmp_path / "win-launcher-protocol"))
    assert kb_dir.is_relative_to(paths.validated_test_root() / "vaults")
    first = second = None
    daemon_pid = None
    try:
        first, first_out = _start_launcher(tmp_path, kb_dir)
        _initialize(first, first_out, request_id=1)

        _send(first, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = _result(first_out.next_json()).get("tools", [])
        names = {t.get("name") for t in tools}
        assert "latch_recent" in names, names

        _send(first, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "latch_insert", "arguments": {
                         "kind": "fact", "title": "protocol test seed",
                         "body": "win launcher protocol regression seed"}}})
        assert _result(first_out.next_json()).get("isError") is not True

        _send(first, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "latch_recent", "arguments": {"limit": 5}}})
        recent = _result(first_out.next_json())
        assert recent.get("isError") is not True
        content = recent.get("content")
        assert isinstance(content, list) and len(content) >= 1, recent.keys()

        daemon_pid = _daemon_pid(kb_dir)
        child_pids = _descendant_pids(first.pid)
        assert child_pids, "expected at least the server child under the launcher"
        proxy_pids = child_pids - {daemon_pid}
        assert proxy_pids, child_pids

        assert _close_launcher(first) == 0
        first = None
        time.sleep(1.0)
        assert all(not _pid_alive(pid) for pid in proxy_pids), proxy_pids
        assert _pid_alive(daemon_pid), "shared daemon died with first Cursor connection"

        second, second_out = _start_launcher(tmp_path, kb_dir)
        _initialize(second, second_out, request_id=5)
        _send(second, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "latch_recent", "arguments": {"limit": 5}}})
        second_recent = _result(second_out.next_json())
        assert second_recent.get("isError") is not True
        assert isinstance(second_recent.get("content"), list)
        assert _daemon_pid(kb_dir) == daemon_pid, "second launcher did not reuse daemon"
        assert _close_launcher(second) == 0
        second = None
        assert _pid_alive(daemon_pid), "shared daemon died with second Cursor connection"
    finally:
        for proc in (first, second):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        if daemon_pid is not None:
            _terminate_pid(daemon_pid)
