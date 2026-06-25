"""Tee-only wrapper for the latch MCP server.

Claude Code spawns the MCP server over stdio and silently swallows its stderr,
so pre-warm exceptions / import failures / FastMCP server-start messages never reach
disk. This wrapper preserves stdin/stdout (MCP protocol passes through
unchanged) but mirrors the child's stderr to a per-machine log file so failures
leave a fingerprint.

Resolves the install root via ${LATCH_HOME} / ${CLAUDE_KB_HOME}, falling back to
the parent of this file's directory. Writes mcp_stderr.log inside that root.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

KB_HOME = Path(os.environ.get("LATCH_HOME") or os.environ.get("CLAUDE_KB_HOME") or Path(__file__).resolve().parent.parent)
LOG_PATH = KB_HOME / "mcp_stderr.log"
MCP_SCRIPT = str(KB_HOME / "src" / "mcp_server.py")


def _slug_for_cwd() -> str:
    cwd = os.getcwd()
    return (
        cwd.replace(":", "-")
        .replace("\\", "-")
        .replace("/", "-")
        .lstrip("-")
        .lower()
    )


def _tee(pipe, log_file, tag: str) -> None:
    for raw in iter(pipe.readline, b""):
        ts = datetime.now(timezone.utc).isoformat()
        try:
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            log_file.write(f"[{ts}] [{tag}] {text}\n".encode("utf-8"))
            log_file.flush()
        except Exception:
            pass
        try:
            sys.stderr.buffer.write(raw)
            sys.stderr.buffer.flush()
        except Exception:
            pass


def main() -> int:
    tag = _slug_for_cwd()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, MCP_SCRIPT],
        stdin=sys.stdin,
        stdout=sys.stdout,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    with open(LOG_PATH, "ab") as log_file:
        ts = datetime.now(timezone.utc).isoformat()
        log_file.write(
            f"[{ts}] [{tag}] === wrapper start pid={os.getpid()} child_pid={proc.pid} python={sys.executable} ===\n".encode(
                "utf-8"
            )
        )
        log_file.flush()

        t = threading.Thread(target=_tee, args=(proc.stderr, log_file, tag), daemon=True)
        t.start()

        rc = proc.wait()
        t.join(timeout=2.0)

        ts = datetime.now(timezone.utc).isoformat()
        log_file.write(
            f"[{ts}] [{tag}] === wrapper exit rc={rc} ===\n".encode("utf-8")
        )
        log_file.flush()

    return rc


if __name__ == "__main__":
    sys.exit(main())
