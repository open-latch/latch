"""Unit tests for merge-safe Cursor hooks wiring."""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import cursor_hooks as ch  # noqa: E402

HOOK_ARGS = (
    "/repo/src/hooks/cursor_session_start.py",
    "/repo/src/hooks/cursor_before_submit.py",
    "/repo/src/hooks/cursor_pre_tool_use.py",
    "/repo/src/hooks/cursor_post_tool_use.py",
)


def test_merge_preserves_unrelated_hooks_and_is_idempotent():
    existing = json.dumps({
        "version": 1,
        "projectSetting": True,
        "hooks": {
            "sessionStart": [{"command": "node user-start.js", "timeout": 2}],
            "afterFileEdit": [{"command": "node format.js"}],
        },
    }, indent=2) + "\n"
    new, changes = ch.merge_hooks(existing, "/py", *HOOK_ARGS)
    obj = json.loads(new)
    assert changes
    assert obj["projectSetting"] is True
    assert obj["hooks"]["sessionStart"][1]["command"] == "node user-start.js"
    assert obj["hooks"]["afterFileEdit"] == [{"command": "node format.js"}]
    assert "matcher" not in obj["hooks"]["postToolUse"][0]
    assert obj["hooks"]["beforeSubmitPrompt"][0]["failClosed"] is True
    assert obj["hooks"]["preToolUse"][0]["failClosed"] is True
    new2, changes2 = ch.merge_hooks(new, "/py", *HOOK_ARGS)
    assert new2 == new
    assert changes2 == []


def test_merge_replaces_stale_owned_entries_across_events():
    existing = json.dumps({
        "version": 1,
        "hooks": {
            "stop": [{"command": "/old/src/hooks/cursor_session_start.py"}],
            "postToolUse": [
                {"command": "/old/src/hooks/cursor_post_tool_use.py"},
                {"command": "user-hook"},
            ],
        },
    })
    new_args = tuple(path.replace("/repo/", "/new/") for path in HOOK_ARGS)
    new, changes = ch.merge_hooks(existing, "/py", *new_args)
    obj = json.loads(new)
    assert "stop" not in obj["hooks"]
    assert obj["hooks"]["postToolUse"][1] == {"command": "user-hook"}
    assert any("removed 2 stale" in change for change in changes)


def test_write_status_and_remove_preserve_user_hooks():
    root = Path(tempfile.mkdtemp(prefix="cursor-hooks-"))
    try:
        path = root / ".cursor" / "hooks.json"
        desired, _ = ch.merge_hooks(
            json.dumps({"version": 1, "hooks": {"stop": [{"command": "user-stop"}]}}),
            "/py", *HOOK_ARGS,
            path=path,
        )
        ch.write_hooks(path, desired)
        ok, detail = ch.hooks_status(path, "/py", *HOOK_ARGS)
        assert ok, detail
        changes = ch.remove_hooks(path)
        assert changes
        remaining = json.loads(path.read_text(encoding="utf-8"))
        assert remaining["hooks"] == {"stop": [{"command": "user-stop"}]}
        assert (path.parent / "hooks.json.latchbak").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rejects_malformed_or_unknown_version():
    for body in ("{bad", '{"version": 2}'):
        try:
            ch.merge_hooks(body, "/py", *HOOK_ARGS)
        except SystemExit:
            pass
        else:
            raise AssertionError(f"expected rejection for {body}")
