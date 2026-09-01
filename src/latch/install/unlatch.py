"""Project instruction-file masking for latch Unlatched mode.

The DISABLE/UNLATCHED sentinels stop latch hooks and model-backed maintenance,
but Claude Code and Codex read CLAUDE.md/AGENTS.md natively before hooks can do
anything. Unlatched mode therefore also masks latch's managed instruction regions
through the active ancestor instruction chain, then restores the exact files
recorded in state when latch is turned back on.
"""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import argparse
import json
from pathlib import Path

from latch.hosts import agents_md_sync
from latch.hosts import claude_md_sync
from latch.store.paths import KB_ROOT


STATE_FILE = KB_ROOT / "UNLATCH_STATE.json"
BEGIN_MARK = "<!-- BEGIN LATCH UNLATCHED OVERRIDE : managed region, run /unlatch to re-latch -->"
END_MARK = "<!-- END LATCH UNLATCHED OVERRIDE -->"
OVERRIDE_BODY = """\
# latch is unlatched

Latch is currently UNLATCHED for this latch install. Ignore any latch managed
snippet or latch KB workflow instructions while this block is present. Do not
run latch gate/search/compact/maintenance or use latch history unless the user
explicitly asks to re-latch by running /unlatch again. If you change repos before
re-latching, latch remains off and will say so.
If LATCH_UNLATCHED is set, unset it too before expecting hooks to resume.

Current user instructions and project-local files outrank stale latch history.

UNLATCHED_LATCH_HOME={latch_home}
"""


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _override_block() -> str:
    body = OVERRIDE_BODY.format(latch_home=str(KB_ROOT))
    return f"{BEGIN_MARK}\n{body.rstrip()}\n{END_MARK}\n"


def _has_override(text: str) -> bool:
    norm = _norm(text)
    return BEGIN_MARK in norm and END_MARK in norm


def _strip_override(text: str) -> tuple[str, bool]:
    norm = _norm(text)
    if BEGIN_MARK not in norm or END_MARK not in norm:
        return norm, False
    before = norm.split(BEGIN_MARK, 1)[0].rstrip("\n")
    after = norm.split(END_MARK, 1)[1].lstrip("\n")
    if before and after:
        return before + "\n\n" + after, True
    return before + after, True


def _prepend_override(path: Path) -> bool:
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    if _has_override(content):
        return False
    path.write_text(_override_block() + "\n" + _norm(content).lstrip("\n"),
                    encoding="utf-8")
    return True


def _write_without_override(path: Path) -> bool:
    if not path.exists():
        return False
    content = path.read_text(encoding="utf-8")
    new, changed = _strip_override(content)
    if changed:
        path.write_text((new.rstrip("\n") + "\n") if new else "",
                        encoding="utf-8")
    return changed


def _managed_mark_pairs(kind: str) -> list[tuple[str, str]]:
    if kind == "agents":
        pairs = [(agents_md_sync.BEGIN_MARK, agents_md_sync.END_MARK)]
        legacy_begin = getattr(agents_md_sync, "LEGACY_BEGIN_MARK", None)
        legacy_end = getattr(agents_md_sync, "LEGACY_END_MARK", None)
        if legacy_begin and legacy_end:
            pairs.append((legacy_begin, legacy_end))
        return pairs
    return [(claude_md_sync.BEGIN_MARK, claude_md_sync.END_MARK)]


def _restore_metadata(kind: str, path: Path) -> dict[str, str]:
    norm = _norm(path.read_text(encoding="utf-8"))
    for begin, end in _managed_mark_pairs(kind):
        if begin not in norm or end not in norm:
            continue
        before, rest = norm.split(begin, 1)
        body, _after = rest.split(end, 1)
        return {
            "managed_block": begin + body + end,
            "restore_prefix": before.rstrip("\n"),
        }
    return {}


def _restore_managed_block(path: Path, record: dict, sync) -> str:
    block = record.get("managed_block")
    prefix = record.get("restore_prefix")
    if not isinstance(block, str) or not isinstance(prefix, str):
        return sync(path, create=True)

    content = _norm(path.read_text(encoding="utf-8"))
    if _has_any_managed_region(content):
        return sync(path, create=True)

    block = block.rstrip("\n")
    if prefix:
        if content.startswith(prefix):
            remainder = content[len(prefix):].lstrip("\n")
            new = prefix.rstrip("\n") + "\n\n" + block
            if remainder:
                new += "\n\n" + remainder.rstrip("\n")
        else:
            new = content.rstrip("\n") + "\n\n" + block
    else:
        remainder = content.lstrip("\n")
        new = block
        if remainder:
            new += "\n\n" + remainder.rstrip("\n")

    path.with_name(path.name + ".latchbak").write_text(content, encoding="utf-8")
    path.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
    return "restored-position"


def _has_any_managed_region(content: str) -> bool:
    norm = _norm(content)
    for begin, end in (
        (claude_md_sync.BEGIN_MARK, claude_md_sync.END_MARK),
        (agents_md_sync.BEGIN_MARK, agents_md_sync.END_MARK),
        (getattr(agents_md_sync, "LEGACY_BEGIN_MARK", ""), getattr(agents_md_sync, "LEGACY_END_MARK", "")),
    ):
        if begin and end and begin in norm and end in norm:
            return True
    return False


