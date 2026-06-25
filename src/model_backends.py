"""Small model-subprocess backend helpers used by maintenance paths.

The engine default remains Claude for existing Claude Code installs. Adapter
surfaces can opt into another backend by setting an explicit environment value
in their MCP process environment.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable


SUPPORTED_BACKENDS = {"claude", "codex"}

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


def first_env_value(names: Iterable[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def resolve_backend(
    name: str | None = None,
    *,
    env_names: Iterable[str] = (),
    default: str = "claude",
) -> str:
    raw = name or first_env_value(env_names) or default
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
) -> ModelCallResult:
    try:
        resolved = resolve_backend(backend, env_names=env_names, default=default)
    except ValueError as e:
        return ModelCallResult(None, str(e), False, str(backend or default))

    if resolved == "codex":
        return _invoke_codex(
            prompt,
            timeout_s=timeout_s,
            purpose=purpose,
            codex_bin=codex_bin,
            model=first_env_value(codex_model_env),
        )
    return _invoke_claude(
        prompt,
        timeout_s=timeout_s,
        purpose=purpose,
        claude_bin=claude_bin,
    )


def _invoke_claude(
    prompt: str,
    *,
    timeout_s: int,
    purpose: str,
    claude_bin: str | None = None,
) -> ModelCallResult:
    bin_path = claude_bin or CLAUDE_BIN
    env = os.environ.copy()
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
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
        return ModelCallResult(None, f"{purpose} timed out after {timeout_s}s", True, "claude")
    except FileNotFoundError as e:
        return ModelCallResult(None, f"subprocess failed: {type(e).__name__}: {e}", False, "claude")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return ModelCallResult(None, f"claude backend exit {proc.returncode}: {detail[:1000]}", False, "claude")
    return ModelCallResult(proc.stdout, None, False, "claude")


def _invoke_codex(
    prompt: str,
    *,
    timeout_s: int,
    purpose: str,
    codex_bin: str | None = None,
    model: str | None = None,
) -> ModelCallResult:
    bin_path = codex_bin or CODEX_BIN
    env = os.environ.copy()
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
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
    except subprocess.TimeoutExpired:
        return ModelCallResult(None, f"{purpose} timed out after {timeout_s}s", True, "codex")
    except FileNotFoundError as e:
        return ModelCallResult(None, f"subprocess failed: {type(e).__name__}: {e}", False, "codex")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return ModelCallResult(None, f"codex backend exit {proc.returncode}: {detail[-1000:]}", False, "codex")
    if not final_text.strip():
        return ModelCallResult(None, "codex backend returned empty final message", False, "codex")
    return ModelCallResult(final_text, None, False, "codex")
