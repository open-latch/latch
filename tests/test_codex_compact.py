"""Unit tests for the Codex compaction entry point."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_compact as cc  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-codex-compact-"))


def test_spawn_background_detaches_child_without_recursive_flag():
    d = _tmp()
    old_ensure = cc.paths.ensure_project_dir
    old_popen = cc.subprocess.Popen
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProc()

    try:
        cc.paths.ensure_project_dir = lambda project: d
        cc.subprocess.Popen = fake_popen
        result = cc.spawn_background(
            session_id="sid",
            project="/repo",
            final=True,
            summarizer_backend="codex",
        )
        _assert(result["ok"] is True and result["background"] is True, result)
        _assert(result["pid"] == 4242, result)
        _assert(result["session_id"] == "sid", result)
        _assert(result["launch_id"], result)
        _assert(result["log_path"] == str(d / "codex_compact_background.log"), result)
        args = captured["args"]
        _assert("--background" not in args, args)
        _assert("--summarizer" in args and "codex" in args, args)
        _assert("--launch-id" in args and result["launch_id"] in args, args)
        _assert("--final" in args, args)
        _assert("stdout" in captured["kwargs"] and "stderr" in captured["kwargs"],
                captured["kwargs"])
        if os.name == "nt":
            _assert(captured["kwargs"].get("creationflags"), captured["kwargs"])
        else:
            _assert(captured["kwargs"].get("start_new_session") is True,
                    captured["kwargs"])
    finally:
        cc.paths.ensure_project_dir = old_ensure
        cc.subprocess.Popen = old_popen
        shutil.rmtree(d, ignore_errors=True)
    print("PASS spawn_background_detaches_child_without_recursive_flag")


def test_wait_for_background_result_reads_only_current_log_slice():
    d = _tmp()
    try:
        log_path = d / "codex_compact_background.log"
        log_path.write_text(
            json.dumps({"ok": True, "summary_node_id": 111}) + "\n",
            encoding="utf-8",
        )
        start_offset = log_path.stat().st_size
        with log_path.open("a", encoding="utf-8") as f:
            f.write("diagnostic text before json\n")
            f.write(json.dumps({"ok": True, "summary_node_id": 222}) + "\n")

        class FakeProc:
            def poll(self):
                return 0

        out = cc.wait_for_background_result(
            FakeProc(), log_path, start_offset, timeout_s=0.1, poll_interval_s=0.01,
        )
        _assert(out["ok"] is True, out)
        _assert(out["summary_node_id"] == 222, out)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS wait_for_background_result_reads_only_current_log_slice")


def test_wait_for_background_result_ignores_other_session_json():
    d = _tmp()
    try:
        log_path = d / "codex_compact_background.log"
        log_path.write_text("", encoding="utf-8")
        start_offset = log_path.stat().st_size
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ok": True,
                "session_id": "other-session",
                "launch_id": "other-launch",
                "summary_node_id": 700,
            }) + "\n")
            f.write(json.dumps({
                "ok": True,
                "session_id": "our-session",
                "launch_id": "our-launch",
                "summary_node_id": 676,
            }) + "\n")

        class FakeProc:
            def poll(self):
                return 0

        out = cc.wait_for_background_result(
            FakeProc(),
            log_path,
            start_offset,
            expected_session_id="our-session",
            expected_launch_id="our-launch",
            timeout_s=0.1,
            poll_interval_s=0.01,
        )
        _assert(out["ok"] is True, out)
        _assert(out["summary_node_id"] == 676, out)
        _assert(out["session_id"] == "our-session", out)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS wait_for_background_result_ignores_other_session_json")


def test_wait_for_background_result_reports_no_matching_json_on_child_exit():
    d = _tmp()
    try:
        log_path = d / "codex_compact_background.log"
        log_path.write_text(
            json.dumps({
                "ok": True,
                "session_id": "other-session",
                "launch_id": "other-launch",
                "summary_node_id": 700,
            }) + "\n",
            encoding="utf-8",
        )

        class FakeProc:
            def poll(self):
                return 0

        out = cc.wait_for_background_result(
            FakeProc(),
            log_path,
            0,
            expected_session_id="our-session",
            expected_launch_id="our-launch",
            timeout_s=0.1,
            poll_interval_s=0.01,
        )
        _assert(out["ok"] is False, out)
        _assert(out["reason"] == "background_no_matching_result", out)
        _assert(out["expected_session_id"] == "our-session", out)
        _assert(out["expected_launch_id"] == "our-launch", out)
        _assert(out["ignored_json"] == 1, out)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS wait_for_background_result_reports_no_matching_json_on_child_exit")


def test_wait_for_background_result_reports_no_json_on_child_exit():
    d = _tmp()
    try:
        log_path = d / "codex_compact_background.log"
        log_path.write_text("non-json child output\n", encoding="utf-8")

        class FakeProc:
            def poll(self):
                return 2

        out = cc.wait_for_background_result(
            FakeProc(), log_path, 0, timeout_s=0.1, poll_interval_s=0.01,
        )
        _assert(out["ok"] is False, out)
        _assert(out["reason"] == "background_no_result", out)
        _assert(out["exit_code"] == 2, out)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS wait_for_background_result_reports_no_json_on_child_exit")


if __name__ == "__main__":
    test_spawn_background_detaches_child_without_recursive_flag()
    test_wait_for_background_result_reads_only_current_log_slice()
    test_wait_for_background_result_ignores_other_session_json()
    test_wait_for_background_result_reports_no_matching_json_on_child_exit()
    test_wait_for_background_result_reports_no_json_on_child_exit()
    print("\nAll codex_compact tests pass.")
