from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

import user_prompt_submit as ups  # noqa: E402


def _stub_main(monkeypatch, tmp_path: Path, *, prompt: str) -> list[dict]:
    logs: list[dict] = []
    monkeypatch.setattr(ups, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(ups, "is_disabled", lambda: False)
    monkeypatch.setattr(ups, "is_in_compact", lambda: False)
    monkeypatch.setattr(ups, "read_hook_input", lambda: {})
    monkeypatch.setattr(ups, "session_id", lambda _payload: "session-1")
    monkeypatch.setattr(ups, "project_cwd", lambda _payload: str(tmp_path))
    monkeypatch.setattr(
        ups,
        "hook_field",
        lambda _payload, *_keys, **_kwargs: prompt,
    )
    monkeypatch.setattr(ups, "_mission_control_directive", lambda _cwd, _prompt: "")
    monkeypatch.setattr(ups, "_take_cite_nudge", lambda _cwd, _sid: 0)
    monkeypatch.setattr(ups, "_write_log", lambda _cwd, row: logs.append(dict(row)))
    return logs


def test_candidate_selection_uses_fixed_retrieval_bounds() -> None:
    assert (ups.MAX_INJECT, ups.SIM_FLOOR) == (5, 0.55)
    candidates = [
        {"id": idx, "kind": "decision", "score": score}
        for idx, score in enumerate((0.90, 0.80, 0.70, 0.60, 0.55, 0.54), 1)
    ]
    chosen = ups._select_candidates(
        candidates,
        set(),
        sim_floor=ups.SIM_FLOOR,
        max_inject=ups.MAX_INJECT,
    )
    assert [row["id"] for row in chosen] == [1, 2, 3, 4, 5]


def test_every_eligible_prompt_runs_retrieval_and_keeps_guideline_nudge(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        prompt="always keep our database migrations backward compatible",
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: {"ready": True})
    calls: list[str] = []

    def retrieve(cwd, _sid, _prompt, _row, **_kwargs):
        calls.append(cwd)
        return []

    monkeypatch.setattr(ups, "_retrieve_and_inject", retrieve)

    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert calls == [str(tmp_path)]
    assert "Standing-guideline signal" in context
    assert "KB hits — no new hits injected" in context
    assert "intensity" not in logs[-1]


def test_degraded_notice_is_visible_and_repeats_while_unscored(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        prompt="continue with another eligible deployment prompt",
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda _cwd: True)
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)

    for expected_rows in (1, 2):
        assert ups.main() == 0
        output = json.loads(capsys.readouterr().out)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert context.startswith("## KB auto-retrieval temporarily unavailable")
        assert "not similarity-scored" in context
        assert len(logs) == expected_rows
        assert logs[-1]["skip"] == "embed_daemon_unavailable"
        assert logs[-1]["context_chars"] == len(context)


def test_degraded_path_keeps_profile_and_citation_nudges(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        prompt="switch to a completely different deployment problem",
    )
    monkeypatch.setattr(ups, "_mission_control_directive", lambda *_args: "MISSION")
    monkeypatch.setattr(ups, "_take_cite_nudge", lambda *_args: 2)
    monkeypatch.setattr(
        ups,
        "profiles",
        SimpleNamespace(
            render_cite_correction_directive=lambda count: f"CITE-{count}"
        ),
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda _cwd: True)
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)

    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(
        "MISSION\n\nCITE-2\n\n## KB auto-retrieval temporarily unavailable"
    )
    assert logs[-1]["mission_control"] is True
    assert logs[-1]["cite_nudge"] == 2


def test_no_hits_receipt_remains_visible(monkeypatch, tmp_path: Path, capsys) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        prompt="please review this implementation plan now",
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: {"ready": True})
    monkeypatch.setattr(ups, "_retrieve_and_inject", lambda *_args, **_kwargs: [])

    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "KB hits — no new hits injected" in context
    assert "already be active" in context
    assert "excluded from prompt surfacing" in context
    assert "below the similarity floor" in context
    assert logs[-1]["context_chars"] == len(context)


def test_retrieval_error_keeps_independent_safety_context(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        prompt="that stored decision is wrong and needs correction",
    )
    monkeypatch.setattr(ups, "_mission_control_directive", lambda *_args: "MISSION")
    monkeypatch.setattr(ups, "_take_cite_nudge", lambda *_args: 2)
    monkeypatch.setattr(
        ups,
        "profiles",
        SimpleNamespace(
            render_cite_correction_directive=lambda count: f"CITE-{count}"
        ),
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: {"ready": True})
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        ups,
        "_retrieve_and_inject",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(ups, "log", lambda _message: None)

    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("MISSION\n\nCITE-2\n\n## ⚠ Possible KB correction signal")
    assert "## KB auto-retrieval failed" in context
    assert logs[-1]["error"] == "RuntimeError: boom"
    assert logs[-1]["context_chars"] == len(context)


def test_retrieval_error_without_nudges_is_still_visible(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        prompt="please review the selected database schema compatibility",
    )
    lifecycle_events: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: {"ready": True})
    monkeypatch.setattr(
        ups.mcp_broker,
        "emit_lifecycle",
        lambda *args, **kwargs: lifecycle_events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        ups,
        "_retrieve_and_inject",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private schema detail")
        ),
    )
    monkeypatch.setattr(ups, "log", lambda _message: None)

    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("## KB auto-retrieval failed")
    assert "degraded retrieval, not a no-hit result" in context
    assert "private schema detail" not in context
    assert logs[-1]["error"] == "RuntimeError: private schema detail"
    assert lifecycle_events == [
        (("prompt_retrieval_degraded",), {"reason": "retrieval_error"})
    ]


def test_hit_copy_points_to_on_demand_orientation() -> None:
    context = ups._format_injection(
        [{"id": 7, "kind": "decision", "title": "Use SQLite", "score": 0.8}]
    )
    assert "Actively query the KB" in context
    assert "latch_project_direction" in context
    assert "SessionStart brief" not in context
