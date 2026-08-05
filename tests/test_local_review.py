"""Tests for the local subscription-backed review orchestrator."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import local_review  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Review Test")
    _git(repo, "config", "user.email", "review@example.test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "source.py").write_text("answer = 42\n", encoding="utf-8")
    _git(repo, "add", "source.py")
    _git(repo, "commit", "-m", "head")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _stub_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _provider_runtime(
    claude_executable: str | Path,
    codex_executable: str | Path,
) -> local_review.ProviderRuntime:
    return local_review.ProviderRuntime(
        authentication={"claude": "claude.ai/max", "codex": "ChatGPT"},
        claude_executable=str(claude_executable),
        claude_version="2.1.220 (Claude Code)",
        codex_executable=str(codex_executable),
        codex_version="codex-cli 0.146.0-alpha.9.2",
    )


def test_preflight_rejects_api_key_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-used")
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("preflight must stop before a subprocess")

    monkeypatch.setattr(local_review, "_run", forbidden)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        local_review.preflight_auth(tmp_path)
    assert called is False


def test_preflight_rejects_provider_endpoint_override_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(
        local_review,
        "_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preflight must stop before a subprocess")
        ),
    )
    with pytest.raises(ValueError, match="OPENAI_BASE_URL"):
        local_review.preflight_auth(tmp_path)


def test_preflight_accepts_subscription_logins_and_scrubs_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name in local_review.BLOCKED_PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    tools = {
        name: _stub_executable(tmp_path / name)
        for name in ("claude", "codex", "gh", "git")
    }
    monkeypatch.setenv("CLAUDE_BIN", str(tools["claude"]))
    monkeypatch.setenv("CODEX_BIN", str(tools["codex"]))
    monkeypatch.setattr(
        local_review.shutil,
        "which",
        lambda name: str(tools[name]) if name in ("gh", "git") else None,
    )
    environments: list[dict[str, str]] = []

    def fake_run(command, *, environment=None, **_kwargs):
        environments.append(environment)
        provider = Path(command[0]).name
        if command[1:] == ["--version"]:
            stdout = (
                "2.1.220 (Claude Code)\n"
                if provider == "claude"
                else "codex-cli 0.146.0-alpha.9.2\n"
            )
        elif provider == "codex" and command[1:] == [
            "debug", "models", "--bundled"
        ]:
            stdout = json.dumps(
                {
                    "models": [
                        {
                            "slug": local_review.CODEX_MODEL,
                            "supported_reasoning_levels": [
                                {"effort": local_review.CODEX_EFFORT}
                            ],
                        }
                    ]
                }
            )
        elif provider == "claude" and command[1:4] == ["auth", "status", "--json"]:
            stdout = json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                    "subscriptionType": "max",
                }
            )
        elif provider == "codex" and command[1:3] == ["login", "status"]:
            stdout = "Logged in using ChatGPT\n"
        else:
            raise AssertionError(f"unexpected preflight command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(local_review, "_run", fake_run)
    runtime = local_review.preflight_auth(tmp_path)
    assert runtime.authentication == {
        "claude": "claude.ai/max",
        "codex": "ChatGPT",
    }
    assert runtime.claude_executable == str(tools["claude"].resolve())
    assert runtime.claude_version == "2.1.220 (Claude Code)"
    assert runtime.codex_executable == str(tools["codex"].resolve())
    assert runtime.codex_version == "codex-cli 0.146.0-alpha.9.2"
    assert all(
        name not in environment
        for environment in environments
        for name in (
            *local_review.BLOCKED_PROVIDER_ENV_VARS,
            *local_review.PROVIDER_EXECUTABLE_ENV_VARS.values(),
        )
    )


def test_provider_executable_override_must_be_absolute_and_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CODEX_BIN", "codex")
    with pytest.raises(ValueError, match="CODEX_BIN must be an absolute"):
        local_review._resolve_provider_executable("codex")
    monkeypatch.setenv("CODEX_BIN", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="does not resolve to an executable"):
        local_review._resolve_provider_executable("codex")
    non_executable = tmp_path / "not-executable"
    non_executable.write_text("fixture\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_BIN", str(non_executable))
    with pytest.raises(ValueError, match="does not resolve to an executable"):
        local_review._resolve_provider_executable("codex")


@pytest.mark.parametrize(
    ("catalog", "message"),
    [
        ("not json", "unreadable bundled model catalog"),
        (json.dumps({"models": []}), "does not bundle gpt-5.6-sol"),
        (
            json.dumps(
                {
                    "models": [
                        {
                            "slug": "gpt-5.6-sol",
                            "supported_reasoning_levels": [{"effort": "medium"}],
                        }
                    ]
                }
            ),
            "does not bundle gpt-5.6-sol with effort high",
        ),
    ],
)
def test_codex_capability_preflight_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    catalog: str,
    message: str,
):
    executable = _stub_executable(tmp_path / "codex")
    monkeypatch.setattr(
        local_review,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, catalog, ""
        ),
    )
    with pytest.raises(ValueError, match=message):
        local_review._require_codex_model_capability(
            str(executable), "codex-cli fixture", tmp_path, {}
        )


def test_incompatible_codex_stops_before_scope_or_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name in local_review.BLOCKED_PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    tools = {
        name: _stub_executable(tmp_path / name)
        for name in ("claude", "codex", "gh", "git")
    }
    monkeypatch.setenv("CLAUDE_BIN", str(tools["claude"]))
    monkeypatch.setenv("CODEX_BIN", str(tools["codex"]))
    monkeypatch.setattr(
        local_review.shutil,
        "which",
        lambda name: str(tools[name]) if name in ("gh", "git") else None,
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[1:] == ["--version"]:
            version = (
                "2.1.220 (Claude Code)"
                if Path(command[0]).name == "claude"
                else "codex-cli 0.142.5"
            )
            return subprocess.CompletedProcess(command, 0, version, "")
        if command[1:] == ["debug", "models", "--bundled"]:
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"models": []}), ""
            )
        raise AssertionError(f"authentication or lane command ran: {command}")

    monkeypatch.setattr(local_review, "_run", fake_run)
    monkeypatch.setattr(local_review, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(
        local_review,
        "resolve_scope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("scope resolution ran")
        ),
    )
    args = argparse.Namespace(pr=75, range=None, commit=None, repo="", post_pr=False)
    with pytest.raises(ValueError, match="does not bundle gpt-5.6-sol"):
        local_review.run_review(args)
    assert commands[-1][1:] == ["debug", "models", "--bundled"]


def test_range_and_commit_scope_are_immutable(tmp_path: Path):
    repo, base, head = _repository(tmp_path)
    range_scope = local_review._resolve_range(repo, f"{base}...{head}", "")
    assert range_scope.base_sha == base
    assert range_scope.head_sha == head
    commit_scope = local_review._resolve_commit(repo, "HEAD", "")
    assert commit_scope.base_sha == base
    assert commit_scope.head_sha == head


def test_root_commit_uses_the_empty_tree(tmp_path: Path):
    repo = tmp_path / "root-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Review Test")
    _git(repo, "config", "user.email", "review@example.test")
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "root")
    scope = local_review._resolve_commit(repo, "HEAD", "")
    assert scope.base_sha == local_review.EMPTY_TREE_SHA
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = local_review._prepare_object_store(repo, workspace, scope)
    assert local_review._artifact_review_needed(
        workspace,
        git_dir,
        scope,
        {"user_facing_paths": ["README.md"]},
    )


def test_commit_scope_fails_closed_when_shallow_clone_lacks_parent(tmp_path: Path):
    source, _base, _head = _repository(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=1", f"file://{source}", str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )
    with pytest.raises(ValueError, match="first parent is missing"):
        local_review._resolve_commit(shallow, "HEAD", "")


def test_pr_scope_uses_fresh_full_ancestry_for_shallow_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    base_repo = tmp_path / "base"
    base_repo.mkdir()
    _git(base_repo, "init")
    _git(base_repo, "config", "user.name", "Review Test")
    _git(base_repo, "config", "user.email", "review@example.test")
    (base_repo / "shared.txt").write_text("shared\n", encoding="utf-8")
    _git(base_repo, "add", "shared.txt")
    _git(base_repo, "commit", "-m", "shared base")
    merge_base = _git(base_repo, "rev-parse", "HEAD")

    fork_repo = tmp_path / "fork"
    subprocess.run(
        ["git", "clone", str(base_repo), str(fork_repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(fork_repo, "config", "user.name", "Review Test")
    _git(fork_repo, "config", "user.email", "review@example.test")
    (fork_repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(fork_repo, "add", "feature.txt")
    _git(fork_repo, "commit", "-m", "feature")
    head_sha = _git(fork_repo, "rev-parse", "HEAD")

    (base_repo / "base-only.txt").write_text("advanced\n", encoding="utf-8")
    _git(base_repo, "add", "base-only.txt")
    _git(base_repo, "commit", "-m", "advanced base")
    base_tip = _git(base_repo, "rev-parse", "HEAD")
    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--depth=1", f"file://{base_repo}", str(shallow)],
        check=True,
        capture_output=True,
        text=True,
    )

    original_run = local_review._run

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "pr", "view"]:
            payload = {
                "number": 73,
                "baseRefOid": base_tip,
                "headRefOid": head_sha,
                "headRepository": {"nameWithOwner": "owner/fork"},
                "url": "https://github.com/owner/base/pull/73",
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return original_run(command, **kwargs)

    remotes = {"owner/base": base_repo, "owner/fork": fork_repo}

    def local_fetch(workspace, git_dir, repository, commit):
        local_review._bare_git(
            workspace, git_dir, "fetch", "--no-tags", str(remotes[repository]), commit
        )
        assert (
            local_review._bare_git(
                workspace, git_dir, "rev-parse", "FETCH_HEAD^{commit}"
            ).stdout.strip()
            == commit
        )

    monkeypatch.setattr(local_review, "_run", fake_run)
    monkeypatch.setattr(local_review, "_fetch_pr_commit", local_fetch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scope = local_review._resolve_pr(shallow, 73, "owner/base", workspace)
    assert scope.base_sha == merge_base
    assert scope.head_sha == head_sha
    changed = local_review._bare_git(
        workspace,
        workspace / "review-target",
        "diff",
        "--name-only",
        scope.base_sha,
        scope.head_sha,
    ).stdout.splitlines()
    assert changed == ["feature.txt"]


def test_provider_commands_pin_models_and_remove_agent_tools(tmp_path: Path):
    raw = tmp_path / "raw"
    raw.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review", encoding="utf-8")
    claude = local_review.Lane(
        "claude", "security-abuse", prompt, raw / "claude.json", raw
    )
    runtime = _provider_runtime("/trusted/claude", "/trusted/codex")
    claude_command = local_review._provider_command(claude, tmp_path, runtime)
    assert claude_command[0] == runtime.claude_executable
    assert local_review.CLAUDE_MODEL in claude_command
    assert claude_command[claude_command.index("--effort") + 1] == "high"
    assert "--safe-mode" in claude_command
    assert "--tools" in claude_command
    assert claude_command[claude_command.index("--tools") + 1] == ""
    assert "*" in claude_command
    assert claude_command.count("--mcp-config") == 1
    mcp_config = claude_command[claude_command.index("--mcp-config") + 1]
    assert json.loads(mcp_config) == {"mcpServers": {}}

    codex = local_review.Lane(
        "codex", "simplicity-consolidation", prompt, raw / "codex.json", raw
    )
    codex_command = local_review._provider_command(codex, tmp_path, runtime)
    assert codex_command[0] == runtime.codex_executable
    assert local_review.CODEX_MODEL in codex_command
    assert 'model_reasoning_effort="high"' in codex_command
    assert "--ignore-user-config" in codex_command
    assert "--skip-git-repo-check" in codex_command
    for feature in local_review.CODEX_DISABLED_FEATURES:
        assert feature in codex_command


def test_provider_output_is_stopped_at_the_live_capture_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    claude = fake_bin / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\nprint('x' * 4096)\n",
        encoding="utf-8",
    )
    claude.chmod(claude.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(local_review, "MAX_MODEL_OUTPUT_BYTES", 128)
    raw = tmp_path / "raw"
    raw.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review", encoding="utf-8")
    lane = local_review.Lane(
        "claude", "security-abuse", prompt, raw / "result.json", raw
    )
    runtime = _provider_runtime(claude.resolve(), fake_bin / "unused-codex")
    result = local_review._invoke_lane(lane, tmp_path, runtime)
    assert result.success is False
    assert "stdout exceeded" in result.detail
    assert (raw / "stdout.txt").stat().st_size == 128


def test_isolated_git_environment_ignores_extensible_config():
    environment = local_review._isolated_git_environment()
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_PAGER"] == "cat"


def _fake_provider(path: Path, provider: str) -> None:
    body = f"""#!/usr/bin/env python3
