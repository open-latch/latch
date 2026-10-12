"""Directory links that work on every CI leg.

On Windows ``Path.symlink_to`` needs ``target_is_directory=True`` for a
directory (without it the link is a file symlink that cannot be traversed) and
the symlink privilege, which a runner may not grant. A directory junction needs
no privilege and resolves the same way under ``os.path.realpath``, so it stands
in when the symlink is refused. Since Python 3.8 ``shutil.rmtree`` removes a
junction without deleting what it points at.
"""
from __future__ import annotations

import os
from pathlib import Path


def link_dir(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
