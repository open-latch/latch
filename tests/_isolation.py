"""Create an authenticated disposable KB root for tests.

The root is capability-bound and inherited by subprocesses.  Production pins
are never consulted while it is active.  Direct test execution without this
bootstrap now fails closed in ``paths.project_dir``.

``conftest.py`` imports it before test modules, so pytest is the supported
runner.  A direct script is safe only if it imports this module before runtime
modules; otherwise it is deliberately refused.
"""
import hashlib
import json
import os
import secrets
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

_TEST_SANDBOX = tempfile.TemporaryDirectory(prefix="latch-pytest-")
_TEST_ROOT = Path(_TEST_SANDBOX.name).resolve()
_CAPABILITY = secrets.token_hex(32)
(_TEST_ROOT / ".latch-test-root.json").write_text(
    json.dumps({
        "format": 1,
        "root_uuid": str(uuid.uuid4()),
        "capability_sha256": hashlib.sha256(_CAPABILITY.encode("utf-8")).hexdigest(),
    }, sort_keys=True) + "\n",
    encoding="utf-8",
)

os.environ.pop("LATCH_KB_DIR", None)
os.environ.pop("CLAUDE_KB_DIR", None)
os.environ["LATCH_TEST_ROOT"] = str(_TEST_ROOT)
os.environ["LATCH_TEST_CAPABILITY"] = _CAPABILITY

import paths  # noqa: E402

paths._PINNED_DIR = None
