"""Windows twins for the backend tests' ``#!/usr/bin/env bash`` CLI fakes.

The model-backend tests fake the claude, codex and Cursor agent CLIs as
shebang text files. Windows CreateProcess cannot run those (OSError
[WinError 193] %1 is not a valid Win32 application), so on Windows each fake
is a small Python program behind a ``.cmd`` shim instead, which is the shape of
a real npm-installed ``claude.cmd`` / ``codex.cmd``. The product passes
prompts on stdin and only flags on argv, so the shim forwards argv with
``%*``. The shim runs this interpreter by absolute path and never bare
``bash``, which can resolve to the WSL stub on Windows.

Each twin is written against the helpers in ``_PRELUDE``, one per bash idiom
the fakes use, so a twin reads line for line like the bash body it replaces.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PRELUDE = r'''import os
import sys

ARGS = sys.argv[1:]


def record_args(env_name, mode="w"):
    # printf '%s\n' "$@" > "$ENV_NAME"   (mode "a" for >>)
    with open(os.environ[env_name], mode, encoding="utf-8") as f:
        f.write("".join(arg + "\n" for arg in ARGS))


def drain_stdin():
    # cat >/dev/null
    sys.stdin.buffer.read()


def save_stdin(env_name):
    # cat > "$ENV_NAME"
    with open(os.environ[env_name], "wb") as f:
        f.write(sys.stdin.buffer.read())


def emit(text, stream=sys.stdout):
    # printf '%s\n' "$TEXT"   (stream=sys.stderr for >&2)
    stream.buffer.write((text + "\n").encode("utf-8"))


def write_last_message(text):
    # out=<value after the last --output-last-message>; printf '%s\n' "$TEXT" > "$out"
    out = ""
    for i, arg in enumerate(ARGS[:-1]):
        if arg == "--output-last-message":
            out = ARGS[i + 1]
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text + "\n")


'''


def write_cmd_fake(path: Path, source: str) -> Path:
    """Write ``<path>.py`` (prelude + *source*) and a ``<path>.cmd`` shim that
    runs it with this interpreter. Returns the shim, to be used as the *_BIN."""
    script = path.with_name(path.name + ".py")
    script.write_text(_PRELUDE + source, encoding="utf-8")
    shim = path.with_name(path.name + ".cmd")
    exe = sys.executable.replace("%", "%%")
    shim.write_bytes(f'@"{exe}" "%~dp0{script.name}" %*\r\n'.encode("utf-8"))
    return shim
