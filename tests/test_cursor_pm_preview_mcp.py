"""Dependency-complete contract for the read-only Cursor PM preview MCP tool."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.hosts import cursor_gate_state as cgs  # noqa: E402
from latch.mcp import mcp_server  # noqa: E402


def test_pm_preview_mcp_tool_returns_canonical_nonwriting_receipt():
    result = mcp_server.latch_pm_preview(
        title="Ruled out path",
        body="Do not use X because Y.",
        links=[
            {"relation": "related_to", "dst": 9},
            {"dst": "7", "relation": "constrains"},
        ],
        workstream_id=1369,
    )
    assert result["ok"] is True
    assert result["write_performed"] is False
    assert result["candidate"]["links"] == [
        {"dst": 7, "relation": "constrains"},
        {"dst": 9, "relation": "related_to"},
    ]
    assert result["candidate_digest"] == cgs.pm_candidate_digest(result["candidate"])

    rejected = mcp_server.latch_pm_preview(
        title="Ruled out path", body="Do not use X because Y.", status="canonical",
    )
    assert rejected["ok"] is False and rejected["write_performed"] is False
