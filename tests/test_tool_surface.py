"""Tool-surface trim (KB decision id=2224): fresh installs see latch_* only.

The shared daemon registry always carries BOTH name families because one
daemon serves every install on the machine.  Each install's advertised surface
is decided in its own per-host stdio proxy from the env the host launched it
with (``LATCH_TOOL_SURFACE=latch``): the proxy filters kb_* aliases out of
``tools/list`` results and hard-rejects kb_* ``tools/call`` requests with a
pointer to the latch_* name.  Installs without the flag — every install that
predates it — pass through byte-identical.

The legacy one-process-per-session fallback (``LATCH_MCP_LEGACY=1``) serves
exactly one install, so there the registry itself is pruned at startup via
``mcp_server.prune_kb_tool_aliases`` (public ``FastMCP.remove_tool``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import mcp_proxy  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


# --------------------------------------------------------------------------- #
# Flag parsing
# --------------------------------------------------------------------------- #
def test_trimmed_tool_surface_flag_parsing():
    _assert(mcp_proxy.trimmed_tool_surface({}) is False, "empty env must be full surface")
    _assert(mcp_proxy.trimmed_tool_surface({"LATCH_TOOL_SURFACE": ""}) is False,
            "blank flag must be full surface")
    _assert(mcp_proxy.trimmed_tool_surface({"LATCH_TOOL_SURFACE": "latch"}) is True,
            "flag=latch must trim")
    _assert(mcp_proxy.trimmed_tool_surface({"LATCH_TOOL_SURFACE": " Latch "}) is True,
            "flag is case/space tolerant")
    _assert(mcp_proxy.trimmed_tool_surface({"LATCH_TOOL_SURFACE": "full"}) is False,
            "unknown value must fail open to the full surface")
    print("PASS trimmed_tool_surface_flag_parsing")


# --------------------------------------------------------------------------- #
# tools/list advertisement filtering
# --------------------------------------------------------------------------- #
def _tools_list_response(names):
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"tools": [{"name": n, "description": n} for n in names]},
    }


def test_filter_tools_list_drops_kb_aliases_only():
    message = _tools_list_response(["latch_search", "kb_search", "latch_gate", "kb_gate"])
    filtered = mcp_proxy.filter_tools_list_result(message)
    names = [t["name"] for t in filtered["result"]["tools"]]
    _assert(names == ["latch_search", "latch_gate"], names)
    # Original object untouched (passthrough path preserves daemon bytes).
    _assert(len(message["result"]["tools"]) == 4, "input message must not be mutated")
    print("PASS filter_tools_list_drops_kb_aliases_only")


def test_filter_tools_list_drops_hidden_latch_diagnostics():
    # latch_runtime_status is kept in the registry but hidden from the surface.
    message = _tools_list_response(
        ["latch_search", "latch_runtime_status", "latch_gate"])
    filtered = mcp_proxy.filter_tools_list_result(message)
    names = [t["name"] for t in filtered["result"]["tools"]]
    _assert(names == ["latch_search", "latch_gate"], names)
    # ...but it is NOT rejected on call (diagnostics stay reachable by name).
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "latch_runtime_status", "arguments": {}}}
    _assert(mcp_proxy.rejected_kb_call(call) is None,
            "latch_runtime_status must stay callable, only unlisted")
    print("PASS filter_tools_list_drops_hidden_latch_diagnostics")


def test_filter_tools_list_identity_when_nothing_to_drop():
    message = _tools_list_response(["latch_search", "latch_get"])
    _assert(mcp_proxy.filter_tools_list_result(message) is message,
            "no kb_* names -> the exact original object must come back")
    malformed = {"jsonrpc": "2.0", "id": 1, "result": {"tools": "oops"}}
    _assert(mcp_proxy.filter_tools_list_result(malformed) is malformed,
            "malformed result must pass through untouched")
    error = {"jsonrpc": "2.0", "id": 2, "error": {"code": -1, "message": "x"}}
    _assert(mcp_proxy.filter_tools_list_result(error) is error,
            "error responses must pass through untouched")
    print("PASS filter_tools_list_identity_when_nothing_to_drop")


# --------------------------------------------------------------------------- #
# kb_* tools/call hard rejection
# --------------------------------------------------------------------------- #
def test_rejected_kb_call_detection():
    call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "kb_search", "arguments": {"query": "x"}}}
    _assert(mcp_proxy.rejected_kb_call(call) == "kb_search", "kb_ call must be detected")
    ok = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
          "params": {"name": "latch_search", "arguments": {}}}
    _assert(mcp_proxy.rejected_kb_call(ok) is None, "latch_ call must pass")
    listing = {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}
    _assert(mcp_proxy.rejected_kb_call(listing) is None, "tools/list is never rejected")
    malformed = {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": None}
    _assert(mcp_proxy.rejected_kb_call(malformed) is None, "malformed params tolerated")
    print("PASS rejected_kb_call_detection")


# --------------------------------------------------------------------------- #
# ProxyBridge wiring (no sockets: stub lease, capture _emit)
# --------------------------------------------------------------------------- #
class _StubLease:
    def touch(self):
        pass


def _bridge(trimmed: bool):
    bridge = object.__new__(mcp_proxy.ProxyBridge)
    bridge._pending = {}
    bridge._trimmed_surface = trimmed
    bridge._replaying = False
    bridge._replay_id = None
    bridge._lease = _StubLease()
    emitted = []
    bridge._emit = lambda line: emitted.append(bytes(line))
    return bridge, emitted


def test_daemon_tools_list_response_is_filtered_when_trimmed():
    bridge, emitted = _bridge(trimmed=True)
    bridge._pending[7] = "tools/list"
    line = (json.dumps(_tools_list_response(
        ["latch_search", "kb_search"])) + "\n").encode("utf-8")
    bridge._handle_daemon_line(line)
    _assert(len(emitted) == 1, emitted)
    out = json.loads(emitted[0].decode("utf-8"))
    names = [t["name"] for t in out["result"]["tools"]]
    _assert(names == ["latch_search"], names)
    _assert(bridge._pending == {}, "pending entry must be consumed")
    print("PASS daemon_tools_list_response_is_filtered_when_trimmed")


def test_daemon_tools_list_response_passthrough_without_flag():
    bridge, emitted = _bridge(trimmed=False)
    bridge._pending[7] = "tools/list"
    line = (json.dumps(_tools_list_response(
        ["latch_search", "kb_search"])) + "\n").encode("utf-8")
    bridge._handle_daemon_line(line)
    _assert(emitted == [line], "legacy installs must receive the exact daemon bytes")
    print("PASS daemon_tools_list_response_passthrough_without_flag")


def test_non_tools_list_responses_never_filtered():
    bridge, emitted = _bridge(trimmed=True)
    bridge._pending[9] = "tools/call latch_search"
    payload = {"jsonrpc": "2.0", "id": 9,
               "result": {"content": [{"type": "text", "text": "kb_search says hi"}]}}
    line = (json.dumps(payload) + "\n").encode("utf-8")
    bridge._handle_daemon_line(line)
    _assert(emitted == [line], "tool results must pass through untouched")
    print("PASS non_tools_list_responses_never_filtered")


def test_forward_rejects_kb_call_when_trimmed():
    bridge, emitted = _bridge(trimmed=True)
    bridge._sock = None  # _forward must return before touching the socket
    call = {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "kb_gate", "arguments": {}}}
    bridge._forward((json.dumps(call) + "\n").encode("utf-8"))
    _assert(len(emitted) == 1, emitted)
    out = json.loads(emitted[0].decode("utf-8"))
    _assert(out["id"] == 11 and "error" in out, out)
    _assert("latch_gate" in out["error"]["message"],
            "rejection must point to the latch_* name")
    _assert(bridge._pending == {}, "rejected calls must not be recorded as pending")
    print("PASS forward_rejects_kb_call_when_trimmed")


# --------------------------------------------------------------------------- #
# Legacy stdio fallback: registry prune
# --------------------------------------------------------------------------- #
def test_legacy_prune_removes_kb_aliases_from_registry():
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception:
        print("SKIP legacy_prune_removes_kb_aliases_from_registry (no mcp package)")
        return
    import mcp_server

    server = FastMCP("probe")

    @server.tool(name="latch_probe")
    def _probe() -> str:
        """probe"""
        return "ok"

    server.add_tool(_probe, name="kb_probe")
    mcp_server.prune_kb_tool_aliases(server)
    names = sorted(t.name for t in server._tool_manager.list_tools())
    _assert(names == ["latch_probe"], names)
    print("PASS legacy_prune_removes_kb_aliases_from_registry")


def main():
    test_trimmed_tool_surface_flag_parsing()
    test_filter_tools_list_drops_kb_aliases_only()
    test_filter_tools_list_drops_hidden_latch_diagnostics()
    test_filter_tools_list_identity_when_nothing_to_drop()
    test_rejected_kb_call_detection()
    test_daemon_tools_list_response_is_filtered_when_trimmed()
    test_daemon_tools_list_response_passthrough_without_flag()
    test_non_tools_list_responses_never_filtered()
    test_forward_rejects_kb_call_when_trimmed()
    test_legacy_prune_removes_kb_aliases_from_registry()
    print("OK test_tool_surface")


if __name__ == "__main__":
    main()
