"""Unit tests for Codex rollout transcript discovery and flattening."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_transcript as ct  # noqa: E402
import compactor  # noqa: E402

SID = "019eb721-73b7-7302-8e74-357047339414"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-codex-home-"))


def _write_rollout(home: Path, sid: str = SID, *, payload_id: str | None = None) -> Path:
    p = home / "sessions" / "2026" / "06" / "11" / f"rollout-2026-06-11T07-41-23-{sid}.jsonl"
    p.parent.mkdir(parents=True)
    rows = [
        {
            "timestamp": "2026-06-11T14:42:01Z",
            "type": "session_meta",
            "payload": {
                "id": payload_id if payload_id is not None else sid,
                "cwd": "/repo",
                "originator": "Codex Desktop",
                "source": "vscode",
            },
        },
        {
            "timestamp": "2026-06-11T14:42:02Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "please wire compact"},
        },
        {
            "timestamp": "2026-06-11T14:42:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-exec",
                "name": "exec_command",
                "arguments": "{\"cmd\":\"git status\"}",
            },
        },
        {
            "timestamp": "2026-06-11T14:42:04Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        {
            "timestamp": "2026-06-11T14:42:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-exec",
                "output": json.dumps({
                    "agent_id": "not-a-subagent",
                    "status": {"ok": True},
                    "value": "regular tool JSON",
                }),
            },
        },
        {
            "timestamp": "2026-06-11T14:42:05Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-spawn",
                "name": "spawn_agent",
                "arguments": "{\"agent_type\":\"explorer\"}",
            },
        },
        {
            "timestamp": "2026-06-11T14:42:06Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-spawn",
                "output": json.dumps({
                    "agent_id": "agent-review-1",
                    "nickname": "Cicero",
                }),
            },
        },
        {
            "timestamp": "2026-06-11T14:42:07Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-wait",
                "name": "wait_agent",
                "arguments": "{\"targets\":[\"agent-review-1\"]}",
            },
        },
        {
            "timestamp": "2026-06-11T14:42:08Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-wait",
                "output": json.dumps({
                    "status": {
                        "agent-review-1": {
                            "completed": "No remaining blocker.",
                        },
                    },
                    "timed_out": True,
                }),
            },
        },
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return p


def test_resolve_session_id_prefers_explicit_then_env():
    old = os.environ.get("CODEX_THREAD_ID")
    try:
        os.environ["CODEX_THREAD_ID"] = "env-id"
        _assert(ct.resolve_session_id("arg-id") == "arg-id", "explicit id should win")
        _assert(ct.resolve_session_id(None) == "env-id", "env id should be used")
    finally:
        if old is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = old
    print("PASS resolve_session_id_prefers_explicit_then_env")


def test_resolve_session_id_fails_without_id():
    old = os.environ.get("CODEX_THREAD_ID")
    try:
        os.environ.pop("CODEX_THREAD_ID", None)
        try:
            ct.resolve_session_id(None)
        except ct.CodexTranscriptError as e:
            _assert("no Codex session id" in str(e), str(e))
        else:
            raise AssertionError("expected missing session id to fail")
    finally:
        if old is not None:
            os.environ["CODEX_THREAD_ID"] = old
    print("PASS resolve_session_id_fails_without_id")


def test_find_transcript_validates_session_meta():
    home = _tmp_home()
    try:
        p = _write_rollout(home)
        found = ct.find_transcript(SID, home=home)
        _assert(found == p, f"expected {p}, got {found}")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("PASS find_transcript_validates_session_meta")


def test_find_transcript_rejects_mismatched_session_meta():
    home = _tmp_home()
    try:
        _write_rollout(home, payload_id="different")
        try:
            ct.find_transcript(SID, home=home)
        except ct.CodexTranscriptError as e:
            _assert("none validated" in str(e), str(e))
        else:
            raise AssertionError("expected mismatched session_meta to fail")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("PASS find_transcript_rejects_mismatched_session_meta")


def test_read_transcript_flattens_codex_rollout():
    home = _tmp_home()
    try:
        p = _write_rollout(home)
        out = ct.read_transcript(p)
        _assert("[session_meta]" in out and f"id={SID}" in out, out)
        _assert("[user] please wire compact" in out, out)
        _assert("[tool_use exec_command]" in out, out)
        _assert("[tool_use spawn_agent]" in out, out)
        _assert("[tool_use wait_agent]" in out, out)
        _assert('"agent_id": "not-a-subagent"' in out, out)
        _assert("[agent_spawned] agent_id=not-a-subagent" not in out, out)
        _assert("[assistant] done" in out, out)
        _assert("[agent_spawned] agent_id=agent-review-1 nickname=Cicero" in out, out)
        _assert(
            "[agent_status] agent_id=agent-review-1 status=completed "
            "No remaining blocker. timed_out=True" in out,
            out,
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("PASS read_transcript_flattens_codex_rollout")


def test_compactor_dispatches_codex_rollout():
    home = _tmp_home()
    try:
        p = _write_rollout(home)
        out = compactor.read_transcript(p)
        _assert("[user] please wire compact" in out, out)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    print("PASS compactor_dispatches_codex_rollout")


if __name__ == "__main__":
    test_resolve_session_id_prefers_explicit_then_env()
    test_resolve_session_id_fails_without_id()
    test_find_transcript_validates_session_meta()
    test_find_transcript_rejects_mismatched_session_meta()
    test_read_transcript_flattens_codex_rollout()
    test_compactor_dispatches_codex_rollout()
    print("\nAll codex_transcript tests pass.")
