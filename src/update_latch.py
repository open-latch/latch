#!/usr/bin/env python3
"""Explicit updater for clean official GitHub-clone latch installations."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import schema_version
import install_engine
from versioning import KB_SCHEMA_VERSION, LATCH_VERSION, ROOT, SEMVER_RE

OFFICIAL_REMOTE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"open-latch/latch(?:\.git)?/?$",
    re.IGNORECASE,
)


class UpdateError(RuntimeError):
    pass


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args), cwd=str(ROOT), text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise UpdateError(f"{' '.join(args)} failed: {detail}")
    return result


def _git(*args: str, check: bool = True) -> str:
    return _run("git", "-C", str(ROOT), *args, check=check).stdout.strip()


def _version_key(value: str) -> tuple[int, int, int]:
    raw = value[1:] if value.startswith("v") else value
    if not SEMVER_RE.fullmatch(raw) or "-" in raw or "+" in raw:
        raise ValueError(value)
    major, minor, patch = raw.split(".")
    return int(major), int(minor), int(patch)


def official_remote(url: str) -> bool:
    return bool(OFFICIAL_REMOTE_RE.fullmatch(url.strip()))


def latest_remote_tag(output: str) -> str | None:
    tags: list[str] = []
    for line in output.splitlines():
        ref = line.rsplit("\t", 1)[-1]
        if not ref.startswith("refs/tags/v"):
            continue
        tag = ref.removeprefix("refs/tags/")
        try:
            _version_key(tag)
        except ValueError:
            continue
        tags.append(tag)
    return max(tags, key=_version_key) if tags else None


def _stable_release_from_payload(data: object, *, requested: str | None) -> str | None:
    releases = data if isinstance(data, list) else [data]
    stable: list[str] = []
    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        found = release.get("tag_name")
        if not isinstance(found, str):
            continue
        try:
            _version_key(found)
        except ValueError:
            continue
        if requested is not None and found == requested:
            return found
        stable.append(found)
    return max(stable, key=_version_key) if requested is None and stable else None


def published_release_tag(tag: str | None = None) -> str | None:
    suffix = (
        "?per_page=100"
        if tag is None
        else "/tags/" + urllib.parse.quote(tag, safe="")
    )
    request = urllib.request.Request(
        f"https://api.github.com/repos/open-latch/latch/releases{suffix}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "latch-updater"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise UpdateError(f"GitHub release lookup failed with HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError(f"GitHub release lookup failed: {exc}") from exc
    return _stable_release_from_payload(data, requested=tag)


def discover_kbs() -> list[Path]:
    candidates = set((ROOT / "projects").glob("**/kb.db"))
    for env_name in ("LATCH_KB_DIR", "CLAUDE_KB_DIR"):
        if os.environ.get(env_name):
            candidates.add(Path(os.environ[env_name]).expanduser() / "kb.db")
    pin = ROOT / "kb_location.json"
    try:
        data = json.loads(pin.read_text(encoding="utf-8")) if pin.is_file() else {}
        if isinstance(data, dict) and isinstance(data.get("kb_dir"), str):
            candidates.add(Path(data["kb_dir"]).expanduser() / "kb.db")
    except (OSError, ValueError):
        pass
    return sorted(path.resolve() for path in candidates if path.is_file())


def _target_schema(tag: str) -> int:
    raw = _git("show", f"{tag}:KB_SCHEMA_VERSION")
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise UpdateError(f"{tag} has invalid KB_SCHEMA_VERSION") from exc
    return value


def _kb_update_plan(
    paths: list[Path], target_schema: int
) -> tuple[dict[Path, int], list[Path]]:
    versions: dict[Path, int] = {}
    for path in paths:
        try:
            installed = schema_version.read_database(path)
        except Exception as exc:
            raise UpdateError(f"cannot read KB schema metadata from {path}: {exc}") from exc
        versions[path] = installed
        if installed > target_schema:
            raise UpdateError(
                f"refusing target KB schema {target_schema}: {path} already uses newer schema "
                f"{installed}; update to a compatible latch release"
            )
    return versions, [path for path, installed in versions.items() if installed < target_schema]


def _dependency_command() -> list[str]:
    venv_python = ROOT / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    python = str(venv_python) if venv_python.is_file() else sys.executable
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", python, "-r", str(ROOT / "requirements.txt")]
    return [python, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")]


def _refresh_claude_commands_if_installed() -> bool:
    dest = Path(os.environ.get("CLAUDE_COMMANDS_DIR") or (Path.home() / ".claude" / "commands"))
    managed: list[tuple[Path, Path]] = []
    for source in sorted((ROOT / "commands").glob("*.md")):
        target = dest / source.name
        if not target.is_file():
            continue
        try:
            body = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise UpdateError(f"cannot inspect installed Claude command {target}: {exc}") from exc
        if install_engine._is_latch_command_body(body):
            managed.append((source, target))
    if not managed:
        return False
    for source, target in managed:
        try:
            target.write_text(
                source.read_text(encoding="utf-8").replace("<KB_HOME>", str(ROOT).replace("\\", "/")),
                encoding="utf-8",
            )
        except OSError as exc:
            raise UpdateError(f"source updated, but refreshing {target} failed: {exc}") from exc
    return True


def inspect() -> dict[str, object]:
    if not (ROOT / ".git").exists() and not _git("rev-parse", "--git-dir", check=False):
        raise UpdateError("latch updater requires a Git clone installation")
    remote = _git("config", "--get", "remote.origin.url")
    if not official_remote(remote):
        raise UpdateError(
            f"refusing non-official origin {remote!r}; update this clone manually"
        )
    latest = published_release_tag()
    return {
        "installed": LATCH_VERSION,
        "latest": latest[1:] if latest else None,
        "latest_tag": latest,
        "update_available": bool(latest and _version_key(latest) > _version_key(LATCH_VERSION)),
        "origin": remote,
        "commit": _git("rev-parse", "--short=12", "HEAD"),
    }


def apply_update(tag: str, *, dry_run: bool) -> dict[str, object]:
    branch = _git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if branch and branch != "main":
        raise UpdateError(
            f"refusing to update developer branch {branch!r}; use a clean main or release checkout"
        )
    if not branch:
        checkout_tag = _git("describe", "--tags", "--exact-match", "HEAD", check=False)
        try:
            _version_key(checkout_tag)
        except ValueError as exc:
            raise UpdateError(
                "refusing an arbitrary detached checkout; use clean main or an exact stable release tag"
            ) from exc
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise UpdateError("refusing to update a dirty tracked worktree")
    if published_release_tag(tag) != tag:
        raise UpdateError(f"{tag} is not a published open-latch/latch GitHub Release")
    _git("fetch", "origin", f"refs/tags/{tag}:refs/tags/{tag}")
    release_version = _git("show", f"{tag}:VERSION").strip()
    if tag != f"v{release_version}":
        raise UpdateError(f"{tag} does not match its VERSION file ({release_version})")
    if _version_key(release_version) < _version_key(LATCH_VERSION):
        raise UpdateError("downgrades are not supported by latch_update; restore a matching backup manually")
    target_schema = _target_schema(tag)
    if target_schema < KB_SCHEMA_VERSION:
        raise UpdateError("target release supports an older KB schema; refusing downgrade")
    kb_versions, kbs = _kb_update_plan(discover_kbs(), target_schema)
    plan = {
        "from_version": LATCH_VERSION,
        "to_version": release_version,
        "target": tag,
        "schema_from": KB_SCHEMA_VERSION,
        "schema_to": target_schema,
        "discovered_kb_schemas": {str(path): version for path, version in kb_versions.items()},
        "kb_backups_required": [str(path) for path in kbs],
    }
    if dry_run:
        return plan

    backups = [
        schema_version.backup_database(
            path, from_version=kb_versions[path], to_version=target_schema
        )
        for path in kbs
    ]
    old_commit = _git("rev-parse", "HEAD")
    old_branch = branch
    try:
        _git("switch", "--detach", tag)
        dep = subprocess.run(_dependency_command(), cwd=str(ROOT))
        if dep.returncode != 0:
            raise UpdateError("dependency synchronization failed")
        commands_refreshed = _refresh_claude_commands_if_installed()
    except Exception:
        if old_branch:
            _git("switch", old_branch, check=False)
        else:
            _git("switch", "--detach", old_commit, check=False)
        raise
    plan["backups"] = [str(path) for path in backups]
    plan["claude_commands_refreshed"] = commands_refreshed
    plan["restart_required"] = True
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="check or apply a stable latch GitHub release update")
    ap.add_argument("--check", action="store_true", help="check only; never change files")
    ap.add_argument("--dry-run", action="store_true", help="show the exact update plan")
    ap.add_argument("--yes", action="store_true", help="apply without confirmation")
    ap.add_argument("--target", metavar="vX.Y.Z", help="install a specific stable release tag")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)
    try:
        info = inspect()
        if args.check or (not args.target and not info["update_available"]):
            result = info
        else:
            target = args.target or str(info["latest_tag"] or "")
            try:
                _version_key(target)
            except ValueError as exc:
                raise UpdateError(f"invalid stable target {target!r}") from exc
            if not args.dry_run and not args.yes:
                raise UpdateError("pass --dry-run to inspect or --yes to apply")
            result = apply_update(target, dry_run=args.dry_run)
    except UpdateError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"ok": True, **result}, indent=2))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
        if result.get("restart_required"):
            print("Restart your agent or open a new task so hooks and MCP processes reload.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
