#!/usr/bin/env python3
"""Codex preview wiring verifier.

This is intentionally separate from ``doctor.py``. The Claude doctor verifies
Claude Code's production wiring; this script verifies the Codex adapter surfaces:
Codex config.toml, AGENTS.md, static MCP launch target, and Codex compact
transcript resolution, plus the current compactor summarizer backend.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import agents_md_sync
import codex_hooks
import compactor
import codex_transcript
import install_codex
import install_engine

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"


@dataclass(frozen=True)
class Check:
    name: str
    level: str
    detail: str


def _exists_or_on_path(command: str) -> bool:
    p = Path(command)
    if p.is_absolute() or os.sep in command or (os.altsep and os.altsep in command):
        return p.exists()
    return shutil.which(command) is not None


def check_codex_config(config_path: Path, python_path: str, server_py: str) -> Check:
    ok, detail = install_codex.config_status(config_path, python_path, server_py)
    return Check("Codex config.toml MCP block", OK if ok else FAIL, detail)


def check_agents_md(agents_path: Path) -> Check:
    status = agents_md_sync.evaluate(agents_path)
    if status == agents_md_sync.OK:
        return Check("AGENTS.md managed region", OK, f"{agents_path} is up to date")
    return Check(
        "AGENTS.md managed region",
        FAIL,
        f"{agents_path} status is {status}; run bin/install_codex.sh --yes",
    )


def check_codex_hooks(hooks_path: Path, config_path: Path, python_path: str, hook_py: str) -> list[Check]:
    ok, detail = codex_hooks.hooks_status(hooks_path, python_path, hook_py)
    checks = [Check("Codex SessionStart hook", OK if ok else FAIL, detail)]
    _trust_ok, trust_detail = codex_hooks.trust_state_hint(config_path, hooks_path)
    checks.append(Check("Codex hook trust state", WARN, trust_detail))
    return checks


def check_mcp_launch_target(python_path: str, server_py: str) -> Check:
    missing: list[str] = []
    if not _exists_or_on_path(python_path):
        missing.append(f"interpreter not found: {python_path}")
    if not Path(server_py).exists():
        missing.append(f"server script not found: {server_py}")
    if missing:
        return Check("Codex MCP launch target", FAIL, "; ".join(missing))
    return Check("Codex MCP launch target", OK, f"{python_path} -> {server_py}")


def check_compact_resolution(session_id: str | None, *, require: bool = False) -> Check:
    try:
        sid = codex_transcript.resolve_session_id(session_id)
    except codex_transcript.CodexTranscriptError as e:
        level = FAIL if require else WARN
        return Check(
            "Codex compact transcript",
            level,
            f"{e}; pass --session-id or run inside Codex with CODEX_THREAD_ID",
        )
    try:
        transcript = codex_transcript.find_transcript(sid)
    except codex_transcript.CodexTranscriptError as e:
        return Check("Codex compact transcript", FAIL, str(e))
    return Check("Codex compact transcript", OK, f"{sid} -> {transcript}")


def _summarizer_error_excerpt(stdout: str, stderr: str) -> str:
    raw = stdout.strip() or stderr.strip()
    if not raw:
        return "no output"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:400]

    parts: list[str] = []
    for key in ("api_error_status", "type", "subtype", "is_error"):
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    result = payload.get("result") or payload.get("error")
    if result:
        parts.append(str(result))
    return "; ".join(parts) if parts else raw[:400]


def _default_summarizer_backend() -> str:
    return (
        os.environ.get("CODEX_KB_COMPACTOR_BACKEND")
        or os.environ.get("CLAUDE_KB_COMPACTOR_BACKEND")
        or os.environ.get("LATCH_COMPACTOR_BACKEND")
        or "codex"
    )


def check_summarizer_backend(
    *,
    backend: str | None = None,
    claude_bin: str | None = None,
    codex_bin: str | None = None,
    timeout_s: float = 60.0,
) -> Check:
    """Probe the current compactor summarizer backend with a tiny prompt."""

    try:
        backend_name = compactor._summarizer_backend(
            backend or _default_summarizer_backend(),
            default="codex",
        )
    except ValueError as e:
        return Check(
            "Codex compactor summarizer",
            FAIL,
            str(e),
        )

    prompt = 'Return only {"ok": true}.\n'
    if backend_name == "claude":
        resolved = claude_bin or os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
        if not _exists_or_on_path(resolved):
            return Check(
                "Codex compactor summarizer",
                FAIL,
                f"Claude CLI not found: {resolved}",
            )
        stdout, err = compactor._invoke_claude_once(
            prompt, claude_bin=resolved, timeout_s=timeout_s,
        )
        display = f"{resolved} -p"
    else:
        resolved = codex_bin or os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
        if not _exists_or_on_path(resolved):
            return Check(
                "Codex compactor summarizer",
                FAIL,
                f"Codex CLI not found: {resolved}",
            )
        stdout, err = compactor._invoke_codex_once(
            prompt, codex_bin=resolved, timeout_s=timeout_s,
        )
        display = f"{resolved} exec"

    if stdout is None:
        return Check(
            "Codex compactor summarizer",
            FAIL,
            f"{display} failed: {err}",
        )
    parsed, parse_err = compactor._parse_json_envelope(stdout)
    if parsed is None:
        excerpt = _summarizer_error_excerpt(stdout, "")
        return Check(
            "Codex compactor summarizer",
            FAIL,
            f"{display} returned unparsable JSON ({parse_err}): {excerpt}",
        )
    return Check("Codex compactor summarizer", OK, f"{display} reachable")


def run_all(
    *,
    config_path: Path,
    hooks_path: Path,
    agents_path: Path,
    python_path: str,
    server_py: str,
    hook_py: str,
    session_id: str | None,
    skip_agents: bool = False,
    skip_hooks: bool = False,
    skip_compact: bool = False,
    skip_summarizer: bool = False,
    summarizer_backend: str | None = None,
    require_compact: bool = False,
) -> list[Check]:
    checks = [
        check_codex_config(config_path, python_path, server_py),
        check_mcp_launch_target(python_path, server_py),
    ]
    if skip_hooks:
        checks.append(Check("Codex SessionStart hook", WARN, "skipped (--skip-hooks)"))
    else:
        checks.extend(check_codex_hooks(hooks_path, config_path, python_path, hook_py))
    if skip_agents:
        checks.append(Check("AGENTS.md managed region", WARN, "skipped (--skip-agents)"))
    else:
        checks.append(check_agents_md(agents_path))
    if skip_compact:
        checks.append(Check("Codex compact transcript", WARN, "skipped (--skip-compact)"))
    else:
        checks.append(check_compact_resolution(session_id, require=require_compact))
    if skip_summarizer:
        checks.append(Check("Codex compactor summarizer", WARN, "skipped (--skip-summarizer)"))
    else:
        checks.append(check_summarizer_backend(backend=summarizer_backend))
    return checks


def print_text(checks: list[Check]) -> None:
    print("\nlatch Codex doctor\n")
    for check in checks:
        print(f"  [{check.level:<4}] {check.name}: {check.detail}")
    failed = sum(1 for c in checks if c.level == FAIL)
    warned = sum(1 for c in checks if c.level == WARN)
    print()
    if failed:
        print(f"FAILED - {failed} check(s) need attention before Codex latch is solid.")
    elif warned:
        print(f"OK with {warned} warning(s).")
    else:
        print("OK - Codex preview wiring looks healthy.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Codex preview doctor")
    ap.add_argument("--python", help="interpreter registered for the MCP server")
    ap.add_argument("--config", default=str(install_codex.CONFIG_PATH),
                    help="Codex config.toml path (default: $CODEX_HOME/config.toml)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to check (default: ./AGENTS.md)")
    ap.add_argument("--hooks", default=str(install_codex.HOOKS_PATH),
                    help="Codex hooks.json path (default: $CODEX_HOME/hooks.json)")
    ap.add_argument("--session-id", default=None,
                    help="Codex session id for compact resolution (default: $CODEX_THREAD_ID)")
    ap.add_argument("--skip-agents", action="store_true",
                    help="skip AGENTS.md managed-region check")
    ap.add_argument("--skip-hooks", action="store_true",
                    help="skip Codex SessionStart hook check")
    ap.add_argument("--skip-compact", action="store_true",
                    help="skip Codex compact transcript resolution check")
    ap.add_argument("--skip-summarizer", action="store_true",
                    help="skip tiny summarizer backend probe")
    ap.add_argument("--summarizer", choices=sorted(compactor.SUPPORTED_SUMMARIZER_BACKENDS),
                    default=_default_summarizer_backend(),
                    help="summarizer backend to probe (default: codex)")
    ap.add_argument("--require-compact", action="store_true",
                    help="treat missing CODEX_THREAD_ID/session id as a failure")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((install_codex.KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
    hook_py = str((install_codex.KB_HOME / "src" / "hooks" / "codex_session_start.py")).replace("\\", "/")
    checks = run_all(
        config_path=Path(args.config),
        hooks_path=Path(args.hooks),
        agents_path=Path(args.agents_md),
        python_path=python_path,
        server_py=server_py,
        hook_py=hook_py,
        session_id=args.session_id,
        skip_agents=args.skip_agents,
        skip_hooks=args.skip_hooks,
        skip_compact=args.skip_compact,
        skip_summarizer=args.skip_summarizer,
        summarizer_backend=args.summarizer,
        require_compact=args.require_compact,
    )

    if args.json:
        print(json.dumps({
            "ok": all(c.level != FAIL for c in checks),
            "checks": [c.__dict__ for c in checks],
        }))
    else:
        print_text(checks)

    return 1 if any(c.level == FAIL for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
