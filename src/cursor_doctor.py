#!/usr/bin/env python3
"""Cursor preview wiring verifier."""
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
import cursor_rules_sync
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


def check_mcp_launch_target(python_path: str, server_py: str) -> Check:
    missing: list[str] = []
    if not _exists_or_on_path(python_path):
        missing.append(f"interpreter not found: {python_path}")
    if not Path(server_py).exists():
        missing.append(f"server script not found: {server_py}")
    if missing:
        return Check("Cursor MCP launch target", FAIL, "; ".join(missing))
    return Check("Cursor MCP launch target", OK, f"{python_path} -> {server_py}")


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

    tools = _run_agent_mcp(resolved, ["mcp", "list-tools", install_cursor.SERVER_NAME], timeout_s)
    if isinstance(tools, str):
        return Check("Cursor CLI MCP visibility", WARN, tools)
    if tools.returncode != 0:
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
        + ", ".join(CRITICAL_MCP_TOOLS),
    )


def run_all(
    *,
    config_path: Path,
    agents_path: Path,
    rules_path: Path,
    python_path: str,
    server_py: str,
    model_backend: str | None = None,
    skip_agents: bool = False,
    skip_rules: bool = False,
    skip_cli: bool = False,
    agent_bin: str | None = None,
    cli_timeout_s: float = 15.0,
) -> list[Check]:
    checks = [
        check_cursor_config(config_path, python_path, server_py, model_backend=model_backend),
        check_mcp_launch_target(python_path, server_py),
    ]
    if skip_agents:
        checks.append(Check("AGENTS.md managed region", WARN, "skipped (--skip-agents)"))
    else:
        checks.append(check_agents_md(agents_path))
    if skip_rules:
        checks.append(Check("Cursor .cursor/rules/latch.mdc rule", WARN, "skipped (--skip-rules)"))
    else:
        checks.append(check_cursor_rule(rules_path))
    if skip_cli:
        checks.append(Check("Cursor CLI MCP visibility", WARN, "skipped (--skip-cli)"))
    else:
        checks.append(check_cursor_cli_mcp(agent_bin=agent_bin, timeout_s=cli_timeout_s))
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
        print("OK - Cursor preview wiring looks healthy.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch Cursor preview doctor")
    ap.add_argument("--python", help="interpreter registered for the MCP server")
    ap.add_argument("--mcp-json", default=str(install_cursor.DEFAULT_MCP_PATH),
                    help="Cursor MCP config path (default: .cursor/mcp.json)")
    ap.add_argument("--agents-md", default="AGENTS.md",
                    help="AGENTS.md path to check (default: ./AGENTS.md)")
    ap.add_argument("--rules-mdc", default=str(install_cursor.DEFAULT_RULE_PATH),
                    help="Cursor rule path to check (default: .cursor/rules/latch.mdc)")
    ap.add_argument("--model-backend", choices=("claude", "codex"),
                    help="expected existing backend env in .cursor/mcp.json")
    ap.add_argument("--agent-bin", help="Cursor CLI executable for live MCP probe")
    ap.add_argument("--cli-timeout", type=float, default=15.0,
                    help="seconds to wait for each Cursor CLI MCP probe")
    ap.add_argument("--skip-agents", action="store_true",
                    help="skip AGENTS.md managed-region check")
    ap.add_argument("--skip-rules", action="store_true",
                    help="skip Cursor rule check")
    ap.add_argument("--skip-cli", action="store_true",
                    help="skip Cursor agent mcp list/list-tools probe")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    python_path = install_engine.resolve_python(args.python)
    server_py = str((install_cursor.KB_HOME / "src" / "mcp_server.py")).replace("\\", "/")
    checks = run_all(
        config_path=Path(args.mcp_json),
        agents_path=Path(args.agents_md),
        rules_path=Path(args.rules_mdc),
        python_path=python_path,
        server_py=server_py,
        model_backend=args.model_backend,
        skip_agents=args.skip_agents,
        skip_rules=args.skip_rules,
        skip_cli=args.skip_cli,
        agent_bin=args.agent_bin,
        cli_timeout_s=args.cli_timeout,
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
