"""claude_md_sync — keep a project's CLAUDE.md latch-contract region in sync
with claude_md_snippet.md, the SINGLE SOURCE OF TRUTH for the engine contract.

The snippet is injected into a MANAGED REGION delimited by the BEGIN/END markers
below. Everything OUTSIDE the region (the project's own paths, ownership, domain
rules) is preserved. This module is the ONE implementation, used three ways:

  * bin/install_claude_md.{sh,ps1} — thin wrappers; manual first-wiring + re-sync.
  * the SessionStart hook — silent auto-sync of an ALREADY-WIRED project when the
    snippet has changed upstream (create=False, so it never auto-wires a project).
  * --check mode — drift gate for pre-commit / CI.

Dependency-free (os/sys/pathlib only; argparse imported lazily in the CLI) so it
is safe to import on the hot SessionStart path — no numpy / db / embeddings.
Comparison is LF-normalized so a CRLF working copy never reads as drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

import managed_doc_sync as mds

KB_HOME = Path(__file__).resolve().parent.parent
SNIPPET_PATH = KB_HOME / "claude_md_snippet.md"

# MUST stay byte-identical to the markers any prior sync wrote into a CLAUDE.md.
BEGIN_MARK = "<!-- BEGIN LATCH SNIPPET : managed region, do not hand-edit; edit claude_md_snippet.md in the latch repo and re-run bin/install_claude_md -->"
END_MARK = "<!-- END LATCH SNIPPET -->"

# evaluate() status codes
OK = mds.OK            # region present and matches the snippet
DRIFT = mds.DRIFT      # region present but differs from the snippet
MISSING = mds.MISSING  # file exists but has no managed region
ABSENT = mds.ABSENT    # file does not exist

SPEC = mds.ManagedDocSpec(
    target_name="CLAUDE.md",
    snippet_path=SNIPPET_PATH,
    begin_mark=BEGIN_MARK,
    end_mark=END_MARK,
)


def _kb_home_str() -> str:
    """Forward-slash KB_HOME, matching the legacy shell-installer rendering."""
    return str(KB_HOME).replace("\\", "/")


def _norm(text: str) -> str:
    return mds._norm(text)


def render_contract(kb_home: str | None = None) -> str:
    """The snippet with {{KB_HOME}} substituted, LF-normalized and trimmed."""
    home = kb_home if kb_home is not None else _kb_home_str()
    return mds.render_contract(SPEC, home)


def extract_region(content: str) -> str | None:
    """Body between the markers (exclusive), LF-normalized + trimmed, or None."""
    return mds.extract_region(content, SPEC)


def evaluate(target: Path, kb_home: str | None = None) -> str:
    """OK | DRIFT | MISSING | ABSENT for the target CLAUDE.md."""
    home = kb_home if kb_home is not None else _kb_home_str()
    return mds.evaluate(target, SPEC, home)


def sync(target: Path, kb_home: str | None = None, *, create: bool = True) -> str:
    """Write the contract into target's managed region. Returns the action taken:
    'created' | 'appended' | 'synced' | 'unchanged' | 'skipped'.

    Non-destructive (id=1191 merge mandate): backs up to ``<name>.latchbak``
    before any modify, only ever rewrites the managed region (never content
    outside it), and never deletes the file. The whole file is rewritten
    LF-normalized so it does not end up with mixed line endings.

    ``create=False`` (the hook's auto-sync mode): skip ABSENT/MISSING targets so
    a project is NEVER auto-wired — only an already-wired (markers present)
    project that has drifted gets re-synced.
    """
    home = kb_home if kb_home is not None else _kb_home_str()
    return mds.sync(target, SPEC, home, create=create)


def unsync(target: Path, *, backup: bool = True) -> str:
    """Remove latch's managed region from target. Returns the action taken:
    'removed' | 'absent' | 'missing'.

    The inverse of ``sync`` and the CLAUDE.md half of ``uninstall_engine``: it
    deletes the BEGIN..END block (markers included) and preserves everything
    outside it — the project's own paths, ownership, and domain rules are never
    touched. Backs up to ``<name>.latchbak`` before modifying (mirrors ``sync``)
    and never deletes the file itself; a CLAUDE.md that was nothing but the
    region collapses to empty rather than being removed.

    'absent'  — file does not exist (nothing to do).
    'missing' — file exists but has no managed region (nothing to do).
    """
    return mds.unsync(target, SPEC, backup=backup)


# Shown ONCE, on first-time wiring (CLAUDE.md has no managed region yet), before
# any write. Trust: latch never edits a project's CLAUDE.md silently the first
# time — it says what it is adding, why, and how it will behave thereafter, then
# asks. Plain ASCII so it renders on any console codepage (Win conhost included).
_FIRST_WIRING_NOTICE = """\
------------------------------------------------------------------------
latch wants to add a short managed snippet to:
    {target}

  - What: a small, clearly delimited block (the "LATCH SNIPPET" region)
    that wires latch's KB workflow into this project. Nothing else in the
    file is touched.
  - Source of truth: the block comes from claude_md_snippet.md in the latch
    repo. From now on latch only ever rewrites THAT delimited region, and
    only when it drifts from the repo copy -- your own content around it is
    never modified.
  - Safety: any existing CLAUDE.md is backed up to <file>.latchbak first.

This is the only time latch touches a not-yet-wired CLAUDE.md, so it asks
first rather than editing the file without your knowledge.
------------------------------------------------------------------------"""


def _stdin_is_tty() -> bool:
    """True only when we can actually prompt a human on stdin. Indirected so
    tests can stub it without a real terminal."""
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def _prompt_yes_no(question: str) -> bool:
    """Blocking [y/N] prompt; True only on an explicit yes. Module-level so tests
    can stub it. A bare Enter / EOF defaults to no (never writes by default)."""
    try:
        reply = input(f"{question} [y/N]: ")
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="install_claude_md",
        description="Sync latch's CLAUDE.md engine-contract region from "
                    "claude_md_snippet.md (single source of truth).",
    )
    ap.add_argument("target", nargs="?", default="CLAUDE.md",
                    help="path to the CLAUDE.md to sync (default ./CLAUDE.md)")
    ap.add_argument("--check", "-c", action="store_true",
                    help="verify only; exit 1 if the region is missing or drifted")
    ap.add_argument("--remove", action="store_true",
                    help="strip latch's managed region from the target (uninstall); "
                         "preserves all content outside the region")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="skip the first-wiring confirmation prompt "
                         "(for non-interactive / scripted installs)")
    args = ap.parse_args(argv)
    target = Path(args.target)

    if args.check:
        status = evaluate(target)
        if status == OK:
            print(f"OK: {target} managed region matches {SNIPPET_PATH}")
            return 0
        print(f"DRIFT [{status}]: {target} differs from {SNIPPET_PATH} "
              f"(run install_claude_md to re-sync)", file=sys.stderr)
        return 1

    if args.remove:
        action = unsync(target)
        if action == "removed":
            print(f"removed latch managed region from {target} "
                  f"(backup: {target}.latchbak)")
        else:
            print(f"{action}: {target} (no managed region to remove)")
        return 0

    # First-time wiring (no managed region yet) is the one case where latch adds
    # content to a CLAUDE.md it has never touched — explain it and confirm before
    # writing. Re-sync of an existing region (DRIFT) is region-only and silent,
    # so it does NOT prompt (the user already consented once).
    if evaluate(target) in (ABSENT, MISSING):
        print(_FIRST_WIRING_NOTICE.format(target=target))
        if not args.yes:
            if not _stdin_is_tty():
                print(f"non-interactive shell: re-run with --yes to confirm, or "
                      f"run interactively. No changes made to {target}.",
                      file=sys.stderr)
                return 1
            if not _prompt_yes_no("Add latch's managed snippet now?"):
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
