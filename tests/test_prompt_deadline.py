"""Deadline failures must not be presented as evidence of a broken daemon."""
import json
import time
from types import SimpleNamespace

import pytest

from latch.hooks import user_prompt_submit as ups
from test_prompt_hook_context import _stub_main


def test_healthy_owner_with_exhausted_local_deadline(monkeypatch, tmp_path, capsys):
    logs = _stub_main(monkeypatch, tmp_path, prompt="retrieve the relevant stored decisions")
    monkeypatch.setattr(ups, "_PROCESS_STARTED", time.perf_counter() - 10)
    monkeypatch.setattr(ups.mcp_broker, "inspect_live_embed_discovery", lambda **kw: SimpleNamespace(status="ready"))
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *a, **kw: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda *a: pytest.fail("healthy owner must not be woken"))
    monkeypatch.setattr(ups, "_retrieve_and_inject", lambda *a, **kw: pytest.fail("expired retrieval attempted"))
    assert ups.main() == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "local retrieval budget" in context
    assert "background wake" not in context
    assert "unreachable" not in context
    assert logs[-1]["skip"] == "local_budget_exhausted"
    assert logs[-1]["deadline_stage"] == "before_discovery"
    assert logs[-1]["overran_budget"] is True


def test_embed_expired_deadline_does_not_attempt_rpc_or_wake(monkeypatch):
    monkeypatch.setattr(ups, "embeddings", SimpleNamespace(embed_remote_result=lambda *a, **kw: pytest.fail("RPC attempted")))
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda *a: pytest.fail("wake attempted"))
    row = {}
    assert ups._embed_with_bounded_wake("hello", ".", time.perf_counter() - 1, row) is None
    assert row == {"embed_status": "local_budget_exhausted", "deadline_stage": "before_embedding_rpc"}


@pytest.mark.parametrize("status", ["context_rejected", "authentication_rejected", "owner_start_failed", "discovery_rejected", "owner_starting"])
def test_preflight_failures_keep_cause_and_never_claim_wake(monkeypatch, tmp_path, capsys, status):
    logs = _stub_main(monkeypatch, tmp_path, prompt="retrieve the relevant stored decisions")
    monkeypatch.setattr(ups.mcp_broker, "inspect_live_embed_discovery", lambda **kw: SimpleNamespace(status=status))
    monkeypatch.setattr(ups.mcp_broker, "emit_lifecycle", lambda *a, **kw: None)
    monkeypatch.setattr(ups.mcp_broker, "request_daemon_start", lambda *a: pytest.fail("wake attempted"))
    monkeypatch.setattr(ups, "_load_runtime", lambda: pytest.fail("heavy imports attempted"))
    assert ups.main() == 0
    assert logs[-1]["skip"] == status
    assert "background wake" not in capsys.readouterr().out


def test_import_cost_consumes_same_deadline(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ups.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(ups, "_load_runtime", lambda: clock.__setitem__(0, 12.0))
    monkeypatch.setattr(ups, "_embed_with_bounded_wake", lambda *a: pytest.fail("expired RPC attempted"))
    with pytest.raises(ups._BudgetExhausted, match="runtime_imports"):
        ups._retrieve_and_inject(".", "sid", "prompt", {}, deadline=11.0)


@pytest.mark.parametrize("value", ["99", "2001", "NaN", "inf", "1.5", "", "True"])
def test_invalid_environment_budget_uses_bounded_default(monkeypatch, value):
    monkeypatch.setenv("LATCH_PROMPT_BUDGET_MS", value)
    assert ups._configured_budget(".") == (ups.HARD_BUDGET_MS, "default_invalid_setting")


def test_budget_setting_survives_install_location_and_env_takes_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("LATCH_PROMPT_BUDGET_MS", raising=False)
    monkeypatch.setattr(ups, "project_dir", lambda cwd: tmp_path)
    settings = tmp_path / "runtime_settings.json"
    settings.write_text(json.dumps({"prompt_hook_budget_ms": 900, "daemon_idle_ttl_s": 60}))
    assert ups._configured_budget("first-install") == (900, "vault")
    assert ups._configured_budget("upgraded-install") == (900, "vault")
    monkeypatch.setenv("LATCH_PROMPT_BUDGET_MS", "100")
    assert ups._configured_budget(".") == (100, "environment")
    monkeypatch.setenv("LATCH_PROMPT_BUDGET_MS", "2000")
    assert ups._configured_budget(".") == (2000, "environment")


@pytest.mark.parametrize("value", [True, 750.5, 50, 5000])
def test_invalid_vault_budget_is_not_coerced(monkeypatch, tmp_path, value):
    monkeypatch.delenv("LATCH_PROMPT_BUDGET_MS", raising=False)
    monkeypatch.setattr(ups, "project_dir", lambda cwd: tmp_path)
    (tmp_path / "runtime_settings.json").write_text(json.dumps({"prompt_hook_budget_ms": value}))
    assert ups._configured_budget(".") == (ups.HARD_BUDGET_MS, "default_invalid_setting")
