#!/usr/bin/env python3
"""Receipt-only executable for the shared A4 policy-check core."""
from __future__ import annotations

from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from latch.enforcement.core import reference_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(reference_main())
