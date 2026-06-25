"""Force legacy (per-cwd) KB resolution for tests.

Tests build isolated KBs via ``db.connect(tempfile.mkdtemp())`` and rely on the
working directory selecting the DB. A machine-level pin (``kb_location.json`` /
``LATCH_KB_DIR`` / ``CLAUDE_KB_DIR``; KB id=1556) overrides that, so without this every
``db.connect`` would return the one real KB and tests would run against live
data. Importing this module neutralizes the pin for the process; it never reads
or writes the on-disk pin.

``conftest.py`` imports it so the whole pytest session is hermetic. **pytest is
the supported runner.** A DIRECT run (``python tests/test_x.py``) on a pinned
install is only hermetic if that script ``import _isolation`` itself.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import paths  # noqa: E402

os.environ.pop("LATCH_KB_DIR", None)
os.environ.pop("CLAUDE_KB_DIR", None)
paths._PINNED_DIR = None
