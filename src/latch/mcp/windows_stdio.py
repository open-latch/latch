"""Recover redirected standard streams for a Windows ``pythonw`` process.

``pythonw.exe`` avoids a foreground console window, but CPython may leave
``sys.stdin``, ``sys.stdout``, and ``sys.stderr`` as ``None`` for that GUI
subsystem executable.  MCP hosts still provide real pipe handles through the
Windows process standard-handle table.  Bind duplicates of those handles to
Python file objects before the stdio proxy starts.
"""
from __future__ import annotations

import io
import os
import sys
from typing import TextIO


_STD_INPUT_HANDLE = -10
_STD_OUTPUT_HANDLE = -11
_STD_ERROR_HANDLE = -12
_DUPLICATE_SAME_ACCESS = 0x00000002


def _stream_from_standard_handle(handle_id: int, mode: str) -> TextIO:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.DuplicateHandle.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.DuplicateHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.GetStdHandle(handle_id & 0xFFFFFFFF)
    invalid = wintypes.HANDLE(-1).value
    if handle in (None, 0, invalid):
        raise OSError(f"Windows standard handle {handle_id} is unavailable")

    process = kernel32.GetCurrentProcess()
    duplicate = wintypes.HANDLE()
    if not kernel32.DuplicateHandle(
        process,
        handle,
        process,
        ctypes.byref(duplicate),
        0,
        False,
        _DUPLICATE_SAME_ACCESS,
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    flags = (os.O_RDONLY if mode == "r" else os.O_WRONLY) | os.O_BINARY
    try:
        fd = msvcrt.open_osfhandle(int(duplicate.value), flags)
    except Exception:
        kernel32.CloseHandle(duplicate)
        raise

    raw = os.fdopen(fd, mode + "b", buffering=0)
    return io.TextIOWrapper(
        raw,
        encoding="utf-8",
        errors="surrogateescape",
        newline=None,
        line_buffering=mode == "w",
        write_through=mode == "w",
    )


def ensure_windows_standard_streams() -> None:
    """Restore any missing Python standard streams from Windows pipe handles."""
    if os.name != "nt":
        return

    specs = (
        ("stdin", "__stdin__", _STD_INPUT_HANDLE, "r"),
        ("stdout", "__stdout__", _STD_OUTPUT_HANDLE, "w"),
        ("stderr", "__stderr__", _STD_ERROR_HANDLE, "w"),
    )
    for public_name, original_name, handle_id, mode in specs:
        stream = getattr(sys, public_name)
        if stream is None:
            stream = _stream_from_standard_handle(handle_id, mode)
            setattr(sys, public_name, stream)
        if getattr(sys, original_name) is None:
            setattr(sys, original_name, stream)
