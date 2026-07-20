"""pytest session setup: isolate the suite from configured Latch installs.

**pytest is the supported test runner for latch.** This conftest forces legacy
(per-cwd) KB resolution for the whole session (via ``_isolation``) so each test's
``db.connect(tempfile.mkdtemp())`` gets an isolated DB instead of the one real
pinned KB (``kb_location.json`` / ``LATCH_KB_DIR`` / ``CLAUDE_KB_DIR``; KB
id=1556). It also redirects the install-wide intensity settings path and forces
the shipped Full tier in the inherited test environment, so a developer's real
Quiet/Standard/Full choice cannot retier either in-process or child-process
tests. It never reads or writes either on-disk configuration file.

Directly-executed test scripts (``python tests/test_x.py``) on a pinned machine
are NOT hermetic unless that script ``import _isolation`` itself — run via pytest.
"""
import os
import sys
from pathlib import Path

# tests/ on sys.path so the shared _isolation shim imports under pytest too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolation  # noqa: F401,E402  (import side-effect: isolates configuration)

# _isolation forces the shipped Full behavior and propagates it to child hooks.
# Tests that exercise another tier set it explicitly or pass an env mapping to
# the resolver.
assert os.environ["LATCH_INTENSITY"] == "full"
