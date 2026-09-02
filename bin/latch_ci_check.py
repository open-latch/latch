#!/usr/bin/env python3
"""Receipt-only local/self-hosted PR/CI policy check."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from latch.enforcement.ci import reference_main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(reference_main())
