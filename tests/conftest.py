"""pytest session setup: bind the suite to a disposable authenticated KB root.

**pytest is the supported test runner for latch.** This conftest creates a
capability-bound temporary root before any test module loads.  Every KB path,
including paths resolved by child processes, stays under that root.

Directly-executed test scripts are refused unless they explicitly load the same
bootstrap before importing runtime modules.
"""
import sys
from pathlib import Path

# tests/ on sys.path so the shared _isolation shim imports under pytest too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolation  # noqa: E402  (import side-effect: isolates configuration)
