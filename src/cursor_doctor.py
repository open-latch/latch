#!/usr/bin/env python3
"""Cursor wiring verifier."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import agents_md_sync
import cursor_backend
import cursor_hooks
import cursor_rules_sync
import cursor_transcript
import install_cursor
import install_engine

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"
CRITICAL_MCP_TOOLS = ("latch_search", "latch_get", "latch_recent", "latch_gate")


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


def check_cursor_config(
    config_path: Path,
    python_path: str,
    server_py: str,
    *,
    model_backend: str | None = None,
) -> Check:
    try:
        ok, detail = install_cursor.mcp_status(
            config_path,
            python_path,
            server_py,
            model_backend=model_backend,
        )
    except SystemExit as e:
        detail = str(e.code) if e.code is not None else "invalid Cursor MCP config"
        return Check("Cursor .cursor/mcp.json MCP server", FAIL, detail)
    return Check("Cursor .cursor/mcp.json MCP server", OK if ok else FAIL, detail)


def check_agents_md(agents_path: Path) -> Check:
    status = agents_md_sync.evaluate(agents_path)
    if status == agents_md_sync.OK:
        return Check("AGENTS.md managed region", OK, f"{agents_path} is up to date")
    return Check(
        "AGENTS.md managed region",
        FAIL,
        f"{agents_path} status is {status}; run bin/install_cursor.sh --yes",
    )


def check_cursor_rule(rules_path: Path) -> Check:
    status = cursor_rules_sync.evaluate(rules_path)
    if status == cursor_rules_sync.OK:
        return Check("Cursor .cursor/rules/latch.mdc rule", OK, f"{rules_path} is up to date")
    return Check(
        "Cursor .cursor/rules/latch.mdc rule",
        FAIL,
        f"{rules_path} status is {status}; run bin/install_cursor.sh --yes",
    )


def check_cursor_commands(commands_dir: Path, *, model_backend: str | None = None) -> Check:
    ok, detail = install_cursor.cursor_commands_status(
        commands_dir, model_backend=model_backend,
    )
    return Check("Cursor .cursor/commands latch commands", OK if ok else FAIL, detail)


def check_cursor_skills(skills_dir: Path, *, model_backend: str | None = None) -> Check:
    ok, detail = install_cursor.cursor_skills_status(
        skills_dir, model_backend=model_backend,
    )
    return Check("Cursor .cursor/skills latch skills", OK if ok else FAIL, detail)


def check_cursor_hooks(
    hooks_path: Path,
    python_path: str,
    session_start_py: str,
    before_submit_py: str,
    pre_tool_use_py: str,
    post_tool_use_py: str,
) -> Check:
    ok, detail = cursor_hooks.hooks_status(
        hooks_path, python_path, session_start_py, before_submit_py,
        pre_tool_use_py, post_tool_use_py
    )
    return Check("Cursor .cursor/hooks.json session/gate/activity hooks", OK if ok else FAIL, detail)


def check_mcp_launch_target(python_path: str, server_py: str) -> Check:
    ok, detail = install_cursor.cursor_mcp_launch_assets_status(
        python_path, server_py,
    )
    return Check("Cursor MCP launch target", OK if ok else FAIL, detail)


def _output_excerpt(proc: subprocess.CompletedProcess[str]) -> str:
    raw = (proc.stderr or proc.stdout or "").strip()
    return raw[:500] if raw else "no output"


def _run_agent_mcp(agent_bin: str, args: list[str], timeout_s: float) -> subprocess.CompletedProcess[str] | str:
    try:
        return subprocess.run(
            [agent_bin, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return f"{agent_bin} {' '.join(args)} timed out after {timeout_s:g}s"
    except FileNotFoundError as e:
        return f"subprocess failed: {type(e).__name__}: {e}"


def _missing_critical_tools(output: str) -> list[str]:
    return [tool for tool in CRITICAL_MCP_TOOLS if tool not in output]


def _needs_mcp_approval(output: str) -> bool:
    text = (output or "").lower()
    return "needs approval" in text or "not been approved" in text or "not approved" in text


def check_cursor_cli_mcp(
    *,
    agent_bin: str | None = None,
    timeout_s: float = 15.0,
) -> Check:
    resolved = agent_bin or os.environ.get("CURSOR_AGENT_BIN") or shutil.which("agent") or "agent"
    if not _exists_or_on_path(resolved):
        return Check(
            "Cursor CLI MCP visibility",
            WARN,
            f"Cursor agent CLI not found: {resolved}; skipped agent mcp list/list-tools probe",
        )

    listed = _run_agent_mcp(resolved, ["mcp", "list"], timeout_s)
    if isinstance(listed, str):
        return Check("Cursor CLI MCP visibility", WARN, listed)
    if listed.returncode != 0:
        return Check(
            "Cursor CLI MCP visibility",
            WARN,
            f"{resolved} mcp list exit {listed.returncode}: {_output_excerpt(listed)}",
        )
    listed_output = (listed.stdout or "") + "\n" + (listed.stderr or "")
    if _needs_mcp_approval(listed_output):
        return Check(
            "Cursor CLI MCP visibility",
            WARN,
            "latch is statically configured but still needs separate user-controlled "
            f"MCP approval in Cursor: {_output_excerpt(listed)}",
        )

    tools = _run_agent_mcp(resolved, ["mcp", "list-tools", install_cursor.SERVER_NAME], timeout_s)
    if isinstance(tools, str):
        return Check("Cursor CLI MCP visibility", WARN, tools)
    if tools.returncode != 0:
        excerpt = _output_excerpt(tools)
        if _needs_mcp_approval(excerpt):
            return Check(
                "Cursor CLI MCP visibility",
                WARN,
                "latch is statically configured but still needs separate user-controlled "
                f"MCP approval in Cursor: {excerpt}",
            )
        return Check(
            "Cursor CLI MCP visibility",
            WARN,
            f"{resolved} mcp list-tools {install_cursor.SERVER_NAME} exit {tools.returncode}: {_output_excerpt(tools)}",
        )
    tool_output = (tools.stdout or "") + "\n" + (tools.stderr or "")
    missing = _missing_critical_tools(tool_output)
    if missing:
        return Check(
            "Cursor CLI MCP visibility",
            FAIL,
            f"{resolved} mcp list-tools {install_cursor.SERVER_NAME} missing critical tool(s): "
            + ", ".join(missing),
        )

    return Check(
        "Cursor CLI MCP visibility",
        OK,
        f"{resolved} mcp list/list-tools {install_cursor.SERVER_NAME} reachable with "
        + ", ".join(CRITICAL_MCP_TOOLS)
        + "; CLI-only proof: separately open Cursor Settings > Tools & MCP, "
        "select this workspace, enable latch, and confirm latch reports tools "
        "enabled (the exact count can grow with the MCP surface)",
    )


def check_cursor_model_backend(
    *,
    backend: str | None,
    agent_bin: str | None = None,
    timeout_s: float = 60.0,
) -> Check:
    backend_name = backend or "cursor"
    if backend_name != "cursor":
        return Check(
            "Cursor native model backend",
            WARN,
            f"compatibility backend {backend_name} selected; native Cursor probe skipped",
        )
    resolved = (
        agent_bin or os.environ.get("CURSOR_AGENT_BIN")
        or shutil.which("agent") or shutil.which("cursor-agent") or "agent"
    )
    if not _exists_or_on_path(resolved):
        return Check("Cursor native model backend", FAIL, f"Cursor Agent CLI not found: {resolved}")
    text, error, _timed_out = cursor_backend.invoke_prompt(
        'Return only {"ok": true}.',
        timeout_s=timeout_s,
        purpose="Cursor backend probe",
        agent_bin=resolved,
    )
    if text is None:
        detail = error or "Cursor backend probe failed"
        if "authentication required" in detail.lower() or "not logged in" in detail.lower():
            detail = (
                "Cursor Agent login is required for live native-backend acceptance; "
                "static wiring alone is not sufficient: " + detail
            )
        return Check("Cursor native model backend", FAIL, detail)
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as e:
        return Check(
            "Cursor native model backend", FAIL,
            f"Cursor Agent CLI returned non-JSON probe text ({e}): {text[:300]}",
        )
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return Check("Cursor native model backend", FAIL, f"unexpected probe result: {payload}")
    return Check("Cursor native model backend", OK, f"{resolved} --print JSON Ask mode reachable")


def check_cursor_compact_resolution(
    *,
    project_path: Path,
    session_id: str | None = None,
    transcript_path: str | None = None,
    require: bool = False,
) -> Check:
    try:
        sid, transcript = cursor_transcript.resolve_current(
            str(project_path), session_id=session_id, transcript_path=transcript_path,
        )
    except cursor_transcript.CursorTranscriptError as e:
        return Check("Cursor current-session compact", FAIL if require else WARN, str(e))
    return Check("Cursor current-session compact", OK, f"{sid} -> {transcript}")


def run_all(
    *,
    config_path: Path,
    agents_path: Path,
    rules_path: Path,
    commands_dir: Path,
    skills_dir: Path = install_cursor.DEFAULT_SKILLS_DIR,
    python_path: str,
    server_py: str,
    model_backend: str | None = None,
    skip_agents: bool = False,
    skip_rules: bool = False,
    skip_commands: bool = False,
    skip_skills: bool = False,
    skip_cli: bool = False,
    agent_bin: str | None = None,
    cli_timeout_s: float = 15.0,
    backend_timeout_s: float = 60.0,
    with_hooks: bool = False,
    hooks_path: Path = install_cursor.DEFAULT_HOOKS_PATH,
    project_path: Path = Path("."),
    session_id: str | None = None,
    transcript_path: str | None = None,
    skip_backend: bool = False,
    skip_compact: bool = False,
    require_compact: bool = False,
) -> list[Check]:
    checks = [
        check_cursor_config(config_path, python_path, server_py, model_backend=model_backend),
        check_mcp_launch_target(python_path, server_py),
    ]
    compact_assets_ok, compact_assets_detail = install_cursor.cursor_compact_assets_status()
    checks.append(Check(
        "Cursor native backend/compact assets",
        OK if compact_assets_ok else FAIL,
        compact_assets_detail,
    ))
    plugin_ok, plugin_detail = install_cursor.cursor_plugin_status()
    checks.append(Check(
        "Cursor plugin/skill distribution assets",
        OK if plugin_ok else FAIL,
        plugin_detail,
    ))
    if skip_agents:
        checks.append(Check("AGENTS.md managed region", WARN, "skipped (--skip-agents)"))
    else:
        checks.append(check_agents_md(agents_path))
    if skip_rules:
        checks.append(Check("Cursor .cursor/rules/latch.mdc rule", WARN, "skipped (--skip-rules)"))
    else:
        checks.append(check_cursor_rule(rules_path))
    if skip_commands:
        checks.append(Check("Cursor .cursor/commands latch commands", WARN, "skipped (--skip-commands)"))
    else:
        checks.append(check_cursor_commands(commands_dir, model_backend=model_backend))
    if skip_skills:
        checks.append(Check("Cursor .cursor/skills latch skills", WARN, "skipped (--skip-skills)"))
    else:
        checks.append(check_cursor_skills(skills_dir, model_backend=model_backend))
    if with_hooks:
        checks.append(check_cursor_hooks(
            hooks_path,
            python_path,
            str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_session_start.py"),
            str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_before_submit.py"),
            str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_pre_tool_use.py"),
            str(install_cursor.KB_HOME / "src" / "hooks" / "cursor_post_tool_use.py"),
        ))
    if skip_cli:
        checks.append(Check("Cursor CLI MCP visibility", WARN, "skipped (--skip-cli)"))
    else:
        checks.append(check_cursor_cli_mcp(agent_bin=agent_bin, timeout_s=cli_timeout_s))
    if skip_backend:
        checks.append(Check("Cursor native model backend", WARN, "skipped (--skip-backend)"))
    else:
        checks.append(check_cursor_model_backend(
            backend=model_backend, agent_bin=agent_bin, timeout_s=backend_timeout_s,
        ))
    if skip_compact:
        checks.append(Check("Cursor current-session compact", WARN, "skipped (--skip-compact)"))
    else:
        checks.append(check_cursor_compact_resolution(
            project_path=project_path,
            session_id=session_id,
            transcript_path=transcript_path,
            require=require_compact,
        ))
    return checks


def print_text(checks: list[Check]) -> None:
    print("\nlatch Cursor doctor\n")
    for check in checks:
        print(f"  [{check.level:<4}] {check.name}: {check.detail}")
    failed = sum(1 for c in checks if c.level == FAIL)
    warned = sum(1 for c in checks if c.level == WARN)
    print()
    if failed:
        print(f"FAILED - {failed} check(s) need attention before Cursor latch is solid.")
    elif warned:
        print(f"OK with {warned} warning(s).")
    else:
        print("OK - Cursor wiring looks healthy.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Cursor doctor")
    ap.add_argument("--python", help="interpreter registered for the MCP server")
    ap.add_argument("--mcp-json", default=str(install_cursor.DEFAULT_MCP_PATH),
                    help="Cursor MCP config path (default: .cursor/mcp.json)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to check (default: ./AGENTS.md)")
    ap.add_argument("--rules-mdc", default=str(install_cursor.DEFAULT_RULE_PATH),
                    help="Cursor rule path to check (default: .cursor/rules/latch.mdc)")
    ap.add_argument("--commands-dir", default=str(install_cursor.DEFAULT_COMMANDS_DIR),
                    help="Cursor commands directory to check (default: .cursor/commands)")
    ap.add_argument("--skills-dir", default=str(install_cursor.DEFAULT_SKILLS_DIR),
                    help="Cursor skills directory to check (default: .cursor/skills)")
    ap.add_argument("--hooks-json", default=str(install_cursor.DEFAULT_HOOKS_PATH),
                    help="Cursor hooks path to check (default: .cursor/hooks.json)")
    ap.add_argument("--with-hooks", action="store_true",
                    help="require opt-in Cursor session/gate/activity hooks")
    ap.add_argument("--model-backend", choices=("cursor", "claude", "codex"),
                    help="expected existing backend env in .cursor/mcp.json")
    ap.add_argument("--project", default=os.getcwd(),
                    help="project path for current-session compact resolution")
    ap.add_argument("--session-id",
                    help="optional Cursor session id; must match SessionStart marker")
    ap.add_argument("--transcript",
                    help="optional Cursor transcript path; must match SessionStart marker")
    ap.add_argument("--agent-bin", help="Cursor CLI executable for live MCP probe")
    ap.add_argument("--cli-timeout", type=float, default=15.0,
                    help="seconds to wait for each Cursor CLI MCP probe")
    ap.add_argument("--backend-timeout", type=float, default=60.0,
                    help="seconds to wait for the native Cursor backend probe")
    ap.add_argument("--skip-agents", action="store_true",
                    help="skip AGENTS.md managed-region check")
    ap.add_argument("--skip-rules", action="store_true",
                    help="skip Cursor rule check")
    ap.add_argument("--skip-commands", action="store_true",
                    help="skip Cursor commands check")
    ap.add_argument("--skip-skills", action="store_true",
                    help="skip Cursor skills check")
    ap.add_argument("--skip-cli", action="store_true",
                    help="skip Cursor agent mcp list/list-tools probe")
    ap.add_argument("--skip-backend", action="store_true",
                    help="skip native Cursor Agent CLI JSON probe")
    ap.add_argument("--skip-compact", action="store_true",
                    help="skip current-session transcript marker check")
    ap.add_argument("--require-compact", action="store_true",
                    help="treat missing current Cursor transcript marker as a failure")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((install_cursor.KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
    checks = run_all(
        config_path=Path(args.mcp_json),
        agents_path=Path(args.agents_md),
        rules_path=Path(args.rules_mdc),
        commands_dir=Path(args.commands_dir),
        skills_dir=Path(args.skills_dir),
        python_path=python_path,
        server_py=server_py,
        model_backend=args.model_backend,
        skip_agents=args.skip_agents,
        skip_rules=args.skip_rules,
        skip_commands=args.skip_commands,
        skip_skills=args.skip_skills,
        skip_cli=args.skip_cli,
        agent_bin=args.agent_bin,
        cli_timeout_s=args.cli_timeout,
        backend_timeout_s=args.backend_timeout,
        with_hooks=args.with_hooks,
        hooks_path=Path(args.hooks_json),
        project_path=Path(args.project).expanduser().resolve(),
        session_id=args.session_id,
        transcript_path=args.transcript,
        skip_backend=args.skip_backend,
        skip_compact=args.skip_compact,
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
