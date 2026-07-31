"""Focused regressions for MCP lifecycle ownership and failure receipts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import mcp_broker  # noqa: E402
import mcp_daemon  # noqa: E402
import mcp_launcher_win  # noqa: E402
import mcp_proxy  # noqa: E402
import mcp_runtime  # noqa: E402


def test_windows_daemon_creation_flags_suppress_console_without_detaching():
    flags = mcp_broker._windows_creation_flags()
    assert flags & mcp_broker.WINDOWS_CREATE_NO_WINDOW
    assert flags & mcp_broker.WINDOWS_CREATE_NEW_PROCESS_GROUP
    assert not (flags & 0x00000008)  # Do not restore the old detached launch mode.


def test_windows_base_command_bypasses_venv_redirector(monkeypatch, tmp_path):
    monkeypatch.delenv(
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV, raising=False
    )
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_python = base_dir / "python.exe"
    base_python.write_bytes(b"")

    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    venv_python = venv_dir / "Scripts" / "python.exe"

    monkeypatch.setattr(
        mcp_broker.sys, "_base_executable", str(base_python), raising=False
    )
    monkeypatch.setattr(mcp_broker.sys, "base_prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "prefix", str(venv_dir))
    monkeypatch.setattr(mcp_broker.sys, "executable", str(venv_python))

    env = {"PYTHONPATH": "/poison"}
    assert mcp_broker._windows_base_command(env) == str(base_python)
    assert env["PYTHONPATH"] == str(site_packages)


@pytest.mark.parametrize("stale_exists", [False, True])
def test_windows_base_command_prefers_live_venv_over_stale_handoff(
    monkeypatch, tmp_path, stale_exists
):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_python = base_dir / "python.exe"
    base_python.write_bytes(b"")
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    stale = tmp_path / "missing-site-packages"
    if stale_exists:
        stale.mkdir()

    monkeypatch.setattr(
        mcp_broker.sys, "_base_executable", str(base_python), raising=False
    )
    monkeypatch.setattr(mcp_broker.sys, "base_prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "prefix", str(venv_dir))
    monkeypatch.setenv(
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV, str(stale)
    )

    env = {}
    assert mcp_broker._windows_base_command(env) == str(base_python)
    assert env["PYTHONPATH"] == str(site_packages)
    assert (
        env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV]
        == str(site_packages)
    )


def test_windows_base_helper_reinjects_explicit_venv_site_packages(
    monkeypatch, tmp_path
):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_python = base_dir / "python.exe"
    base_python.write_bytes(b"")
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(
        mcp_broker.sys, "_base_executable", str(base_python), raising=False
    )
    monkeypatch.setattr(mcp_broker.sys, "base_prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "executable", str(base_python))

    env = {"PYTHONPATH": "/helper/poison"}
    assert mcp_broker._windows_base_command(
        env, site_packages=str(site_packages)
    ) == str(base_python)
    assert env["PYTHONPATH"] == str(site_packages)

    with pytest.raises(mcp_broker.BrokerError):
        mcp_broker._windows_base_command(
            {}, site_packages=str(tmp_path / "missing")
        )


def test_windows_launcher_exports_private_venv_site_packages(monkeypatch, tmp_path):
    venv_dir = tmp_path / "venv"
    site_packages = venv_dir / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(mcp_launcher_win.sys, "prefix", str(venv_dir))
    child_env = mcp_launcher_win._child_environment({
        "PYTHONPATH": "/caller/poison",
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV: "/stale/handoff",
    })
    assert child_env["PYTHONPATH"].split(os.pathsep)[0] == str(site_packages)
    assert (
        child_env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV]
        == str(site_packages)
    )


def test_windows_launcher_drops_stale_handoff_without_live_venv(monkeypatch):
    monkeypatch.setattr(mcp_launcher_win, "_venv_site_packages", lambda: None)
    child_env = mcp_launcher_win._child_environment({
        "PYTHONPATH": "/caller/path",
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV: "/stale/handoff",
    })
    assert child_env["PYTHONPATH"] == "/caller/path"
    assert mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV not in child_env


def test_windows_base_proxy_handoff_reaches_daemon_environment(
    monkeypatch, tmp_path
):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    base_python = base_dir / "python.exe"
    base_python.write_bytes(b"")
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    monkeypatch.setattr(
        mcp_broker.sys, "_base_executable", str(base_python), raising=False
    )
    monkeypatch.setattr(mcp_broker.sys, "base_prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "executable", str(base_python))
    monkeypatch.setenv(
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV, str(site_packages)
    )
    daemon_env = {"PYTHONPATH": "/proxy/poison"}
    assert mcp_broker._windows_base_command(daemon_env) == str(base_python)
    assert daemon_env["PYTHONPATH"] == str(site_packages)
    assert (
        daemon_env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV]
        == str(site_packages)
    )


def test_windows_daemon_processes_private_venv_pth_before_anyio(
    monkeypatch, tmp_path
):
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setenv(
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV, str(site_packages)
    )
    activated = []
    import site

    monkeypatch.setattr(site, "addsitedir", activated.append)
    assert (
        mcp_runtime.activate_windows_venv_site_packages(str(site_packages))
        == str(site_packages)
    )
    assert activated == [str(site_packages)]

    source = (ROOT / "src" / "mcp_daemon.py").read_text(encoding="utf-8")
    windows_main = source.index(
        'if os.name == "nt" and __name__ == "__main__":'
    )
    activation_call = source.index(
        "        _activate_windows_venv_site_packages()", windows_main
    )
    assert windows_main < activation_call < source.index("import anyio")
    legacy_source = (ROOT / "src" / "mcp_server.py").read_text(encoding="utf-8")
    legacy_guard = legacy_source.index('    if os.name == "nt":')
    legacy_call = legacy_source.index(
        "mcp_runtime.activate_windows_venv_site_packages", legacy_guard
    )
    assert legacy_guard < legacy_call < legacy_source.index(
        "from mcp.server.fastmcp"
    )


def test_windows_activation_rejects_unvalidated_handoff(
    monkeypatch, tmp_path
):
    stale = tmp_path / "missing-site-packages"
    activated = []
    import site

    monkeypatch.setattr(site, "addsitedir", activated.append)
    with pytest.raises(ValueError, match="invalid Windows venv"):
        mcp_runtime.activate_windows_venv_site_packages(str(stale))
    assert activated == []


def test_windows_daemon_invalid_handoff_publishes_start_failure(
    monkeypatch, tmp_path
):
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    stale = tmp_path / "missing-site-packages"
    monkeypatch.setattr(mcp_broker.sys, "base_prefix", str(base_dir))
    monkeypatch.setattr(mcp_broker.sys, "prefix", str(base_dir))
    monkeypatch.setenv(
        mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV, str(stale)
    )
    published = []
    events = []
    monkeypatch.setattr(
        mcp_daemon.mcp_broker,
        "publish_start_failure",
        lambda runtime_key, message: published.append((runtime_key, message)),
    )
    monkeypatch.setattr(
        mcp_daemon.mcp_broker,
        "emit_lifecycle",
        lambda event, **fields: events.append((event, fields)),
    )

    with pytest.raises(
        mcp_broker.BrokerError,
        match="invalid Windows venv handoff",
    ):
        mcp_daemon._activate_windows_venv_site_packages()
    assert published == [(
        mcp_daemon._REQUESTED_RUNTIME_KEY,
        "Latch received an invalid Windows venv handoff. Reinstall Latch "
        "and start a fresh task.",
    )]
    assert events == [(
        "daemon_start_failed",
        {"reason": published[0][1]},
    )]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows import contract")
def test_windows_plain_daemon_import_ignores_stale_handoff(tmp_path):
    env = os.environ.copy()
    env.pop("LATCH_MCP_DAEMONIZE", None)
    env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV] = str(
        tmp_path / "missing-site-packages"
    )
    proc = subprocess.run(
        [sys.executable, "-c", "import mcp_daemon"],
        cwd=str(ROOT / "src"),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Windows startup contract")
def test_windows_daemon_execution_publishes_invalid_handoff(tmp_path):
    base_python = Path(
        getattr(sys, "_base_executable", "") or sys.base_prefix
    )
    if base_python.is_dir():
        base_python /= "python.exe"
    assert base_python.is_file()
    test_root = mcp_broker.paths.validated_test_root()
    assert test_root is not None
    vault = (
        test_root
        / "vaults"
        / f"invalid-handoff-{os.getpid()}-{time.time_ns()}"
    )
    vault.mkdir(parents=True)
    runtime_key = "invalid-handoff-test"
    env = os.environ.copy()
    env.pop("LATCH_MCP_DAEMONIZE", None)
    env["LATCH_HOME"] = str(ROOT)
    env["LATCH_KB_DIR"] = str(vault)
    env["PYTHONPATH"] = str(ROOT / "src")
    env["LATCH_MCP_RUNTIME_KEY"] = runtime_key
    env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV] = str(
        tmp_path / "missing-site-packages"
    )

    started = time.monotonic()
    proc = subprocess.run(
        [str(base_python), str(ROOT / "src" / "mcp_daemon.py")],
        cwd=str(ROOT / "src"),
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode != 0
    assert elapsed < 5
    message = (
        "Latch received an invalid Windows venv handoff. Reinstall Latch "
        "and start a fresh task."
    )
    assert message in proc.stderr
    receipt = (
        vault
        / "runtime"
        / mcp_broker.RUNTIME_REGISTRY_DIR
        / runtime_key
        / mcp_broker.DISCOVERY_FILE
    )
    assert json.loads(receipt.read_text(encoding="utf-8"))["error"] == message


def test_windows_site_packages_cli_handoff_reaches_ensure_daemon(
    monkeypatch, tmp_path
):
    site_packages = tmp_path / "venv" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)
    captured = []
    monkeypatch.setattr(
        mcp_broker,
        "ensure_daemon",
        lambda project, **kwargs: captured.append((project, kwargs)),
    )
    monkeypatch.setattr(
        mcp_broker.sys,
        "argv",
        [
            "mcp_broker.py",
            "--ensure-daemon",
            str(ROOT),
            "prompt_hook",
            "--windows-site-packages",
            str(site_packages),
        ],
    )
    assert mcp_broker._main() == 0
    assert captured == [(
        str(ROOT),
        {
            "start_reason": "prompt_hook",
            "windows_site_packages": str(site_packages),
        },
    )]


def test_daemon_environment_is_closed_and_shared_by_both_start_paths(
    monkeypatch, tmp_path
):
    source = {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "TEMP": "/safe/tmp",
        "LATCH_MCP_DAEMON_IDLE_TTL_SEC": "41",
        "LATCH_MCP_DAEMON_START_TIMEOUT_SEC": "7",
        "LATCH_IN_COMPACT": "1",
        "CLAUDE_KB_IN_COMPACT": "1",
        "LATCH_GATE_BACKEND": "codex",
        "LATCH_SESSION_ID": "session-poison",
        "LATCH_ADAPTER": "cursor",
        "LATCH_ARBITRARY_POISON": "poison",
        "PYTHONPATH": "/poison/python",
        "PYTHONHOME": "/poison/home",
        "OPENAI_API_KEY": "must-not-cross-the-boundary",
    }
    vault = tmp_path / "vault"
    vault.mkdir()
    install = tmp_path / "install"
    built = None
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setattr(mcp_broker.paths, "KB_ROOT", install)
    monkeypatch.setattr(mcp_broker, "read_discovery", lambda: None)
    monkeypatch.setattr(mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)

    with monkeypatch.context() as environment:
        for name in tuple(os.environ):
            environment.delenv(name, raising=False)
        for name, value in source.items():
            environment.setenv(name, value)
        built = mcp_broker._daemon_environment()

        captured = []

        class FakeProcess:
            pid = 12345

            def wait(self, timeout=None):
                return 0

        def fake_popen(*args, **kwargs):
            captured.append((args, dict(kwargs["env"])))
            return FakeProcess()

        environment.setattr(mcp_broker.subprocess, "Popen", fake_popen)
        mcp_broker._spawn_daemon(str(ROOT), start_reason="proxy_connect")
        assert mcp_broker.request_daemon_start(str(ROOT)) is True

    assert built == {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "TEMP": "/safe/tmp",
        "LATCH_HOME": str(install),
        "LATCH_KB_DIR": str(vault),
    }
    assert len(captured) == 2
    direct_env = captured[0][1]
    helper_env = captured[1][1]
    assert "LATCH_MCP_DAEMON_START_TIMEOUT_SEC" not in direct_env
    assert helper_env["LATCH_MCP_DAEMON_START_TIMEOUT_SEC"] == "7"
    forbidden = (
        set(source)
        - set(mcp_broker.DAEMON_OS_ENV_VARS)
        - set(mcp_broker.DAEMON_OWNER_ENV_VARS)
        - set(mcp_broker.DAEMON_HELPER_ENV_VARS)
    )
    for _args, env in captured:
        assert not (forbidden - {"PYTHONPATH"}).intersection(env), env
        assert env.get("PYTHONPATH") != source["PYTHONPATH"]
        if "PYTHONPATH" in env:
            assert env["PYTHONPATH"] == mcp_broker._windows_venv_site_packages()
            assert (
                env[mcp_runtime.WINDOWS_VENV_SITE_PACKAGES_ENV]
                == env["PYTHONPATH"]
            )
        assert env["LATCH_HOME"] == str(install)
        assert env["LATCH_KB_DIR"] == str(vault)
        assert "LATCH_MCP_DAEMON_PROCESS" not in env


def test_daemon_and_children_preserve_validated_vault_context(
    monkeypatch, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    install = tmp_path / "install"
    test_root = mcp_broker.paths.validated_test_root()
    source = {
        "PATH": "/safe/bin",
        "HOME": "/safe/home",
        "LATCH_PRODUCTION_DATA_ROOT": str(tmp_path / "production"),
        "LATCH_VAULT_REGISTRY_ROOT": str(tmp_path / "registry"),
        "LATCH_DURABILITY_ROOT": str(tmp_path / "durability"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        mcp_broker.paths.TEST_ROOT_ENV: str(test_root),
        mcp_broker.paths.TEST_CAPABILITY_ENV: os.environ[
            mcp_broker.paths.TEST_CAPABILITY_ENV
        ],
        "LATCH_ARBITRARY_POISON": "must-not-cross",
    }
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setattr(mcp_broker.paths, "KB_ROOT", install)

    built = mcp_broker._daemon_environment(source)
    expected_names = (
        "LATCH_PRODUCTION_DATA_ROOT",
        "LATCH_VAULT_REGISTRY_ROOT",
        "LATCH_DURABILITY_ROOT",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        mcp_broker.paths.TEST_ROOT_ENV,
        mcp_broker.paths.TEST_CAPABILITY_ENV,
    )
    for name in expected_names:
        assert built[name] == source[name]
    assert "LATCH_ARBITRARY_POISON" not in built

    with monkeypatch.context() as environment:
        for name in tuple(os.environ):
            environment.delenv(name, raising=False)
        for name, value in built.items():
            environment.setenv(name, value)
        context = mcp_runtime.ConnectionContext(
            connection_id="vault-context",
            project_cwd=str(ROOT),
            session_id=None,
            session_source="none",
            proxy_pid=123,
            proxy_started_at="now",
            runtime_key="test",
        )
        with mcp_runtime.bind_connection(context):
            for child in (
                mcp_runtime.connection_subprocess_environment("codex"),
                mcp_runtime.autonomous_subprocess_environment(),
            ):
                for name in expected_names:
                    assert child[name] == source[name]


def test_daemon_environment_rejects_invalid_vault_context(tmp_path):
    test_root = mcp_broker.paths.validated_test_root()
    capability = os.environ[mcp_broker.paths.TEST_CAPABILITY_ENV]
    with pytest.raises(mcp_broker.paths.UnsafeTestExecutionError):
        mcp_broker._daemon_environment({
            mcp_broker.paths.TEST_ROOT_ENV: str(test_root),
        })
    with pytest.raises(mcp_broker.paths.UnsafeTestExecutionError):
        mcp_broker._daemon_environment({
            mcp_broker.paths.TEST_ROOT_ENV: str(test_root),
            mcp_broker.paths.TEST_CAPABILITY_ENV: capability + "-forged",
        })
    with pytest.raises(mcp_broker.BrokerError, match="absolute"):
        mcp_broker._daemon_environment({
            "LATCH_DURABILITY_ROOT": "relative/backups",
        })


@pytest.mark.parametrize(
    "name",
    mcp_broker.DAEMON_VAULT_ROOT_ENV_VARS,
)
def test_vault_context_digest_changes_with_root_authority(
    monkeypatch, tmp_path, name
):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    first = os.environ.copy()
    second = os.environ.copy()
    first[name] = str(tmp_path / "first" / name.lower())
    second[name] = str(tmp_path / "second" / name.lower())

    assert mcp_broker.vault_context_digest(first) != (
        mcp_broker.vault_context_digest(second)
    )


def test_vault_context_digest_is_normalized_and_discovery_is_opaque(
    monkeypatch, tmp_path
):
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    first = os.environ.copy()
    second = os.environ.copy()
    first["LATCH_DURABILITY_ROOT"] = str(tmp_path / "roots" / ".." / "backups")
    second["LATCH_DURABILITY_ROOT"] = str(tmp_path / "backups")
    assert mcp_broker.vault_context_digest(first) == (
        mcp_broker.vault_context_digest(second)
    )

    monkeypatch.setenv("LATCH_DURABILITY_ROOT", str(tmp_path / "backups"))
    discovery = mcp_broker.publish_discovery(
        port=1234,
        token="opaque-token",
        pid=os.getpid(),
        started_at="now",
    )
    serialized = discovery.read_text(encoding="utf-8")
    capability = os.environ[mcp_broker.paths.TEST_CAPABILITY_ENV]
    assert capability not in serialized
    assert str(tmp_path / "backups") not in serialized
    assert mcp_broker._checked_discovery() is not None

    monkeypatch.setenv("LATCH_DURABILITY_ROOT", str(tmp_path / "different"))
    with pytest.raises(mcp_broker.BrokerError, match="vault context differs"):
        mcp_broker._checked_discovery()


def test_daemon_context_revalidates_typed_connection_settings():
    metadata = {
        "project_cwd": str(ROOT),
        "proxy_pid": 123,
        "in_compact": True,
        "unlatched": False,
        "disabled": False,
        "write_disabled": True,
        "in_maintenance": False,
        "gate_backend": "codex",
        "maintenance_backend": "cursor",
        "gate_classifier_timeout_s": 44,
        "gate_adversary_timeout_s": 22,
        "gate_adversary_enabled": False,
        "proxy_policy": {
            "cap": 7,
            "retire_idle_s": 11.0,
            "heartbeat_s": 3.0,
            "stale_s": 19.0,
        },
        "OPENAI_API_KEY": "ignored-secret",
        "LATCH_ARBITRARY_POISON": "ignored",
    }
    context = mcp_daemon._context_from(metadata, "typed")
    assert context.in_compact is True
    assert context.write_disabled is True
    assert context.gate_backend == "codex"
    assert context.maintenance_backend == "cursor"
    assert context.gate_classifier_timeout_s == 44
    assert context.gate_adversary_timeout_s == 22
    assert context.gate_adversary_enabled is False
    assert context.proxy_cap == 7
    assert context.proxy_stale_s == 19.0
    assert "OPENAI_API_KEY" not in context.__dict__
    assert "LATCH_ARBITRARY_POISON" not in context.__dict__

    invalid_values = (
        ("project_cwd", ""),
        ("project_cwd", "relative/path"),
        ("in_compact", "true"),
        ("disabled", 1),
        ("in_maintenance", "false"),
        ("gate_backend", "CODEX"),
        ("maintenance_backend", "unknown"),
        ("gate_classifier_timeout_s", 0),
        ("gate_adversary_timeout_s", True),
        ("gate_adversary_enabled", 1),
        ("proxy_policy", {"cap": -1}),
        ("proxy_policy", {
            "cap": 7,
            "retire_idle_s": float("inf"),
            "heartbeat_s": 3.0,
            "stale_s": 19.0,
        }),
    )
    for name, value in invalid_values:
        invalid = {**metadata, name: value}
        try:
            mcp_daemon._context_from(invalid, f"invalid-{name}")
        except ValueError:
            pass
        else:
            raise AssertionError(f"{name}={value!r} bypassed daemon validation")


def test_private_child_environment_is_validated_and_never_in_runtime_snapshot():
    metadata = {
        "child_process_env": {
            "PATH": "/client/bin",
            "OPENAI_API_KEY": "sentinel-openai-secret",
        }
    }
    private = mcp_daemon._child_environment_from(
        metadata,
        allowed_backends=frozenset({"codex"}),
    )
    assert "child_process_env" not in metadata
    context = mcp_runtime.ConnectionContext(
        connection_id="private-env",
        project_cwd=str(ROOT),
        session_id=None,
        session_source="test",
        proxy_pid=123,
        proxy_started_at="now",
        runtime_key="test",
        gate_backend="codex",
        maintenance_backend="codex",
    )
    with mcp_runtime.bind_connection(context, child_environment=private):
        snapshot = mcp_runtime.connection_snapshot()
        assert "child_environment" not in snapshot
        assert "sentinel-openai-secret" not in json.dumps(snapshot)
        child = mcp_runtime.connection_subprocess_environment("codex")
        assert child["OPENAI_API_KEY"] == "sentinel-openai-secret"
        assert "ANTHROPIC_API_KEY" not in child

    with pytest.raises(ValueError, match="unsupported child_environment"):
        mcp_runtime.validate_child_environment({"LATCH_POISON": "sentinel"})
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        mcp_runtime.validate_child_environment({"OPENAI_API_KEY": "bad\0value"})
    with pytest.raises(ValueError, match="unsupported child_environment"):
        mcp_runtime.validate_child_environment(
            {"ANTHROPIC_API_KEY": "wrong-backend-secret"},
            allowed_backends=frozenset({"codex"}),
        )
    with pytest.raises(ValueError, match="must be absolute"):
        mcp_runtime.validate_child_environment(
            {"CODEX_BIN": "codex"},
            allowed_backends=frozenset({"codex"}),
        )


def test_windows_daemon_creation_flags_are_defense_in_depth():
    flags = mcp_broker._windows_creation_flags()
    assert flags & mcp_broker.WINDOWS_CREATE_NO_WINDOW
    assert flags & mcp_broker.WINDOWS_CREATE_NEW_PROCESS_GROUP
    assert not (flags & 0x00000008)


def test_windows_launcher_diagnostic_records_process_lineage():
    source = (ROOT / "src" / "mcp_launcher_win.py").read_text(encoding="utf-8")
    for field in ("parent_pid=", "executable=", "argv=", "launching child="):
        assert field in source


def test_blue_green_registry_is_keyed_for_v1_v2_v1(monkeypatch, tmp_path):
    vault = tmp_path / "registry"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    original = mcp_broker.RUNTIME_KEY
    try:
        mcp_broker.RUNTIME_KEY = "runtime-v1"
        v1_path = mcp_broker.publish_discovery(
            port=1111, token="v1-token", pid=os.getpid(), started_at="v1"
        )
        mcp_broker.RUNTIME_KEY = "runtime-v2"
        v2_path = mcp_broker.publish_discovery(
            port=2222, token="v2-token", pid=os.getpid(), started_at="v2"
        )
        assert v1_path != v2_path
        assert mcp_broker.start_lock_path("runtime-v1") != mcp_broker.start_lock_path("runtime-v2")

        mcp_broker.RUNTIME_KEY = "runtime-v1"
        assert mcp_broker.read_discovery()["port"] == 1111
        mcp_broker.RUNTIME_KEY = "runtime-v2"
        assert mcp_broker.read_discovery()["port"] == 2222
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_daemon_owner_fence_survives_broker_death_and_releases_with_owner(
    monkeypatch, tmp_path
):
    vault = mcp_broker.paths.project_dir(str(tmp_path / "owner-fence"))
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    env = os.environ.copy()
    env.update({"LATCH_KB_DIR": str(vault), "PYTHONPATH": str(ROOT / "src")})
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import mcp_broker,time; "
                "h=mcp_broker.acquire_owner_fence(); "
                "print('held' if h else 'failed', flush=True); time.sleep(30)"
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        mcp_broker.start_lock_path().write_text(
            json.dumps({"pid": 2**30, "runtime_key": mcp_broker.RUNTIME_KEY}),
            encoding="utf-8",
        )
        assert mcp_broker._acquire_start_lock() is True
        contender = subprocess.run(
            [sys.executable, str(ROOT / "src" / "mcp_daemon.py")],
            env=env,
            capture_output=True,
            timeout=5,
        )
        assert contender.returncode == 0
        assert not mcp_broker.discovery_path().exists()
        assert mcp_broker.acquire_owner_fence() is None
        mcp_broker._release_start_lock()
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    fence = mcp_broker.acquire_owner_fence()
    assert fence is not None
    fence.close()


def test_incompatible_upgrade_fails_before_owner_fence_and_heavy_imports(
    monkeypatch, tmp_path
):
    vault = mcp_broker.paths.project_dir(str(tmp_path / "incompatible-upgrade"))
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(
        "import sys\n"
        "class BlockHeavy:\n"
        " def find_spec(self, fullname, path=None, target=None):\n"
        "  if fullname.split('.')[0] in {'anyio','mcp','numpy','onnxruntime'}:\n"
        "   raise RuntimeError('heavy import crossed upgrade preflight')\n"
        "  return None\n"
        "sys.meta_path.insert(0, BlockHeavy())\n",
        encoding="utf-8",
    )
    requested_key = "retained-runtime-v1"
    env = os.environ.copy()
    env.update({
        "LATCH_HOME": str(ROOT),
        "LATCH_KB_DIR": str(vault),
        "LATCH_MCP_RUNTIME_KEY": requested_key,
        "LATCH_MCP_PROTOCOL_VERSION": "999",
        "PYTHONPATH": os.pathsep.join((str(site_dir), str(ROOT / "src"))),
    })
    result = subprocess.run(
        [sys.executable, str(ROOT / "src" / "mcp_daemon.py")],
        env=env,
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    assert result.returncode == 1
    marker = (
        vault
        / "runtime"
        / "mcp-runtimes"
        / requested_key
        / mcp_broker.DISCOVERY_FILE
    )
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert "Start a fresh task" in payload["error"]
    current_key = mcp_broker.RUNTIME_KEY
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setattr(mcp_broker, "RUNTIME_KEY", requested_key)
    try:
        mcp_broker.ensure_daemon(str(ROOT))
    except mcp_broker.BrokerError as exc:
        assert "Start a fresh task" in str(exc)
    else:
        raise AssertionError("retained proxy did not receive actionable upgrade failure")
    assert not (
        vault
        / "runtime"
        / "mcp-runtimes"
        / current_key
        / mcp_broker.OWNER_FENCE_FILE
    ).exists()
    assert "heavy import crossed upgrade preflight" not in result.stderr


def test_live_pid_with_stale_heartbeat_does_not_hold_proxy_capacity(
    monkeypatch, tmp_path
):
    vault = tmp_path / "stale-lease"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setenv("LATCH_MCP_PROXY_STALE_SEC", "1")
    lease = mcp_broker.write_proxy_lease(
        "stale-live-pid",
        {
            "connection_id": "stale-live-pid",
            "pid": os.getpid(),
            "runtime_key": mcp_broker.RUNTIME_KEY,
            "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
            "started_epoch": time.time() - 100,
            "last_activity_epoch": time.time() - 100,
            "heartbeat_epoch": time.time() - 100,
        },
    )
    state = mcp_broker.proxy_lease_state()
    assert state["live"] == []
    assert state["stale_count"] == 1
    assert lease.exists(), "a live owner must repair its own lease"
    summary = mcp_broker.lifecycle_summary(hours=1, lease_state=state)
    assert summary["current_stale_leases"] == 1
    assert summary["max_stale_lease_age_s"] >= 99

    payload = json.loads(lease.read_text(encoding="utf-8"))
    payload["heartbeat_epoch"] = time.time()
    mcp_broker.write_proxy_lease("stale-live-pid", payload)
    assert len(mcp_broker.proxy_inventory()) == 1

    mcp_broker.emit_lifecycle("proxy_over_cap", live_leases=41, cap=32)
    mcp_broker.emit_lifecycle(
        "proxy_retired", cap=32, over_cap_duration_s=301.5, reason="idle_over_cap"
    )
    summary = mcp_broker.lifecycle_summary(hours=1)
    assert summary["proxy_high_water"] == 41
    assert summary["max_over_cap_duration_s"] == 301.5


def test_proxy_cap_is_aggregated_across_capable_runtime_aliases(monkeypatch, tmp_path):
    vault = tmp_path / "owner-lease-scope"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setenv("LATCH_MCP_PROXY_CAP", "32")
    original = mcp_broker.RUNTIME_KEY
    now = time.time()
    try:
        mcp_broker.RUNTIME_KEY = "runtime-v2"
        mcp_broker.publish_discovery(
            port=2222,
            token="owner-token",
            pid=os.getpid(),
            started_at="ready",
        )
        mcp_broker.publish_discovery(
            port=2222,
            token="owner-token",
            pid=os.getpid(),
            started_at="ready",
            runtime_key="runtime-v1",
            owner_runtime_key="runtime-v2",
        )
        for index in range(33):
            key = "runtime-v1" if index < 17 else "runtime-v2"
            mcp_broker.write_proxy_lease(
                f"lease-{index}",
                {
                    "connection_id": f"lease-{index}",
                    "pid": os.getpid(),
                    "runtime_key": key,
                    "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
                    "started_epoch": now,
                    "last_activity_epoch": now,
                    "heartbeat_epoch": now,
                    "over_cap_since_epoch": now - 10,
                },
                runtime_key=key,
            )
        mcp_broker.emit_lifecycle(
            "proxy_over_cap", live_leases=33, cap=32, runtime_key="runtime-v1"
        )
        state = mcp_broker.proxy_lease_state()
        assert len(state["live"]) == 33
        assert state["alias_runtime_keys"] == ["runtime-v1", "runtime-v2"]
        summary = mcp_broker.lifecycle_summary(hours=1, lease_state=state)
        assert summary["currently_over_cap"] is True
        assert summary["proxy_high_water"] == 33
        assert summary["lease_scope"] == "owner_runtime_key"
        retiring = mcp_proxy.ProxyLease({
            "connection_id": "lease-0",
            "proxy_pid": os.getpid(),
            "runtime_key": "runtime-v1",
            "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
        })
        retiring.last_activity_epoch = now - 301
        retiring._write()
        retiring.migrate_scope("runtime-v2")
        assert retiring.should_retire() is True
        retiring.close()
        assert mcp_broker.proxy_lease_dir("runtime-v1") != mcp_broker.proxy_lease_dir(
            "runtime-v2"
        )
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_capable_proxy_lease_migrates_write_first_and_deduplicates(
    monkeypatch, tmp_path
):
    vault = tmp_path / "lease-migration"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    original = mcp_broker.RUNTIME_KEY
    try:
        mcp_broker.RUNTIME_KEY = "owner-v2"
        mcp_broker.publish_discovery(
            port=2222,
            token="owner-token",
            pid=os.getpid(),
            started_at="ready",
        )
        mcp_broker.publish_discovery(
            port=2222,
            token="owner-token",
            pid=os.getpid(),
            started_at="ready",
            runtime_key="alias-v1",
            owner_runtime_key="owner-v2",
        )
        lease = mcp_proxy.ProxyLease({
            "connection_id": "migrating-proxy",
            "proxy_pid": os.getpid(),
            "runtime_key": "alias-v1",
            "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
        })
        lease.start()
        old_path = mcp_broker.proxy_lease_dir("alias-v1") / "migrating-proxy.json"
        new_path = mcp_broker.proxy_lease_dir("owner-v2") / "migrating-proxy.json"
        assert old_path.exists()
        lease.migrate_scope("owner-v2")
        assert new_path.exists()
        assert not old_path.exists()
        migrated = json.loads(new_path.read_text(encoding="utf-8"))
        duplicate = dict(migrated, heartbeat_epoch=migrated["heartbeat_epoch"] - 1)
        mcp_broker.write_proxy_lease(
            "migrating-proxy", duplicate, runtime_key="alias-v1"
        )
        mcp_broker.discovery_path("alias-v1").unlink()
        state = mcp_broker.proxy_lease_state()
        assert len(state["live"]) == 1
        assert state["unassociated_capable"] == []
        lease.close()
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_legacy_alias_and_pre_registry_leases_are_truthfully_reported(
    monkeypatch, tmp_path
):
    vault = tmp_path / "legacy-evidence"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    original = mcp_broker.RUNTIME_KEY
    now = time.time()
    try:
        mcp_broker.RUNTIME_KEY = "owner-v2"
        mcp_broker.publish_discovery(
            port=2222,
            token="owner-token",
            pid=os.getpid(),
            started_at="ready",
            runtime_key="legacy-v1",
            owner_runtime_key="owner-v2",
            compatibility="fresh_task_required",
        )
        legacy_payload = {
            "connection_id": "legacy-keyed",
            "pid": os.getpid(),
            "runtime_key": "legacy-v1",
            "started_epoch": now,
            "last_activity_epoch": now,
            "heartbeat_epoch": now,
        }
        mcp_broker.write_proxy_lease(
            "legacy-keyed", legacy_payload, runtime_key="legacy-v1"
        )
        pre_registry = dict(legacy_payload, connection_id="legacy-root")
        mcp_broker._atomic_json(
            mcp_broker.legacy_proxy_lease_dir() / "legacy-root.json",
            pre_registry,
        )
        state = mcp_broker.proxy_lease_state()
        assert state["live"] == []
        assert len(state["legacy_incompatible"]) == 2
        summary = mcp_broker.lifecycle_summary(hours=1, lease_state=state)
        assert summary["legacy_incompatible_leases"] == 2
        assert summary["observed_live_leases"] == 2
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_idle_historical_keyed_leases_are_visible_before_alias_or_reconnect(
    monkeypatch, tmp_path
):
    """Reproduce the final review: 2 idle old leases plus 1 fresh lease."""
    vault = tmp_path / "idle-historical-evidence"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    original = mcp_broker.RUNTIME_KEY
    now = time.time()
    try:
        mcp_broker.RUNTIME_KEY = "current-owner"
        mcp_broker.publish_discovery(
            port=2222,
            token="current-token",
            pid=os.getpid(),
            started_at="ready",
        )
        mcp_broker.write_proxy_lease(
            "current-proxy",
            {
                "connection_id": "current-proxy",
                "pid": os.getpid(),
                "runtime_key": "current-owner",
                "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
                "started_epoch": now,
                "last_activity_epoch": now,
                "heartbeat_epoch": now,
            },
        )
        for index in range(2):
            mcp_broker.write_proxy_lease(
                f"historical-{index}",
                {
                    "connection_id": f"historical-{index}",
                    "pid": os.getpid(),
                    "runtime_key": "historical-key",
                    "started_epoch": now,
                    "last_activity_epoch": now,
                    "heartbeat_epoch": now,
                },
                runtime_key="historical-key",
            )

        assert not mcp_broker.discovery_path("historical-key").exists()
        state = mcp_broker.proxy_lease_state()
        assert len(state["live"]) == 1
        assert len(state["legacy_incompatible"]) == 2
        assert len(state["observed_live"]) == 3
        assert state["alias_runtime_keys"] == ["current-owner"]
        assert state["unassociated_runtime_keys"] == ["historical-key"]
        status = mcp_daemon.mcp_server.kb_runtime_status()["proxy_pool"]
        assert status["live_leases"] == 1
        assert status["legacy_incompatible_leases"] == 2
        assert status["observed_live_leases"] == 3
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_unassociated_capable_leases_join_cap_but_other_live_owner_does_not(
    monkeypatch, tmp_path
):
    vault = tmp_path / "registry-wide-cap"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setenv("LATCH_MCP_PROXY_CAP", "1")
    original = mcp_broker.RUNTIME_KEY
    now = time.time()
    try:
        mcp_broker.RUNTIME_KEY = "current-owner"
        for index, key in enumerate(("current-owner", "orphaned-capable")):
            mcp_broker.write_proxy_lease(
                f"pool-{index}",
                {
                    "connection_id": f"pool-{index}",
                    "pid": os.getpid(),
                    "runtime_key": key,
                    "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
                    "started_epoch": now,
                    "last_activity_epoch": now,
                    "heartbeat_epoch": now,
                },
                runtime_key=key,
            )
        mcp_broker.publish_discovery(
            port=3333,
            token="other-token",
            pid=os.getpid(),
            started_at="other-ready",
            runtime_key="other-owner",
            owner_runtime_key="other-owner",
        )
        other_discovery = mcp_broker.discovery_path("other-owner")
        historical_discovery = json.loads(other_discovery.read_text(encoding="utf-8"))
        historical_discovery.pop("owner_runtime_key")
        mcp_broker._atomic_json(other_discovery, historical_discovery)
        mcp_broker.write_proxy_lease(
            "other-proxy",
            {
                "connection_id": "other-proxy",
                "pid": os.getpid(),
                "runtime_key": "other-owner",
                "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
                "started_epoch": now,
                "last_activity_epoch": now,
                "heartbeat_epoch": now,
            },
            runtime_key="other-owner",
        )

        state = mcp_broker.proxy_lease_state()
        assert len(state["live"]) == 2
        assert len(state["unassociated_capable"]) == 1
        assert len(state["other_live_owner"]) == 1
        assert len(state["observed_live"]) == 3
        assert state["other_owner_runtime_keys"] == ["other-owner"]
        summary = mcp_broker.lifecycle_summary(hours=1, lease_state=state)
        assert summary["currently_over_cap"] is True
        assert summary["unassociated_capable_leases"] == 1
        assert summary["other_live_owner_leases"] == 1
        retiring = mcp_proxy.ProxyLease({
            "connection_id": "pool-0",
            "proxy_pid": os.getpid(),
            "runtime_key": "current-owner",
            "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
        })
        retiring.last_activity_epoch = now - 301
        retiring._write()
        assert retiring.should_retire() is True
        retiring.close()
    finally:
        mcp_broker.RUNTIME_KEY = original


def test_discovery_aliases_are_not_published_before_runtime_initialization(
    monkeypatch, tmp_path
):
    vault = tmp_path / "readiness-publication"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setattr(mcp_daemon.mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setattr(mcp_daemon, "_REQUESTED_RUNTIME_KEY", "retained-v1")
    monkeypatch.setenv(
        "LATCH_MCP_PROXY_CAPABILITY_EPOCH",
        str(mcp_broker.PROXY_CAPABILITY_EPOCH),
    )

    class StopInitialization(RuntimeError):
        pass

    def fail_after_check(_cwd, *, start_embed_listener):
        assert start_embed_listener is True
        assert not mcp_broker.discovery_path().exists()
        assert not mcp_broker.discovery_path("retained-v1").exists()
        raise StopInitialization("checked before ready")

    monkeypatch.setattr(mcp_daemon.mcp_server, "initialize_runtime", fail_after_check)
    mcp_daemon._OWNER_FENCE = None
    try:
        try:
            mcp_daemon.anyio.run(mcp_daemon._main_async)
        except StopInitialization:
            pass
        else:
            raise AssertionError("synthetic initialization failure was swallowed")
    finally:
        if mcp_daemon._OWNER_FENCE is not None:
            mcp_daemon._OWNER_FENCE.close()
            mcp_daemon._OWNER_FENCE = None


def test_recent_lifecycle_warnings_are_chronological_across_day_files(
    monkeypatch, tmp_path
):
    vault = tmp_path / "lifecycle-order"
    vault.mkdir()
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    now = datetime.now(timezone.utc)
    today_rows = [
        {
            "ts": (now - timedelta(minutes=index)).isoformat(),
            "event": "daemon_failed",
            "runtime_key": mcp_broker.RUNTIME_KEY,
            "reason": f"today-{index}",
        }
        for index in range(11)
    ]
    yesterday_row = {
        "ts": (now - timedelta(hours=25)).isoformat(),
        "event": "daemon_failed",
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "reason": "yesterday",
    }
    mcp_broker._lifecycle_path(now).write_text(
        "\n".join(json.dumps(row) for row in today_rows) + "\n", encoding="utf-8"
    )
    mcp_broker._lifecycle_path(now - timedelta(days=1)).write_text(
        json.dumps(yesterday_row) + "\n", encoding="utf-8"
    )
    recent = mcp_broker.lifecycle_summary(hours=48)["recent_warnings"]
    reasons = {row["reason"] for row in recent}
    assert len(recent) == 10
    assert "yesterday" not in reasons
    assert "today-0" in reasons and "today-9" in reasons


def test_sustained_over_cap_duration_is_visible_from_live_leases(
    monkeypatch, tmp_path
):
    vault = tmp_path / "over-cap"
    monkeypatch.setattr(mcp_broker, "runtime_dir", lambda: vault)
    monkeypatch.setenv("LATCH_MCP_PROXY_CAP", "2")
    monkeypatch.setenv("LATCH_MCP_PROXY_STALE_SEC", "300")
    now = time.time()
    for index in range(3):
        mcp_broker.write_proxy_lease(
            f"live-{index}",
            {
                "connection_id": f"live-{index}",
                "pid": os.getpid(),
                "runtime_key": mcp_broker.RUNTIME_KEY,
                "proxy_capability_epoch": mcp_broker.PROXY_CAPABILITY_EPOCH,
                "started_epoch": now - 100 + index,
                "last_activity_epoch": now,
                "heartbeat_epoch": now,
                "over_cap_since_epoch": now - 45,
            },
        )
    mcp_broker.emit_lifecycle("proxy_over_cap", live_leases=3, cap=2)
    summary = mcp_broker.lifecycle_summary(hours=1)
    assert summary["current_live_leases"] == 3
    assert summary["currently_over_cap"] is True
    assert summary["current_over_cap_duration_s"] >= 44
    assert summary["over_cap_duration_is_lower_bound"] is True


def test_disconnect_reports_unknown_mutation_outcome_without_retry_advice(monkeypatch):
    monkeypatch.setattr(mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)
    metadata = {
        "connection_id": "receipt-test",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    }
    bridge = mcp_proxy.ProxyBridge(metadata)
    emitted: list[tuple[object, str]] = []
    bridge._emit_request_error = lambda request_id, message: emitted.append((request_id, message))
    bridge._pending = {7: "tools/call latch_insert"}
    try:
        bridge._daemon_lost("response channel closed")
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert emitted and emitted[0][0] == 7
    message = emitted[0][1].lower()
    assert "outcome is unknown" in message
    assert "inspect current latch state" in message
    assert "retry" not in message


def test_partial_replay_flush_fails_pending_and_deferred_tail(monkeypatch):
    monkeypatch.setattr(mcp_broker, "emit_lifecycle", lambda *_args, **_kwargs: None)

    class FailingSocket:
        def __init__(self):
            self.calls = 0

        def sendall(self, _line):
            self.calls += 1
            if self.calls == 3:
                raise OSError("synthetic partial flush failure")

        def close(self):
            pass

    bridge = mcp_proxy.ProxyBridge({
        "connection_id": "partial-flush",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    })
    bridge._sock = FailingSocket()
    bridge._initialized_line = b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    bridge._deferred = [
        json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}).encode()
        + b"\n"
        for request_id in (1, 2, 3)
    ]
    emitted: list[tuple[object, str]] = []
    bridge._emit_request_error = lambda request_id, message: emitted.append(
        (request_id, message)
    )
    try:
        assert bridge._finish_replay() is False
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert {request_id for request_id, _message in emitted} == {1, 2, 3}
    assert "not sent" in next(message for request_id, message in emitted if request_id == 3)


def test_reconnect_success_is_emitted_only_after_initialize_reply(monkeypatch):
    events: list[str] = []
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )

    class Socket:
        def sendall(self, _line):
            pass

        def close(self):
            pass

    bridge = mcp_proxy.ProxyBridge({
        "connection_id": "reconnect-timing",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    })
    bridge._sock = Socket()
    bridge._replaying = True
    bridge._replay_id = 17
    try:
        assert "daemon_reconnect_succeeded" not in events
        bridge._handle_daemon_line(b'{"jsonrpc":"2.0","id":17,"result":{}}\n')
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert events == ["daemon_reconnect_succeeded"]


def test_idle_reclaim_revalidates_activity_generation():
    state = mcp_daemon.DaemonState(started_at="now", idle_ttl_s=0.01)
    connection_id = state.register({"connection_id": "generation-race"})
    time.sleep(0.02)
    generation = state.idle_candidate()
    assert generation is not None
    state.request_started(connection_id, 7)

    class CancelScope:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    cancel_scope = CancelScope()
    assert state.cancel_reclaim_if_unchanged(generation, cancel_scope) is None
    assert cancel_scope.cancelled is False
    assert state.snapshot()["inflight_requests"] == 1

    state.request_finished(connection_id, 7)
    time.sleep(0.02)
    generation = state.idle_candidate()
    assert generation is not None
    assert state.cancel_reclaim_if_unchanged(generation, cancel_scope) is not None
    assert cancel_scope.cancelled is True


def test_request_stays_pending_until_response_delivery():
    state = mcp_daemon.DaemonState(started_at="now", idle_ttl_s=60)
    connection_id = state.register({"connection_id": "delivery-window"})
    state.request_started(connection_id, 9)

    class Message:
        def model_dump(self, **_kwargs):
            return {"jsonrpc": "2.0", "id": 9, "result": {}}

        def model_dump_json(self, **_kwargs):
            return '{"jsonrpc":"2.0","id":9,"result":{}}'

    class Session:
        message = Message()

    class Stream:
        async def send(self, _line):
            assert state.snapshot()["inflight_requests"] == 1

    async def run():
        await mcp_daemon._send_session_message(
            Stream(), state, connection_id, Session()
        )

    mcp_daemon.anyio.run(run)
    assert state.snapshot()["inflight_requests"] == 0


def test_reconnect_failure_emits_lifecycle_signal(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        mcp_broker,
        "connect_mcp",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            mcp_broker.BrokerError("synthetic reconnect failure")
        ),
    )
    metadata = {
        "connection_id": "reconnect-receipt",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    }
    bridge = mcp_proxy.ProxyBridge(metadata)
    bridge._init_line = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    bridge._init_id = 1
    emitted: list[tuple[object, str]] = []
    bridge._emit_request_error = lambda request_id, message: emitted.append((request_id, message))
    try:
        bridge._handle_host_line(
            b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
        )
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()
    assert emitted and "synthetic reconnect failure" in emitted[0][1]
    assert any(event == "daemon_reconnect_failed" for event, _fields in events)


def test_dead_local_owner_is_reconnected_before_new_request_is_sent(monkeypatch):
    metadata = {
        "connection_id": "dead-owner-preflight",
        "proxy_pid": os.getpid(),
        "runtime_key": mcp_broker.RUNTIME_KEY,
        "project_cwd": str(ROOT),
    }

    class Socket:
        def __init__(self):
            self.sent: list[bytes] = []
            self.closed = False

        def sendall(self, line):
            self.sent.append(line)

        def close(self):
            self.closed = True

    stale = Socket()
    replacement = Socket()
    bridge = mcp_proxy.ProxyBridge(metadata)
    bridge._sock = stale
    bridge._owner_pid = 12345
    bridge._init_line = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n'
    bridge._init_id = 1
    reconnects: list[bool] = []

    def reconnect(*, replay):
        reconnects.append(replay)
        bridge._sock = replacement
        bridge._owner_pid = 67890
        bridge._replaying = replay
        bridge._replay_id = bridge._init_id if replay else None

    monkeypatch.setattr(mcp_broker, "_pid_alive", lambda pid: pid != 12345)
    monkeypatch.setattr(bridge, "_connect", reconnect)
    request = b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n'
    try:
        bridge._handle_host_line(request)
    finally:
        bridge._wake_read.close()
        bridge._wake_write.close()

    assert stale.closed is True
    assert stale.sent == []
    assert reconnects == [True]
    assert bridge._deferred == [request]
    assert replacement.sent == []


def test_shared_start_failure_is_visible_and_legacy_is_opt_in(monkeypatch, capsys):
    events: list[str] = []
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )
    monkeypatch.delenv("LATCH_MCP_ALLOW_LEGACY_FALLBACK", raising=False)
    monkeypatch.delenv("LATCH_MCP_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(
        mcp_broker,
        "ensure_daemon",
        lambda _cwd, **_kwargs: (_ for _ in ()).throw(
            mcp_broker.BrokerError("synthetic failure")
        ),
    )
    legacy_called = []
    monkeypatch.setattr(mcp_proxy, "_exec_legacy_server", lambda: legacy_called.append(True))
    assert mcp_proxy.main() == 1
    assert legacy_called == []
    stderr = capsys.readouterr().err
    assert "shared MCP daemon unavailable" in stderr
    assert "LATCH_MCP_ALLOW_LEGACY_FALLBACK=1" in stderr
    assert "daemon_start_failed" in events


def test_forced_legacy_precedes_shared_connection_validation(monkeypatch):
    events: list[str] = []
    legacy_called = []
    monkeypatch.setenv("LATCH_MCP_FORCE_LEGACY", "1")
    monkeypatch.setenv("LATCH_GATE_BACKEND", "future-backend")
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )
    monkeypatch.setattr(
        mcp_broker,
        "ensure_daemon",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("forced legacy must not inspect shared runtime")
        ),
    )
    monkeypatch.setattr(
        mcp_proxy,
        "_exec_legacy_server",
        lambda: legacy_called.append(True),
    )
    assert mcp_proxy.main() == 0
    assert legacy_called == [True]
    assert events == ["legacy_fallback"]


def test_explicit_legacy_fallback_emits_lifecycle_signal(monkeypatch):
    events: list[str] = []
    monkeypatch.setenv("LATCH_MCP_ALLOW_LEGACY_FALLBACK", "1")
    monkeypatch.delenv("LATCH_MCP_FORCE_LEGACY", raising=False)
    monkeypatch.setattr(
        mcp_broker,
        "emit_lifecycle",
        lambda event, **_kwargs: events.append(event),
    )
    monkeypatch.setattr(
        mcp_broker,
        "ensure_daemon",
        lambda _cwd, **_kwargs: (_ for _ in ()).throw(
            mcp_broker.BrokerError("synthetic failure")
        ),
    )

    class LegacyExec(RuntimeError):
        pass

    monkeypatch.setattr(
        mcp_proxy,
        "_exec_legacy_server",
        lambda: (_ for _ in ()).throw(LegacyExec()),
    )
    try:
        mcp_proxy.main()
        raise AssertionError("legacy exec sentinel did not fire")
    except LegacyExec:
        pass
    assert "legacy_fallback" in events


def test_fastmcp_private_boundary_is_pinned_and_available():
    import mcp_server

    server = getattr(mcp_server.mcp, "_mcp_server", None)
    assert callable(getattr(server, "run", None))
    assert callable(getattr(server, "create_initialization_options", None))
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "mcp>=1.28.1,<1.29" in requirements
