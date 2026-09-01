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

# One Latch-owned Claude default for every background purpose. Purpose-specific
# selectors are prepended by callers; these two are the shared fallback chain.
CLAUDE_MODEL_ENV = (
    "LATCH_MAINTENANCE_CLAUDE_MODEL",
    "LATCH_CLAUDE_MODEL",
)
DEFAULT_CLAUDE_MODEL = "sonnet"

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
    model: str | None = None


def first_env_value(names: Iterable[str]) -> str | None:
    for name in names:
        value = mcp_runtime.connection_env_value(name)
        if value:
            return value
    return None


def resolve_claude_model(env_names: Iterable[str] = ()) -> str:
    """Resolve a Claude model without falling through to the host CLI default."""
    raw = first_env_value((*tuple(env_names), *CLAUDE_MODEL_ENV))
    model = str(raw if raw is not None else DEFAULT_CLAUDE_MODEL).strip()
    if not model:
        raise ValueError("Claude model resolved to an empty value")
    return model


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
    claude_model_env: Iterable[str] = (),
    codex_bin: str | None = None,
    codex_model_env: Iterable[str] = (),
    cursor_bin: str | None = None,
    cursor_model_env: Iterable[str] = (),
) -> ModelCallResult:
    try:
        resolved = resolve_backend(backend, env_names=env_names, default=default)
    except ValueError as e:
        return ModelCallResult(None, str(e), False, str(backend or default))

    if resolved == "codex":
        model = first_env_value(codex_model_env)
        return _invoke_codex(
            prompt,
            timeout_s=timeout_s,
            purpose=purpose,
            codex_bin=codex_bin,
            model=model,
        )
    if resolved == "cursor":
        model = first_env_value(cursor_model_env)
        text, error, timed_out = cursor_backend.invoke_prompt(
            prompt,
            timeout_s=timeout_s,
            purpose=purpose,
            agent_bin=cursor_bin,
            model=model,
        )
        return ModelCallResult(text, error, timed_out, "cursor", model)
    try:
        model = resolve_claude_model(claude_model_env)
    except ValueError as e:
        return ModelCallResult(None, str(e), False, "claude", None)
    return _invoke_claude(
        prompt,
        timeout_s=timeout_s,
        purpose=purpose,
        claude_bin=claude_bin,
        model=model,
    )


def _invoke_claude(
    prompt: str,
    *,
    timeout_s: int,
    purpose: str,
    claude_bin: str | None = None,
    model: str | None = None,
) -> ModelCallResult:
    env = mcp_runtime.connection_subprocess_environment("claude")
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        resolved_model = model or resolve_claude_model()
    except ValueError as e:
        return ModelCallResult(None, str(e), False, "claude", None)
    try:
        bin_path = claude_bin or mcp_runtime.connection_binary(
            "CLAUDE_BIN", process_default=CLAUDE_BIN
        )
        proc = subprocess.run(
            [
                bin_path,
                "-p",
                "--no-session-persistence",
                "--output-format",
                "json",
                "--model",
                resolved_model,
            ],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
            env=env,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return ModelCallResult(None, f"{purpose} timed out after {timeout_s}s", True, "claude", resolved_model)
    except OSError as e:
        return ModelCallResult(None, f"subprocess failed: {type(e).__name__}: {e}", False, "claude", resolved_model)

    if proc.returncode != 0:
        detail = mcp_runtime.redact_subprocess_output(
            (proc.stderr or proc.stdout or "").strip()
        )
        return ModelCallResult(None, f"claude backend exit {proc.returncode}: {detail[:1000]}", False, "claude", resolved_model)
    return ModelCallResult(
        mcp_runtime.redact_subprocess_output(proc.stdout),
        None,
        False,
        "claude",
        resolved_model,
    )


def _invoke_codex(
    prompt: str,
    *,
    timeout_s: int,
    purpose: str,
    codex_bin: str | None = None,
    model: str | None = None,
) -> ModelCallResult:
    env = mcp_runtime.connection_subprocess_environment("codex")
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
        return ModelCallResult(None, f"{purpose} timed out after {timeout_s}s", True, "codex", model)
    except OSError as e:
        return ModelCallResult(None, f"subprocess failed: {type(e).__name__}: {e}", False, "codex", model)

    if proc.returncode != 0:
        detail = mcp_runtime.redact_subprocess_output(
            (proc.stderr or proc.stdout or "").strip()
        )
        return ModelCallResult(None, f"codex backend exit {proc.returncode}: {detail[-1000:]}", False, "codex", model)
    if not final_text.strip():
        return ModelCallResult(None, "codex backend returned empty final message", False, "codex", model)
    return ModelCallResult(final_text, None, False, "codex", model)
