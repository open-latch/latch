"""Tests for the local subscription-backed review orchestrator."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import local_review  # noqa: E402


SCHEMA_CANARY = json.loads(
    (ROOT / "tests" / "fixtures" / "review_provider_schema_canary.json").read_text(
        encoding="utf-8"
    )
)


def _schema_patterns(value):
    if isinstance(value, dict):
        pattern = value.get("pattern")
        if isinstance(pattern, str):
            yield pattern
        for child in value.values():
            yield from _schema_patterns(child)
    elif isinstance(value, list):
        for child in value:
            yield from _schema_patterns(child)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _repository(
    tmp_path: Path, changed_path: str = "source.py"
) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Review Test")
    _git(repo, "config", "user.email", "review@example.test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    changed = repo / changed_path
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("answer = 42\n", encoding="utf-8")
    _git(repo, "add", changed_path)
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


def _write_fake_chatgpt_auth(codex_home: Path) -> None:
    codex_home.mkdir()
    token_body = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 7200}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "last_refresh": "2026-08-05T00:00:00Z",
                "tokens": {
                    "access_token": f"e30.{token_body}.fixture",
                    "account_id": "fixture-account",
                    "id_token": "fixture-id-token",
                    "refresh_token": "must-not-be-copied",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("variable", local_review.BLOCKED_PROVIDER_ENV_VARS)
def test_preflight_rejects_provider_override_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
):
    for name in local_review.BLOCKED_PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, "must-not-be-used")
    called = False

    def forbidden(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("preflight must stop before a subprocess")

    monkeypatch.setattr(local_review, "_run", forbidden)
    with pytest.raises(ValueError, match=variable):
        local_review.preflight_auth(tmp_path)
    assert called is False


def test_preflight_accepts_subscription_logins_and_scrubs_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    for name in local_review.BLOCKED_PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    tools = {
        name: _stub_executable(tmp_path / name)
        for name in ("claude", "codex", "git")
    }
    monkeypatch.setenv("CLAUDE_BIN", str(tools["claude"]))
    monkeypatch.setenv("CODEX_BIN", str(tools["codex"]))
    executable_probes: list[str] = []

    def fake_which(name):
        executable_probes.append(name)
        return str(tools[name]) if name == "git" else None

    monkeypatch.setattr(local_review.shutil, "which", fake_which)
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
        elif provider == "codex" and command[1:2] == ["exec"]:
            schema = Path(command[command.index("--output-schema") + 1])
            assert schema.read_text(encoding="utf-8") == "{"
            assert "--strict-config" in command
            assert "tools.view_image=false" not in command
            for value in local_review.CODEX_PERMISSION_CONFIG:
                assert command.count(value) == 1
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                f"Output schema file {schema} is not valid JSON: fixture\n",
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
    runtime = local_review.preflight_auth(tmp_path, require_gh=False)
    assert runtime.authentication == {
        "claude": "claude.ai/max",
        "codex": "ChatGPT",
    }
    assert runtime.claude_executable == str(tools["claude"].resolve())
    assert runtime.claude_version == "2.1.220 (Claude Code)"
    assert runtime.codex_executable == str(tools["codex"].resolve())
    assert runtime.codex_version == "codex-cli 0.146.0-alpha.9.2"
    assert executable_probes == ["git"]
    assert all(
        name not in environment
        for environment in environments
        for name in (
            *local_review.BLOCKED_PROVIDER_ENV_VARS,
            *local_review.PROVIDER_EXECUTABLE_ENV_VARS.values(),
        )
    )


def test_isolated_codex_runtime_uses_access_only_chatgpt_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_home = tmp_path / "source-codex-home"
    _write_fake_chatgpt_auth(source_home)
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "ambient-token-must-not-leak")
    executable = _stub_executable(tmp_path / "codex")
    runtime = _provider_runtime(tmp_path / "claude", executable)
    observed_environment: dict[str, str] = {}

    def fake_run(command, *, environment=None, **_kwargs):
        assert command == [str(executable), "login", "status"]
        observed_environment.update(environment)
        return subprocess.CompletedProcess(
            command, 0, "Logged in using ChatGPT\n", ""
        )

    monkeypatch.setattr(local_review, "_run", fake_run)
    provider_root = tmp_path / "provider-runtime"
    environment = local_review._isolated_codex_environment(
        provider_root, runtime, tmp_path
    )
    isolated_auth = Path(environment["CODEX_HOME"]) / "auth.json"
    payload = json.loads(isolated_auth.read_text(encoding="utf-8"))
    assert payload["auth_mode"] == "chatgpt"
    assert payload["last_refresh"].endswith("Z")
    assert payload["tokens"]["access_token"]
    assert payload["tokens"]["account_id"] == "fixture-account"
    assert payload["tokens"]["id_token"] == "fixture-id-token"
    assert payload["tokens"]["refresh_token"] == ""
    assert stat.S_IMODE(isolated_auth.stat().st_mode) == 0o600
    assert environment == observed_environment
    assert environment["HOME"].startswith(str(provider_root))
    assert environment["CODEX_HOME"].startswith(str(provider_root))
    assert "CODEX_ACCESS_TOKEN" not in environment


@pytest.mark.parametrize("missing", ["access_token", "account_id", "id_token"])
def test_isolated_codex_runtime_rejects_incomplete_token_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
):
    source_home = tmp_path / "source-codex-home"
    _write_fake_chatgpt_auth(source_home)
    source_auth = source_home / "auth.json"
    payload = json.loads(source_auth.read_text(encoding="utf-8"))
    del payload["tokens"][missing]
    source_auth.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    runtime = _provider_runtime(tmp_path / "claude", tmp_path / "codex")

    with pytest.raises(ValueError, match="readable ChatGPT access token"):
        local_review._isolated_codex_environment(
            tmp_path / "provider-runtime", runtime, tmp_path
        )


def test_isolated_codex_runtime_rechecks_token_lifetime_after_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_home = tmp_path / "source-codex-home"
    _write_fake_chatgpt_auth(source_home)
    source_auth = source_home / "auth.json"
    payload = json.loads(source_auth.read_text(encoding="utf-8"))
    token_body = base64.urlsafe_b64encode(
        json.dumps({"exp": 3101}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    payload["tokens"]["access_token"] = f"e30.{token_body}.fixture"
    source_auth.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    executable = _stub_executable(tmp_path / "codex")
    runtime = _provider_runtime(tmp_path / "claude", executable)
    clock = iter((1000, 1002))
    monkeypatch.setattr(local_review.time, "time", lambda: next(clock))
    monkeypatch.setattr(
        local_review,
        "_run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "Logged in using ChatGPT\n", ""
        ),
    )

    with pytest.raises(ValueError, match="cannot cover the full review window"):
        local_review._isolated_codex_environment(
            tmp_path / "provider-runtime", runtime, tmp_path
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


def test_codex_invocation_preflight_fails_closed_on_strict_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = _stub_executable(tmp_path / "codex")
    commands: list[list[str]] = []

    def reject_config(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "Error: unknown configuration field `tools.view_image`\n",
        )

    monkeypatch.setattr(local_review, "_run", reject_config)
    with pytest.raises(ValueError, match="strict local-review isolation contract"):
        local_review._require_codex_invocation_capability(
            str(executable), "codex-cli fixture", tmp_path, {}
        )
    assert len(commands) == 1
    assert "--strict-config" in commands[0]


def test_codex_invocation_preflight_uses_the_live_lane_config_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    executable = _stub_executable(tmp_path / "codex")
    observed: list[str] = []

    def accept_until_schema(command, *, environment, **_kwargs):
        observed.extend(command)
        schema = Path(command[command.index("--output-schema") + 1])
        assert schema.read_text(encoding="utf-8") == "{"
        assert Path(environment["CODEX_HOME"]).parent == schema.parent
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            f"Output schema file {schema} is not valid JSON: fixture\n",
        )

    monkeypatch.setattr(local_review, "_run", accept_until_schema)
    local_review._require_codex_invocation_capability(
        str(executable), "codex-cli fixture", tmp_path, {}
    )
    assert "--sandbox" not in observed
    assert "tools.view_image=false" not in observed
    for value in local_review.CODEX_PERMISSION_CONFIG:
        assert observed.count(value) == 1
    for feature in local_review.CODEX_DISABLED_FEATURES:
        assert observed.count(feature) == 1


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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (argparse.Namespace(pr=75, range=None, commit=None, post_pr=False), True),
        (argparse.Namespace(pr=None, range=None, commit=None, post_pr=False), True),
        (
            argparse.Namespace(
                pr=None, range="main...HEAD", commit=None, post_pr=False
            ),
            False,
        ),
        (
            argparse.Namespace(pr=None, range=None, commit="HEAD", post_pr=False),
            False,
        ),
        (
            argparse.Namespace(
                pr=None, range="main...HEAD", commit=None, post_pr=True
            ),
            True,
        ),
    ],
)
def test_github_cli_requirement_is_scoped_to_github_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: argparse.Namespace,
    expected: bool,
):
    args.repo = ""
    observed: list[bool] = []

    def capture_preflight(_repo, *, require_gh):
        observed.append(require_gh)
        raise RuntimeError("preflight captured")

    monkeypatch.setattr(local_review, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(local_review, "preflight_auth", capture_preflight)
    with pytest.raises(RuntimeError, match="preflight captured"):
        local_review.run_review(args)
    assert observed == [expected]


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
    changed = local_review._bare_git(
        workspace,
        git_dir,
        "diff",
        "--name-only",
        scope.base_sha,
        scope.head_sha,
    ).stdout.splitlines()
    assert changed == ["README.md"]


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


@pytest.mark.parametrize("invalid_count", [None, True, -1, 1.5, "1"])
def test_build_lanes_rejects_malformed_path_coverage_gap_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_count,
):
    workspace = tmp_path / "workspace"
    raw_root = tmp_path / "raw"
    workspace.mkdir()
    raw_root.mkdir()
    scope = local_review.ReviewScope(
        "a" * 40,
        "b" * 40,
        "",
        None,
        "range",
    )

    def write_manifest(_workspace, *args):
        prompt_root = Path(args[args.index("--output-dir") + 1])
        prompt_root.mkdir()
        prompt_name = "claude-correctness-concurrency.md"
        (prompt_root / prompt_name).write_text("review", encoding="utf-8")
        manifest = {
            "version": 1,
            "base_sha": scope.base_sha,
            "head_sha": scope.head_sha,
            "artifact_review_needed": False,
            "runtime_evidence_required": [],
            "path_classification_coverage_gap_count": invalid_count,
            "lanes": [
                {
                    "provider": "claude",
                    "lane": "correctness-concurrency",
                    "prompt": prompt_name,
                }
            ],
            "skipped": [],
        }
        Path(args[args.index("--manifest") + 1]).write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )

    monkeypatch.setattr(local_review, "_panel", write_manifest)
    with pytest.raises(ValueError, match="path coverage-gap count"):
        local_review._build_lanes(workspace, scope, raw_root)


def test_aggregate_forwards_trusted_path_coverage_gap_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    receipts = tmp_path / "receipts"
    output = tmp_path / "output"
    receipts.mkdir()
    output.mkdir()
    scope = local_review.ReviewScope(
        "a" * 40,
        "b" * 40,
        "",
        None,
        "range",
    )
    observed: list[str] = []

    def capture(_workspace, *args):
        observed.extend(args)
        summary_path = Path(args[args.index("--output-summary") + 1])
        summary_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(local_review, "_panel", capture)
    assert local_review._aggregate(
        tmp_path,
        receipts,
        scope,
        False,
        [],
        2,
        output,
    ) == {}
    marker = observed.index("--path-classification-coverage-gap-count")
    assert observed[marker + 1] == "2"


def test_provider_commands_pin_models_and_isolate_provider_tools(tmp_path: Path):
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
    assert claude_command.count("--model") == 1
    assert claude_command.count("--effort") == 1
    assert local_review.CLAUDE_MODEL in claude_command
    assert claude_command[claude_command.index("--effort") + 1] == "high"
    assert "--safe-mode" in claude_command
    assert claude_command.count("--tools") == 1
    assert claude_command[claude_command.index("--tools") + 1] == ""
    assert "--disallowedTools" not in claude_command
    assert "--max-turns" not in claude_command
    assert claude_command.count("--mcp-config") == 1
    mcp_config = claude_command[claude_command.index("--mcp-config") + 1]
    assert json.loads(mcp_config) == {"mcpServers": {}}
    assert claude_command.count("--json-schema") == 1
    claude_schema = json.loads(
        claude_command[claude_command.index("--json-schema") + 1]
    )

    codex = local_review.Lane(
        "codex", "simplicity-consolidation", prompt, raw / "codex.json", raw
    )
    codex_command = local_review._provider_command(codex, tmp_path, runtime)
    assert codex_command[0] == runtime.codex_executable
    assert local_review.CODEX_MODEL in codex_command
    assert codex_command.count("--model") == 1
    assert 'model_reasoning_effort="high"' in codex_command
    for value in local_review.CODEX_PERMISSION_CONFIG:
        assert codex_command.count(value) == 1
    assert 'web_search="disabled"' in codex_command
    assert "tools.web_search=false" in codex_command
    assert "tools.view_image=false" not in codex_command
    assert "skills.bundled.enabled=false" in codex_command
    assert codex_command.count('model_reasoning_effort="high"') == 1
    assert codex_command.count('web_search="disabled"') == 1
    assert codex_command.count("tools.web_search=false") == 1
    assert codex_command.count("skills.bundled.enabled=false") == 1
    assert "--sandbox" not in codex_command
    assert "--search" not in codex_command
    assert "--output-last-message" not in codex_command
    assert "--ignore-user-config" in codex_command
    assert "--skip-git-repo-check" in codex_command
    assert codex_command.count("--output-schema") == 1
    codex_schema_path = Path(
        codex_command[codex_command.index("--output-schema") + 1]
    )
    codex_schema = json.loads(codex_schema_path.read_text(encoding="utf-8"))
    assert claude_schema == codex_schema
    assert claude_schema["$schema"] == SCHEMA_CANARY["claude"][
        "compatible_schema_uri"
    ]
    assert claude_schema["type"] == "object"
    assert not any(
        token in pattern
        for pattern in _schema_patterns(claude_schema)
        for token in SCHEMA_CANARY["codex"]["unsupported_pattern_tokens"]
    )
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
    result = local_review._invoke_lane(
        lane, tmp_path, runtime, local_review.sanitized_environment()
    )
    assert result.success is False
    assert "stdout exceeded" in result.detail
    assert (raw / "stdout.txt").stat().st_size == 128


def test_control_command_output_is_stopped_at_the_live_capture_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    command = tmp_path / "large-output"
    command.write_text(
        "#!/usr/bin/env python3\nprint('x' * 4096)\n",
        encoding="utf-8",
    )
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(local_review, "MAX_CONTROL_OUTPUT_BYTES", 128)
    with pytest.raises(RuntimeError, match="command stdout exceeded"):
        local_review._run([str(command)], cwd=tmp_path)


def test_control_timeout_includes_a_child_that_never_reads_stdin(tmp_path: Path):
    command = tmp_path / "ignore-stdin"
    command.write_text(
        "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="exceeded the 0.2s timeout"):
        local_review._run(
            [str(command)],
            cwd=tmp_path,
            input_text="x" * (2 * 1024 * 1024),
            timeout=0.2,
        )
    assert time.monotonic() - started < 3


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_keyboard_interrupt_terminates_the_bounded_child_group(tmp_path: Path):
    pid_path = tmp_path / "child.pid"
    command = tmp_path / "long-running"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    timer = threading.Timer(0.25, lambda: os.kill(os.getpid(), signal.SIGINT))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            local_review._run([str(command), str(pid_path)], cwd=tmp_path, timeout=30)
    finally:
        timer.cancel()
    pid = int(pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group regression")
def test_provider_cancellation_terminates_registered_and_late_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executables = {}
    for name in ("early-provider", "late-provider"):
        executable = tmp_path / name
        executable.write_text(
            "#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        executables[name] = executable

    real_register = local_review._register_process
    early_registered = threading.Event()
    late_before_register = threading.Event()
    release_late = threading.Event()
    process_ids: dict[str, int] = {}

    def controlled_register(process, cancellation_event=None):
        name = Path(process.args[0]).name
        process_ids[name] = process.pid
        if name == "late-provider":
            late_before_register.set()
            if not release_late.wait(timeout=5):
                raise RuntimeError("test did not release late provider")
        accepted = real_register(process, cancellation_event)
        if name == "early-provider":
            early_registered.set()
        return accepted

    monkeypatch.setattr(local_review, "_register_process", controlled_register)
    local_review._PROVIDER_CANCELLATION.clear()
    outcomes: dict[str, object] = {}

    def run_provider(name: str) -> None:
        try:
            outcomes[name] = local_review._execute_bounded(
                [str(executables[name])],
                cwd=tmp_path,
                environment=local_review.sanitized_environment(),
                timeout=30,
                output_limit=1024,
                stream_label="provider",
                cancellation_event=local_review._PROVIDER_CANCELLATION,
            )
        except BaseException as exc:
            outcomes[name] = exc

    early_thread = threading.Thread(target=run_provider, args=("early-provider",))
    late_thread = threading.Thread(target=run_provider, args=("late-provider",))
    try:
        early_thread.start()
        assert early_registered.wait(timeout=5)
        late_thread.start()
        assert late_before_register.wait(timeout=5)
        local_review._cancel_provider_executions()
        release_late.set()
        early_thread.join(timeout=5)
        late_thread.join(timeout=5)
        assert not early_thread.is_alive()
        assert not late_thread.is_alive()
        assert isinstance(outcomes["early-provider"], local_review.BoundedExecution)
        assert isinstance(outcomes["late-provider"], RuntimeError)
        assert "cancelled" in str(outcomes["late-provider"])
        for pid in process_ids.values():
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        release_late.set()
        local_review._cancel_provider_executions()
        if early_thread.ident is not None:
            early_thread.join(timeout=5)
        if late_thread.ident is not None:
            late_thread.join(timeout=5)
        local_review._PROVIDER_CANCELLATION.clear()


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor inheritance")
def test_descendant_cannot_hold_control_streams_open_forever(tmp_path: Path):
    child_pid_path = tmp_path / "descendant.pid"
    command = tmp_path / "inherited-stream"
    command.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    command.chmod(command.stat().st_mode | stat.S_IXUSR)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="descendants kept output streams open"):
        local_review._run([str(command), str(child_pid_path)], cwd=tmp_path)
    assert time.monotonic() - started < 8
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_isolated_git_environment_preserves_required_os_state_only(monkeypatch):
    monkeypatch.setenv("SystemRoot", "C:/Windows")
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/attacker/objects")
    environment = local_review._isolated_git_environment()
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_PAGER"] == "cat"
    assert environment["SystemRoot"] == "C:/Windows"
    assert "GIT_OBJECT_DIRECTORY" not in environment


def _fake_provider(path: Path, provider: str) -> None:
    body = f"""#!/usr/bin/env python3
