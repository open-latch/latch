#!/usr/bin/env python3
"""Fail-closed replacement for model CLIs in consultant vaulted mode."""
import sys


print(
    "Latch vaulted mode blocked a Latch-owned model subprocess because "
    "the provider account identity cannot be verified.",
    file=sys.stderr,
)
raise SystemExit(78)
