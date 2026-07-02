#!/usr/bin/env python3
"""Public release hygiene checks for the latch source tree."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


ROOT_MARKDOWN_ALLOW = {
    "README.md",
    "ARCHITECTURE.md",
    "CONTRIBUTING.md",
    "TRADEMARK.md",
    "claude_md_snippet.md",
}

DOCS_MARKDOWN_ALLOW = {
    "docs/first_run_mission.md",
}

GUARD_IMPLEMENTATION_FILES = {
    ".githooks/denylist.txt",
    "src/public_release_check.py",
    "tests/test_public_release_check.py",
}

BLOCKED_DOC_NAME = re.compile(
    r"(strategy|roadmap|vision|moat|competitive|market|moneti[sz]|pricing|billing|"
    r"commercial|cowork|cross[_-]?app|adapter|first[-_]?oss[-_]?audit)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    markdown_only: bool = False


RULES = (
    Rule("personal account or filesystem reference", re.compile(r"\b(?:nicomey|nmeyer)\b|/Users/(?:nicomey|nico)\b", re.IGNORECASE)),
    Rule("paid or billing language", re.compile(r"\b(?:paid|billing|moneti[sz](?:e|ation|ing)|commercialization)\b", re.IGNORECASE)),
    Rule("team/shared layer language", re.compile(r"\b(?:team[-/ ]shared|shared layer|team graph|shared graph|team product)\b", re.IGNORECASE)),
    Rule("Meta company reference", re.compile(r"\bMeta\b")),
    Rule("quant-research reference", re.compile(r"\bquant(?:itative)? research\b|\bquant\b", re.IGNORECASE)),
    Rule("strategy term in markdown", re.compile(r"\b(?:strategy|roadmap|vision|moat|competitive|source-available)\b", re.IGNORECASE), markdown_only=True),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str


def repo_root() -> Path:
    out = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True)
    return Path(out.strip())


def git_files(root: Path) -> list[str]:
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=root)
    return [p.decode("utf-8") for p in raw.split(b"\0") if p]


def is_markdown(path: str) -> bool:
    return path.endswith(".md") or path.endswith(".mdx") or path.endswith(".rst")


def is_text(data: bytes) -> bool:
    return b"\0" not in data


def read_text(root: Path, rel: str) -> str | None:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        return None
    if not is_text(data):
        return None
    return data.decode("utf-8", errors="replace")


def check_path_policy(paths: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        if path in GUARD_IMPLEMENTATION_FILES:
            continue
        if is_markdown(path):
            if path.startswith("docs/") and path not in DOCS_MARKDOWN_ALLOW:
                findings.append(Finding(path, 0, "unapproved docs markdown", "only docs/first_run_mission.md is public-release approved"))
            if "/" not in path and path not in ROOT_MARKDOWN_ALLOW and BLOCKED_DOC_NAME.search(path):
                findings.append(Finding(path, 0, "strategy-like markdown filename", path))
            if BLOCKED_DOC_NAME.search(Path(path).name):
                findings.append(Finding(path, 0, "strategy-like markdown filename", path))
    return findings


def scan_text(path: str, text: str) -> list[Finding]:
    if path in GUARD_IMPLEMENTATION_FILES:
        return []
    markdown = is_markdown(path)
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for rule in RULES:
            if rule.markdown_only and not markdown:
                continue
            if rule.pattern.search(line):
                findings.append(Finding(path, lineno, rule.name, line.strip()))
    return findings


def check_tree(root: Path) -> list[Finding]:
    paths = [path for path in git_files(root) if (root / path).exists()]
    findings = check_path_policy(paths)
    for path in paths:
        if path.startswith("vendor/"):
            continue
        text = read_text(root, path)
        if text is None:
            continue
        findings.extend(scan_text(path, text))
    return findings


def format_findings(findings: list[Finding]) -> str:
    lines = []
    for finding in findings:
        loc = finding.path if finding.line == 0 else f"{finding.path}:{finding.line}"
        lines.append(f"{loc}: {finding.rule}: {finding.text}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="scan the tracked latch tree for public-release hygiene leaks")
    ap.add_argument("--repo", type=Path, default=None, help="repository root; defaults to git rev-parse")
    args = ap.parse_args(argv)

    root = args.repo or repo_root()
    findings = check_tree(root)
    if findings:
        print("public release hygiene check failed:", file=sys.stderr)
        print(format_findings(findings), file=sys.stderr)
        return 1
    print("public release hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
