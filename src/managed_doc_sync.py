"""Shared managed-region sync for latch instruction documents.

The Claude and Codex instruction files use the same engine contract, but they
live in different agent surfaces (`CLAUDE.md` and `AGENTS.md`).  This module
holds the region-rewrite mechanics so each surface can keep its own stable
markers, CLI wording, and first-wire prompt.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
DRIFT = "drift"
MISSING = "missing"
ABSENT = "absent"


@dataclass(frozen=True)
class ManagedDocSpec:
    target_name: str
    snippet_path: Path
    begin_mark: str
    end_mark: str
    source_doc_name: str = "CLAUDE.md"
    installer_name: str = "install_claude_md"


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def render_contract(spec: ManagedDocSpec, kb_home: str) -> str:
    """Render the shared latch contract for one instruction-file surface."""
    text = spec.snippet_path.read_text(encoding="utf-8")
    text = text.replace("{{KB_HOME}}", kb_home)
    if spec.target_name != spec.source_doc_name:
        text = text.replace(spec.source_doc_name, spec.target_name)
    if spec.installer_name != "install_claude_md":
        text = text.replace("install_claude_md", spec.installer_name)
    return _norm(text).strip("\n")


def extract_region(content: str, spec: ManagedDocSpec) -> str | None:
    norm = _norm(content)
    if spec.begin_mark not in norm or spec.end_mark not in norm:
        return None
    after = norm.split(spec.begin_mark, 1)[1]
    if spec.end_mark not in after:
        return None
    return after.split(spec.end_mark, 1)[0].strip("\n")


def evaluate(target: Path, spec: ManagedDocSpec, kb_home: str) -> str:
    if not target.is_file():
        return ABSENT
    region = extract_region(target.read_text(encoding="utf-8"), spec)
    if region is None:
        return MISSING
    return OK if region == render_contract(spec, kb_home) else DRIFT


def sync(
    target: Path,
    spec: ManagedDocSpec,
    kb_home: str,
    *,
    create: bool = True,
) -> str:
    contract = render_contract(spec, kb_home)
    block = f"{spec.begin_mark}\n{contract}\n{spec.end_mark}"
    status = evaluate(target, spec, kb_home)

    if status == OK:
        return "unchanged"

    if status == ABSENT:
        if not create:
            return "skipped"
        target.write_text(block + "\n", encoding="utf-8", newline="\n")
        return "created"

    content = target.read_text(encoding="utf-8")
    target.with_name(target.name + ".latchbak").write_text(content, encoding="utf-8")
    norm = _norm(content)

    if status == MISSING:
        if not create:
            return "skipped"
        target.write_text(norm.rstrip("\n") + "\n\n" + block + "\n",
                          encoding="utf-8", newline="\n")
        return "appended"

    before = norm.split(spec.begin_mark, 1)[0]
    after = norm.split(spec.end_mark, 1)[1]
    target.write_text(before + block + after, encoding="utf-8", newline="\n")
    return "synced"


def unsync(target: Path, spec: ManagedDocSpec, *, backup: bool = True) -> str:
    if not target.is_file():
        return ABSENT
    content = target.read_text(encoding="utf-8")
    if extract_region(content, spec) is None:
        return MISSING
    if backup:
        target.with_name(target.name + ".latchbak").write_text(
            content, encoding="utf-8"
        )
    norm = _norm(content)
    before = norm.split(spec.begin_mark, 1)[0].rstrip("\n")
    after = norm.split(spec.end_mark, 1)[1].lstrip("\n")
    if before and after:
        new = before + "\n\n" + after
    else:
        new = before + after
    new = new.rstrip("\n")
    target.write_text((new + "\n") if new else "", encoding="utf-8", newline="\n")
    return "removed"


def stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        return False


def prompt_yes_no(question: str) -> bool:
    try:
        reply = input(f"{question} [y/N]: ")
    except EOFError:
        return False
    return reply.strip().lower() in ("y", "yes")
