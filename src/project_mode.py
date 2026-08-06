#!/usr/bin/env python3
"""The small public scope controller behind ``latch`` and ``unlatch``.

Bare invocations are read-only.  Mutations require the exact confirmation word
and operate on one explicit filesystem root.  No transition copies, imports,
merges, or deletes KB knowledge.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import paths
import project_config
import lockfile
import unlatch
import vault_identity


def _selected_root(value: str | os.PathLike[str]) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise project_config.ProjectConfigError(f"selected root is not a directory: {root}")
    if root == Path(root.anchor):
        raise project_config.ProjectConfigError("refusing a filesystem root as a Latch scope")
    return root


def _project_slug(root: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", root.name).strip("-.") or "project"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def _private_vault_parent() -> Path:
    test_root = paths.validated_test_root()
    if test_root is not None:
        return test_root / "vaults" / "private"
    return vault_identity.platform_production_root() / "vaults" / "private"


def create_new_project_kb(root: Path) -> Path:
    """Allocate one empty external directory; no DB is created here."""
    parent = _private_vault_parent()
    try:
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(
            tempfile.mkdtemp(prefix=f"{_project_slug(root)}-", dir=str(parent))
        ).resolve(strict=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        return directory
    except OSError as exc:
        raise project_config.ProjectConfigError(
            f"could not create a private KB under {parent}: {exc}"
        ) from exc


def _global_override() -> str | None:
    if paths.UNLATCHED_FILE.exists() or os.environ.get("LATCH_UNLATCHED"):
        return "legacy/global UNLATCHED override"
    if (
        paths.DISABLE_FILE.exists()
        or os.environ.get("LATCH_DISABLE")
        or os.environ.get("CLAUDE_KB_DISABLE")
    ):
        return "legacy/global kill switch"
    return None


def _writes_disabled() -> bool:
    return bool(
        paths.DISABLE_WRITE_FILE.exists()
        or os.environ.get("LATCH_DISABLE_WRITE")
        or os.environ.get("CLAUDE_KB_DISABLE_WRITE")
    )


def _assert_local_transition_allowed(*, enabling: bool) -> None:
    override = _global_override()
    if override:
        raise project_config.ProjectConfigError(
            f"a {override} is active; recover it before changing a project scope"
        )
    if enabling and _writes_disabled():
        raise project_config.ProjectConfigError(
            "Latch writes are globally disabled; recover that switch before latching"
        )


def status_payload(project: str | os.PathLike[str]) -> dict[str, object]:
    selected = _selected_root(project)
    target = project_config.resolve(selected)
    override = _global_override()
    state = project_config.MODE_UNLATCHED if override else target.state
    exact_kb = target.kb_dir or target.remembered_kb_dir
    aliases = (
        len(project_config.authorized_scope_roots(target.scope_id))
        if target.scope_id is not None
        else 0
    )
    return {
        "format": 1,
        "selected_root": str(selected),
        "effective_root": str(target.project_root),
        "state": state,
        "policy": target.policy,
        "scope_id": target.scope_id,
        "kb_dir": str(exact_kb) if exact_kb is not None else None,
        "source": target.source,
        "inherited": target.project_root != selected,
        "aliases": aliases,
        "machine_policy": project_config.read_machine_policy(),
        "global_override": override,
        "reason": target.reason,
        "reason_code": target.reason_code,
    }


def status(
    project: str | os.PathLike[str],
    *,
    as_json: bool = False,
    intent: str | None = None,
) -> int:
    payload = status_payload(project)
    if as_json:
        print(json.dumps(payload, sort_keys=True))
        return 2 if payload["state"] == project_config.MODE_LOCKED else 0
    print("Latch scope status")
    print(f"  selected : {payload['selected_root']}")
    print(f"  state    : {str(payload['state']).upper()}")
    print(f"  root     : {payload['effective_root']}")
    print(f"  policy   : {(payload['policy'] or 'none').upper()}")
    print(f"  KB       : {payload['kb_dir'] or 'none (no data-plane access)'}")
    print(f"  source   : {payload['source']}")
    print(f"  machine  : {payload['machine_policy']}")
    if payload["scope_id"]:
        print(f"  scope id : {payload['scope_id']} ({payload['aliases']} authorized root(s))")
    if payload["inherited"]:
        print("  inherited: yes; a child boundary requires an explicit latch choice")
    if payload["global_override"]:
        print(f"  override : {payload['global_override']}")
    if payload["reason"]:
        print(f"  note     : {payload['reason']}")
    if intent == "latch":
        print("\nNo state changed. Confirm with the exact word: latch")
    elif intent == "unlatch":
        print("\nNo state changed. Confirm with the exact word: unlatch")
    return 2 if payload["state"] == project_config.MODE_LOCKED else 0


def _existing_marker(root: Path) -> dict[str, object] | None:
    path = root / project_config.PORTABLE_DIR_NAME / project_config.PORTABLE_FILE_NAME
    if not (path.exists() or path.is_symlink()):
        return None
    if path.is_symlink() or not path.is_file():
        raise project_config.ProjectConfigError(f"unsafe portable scope declaration: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise project_config.ProjectConfigError(
            f"portable scope declaration is unreadable: {path}: {exc}"
        ) from exc
    return payload if isinstance(payload, dict) else None


def _rollback_empty_vault(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.rmdir()
    except OSError:
        # Never recurse or delete a directory that gained content.
        pass


def _instruction_roots(target: project_config.ResolvedScope) -> list[Path]:
    if target.scope_id is None:
        return [target.project_root]
    roots = project_config.authorized_scope_roots(target.scope_id)
    return roots or [target.project_root]


def _disable_instructions(roots: list[Path]) -> list[str]:
    messages: list[str] = []
    for root in roots:
        if not root.is_dir():
            messages.append(f"skipped unavailable authorized root {root}")
            continue
        messages.extend(unlatch.disable(root))
    return messages


def _enable_instructions(roots: list[Path]) -> list[str]:
    messages: list[str] = []
    for root in roots:
        if not root.is_dir():
            messages.append(f"skipped unavailable authorized root {root}")
            continue
        messages.extend(unlatch.enable(root))
    return messages


def _configure_or_change_scope(
    root: Path,
    *,
    policy: str | None,
    kb_dir: str | None,
    new_kb: bool,
) -> tuple[project_config.ResolvedScope, Path | None]:
    if policy not in (None, *project_config.POLICIES):
        raise project_config.ProjectConfigError(f"invalid policy: {policy}")
    if new_kb and kb_dir is not None:
        raise project_config.ProjectConfigError("choose either --new-kb or --kb-dir")
    if (new_kb or kb_dir is not None) and policy != project_config.POLICY_PRIVATE:
        raise project_config.ProjectConfigError(
            "KB selection is valid only with an explicit Private choice"
        )
    before = project_config.resolve(root)
    created: Path | None = create_new_project_kb(root) if new_kb else None
    selected_kb = str(created) if created is not None else kb_dir
    try:
        marker = _existing_marker(root)
        exact_boundary = before.project_root == root and before.source in {
            "explicit", "off-boundary"
        }
        if before.source == "off-boundary" and exact_boundary:
            if policy is not None:
                project_config.replace_off_boundary(root, policy=policy)
                return project_config.authorize_scope(
                    root,
                    kb_dir=(
                        selected_kb
                        if policy == project_config.POLICY_PRIVATE
                        else None
                    ),
                ), created
            return project_config.remove_off_boundary(root), created
        if before.state == project_config.MODE_UNLATCHED and exact_boundary:
            if before.scope_id is None:
                return project_config.remove_off_boundary(root), created
            if policy is None and selected_kb is None:
                return project_config.set_scope_mode(root, project_config.MODE_LATCHED), created
        if before.state == project_config.MODE_LOCKED and marker is not None:
            marker_policy = marker.get("policy")
            if policy is not None and marker_policy != policy:
                raise project_config.ProjectConfigError(
                    "the requested policy conflicts with the portable scope declaration"
                )
            if (
                before.reason_code
                == project_config.LOCK_INTERRUPTED_OFF_REPLACEMENT
                and policy is not None
            ):
                project_config.replace_off_boundary(root, policy=policy)
                return project_config.authorize_scope(
                    root,
                    kb_dir=(
                        selected_kb
                        if policy == project_config.POLICY_PRIVATE
                        else None
                    ),
                ), created
            if before.reason_code == project_config.LOCK_UNAUTHORIZED_ROOT:
                return project_config.authorize_scope(root, kb_dir=selected_kb), created
            if (
                marker_policy == project_config.POLICY_SHARED
                and before.reason_code == project_config.LOCK_GLOBAL_PIN_CHANGED
            ):
                return project_config.reauthorize_shared_scope(root), created
            if marker_policy == project_config.POLICY_PRIVATE and selected_kb is not None:
                return project_config.repin_private_scope(root, selected_kb), created
            raise project_config.ProjectConfigError(
                f"scope is LOCKED and needs explicit repair: {before.reason}"
            )
        if before.state == project_config.MODE_LOCKED or not exact_boundary:
            if policy is None:
                if before.state == project_config.MODE_LATCHED:
                    return before, created
                raise project_config.ProjectConfigError(
                    "choose Shared or Private before creating this scope"
                )
            project_config.create_scope(root, policy=policy)
            return project_config.authorize_scope(root, kb_dir=selected_kb), created
        if before.policy == project_config.POLICY_PRIVATE:
            if policy == project_config.POLICY_SHARED:
                raise project_config.ProjectConfigError(
                    "a Private scope cannot transition, merge, or fall back to Shared/global"
                )
            if selected_kb is not None:
                return project_config.repin_private_scope(root, selected_kb), created
            return before, created
        if before.policy == project_config.POLICY_SHARED:
            if policy == project_config.POLICY_PRIVATE:
                if selected_kb is None:
                    raise project_config.ProjectConfigError(
                        "locking down a Shared scope requires a separate Private KB"
                    )
                return project_config.convert_shared_scope_to_private(root, selected_kb), created
            return before, created
        raise project_config.ProjectConfigError("could not determine a safe latch transition")
    except Exception:
        _rollback_empty_vault(created)
        raise


def apply_latch(
    project: str | os.PathLike[str],
    *,
    policy: str | None = None,
    kb_dir: str | None = None,
    new_kb: bool = False,
    require_explicit_scopes: bool = False,
) -> int:
    root = _selected_root(project)
    _assert_local_transition_allowed(enabling=True)
    with project_config.transition_lock(root):
        with lockfile.project_access_lock(str(root), exclusive=True):
            before = project_config.resolve(root)
            if require_explicit_scopes and before.policy == project_config.POLICY_PRIVATE:
                raise project_config.ProjectConfigError(
                    "--require-explicit-scopes must be run from a Shared or "
                    "compatibility-global location, not from a Private scope"
                )
            if (
                require_explicit_scopes
                and project_config.read_machine_policy()
                == project_config.MACHINE_POLICY_COMPATIBILITY
                and (
                    before.state != project_config.MODE_LATCHED
                    or before.policy != project_config.POLICY_SHARED
                    or before.lock_key != "shared-global"
                )
            ):
                raise project_config.ProjectConfigError(
                    "--require-explicit-scopes migration requires a healthy "
                    "Shared or compatibility-global location"
                )
            roots_to_enable = (
                [root]
                if before.source == "off-boundary"
                else _instruction_roots(before)
                if before.state == project_config.MODE_UNLATCHED
                else [root]
            )
            messages: list[str] = []
            if before.state == project_config.MODE_UNLATCHED:
                messages = _enable_instructions(roots_to_enable)
            try:
                target, _created = _configure_or_change_scope(
                    root,
                    policy=policy,
                    kb_dir=kb_dir,
                    new_kb=new_kb,
                )
                if (
                    target.state == project_config.MODE_UNLATCHED
                    and target.scope_id is not None
                    and target.source == "explicit"
                ):
                    target = project_config.set_scope_mode(
                        root, project_config.MODE_LATCHED
                    )
                if require_explicit_scopes:
                    if target.source != "explicit" or target.state != project_config.MODE_LATCHED:
                        raise project_config.ProjectConfigError(
                            "authorize this root as Shared or Private before requiring explicit scopes"
                        )
                    project_config.write_machine_policy(
                        project_config.MACHINE_POLICY_EXPLICIT
                    )
                    target = project_config.resolve(root)
            except Exception:
                if before.state == project_config.MODE_UNLATCHED:
                    with contextlib.suppress(Exception):
                        _disable_instructions(roots_to_enable)
                raise
        for message in messages:
            print(f"  {message}")
        print(f"Latch is LATCHED for: {target.project_root}")
        print(f"Policy: {target.policy.upper() if target.policy else 'NONE'}")
        print(f"KB: {target.kb_dir}")
        print(f"Scope: {target.scope_id or target.source}")
        print("No KB content was copied, imported, merged, or deleted.")
        if before.revision != target.revision or before.state != target.state:
            print("Start a fresh agent task in this scope; do not resume the old task.")
        return 0


def apply_unlatch(project: str | os.PathLike[str]) -> int:
    root = _selected_root(project)
    _assert_local_transition_allowed(enabling=False)
    with project_config.transition_lock(root):
        with lockfile.project_access_lock(str(root), exclusive=True):
            before = project_config.resolve(root)
            if before.state == project_config.MODE_LOCKED:
                raise project_config.ProjectConfigError(
                    f"this location already has no Latch data-plane access: {before.reason}"
                )
            exact_scope = (
                before.source == "explicit"
                and before.project_root == root
                and before.scope_id is not None
            )
            if exact_scope:
                target = project_config.set_scope_mode(root, project_config.MODE_UNLATCHED)
                roots = _instruction_roots(target)
            elif before.source == "off-boundary" and before.project_root == root:
                target = before
                roots = [root]
            else:
                target = project_config.create_off_boundary(root)
                roots = [root]
            try:
                messages = _disable_instructions(roots)
            except Exception:
                # Runtime state remains OFF.  That is safer than rolling data-plane
                # access back on while native instructions are in an unknown state.
                raise
        for message in messages:
            print(f"  {message}")
        print(f"Latch is UNLATCHED for: {target.project_root}")
        print("KB data and the remembered binding are unchanged.")
        print("Other scopes are unchanged; authorized aliases of this same scope share its mode.")
        print("Start a fresh agent task in this root; do not resume the old task.")
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or change one explicit Latch filesystem scope."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--project", default=os.getcwd())
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--intent", choices=("latch", "unlatch"))

    latch_parser = sub.add_parser("latch")
    latch_parser.add_argument("--project", default=os.getcwd())
    latch_parser.add_argument("--confirm", required=True)
    choice = latch_parser.add_mutually_exclusive_group()
    choice.add_argument("--shared", action="store_true")
    choice.add_argument("--private", action="store_true")
    latch_parser.add_argument("--kb-dir")
    latch_parser.add_argument("--new-kb", action="store_true")
    latch_parser.add_argument("--require-explicit-scopes", action="store_true")

    unlatch_parser = sub.add_parser("unlatch")
    unlatch_parser.add_argument("--project", default=os.getcwd())
    unlatch_parser.add_argument("--confirm", required=True)

    check_parser = sub.add_parser("is-unlatched", help=argparse.SUPPRESS)
    check_parser.add_argument("--project", default=os.getcwd())
    root_parser = sub.add_parser("root", help=argparse.SUPPRESS)
    root_parser.add_argument("--project", default=os.getcwd())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "status":
            return status(
                args.project,
                as_json=args.json,
                intent=args.intent,
            )
        if args.command == "is-unlatched":
            return 0 if project_config.resolve(args.project).state == project_config.MODE_UNLATCHED else 1
        if args.command == "root":
            print(project_config.resolve(args.project).project_root)
            return 0
        if args.command == "unlatch":
            if args.confirm != "unlatch":
                raise project_config.ProjectConfigError(
                    "unlatch confirmation must be exactly 'unlatch'"
                )
            return apply_unlatch(args.project)
        if args.confirm != "latch":
            raise project_config.ProjectConfigError(
                "latch confirmation must be exactly 'latch'"
            )
        policy = (
            project_config.POLICY_SHARED
            if args.shared
            else project_config.POLICY_PRIVATE
            if args.private
            else None
        )
        return apply_latch(
            args.project,
            policy=policy,
            kb_dir=args.kb_dir,
            new_kb=args.new_kb,
            require_explicit_scopes=args.require_explicit_scopes,
        )
    except (project_config.ProjectConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