def _ancestor_dirs(project: Path):
    current = project.resolve()
    if current.is_file():
        current = current.parent
    dirs = []
    while True:
        dirs.append(current)
        if current.parent == current:
            break
        current = current.parent
    yield from reversed(dirs)


def _path_key(path: Path) -> str:
    return str(path.resolve())


def _target_specs(project: Path):
    surfaces = [
        ("claude", "CLAUDE.md", claude_md_sync.evaluate, claude_md_sync.unsync, claude_md_sync.sync),
        ("agents", "AGENTS.md", agents_md_sync.evaluate, agents_md_sync.unsync, agents_md_sync.sync),
    ]
    specs = []
    seen: set[str] = set()
    for directory in _ancestor_dirs(project):
        for kind, filename, evaluate, unsync, sync in surfaces:
            path = directory / filename
            if not path.is_file():
                continue
            key = _path_key(path)
            if key in seen:
                continue
            seen.add(key)
            specs.append((kind, path.resolve(), evaluate, unsync, sync))
    return specs


def _managed_region_present(evaluate, path: Path) -> bool:
    return evaluate(path) in (claude_md_sync.OK, claude_md_sync.DRIFT)


def enable(project: Path) -> list[str]:
    """Remove Unlatched overrides and restore any masked latch regions."""
    project = project.resolve()
    messages: list[str] = []
    state = _read_state()
    records = state.get("instruction_files", [])
    if not records:
        records = [
            {"path": str(path), "kind": kind, "had_managed_region": False}
            for kind, path, _evaluate, _unsync, _sync in _target_specs(project)
        ]

    for record in records:
        path = Path(record["path"])
        kind = record.get("kind")
        sync = claude_md_sync.sync if kind == "claude" else agents_md_sync.sync
        if not path.is_file():
            messages.append(f"skipped missing instruction file {path}; not recreating latch managed region")
            continue
        removed_override = _write_without_override(path)
        if removed_override:
            messages.append(f"removed unlatched override from {path}")
        if record.get("had_managed_region"):
            action = _restore_managed_block(path, record, sync)
            messages.append(f"restored latch managed region in {path} ({action})")

    if STATE_FILE.exists():
        STATE_FILE.unlink()
        messages.append(f"removed {STATE_FILE}")
    return messages


def disable(project: Path) -> list[str]:
    """Mask latch-managed regions in ancestor instruction files."""
    project = project.resolve()
    messages: list[str] = []
    records: list[dict] = []
    old_state = _read_state()
    old_records = {
        _path_key(Path(record["path"])): record
        for record in old_state.get("instruction_files", [])
        if isinstance(record, dict) and record.get("path")
    }

    for kind, path, evaluate, unsync, _sync in _target_specs(project):
        prior = old_records.pop(_path_key(path), {})
        had_managed = _managed_region_present(evaluate, path)
        has_override = _has_override(path.read_text(encoding="utf-8"))
        if not (had_managed or has_override or prior):
            continue
        restore = _restore_metadata(kind, path) if had_managed else {}
        if had_managed:
            action = unsync(path, backup=True)
            messages.append(f"masked latch managed region in {path} ({action})")
        inserted = _prepend_override(path)
        if inserted:
            messages.append(f"added unlatched override to {path}")
        should_restore = had_managed or bool(prior.get("had_managed_region"))
        if should_restore and not had_managed and prior:
            messages.append(f"preserved latch restore state for {path}")
        record = {
            "kind": kind,
            "path": str(path),
            "had_managed_region": should_restore,
            "inserted_override": inserted or bool(prior.get("inserted_override")),
        }
        record.update(restore or {
            key: value
            for key, value in prior.items()
            if key in ("managed_block", "restore_prefix")
        })
        records.append(record)

    # Do not lose restore records from an earlier unlatch run merely because
    # the command is repeated from a different project directory or after the
    # managed region has already been masked.
    records.extend(old_records.values())

    state = {"project": str(project), "instruction_files": records}
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
    messages.append(f"wrote {STATE_FILE}")
    if not records:
        messages.append("no latch-managed CLAUDE.md or AGENTS.md regions found in current project or ancestors; hook banner remains the visible unlatched receipt")
    return messages


def status(project: Path) -> list[str]:
    project = project.resolve()
    messages: list[str] = []
    state = _read_state()
    if state:
        messages.append(f"instruction mask state: {STATE_FILE}")
    for _kind, path, _evaluate, _unsync, _sync in _target_specs(project):
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if _has_override(content):
            messages.append(f"unlatched override present in {path}")
    return messages


def _read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mask/restore latch instruction regions for Unlatched mode.")
    ap.add_argument("mode", choices=("off", "on", "status"))
    ap.add_argument("--project", default=".", help="project directory containing CLAUDE.md/AGENTS.md")
    args = ap.parse_args(argv)

    project = Path(args.project)
    if args.mode == "off":
        messages = disable(project)
    elif args.mode == "on":
        messages = enable(project)
    else:
        messages = status(project)
    for msg in messages:
        print(f"  {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
