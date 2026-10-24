"""Fixture isolation and elected-owner cleanup in the prompt profiler."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "profile_prompt_hook.py"
_spec = importlib.util.spec_from_file_location("profile_prompt_hook", _SCRIPT)
profiler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(profiler)


class FixtureBroker:
    """The broker may select a winner different from a bootstrap process."""

    def __init__(self, vault):
        self.vault = vault
        self.owner = None
        self.live = False
        self.removed = []
        self.stopped = []

    def runtime_dir(self):
        return Path(self.vault())

    def ensure_daemon(self, source, *, start_reason):
        assert start_reason == "prompt_hook"
        self.owner = {"pid": 4321, "token": "fixture-mcp-token"}
        self.live = True
        return self.owner

    def read_discovery(self):
        return self.owner if self.live else None

    _checked_discovery = read_discovery

    def _pid_alive(self, pid):
        assert pid == self.owner["pid"]
        return self.live

    def probe_discovery(self, owner):
        return owner == self.owner and self.live

    def read_live_embed_discovery(self, *, owner_payload):
        assert owner_payload == self.owner
        return {"pid": self.owner["pid"], "token": "fixture-embed-token"}

    def remove_discovery_aliases_if_owner(self, **owner):
        self.removed.append(("mcp", owner))

    def remove_embed_discovery_if_owner(self, **owner):
        self.removed.append(("embed", owner))

    def _windows_base_command(self, env):
        return sys.executable

    def kill(self, pid, sig):
        assert pid == self.owner["pid"]
        assert sig == signal.SIGTERM
        self.stopped.append(pid)
        self.live = False


def args_for(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    return SimpleNamespace(source=str(source), output_dir=str(tmp_path / "evidence"),
                           budget_ms=750, samples=1, cold_samples=1, concurrency=1)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_parent_keeps_measurements_and_restores_environment(tmp_path, monkeypatch, capsys, cleanup_fails):
    args = args_for(tmp_path)
    monkeypatch.setenv("LATCH_MCP_RUNTIME_KEY", "inherited-runtime")
    monkeypatch.setenv("LATCH_PROMPT_BUDGET_MS", "100")
    monkeypatch.setenv("CLAUDE_KB_IN_MAINTENANCE", "1")
    original = os.environ.copy()
    imported_with = []
    child_envs = []
    broker = FixtureBroker(lambda: os.environ["LATCH_KB_DIR"])
    module = ModuleType("latch.mcp")

    def get_attribute(name):
        if name != "mcp_broker":
            raise AttributeError(name)
        imported_with.append(os.environ.copy())
        return broker

    module.__getattr__ = get_attribute
    monkeypatch.setitem(sys.modules, "latch.mcp", module)

    def kill(pid, sig):
        if broker.stopped:
            pending = json.loads((Path(args.output_dir) / "results.json").read_text())
            assert pending["cleanup_status"] == "pending"
            assert pending["summary"]["owner_warm"]["actual_fixture_context"] == 1
            if cleanup_fails:
                raise TimeoutError("final cleanup failed")
        broker.kill(pid, sig)

    monkeypatch.setattr(profiler.os, "kill", kill)

    def run(command, **kwargs):
        child_envs.append(kwargs["env"].copy())
        assert dict(os.environ) == kwargs["env"]
        if "setup" in command:
            output = json.dumps({"fixture_node_id": 1, "embedding_dimension": 384})
        elif "-c" in command:
            output = "## KB hits\n" + profiler.TITLE
        else:
            output = json.dumps({"first_python_tick": profiler.time.perf_counter(),
                                 "actual_fixture_context": True, "stages_ms": {},
                                 "retrieval_log": {}, "output": profiler.TITLE})
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("use broker election"))
    lock = SimpleNamespace(execute=lambda *_: None, rollback=lambda: None, close=lambda: None)
    import sqlite3
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: lock)
    # Eliminate the contention wait; SQL lock behavior is tested elsewhere.
    class Timer:
        def __init__(self, _, callback):
            self.callback = callback

        def start(self):
            self.callback()

        def join(self):
            pass

    monkeypatch.setattr(threading, "Timer", Timer)
    if cleanup_fails:
        with pytest.raises(TimeoutError, match="final cleanup failed"):
            profiler.parent(args)
        assert capsys.readouterr().out == ""
    else:
        profiler.parent(args)
        assert json.loads(capsys.readouterr().out)["output"].endswith("results.json")

    assert imported_with and child_envs
    expected = child_envs[0]
    assert all(env == expected for env in imported_with + child_envs)
    assert "LATCH_MCP_RUNTIME_KEY" not in expected
    assert "CLAUDE_KB_IN_MAINTENANCE" not in expected
    assert expected["LATCH_PROMPT_BUDGET_MS"] == "750"
    assert Path(expected["LATCH_KB_DIR"]).is_relative_to(Path(args.output_dir))
    assert dict(os.environ) == original
    stopped_count = 1 if cleanup_fails else 2
    assert broker.stopped == [4321] * stopped_count
    assert broker.removed == [
        (kind, {"pid": 4321, "token": f"fixture-{kind}-token"})
        for _ in range(stopped_count) for kind in ("mcp", "embed")
    ]
    report = json.loads((Path(args.output_dir) / "results.json").read_text())
    assert report["cleanup_status"] == ("failed" if cleanup_fails else "succeeded")
    assert report["summary"]["owner_warm"]["actual_fixture_context"] == 1
    assert report["groups"]["owner_warm"][0]["actual_fixture_context"]
    assert [row["pid"] for row in report["owner_readiness"]] == [4321, 4321]
    assert "fixture-mcp-token" not in json.dumps(report)
    assert "fixture-embed-token" not in json.dumps(report)


def test_failed_setup_restores_controller_environment(tmp_path, monkeypatch):
    args = args_for(tmp_path)
    monkeypatch.setenv("LATCH_MCP_RUNTIME_KEY", "original-runtime")
    original = os.environ.copy()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="setup failed"))
    with pytest.raises(RuntimeError, match="setup failed"):
        profiler.parent(args)
    assert dict(os.environ) == original


def test_sample_failure_waits_for_late_elected_owner_and_cleans_it(tmp_path, monkeypatch):
    broker = FixtureBroker(lambda: tmp_path)
    entered = threading.Event()
    publish = threading.Event()
    start = broker.ensure_daemon

    def delayed_start(*args, **kwargs):
        entered.set()
        assert publish.wait(timeout=2)
        return start(*args, **kwargs)

    monkeypatch.setattr(broker, "ensure_daemon", delayed_start)
    monkeypatch.setattr(profiler.os, "kill", broker.kill)
    with pytest.raises(ValueError, match="cold sample failed"):
        with profiler._FixtureOwner(broker, tmp_path, tmp_path):
            assert entered.wait(timeout=2)
            # Discovery is absent when sampling fails; election finishes in cleanup.
            assert broker.read_discovery() is None
            timer = threading.Timer(.01, publish.set)
            timer.start()
            raise ValueError("cold sample failed")
    timer.join()
    assert broker.stopped == [4321]
    assert broker.removed == [
        ("mcp", {"pid": 4321, "token": "fixture-mcp-token"}),
        ("embed", {"pid": 4321, "token": "fixture-embed-token"}),
    ]


def test_failed_election_still_cleans_published_fixture_winner(tmp_path, monkeypatch):
    broker = FixtureBroker(lambda: tmp_path)
    start = broker.ensure_daemon

    def failed_start(*args, **kwargs):
        start(*args, **kwargs)
        raise RuntimeError("broker wait failed after publish")

    monkeypatch.setattr(broker, "ensure_daemon", failed_start)
    monkeypatch.setattr(profiler.os, "kill", broker.kill)
    with pytest.raises(RuntimeError, match="broker wait failed"):
        with profiler._FixtureOwner(broker, tmp_path, tmp_path) as owner:
            owner.wait_ready()
    assert broker.stopped == [4321]


@pytest.mark.parametrize("invalid", ["wrong_vault", "unauthenticated"])
def test_cleanup_rejects_unverified_owner(tmp_path, monkeypatch, invalid):
    broker = FixtureBroker(lambda: tmp_path)
    broker.ensure_daemon(str(tmp_path), start_reason="prompt_hook")
    monkeypatch.setattr(profiler.os, "kill", lambda *a: pytest.fail("unverified owner termination"))
    vault = tmp_path / "different" if invalid == "wrong_vault" else tmp_path
    if invalid == "unauthenticated":
        monkeypatch.setattr(broker, "probe_discovery", lambda _: False)
    with pytest.raises(RuntimeError):
        profiler._stop_fixture_owner(broker, vault)
    assert broker.removed == []
