"""Simulated Windows 8.3 short-name resolution for path-spelling regressions.

On windows-latest ``tempfile`` hands out ``C:\\Users\\RUNNER~1\\...`` while
``Path.resolve()`` (``ntpath.realpath``) returns ``C:\\Users\\runneradmin\\...``.
The CLI mains resolve ``--project``; hooks and Cursor keep the raw spelling.
"""
from __future__ import annotations

import contextlib
import ntpath

from latch.store import paths

# A made-up profile, so the real runner's RUNNER~1 paths still resolve for real.
SHORT_PROFILE = "C:\\Users\\LATCHT~1"
LONG_PROFILE = "C:\\Users\\latchtester"


@contextlib.contextmanager
def windows_short_name_host():
    """Run as a Windows host whose realpath expands ``SHORT_PROFILE``.

    Only two things are simulated: ``paths._WINDOWS_HOST`` is forced on, and
    ``ntpath.realpath`` maps ``SHORT_PROFILE`` to ``LONG_PROFILE`` the way
    Windows expands an 8.3 name. Every other path reaches the real
    ``ntpath.realpath`` (``Path.resolve()`` uses it on Windows).
    """
    real_realpath = ntpath.realpath

    def realpath(path, **kwargs):
        normalized = ntpath.normpath(path)
        if normalized.startswith(SHORT_PROFILE + "\\"):
            return LONG_PROFILE + normalized[len(SHORT_PROFILE):]
        return real_realpath(path, **kwargs)

    previous_host = paths._WINDOWS_HOST
    paths._WINDOWS_HOST = True
    ntpath.realpath = realpath
    try:
        yield
    finally:
        ntpath.realpath = real_realpath
        paths._WINDOWS_HOST = previous_host
