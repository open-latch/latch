"""Simulated Windows path resolution for path-spelling regressions.

On windows-latest ``tempfile`` hands out ``C:\\Users\\RUNNER~1\\...`` while
``Path.resolve()`` (``ntpath.realpath``) returns ``C:\\Users\\runneradmin\\...``.
The CLI mains resolve ``--project``; hooks and Cursor keep the raw spelling.
Junctions, symlinks, subst and mapped drives resolve the same way.
"""
from __future__ import annotations

import contextlib
import ntpath

from latch.store import paths

# A made-up profile, so the real runner's RUNNER~1 paths still resolve for real.
SHORT_PROFILE = "C:\\Users\\LATCHT~1"
LONG_PROFILE = "C:\\Users\\latchtester"
# A made-up junction (or subst drive) and its target, for the same reason.
JUNCTION = "C:\\latch-junction\\alias"
JUNCTION_TARGET = "D:\\latch-target\\repo"


@contextlib.contextmanager
def windows_realpath_host(aliases: dict[str, str]):
    """Run as a Windows host whose realpath expands each alias prefix.

    Only two things are simulated: ``paths._WINDOWS_HOST`` is forced on, and
    ``ntpath.realpath`` maps a path at or below an alias to its target the way
    Windows expands an 8.3 name or follows a junction. Every other path
    reaches the real ``ntpath.realpath`` (``Path.resolve()`` uses it on
    Windows).
    """
    real_realpath = ntpath.realpath

    def realpath(path, **kwargs):
        normalized = ntpath.normpath(path)
        for alias, target in aliases.items():
            if normalized == alias or normalized.startswith(alias + "\\"):
                return target + normalized[len(alias):]
        return real_realpath(path, **kwargs)

    previous_host = paths._WINDOWS_HOST
    paths._WINDOWS_HOST = True
    ntpath.realpath = realpath
    try:
        yield
    finally:
        ntpath.realpath = real_realpath
        paths._WINDOWS_HOST = previous_host


def windows_short_name_host():
    """Run as a Windows host whose realpath expands ``SHORT_PROFILE``."""
    return windows_realpath_host({SHORT_PROFILE: LONG_PROFILE})
