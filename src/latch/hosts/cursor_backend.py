"""Read-only Cursor Agent CLI model backend.

Cursor exposes a stable single-object JSON result in headless print mode. The
backend runs in an empty temporary workspace, uses Ask mode, never passes
``--force``, and feeds prompts on stdin so compaction payloads do not hit
platform command-line limits.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from latch.mcp import mcp_runtime


CURSOR_AGENT_BIN = (
    os.environ.get("CURSOR_AGENT_BIN")
    or shutil.which("agent")
    or shutil.which("cursor-agent")
    or "agent"
)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def invoke_prompt(
    prompt: str,
    *,
    timeout_s: float,
    purpose: str,
    agent_bin: str | None = None,
    model: str | None = None,
) -> tuple[str | None, str | None, bool]:
    """Return ``(final_text, error, timed_out)`` from Cursor headless mode."""
    model = (
        model
        or mcp_runtime.connection_env_value("LATCH_CURSOR_MODEL")
        or mcp_runtime.connection_env_value("CURSOR_MODEL")
    )
    env = mcp_runtime.connection_subprocess_environment("cursor")
    env["CLAUDE_KB_IN_COMPACT"] = "1"
    try:
        resolved = agent_bin or mcp_runtime.connection_binary(
            "CURSOR_AGENT_BIN", process_default=CURSOR_AGENT_BIN
        )
        with tempfile.TemporaryDirectory(prefix="latch-cursor-model-") as tmp:
            args = [
                resolved,
                "--print",
                "--output-format", "json",
                "--mode", "ask",
                "--trust",
                "--workspace", tmp,
            ]
            if model:
                args.extend(["--model", model])
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
    except subprocess.TimeoutExpired:
        return None, f"{purpose} timed out after {timeout_s:g}s", True
    except (FileNotFoundError, OSError) as e:
        return None, f"subprocess failed: {type(e).__name__}: {e}", False

    if proc.returncode != 0:
        detail = mcp_runtime.redact_subprocess_output(
            (proc.stderr or proc.stdout or "").strip()
        )
        return None, f"cursor backend exit {proc.returncode}: {detail[-1000:]}", False
    output = mcp_runtime.redact_subprocess_output((proc.stdout or "").strip())
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as e:
        excerpt = mcp_runtime.redact_subprocess_output(
            (proc.stdout or proc.stderr or "").strip()
        )[:500]
        return None, f"cursor backend returned invalid JSON ({e}): {excerpt}", False
    if not isinstance(payload, dict):
        return None, "cursor backend JSON result was not an object", False
    if payload.get("type") != "result" or payload.get("subtype") != "success":
        return None, f"cursor backend returned non-success result: {payload}", False
    if payload.get("is_error") is True:
        return None, f"cursor backend reported an error: {payload.get('result')}", False
    final_text = payload.get("result")
    if not isinstance(final_text, str) or not final_text.strip():
        return None, "cursor backend returned an empty final result", False
    return final_text, None, False
