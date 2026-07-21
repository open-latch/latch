#!/usr/bin/env python3
"""Inspect or change Latch's prompt/brief intensity."""
from __future__ import annotations

import argparse
import json
import sys

import paths


DESCRIPTIONS = {
    "quiet": (
        "up to 1 workstream and 1 open question at startup; on supported "
        "prompt-hook hosts, hook-added similarity hits are off"
    ),
    "standard": (
        "on supported prompt-hook hosts, a local topic-similarity check on each "
        "eligible prompt and up to 3 hits injected on the first prompt or a "
        "topic change; 3-workstream/2-question/2-idea brief where supported"
    ),
    "full": (
        "on supported prompt-hook hosts, up to 5 prompt hits on every eligible "
        "prompt; 5-workstream/3-question/5-idea brief where supported"
    ),
}


def status_payload() -> dict[str, str | None]:
    value, source, warning = paths.latch_intensity_state()
    return {
        "intensity": value,
        "source": source,
        "description": DESCRIPTIONS[value],
        "warning": warning,
        "settings_file": str(paths.LATCH_SETTINGS_FILE),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Show or change Latch intensity (Quiet, Standard, or Full)."
    )
    ap.add_argument("intensity", nargs="?", choices=paths.LATCH_INTENSITIES)
    ap.add_argument("--json", action="store_true", help="emit machine-readable status")
    args = ap.parse_args(argv)

    if args.intensity:
        try:
            target = paths.write_latch_intensity(args.intensity)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not args.json:
            print(f"Latch intensity set to {args.intensity.title()} in {target}.")
            print(
                "  Scope: every project and host using this Latch install. "
                "Gate configuration is unchanged."
            )

    payload = status_payload()
    if args.json:
        print(json.dumps(payload, sort_keys=True))
        return 0

    print(
        f"Latch intensity: {str(payload['intensity']).title()} "
        f"({payload['source']})"
    )
    print(f"  {payload['description']}")
    print("  Scope: every project and host using this Latch install.")
    print("  Static contract-driven Latch reads remain required at every level.")
    if payload["warning"]:
        print(f"  warning: {payload['warning']}")
        return 1
    if payload["source"] == "env":
        print("  LATCH_INTENSITY overrides the saved setting for this process.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
