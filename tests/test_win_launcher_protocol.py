"""End-to-end MCP protocol/lifecycle test for the windowless Windows launcher.

Unlike the pipe-echo unit test, this drives a real MCP session through
``mcp_launcher_win.py`` exactly as an MCP host would:

    initialize -> notifications/initialized -> tools/list
        -> latch_insert (seed) -> latch_recent(limit=5) -> validate JSON-RPC
        -> close stdin -> assert launcher + child exit, no descendant remains.

Windows-only; requires the built ``.venv`` and a writable project KB.
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


def _env() -> dict:
    env = os.environ.copy()
    env["LATCH_ADAPTER"] = "cursor"
    env.setdefault("LATCH_PYTHON", sys.executable)
    return env


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


def test_launcher_full_mcp_lifecycle_and_reaping(tmp_path):
    assert PYTHONW.is_file(), PYTHONW
    proc = subprocess.Popen(
        [str(PYTHONW), str(LAUNCHER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(tmp_path), env=_env(),
    )
    out = _Reader(proc.stdout)
    out.start()
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "pytest", "version": "0"}}})
        init = _result(out.next_json())
        assert "serverInfo" in init or "capabilities" in init, init.keys()
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = _result(out.next_json()).get("tools", [])
        names = {t.get("name") for t in tools}
        assert "latch_recent" in names, names

        _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "latch_insert", "arguments": {
                         "kind": "fact", "title": "protocol test seed",
                         "body": "win launcher protocol regression seed"}}})
        assert _result(out.next_json()).get("isError") is not True

        _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "latch_recent", "arguments": {"limit": 5}}})
        recent = _result(out.next_json())
        assert recent.get("isError") is not True
        content = recent.get("content")
        assert isinstance(content, list) and len(content) >= 1, recent.keys()

        child_pids = _descendant_pids(proc.pid)
        assert child_pids, "expected at least the server child under the launcher"
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        code = proc.wait(timeout=30)

    assert code == 0, code
    time.sleep(2.0)  # allow JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE to reap
    remaining = _descendant_pids(proc.pid)
    assert not remaining, f"orphaned descendants after EOF: {remaining}"
