"""pytest session setup: make the suite hermetic on a pinned KB install.

**pytest is the supported test runner for latch.** This conftest forces legacy
(per-cwd) KB resolution for the whole session (via ``_isolation``) so each test's
``db.connect(tempfile.mkdtemp())`` gets an isolated DB instead of the one real
pinned KB (``kb_location.json`` / ``LATCH_KB_DIR`` / ``CLAUDE_KB_DIR``; KB
id=1556). It never reads or writes the on-disk pin.

Directly-executed test scripts (``python tests/test_x.py``) on a pinned machine
are NOT hermetic unless that script ``import _isolation`` itself — run via pytest.
"""
import sys
from pathlib import Path

# tests/ on sys.path so the shared _isolation shim imports under pytest too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolation  # noqa: F401,E402  (import side-effect: neutralizes the pin)
