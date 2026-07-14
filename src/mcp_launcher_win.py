"""Windowless Windows launcher for the latch MCP server.

Rationale
---------
Cursor on Windows launches the ``mcp.json`` command with an inherited
stdin/stdout/stderr pipe trio.  Two naive options both fail:

* ``python.exe`` directly -> a **console window** appears for the server's
  whole lifetime.
* ``pythonw.exe`` running the server in-process -> **no window**, but CPython
  leaves ``sys.stdin/out/err`` as ``None`` and the recovered-handle proxy did
  not deliver tool responses back to Cursor reliably (empty-payload failure).

This launcher takes the proven Windows pattern instead:

* it is itself launched via ``pythonw.exe`` (so *it* is windowless);
* it hands the **real inherited std handles** straight to a normal
  ``python.exe`` child started with ``CREATE_NO_WINDOW`` (also windowless);
* the child runs ``mcp_server.py`` with genuine OS pipes as its std streams —
  no in-process handle recovery, no re-wrapping;
* the child is owned by a Job Object with
  ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` so it (and anything it spawns that
  does not break away) is reaped the instant Cursor closes or kills this
  launcher — no orphaned proxy/daemon processes;
* the child's exit code is propagated.

Only used on Windows; other platforms never launch this module.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000

_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_DUPLICATE_SAME_ACCESS = 0x00000002

# Job object
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# Keep the job handle alive for this process's lifetime so KILL_ON_JOB_CLOSE
# only fires when the launcher itself dies.
_JOB_HANDLE = None


def _diag(msg: str) -> None:
    """Best-effort file log (stderr may be ``None`` under pythonw)."""
    try:
        path = Path(__file__).resolve().parent.parent / "mcp_launcher_win.log"
        from datetime import datetime, timezone
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat(timespec='milliseconds')}] "
                    f"pid={os.getpid()} {msg}\n")
    except Exception:
        pass


def _dup_std_fd(handle_id: int, oflag: int) -> int:
    """Duplicate one process standard handle and expose it as an OS file
    descriptor owned by this process. The duplicate is inheritable so the
    child can receive it as its own std handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.DuplicateHandle.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
    ]
    k32.DuplicateHandle.restype = wintypes.BOOL

    handle = k32.GetStdHandle(handle_id & 0xFFFFFFFF)
    invalid = wintypes.HANDLE(-1).value
    if handle in (None, 0, invalid):
        raise OSError(f"standard handle {handle_id} unavailable")
    proc = k32.GetCurrentProcess()
    dup = wintypes.HANDLE()
    if not k32.DuplicateHandle(proc, handle, proc, ctypes.byref(dup),
                               0, True, _DUPLICATE_SAME_ACCESS):
        raise ctypes.WinError(ctypes.get_last_error())
    return msvcrt.open_osfhandle(int(dup.value), oflag | os.O_BINARY)


def _create_kill_on_close_job():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(wintypes.ULONG)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    info = _EXT()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    if not k32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        err = ctypes.get_last_error()
        k32.CloseHandle(job)
        raise ctypes.WinError(err)
    return job


def _assign_to_job(job, process_handle) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    if not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process_handle))):
        raise ctypes.WinError(ctypes.get_last_error())


def _same_file(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _venv_launcher_python() -> str | None:
    """The venv's own ``python.exe`` (the redirector). Used only as the
    ``__PYVENV_LAUNCHER__`` marker so the base interpreter adopts venv
    site-packages — never launched directly (its re-exec drops
    CREATE_NO_WINDOW and spawns a console window)."""
    for name in ("LATCH_PYTHON", "CLAUDE_KB_PYTHON"):
        value = (os.environ.get(name) or "").strip()
        if value and Path(value).exists():
            return value
    candidate = Path(sys.prefix) / "Scripts" / "python.exe"
    return str(candidate) if candidate.exists() else None


def _resolve_child_python() -> str:
    """The **base** console ``python.exe`` (never the venv redirector).

    The stdlib venv ``python.exe``/``pythonw.exe`` on Windows re-exec the base
    interpreter as a child and drop ``CREATE_NO_WINDOW`` in the process, which
    is what puts a console window on screen. Launching the base interpreter
    directly (with ``__PYVENV_LAUNCHER__`` set by the caller for venv
    site-packages) keeps CREATE_NO_WINDOW effective — no window."""
    base = getattr(sys, "_base_executable", None) or ""
    if base:
        console = Path(base).with_name("python.exe")
        if console.exists():
            return str(console)
    base_console = Path(sys.base_prefix) / "python.exe"
    if base_console.exists():
        return str(base_console)
    # Last resort: the venv redirector (may flash a window, but still works).
    return _venv_launcher_python() or sys.executable


def main() -> int:
    global _JOB_HANDLE
    if os.name != "nt":
        # Never the launch path off Windows; exec the server directly.
        server = Path(__file__).resolve().parent / "mcp_server.py"
        os.execv(sys.executable, [sys.executable, str(server)])

    server_py = Path(__file__).resolve().parent / "mcp_server.py"
    child_python = _resolve_child_python()
    child_env = os.environ.copy()
    venv_marker = _venv_launcher_python()
    if venv_marker and Path(child_python).name.lower() == "python.exe" and \
            not _same_file(child_python, venv_marker):
        # Base interpreter direct-launch: adopt the venv via the same marker the
        # redirector would have set. Skipped when we fell back to the redirector.
        child_env["__PYVENV_LAUNCHER__"] = venv_marker

    stdin_fd = _dup_std_fd(_STD_INPUT_HANDLE, os.O_RDONLY)
    stdout_fd = _dup_std_fd(_STD_OUTPUT_HANDLE, os.O_WRONLY)
    stderr_fd = _dup_std_fd(_STD_ERROR_HANDLE, os.O_WRONLY)

    try:
        _JOB_HANDLE = _create_kill_on_close_job()
    except Exception as exc:  # a missing job still lets the server run
        _JOB_HANDLE = None
        _diag(f"job-create failed (continuing without reaper): {exc}")

    _diag(f"launching child={child_python} server={server_py}")
    proc = subprocess.Popen(
        [child_python, str(server_py)],
        stdin=stdin_fd,
        stdout=stdout_fd,
        stderr=stderr_fd,
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
        cwd=os.getcwd(),
        env=child_env,
    )
    # Parent no longer needs its copies of the inherited pipe fds.
    for fd in (stdin_fd, stdout_fd, stderr_fd):
        try:
            os.close(fd)
        except OSError:
            pass

    if _JOB_HANDLE is not None:
        try:
            _assign_to_job(_JOB_HANDLE, proc._handle)
            _diag(f"assigned child pid={proc.pid} to kill-on-close job")
        except Exception as exc:
            _diag(f"job-assign failed pid={proc.pid}: {exc}")

    code = proc.wait()
    _diag(f"child pid={proc.pid} exited code={code}")
    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