import json
from pathlib import Path
import re
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.220 (Claude Code)" if {provider!r} == "claude" else "codex-cli 0.146.0-alpha.9.2")
    raise SystemExit(0)
if {provider!r} == "codex" and args == ["debug", "models", "--bundled"]:
    print(json.dumps({{"models": [{{"slug": "gpt-5.6-sol", "supported_reasoning_levels": [{{"effort": "high"}}]}}]}}))
    raise SystemExit(0)
if {provider!r} == "claude" and args[:3] == ["auth", "status", "--json"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}}))
    raise SystemExit(0)
if {provider!r} == "codex" and args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)

if {provider!r} == "claude":
    if args.count("--strict-mcp-config") != 1 or args.count("--mcp-config") != 1:
        raise SystemExit("invalid strict MCP arguments")
    try:
        mcp_config = json.loads(args[args.index("--mcp-config") + 1])
    except (ValueError, IndexError, json.JSONDecodeError):
        raise SystemExit("malformed MCP config")
    if mcp_config != {{"mcpServers": {{}}}}:
        raise SystemExit("MCP config must contain only an empty mcpServers record")
    if args[args.index("--model") + 1] != "claude-opus-5":
        raise SystemExit("unexpected Claude model")
    if args[args.index("--effort") + 1] != "high":
        raise SystemExit("unexpected Claude effort")
    if args[args.index("--tools") + 1] != "" or args[args.index("--disallowedTools") + 1] != "*":
        raise SystemExit("Claude tools were not disabled")
