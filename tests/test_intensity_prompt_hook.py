from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "hooks"))

import user_prompt_submit as ups  # noqa: E402


def _stub_main(monkeypatch, tmp_path: Path, *, intensity: str, prompt: str) -> list[dict]:
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
    monkeypatch.setattr(ups, "latch_intensity", lambda: intensity)
    monkeypatch.setattr(ups, "_mission_control_directive", lambda _cwd, _prompt: "")
    monkeypatch.setattr(ups, "_take_cite_nudge", lambda _cwd, _sid: 0)
    monkeypatch.setattr(ups, "_write_log", lambda _cwd, row: logs.append(dict(row)))
    return logs


def test_tier_retrieval_policy() -> None:
    assert ups._should_retrieve_for_intensity("quiet", None) is False
    assert ups._should_retrieve_for_intensity("standard", None) is True
    assert ups._should_retrieve_for_intensity("standard", 0.69) is True
    assert ups._should_retrieve_for_intensity("standard", 0.70) is False
    assert ups._should_retrieve_for_intensity("full", 0.99) is True


def test_quiet_never_wakes_retrieval_but_keeps_correction(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="quiet",
        prompt="that stored decision is wrong from now on",
    )
    monkeypatch.setattr(
        ups.mcp_broker,
        "read_discovery",
        lambda: (_ for _ in ()).throw(AssertionError("Quiet touched broker")),
    )
    monkeypatch.setattr(
        ups,
        "_retrieve_and_inject",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Quiet retrieved")
        ),
    )

    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "Possible KB correction signal" in context
    assert "Standing-guideline signal" not in context
    assert logs[-1]["intensity"] == "quiet"
    assert logs[-1]["skip"] == "intensity_quiet"
    assert logs[-1]["context_chars"] == len(context)


def test_standard_same_topic_is_silent(monkeypatch, tmp_path: Path, capsys) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="standard",
        prompt="continue reviewing the same implementation detail",
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: {"ready": True})

    def retrieve(_cwd, _sid, _prompt, row, **_kwargs):
        row["skip"] = "standard_same_topic"
        row["topic_sim"] = 0.91
        return []

    monkeypatch.setattr(ups, "_retrieve_and_inject", retrieve)
    assert ups.main() == 0
    assert capsys.readouterr().out == ""
    assert logs[-1]["context_chars"] == 0
    assert logs[-1]["intensity"] == "standard"


def test_standard_degraded_notice_is_short_and_visible(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="standard",
        prompt="switch to a completely different deployment problem",
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda _cwd: True)
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)
    assert ups.main() == 0
    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert context.startswith(
        "Latch Standard could not run this prompt's topic-similarity check"
    )
    assert "could not determine whether this prompt qualified" in context
    assert "## KB auto-retrieval" not in context
    assert logs[-1]["skip"] == "embed_daemon_unavailable"
    assert logs[-1]["context_chars"] == len(context)


def test_standard_degraded_notice_repeats_while_prompts_remain_unscored(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="standard",
        prompt="continue with another eligible deployment prompt",
    )
    monkeypatch.setattr(ups.mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda _cwd: True)
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)

    for expected_rows in (1, 2):
        assert ups.main() == 0
        output = json.loads(capsys.readouterr().out)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert "could not run this prompt's topic-similarity check" in context
        assert len(logs) == expected_rows
        assert logs[-1]["skip"] == "embed_daemon_unavailable"
        assert logs[-1]["context_chars"] == len(context)


def test_degraded_path_keeps_profile_and_citation_nudges(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="standard",
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
    assert context.startswith("MISSION\n\nCITE-2\n\nLatch Standard")
    assert logs[-1]["mission_control"] is True
    assert logs[-1]["cite_nudge"] == 2


def test_full_retains_no_hits_receipt(monkeypatch, tmp_path: Path, capsys) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="full",
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
    assert logs[-1]["intensity"] == "full"
    assert logs[-1]["context_chars"] == len(context)


def test_retrieval_error_keeps_independent_safety_context(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = _stub_main(
        monkeypatch,
        tmp_path,
        intensity="standard",
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
    assert logs[-1]["error"] == "RuntimeError: boom"
    assert logs[-1]["context_chars"] == len(context)


def test_standard_first_prompt_copy_does_not_claim_topic_change() -> None:
    context = ups._format_injection(
        [{"id": 7, "kind": "decision", "title": "Use SQLite", "score": 0.8}],
        intensity="standard",
    )
    assert "first prompt or the topic changed" in context
