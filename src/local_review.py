#!/usr/bin/env python3
"""Run the Latch review panel with local subscription-backed CLIs."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any


LATCH_HOME = Path(__file__).resolve().parent.parent
PANEL_SCRIPT = LATCH_HOME / ".github" / "scripts" / "review_panel.py"
POLICY_PATH = LATCH_HOME / ".github" / "review-panel" / "policy.json"
SCHEMA_PATH = LATCH_HOME / ".github" / "review-panel" / "review.schema.json"
REPORT_MARKER = "<!-- ai-review-panel-report -->"
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")
PROVIDER_OVERRIDE_ENV_VARS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
)
BLOCKED_PROVIDER_ENV_VARS = API_KEY_ENV_VARS + PROVIDER_OVERRIDE_ENV_VARS
PROVIDER_EXECUTABLE_ENV_VARS = {
    "claude": "CLAUDE_BIN",
    "codex": "CODEX_BIN",
}
CLAUDE_MODEL = "claude-opus-5"
CLAUDE_EFFORT = "high"
CODEX_MODEL = "gpt-5.6-sol"
CODEX_EFFORT = "high"
CODEX_DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "network_proxy",
    "plugin_sharing",
    "plugins",
    "shell_tool",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_MODEL_OUTPUT_BYTES = 10 * 1024 * 1024
MODEL_TIMEOUT_SECONDS = 30 * 60
FETCH_TIMEOUT_SECONDS = 5 * 60
PREFLIGHT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ReviewScope:
    base_sha: str
    head_sha: str
    repository: str
    pr_number: int | None
    source: str


@dataclass(frozen=True)
class Lane:
    provider: str
    lane: str
    prompt: Path
    result: Path
    raw_dir: Path


@dataclass(frozen=True)
class LaneResult:
    provider: str
    lane: str
    success: bool
    result: Path
    detail: str


@dataclass(frozen=True)
class ProviderRuntime:
    authentication: dict[str, str]
    claude_executable: str
    claude_version: str
    codex_executable: str
    codex_version: str


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{command[0]} exceeded the {timeout:g}s timeout"
        ) from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"{command[0]} exited {result.returncode}: "
            f"{detail[:2000] or 'no diagnostic output'}"
        )
    return result


def _sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not SHA_RE.fullmatch(normalized):
        raise ValueError(f"{label} did not resolve to a full commit SHA")
    return normalized


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], cwd=repo, check=check)


def _isolated_git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", ""),
    }


def _bare_git(
    workspace: Path,
    git_dir: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        [
            "git",
            f"--git-dir={git_dir}",
            "-c",
            f"core.attributesFile={os.devnull}",
            "-c",
            "diff.external=",
            *args,
        ],
        cwd=workspace,
        environment=_isolated_git_environment(),
        check=check,
    )


def repository_root() -> Path:
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=Path.cwd())
    root = Path(result.stdout.strip()).resolve()
    if not root.is_dir():
        raise ValueError("the current directory is not inside a Git repository")
    return root


def _repository_name(repo: Path, explicit: str) -> str:
    if explicit:
        if not REPOSITORY_RE.fullmatch(explicit):
            raise ValueError("--repo must be an owner/name pair")
        return explicit
    result = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=repo,
    )
    value = result.stdout.strip()
    if not REPOSITORY_RE.fullmatch(value):
        raise ValueError("could not resolve the current GitHub owner/name")
    return value


def _rev_parse(repo: Path, revision: str, label: str) -> str:
    if not revision.strip():
        raise ValueError(f"{label} revision is empty")
    return _sha(
        _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout,
        label,
    )


def _network_git_environment() -> dict[str, str]:
    environment = sanitized_environment()
    for name in list(environment):
        if name.startswith("GIT_CONFIG_") or name in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_EXTERNAL_DIFF",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_PROXY_COMMAND",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_WORK_TREE",
        }:
            environment.pop(name, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_PAGER"] = "cat"
    return environment


def _fetch_pr_commit(
    workspace: Path,
    git_dir: Path,
    repository: str,
    commit: str,
) -> None:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError(f"invalid GitHub repository name: {repository!r}")
    _run(
        [
            "git",
            f"--git-dir={git_dir}",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            f"https://github.com/{repository}.git",
            commit,
        ],
        cwd=workspace,
        environment=_network_git_environment(),
        timeout=FETCH_TIMEOUT_SECONDS,
    )
    resolved = _sha(
        _bare_git(workspace, git_dir, "rev-parse", "FETCH_HEAD^{commit}").stdout,
        "fetched commit",
    )
    if resolved != commit:
        raise ValueError(f"fetched {resolved}, expected immutable commit {commit}")


def _resolve_pr(
    repo: Path,
    number: int,
    repository: str,
    workspace: Path,
) -> ReviewScope:
    fields = "number,baseRefOid,headRefOid,headRepository,url"
    command = ["gh", "pr", "view", str(number), "--json", fields]
    if repository:
        command.extend(["--repo", repository])
    payload = json.loads(_run(command, cwd=repo).stdout)
    base_tip = _sha(str(payload.get("baseRefOid") or ""), "PR base")
    head_sha = _sha(str(payload.get("headRefOid") or ""), "PR head")
    head_repository = str((payload.get("headRepository") or {}).get("nameWithOwner") or "")
    base_repository = _repository_name(repo, repository)
    target = workspace / "review-target"
    _run(["git", "init", "--bare", str(target)], cwd=workspace)
    _fetch_pr_commit(workspace, target, base_repository, base_tip)
    _bare_git(workspace, target, "update-ref", "refs/review/base-tip", base_tip)
    _fetch_pr_commit(workspace, target, head_repository, head_sha)
    _bare_git(workspace, target, "update-ref", "refs/review/head", head_sha)
    base_sha = _sha(
        _bare_git(workspace, target, "merge-base", base_tip, head_sha).stdout,
        "PR merge base",
    )
    _bare_git(workspace, target, "update-ref", "refs/review/base", base_sha)
    return ReviewScope(
        base_sha,
        head_sha,
        base_repository,
        number,
        str(payload.get("url") or f"{base_repository}#{number}"),
    )


def _resolve_range(repo: Path, value: str, repository: str) -> ReviewScope:
    if value.count("...") == 1:
        left, right = value.split("...", 1)
        base_tip = _rev_parse(repo, left, "range base")
        head_sha = _rev_parse(repo, right, "range head")
        base_sha = _sha(
            _git(repo, "merge-base", base_tip, head_sha).stdout,
            "range merge base",
        )
    elif value.count("..") == 1:
        left, right = value.split("..", 1)
        base_sha = _rev_parse(repo, left, "range base")
        head_sha = _rev_parse(repo, right, "range head")
    else:
        raise ValueError("--range must use BASE...HEAD or BASE..HEAD syntax")
    return ReviewScope(base_sha, head_sha, repository, None, value)


def _resolve_commit(repo: Path, value: str, repository: str) -> ReviewScope:
    head_sha = _rev_parse(repo, value, "commit")
    commit_body = _git(repo, "cat-file", "-p", head_sha).stdout
    parent_match = re.search(r"^parent ([0-9a-f]{40})$", commit_body, re.MULTILINE)
    if parent_match is None:
        base_sha = EMPTY_TREE_SHA
    else:
        base_sha = _sha(parent_match.group(1), "commit parent")
        available = _git(
            repo,
            "cat-file",
            "-e",
            f"{base_sha}^{{commit}}",
            check=False,
        )
        if available.returncode != 0:
            raise ValueError(
                "the commit's first parent is missing locally; fetch history and retry"
            )
    return ReviewScope(base_sha, head_sha, repository, None, value)


def resolve_scope(
    args: argparse.Namespace, repo: Path, workspace: Path
) -> ReviewScope:
    if args.pr is not None:
        return _resolve_pr(repo, args.pr, args.repo, workspace)
    repository = _repository_name(repo, args.repo) if args.repo else ""
    if args.commit:
        return _resolve_commit(repo, args.commit, repository)
    if args.range:
        return _resolve_range(repo, args.range, repository)
    command = ["gh", "pr", "view", "--json", "number", "--jq", ".number"]
    if args.repo:
        command.extend(["--repo", args.repo])
    result = _run(command, cwd=repo, check=False)
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise ValueError(
            "no review scope supplied and the current branch has no pull request; "
            "use --pr, --range, or --commit"
        )
    return _resolve_pr(repo, int(result.stdout.strip()), args.repo, workspace)


def sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in BLOCKED_PROVIDER_ENV_VARS:
        environment.pop(name, None)
    for name in PROVIDER_EXECUTABLE_ENV_VARS.values():
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _resolve_provider_executable(provider: str) -> str:
    selector = PROVIDER_EXECUTABLE_ENV_VARS[provider]
    configured = os.environ.get(selector)
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"{selector} must be an absolute executable path")
    else:
        located = shutil.which(provider)
        if located is None:
            raise ValueError(f"required executable is not installed: {provider}")
        candidate = Path(located)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{selector} does not resolve to an executable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"{selector} does not resolve to an executable")
    return str(resolved)


def _provider_version(executable: str, repo: Path, environment: dict[str, str]) -> str:
    result = _run(
        [executable, "--version"],
        cwd=repo,
        environment=environment,
        check=False,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    version = result.stdout.strip().splitlines()
    if result.returncode != 0 or not version:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            f"could not read provider version from {executable}: "
            f"{detail[:500] or 'no diagnostic output'}"
        )
    return version[-1][:200]


def _require_codex_model_capability(
    executable: str,
    version: str,
    repo: Path,
    environment: dict[str, str],
) -> None:
    result = _run(
        [executable, "debug", "models", "--bundled"],
        cwd=repo,
        environment=environment,
        check=False,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(
            f"Codex executable {executable} ({version}) could not expose its "
            f"bundled model catalog: {detail[:500] or 'no diagnostic output'}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Codex executable {executable} ({version}) returned an unreadable "
            "bundled model catalog"
        ) from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    model = next(
        (
            item
            for item in models or []
            if isinstance(item, dict) and item.get("slug") == CODEX_MODEL
        ),
        None,
    )
    if model is None:
        raise ValueError(
            f"Codex executable {executable} ({version}) does not bundle "
            f"{CODEX_MODEL}. Select a compatible ChatGPT-authenticated CLI with "
            "an absolute CODEX_BIN; no model downgrade was attempted."
        )
    efforts = model.get("supported_reasoning_levels")
    supported = {
        item.get("effort")
        for item in efforts or []
        if isinstance(item, dict)
    }
    if CODEX_EFFORT not in supported:
        raise ValueError(
            f"Codex executable {executable} ({version}) does not bundle "
            f"{CODEX_MODEL} with effort {CODEX_EFFORT}. Select a compatible "
            "ChatGPT-authenticated CLI with an absolute CODEX_BIN; no effort "
            "downgrade was attempted."
        )


def preflight_auth(repo: Path) -> ProviderRuntime:
    present = [name for name in BLOCKED_PROVIDER_ENV_VARS if os.environ.get(name)]
    if present:
        raise ValueError(
            "refusing to start while provider authentication or endpoint "
            "override environment variables are "
            f"set: {', '.join(present)}. Unset them and retry."
        )
    claude_executable = _resolve_provider_executable("claude")
    codex_executable = _resolve_provider_executable("codex")
    for executable in ("gh", "git"):
        if shutil.which(executable) is None:
            raise ValueError(f"required executable is not installed: {executable}")
    environment = sanitized_environment()
    claude_version = _provider_version(claude_executable, repo, environment)
    codex_version = _provider_version(codex_executable, repo, environment)
    _require_codex_model_capability(
        codex_executable, codex_version, repo, environment
    )
    claude = _run(
        [claude_executable, "auth", "status", "--json"],
        cwd=repo,
        environment=environment,
        check=False,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    try:
        claude_status = json.loads(claude.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Claude Code did not return a readable auth status") from exc
    if (
        claude.returncode != 0
        or claude_status.get("loggedIn") is not True
        or claude_status.get("authMethod") != "claude.ai"
        or claude_status.get("apiProvider") != "firstParty"
        or not claude_status.get("subscriptionType")
    ):
        raise ValueError(
            "Claude Code is not using a claude.ai subscription login. Run "
            "`claude auth login`, and ensure ANTHROPIC_API_KEY is unset."
        )
    codex = _run(
        [codex_executable, "login", "status"],
        cwd=repo,
        environment=environment,
        check=False,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    if (
        codex.returncode != 0
        or "Logged in using ChatGPT" not in f"{codex.stdout}\n{codex.stderr}"
    ):
        raise ValueError(
            "Codex CLI is not using a ChatGPT login. Run `codex login`, and "
            "ensure OPENAI_API_KEY and CODEX_API_KEY are unset."
        )
    return ProviderRuntime(
        authentication={
            "claude": f"claude.ai/{str(claude_status['subscriptionType']).lower()}",
            "codex": "ChatGPT",
        },
        claude_executable=claude_executable,
        claude_version=claude_version,
        codex_executable=codex_executable,
        codex_version=codex_version,
    )


def _prepare_object_store(repo: Path, workspace: Path, scope: ReviewScope) -> Path:
    target = workspace / "review-target"
    _run(["git", "init", "--bare", str(target)], cwd=workspace)
    for label, commit in (("base", scope.base_sha), ("head", scope.head_sha)):
        if commit == EMPTY_TREE_SHA:
            created = _run(
                [
                    "git",
                    f"--git-dir={target}",
                    "hash-object",
                    "-w",
                    "-t",
                    "tree",
                    "--stdin",
                ],
                cwd=workspace,
                environment=_isolated_git_environment(),
                input_text="",
            )
            resolved = _sha(created.stdout, "empty tree")
        else:
            _bare_git(workspace, target, "fetch", "--no-tags", str(repo), commit)
            resolved = _sha(
                _bare_git(
                    workspace, target, "rev-parse", "FETCH_HEAD^{commit}"
                ).stdout,
                f"local {label}",
            )
        if resolved != commit:
            raise ValueError(f"local object store resolved {resolved}, expected {commit}")
        _bare_git(workspace, target, "update-ref", f"refs/review/{label}", commit)
    return target


def _artifact_review_needed(
    workspace: Path,
    git_dir: Path,
    scope: ReviewScope,
    policy: dict[str, Any],
) -> bool:
    pathspecs = [f":(glob){pattern}" for pattern in policy["user_facing_paths"]]
    result = _bare_git(
        workspace,
        git_dir,
        "diff",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        scope.base_sha,
        scope.head_sha,
        "--",
        *pathspecs,
        check=False,
    )
    if result.returncode not in {0, 1}:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Git could not classify user-facing paths: {detail}")
    return result.returncode == 1


def _panel(workspace: Path, *args: str) -> None:
    _run(
        [sys.executable, str(PANEL_SCRIPT), *args],
        cwd=workspace,
        environment=sanitized_environment(),
    )


def _build_lanes(
    workspace: Path,
    scope: ReviewScope,
    policy: dict[str, Any],
    artifact_needed: bool,
    raw_root: Path,
) -> tuple[list[Lane], list[tuple[str, str]]]:
    lanes: list[Lane] = []
    skipped: list[tuple[str, str]] = []
    prompt_root = workspace / "prompts"
    prompt_root.mkdir()
    for config in policy["lanes"]:
        provider, lane = str(config["provider"]), str(config["id"])
        if config["when"] != "always" and not artifact_needed:
            skipped.append((provider, lane))
            continue
        prompt = prompt_root / f"{provider}-{lane}.md"
        _panel(
            workspace,
            "build-prompt",
            "--provider",
            provider,
            "--lane",
            lane,
            "--base-sha",
            scope.base_sha,
            "--head-sha",
            scope.head_sha,
            "--review-directory",
            "review-target",
            "--output",
            str(prompt),
        )
        raw_dir = raw_root / f"{provider}-{lane}"
        raw_dir.mkdir(parents=True)
        lanes.append(
            Lane(provider, lane, prompt, raw_dir / "result.json", raw_dir)
        )
    return lanes, skipped


def _provider_command(
    lane: Lane,
    workspace: Path,
    runtime: ProviderRuntime,
) -> list[str]:
    if lane.provider == "claude":
        schema = json.dumps(
            json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
            sort_keys=True,
            separators=(",", ":"),
        )
        return [
            runtime.claude_executable,
            "-p",
            "--safe-mode",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "json",
            "--json-schema",
            schema,
            "--model",
            CLAUDE_MODEL,
            "--effort",
            CLAUDE_EFFORT,
            "--max-turns",
            "1",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {}}, separators=(",", ":")),
            "--tools",
            "",
            "--disallowedTools",
            "*",
        ]
    command = [
        runtime.codex_executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--model",
        CODEX_MODEL,
        "--config",
        f'model_reasoning_effort="{CODEX_EFFORT}"',
    ]
    for feature in CODEX_DISABLED_FEATURES:
        command.extend(["--disable", feature])
    command.extend(
        [
            "--output-schema",
            str(SCHEMA_PATH),
            "--output-last-message",
            str(lane.result),
            "--color",
            "never",
            "-",
        ]
    )
    return command


def _extract_claude(stdout_path: Path, result_path: Path) -> None:
    if stdout_path.stat().st_size > MAX_MODEL_OUTPUT_BYTES:
        raise ValueError("Claude output exceeded the local safety limit")
    outer = json.loads(stdout_path.read_text(encoding="utf-8"))
    structured = outer.get("structured_output")
    if isinstance(structured, dict):
        result_path.write_text(
            json.dumps(structured, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    elif isinstance(outer.get("result"), str):
        result_path.write_text(outer["result"], encoding="utf-8")
    else:
        raise ValueError("Claude output lacked structured_output or result")


def _read_limited_text(path: Path, limit: int = 1_000_000) -> str:
    if not path.exists():
        return ""
    return path.read_bytes()[:limit].decode("utf-8", errors="replace")


def _drain_provider_stream(
    stream: Any,
    destination: Path,
    process: subprocess.Popen[bytes],
    failures: list[str],
    label: str,
) -> None:
    written = 0
    try:
        with destination.open("wb") as output:
            while chunk := stream.read(64 * 1024):
                remaining = MAX_MODEL_OUTPUT_BYTES - written
                if remaining > 0:
                    output.write(chunk[:remaining])
                    written += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    failures.append(f"{label} exceeded the local safety limit")
                    process.kill()
                    return
    except Exception as exc:
        failures.append(f"could not capture {label}: {exc}")
        try:
            process.kill()
        except OSError:
            pass


def _invoke_lane(
    lane: Lane,
    workspace: Path,
    runtime: ProviderRuntime,
) -> LaneResult:
    stdout_path, stderr_path = lane.raw_dir / "stdout.txt", lane.raw_dir / "stderr.txt"
    try:
        with lane.prompt.open("rb") as prompt:
            process = subprocess.Popen(
                _provider_command(lane, workspace, runtime),
                cwd=workspace,
                env=sanitized_environment(),
                stdin=prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            capture_failures: list[str] = []
            drainers = [
                threading.Thread(
                    target=_drain_provider_stream,
                    args=(stream, path, process, capture_failures, label),
                    daemon=True,
                )
                for stream, path, label in (
                    (process.stdout, stdout_path, "provider stdout"),
                    (process.stderr, stderr_path, "provider stderr"),
                )
            ]
            for drainer in drainers:
                drainer.start()
            try:
                returncode = process.wait(timeout=MODEL_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                for drainer in drainers:
                    drainer.join()
                return LaneResult(
                    lane.provider, lane.lane, False, lane.result, "timed out"
                )
            for drainer in drainers:
                drainer.join()
            if capture_failures:
                return LaneResult(
                    lane.provider,
                    lane.lane,
                    False,
                    lane.result,
                    "; ".join(capture_failures),
                )
        if returncode != 0:
            detail = _read_limited_text(stderr_path).strip()
            return LaneResult(
                lane.provider,
                lane.lane,
                False,
                lane.result,
                f"provider exited {returncode}: {detail[:1000]}",
            )
        if lane.provider == "claude":
            _extract_claude(stdout_path, lane.result)
        if (
            not lane.result.is_file()
            or lane.result.stat().st_size > MAX_MODEL_OUTPUT_BYTES
        ):
            raise ValueError("provider did not produce a bounded structured result")
        return LaneResult(lane.provider, lane.lane, True, lane.result, "completed")
    except Exception as exc:
        prior = _read_limited_text(stderr_path)
        stderr_path.write_text(f"{prior}\nlocal runner: {exc}\n", encoding="utf-8")
        return LaneResult(lane.provider, lane.lane, False, lane.result, str(exc)[:1000])


def _normalize(
    workspace: Path,
    receipts: Path,
    scope: ReviewScope,
    provider: str,
    lane: str,
    *,
    success: bool,
    source: Path | None = None,
    applicable: bool = True,
) -> None:
    command = [
        "normalize",
        "--provider",
        provider,
        "--lane",
        lane,
        "--base-sha",
        scope.base_sha,
        "--head-sha",
        scope.head_sha,
        "--output",
        str(receipts / f"{provider}-{lane}.json"),
        "--action-outcome",
        "success" if success else "failure",
        "--lane-applicable",
        "true" if applicable else "false",
    ]
    if source is not None and source.is_file():
        command.extend(["--source", str(source)])
    _panel(workspace, *command)


def _aggregate(
    workspace: Path,
    receipts: Path,
    scope: ReviewScope,
    artifact_needed: bool,
    output: Path,
) -> dict[str, Any]:
    report, summary = output / "report.md", output / "summary.json"
    _panel(
        workspace,
        "aggregate",
        "--input-dir",
        str(receipts),
        "--base-sha",
        scope.base_sha,
        "--head-sha",
        scope.head_sha,
        "--artifact-review-needed",
        "true" if artifact_needed else "false",
        "--output-report",
        str(report),
        "--output-summary",
        str(summary),
    )
    return json.loads(summary.read_text(encoding="utf-8"))


def _post_report(repo: Path, scope: ReviewScope, report: Path) -> None:
    if scope.pr_number is None or not scope.repository:
        raise ValueError("--post-pr requires a pull-request review scope")
    environment = sanitized_environment()
    viewer = json.loads(
        _run(["gh", "api", "user"], cwd=repo, environment=environment).stdout
    )
    comment_pages = json.loads(
        _run(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{scope.repository}/issues/{scope.pr_number}/comments?per_page=100",
            ],
            cwd=repo,
            environment=environment,
        ).stdout
    )
    comments = [
        comment
        for page in comment_pages
        if isinstance(page, list)
        for comment in page
        if isinstance(comment, dict)
    ]
    login = str(viewer.get("login") or "")
    existing = [
        comment
        for comment in comments
        if str((comment.get("user") or {}).get("login") or "") == login
        and REPORT_MARKER in str(comment.get("body") or "")
    ]
    if existing:
        endpoint = f"repos/{scope.repository}/issues/comments/{int(existing[-1]['id'])}"
        method = "PATCH"
    else:
        endpoint = f"repos/{scope.repository}/issues/{scope.pr_number}/comments"
        method = "POST"
    current_pr = json.loads(
        _run(
            [
                "gh",
                "pr",
                "view",
                str(scope.pr_number),
                "--repo",
                scope.repository,
                "--json",
                "headRefOid",
            ],
            cwd=repo,
            environment=environment,
        ).stdout
    )
    current_head = _sha(
        str(current_pr.get("headRefOid") or ""), "current PR head"
    )
    if current_head != scope.head_sha:
        raise ValueError(
            f"PR #{scope.pr_number} advanced from {scope.head_sha[:12]} to "
            f"{current_head[:12]}; local report was not posted"
        )
    _run(
        ["gh", "api", "--method", method, endpoint, "--input", "-"],
        cwd=repo,
        environment=environment,
        input_text=json.dumps({"body": report.read_text(encoding="utf-8")}),
    )


def _output_dir(repo: Path, head_sha: str) -> Path:
    git_dir = Path(
        _git(repo, "rev-parse", "--absolute-git-dir").stdout.strip()
    ).resolve()
    root = (git_dir / "latch" / "reviews").resolve()
    if not root.is_relative_to(git_dir):
        raise ValueError("Git returned an unsafe local review storage path")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = root / f"{stamp}-{head_sha[:12]}-{secrets.token_hex(2)}"
    output.mkdir(parents=True)
    return output


def run_review(args: argparse.Namespace) -> int:
    repo = repository_root()
    runtime = preflight_auth(repo)
    print(
        "Authentication guard: provider API-key auth is disabled. Account-level "
        "usage credits and auto-top-up cannot be inspected locally.",
        file=sys.stderr,
    )
    print(
        f"Provider executables: Claude {runtime.claude_executable} "
        f"({runtime.claude_version}); Codex {runtime.codex_executable} "
        f"({runtime.codex_version}; bundled {CODEX_MODEL}/{CODEX_EFFORT} verified)",
        file=sys.stderr,
    )
    policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="latch-review-") as temporary:
        workspace = Path(temporary)
        scope = resolve_scope(args, repo, workspace)
        output = _output_dir(repo, scope.head_sha)
        raw_root, receipts = output / "raw", output / "receipts"
        raw_root.mkdir()
        receipts.mkdir()
        prepared_pr_store = workspace / "review-target"
        git_dir = (
            prepared_pr_store
            if prepared_pr_store.is_dir()
            else _prepare_object_store(repo, workspace, scope)
        )
        artifact_needed = _artifact_review_needed(
            workspace, git_dir, scope, policy
        )
        lanes, skipped = _build_lanes(
            workspace, scope, policy, artifact_needed, raw_root
        )
        print(
            f"Latch review: {scope.base_sha[:12]}..{scope.head_sha[:12]} "
            f"with {len(lanes)} parallel lane(s)",
            file=sys.stderr,
        )
        print(
            f"Models: Claude {CLAUDE_MODEL} (effort {CLAUDE_EFFORT}); "
            f"Codex {CODEX_MODEL} (effort {CODEX_EFFORT})",
            file=sys.stderr,
        )
        results: list[LaneResult] = []
        with ThreadPoolExecutor(max_workers=len(lanes)) as executor:
            futures = {
                executor.submit(_invoke_lane, lane, workspace, runtime): lane
                for lane in lanes
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                print(
                    f"[{'ok' if result.success else 'failed'}] "
                    f"{result.provider}/{result.lane}: {result.detail}",
                    file=sys.stderr,
                )
        for result in results:
            _normalize(
                workspace,
                receipts,
                scope,
                result.provider,
                result.lane,
                success=result.success,
                source=result.result,
            )
        for provider, lane in skipped:
            _normalize(
                workspace,
                receipts,
                scope,
                provider,
                lane,
                success=False,
                applicable=False,
            )
        summary = _aggregate(workspace, receipts, scope, artifact_needed, output)

    report = output / "report.md"
    posted_to_pr = False
    post_error = ""
    if args.post_pr:
        try:
            _post_report(repo, scope, report)
            posted_to_pr = True
            print(
                f"Posted consolidated report to PR #{scope.pr_number}.",
                file=sys.stderr,
            )
        except Exception as exc:
            post_error = str(exc)[:2000]

    metadata = {
        "scope": {
            "base_sha": scope.base_sha,
            "head_sha": scope.head_sha,
            "repository": scope.repository,
            "pr_number": scope.pr_number,
            "source": scope.source,
        },
        "models": {
            "claude": {
                "model": CLAUDE_MODEL,
                "reasoning_effort": CLAUDE_EFFORT,
            },
            "codex": {"model": CODEX_MODEL, "reasoning_effort": CODEX_EFFORT},
        },
        "authentication": runtime.authentication,
        "executables": {
            "claude": {
                "path": runtime.claude_executable,
                "version": runtime.claude_version,
            },
            "codex": {
                "path": runtime.codex_executable,
                "version": runtime.codex_version,
                "capability_source": "bundled_model_catalog",
                "model": CODEX_MODEL,
                "reasoning_effort": CODEX_EFFORT,
            },
        },
        "billing_guard": {
            "provider_api_key_environment": "absent",
            "account_credit_settings": "not_verifiable_by_cli",
        },
        "artifact_review_needed": artifact_needed,
        "posted_to_pr": posted_to_pr,
        "post_error": post_error or None,
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(report.read_text(encoding="utf-8"))
    print(f"Local review saved to {output}", file=sys.stderr)
    if post_error:
        print(f"PR posting failed: {post_error}", file=sys.stderr)
        return 2
    return 1 if summary.get("should_fail") else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--pr", type=int, help="review a GitHub pull request")
    scope.add_argument(
        "--range",
        help="review BASE...HEAD (merge-base scope) or BASE..HEAD",
    )
    scope.add_argument("--commit", help="review one commit against its first parent")
    parser.add_argument(
        "--repo",
        default="",
        help="GitHub owner/name for --pr (defaults to the current repository)",
    )
    parser.add_argument(
        "--post-pr",
        action="store_true",
        help="explicitly create or update the consolidated PR comment",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.pr is not None and args.pr <= 0:
            raise ValueError("--pr must be a positive integer")
        if args.post_pr and args.pr is None and (args.range or args.commit):
            raise ValueError("--post-pr requires --pr or an auto-detected current PR")
        return run_review(args)
    except KeyboardInterrupt:
        print("latch-review: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"latch-review: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
