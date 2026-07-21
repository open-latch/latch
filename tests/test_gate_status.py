from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import mcp_server  # noqa: E402


def test_gate_status_names_each_skip_cause():
    disabled = mcp_server._gate_status({
        "recommendation": None, "skipped": True, "error": "disabled",
    })
    in_compact = mcp_server._gate_status({
        "recommendation": None, "skipped": True, "error": "in-compact",
    })
    budget = mcp_server._gate_status({
        "recommendation": None, "skipped": True, "error": "daily budget cap hit",
    })

    assert "disabled" in disabled
    assert "compaction/model subprocess" in in_compact
    assert "budget cap reached" in budget
    assert len({disabled, in_compact, budget}) == 3
