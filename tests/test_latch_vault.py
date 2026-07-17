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


def test_vault_code_is_not_imported_by_existing_runtime() -> None:
    offenders = []
    for path in (ROOT / "src").glob("*.py"):
        if "latch_vault" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == []


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
    assert "CLAUDE_KB_DEBUG_LOG" not in env


def _subprocess_env(state) -> dict[str, str]:
    env = vault.build_environment(state)
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def test_real_latch_paths_db_logs_and_temp_are_client_local(tmp_path: Path) -> None:
    client = tmp_path / "client"
    client.mkdir()
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
        env=_subprocess_env(state),
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
    assert list(state.kb_dir.glob("runtime/mcp-runtimes/*/mcp-daemon.json"))
    assert not state.home_dir.joinpath("projects").exists()


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
    assert "Normal Latch: unchanged" in output
