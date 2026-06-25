"""Regression tests for bin/run_compact_now.sh transcript selection (KB id=1523).

The bug: the wrapper picked the newest-mtime transcript across ALL projects,
so a concurrent session's transcript silently got compacted instead of the
invoking session's. The fix targets the invoking session explicitly via the
first positional arg or $CLAUDE_CODE_SESSION_ID, falling back to the mtime
heuristic (with a loud stderr warning) only when neither is set.

We stub the compactor by pointing the legacy CLAUDE_KB_PYTHON alias at `echo`,
so the exec line prints `<compactor.py> <session_id> <project_dir>
<transcript>` instead of running a real compaction. Requires bash on PATH (Git
Bash on Windows).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

KB_HOME = Path(__file__).resolve().parent.parent
SCRIPT = KB_HOME / "bin" / "run_compact_now.sh"

SID_OURS = "11111111-aaaa-bbbb-cccc-000000000001"
SID_CONCURRENT = "22222222-aaaa-bbbb-cccc-000000000002"


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _to_bash_path(p: Path) -> str:
    # Git Bash understands C:/... paths for $HOME; normalize backslashes.
    return str(p).replace("\\", "/")


def _fake_home() -> tuple[Path, Path, Path]:
    """Build <home>/.claude/projects with two project dirs, one transcript
    each. The CONCURRENT session's transcript is strictly newer."""
    home = Path(tempfile.mkdtemp(prefix="kb_compact_now_test_"))
    proj_a = home / ".claude" / "projects" / "c--proj-ours"
    proj_b = home / ".claude" / "projects" / "c--proj-concurrent"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)
    ours = proj_a / f"{SID_OURS}.jsonl"
    theirs = proj_b / f"{SID_CONCURRENT}.jsonl"
    ours.write_text("{}\n", encoding="utf-8")
    theirs.write_text("{}\n", encoding="utf-8")
    now = time.time()
    os.utime(ours, (now - 600, now - 600))
    os.utime(theirs, (now, now))  # concurrent session touched more recently
    return home, ours, theirs


def _run(home: Path, *, session_env: str | None, args: list[str] = []):
    env = dict(os.environ)
    env["HOME"] = _to_bash_path(home)
    env.pop("LATCH_PYTHON", None)
    env["CLAUDE_KB_PYTHON"] = "echo"
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    if session_env is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_env
    return subprocess.run(
        ["bash", _to_bash_path(SCRIPT), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_env_session_id_beats_newer_concurrent_transcript():
    home, ours, theirs = _fake_home()
    try:
        r = _run(home, session_env=SID_OURS)
        _assert(r.returncode == 0, f"exit {r.returncode}: {r.stderr}")
        _assert(SID_OURS in r.stdout,
                f"expected our session id in compactor args, got: {r.stdout}")
        _assert(SID_CONCURRENT not in r.stdout,
                f"concurrent session leaked into compactor args: {r.stdout}")
        _assert(ours.name in r.stdout,
                f"expected our transcript path, got: {r.stdout}")
        print("PASS env_session_id_beats_newer_concurrent_transcript")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_positional_arg_beats_env():
    home, ours, theirs = _fake_home()
    try:
        r = _run(home, session_env=SID_CONCURRENT, args=[SID_OURS])
        _assert(r.returncode == 0, f"exit {r.returncode}: {r.stderr}")
        _assert(SID_OURS in r.stdout and SID_CONCURRENT not in r.stdout,
                f"positional arg should win over env, got: {r.stdout}")
        print("PASS positional_arg_beats_env")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_missing_transcript_for_session_id_fails_loud():
    home, _, _ = _fake_home()
    try:
        r = _run(home, session_env="99999999-dead-beef-0000-000000000009")
        _assert(r.returncode != 0,
                "should exit nonzero when the session's transcript is absent")
        _assert("no transcript found for session" in r.stderr,
                f"expected explicit error, got: {r.stderr}")
        print("PASS missing_transcript_for_session_id_fails_loud")
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_no_session_id_falls_back_to_mtime_with_warning():
    home, ours, theirs = _fake_home()
    try:
        r = _run(home, session_env=None)
        _assert(r.returncode == 0, f"exit {r.returncode}: {r.stderr}")
        _assert(SID_CONCURRENT in r.stdout,
                f"fallback should pick newest-mtime transcript, got: {r.stdout}")
        _assert("WARNING" in r.stderr,
                f"fallback must warn on stderr, got: {r.stderr}")
        print("PASS no_session_id_falls_back_to_mtime_with_warning")
    finally:
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    if shutil.which("bash") is None:
        print("SKIP: bash not on PATH")
        sys.exit(0)
    test_env_session_id_beats_newer_concurrent_transcript()
    test_positional_arg_beats_env()
    test_missing_transcript_for_session_id_fails_loud()
    test_no_session_id_falls_back_to_mtime_with_warning()
    print("\nAll run_compact_now tests pass.")
