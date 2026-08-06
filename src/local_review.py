#!/usr/bin/env python3
"""Run the Latch review panel with local subscription-backed CLIs."""
from __future__ import annotations

import argparse
import base64
import binascii
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any


LATCH_HOME = Path(__file__).resolve().parent.parent
PANEL_SCRIPT = LATCH_HOME / ".github" / "scripts" / "review_panel.py"
SCHEMA_PATH = LATCH_HOME / ".github" / "review-panel" / "review.schema.json"
TRUSTED_RECEIPT_FIELDS = ("normalization_dropped_findings",)
REPORT_MARKER = "<!-- ai-review-panel-report -->"
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^+\-]*$")
API_KEY_ENV_VARS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")
PROVIDER_OVERRIDE_ENV_VARS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CODEX_ACCESS_TOKEN",
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
PROVIDER_ENV_PREFIXES = (
    "ANTHROPIC_",
    "AZURE_OPENAI_",
    "CLAUDE_CODE_",
    "CODEX_",
    "OPENAI_",
)
CLAUDE_MODEL = "claude-opus-5"
CLAUDE_EFFORT = "high"
CODEX_MODEL = "gpt-5.6-sol"
CODEX_EFFORT = "high"
CODEX_PERMISSION_PROFILE = "latch-review"
CODEX_PERMISSION_CONFIG = (
    f'default_permissions="{CODEX_PERMISSION_PROFILE}"',
    (
        f'permissions.{CODEX_PERMISSION_PROFILE}.filesystem='
        '{":minimal"="read",":workspace_roots"={"."="deny"}}'
    ),
    f"permissions.{CODEX_PERMISSION_PROFILE}.network.enabled=false",
)
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
    "skill_search",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MAX_MODEL_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_CONTROL_OUTPUT_BYTES = 4 * 1024 * 1024
MODEL_TIMEOUT_SECONDS = 30 * 60
FETCH_TIMEOUT_SECONDS = 5 * 60
PREFLIGHT_TIMEOUT_SECONDS = 30
CONTROL_COMMAND_TIMEOUT_SECONDS = 60
PANEL_TIMEOUT_SECONDS = 10 * 60
_ACTIVE_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES: set[subprocess.Popen[bytes]] = set()
_PROVIDER_CANCELLATION = threading.Event()


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


