"""Sync latch's managed AGENTS.md region for Codex."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import managed_doc_sync as mds

KB_HOME = Path(__file__).resolve().parent.parent
SNIPPET_PATH = KB_HOME / "claude_md_snippet.md"

BEGIN_MARK = "<!-- BEGIN LATCH AGENTS SNIPPET : managed region, do not hand-edit; edit claude_md_snippet.md in the latch repo and re-run bin/install_agents_md -->"
END_MARK = "<!-- END LATCH AGENTS SNIPPET -->"
LEGACY_BEGIN_MARK = "<!-- BEGIN LATCH SNIPPET : managed region, do not hand-edit; edit claude_md_snippet.md in the latch repo and re-run bin/install_claude_md -->"
LEGACY_END_MARK = "<!-- END LATCH SNIPPET -->"

OK = mds.OK
DRIFT = mds.DRIFT
MISSING = mds.MISSING
ABSENT = mds.ABSENT

SPEC = mds.ManagedDocSpec(
    target_name="AGENTS.md",
    snippet_path=SNIPPET_PATH,
    begin_mark=BEGIN_MARK,
    end_mark=END_MARK,
    installer_name="install_agents_md",
)
LEGACY_SPEC = mds.ManagedDocSpec(
    target_name="AGENTS.md",
    snippet_path=SNIPPET_PATH,
    begin_mark=LEGACY_BEGIN_MARK,
    end_mark=LEGACY_END_MARK,
    installer_name="install_agents_md",
)


def _kb_home_str() -> str:
    return str(KB_HOME).replace("\\", "/")


def render_contract(kb_home: str | None = None) -> str:
    return mds.render_contract(SPEC, kb_home if kb_home is not None else _kb_home_str())


def extract_region(content: str) -> str | None:
    return mds.extract_region(content, SPEC)


def evaluate(target: Path, kb_home: str | None = None) -> str:
    home = kb_home if kb_home is not None else _kb_home_str()
    status = mds.evaluate(target, SPEC, home)
    if status == MISSING and target.is_file():
        legacy = mds.extract_region(target.read_text(encoding="utf-8"), LEGACY_SPEC)
        if legacy is not None:
            return DRIFT
    return status


def sync(target: Path, kb_home: str | None = None, *, create: bool = True) -> str:
    home = kb_home if kb_home is not None else _kb_home_str()
    status = mds.evaluate(target, SPEC, home)
    if status == MISSING and target.is_file():
        content = target.read_text(encoding="utf-8")
        if mds.extract_region(content, LEGACY_SPEC) is not None:
            contract = mds.render_contract(SPEC, home)
            block = f"{BEGIN_MARK}\n{contract}\n{END_MARK}"
            target.with_name(target.name + ".latchbak").write_text(
                content, encoding="utf-8"
            )
            norm = mds._norm(content)
            before = norm.split(LEGACY_BEGIN_MARK, 1)[0]
            after = norm.split(LEGACY_END_MARK, 1)[1]
            target.write_text(before + block + after, encoding="utf-8", newline="\n")
            return "synced"
    return mds.sync(target, SPEC, home, create=create)


def unsync(target: Path, *, backup: bool = True) -> str:
    if target.is_file() and mds.extract_region(
        target.read_text(encoding="utf-8"), SPEC
    ) is None:
        if mds.extract_region(target.read_text(encoding="utf-8"), LEGACY_SPEC) is not None:
            return mds.unsync(target, LEGACY_SPEC, backup=backup)
    return mds.unsync(target, SPEC, backup=backup)


_FIRST_WIRING_NOTICE = """\
------------------------------------------------------------------------
latch wants to add a short managed snippet to:
    {target}

  - What: a small, clearly delimited block (the "LATCH AGENTS SNIPPET"
    region) that wires latch's KB workflow into Codex for this project.
    Nothing else in the file is touched.
  - Source of truth: the block is rendered from claude_md_snippet.md in the
    latch repo with Codex/AGENTS.md wording. From now on latch only ever
    rewrites THAT delimited region, and only when it drifts from the repo copy.
  - Safety: any existing AGENTS.md is backed up to <file>.latchbak first.

This is the only time latch touches a not-yet-wired AGENTS.md, so it asks
first rather than editing the file without your knowledge.
------------------------------------------------------------------------"""


def _stdin_is_tty() -> bool:
    return mds.stdin_is_tty()


def _prompt_yes_no(question: str) -> bool:
    return mds.prompt_yes_no(question)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="install_agents_md",
        description="Sync latch's AGENTS.md engine-contract region for Codex.",
    )
    ap.add_argument("target", nargs="?", default="AGENTS.md",
                    help="path to the AGENTS.md to sync (default ./AGENTS.md)")
    ap.add_argument("--check", "-c", action="store_true",
                    help="verify only; exit 1 if the region is missing or drifted")
    ap.add_argument("--remove", action="store_true",
                    help="strip latch's managed AGENTS.md region from the target")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the first-wiring confirmation prompt")
    args = ap.parse_args(argv)
    target = Path(args.target)

    if args.check:
        status = evaluate(target)
        if status == OK:
            print(f"OK: {target} managed region matches {SNIPPET_PATH}")
            return 0
        print(f"DRIFT [{status}]: {target} differs from {SNIPPET_PATH} "
              f"(run install_agents_md to re-sync)", file=sys.stderr)
        return 1

    if args.remove:
        action = unsync(target)
        if action == "removed":
            print(f"removed latch managed region from {target} "
                  f"(backup: {target}.latchbak)")
        else:
            print(f"{action}: {target} (no managed region to remove)")
        return 0

    if evaluate(target) in (ABSENT, MISSING):
        print(_FIRST_WIRING_NOTICE.format(target=target))
        if not args.yes:
            if not _stdin_is_tty():
                print(f"non-interactive shell: re-run with --yes to confirm, or "
                      f"run interactively. No changes made to {target}.",
                      file=sys.stderr)
                return 1
            if not _prompt_yes_no("Add latch's managed AGENTS.md snippet now?"):
                print(f"aborted: no changes made to {target}.")
                return 0

    action = sync(target, create=True)
    if action in ("synced", "appended"):
        print(f"{action} managed region in {target} (backup: {target}.latchbak)")
    elif action == "created":
        print(f"created {target} with managed region")
    else:
        print(f"{action}: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
