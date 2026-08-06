"""Unit tests for the Codex compaction entry point."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("compatibility_scope_env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import codex_compact as cc  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="latch-codex-compact-"))


def test_spawn_background_detaches_child_without_recursive_flag():
    root = _tmp()
    project = root / "project"
    project.mkdir()
    (project / ".git").mkdir()
    cc.project_config.record_session_binding(project, "sid")
    kb_dir = cc.paths.project_dir(project)
    old_popen = cc.subprocess.Popen
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        try:
            with cc.lockfile.project_access_lock(str(project), exclusive=True):
                pass
        except RuntimeError as exc:
            captured["lease_held"] = "cannot upgrade" in str(exc)
        return FakeProc()

    try:
        cc.subprocess.Popen = fake_popen
        result = cc.spawn_background(
            session_id="sid",
            project=str(project),
            final=True,
            summarizer_backend="codex",
        )
        _assert(result["ok"] is True and result["background"] is True, result)
        _assert(result["pid"] == 4242, result)
        _assert(result["session_id"] == "sid", result)
        _assert(result["launch_id"], result)
        _assert(
            result["log_path"]
            == str(kb_dir / "codex_compact_background.log"),
            result,
        )
        _assert(captured.get("lease_held") is True, captured)
        args = captured["args"]
        _assert("--background" not in args, args)
        _assert("--summarizer" in args and "codex" in args, args)
        _assert("--launch-id" in args and result["launch_id"] in args, args)
        _assert(
            "--binding-revision" in args
            and cc.project_config.resolve(project).revision in args,
            args,
        )
        _assert(
            "--kb-dir" in args and str(Path(result["log_path"]).parent) in args,
            args,
        )
        _assert("--final" in args, args)
        _assert("stdout" in captured["kwargs"] and "stderr" in captured["kwargs"],
                captured["kwargs"])
        if os.name == "nt":
            _assert(captured["kwargs"].get("creationflags"), captured["kwargs"])
        else:
            _assert(captured["kwargs"].get("start_new_session") is True,
                    captured["kwargs"])
    finally:
        cc.subprocess.Popen = old_popen
        shutil.rmtree(root, ignore_errors=True)
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


def _bound_project(prefix: str, *, session_id: str = "sid"):
    root = _tmp()
    project = root / "project"
    project.mkdir()
    (project / ".git").mkdir()
    test_root = cc.paths.validated_test_root()
    vault_root = (test_root / "vaults") if test_root is not None else root
    kb_a = vault_root / f"{prefix}-a-{root.name}"
    kb_b = vault_root / f"{prefix}-b-{root.name}"
    kb_a.mkdir(parents=True)
    kb_b.mkdir(parents=True)
    cc.project_config.mark_kb_target(kb_a)
    cc.project_config.mark_kb_target(kb_b)
    binding = cc.project_config.write_binding(
        project,
        mode=cc.project_config.MODE_LATCHED,
        kb_dir=kb_a,
    )
    cc.project_config.record_session_binding(project, session_id)
    return root, project, kb_a, kb_b, binding


def test_stale_task_cannot_log_or_spawn_after_repin_or_unlatch():
    for transition in ("repin", "unlatch"):
        sid = f"sid-{transition}"
        root, project, kb_a, kb_b, _binding_a = _bound_project(
            f"compact-stale-{transition}",
            session_id=sid,
        )
        old_popen = cc.subprocess.Popen
        spawned = []
        try:
            if transition == "repin":
                cc.project_config.repin_private_scope(project, kb_b)
            else:
                cc.project_config.set_scope_mode(
                    project,
                    cc.project_config.MODE_UNLATCHED,
                )
            cc.subprocess.Popen = lambda *_args, **_kwargs: spawned.append(True)
            try:
                cc.spawn_background(
                    session_id=sid,
                    project=str(project),
                    final=False,
                    summarizer_backend="codex",
                )
            except cc.lockfile.ProjectTargetChangedError as exc:
                _assert(
                    exc.reason in {"stale_session_binding", "unlatched"},
                    exc.reason,
                )
            else:
                raise AssertionError("stale task must not launch a compactor")
            _assert(spawned == [], spawned)
            _assert(not (kb_a / "codex_compact_background.log").exists(), kb_a)
            _assert(not (kb_b / "codex_compact_background.log").exists(), kb_b)
        finally:
            cc.subprocess.Popen = old_popen
            shutil.rmtree(root, ignore_errors=True)
    print("PASS stale_task_cannot_log_or_spawn_after_repin_or_unlatch")


def test_background_child_rejects_parent_snapshot_after_repin(capsys, monkeypatch):
    root, project, kb_a, kb_b, binding_a = _bound_project("compact-child")
    captured: dict = {}

    class FakeProc:
        pid = 4242

    def fake_popen(args, **_kwargs):
        captured["args"] = args
        return FakeProc()

    try:
        monkeypatch.setattr(cc.subprocess, "Popen", fake_popen)
        cc.spawn_background(
            session_id="sid",
            project=str(project),
            final=False,
            summarizer_backend="codex",
        )
        child_args = captured["args"][2:]
        _assert(binding_a.revision in child_args, child_args)
        _assert(str(kb_a) in child_args, child_args)

        transcript = project / "rollout.jsonl"
        transcript.write_text("", encoding="utf-8")
        monkeypatch.setattr(
            cc.codex_transcript, "resolve_session_id", lambda value: value,
        )
        monkeypatch.setattr(
            cc.codex_transcript, "find_transcript", lambda _sid: transcript,
        )
        compacted = []

        def fake_compact(session_id, project_path, transcript_path, **kwargs):
            compacted.append({
                "session_id": session_id,
                "project_path": project_path,
                "transcript_path": transcript_path,
                "inner_session": os.environ.get("LATCH_SESSION_ID"),
                **kwargs,
            })
            return {"ok": True, "summary_node_id": 42}

        monkeypatch.setattr(cc.compactor, "run_compaction", fake_compact)

        prior_session = os.environ.get("LATCH_SESSION_ID")
        current = cc.main(child_args)
        current_payload = json.loads(capsys.readouterr().out)
        _assert(current == 0 and current_payload["ok"] is True, current_payload)
        _assert(compacted[0]["binding_revision"] == binding_a.revision, compacted)
        _assert(compacted[0]["expected_kb_dir"] == str(kb_a), compacted)
        _assert(compacted[0]["inner_session"] == prior_session, compacted)
        _assert(os.environ.get("LATCH_SESSION_ID") == prior_session, os.environ)
        compacted.clear()

        wrong_kb_args = list(child_args)
        kb_index = wrong_kb_args.index("--kb-dir") + 1
        wrong_kb_args[kb_index] = str(kb_b)
        wrong_kb = cc.main(wrong_kb_args)
        _assert(wrong_kb == 1, wrong_kb)
        _assert("project KB changed" in capsys.readouterr().err, "missing error")
        _assert(compacted == [], compacted)

        cc.project_config.repin_private_scope(project, kb_b)
        rc = cc.main(child_args)
        _assert(rc == 1, rc)
        _assert("older project KB binding" in capsys.readouterr().err, "missing error")
        _assert(compacted == [], compacted)
        _assert(not (kb_b / "codex_compact_background.log").exists(), kb_b)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS background_child_rejects_parent_snapshot_after_repin")


if __name__ == "__main__":
    test_spawn_background_detaches_child_without_recursive_flag()
    test_wait_for_background_result_reads_only_current_log_slice()
    test_wait_for_background_result_ignores_other_session_json()
    test_wait_for_background_result_reports_no_matching_json_on_child_exit()
    test_wait_for_background_result_reports_no_json_on_child_exit()
    test_stale_task_cannot_log_or_spawn_after_repin_or_unlatch()
    print("\nAll codex_compact tests pass.")
