"""Force isolated KB and intensity-setting resolution for tests.

Tests build isolated KBs via ``db.connect(tempfile.mkdtemp())`` and rely on the
working directory selecting the DB. A machine-level pin (``kb_location.json`` /
``LATCH_KB_DIR`` / ``CLAUDE_KB_DIR``; KB id=1556) overrides that, so without this every
``db.connect`` would return the one real KB and tests would run against live
data. Importing this module neutralizes the pin for the process; it never reads
or writes the on-disk pin.

The install-wide ``latch_settings.json`` is similarly redirected to a missing
file in a temporary directory. ``LATCH_INTENSITY=full`` is also forced for the
test process so child hooks inherit the same isolation; Python monkeypatches do
not cross a subprocess boundary. Tests for other tiers override it explicitly.
Together these keep a developer's dogfooding tier from changing legacy tests
that intentionally exercise the shipped Full behavior.

``conftest.py`` imports this module so the whole pytest session is hermetic.
**pytest is the supported runner.** A DIRECT run (``python tests/test_x.py``)
on a configured install is only hermetic if that script ``import _isolation``
itself.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import paths  # noqa: E402

os.environ.pop("LATCH_KB_DIR", None)
os.environ.pop("CLAUDE_KB_DIR", None)
paths._PINNED_DIR = None
os.environ["LATCH_INTENSITY"] = paths.LEGACY_LATCH_INTENSITY

# Keep the TemporaryDirectory owner alive for the life of the process. The
# settings file itself deliberately does not exist unless a test writes it.
_SETTINGS_DIR = tempfile.TemporaryDirectory(prefix="latch_test_settings_")
paths.LATCH_SETTINGS_FILE = Path(_SETTINGS_DIR.name) / "latch_settings.json"
