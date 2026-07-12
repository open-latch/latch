#!/usr/bin/env python3
"""Release, schema, and project-wiring version diagnostics for latch.

The three versions intentionally have separate lifecycles:

* ``LATCH_VERSION`` is the user-facing SemVer release.
* ``KB_SCHEMA_VERSION`` is a monotonic database compatibility integer.
* ``WIRING_VERSION`` changes only when copied project integration assets change.

This module is stdlib-only so installers, doctors, and SessionStart hooks can
import it before latch's runtime dependencies are available.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?$")
STABLE_SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _read(name: str) -> str:
    path = ROOT / name
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{path} is empty")
    return value


def _read_positive_int(name: str) -> int:
    raw = _read(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{ROOT / name} must contain an integer") from exc
    if value < 1:
        raise RuntimeError(f"{ROOT / name} must be >= 1")
    return value


LATCH_VERSION = _read("VERSION")
if not SEMVER_RE.fullmatch(LATCH_VERSION):
    raise RuntimeError(f"{ROOT / 'VERSION'} must contain SemVer (got {LATCH_VERSION!r})")
KB_SCHEMA_VERSION = _read_positive_int("KB_SCHEMA_VERSION")
WIRING_VERSION = _read_positive_int("WIRING_VERSION")


def _git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def payload() -> dict[str, object]:
    commit = _git("rev-parse", "--short=12", "HEAD")
    describe = _git("describe", "--tags", "--exact-match", "HEAD")
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return {
        "latch_version": LATCH_VERSION,
        "kb_schema_version": KB_SCHEMA_VERSION,
        "wiring_version": WIRING_VERSION,
        "commit": commit,
        "release_tag": describe,
        "dirty": dirty,
        "install_root": str(ROOT),
    }


def check_tag(tag: str) -> tuple[bool, str]:
    if not STABLE_SEMVER_RE.fullmatch(LATCH_VERSION):
        return False, (
            f"VERSION {LATCH_VERSION} is not stable SemVer; stable releases require "
            "MAJOR.MINOR.PATCH with no prerelease or build suffix"
        )
    expected = f"v{LATCH_VERSION}"
    if tag == expected:
        return True, f"tag {tag} matches VERSION {LATCH_VERSION}"
    return False, f"tag {tag} does not match VERSION {LATCH_VERSION} (expected {expected})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="show latch release and compatibility versions")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ap.add_argument("--check-tag", metavar="TAG", help="verify vX.Y.Z matches VERSION")
    args = ap.parse_args(argv)
    if args.check_tag:
        ok, detail = check_tag(args.check_tag)
        print(detail)
        return 0 if ok else 1
    info = payload()
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    tag = info["release_tag"] or "unreleased checkout"
    commit = info["commit"] or "unknown"
    dirty = " dirty" if info["dirty"] else ""
    print(f"latch {LATCH_VERSION} ({tag}, {commit}{dirty})")
    print(f"KB schema {KB_SCHEMA_VERSION}; project wiring {WIRING_VERSION}")
    print(f"install root: {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
