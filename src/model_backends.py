"""Small model-subprocess backend helpers used by maintenance paths.

The engine default remains Claude for existing Claude Code installs. Shared
connections carry validated backend/private child settings through
``mcp_runtime``; standalone and legacy callers retain process-env behavior.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable

import cursor_backend
import mcp_runtime


SUPPORTED_BACKENDS = {"claude", "codex", "cursor"}

# Maintenance is launched from the MCP server environment, sometimes long after
# the original user action. Prefer a maintenance-specific knob, then a generic
# model knob, then the gate knob already written by earlier Codex installs.
MAINTENANCE_BACKEND_ENV = (
    "LATCH_MAINTENANCE_BACKEND",
    "CLAUDE_KB_MAINTENANCE_BACKEND",
    "LATCH_MODEL_BACKEND",
    "LATCH_GATE_BACKEND",
    "CLAUDE_KB_GATE_BACKEND",
)

CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"

# CREATE_NO_WINDOW: don't flash a console window per CLI call when the parent
# has no console. 0 on POSIX.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass(frozen=True)
class ModelCallResult:
    text: str | None
    error: str | None
    timed_out: bool
    backend: str
    failure_kind: str | None = None
    terminal: bool = False


_AUTH_FAILURE_MARKERS = (
    "authenticate",
    "authentication required",
    "not authenticated",
    "not logged in",
    "login required",
    "oauth session expired",
    "oauth token expired",
    "invalid api key",
    "missing api key",
    "unauthorized",
)


def classify_failure(error: str | None, *, timed_out: bool = False) -> tuple[str, bool]:
    """Return a privacy-safe failure class and whether this run must stop.

    Authentication/configuration/executable failures happen before a useful
    model response can exist.  Retrying the same autonomous runner inside one
    fan-out only spends budget, so callers must circuit-break (or move to the
    next explicitly approved backend).  Timeouts and response failures remain
    ordinary non-terminal model failures.
    """
    if timed_out:
        return "timeout", False
    detail = str(error or "").lower()
    if "unsupported model backend" in detail:
        return "configuration", True
    if "filenotfounderror" in detail or "not found" in detail:
        return "missing_executable", True
    if any(marker in detail for marker in _AUTH_FAILURE_MARKERS):
        return "authentication", True
    return "backend_error", False


def _failure_result(
    error: str,
    *,
    backend: str,
    timed_out: bool = False,
) -> ModelCallResult:
    failure_kind, terminal = classify_failure(error, timed_out=timed_out)
    return ModelCallResult(
        None,
        error,
        timed_out,
        backend,
        failure_kind=failure_kind,
        terminal=terminal,
    )


def first_env_value(names: Iterable[str]) -> str | None:
    for name in names:
        value = mcp_runtime.connection_env_value(name)
        if value:
            return value
    return None


def resolve_backend(
    name: str | None = None,
    *,
    env_names: Iterable[str] = (),
    default: str = "claude",
) -> str:
    connection = mcp_runtime.current_connection()
    raw = (
        name
        or (connection.maintenance_backend if connection is not None else None)
        or first_env_value(env_names)
        or default
    )
    backend = str(raw).strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise ValueError(f"unsupported model backend {raw!r}; expected one of: {supported}")
    return backend


def invoke_prompt(
    prompt: str,
    *,
    backend: str | None = None,
    env_names: Iterable[str] = (),
    default: str = "claude",
    timeout_s: int,
    purpose: str = "model",
    claude_bin: str | None = None,
    codex_bin: str | None = None,
    codex_model_env: Iterable[str] = (),
    cursor_bin: str | None = None,
    cursor_model_env: Iterable[str] = (),
    subprocess_env: dict[str, str] | None = None,
) -> ModelCallResult:
    try:
        resolved = resolve_backend(backend, env_names=env_names, default=default)
    except ValueError as e:
        return _failure_result(str(e), backend=str(backend or default))

    if resolved == "codex":
        return _invoke_codex(
            prompt,
            timeout_s=timeout_s,
            purpose=purpose,
            codex_bin=codex_bin,
            model=first_env_value(codex_model_env),
            subprocess_env=subprocess_env,
        )
    if resolved == "cursor":
        cursor_kwargs = {
            "timeout_s": timeout_s,
            "purpose": purpose,
            "agent_bin": cursor_bin,
            "model": first_env_value(cursor_model_env),
        }
        if subprocess_env is not None:
            cursor_kwargs["subprocess_env"] = subprocess_env
        text, error, timed_out = cursor_backend.invoke_prompt(prompt, **cursor_kwargs)
        if error is not None or text is None:
            return _failure_result(
                error or "cursor backend returned no result",
                backend="cursor",
                timed_out=timed_out,
            )
        return ModelCallResult(text, None, False, "cursor")
    return _invoke_claude(
        prompt,
        timeout_s=timeout_s,
        purpose=purpose,
        claude_bin=claude_bin,
        subprocess_env=subprocess_env,
    )


def _invoke_claude(
    prompt: str,
    *,
    timeout_s: int,
    purpose: str,
    claude_bin: str | None = None,
    subprocess_env: dict[str, str] | None = None,
) -> ModelCallResult:
    env = (
        dict(subprocess_env)
        if subprocess_env is not None
        else mcp_runtime.connection_subprocess_environment("claude")
    )
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        bin_path = claude_bin or mcp_runtime.connection_binary(
            "CLAUDE_BIN", process_default=CLAUDE_BIN
        )
        proc = subprocess.run(
            [bin_path, "-p", "--no-session-persistence", "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return _failure_result(
            f"{purpose} timed out after {timeout_s}s",
            backend="claude",
            timed_out=True,
        )
    except OSError as e:
        return _failure_result(
            f"subprocess failed: {type(e).__name__}: {e}",
            backend="claude",
        )

    if proc.returncode != 0:
        detail = mcp_runtime.redact_subprocess_output(
            (proc.stderr or proc.stdout or "").strip()
        )
        return _failure_result(
            f"claude backend exit {proc.returncode}: {detail[:1000]}",
            backend="claude",
        )
    return ModelCallResult(
        mcp_runtime.redact_subprocess_output(proc.stdout),
        None,
        False,
        "claude",
    )


def _invoke_codex(
    prompt: str,
    *,
    timeout_s: int,
    purpose: str,
    codex_bin: str | None = None,
    model: str | None = None,
    subprocess_env: dict[str, str] | None = None,
) -> ModelCallResult:
    env = (
        dict(subprocess_env)
        if subprocess_env is not None
        else mcp_runtime.connection_subprocess_environment("codex")
    )
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        bin_path = codex_bin or mcp_runtime.connection_binary(
            "CODEX_BIN", process_default=CODEX_BIN
        )
        with tempfile.TemporaryDirectory(prefix="latch-codex-model-") as tmp:
            out_path = Path(tmp) / "last_message.txt"
            args = [
                bin_path,
                "exec",
                "--ignore-user-config",
                "--ignore-rules",
                "--cd", tmp,
                "--skip-git-repo-check",
                "--ephemeral",
                "--sandbox", "read-only",
                "--output-last-message", str(out_path),
            ]
            if model:
                args.extend(["--model", model])
            args.append("-")
            proc = subprocess.run(
                args,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_s,
                env=env,
                creationflags=CREATE_NO_WINDOW,
            )
            final_text = ""
            if out_path.exists():
                final_text = out_path.read_text(encoding="utf-8", errors="replace")
            if not final_text.strip():
                final_text = proc.stdout
            final_text = mcp_runtime.redact_subprocess_output(final_text)
    except subprocess.TimeoutExpired:
        return _failure_result(
            f"{purpose} timed out after {timeout_s}s",
            backend="codex",
            timed_out=True,
        )
    except OSError as e:
        return _failure_result(
            f"subprocess failed: {type(e).__name__}: {e}",
            backend="codex",
        )

    if proc.returncode != 0:
        detail = mcp_runtime.redact_subprocess_output(
            (proc.stderr or proc.stdout or "").strip()
        )
        return _failure_result(
            f"codex backend exit {proc.returncode}: {detail[-1000:]}",
            backend="codex",
        )
    if not final_text.strip():
        return _failure_result(
            "codex backend returned empty final message",
            backend="codex",
        )
    return ModelCallResult(final_text, None, False, "codex")
