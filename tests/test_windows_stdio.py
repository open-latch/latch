"""Windows subprocess coverage for the windowless MCP stdio bootstrap."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parent.parent / "src"


def test_non_windows_bootstrap_is_noop():
    if os.name == "nt":
        pytest.skip("non-Windows contract")
    sys.path.insert(0, str(SRC))
    try:
        from windows_stdio import ensure_windows_standard_streams

        before = (sys.stdin, sys.stdout, sys.stderr)
        ensure_windows_standard_streams()
        assert (sys.stdin, sys.stdout, sys.stderr) == before
    finally:
        sys.path.remove(str(SRC))


@pytest.mark.skipif(os.name != "nt", reason="requires Windows standard handles")
def test_pythonw_recovers_redirected_pipe_streams():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.is_file(), pythonw
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(SRC)!r}); "
        "sys.stdin = sys.stdout = sys.stderr = None; "
        "sys.__stdin__ = sys.__stdout__ = sys.__stderr__ = None; "
        "from windows_stdio import ensure_windows_standard_streams; "
        "ensure_windows_standard_streams(); "
        "data = sys.stdin.buffer.readline(); "
        "sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'err\\n'); sys.stderr.buffer.flush()"
    )
    result = subprocess.run(
        [str(pythonw), "-c", code],
        input=b"mcp-pipe-roundtrip\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result
    assert result.stdout == b"mcp-pipe-roundtrip\n"
    assert result.stderr == b"err\n"
