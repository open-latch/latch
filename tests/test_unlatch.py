"""Public latch/unlatch behavior for filesystem scopes.

This file intentionally stays at the command/product-lifecycle layer. The
lower-level race, database, MCP, session, control-file, and instruction-mask
invariants live in their focused scope suites. Git metadata and files inside a
vault are not scope authority.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import paths
import project_config


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ScopeHarness:
    home: Path
    control: Path
    root: Path
    shared_kb: Path


def _directory(path: Path) -> Path:
    path.mkdir(parents=True)
    return path.resolve()


def _snapshot(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


@pytest.fixture
def scope_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ScopeHarness:
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "unlatch-scope-tests" / tmp_path.name
    vaults = test_root / "vaults" / "unlatch-scope-tests" / tmp_path.name
    shared_kb = _directory(vaults / "shared")
    (shared_kb / "global-canary.txt").write_text(
        "existing global knowledge stays intact\n",
        encoding="utf-8",
    )
    home = _directory(tmp_path / "latch-home")
    (home / "src").symlink_to(ROOT / "src", target_is_directory=True)
    pin = home / "kb_location.json"
    pin.write_text(
        json.dumps({"kb_dir": str(shared_kb)}) + "\n",
        encoding="utf-8",
    )
    root = _directory(tmp_path / "workspace")

    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    for name in (
        "CLAUDE_KB_HOME",
        "LATCH_KB_DIR",
        "CLAUDE_KB_DIR",
        "LATCH_UNLATCHED",
        "LATCH_DISABLE",
        "CLAUDE_KB_DISABLE",
        "LATCH_DISABLE_WRITE",
        "CLAUDE_KB_DISABLE_WRITE",
        "LATCH_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CLAUDECODE",
        "CURSOR_PLUGIN_ROOT",
        "LATCH_ADAPTER",
    ):
        monkeypatch.delenv(name, raising=False)
    # paths was imported before this per-test install root existed. Keep direct
    # calls aligned with the fresh subprocesses launched by the wrappers.
    monkeypatch.setattr(paths, "KB_LOCATION_FILE", pin)
    monkeypatch.setattr(paths, "_PINNED_DIR", False)

    # Model an upgraded Rob-style install: the installer has explicitly
    # persisted the exact existing global KB as compatibility authority.
    project_config.write_machine_policy(
        project_config.MACHINE_POLICY_COMPATIBILITY
    )
    project_config.initialize_compatibility_binding()
    return ScopeHarness(
        home=home,
        control=control,
        root=root,
        shared_kb=shared_kb,
    )


def _env(harness: ScopeHarness, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "CLAUDE_KB_HOME",
        "LATCH_KB_DIR",
        "CLAUDE_KB_DIR",
        "LATCH_UNLATCHED",
        "LATCH_DISABLE",
        "CLAUDE_KB_DISABLE",
        "LATCH_DISABLE_WRITE",
        "CLAUDE_KB_DISABLE_WRITE",
        "LATCH_SESSION_ID",
        "CLAUDE_CODE_SESSION_ID",
        "CODEX_THREAD_ID",
        "CLAUDECODE",
        "CURSOR_PLUGIN_ROOT",
        "LATCH_ADAPTER",
    ):
        env.pop(name, None)
    env.update({
        "LATCH_HOME": str(harness.home),
        project_config.CONTROL_ROOT_ENV: str(harness.control),
        "LATCH_PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
        **extra,
    })
    return env


def _run(
    script: str,
    harness: ScopeHarness,
    cwd: Path,
    *args: str,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(ROOT / "bin" / script), *args],
        cwd=cwd,
        env=_env(harness, **(env_extra or {})),
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"{script} failed ({result.returncode})\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _private_vault(harness: ScopeHarness, name: str) -> Path:
    return _directory(harness.shared_kb.parent / name)


def _assert_global_unchanged(harness: ScopeHarness) -> None:
    assert (harness.shared_kb / "global-canary.txt").read_text(
        encoding="utf-8"
    ) == "existing global knowledge stays intact\n"


def test_bare_status_is_read_only_and_reports_exact_compatibility_binding(
    scope_harness: ScopeHarness,
) -> None:
    before_root = _snapshot(scope_harness.root)
    before_control = _snapshot(scope_harness.control)

    result = _run("latch.sh", scope_harness, scope_harness.root)
    target = project_config.resolve(scope_harness.root)

    assert target.state == project_config.MODE_LATCHED
    assert target.policy == project_config.POLICY_SHARED
    assert target.source == project_config.SOURCE_COMPATIBILITY
    assert target.scope_id is None
    assert target.kb_dir == scope_harness.shared_kb
    assert "LATCHED" in result.stdout
    assert "SHARED" in result.stdout
    assert str(scope_harness.shared_kb) in result.stdout
    assert _snapshot(scope_harness.root) == before_root
    assert _snapshot(scope_harness.control) == before_control
    assert not (scope_harness.root / ".git").exists()
    assert not (scope_harness.root / ".latch").exists()


def test_missing_python_never_claims_a_latched_status(
    scope_harness: ScopeHarness,
) -> None:
    env = _env(scope_harness)
    env["LATCH_PYTHON"] = ""
    env["CLAUDE_KB_PYTHON"] = ""
    env["PATH"] = "/path/that/does/not/exist"

    summary = subprocess.run(
        ["/bin/bash", str(ROOT / "bin" / "latch_status.sh")],
        cwd=scope_harness.root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    bare = subprocess.run(
        ["/bin/bash", str(ROOT / "bin" / "latch.sh")],
        cwd=scope_harness.root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert "[UNKNOWN]" in summary.stdout
    assert "state    : LATCHED" not in summary.stdout
    assert bare.returncode != 0
    assert "no Python found" in bare.stderr


def test_commands_require_exact_names_and_confirmation(
    scope_harness: ScopeHarness,
) -> None:
    before = project_config.resolve(scope_harness.root)
    cases = (
        ("latch.sh", ("--confirm", "yes", "--shared")),
        ("latch.sh", ("on",)),
        ("unlatch.sh", ("--confirm", "yes")),
        ("unlatch.sh", ("off",)),
        ("unlatch.sh", ("--confirm", "unlatch", "extra")),
    )
    for script, args in cases:
        result = _run(
            script,
            scope_harness,
            scope_harness.root,
            *args,
            check=False,
        )
        assert result.returncode == 2, (script, args, result.stderr)

    after = project_config.resolve(scope_harness.root)
    assert after.revision == before.revision
    assert after.state == before.state
    assert not (scope_harness.root / ".latch").exists()


@pytest.mark.parametrize(
    "variable",
    ["LATCH_UNLATCHED", "LATCH_DISABLE", "CLAUDE_KB_DISABLE"],
)
def test_global_overrides_cannot_create_or_change_a_scope(
    scope_harness: ScopeHarness,
    variable: str,
) -> None:
    result = _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--private",
        "--new-kb",
        check=False,
        env_extra={variable: "1"},
    )

    assert result.returncode == 2
    assert "global" in result.stderr.lower()
    assert project_config.resolve(scope_harness.root).source == (
        project_config.SOURCE_COMPATIBILITY
    )
    assert not (scope_harness.root / ".latch").exists()


def test_global_disable_file_blocks_latch_but_write_off_still_allows_unlatch(
    scope_harness: ScopeHarness,
) -> None:
    disable = scope_harness.home / "DISABLE"
    disable.write_text("disabled\n", encoding="utf-8")
    blocked = _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
        check=False,
    )
    assert blocked.returncode == 2
    disable.unlink()

    write_blocked = _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
        check=False,
        env_extra={"LATCH_DISABLE_WRITE": "1"},
    )
    assert write_blocked.returncode == 2
    turned_off = _run(
        "unlatch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "unlatch",
        env_extra={"LATCH_DISABLE_WRITE": "1"},
    )
    assert "UNLATCHED" in turned_off.stdout
    assert project_config.resolve(scope_harness.root).state == (
        project_config.MODE_UNLATCHED
    )


def test_compatibility_binding_never_follows_a_changed_global_pin(
    scope_harness: ScopeHarness,
) -> None:
    original = project_config.resolve(scope_harness.root)
    assert original.kb_dir == scope_harness.shared_kb
    replacement = _private_vault(scope_harness, "replacement-global")
    (scope_harness.home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(replacement)}) + "\n",
        encoding="utf-8",
    )

    status = _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        check=False,
    )
    locked = project_config.resolve(scope_harness.root)

    assert status.returncode == 2
    assert "LOCKED" in status.stdout
    assert locked.state == project_config.MODE_LOCKED
    assert locked.source == project_config.SOURCE_COMPATIBILITY
    assert locked.kb_dir is None
    assert locked.remembered_kb_dir == scope_harness.shared_kb
    binding = json.loads(
        project_config.compatibility_binding_path().read_text(encoding="utf-8")
    )
    assert Path(str(binding["kb_dir"])) == scope_harness.shared_kb
    assert not (replacement / "kb.db").exists()
    _assert_global_unchanged(scope_harness)


def test_existing_global_user_can_require_explicit_filesystem_scopes(
    scope_harness: ScopeHarness,
) -> None:
    descendant = _directory(scope_harness.root / "projects" / "shared-work")
    outside = _directory(scope_harness.root.parent / "outside")

    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
        "--require-explicit-scopes",
    )

    root = project_config.resolve(scope_harness.root)
    child = project_config.resolve(descendant)
    unknown = project_config.resolve(outside)
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_EXPLICIT
    )
    assert root.state == project_config.MODE_LATCHED
    assert root.source == project_config.SOURCE_EXPLICIT
    assert root.policy == project_config.POLICY_SHARED
    assert root.kb_dir == scope_harness.shared_kb
    assert child.project_root == scope_harness.root
    assert child.scope_id == root.scope_id
    assert child.kb_dir == scope_harness.shared_kb
    assert unknown.state == project_config.MODE_LOCKED
    assert unknown.kb_dir is None
    _assert_global_unchanged(scope_harness)


def test_existing_global_user_can_add_one_private_client_without_migrating_all_roots(
    scope_harness: ScopeHarness,
) -> None:
    client = _directory(scope_harness.root / "nico")
    nested = _directory(client / "service")
    other = _directory(scope_harness.root / "other-work")

    _run(
        "latch.sh",
        scope_harness,
        client,
        "--confirm",
        "latch",
        "--private",
        "--new-kb",
    )

    private = project_config.resolve(client)
    inherited = project_config.resolve(nested)
    global_other = project_config.resolve(other)
    assert project_config.read_machine_policy() == (
        project_config.MACHINE_POLICY_COMPATIBILITY
    )
    assert private.state == project_config.MODE_LATCHED
    assert private.policy == project_config.POLICY_PRIVATE
    assert private.scope_id is not None
    assert private.kb_dir is not None
    assert private.kb_dir != scope_harness.shared_kb
    assert list(private.kb_dir.iterdir()) == []
    assert inherited.project_root == client
    assert inherited.scope_id == private.scope_id
    assert inherited.kb_dir == private.kb_dir
    assert global_other.source == project_config.SOURCE_COMPATIBILITY
    assert global_other.kb_dir == scope_harness.shared_kb
    _assert_global_unchanged(scope_harness)


def test_git_metadata_does_not_define_or_split_a_filesystem_scope(
    scope_harness: ScopeHarness,
) -> None:
    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
    )
    nested_repo = _directory(scope_harness.root / "vendor" / "nested-repo")
    subprocess.run(["git", "init", "-q", str(nested_repo)], check=True)

    inherited = project_config.resolve(nested_repo)

    assert inherited.project_root == scope_harness.root
    assert inherited.policy == project_config.POLICY_SHARED
    assert inherited.kb_dir == scope_harness.shared_kb


def test_private_existing_and_new_vault_choices_never_mutate_a_vault(
    scope_harness: ScopeHarness,
) -> None:
    existing_root = _directory(scope_harness.root / "existing-client")
    existing_kb = _private_vault(scope_harness, "existing-private")
    (existing_kb / "client-canary.bin").write_bytes(b"client knowledge\x00exact")
    existing_before = _snapshot(existing_kb)
    new_root = _directory(scope_harness.root / "new-client")

    _run(
        "latch.sh",
        scope_harness,
        existing_root,
        "--confirm",
        "latch",
        "--private",
        "--kb-dir",
        str(existing_kb),
    )
    _run(
        "latch.sh",
        scope_harness,
        new_root,
        "--confirm",
        "latch",
        "--private",
        "--new-kb",
    )

    existing = project_config.resolve(existing_root)
    created = project_config.resolve(new_root)
    assert existing.kb_dir == existing_kb
    assert _snapshot(existing_kb) == existing_before
    assert not (existing_kb / "kb.db").exists()
    assert created.kb_dir is not None and created.kb_dir != existing_kb
    assert list(created.kb_dir.iterdir()) == []
    _assert_global_unchanged(scope_harness)


def test_downstream_unlatch_is_an_off_boundary_not_a_new_vault(
    scope_harness: ScopeHarness,
) -> None:
    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
    )
    parent = project_config.resolve(scope_harness.root)
    child = _directory(scope_harness.root / "paused")
    nested = _directory(child / "deep")
    sibling = _directory(scope_harness.root / "active")
    private_parent = paths.validated_test_root() / "vaults" / "private"
    vaults_before = set(private_parent.iterdir()) if private_parent.exists() else set()

    _run(
        "unlatch.sh",
        scope_harness,
        child,
        "--confirm",
        "unlatch",
    )

    off = project_config.resolve(nested)
    assert off.state == project_config.MODE_UNLATCHED
    assert off.source == project_config.SOURCE_OFF_BOUNDARY
    assert off.project_root == child
    assert off.scope_id == parent.scope_id
    assert off.kb_dir is None
    assert off.remembered_kb_dir == scope_harness.shared_kb
    assert project_config.resolve(sibling).state == project_config.MODE_LATCHED
    assert not (child / ".latch" / "scope.json").exists()
    vaults_after = set(private_parent.iterdir()) if private_parent.exists() else set()
    assert vaults_after == vaults_before

    _run(
        "latch.sh",
        scope_harness,
        child,
        "--confirm",
        "latch",
    )
    resumed = project_config.resolve(nested)
    assert resumed.state == project_config.MODE_LATCHED
    assert resumed.project_root == scope_harness.root
    assert resumed.scope_id == parent.scope_id
    assert resumed.kb_dir == scope_harness.shared_kb
    assert not project_config.local_binding_path(child).exists()


def test_private_unlatch_and_latch_restore_the_exact_scope_and_kb(
    scope_harness: ScopeHarness,
) -> None:
    client = _directory(scope_harness.root / "client")
    nested = _directory(client / "src")
    kb = _private_vault(scope_harness, "client-private")
    (kb / "private-canary.txt").write_text("unchanged\n", encoding="utf-8")
    _run(
        "latch.sh",
        scope_harness,
        client,
        "--confirm",
        "latch",
        "--private",
        "--kb-dir",
        str(kb),
    )
    before = project_config.resolve(client)
    marker_before = (client / ".latch" / "scope.json").read_bytes()

    _run(
        "unlatch.sh",
        scope_harness,
        client,
        "--confirm",
        "unlatch",
    )
    off = project_config.resolve(nested)
    assert off.state == project_config.MODE_UNLATCHED
    assert off.policy == project_config.POLICY_PRIVATE
    assert off.scope_id == before.scope_id
    assert off.kb_dir is None
    assert off.remembered_kb_dir == kb

    _run(
        "latch.sh",
        scope_harness,
        client,
        "--confirm",
        "latch",
    )
    restored = project_config.resolve(nested)
    assert restored.state == project_config.MODE_LATCHED
    assert restored.project_root == client
    assert restored.policy == project_config.POLICY_PRIVATE
    assert restored.scope_id == before.scope_id
    assert restored.kb_dir == kb
    assert (client / ".latch" / "scope.json").read_bytes() == marker_before
    assert (kb / "private-canary.txt").read_text(encoding="utf-8") == (
        "unchanged\n"
    )
    _assert_global_unchanged(scope_harness)


def test_unlatch_is_scope_local_across_two_private_clients(
    scope_harness: ScopeHarness,
) -> None:
    client_a = _directory(scope_harness.root / "client-a")
    client_b = _directory(scope_harness.root / "client-b")
    kb_a = _private_vault(scope_harness, "private-a")
    kb_b = _private_vault(scope_harness, "private-b")
    (kb_a / "a.txt").write_text("A\n", encoding="utf-8")
    (kb_b / "b.txt").write_text("B\n", encoding="utf-8")
    for client, kb in ((client_a, kb_a), (client_b, kb_b)):
        _run(
            "latch.sh",
            scope_harness,
            client,
            "--confirm",
            "latch",
            "--private",
            "--kb-dir",
            str(kb),
        )

    _run(
        "unlatch.sh",
        scope_harness,
        client_a,
        "--confirm",
        "unlatch",
    )

    assert project_config.resolve(client_a).state == project_config.MODE_UNLATCHED
    active = project_config.resolve(client_b)
    assert active.state == project_config.MODE_LATCHED
    assert active.kb_dir == kb_b
    assert project_config.resolve(scope_harness.root).kb_dir == (
        scope_harness.shared_kb
    )
    assert (kb_a / "a.txt").read_text(encoding="utf-8") == "A\n"
    assert (kb_b / "b.txt").read_text(encoding="utf-8") == "B\n"
    _assert_global_unchanged(scope_harness)


def test_private_scope_never_falls_back_or_nests_a_shared_scope(
    scope_harness: ScopeHarness,
) -> None:
    client = _directory(scope_harness.root / "client")
    nested = _directory(client / "nested")
    kb = _private_vault(scope_harness, "private-client")
    _run(
        "latch.sh",
        scope_harness,
        client,
        "--confirm",
        "latch",
        "--private",
        "--kb-dir",
        str(kb),
    )
    before = project_config.resolve(client)

    convert = _run(
        "latch.sh",
        scope_harness,
        client,
        "--confirm",
        "latch",
        "--shared",
        check=False,
    )
    nested_shared = _run(
        "latch.sh",
        scope_harness,
        nested,
        "--confirm",
        "latch",
        "--shared",
        check=False,
    )

    assert convert.returncode == 2
    assert nested_shared.returncode == 2
    after = project_config.resolve(client)
    assert after.state == project_config.MODE_LATCHED
    assert after.policy == project_config.POLICY_PRIVATE
    assert after.scope_id == before.scope_id
    assert after.kb_dir == kb
    assert project_config.resolve(nested).scope_id == before.scope_id
    assert not (nested / ".latch").exists()
    _assert_global_unchanged(scope_harness)


def test_repeated_latch_and_unlatch_are_idempotent(
    scope_harness: ScopeHarness,
) -> None:
    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
    )
    latched = project_config.resolve(scope_harness.root)
    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
    )
    assert project_config.resolve(scope_harness.root).revision == latched.revision

    _run(
        "unlatch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "unlatch",
    )
    off = project_config.resolve(scope_harness.root)
    _run(
        "unlatch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "unlatch",
    )
    repeated = project_config.resolve(scope_harness.root)
    assert repeated.state == project_config.MODE_UNLATCHED
    assert repeated.revision == off.revision

    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
    )
    restored = project_config.resolve(scope_harness.root)
    assert restored.state == project_config.MODE_LATCHED
    assert restored.policy == project_config.POLICY_SHARED
    assert restored.scope_id == latched.scope_id
    assert restored.kb_dir == scope_harness.shared_kb


def test_unlatched_hook_receipt_is_bounded_to_its_filesystem_subtree(
    scope_harness: ScopeHarness,
) -> None:
    _run(
        "latch.sh",
        scope_harness,
        scope_harness.root,
        "--confirm",
        "latch",
        "--shared",
    )
    paused = _directory(scope_harness.root / "paused")
    active = _directory(scope_harness.root / "active")
    _run(
        "unlatch.sh",
        scope_harness,
        paused,
        "--confirm",
        "unlatch",
    )
    command = [sys.executable, str(ROOT / "src" / "hooks" / "session_start.py")]
    paused_result = subprocess.run(
        command,
        input=json.dumps({"cwd": str(paused), "session_id": "paused-task"}),
        cwd=paused,
        env=_env(scope_harness),
        text=True,
        capture_output=True,
        check=True,
    )
    active_result = subprocess.run(
        command,
        input=json.dumps({"cwd": str(active), "session_id": "active-task"}),
        cwd=active,
        env=_env(scope_harness),
        text=True,
        capture_output=True,
        check=True,
    )

    assert "UNLATCHED" in paused_result.stdout
    assert "UNLATCHED" not in active_result.stdout
    assert project_config.resolve(active).state == project_config.MODE_LATCHED
