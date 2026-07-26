from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import update_latch  # noqa: E402


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args], text=True, stdout=subprocess.PIPE, check=True)
    return result.stdout.strip()


def test_remote_and_tag_parsing():
    assert update_latch.official_remote("git@github.com:open-latch/latch.git")
    assert update_latch.official_remote("https://github.com/open-latch/latch")
    assert not update_latch.official_remote("https://github.com/example/latch")
    refs = "a\trefs/tags/v0.1.0\nb\trefs/tags/v0.2.0\nc\trefs/tags/v0.3.0-beta\n"
    assert update_latch.latest_remote_tag(refs) == "v0.2.0"


def test_latest_release_selection_ignores_prerelease_and_nonstable_tags():
    payload = [
        {"tag_name": "v0.3.0-beta.1", "draft": False, "prerelease": False},
        {"tag_name": "v0.2.1", "draft": False, "prerelease": True},
        {"tag_name": "v0.1.0", "draft": False, "prerelease": False},
        {"tag_name": "v0.2.0", "draft": False, "prerelease": False},
    ]
    assert update_latch._stable_release_from_payload(payload, requested=None) == "v0.2.0"


def test_clean_release_checkout_updates_to_exact_tag(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    work = tmp_path / "source"
    clone = tmp_path / "install"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "init", "-b", "main", str(work)], check=True, stdout=subprocess.DEVNULL)
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (work / "KB_SCHEMA_VERSION").write_text("3\n", encoding="utf-8")
    (work / "requirements.txt").write_text("", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "v1.0.0")
    _git(work, "tag", "v1.0.0")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main", "--tags")
    (work / "VERSION").write_text("1.0.1\n", encoding="utf-8")
    _git(work, "add", "VERSION")
    _git(work, "commit", "-m", "v1.0.1")
    _git(work, "tag", "v1.0.1")
    _git(work, "push", "origin", "main", "--tags")
    subprocess.run(["git", "clone", "--branch", "v1.0.0", str(remote), str(clone)], check=True, stdout=subprocess.DEVNULL)

    monkeypatch.setattr(update_latch, "ROOT", clone)
    monkeypatch.setattr(update_latch, "official_remote", lambda _url: True)
    monkeypatch.setattr(update_latch, "published_release_tag", lambda tag=None: tag or "v1.0.1")
    monkeypatch.setattr(update_latch, "_dependency_command", lambda: ["true"])
    monkeypatch.setattr(update_latch, "_refresh_claude_commands_if_installed", lambda: False)
    info = update_latch.inspect()
    assert info["latest_tag"] == "v1.0.1"
    result = update_latch.apply_update("v1.0.1", dry_run=False)
    assert result["to_version"] == "1.0.1"
    assert _git(clone, "describe", "--tags", "--exact-match", "HEAD") == "v1.0.1"


def test_update_refuses_dirty_or_developer_branch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, stdout=subprocess.DEVNULL)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (repo / "KB_SCHEMA_VERSION").write_text("1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "tag", "v0.1.0")
    _git(repo, "switch", "-c", "feature")
    monkeypatch.setattr(update_latch, "ROOT", repo)
    with pytest.raises(update_latch.UpdateError, match="developer branch"):
        update_latch.apply_update("v0.1.0", dry_run=True)


def test_update_refuses_arbitrary_detached_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, stdout=subprocess.DEVNULL)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (repo / "KB_SCHEMA_VERSION").write_text("1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "switch", "--detach")
    monkeypatch.setattr(update_latch, "ROOT", repo)
    with pytest.raises(update_latch.UpdateError, match="arbitrary detached checkout"):
        update_latch.apply_update("v0.1.0", dry_run=True)


def test_command_refresh_preserves_same_named_user_file(tmp_path, monkeypatch):
    commands = tmp_path / "commands"
    commands.mkdir()
    managed = commands / "latch-gate.md"
    managed.write_text(f"run {update_latch.ROOT}/bin/run_latch_gate.sh\n", encoding="utf-8")
    user_owned = commands / "latch-compact.md"
    user_owned.write_text("my unrelated command\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_COMMANDS_DIR", str(commands))

    assert update_latch._refresh_claude_commands_if_installed()
    assert "my unrelated command" == user_owned.read_text(encoding="utf-8").strip()
    assert "<KB_HOME>" not in managed.read_text(encoding="utf-8")


def test_update_refuses_kb_newer_than_target_before_backup_or_source_change(tmp_path, monkeypatch):
    kb = tmp_path / "kb.db"
    conn = sqlite3.connect(kb)
    conn.execute("CREATE TABLE latch_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO latch_meta VALUES('kb_schema_version', '4')")
    conn.commit()
    conn.close()
    before = kb.read_bytes()

    git_calls: list[tuple[str, ...]] = []

    def fake_git(*args: str, check: bool = True) -> str:
        git_calls.append(args)
        if args[:3] == ("symbolic-ref", "--quiet", "--short"):
            return "main"
        if args[:2] == ("status", "--porcelain"):
            return ""
        if args[:2] == ("show", "v1.0.0:VERSION"):
            return "1.0.0"
        if args[:2] == ("show", "v1.0.0:KB_SCHEMA_VERSION"):
            return "3"
        return ""

    monkeypatch.setattr(update_latch, "_git", fake_git)
    monkeypatch.setattr(update_latch, "published_release_tag", lambda tag=None: tag)
    monkeypatch.setattr(update_latch, "discover_kbs", lambda: [kb])
    monkeypatch.setattr(
        update_latch.schema_version,
        "backup_database",
        lambda *_args, **_kwargs: pytest.fail("backup must not run after incompatible preflight"),
    )

    with pytest.raises(update_latch.UpdateError, match="already uses newer schema 4"):
        update_latch.apply_update("v1.0.0", dry_run=False)
    assert not any(call and call[0] == "switch" for call in git_calls)
    assert kb.read_bytes() == before
    assert list(tmp_path.glob("*.bak.*")) == []
