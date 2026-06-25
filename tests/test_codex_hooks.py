"""Unit tests for Codex hooks.json merging."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_hooks as ch  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-codex-hooks-"))


def test_merge_hooks_installs_session_start_only_and_preserves_unrelated():
    existing = json.dumps({
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {"type": "command", "command": "/old/python /repo/src/hooks/session_start.py"},
                        {"type": "command", "command": "/user/custom-start"},
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": "/old/python /repo/src/hooks/stop.py"},
                        {"type": "command", "command": "/user/custom-stop"},
                    ]
                }
            ],
        }
    }, indent=2) + "\n"
    new, changes = ch.merge_hooks(existing, "/py", "/repo/src/hooks/codex_session_start.py")
    _assert(changes, "merge should report changes")
    obj = json.loads(new)
    starts = obj["hooks"]["SessionStart"]
    _assert(starts[0]["hooks"][0]["command"] == "/py /repo/src/hooks/codex_session_start.py",
            starts)
    _assert("/user/custom-start" in json.dumps(obj), obj)
    _assert("/user/custom-stop" in json.dumps(obj), obj)
    _assert("src/hooks/stop.py" not in json.dumps(obj), obj)
    _assert("src/hooks/session_start.py" not in json.dumps(obj), obj)
    print("PASS merge_hooks_installs_session_start_only_and_preserves_unrelated")


def test_merge_hooks_idempotent():
    new1, changes1 = ch.merge_hooks("", "/py", "/repo/src/hooks/codex_session_start.py")
    _assert(changes1, "first merge should change")
    new2, changes2 = ch.merge_hooks(new1, "/py", "/repo/src/hooks/codex_session_start.py")
    _assert(new2 == new1, "second merge should be byte-identical")
    _assert(changes2 == [], f"second merge should report no changes, got {changes2}")
    print("PASS merge_hooks_idempotent")


def test_hooks_status_and_backup():
    d = _tmp()
    try:
        hooks = d / "hooks.json"
        desired, _ = ch.merge_hooks("", "/py", "/repo/src/hooks/codex_session_start.py")
        ch.write_hooks(hooks, desired)
        ok, detail = ch.hooks_status(hooks, "/py", "/repo/src/hooks/codex_session_start.py")
        _assert(ok, detail)
        ch.write_hooks(hooks, desired)
        _assert((d / "hooks.json.latchbak").exists(), "backup should exist")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS hooks_status_and_backup")


if __name__ == "__main__":
    test_merge_hooks_installs_session_start_only_and_preserves_unrelated()
    test_merge_hooks_idempotent()
    test_hooks_status_and_backup()
    print("\nAll codex_hooks tests pass.")