import json
import os
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
    if args.count("--json-schema") != 1:
        raise SystemExit("Claude must receive exactly one inline JSON schema")
    try:
        provider_schema = json.loads(args[args.index("--json-schema") + 1])
    except (ValueError, IndexError, json.JSONDecodeError):
        raise SystemExit("malformed Claude JSON schema")
    schema_canary = {SCHEMA_CANARY!r}
    if provider_schema.get("$schema") != schema_canary["claude"]["compatible_schema_uri"]:
        raise SystemExit(schema_canary["claude"]["stderr"])
    if args[args.index("--model") + 1] != "claude-opus-5":
        raise SystemExit("unexpected Claude model")
    if args[args.index("--effort") + 1] != "high":
        raise SystemExit("unexpected Claude effort")
    if args.count("--tools") != 1 or args[args.index("--tools") + 1] != "":
        raise SystemExit("Claude tools were not disabled")
    if "--disallowedTools" in args:
        raise SystemExit("Claude StructuredOutput must not be denied")
    if "--max-turns" in args:
        raise SystemExit("Claude StructuredOutput must not have a one-turn ceiling")
else:
    if args.count("--strict-config") != 1:
        raise SystemExit("Codex must use strict configuration")
    if "tools.view_image=false" in args:
        raise SystemExit("unknown configuration field `tools.view_image`")
    permission_config = {local_review.CODEX_PERMISSION_CONFIG!r}
    if any(args.count(value) != 1 for value in permission_config):
        raise SystemExit("Codex local-review permission profile is malformed")
    if "--sandbox" in args:
        raise SystemExit("Codex must not combine legacy sandbox mode with permissions")
    if args.count("--output-schema") != 1:
        raise SystemExit("Codex must receive exactly one output schema")
    output_schema = Path(args[args.index("--output-schema") + 1])
    if output_schema.name == "invalid-output-schema.json":
        print(
            f"Output schema file {{output_schema}} is not valid JSON: fixture",
            file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        provider_schema = json.loads(
            output_schema.read_text(encoding="utf-8")
        )
    except (ValueError, IndexError, OSError, json.JSONDecodeError):
        raise SystemExit("malformed Codex output schema")
    schema_canary = {SCHEMA_CANARY!r}
    def schema_patterns(value):
        if isinstance(value, dict):
            if isinstance(value.get("pattern"), str):
                yield value["pattern"]
            for child in value.values():
                yield from schema_patterns(child)
        elif isinstance(value, list):
            for child in value:
                yield from schema_patterns(child)
    def strict_object_schema_errors(value, path="$"):
        if isinstance(value, dict):
            if value.get("type") == "object":
                properties = set(value.get("properties", {{}}))
                required = set(value.get("required", []))
                if properties != required:
                    yield f"{{path}} object properties must all be required"
            for key, child in value.items():
                yield from strict_object_schema_errors(child, f"{{path}}.{{key}}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from strict_object_schema_errors(child, f"{{path}}[{{index}}]")
    strict_errors = list(strict_object_schema_errors(provider_schema))
    if strict_errors:
        raise SystemExit("invalid_json_schema: " + strict_errors[0])
    if any(
        token in pattern
        for pattern in schema_patterns(provider_schema)
        for token in schema_canary["codex"]["unsupported_pattern_tokens"]
    ):
        raise SystemExit(schema_canary["codex"]["stderr"])
    isolated_auth = json.loads(
        (Path(os.environ["CODEX_HOME"]) / "auth.json").read_text(encoding="utf-8")
    )
    if not isolated_auth.get("last_refresh"):
        raise SystemExit("Codex token metadata was incomplete")
    if isolated_auth.get("tokens", {{}}).get("refresh_token"):
        raise SystemExit("Codex reusable refresh credentials were copied")
    if args.count("--model") != 1 or args[args.index("--model") + 1] != "gpt-5.6-sol":
        raise SystemExit("unexpected Codex model")
    if args.count('model_reasoning_effort="high"') != 1:
        raise SystemExit("unexpected Codex effort")
    if args.count('web_search="disabled"') != 1 or args.count("tools.web_search=false") != 1:
        raise SystemExit("Codex web search was not explicitly disabled")
    if args.count("skills.bundled.enabled=false") != 1 or "skill_search" not in args:
        raise SystemExit("Codex skills were not isolated")
    if "--search" in args:
        raise SystemExit("Codex web search was enabled")

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
    "normalization_dropped_findings": 99,
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
    if "--output-last-message" in args:
        raise SystemExit("Codex result output must stay on bounded stdout")
    print(json.dumps(receipt))
"""
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fake_providers_reject_the_live_canary_schema_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
):
    schema = json.loads(local_review.SCHEMA_PATH.read_text(encoding="utf-8"))
    if provider == "claude":
        schema["$schema"] = SCHEMA_CANARY["claude"]["rejected_schema_uri"]
        expected = "no schema with key or ref"
    else:
        path_schema = (
            schema["properties"]["findings"]["items"]["properties"]
            ["code_location"]["properties"]["path"]
        )
        path_schema["pattern"] = SCHEMA_CANARY["codex"]["rejected_pattern"]
        expected = "regex lookaround is not supported"
    schema_path = tmp_path / "review.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.setattr(local_review, "SCHEMA_PATH", schema_path)

    fake = tmp_path / provider
    _fake_provider(fake, provider)
    raw = tmp_path / "raw"
    raw.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review", encoding="utf-8")
    lane = local_review.Lane(provider, "fixture", prompt, raw / "result.json", raw)
    runtime = _provider_runtime(fake, fake)

    result = local_review._invoke_lane(
        lane, tmp_path, runtime, local_review.sanitized_environment()
    )
    assert result.success is False
    assert expected in result.detail


def test_fake_codex_rejects_an_optional_object_property_in_strict_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    schema = json.loads(local_review.SCHEMA_PATH.read_text(encoding="utf-8"))
    schema["required"].remove("normalization_dropped_findings")
    schema_path = tmp_path / "review.schema.json"
    schema_path.write_text(json.dumps(schema), encoding="utf-8")
    monkeypatch.setattr(local_review, "SCHEMA_PATH", schema_path)

    fake = tmp_path / "codex"
    _fake_provider(fake, "codex")
    raw = tmp_path / "raw"
    raw.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review", encoding="utf-8")
    lane = local_review.Lane("codex", "fixture", prompt, raw / "result.json", raw)
    result = local_review._invoke_lane(
        lane,
        tmp_path,
        _provider_runtime(fake, fake),
        local_review.sanitized_environment(),
    )
    assert result.success is False
    assert "object properties must all be required" in result.detail


def test_fake_codex_reproduces_the_unsupported_view_image_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    fake = tmp_path / "codex"
    _fake_provider(fake, "codex")
    raw = tmp_path / "raw"
    raw.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review", encoding="utf-8")
    lane = local_review.Lane("codex", "fixture", prompt, raw / "result.json", raw)
    runtime = _provider_runtime(fake, fake)
    monkeypatch.setattr(
        local_review,
        "CODEX_PERMISSION_CONFIG",
        (*local_review.CODEX_PERMISSION_CONFIG, "tools.view_image=false"),
    )

    result = local_review._invoke_lane(
        lane, tmp_path, runtime, local_review.sanitized_environment()
    )
    assert result.success is False
    assert "unknown configuration field `tools.view_image`" in result.detail


@pytest.mark.parametrize(
    (
        "changed_path",
        "post_failure",
        "force_review_failure",
        "expected_review_failure",
        "expected_exit",
        "expected_lanes",
    ),
    [
        ("source.py", False, False, False, 0, 5),
        ("source.py", True, False, False, 3, 5),
        ("source.py", True, True, True, 3, 5),
        ("commands/latch-review.md", False, False, False, 0, 6),
        ("src/seed.py", False, False, True, 1, 6),
    ],
)
def test_end_to_end_local_panel_with_fake_subscription_clis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    changed_path: str,
    post_failure: bool,
    force_review_failure: bool,
    expected_review_failure: bool,
    expected_exit: int,
    expected_lanes: int,
):
    repo, base, head = _repository(tmp_path, changed_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _fake_provider(fake_bin / "claude", "claude")
    _fake_provider(fake_bin / "codex", "codex")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_BIN", str((fake_bin / "claude").resolve()))
    monkeypatch.setenv("CODEX_BIN", str((fake_bin / "codex").resolve()))
    codex_home = tmp_path / "codex-home"
    _write_fake_chatgpt_auth(codex_home)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    for name in local_review.BLOCKED_PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    launch_order: list[str] = []
    real_build_lanes = local_review._build_lanes
    real_isolated_codex_environment = local_review._isolated_codex_environment

    def build_lanes_then_record(*build_args, **build_kwargs):
        result = real_build_lanes(*build_args, **build_kwargs)
        launch_order.append("evidence_ready")
        return result

    def isolate_codex_then_record(*isolation_args, **isolation_kwargs):
        launch_order.append("codex_auth_checked")
        return real_isolated_codex_environment(*isolation_args, **isolation_kwargs)

    monkeypatch.setattr(local_review, "_build_lanes", build_lanes_then_record)
    monkeypatch.setattr(
        local_review,
        "_isolated_codex_environment",
        isolate_codex_then_record,
    )
    if post_failure:
        def fail_post(*_args, **_kwargs):
            raise RuntimeError("posting unavailable")

        monkeypatch.setattr(local_review, "_post_report", fail_post)
    if force_review_failure:
        real_aggregate = local_review._aggregate

        def block_after_aggregate(*aggregate_args, **aggregate_kwargs):
            value = real_aggregate(*aggregate_args, **aggregate_kwargs)
            value["should_fail"] = True
            output_dir = aggregate_args[-1]
            (output_dir / "summary.json").write_text(
                json.dumps(value, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return value

        monkeypatch.setattr(local_review, "_aggregate", block_after_aggregate)
    monkeypatch.chdir(repo)
    args = argparse.Namespace(
        pr=None,
        range=f"{base}..{head}",
        commit=None,
        repo="",
        post_pr=post_failure,
    )
    assert local_review.run_review(args) == expected_exit
    assert launch_order == ["evidence_ready", "codex_auth_checked"]
    review_root = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    review_root = review_root / "latch" / "reviews"
    runs = list(review_root.iterdir())
    assert len(runs) == 1
    output = runs[0]
    assert "# Latch review panel" in (output / "report.md").read_text(
        encoding="utf-8"
    )
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["completed_lanes"] == expected_lanes
    assert summary["should_fail"] is expected_review_failure
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
        "isolation_capability_source": "strict_config_invalid_schema_probe",
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
    assert metadata["review_should_fail"] is expected_review_failure
    assert metadata["review_exit_code"] == (1 if expected_review_failure else 0)
    assert metadata["process_exit_code"] == expected_exit
    assert metadata["runtime_evidence_required"] == (
        ["seed-report"] if changed_path == "src/seed.py" else []
    )
    assert metadata["path_classification_coverage_gap_count"] == 0
    captured = capsys.readouterr()
    assert "# Latch review panel" in captured.out
    assert "Account-level usage credits" in captured.err
    assert f"with {expected_lanes} parallel lane(s)" in captured.err
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
