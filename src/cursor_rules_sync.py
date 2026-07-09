"""Sync latch's Cursor project rule file.

The Cursor activation rule is a dedicated, latch-owned `.mdc` file. Unlike
CLAUDE.md/AGENTS.md, it is not a managed region inside a user-authored file: the
YAML frontmatter needs to stay first for Cursor, so latch owns the whole file
and backs up any drift before overwriting it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import managed_doc_sync as mds

KB_HOME = Path(__file__).resolve().parent.parent
SNIPPET_PATH = KB_HOME / "cursor_rule_snippet.mdc"
DEFAULT_RULE_PATH = Path(".cursor") / "rules" / "latch.mdc"

OK = mds.OK
DRIFT = mds.DRIFT
ABSENT = mds.ABSENT


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"


def _kb_home_str() -> str:
    return str(KB_HOME).replace("\\", "/")


def render_rule(kb_home: str | None = None) -> str:
    home = kb_home if kb_home is not None else _kb_home_str()
    text = SNIPPET_PATH.read_text(encoding="utf-8")
    return _norm(text.replace("{{KB_HOME}}", home))


def evaluate(target: Path, kb_home: str | None = None) -> str:
    if not target.is_file():
        return ABSENT
    return OK if _norm(target.read_text(encoding="utf-8")) == render_rule(kb_home) else DRIFT


def sync(target: Path, kb_home: str | None = None, *, create: bool = True) -> str:
    status = evaluate(target, kb_home)
    if status == OK:
        return "unchanged"
    if status == ABSENT and not create:
        return "skipped"

    content = render_rule(kb_home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.with_name(target.name + ".latchbak").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8"
        )
        target.write_text(content, encoding="utf-8")
        return "synced"
    target.write_text(content, encoding="utf-8")
    return "created"


def remove(target: Path, kb_home: str | None = None, *, backup: bool = True) -> str:
    if not target.is_file():
        return ABSENT
    if evaluate(target, kb_home) != OK:
        return DRIFT
    if backup:
        target.with_name(target.name + ".latchbak").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8"
        )
    target.unlink()
    return "removed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="install_cursor_rules",
        description="Sync latch's Cursor .mdc activation rule.",
    )
    ap.add_argument("target", nargs="?", default=str(DEFAULT_RULE_PATH),
                    help="path to the Cursor rule file")
    ap.add_argument("--check", "-c", action="store_true",
                    help="verify only; exit 1 if the rule is missing or drifted")
    ap.add_argument("--remove", action="store_true",
                    help="remove the rule only if it still matches latch's copy")
    args = ap.parse_args(argv)
    target = Path(args.target)

    if args.check:
        status = evaluate(target)
        if status == OK:
            print(f"OK: {target} matches {SNIPPET_PATH}")
            return 0
        print(f"DRIFT [{status}]: {target} differs from {SNIPPET_PATH}",
              file=sys.stderr)
        return 1

    if args.remove:
        action = remove(target)
        if action == "removed":
            print(f"removed {target} (backup: {target}.latchbak)")
            return 0
        print(f"{action}: {target} not removed")
        return 1 if action == DRIFT else 0

    action = sync(target)
    if action == "synced":
        print(f"synced {target} (backup: {target}.latchbak)")
    elif action == "created":
        print(f"created {target}")
    else:
        print(f"{action}: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
