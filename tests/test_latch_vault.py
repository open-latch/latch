from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_PATH = ROOT / "bin" / "latch_vault.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("latch_vault", LAUNCHER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vault = _load_launcher()
import vault_policy  # noqa: E402


VAULT_ENV_NAMES = {
    "LATCH_VAULT_MODE",
    "LATCH_VAULT_PROTECTED_ROOT",
    "LATCH_VAULT_BINDING_ID",
    "LATCH_VAULT_FINGERPRINT",
    "LATCH_VAULT_DISABLE_SQLITE_VEC",
}


def _ordinary_env(*, outer_home: Path, outer_kb: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in VAULT_ENV_NAMES:
        env.pop(name, None)
    for name in (
        "CLAUDE_KB_HOME",
        "CLAUDE_KB_DIR",
        "LATCH_MCP_FORCE_LEGACY",
        "LATCH_MCP_ALLOW_LEGACY_FALLBACK",
        "LATCH_MCP_LEGACY",
    ):
        env.pop(name, None)
    env.update(
        {
            "LATCH_HOME": str(outer_home),
            "LATCH_KB_DIR": str(outer_kb),
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def test_unvaulted_metadata_and_path_outputs_remain_ordinary(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    project.mkdir()
    outer_home.mkdir()
    outer_kb.mkdir()
    code = """
import json
import mcp_proxy
import paths
metadata = mcp_proxy.connection_metadata()
print(json.dumps({
    "kb_root": str(paths.KB_ROOT),
    "project_dir": str(paths.project_dir()),
    "has_vault_binding": "vault_binding" in metadata,
    "metadata_project": metadata["project_cwd"],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=project,
        env=_ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt == {
        "kb_root": str(outer_home),
        "project_dir": str(outer_kb),
        "has_vault_binding": False,
        "metadata_project": str(project),
    }


def test_initialize_is_idempotent_and_strict(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    again = vault.initialize(client)

    assert again == state
    assert state.marker_path.stat().st_mode & 0o777 == 0o600
    for path in (state.state_dir, state.home_dir, state.kb_dir, state.temp_dir):
        assert not path.is_symlink()
        assert path.stat().st_mode & 0o777 == 0o700
    assert not state.home_dir.joinpath("src").is_symlink()
    assert state.home_dir.joinpath("src/schema.sql").read_bytes() == ROOT.joinpath(
        "src/schema.sql"
    ).read_bytes()
    assert state.home_dir.joinpath("src/schema.sql").stat().st_mode & 0o777 == 0o400
    assert not state.home_dir.joinpath("src/seed.py").exists()
    assert not state.home_dir.joinpath("src/compactor.py").exists()
    assert state.home_dir.joinpath("vendor").resolve() == (ROOT / "vendor").resolve()

    marker = json.loads(state.marker_path.read_text(encoding="utf-8"))
    marker["binding_id"] = "tampered"
    state.marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(vault.VaultError, match="does not exactly match"):
        vault.load(client)


def test_initialize_rejects_symlinked_state(tmp_path: Path) -> None:
    client = tmp_path / "client"
    elsewhere = tmp_path / "elsewhere"
    client.mkdir()
    elsewhere.mkdir()
    client.joinpath(vault.STATE_DIR_NAME).symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(vault.VaultError, match="must not be a symlink"):
        vault.initialize(client)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_runtime_rejects_widened_state_or_binding_permissions(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    state.kb_dir.chmod(0o755)
    with pytest.raises(vault_policy.VaultPolicyError, match="group or other"):
        vault_policy.load_binding(client)

    state.kb_dir.chmod(0o700)
    state.marker_path.chmod(0o644)
    with pytest.raises(vault_policy.VaultPolicyError, match="group or other"):
        vault_policy.load_binding(client)
    with pytest.raises(vault.VaultError, match="group or other"):
        vault.initialize(client)


def test_load_fails_closed_instead_of_repairing_overlay(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    state.home_dir.joinpath("vendor").unlink()

    with pytest.raises(vault.VaultError, match="must be the expected symlink"):
        vault.load(client)
    assert not state.home_dir.joinpath("vendor").exists()


def test_load_rejects_changed_private_schema(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    schema = state.home_dir / "src" / "schema.sql"
    schema.chmod(0o600)
    schema.write_text("tampered", encoding="utf-8")

    with pytest.raises(vault.VaultError, match="does not match"):
        vault.load(client)


def test_initialize_adds_only_local_git_exclude(tmp_path: Path) -> None:
    client = tmp_path / "client"
    info = client / ".git" / "info"
    info.mkdir(parents=True)

    vault.initialize(client)
    exclude = info / "exclude"
    assert f"/{vault.STATE_DIR_NAME}/" in exclude.read_text(encoding="utf-8")


def test_build_environment_overrides_every_data_root(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/keep/account-home",
        "CODEX_HOME": "/keep/client-codex-account",
        "LATCH_HOME": "/outer/latch",
        "LATCH_KB_DIR": "/outer/kb",
        "CLAUDE_KB_DEBUG_LOG": "/outer/debug.log",
    }
    env = vault.build_environment(state, base)

    assert env["HOME"] == base["HOME"]
    assert env["CODEX_HOME"] == base["CODEX_HOME"]
    assert env["LATCH_HOME"] == str(state.home_dir)
    assert env["CLAUDE_KB_HOME"] == str(state.home_dir)
    assert env["LATCH_KB_DIR"] == str(state.kb_dir)
    assert env["CLAUDE_KB_DIR"] == str(state.kb_dir)
    assert {env[name] for name in ("TMPDIR", "TEMP", "TMP")} == {str(state.temp_dir)}
    assert env["CLAUDE_BIN"] == str(vault.MODEL_BLOCKER)
    assert env["CODEX_BIN"] == str(vault.MODEL_BLOCKER)
    assert env["CURSOR_AGENT_BIN"] == str(vault.MODEL_BLOCKER)
    assert env["LATCH_DISABLE_WRITE"] == "1"
    assert env["CLAUDE_KB_DISABLE_WRITE"] == "1"
    assert env["CLAUDE_KB_GIT_SNAPSHOT"] == "0"
    assert env["LATCH_VAULT_DISABLE_SQLITE_VEC"] == "1"
    assert env["LATCH_VAULT_FINGERPRINT"] == vault_policy.load_binding(
        client
    ).fingerprint
    assert "CLAUDE_KB_DEBUG_LOG" not in env


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CLAUDE_BIN", "/usr/bin/claude"),
        ("CLAUDE_KB_GIT_SNAPSHOT", "1"),
        ("LATCH_DISABLE_WRITE", "0"),
        ("LATCH_MCP_LAUNCHER_LOG", "/tmp/client-leak.log"),
    ],
)
def test_active_binding_rejects_policy_environment_overrides(
    tmp_path: Path, name: str, value: str
) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    env = vault.build_environment(state)
    env[name] = value

    with pytest.raises(vault_policy.VaultPolicyError):
        vault_policy.enforce(client, env=env)


def _subprocess_env(
    state, base: dict[str, str] | None = None
) -> dict[str, str]:
    env = vault.build_environment(state, base)
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def test_real_latch_paths_db_logs_and_temp_are_client_local(tmp_path: Path) -> None:
    client = tmp_path / "client"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    client.mkdir()
    outer_home.mkdir()
    outer_kb.mkdir()
    outer_home.joinpath("outer-sentinel.txt").write_text("unchanged", encoding="utf-8")
    outer_kb.joinpath("outer-sentinel.txt").write_text("unchanged", encoding="utf-8")
    state = vault.initialize(client)
    code = """
import json
import tempfile
import compactor
import db
import paths

conn = db.connect()
conn.close()
compactor._log("vault-routing-proof")
print(json.dumps({
    "kb_root": str(paths.KB_ROOT),
    "project_dir": str(paths.project_dir()),
    "schema_exists": paths.SCHEMA_PATH.exists(),
    "temp_dir": tempfile.gettempdir(),
    "write_disabled": paths.is_write_disabled(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=client,
        env=_subprocess_env(
            state,
            _ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(result.stdout)

    assert receipt == {
        "kb_root": str(state.home_dir),
        "project_dir": str(state.kb_dir),
        "schema_exists": True,
        "temp_dir": str(state.temp_dir),
        "write_disabled": True,
    }
    assert state.kb_dir.joinpath("kb.db").exists()
    assert "vault-routing-proof" in state.home_dir.joinpath("compactor.log").read_text(
        encoding="utf-8"
    )
    assert not state.home_dir.joinpath("projects").exists()
    assert {
        path.name for path in outer_home.iterdir()
    } == {"outer-sentinel.txt"}
    assert {path.name for path in outer_kb.iterdir()} == {"outer-sentinel.txt"}


@pytest.mark.parametrize("backend", ["claude", "codex", "cursor"])
def test_model_backends_are_replaced_by_local_blocker(
    tmp_path: Path, backend: str
) -> None:
    client = tmp_path / backend
    client.mkdir()
    state = vault.initialize(client)
    code = f"""
import json
import model_backends

result = model_backends.invoke_prompt(
    "CLIENT_SECRET_MUST_NOT_REACH_A_PROVIDER",
    backend={backend!r},
    timeout_s=10,
)
print(json.dumps({{"text": result.text, "error": result.error, "backend": result.backend}}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=client,
        env=_subprocess_env(state),
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["text"] is None
    assert receipt["backend"] == backend
    assert "blocked a Latch-owned model subprocess" in receipt["error"]


def test_compactor_and_gate_direct_model_paths_are_blocked(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    code = """
import json
import compactor
import gate

_text1, compact_claude = compactor._invoke_claude_once("CLIENT_SECRET", timeout_s=10)
_text2, compact_codex = compactor._invoke_codex_once("CLIENT_SECRET", timeout_s=10)
_text3, gate_claude, _ = gate._invoke_claude_classifier_once(
    "CLIENT_SECRET", timeout_s=10
)
_text4, gate_codex, _ = gate._invoke_codex_classifier_once(
    "CLIENT_SECRET", timeout_s=10
)
print(json.dumps([compact_claude, compact_codex, gate_claude, gate_codex]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=client,
        env=_subprocess_env(state),
        capture_output=True,
        text=True,
        check=True,
    )
    errors = json.loads(result.stdout)
    assert len(errors) == 4
    assert all("blocked a Latch-owned model subprocess" in error for error in errors)


@pytest.mark.parametrize("host", ["claude", "codex"])
def test_vault_environment_does_not_break_host_cli(
    tmp_path: Path, host: str
) -> None:
    host_path = shutil.which(host)
    if not host_path:
        pytest.skip(f"{host} is not installed")
    client = tmp_path / host
    client.mkdir()
    state = vault.initialize(client)

    result = subprocess.run(
        [host_path, "--version"],
        cwd=client,
        env=_subprocess_env(state),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "blocked a Latch-owned model subprocess" not in result.stderr


def test_real_proxy_daemon_and_mcp_tools_stay_inside_vault(tmp_path: Path) -> None:
    from test_mcp_shared_runtime import McpClient, _stop_daemon

    client_root = tmp_path / "client"
    project = client_root / "repo"
    project.mkdir(parents=True)
    state = vault.initialize(client_root)
    client = None
    try:
        client = McpClient(
            state.kb_dir,
            "vault-integration-session",
            project_cwd=project,
            env_overrides=_subprocess_env(state),
        )
        status = client.status()
        assert status["mode"] == "shared_daemon"
        assert Path(status["project_cwd"]).resolve() == project.resolve()
        assert status["process_pid"] != client.process.pid

        inserted = client.call_tool(
            "latch_insert",
            {
                "kind": "fact",
                "title": "vault integration proof",
                "body": "client-local-only proof payload",
                "status": "staging",
            },
        )
        assert inserted["id"] > 0
        rows = client.call_tool(
            "latch_search", {"query": "client-local-only proof payload", "limit": 5}
        )
        if isinstance(rows, dict):
            rows = rows.get("result", [rows])
        assert any(row["id"] == inserted["id"] for row in rows)
    finally:
        if client is not None:
            client.close()
        _stop_daemon(state.kb_dir)

    assert state.kb_dir.joinpath("kb.db").exists()
    assert list(state.kb_dir.glob("mcp_lifecycle-*.log"))
    discoveries = list(
        state.kb_dir.glob("runtime/mcp-runtimes/*/mcp-daemon.json")
    )
    assert len(discoveries) == 1
    discovery = json.loads(discoveries[0].read_text(encoding="utf-8"))
    assert discovery["vault_binding"] == vault_policy.load_binding(
        client_root
    ).connection_metadata()
    assert not state.home_dir.joinpath("projects").exists()


def test_live_daemon_revalidates_marker_before_later_requests(tmp_path: Path) -> None:
    from test_mcp_shared_runtime import McpClient, _stop_daemon

    client_root = tmp_path / "client"
    project = client_root / "repo"
    project.mkdir(parents=True)
    state = vault.initialize(client_root)
    original_marker = state.marker_path.read_text(encoding="utf-8")
    client = None
    try:
        client = McpClient(
            state.kb_dir,
            "vault-tamper-session",
            project_cwd=project,
            env_overrides=_subprocess_env(state),
        )
        assert client.status()["mode"] == "shared_daemon"

        marker = json.loads(original_marker)
        marker["binding_id"] = "tampered-after-connect"
        state.marker_path.write_text(json.dumps(marker), encoding="utf-8")
        with pytest.raises(AssertionError, match="disconnected|failed|exited"):
            client.status()
    finally:
        state.marker_path.write_text(original_marker, encoding="utf-8")
        if client is not None:
            client.close()
        _stop_daemon(state.kb_dir)


def test_status_receipt_is_explicit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client = tmp_path / "client"
    repo = client / "repo"
    repo.mkdir(parents=True)
    state = vault.initialize(client)

    previous = Path.cwd()
    try:
        os.chdir(repo)
        assert vault.main(["status"]) == 0
    finally:
        os.chdir(previous)
    output = capsys.readouterr().out
    assert "Latch vault: READY" in output
    assert f"Protected root: {client.resolve()}" in output
    assert f"Binding: {state.binding_id}" in output
    assert "Outer KB: disconnected" in output
    assert "Automatic transcript compaction: off" in output
    assert "Uninitialized repositories: ordinary Latch behavior unchanged" in output
    assert "This initialized root: Latch requires the vault launcher" in output


def test_forgotten_launcher_blocks_outer_paths_before_import(tmp_path: Path) -> None:
    client = tmp_path / "client"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    client.mkdir()
    outer_home.mkdir()
    outer_kb.mkdir()
    vault.initialize(client)

    result = subprocess.run(
        [sys.executable, "-c", "import paths; print(paths.project_dir())"],
        cwd=client,
        env=_ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "outer Latch fallback is blocked" in result.stderr
    assert list(outer_kb.iterdir()) == []
    assert list(outer_home.iterdir()) == []


def test_forgotten_launcher_hook_payload_cannot_log_or_read_outer_kb(
    tmp_path: Path,
) -> None:
    client = tmp_path / "client"
    runner = tmp_path / "runner"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    for path in (client, runner, outer_home, outer_kb):
        path.mkdir()
    vault.initialize(client)
    payload = json.dumps({"cwd": str(client), "session_id": "forgotten-launcher"})
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "hooks" / "session_start.py")],
        cwd=runner,
        env=_ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        input=payload,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "outer Latch fallback is blocked" in result.stderr
    assert list(outer_home.iterdir()) == []
    assert list(outer_kb.iterdir()) == []


@pytest.mark.parametrize(
    "script_name",
    [
        "session_start.py",
        "user_prompt_submit.py",
        "stop.py",
        "session_end.py",
        "post_tool_use.py",
        "codex_session_start.py",
        "vscode_session_start.py",
        "cursor_session_start.py",
        "cursor_pre_tool_use.py",
        "cursor_before_submit.py",
        "cursor_post_tool_use.py",
    ],
)
def test_every_stateful_hook_validates_payload_before_outer_controls(
    tmp_path: Path, script_name: str
) -> None:
    client = tmp_path / "client"
    runner = tmp_path / "runner"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    for path in (client, runner, outer_home, outer_kb):
        path.mkdir()
    vault.initialize(client)
    payload = json.dumps(
        {
            "cwd": str(client),
            "workingDirectory": str(client),
            "workspaceRoot": str(client),
            "workspace_roots": [str(client)],
            "session_id": "forgotten-launcher",
            "conversation_id": "forgotten-launcher",
            "prompt": "change protected client code",
            "tool_name": "Write",
            "tool_input": {"path": str(client / "secret.txt")},
        }
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "hooks" / script_name)],
        cwd=runner,
        env=_ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0, (script_name, result.stdout, result.stderr)
    assert list(outer_home.iterdir()) == []
    assert list(outer_kb.iterdir()) == []


def test_deleted_marker_remains_a_fail_closed_tripwire(tmp_path: Path) -> None:
    client = tmp_path / "client"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    for path in (client, outer_home, outer_kb):
        path.mkdir()
    state = vault.initialize(client)
    state.marker_path.unlink()

    result = subprocess.run(
        [sys.executable, "-c", "import paths"],
        cwd=client,
        env=_ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "outer Latch fallback is blocked" in result.stderr
    assert list(outer_kb.iterdir()) == []


def test_status_treats_deleted_marker_as_damaged_opt_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    state.marker_path.unlink()

    assert vault.main(["status", str(client)]) == 2
    output = capsys.readouterr().err
    assert "Latch vault: BLOCKED" in output
    assert "binding is unreadable or malformed" in output


def test_copied_binding_and_nested_vaults_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    copied = tmp_path / "copied"
    nested = source / "nested"
    source.mkdir()
    copied.mkdir()
    nested.mkdir()
    state = vault.initialize(source)
    shutil.copytree(state.state_dir, copied / vault.STATE_DIR_NAME, symlinks=True)

    with pytest.raises(vault_policy.VaultPolicyError, match="does not exactly match"):
        vault_policy.load_binding(copied)
    with pytest.raises(vault.VaultError, match="nested vault"):
        vault.initialize(nested)


def test_runtime_rejects_conflicting_parent_and_child_tripwires(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    child_state = vault.initialize(child)
    vault.initialize(parent)

    with pytest.raises(vault_policy.VaultPolicyError, match="nested or conflicting"):
        vault_policy.enforce(child, env=vault.build_environment(child_state))


def test_symlinked_binding_file_is_rejected(tmp_path: Path) -> None:
    client = tmp_path / "client"
    elsewhere = tmp_path / "elsewhere.json"
    client.mkdir()
    state = vault.initialize(client)
    elsewhere.write_text(state.marker_path.read_text(encoding="utf-8"), encoding="utf-8")
    state.marker_path.unlink()
    state.marker_path.symlink_to(elsewhere)

    with pytest.raises(vault_policy.VaultPolicyError, match="regular file"):
        vault_policy.load_binding(client)


def test_cross_root_process_and_connection_metadata_are_rejected(tmp_path: Path) -> None:
    client = tmp_path / "client"
    outside = tmp_path / "outside"
    client.mkdir()
    outside.mkdir()
    state = vault.initialize(client)
    env = vault.build_environment(state)
    binding = vault_policy.load_binding(client)
    metadata = {
        "project_cwd": str(outside),
        "vault_binding": binding.connection_metadata(),
    }
    with pytest.raises(vault_policy.VaultPolicyError, match="outside"):
        vault_policy.validate_connection_metadata(metadata, env=env)

    metadata["project_cwd"] = str(client)
    metadata["vault_binding"] = {
        **binding.connection_metadata(),
        "fingerprint": "wrong",
    }
    with pytest.raises(vault_policy.VaultPolicyError, match="handshake"):
        vault_policy.validate_connection_metadata(metadata, env=env)

    with pytest.raises(vault_policy.VaultPolicyError):
        vault_policy.validate_connection_metadata(
            {"vault_binding": binding.connection_metadata()}, env=env
        )


def test_inherited_vault_environment_cannot_escape_root(tmp_path: Path) -> None:
    client = tmp_path / "client"
    outside = tmp_path / "outside"
    client.mkdir()
    outside.mkdir()
    state = vault.initialize(client)
    result = subprocess.run(
        [sys.executable, "-c", "import paths"],
        cwd=outside,
        env=_subprocess_env(state),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "outside its bound protected root" in result.stderr


def test_custom_mcp_path_override_and_legacy_server_are_rejected(
    tmp_path: Path,
) -> None:
    client = tmp_path / "client"
    outer = tmp_path / "outer"
    client.mkdir()
    outer.mkdir()
    state = vault.initialize(client)
    env = _subprocess_env(state)
    env["LATCH_KB_DIR"] = str(outer)
    env["CLAUDE_KB_DIR"] = str(outer)

    override = subprocess.run(
        [sys.executable, "-c", "import mcp_proxy"],
        cwd=client,
        env=env,
        capture_output=True,
        text=True,
    )
    assert override.returncode != 0
    assert "must be pinned" in override.stderr
    assert list(outer.iterdir()) == []

    legacy_env = _subprocess_env(state)
    legacy_env["LATCH_MCP_LEGACY"] = "1"
    legacy = subprocess.run(
        [sys.executable, str(ROOT / "src" / "mcp_server.py")],
        cwd=client,
        env=legacy_env,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert legacy.returncode != 0
    assert "LATCH_MCP_LEGACY is forbidden" in legacy.stderr


@pytest.mark.parametrize(
    "entrypoint",
    ["mcp_server.py", "mcp_daemon.py", "mcp_launcher_win.py"],
)
def test_forgotten_launcher_blocks_direct_mcp_entrypoints_before_state_access(
    tmp_path: Path, entrypoint: str
) -> None:
    client = tmp_path / "client"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    for path in (client, outer_home, outer_kb):
        path.mkdir()
    vault.initialize(client)
    env = _ordinary_env(outer_home=outer_home, outer_kb=outer_kb)
    env["LATCH_ADAPTER"] = "cursor"
    if entrypoint == "mcp_daemon.py":
        # The pre-fork policy check must reject before any background process
        # is created.
        env["LATCH_MCP_DAEMONIZE"] = "1"

    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / entrypoint)],
        cwd=client,
        env=env,
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "outer Latch fallback is blocked" in result.stderr
    assert {path.name for path in client.iterdir()} == {vault.STATE_DIR_NAME}
    assert list(outer_home.iterdir()) == []
    assert list(outer_kb.iterdir()) == []


def test_forgotten_launcher_blocks_stderr_wrapper_before_outer_log(
    tmp_path: Path,
) -> None:
    client = tmp_path / "client"
    outer_home = tmp_path / "outer-home"
    outer_kb = tmp_path / "outer-kb"
    for path in (client, outer_home, outer_kb):
        path.mkdir()
    vault.initialize(client)

    result = subprocess.run(
        [sys.executable, str(ROOT / "bin" / "run_mcp_with_stderr.py")],
        cwd=client,
        env=_ordinary_env(outer_home=outer_home, outer_kb=outer_kb),
        input="",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "outer Latch fallback is blocked" in result.stderr
    assert list(outer_home.iterdir()) == []
    assert list(outer_kb.iterdir()) == []


def test_server_policy_blocks_seed_compaction_and_outer_import(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
    state = vault.initialize(client)
    code = """
import json
import compactor
import seed
import vault_policy

seed_code = seed.main(["--project", "."])
compact = compactor.run_compaction("vault-session", ".", None)
try:
    vault_policy.require_operation_allowed("outer_import", ".")
except vault_policy.VaultPolicyError as exc:
    outer_import = str(exc)
else:
    outer_import = "allowed"
print(json.dumps({"seed": seed_code, "compact": compact, "outer_import": outer_import}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=client,
        env=_subprocess_env(state),
        capture_output=True,
        text=True,
        check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["seed"] == 2
    assert receipt["compact"]["reason"] == "vault_policy"
    assert "disabled by consultant vault server policy" in receipt["outer_import"]
    assert "latch seed blocked" in result.stderr


def test_outside_root_launcher_invocation_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = tmp_path / "client"
    outside = tmp_path / "outside"
    client.mkdir()
    outside.mkdir()
    state = vault.initialize(client)
    monkeypatch.chdir(outside)

    with pytest.raises(vault.VaultError, match="outside --root invocation"):
        vault.launch("true", state, [])