@dataclass(frozen=True)
class BoundedExecution:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    failures: tuple[str, ...]


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Best-effort termination of the isolated child process group."""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.kill()
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


def _register_process(
    process: subprocess.Popen[bytes],
    cancellation_event: threading.Event | None = None,
) -> bool:
    with _ACTIVE_PROCESS_LOCK:
        if cancellation_event is not None and cancellation_event.is_set():
            return False
        _ACTIVE_PROCESSES.add(process)
        return True


def _unregister_process(process: subprocess.Popen[bytes]) -> None:
    with _ACTIVE_PROCESS_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def _cancel_provider_executions() -> None:
    """Atomically block late provider starts and terminate registered children."""
    with _ACTIVE_PROCESS_LOCK:
        _PROVIDER_CANCELLATION.set()
        processes = tuple(_ACTIVE_PROCESSES)
    for process in processes:
        _terminate_process_tree(process)


def _wait_after_termination(
    process: subprocess.Popen[bytes], failures: list[str]
) -> int:
    try:
        return process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        failures.append("child process did not exit after termination")
        return process.returncode if process.returncode is not None else -1


def _drain_bounded_stream(
    stream: Any,
    process: subprocess.Popen[bytes],
    failures: list[str],
    label: str,
    *,
    limit: int,
    chunks: list[bytes] | None = None,
    destination: Path | None = None,
) -> None:
    """Drain one child stream live, killing the child before the limit grows."""
    written = 0
    output = None
    try:
        if destination is not None:
            output = destination.open("wb")
        while chunk := stream.read(64 * 1024):
            remaining = limit - written
            accepted = chunk[: max(0, remaining)]
            if accepted:
                if output is not None:
                    output.write(accepted)
                if chunks is not None:
                    chunks.append(accepted)
                written += len(accepted)
            if len(chunk) > remaining:
                failures.append(f"{label} exceeded the local safety limit")
                _terminate_process_tree(process)
                return
    except Exception as exc:
        failures.append(f"could not capture {label}: {exc}")
        try:
            _terminate_process_tree(process)
        except OSError:
            pass
    finally:
        if output is not None:
            output.close()


def _write_process_input(
    stream: Any,
    payload: bytes,
    failures: list[str],
) -> None:
    """Write child stdin without putting the wall-clock timeout behind the write."""
    try:
        stream.write(payload)
        stream.close()
    except BrokenPipeError:
        pass
    except Exception as exc:
        failures.append(f"could not send command stdin: {exc}")
        try:
            stream.close()
        except OSError:
            pass


def _execute_bounded(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None,
    timeout: float,
    output_limit: int,
    input_text: str | None = None,
    input_path: Path | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    stream_label: str = "command",
    cancellation_event: threading.Event | None = None,
) -> BoundedExecution:
    """Own one bounded subprocess lifecycle for control and provider commands."""
    if input_text is not None and input_path is not None:
        raise ValueError("a subprocess cannot receive both text and file input")
    if cancellation_event is not None and cancellation_event.is_set():
        raise RuntimeError(f"{stream_label} execution was cancelled")
    input_file = input_path.open("rb") if input_path is not None else None
    group_options: dict[str, Any]
    if os.name == "nt":
        group_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        group_options = {"start_new_session": True}
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=(
                input_file
                if input_file is not None
                else subprocess.PIPE if input_text is not None else subprocess.DEVNULL
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **group_options,
        )
    finally:
        if input_file is not None:
            input_file.close()
    if not _register_process(process, cancellation_event):
        _terminate_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise RuntimeError(f"{stream_label} execution was cancelled")

    try:
        assert process.stdout is not None and process.stderr is not None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        failures: list[str] = []
        drainers = [
            threading.Thread(
                target=_drain_bounded_stream,
                args=(stream, process, failures, label),
                kwargs={
                    "limit": output_limit,
                    "chunks": chunks if destination is None else None,
                    "destination": destination,
                },
                daemon=True,
            )
            for stream, chunks, destination, label in (
                (process.stdout, stdout_chunks, stdout_path, f"{stream_label} stdout"),
                (process.stderr, stderr_chunks, stderr_path, f"{stream_label} stderr"),
            )
        ]
        for drainer in drainers:
            drainer.start()

        writer = None
        if input_text is not None:
            assert process.stdin is not None
            writer = threading.Thread(
                target=_write_process_input,
                args=(process.stdin, input_text.encode("utf-8"), failures),
                daemon=True,
            )
            writer.start()

        timed_out = False
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            returncode = _wait_after_termination(process, failures)
        workers = [*drainers, *([writer] if writer is not None else [])]
        for worker in workers:
            worker.join(timeout=2)
        lingering = [worker for worker in workers if worker.is_alive()]
        if lingering:
            failures.append("child process descendants kept output streams open")
            _terminate_process_tree(process)
            for worker in lingering:
                worker.join(timeout=2)
        if any(worker.is_alive() for worker in workers):
            failures.append("child process streams did not close after termination")
        return BoundedExecution(
            returncode=returncode,
            stdout=b"".join(stdout_chunks),
            stderr=b"".join(stderr_chunks),
            timed_out=timed_out,
            failures=tuple(failures),
        )
    except BaseException:
        _terminate_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        _unregister_process(process)


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = CONTROL_COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    execution = _execute_bounded(
        command,
        cwd=cwd,
        environment=environment,
        timeout=timeout,
        output_limit=MAX_CONTROL_OUTPUT_BYTES,
        input_text=input_text,
    )
    if execution.timed_out:
        raise RuntimeError(
            f"{command[0]} exceeded the {timeout:g}s timeout"
        )
    if execution.failures:
        raise RuntimeError(f"{command[0]}: {'; '.join(execution.failures)}")
    result = subprocess.CompletedProcess(
        command,
        execution.returncode,
        execution.stdout.decode("utf-8", errors="replace"),
        execution.stderr.decode("utf-8", errors="replace"),
    )
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
    environment = {
        name: os.environ[name]
        for name in (
            "COMSPEC",
            "HOME",
            "HOMEDRIVE",
            "HOMEPATH",
            "PATHEXT",
            "SystemRoot",
            "TEMP",
            "TMP",
            "TMPDIR",
            "USERPROFILE",
            "WINDIR",
        )
        if name in os.environ
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    })
    return environment


def _bare_git(
    workspace: Path,
    git_dir: Path,
    *args: str,
    check: bool = True,
    timeout: float = CONTROL_COMMAND_TIMEOUT_SECONDS,
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
        timeout=timeout,
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
    if not REVISION_RE.fullmatch(revision):
        raise ValueError(f"{label} revision contains unsupported characters")
    return _sha(
        _git(
            repo,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        ).stdout,
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
    if number < 1:
        raise ValueError("pull-request number must be a positive integer")
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
    for name in list(environment):
        if (
            name in BLOCKED_PROVIDER_ENV_VARS
            or name in PROVIDER_EXECUTABLE_ENV_VARS.values()
            or name.startswith(PROVIDER_ENV_PREFIXES)
        ):
            environment.pop(name, None)
    environment["GH_PROMPT_DISABLED"] = "1"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _codex_auth_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (Path.home() / ".codex").resolve()
    )


def _preflight_environment() -> dict[str, str]:
    environment = sanitized_environment()
    if os.environ.get("CODEX_HOME"):
        environment["CODEX_HOME"] = str(_codex_auth_home())
    return environment


def _isolated_codex_environment(
    provider_root: Path,
    runtime: ProviderRuntime,
    repo: Path,
) -> dict[str, str]:
    """Bridge only Codex auth into clean runtime state with no ambient skills."""
    source_auth = _codex_auth_home() / "auth.json"
    if not source_auth.is_file():
        raise ValueError(
            "Codex ChatGPT authentication is not available as CODEX_HOME/auth.json; "
            "run `codex login` with file-backed credential storage and retry"
        )
    codex_home = provider_root / "codex-home"
    user_home = provider_root / "user-home"
    config_home = provider_root / "config"
    data_home = provider_root / "data"
    for directory in (codex_home, user_home, config_home, data_home):
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        auth_payload = json.loads(source_auth.read_text(encoding="utf-8"))
        tokens = auth_payload["tokens"]
        access_token = tokens["access_token"]
        account_id = tokens["account_id"]
        id_token = tokens["id_token"]
        if not all(
            isinstance(value, str) and value
            for value in (access_token, account_id, id_token)
        ):
            raise TypeError("incomplete ChatGPT token bundle")
        token_payload = access_token.split(".")[1]
        token_payload += "=" * (-len(token_payload) % 4)
        expires_at = float(
            json.loads(base64.urlsafe_b64decode(token_payload))["exp"]
        )
    except (
        binascii.Error,
        KeyError,
        IndexError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise ValueError(
            "Codex auth.json does not contain a readable ChatGPT access token"
        ) from exc
    required_lifetime = MODEL_TIMEOUT_SECONDS + 5 * 60
    if auth_payload.get("auth_mode") != "chatgpt" or expires_at <= (
        time.time() + required_lifetime
    ):
        raise ValueError(
            "the current Codex ChatGPT access token cannot cover the full review "
            "window; refresh the subscription login with `codex login` and retry"
        )
    isolated_auth = codex_home / "auth.json"
    isolated_auth.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": None,
                "auth_mode": "chatgpt",
                "last_refresh": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "tokens": {
                    "access_token": access_token,
                    "account_id": account_id,
                    "id_token": id_token,
                    "refresh_token": "",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    isolated_auth.chmod(0o600)
    environment = sanitized_environment()
    environment.update(
        {
            "APPDATA": str(config_home),
            "CODEX_HOME": str(codex_home),
            "HOME": str(user_home),
            "LOCALAPPDATA": str(data_home),
            "USERPROFILE": str(user_home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
        }
    )
    status = _run(
        [runtime.codex_executable, "login", "status"],
        cwd=repo,
        environment=environment,
        check=False,
        timeout=PREFLIGHT_TIMEOUT_SECONDS,
    )
    if (
        status.returncode != 0
        or "Logged in using ChatGPT" not in f"{status.stdout}\n{status.stderr}"
    ):
        detail = (status.stderr or status.stdout).strip()
        raise ValueError(
            "the isolated Codex reviewer could not reuse the verified ChatGPT login: "
            f"{detail[:500] or 'no diagnostic output'}"
        )
    if expires_at <= time.time() + required_lifetime:
        raise ValueError(
            "the current Codex ChatGPT access token cannot cover the full review "
            "window; refresh the subscription login with `codex login` and retry"
        )
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


def _codex_config_arguments() -> list[str]:
    """Return the one strict, tool-isolated Codex invocation contract."""
    values = [
        f'model_reasoning_effort="{CODEX_EFFORT}"',
        *CODEX_PERMISSION_CONFIG,
        'web_search="disabled"',
        "tools.web_search=false",
        "skills.bundled.enabled=false",
    ]
    arguments = [item for value in values for item in ("--config", value)]
    for feature in CODEX_DISABLED_FEATURES:
        arguments.extend(["--disable", feature])
    return arguments


def _codex_exec_command(
    executable: str,
    workspace: Path,
    output_schema: Path,
    prompt_argument: str,
) -> list[str]:
    """Build the one Codex exec contract used by preflight and live lanes."""
    return [
        executable,
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--cd",
        str(workspace),
        "--model",
        CODEX_MODEL,
        *_codex_config_arguments(),
        "--output-schema",
        str(output_schema),
        "--color",
        "never",
        prompt_argument,
    ]


def _require_codex_invocation_capability(
    executable: str,
    version: str,
    repo: Path,
    environment: dict[str, str],
) -> None:
    """Fail before inference unless the selected binary accepts our contract.

    An intentionally invalid output schema makes the exact ``codex exec``
    process stop locally after strict configuration parsing and before a model
    request. The sentinel diagnostic proves that every preceding flag and
    override was accepted; any other result fails closed.
    """
    with tempfile.TemporaryDirectory(prefix="latch-review-codex-probe-") as value:
        probe_root = Path(value)
        probe_home = probe_root / "codex-home"
        probe_home.mkdir(mode=0o700)
        invalid_schema = probe_root / "invalid-output-schema.json"
        invalid_schema.write_text("{", encoding="utf-8")
        probe_environment = dict(environment)
        probe_environment["CODEX_HOME"] = str(probe_home)
        command = _codex_exec_command(
            executable,
            probe_root,
            invalid_schema,
            "Latch review invocation compatibility probe",
        )
        result = _run(
            command,
            cwd=repo,
            environment=probe_environment,
            check=False,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
        detail = f"{result.stdout}\n{result.stderr}"
        expected = f"Output schema file {invalid_schema} is not valid JSON"
        if result.returncode == 0 or expected not in detail:
            diagnostic = (result.stderr or result.stdout).strip()
            raise ValueError(
                f"Codex executable {executable} ({version}) rejected the strict "
                "local-review isolation contract before inference: "
                f"{diagnostic[:500] or 'no diagnostic output'}"
            )


def preflight_auth(repo: Path, *, require_gh: bool = True) -> ProviderRuntime:
    present = [name for name in BLOCKED_PROVIDER_ENV_VARS if os.environ.get(name)]
    if present:
        raise ValueError(
            "refusing to start while provider API-key/alternate-token auth or "
            "endpoint override environment variables are "
            f"set: {', '.join(present)}. Unset them and retry."
        )
    claude_executable = _resolve_provider_executable("claude")
    codex_executable = _resolve_provider_executable("codex")
    required_executables = ("git", "gh") if require_gh else ("git",)
    for executable in required_executables:
        if shutil.which(executable) is None:
            raise ValueError(f"required executable is not installed: {executable}")
    environment = _preflight_environment()
    claude_version = _provider_version(claude_executable, repo, environment)
    codex_version = _provider_version(codex_executable, repo, environment)
    _require_codex_model_capability(
        codex_executable, codex_version, repo, environment
    )
    _require_codex_invocation_capability(
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
            "Claude Code did not expose a claude.ai subscription login. If this "
            "runner was started by Codex Desktop, rerun it outside the filesystem "
            "sandbox before concluding that Claude is logged out; the sandbox "
            "cannot read Claude's saved login. Otherwise run `claude auth login`, "
            "and ensure ANTHROPIC_API_KEY is unset."
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
            _bare_git(
                workspace,
                target,
                "fetch",
                "--no-tags",
                str(repo),
                commit,
                timeout=FETCH_TIMEOUT_SECONDS,
            )
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


def _panel(workspace: Path, *args: str) -> None:
    _run(
        [sys.executable, str(PANEL_SCRIPT), *args],
        cwd=workspace,
        environment=sanitized_environment(),
        timeout=PANEL_TIMEOUT_SECONDS,
    )


def _build_lanes(
    workspace: Path,
    scope: ReviewScope,
    raw_root: Path,
) -> tuple[list[Lane], list[tuple[str, str]], bool, list[str], int]:
    lanes: list[Lane] = []
    prompt_root = workspace / "prompts"
    manifest_path = workspace / "prompt-manifest.json"
    _panel(
        workspace,
        "prepare-prompts",
        "--base-sha",
        scope.base_sha,
        "--head-sha",
        scope.head_sha,
        "--review-directory",
        "review-target",
        "--output-dir",
        str(prompt_root),
        "--manifest",
        str(manifest_path),
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("version") != 1
        or manifest.get("base_sha") != scope.base_sha
        or manifest.get("head_sha") != scope.head_sha
    ):
        raise ValueError("prompt manifest does not match the immutable review scope")
    manifest_lanes = manifest.get("lanes")
    if not isinstance(manifest_lanes, list) or not manifest_lanes:
        raise ValueError("review policy selected no applicable lanes")
    for config in manifest_lanes:
        if not isinstance(config, dict):
            raise ValueError("prompt manifest lane is malformed")
        provider = str(config.get("provider") or "")
        lane = str(config.get("lane") or "")
        filename = str(config.get("prompt") or "")
        expected = f"{provider}-{lane}.md"
        if provider not in {"claude", "codex"} or filename != expected:
            raise ValueError("prompt manifest lane is unsafe")
        prompt = prompt_root / filename
        if not prompt.is_file():
            raise ValueError(f"prompt manifest output is missing: {filename}")
        raw_dir = raw_root / f"{provider}-{lane}"
        raw_dir.mkdir(parents=True)
        lanes.append(
            Lane(provider, lane, prompt, raw_dir / "result.json", raw_dir)
        )
    skipped_rows = manifest.get("skipped")
    if not isinstance(skipped_rows, list):
        raise ValueError("prompt manifest skipped-lane list is malformed")
    skipped = [
        (str(row.get("provider") or ""), str(row.get("lane") or ""))
        for row in skipped_rows
        if isinstance(row, dict)
    ]
    if len(skipped) != len(skipped_rows):
        raise ValueError("prompt manifest skipped lane is malformed")
    runtime_required = manifest.get("runtime_evidence_required")
    if not isinstance(runtime_required, list) or not all(
        isinstance(item, str) for item in runtime_required
    ):
        raise ValueError("prompt manifest runtime-evidence list is malformed")
    artifact_needed = manifest.get("artifact_review_needed")
    if not isinstance(artifact_needed, bool):
        raise ValueError("prompt manifest artifact classification is malformed")
    path_coverage_gap_count = manifest.get(
        "path_classification_coverage_gap_count"
    )
    if (
        not isinstance(path_coverage_gap_count, int)
        or isinstance(path_coverage_gap_count, bool)
        or path_coverage_gap_count < 0
    ):
        raise ValueError("prompt manifest path coverage-gap count is malformed")
    return (
        lanes,
        skipped,
        artifact_needed,
        runtime_required,
        path_coverage_gap_count,
    )


def _provider_command(
    lane: Lane,
    workspace: Path,
    runtime: ProviderRuntime,
) -> list[str]:
    provider_schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    for field in TRUSTED_RECEIPT_FIELDS:
        provider_schema["properties"].pop(field, None)
        if field in provider_schema["required"]:
            provider_schema["required"].remove(field)
    provider_schema_path = workspace / "provider-review.schema.json"
    schema_body = json.dumps(provider_schema, indent=2, sort_keys=True) + "\n"
    temporary_schema = workspace / (
        f".provider-review.schema.{threading.get_ident()}.{secrets.token_hex(4)}"
    )
    temporary_schema.write_text(
        schema_body,
        encoding="utf-8",
    )
    os.replace(temporary_schema, provider_schema_path)
    if lane.provider == "claude":
        schema = json.dumps(
            provider_schema,
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
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {}}, separators=(",", ":")),
            "--tools",
            "",
        ]
    return _codex_exec_command(
        runtime.codex_executable,
        workspace,
        provider_schema_path,
        "-",
    )


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


def _invoke_lane(
    lane: Lane,
    workspace: Path,
    runtime: ProviderRuntime,
    environment: dict[str, str],
) -> LaneResult:
    stdout_path, stderr_path = lane.raw_dir / "stdout.txt", lane.raw_dir / "stderr.txt"
    try:
        execution = _execute_bounded(
            _provider_command(lane, workspace, runtime),
            cwd=workspace,
            environment=environment,
            timeout=MODEL_TIMEOUT_SECONDS,
            output_limit=MAX_MODEL_OUTPUT_BYTES,
            input_path=lane.prompt,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            stream_label="provider",
            cancellation_event=_PROVIDER_CANCELLATION,
        )
        if execution.timed_out:
            return LaneResult(
                lane.provider, lane.lane, False, lane.result, "timed out"
            )
        if execution.failures:
            return LaneResult(
                lane.provider,
                lane.lane,
                False,
                lane.result,
                "; ".join(execution.failures),
            )
        if execution.returncode != 0:
            detail = _read_limited_text(stderr_path).strip()
            return LaneResult(
                lane.provider,
                lane.lane,
                False,
                lane.result,
                f"provider exited {execution.returncode}: {detail[:1000]}",
            )
        if lane.provider == "claude":
            _extract_claude(stdout_path, lane.result)
        else:
            shutil.copyfile(stdout_path, lane.result)
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
    runtime_evidence_required: list[str],
    path_coverage_gap_count: int,
    output: Path,
) -> dict[str, Any]:
    report, summary = output / "report.md", output / "summary.json"
    command = [
        "aggregate",
        "--input-dir",
        str(receipts),
        "--base-sha",
        scope.base_sha,
        "--head-sha",
        scope.head_sha,
        "--artifact-review-needed",
        "true" if artifact_needed else "false",
        "--path-classification-coverage-gap-count",
        str(path_coverage_gap_count),
        "--output-report",
        str(report),
        "--output-summary",
        str(summary),
    ]
    for requirement_id in runtime_evidence_required:
        command.extend(["--runtime-evidence-required", requirement_id])
    _panel(workspace, *command)
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
    require_gh = bool(
        args.pr is not None
        or args.post_pr
        or (not args.range and not args.commit)
    )
    runtime = preflight_auth(repo, require_gh=require_gh)
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
    with (
        tempfile.TemporaryDirectory(prefix="latch-review-") as temporary,
        tempfile.TemporaryDirectory(prefix="latch-review-provider-") as provider_temporary,
    ):
        workspace = Path(temporary)
        provider_root = Path(provider_temporary)
        scope = resolve_scope(args, repo, workspace)
        output = _output_dir(repo, scope.head_sha)
        raw_root, receipts = output / "raw", output / "receipts"
        raw_root.mkdir()
        receipts.mkdir()
        prepared_pr_store = workspace / "review-target"
        if not prepared_pr_store.is_dir():
            _prepare_object_store(repo, workspace, scope)
        (
            lanes,
            skipped,
            artifact_needed,
            runtime_evidence_required,
            path_coverage_gap_count,
        ) = _build_lanes(workspace, scope, raw_root)
        # Validate and bridge the short-lived Codex access token only after all
        # potentially slow fetch/evidence work.  The lifetime guarantee must
        # cover lane execution, not repository preparation.
        provider_environments = {
            "claude": sanitized_environment(),
            "codex": _isolated_codex_environment(provider_root, runtime, repo),
        }
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
        if not lanes:
            raise ValueError("review policy selected no applicable lanes")
        _PROVIDER_CANCELLATION.clear()
        try:
            with ThreadPoolExecutor(max_workers=len(lanes)) as executor:
                futures = {}
                try:
                    futures = {
                        executor.submit(
                            _invoke_lane,
                            lane,
                            workspace,
                            runtime,
                            provider_environments[lane.provider],
                        ): lane
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
                except BaseException:
                    _cancel_provider_executions()
                    for future in futures:
                        future.cancel()
                    raise
        finally:
            _PROVIDER_CANCELLATION.clear()
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
        summary = _aggregate(
            workspace,
            receipts,
            scope,
            artifact_needed,
            runtime_evidence_required,
            path_coverage_gap_count,
            output,
        )

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

    review_should_fail = bool(summary.get("should_fail"))
    review_exit_code = 1 if review_should_fail else 0
    process_exit_code = 3 if post_error else review_exit_code
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
                "isolation_capability_source": (
                    "strict_config_invalid_schema_probe"
                ),
                "model": CODEX_MODEL,
                "reasoning_effort": CODEX_EFFORT,
            },
        },
        "billing_guard": {
            "provider_api_key_environment": "absent",
            "account_credit_settings": "not_verifiable_by_cli",
        },
        "artifact_review_needed": artifact_needed,
        "path_classification_coverage_gap_count": path_coverage_gap_count,
        "runtime_evidence_required": runtime_evidence_required,
        "review_should_fail": review_should_fail,
        "review_exit_code": review_exit_code,
        "posted_to_pr": posted_to_pr,
        "post_error": post_error or None,
        "process_exit_code": process_exit_code,
    }
    (output / "metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(report.read_text(encoding="utf-8"))
    print(f"Local review saved to {output}", file=sys.stderr)
    if post_error:
        print(f"PR posting failed: {post_error}", file=sys.stderr)
    return process_exit_code


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