else:
    if args[args.index("--model") + 1] != "gpt-5.6-sol":
        raise SystemExit("unexpected Codex model")
    if 'model_reasoning_effort="high"' not in args:
        raise SystemExit("unexpected Codex effort")

prompt = sys.stdin.read()
def field(name):
    match = re.search(r"- " + re.escape(name) + r": `([^`]+)`", prompt)
    if not match:
        raise SystemExit("missing " + name)
    return match.group(1)

receipt = {{
    "provider": field("Provider"),
    "lane": field("Lane"),
    "base_sha": field("Base SHA"),
    "head_sha": field("Head SHA"),
    "review_status": "completed",
    "overall_verdict": "pass",
    "summary": "No actionable finding.",
    "findings": [],
    "complexity": {{
        "net_complexity_delta": "neutral",
        "complexity_risk": "low",
        "added_complexity_justified": True,
        "justification": "No structural growth.",
        "new_structural_surfaces": [],
        "consolidation_opportunities": [],
        "simplest_credible_alternative": "Keep the current implementation."
    }},
    "coverage_gaps": []
}}
if {provider!r} == "claude":
    print(json.dumps({{"structured_output": receipt}}))
else:
    output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
    output.write_text(json.dumps(receipt), encoding="utf-8")
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.parametrize("post_failure", [False, True])
def test_end_to_end_local_panel_with_fake_subscription_clis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    post_failure: bool,
):
    repo, base, head = _repository(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_provider(fake_bin / "claude", "claude")
    _fake_provider(fake_bin / "codex", "codex")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_BIN", str((fake_bin / "claude").resolve()))
    monkeypatch.setenv("CODEX_BIN", str((fake_bin / "codex").resolve()))
    for name in local_review.BLOCKED_PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    if post_failure:
        def fail_post(*_args, **_kwargs):
            raise RuntimeError("posting unavailable")

        monkeypatch.setattr(local_review, "_post_report", fail_post)
    monkeypatch.chdir(repo)
    args = argparse.Namespace(
        pr=None,
        range=f"{base}..{head}",
        commit=None,
        repo="",
        post_pr=post_failure,
    )
    assert local_review.run_review(args) == (2 if post_failure else 0)
    review_root = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    review_root = review_root / "latch" / "reviews"
    runs = list(review_root.iterdir())
    assert len(runs) == 1
    output = runs[0]
    assert "# Latch review panel" in (output / "report.md").read_text(
        encoding="utf-8"
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["completed_lanes"] == 5
    assert summary["should_fail"] is False
    metadata = json.loads((output / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["models"]["claude"] == {
        "model": "claude-opus-5",
        "reasoning_effort": "high",
    }
    assert metadata["models"]["codex"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }
    assert metadata["executables"]["claude"] == {
        "path": str((fake_bin / "claude").resolve()),
        "version": "2.1.220 (Claude Code)",
    }
    assert metadata["executables"]["codex"] == {
        "path": str((fake_bin / "codex").resolve()),
        "version": "codex-cli 0.146.0-alpha.9.2",
        "capability_source": "bundled_model_catalog",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
    }
    assert metadata["billing_guard"]["account_credit_settings"] == (
        "not_verifiable_by_cli"
    )
    assert metadata["posted_to_pr"] is False
    assert metadata["post_error"] == (
        "posting unavailable" if post_failure else None
    )
    captured = capsys.readouterr()
    assert "# Latch review panel" in captured.out
    assert "Account-level usage credits" in captured.err
    assert "with 5 parallel lane(s)" in captured.err
    assert "Local review saved to" in captured.err
    if post_failure:
        assert "PR posting failed: posting unavailable" in captured.err


def test_post_report_paginates_and_updates_the_existing_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    report = tmp_path / "report.md"
    report.write_text(f"{local_review.REPORT_MARKER}\nreport\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[-1] == "user":
            output = json.dumps({"login": "reviewer"})
        elif "comments?per_page=100" in command[-1]:
            output = json.dumps(
                [
                    [],
                    [
                        {
                            "id": 321,
                            "user": {"login": "reviewer"},
                            "body": local_review.REPORT_MARKER,
                        }
                    ],
                ]
            )
        elif command[:3] == ["gh", "pr", "view"]:
            output = json.dumps({"headRefOid": "b" * 40})
        else:
            output = "{}"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(local_review, "_run", fake_run)
    scope = local_review.ReviewScope(
        "a" * 40, "b" * 40, "open-latch/latch", 73, "fixture"
    )
    local_review._post_report(tmp_path, scope, report)
    comments_call = commands[1]
    assert "--paginate" in comments_call
    assert "--slurp" in comments_call
    assert commands[-1][commands[-1].index("--method") + 1] == "PATCH"
    assert "repos/open-latch/latch/issues/comments/321" in commands[-1]


def test_post_report_refuses_when_pr_head_advanced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    report = tmp_path / "report.md"
    report.write_text(f"{local_review.REPORT_MARKER}\nreport\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[-1] == "user":
            output = json.dumps({"login": "reviewer"})
        elif "comments?per_page=100" in command[-1]:
            output = json.dumps([[]])
        elif command[:3] == ["gh", "pr", "view"]:
            output = json.dumps({"headRefOid": "c" * 40})
        else:
            raise AssertionError(f"unexpected write command: {command}")
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(local_review, "_run", fake_run)
    scope = local_review.ReviewScope(
        "a" * 40, "b" * 40, "open-latch/latch", 73, "fixture"
    )
    with pytest.raises(ValueError, match="advanced.*local report was not posted"):
        local_review._post_report(tmp_path, scope, report)

    assert report.is_file()
    assert not any("--method" in command for command in commands)
