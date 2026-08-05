"""Hermetic acceptance tests for the remote one-command bootstraps.

The POSIX tests use a tiny local Git origin and a fake uv executable, so they
exercise acquisition, runtime setup, quickstart handoff, reruns, upgrades, and
failure recovery without network access or real agent configuration changes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "public-release-hygiene.yml"

sys.path.insert(0, str(ROOT / "src"))

from versioning import LATCH_VERSION  # noqa: E402


def run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None):
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def git(repo: Path, *args: str) -> str:
    result = run("git", "-C", str(repo), *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    result = run("git", "init", "-b", "main", str(origin))
    if result.returncode != 0:
        assert run("git", "init", str(origin)).returncode == 0
        assert run("git", "-C", str(origin), "checkout", "-b", "main").returncode == 0
    git(origin, "config", "user.email", "latch-tests@example.invalid")
    git(origin, "config", "user.name", "Latch Tests")
    (origin / "src").mkdir()
    (origin / "src" / "quickstart.py").write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

payload = {
    "argv": sys.argv[1:],
    "latch_home": os.environ.get("LATCH_HOME"),
    "latch_python": os.environ.get("LATCH_PYTHON"),
}
config_file = os.environ.get("FAKE_CONFIG_FILE")
if config_file:
    path = Path(config_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("configured\\n", encoding="utf-8")
with Path(os.environ["FAKE_QUICKSTART_LOG"]).open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload) + "\\n")
raise SystemExit(int(os.environ.get("FAKE_QUICKSTART_FAIL", "0")))
""",
        encoding="utf-8",
    )
    (origin / "src" / "doctor.py").write_text(
        """import os
from pathlib import Path

OK = "OK"
_VEC_PROBE = "existing sqlite-vec probe"


def _run_probe(code, ok_token, timeout, arch_hint):
    assert code == _VEC_PROBE
    assert ok_token == "VEC_OK"
    assert timeout == 30
    assert arch_hint is True
    installed_version = (Path(__file__).resolve().parents[1] / "VERSION").read_text().strip()
    if (
        os.environ.get("FAKE_VEC_PROBE_FAIL")
        or os.environ.get("FAKE_VEC_PROBE_FAIL_VERSION") == installed_version
    ):
        return "FAIL", "RuntimeError: SQLite extension loading is disabled"
    return OK, ""
""",
        encoding="utf-8",
    )
    (origin / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (origin / "WIRING_VERSION").write_text("3\n", encoding="utf-8")
    (origin / "requirements.txt").write_text("\n", encoding="utf-8")
    (origin / "requirements.lock").write_text(
        "# hermetic empty runtime lock\n", encoding="utf-8",
    )
    (origin / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    git(origin, "add", ".")
    git(origin, "commit", "-m", "fixture v1")
    return origin


def make_fake_uv(tmp_path: Path) -> Path:
    fake_uv = tmp_path / "fake-uv"
    python = shlex.quote(sys.executable)
    fake_uv.write_text(
        f"""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"
case "$1" in
  venv)
    if [ "${{FAKE_UV_FAIL:-}}" = "venv" ]; then exit 41; fi
    target="${{@:$#}}"
    mkdir -p "$target/bin"
    ln -sf {python} "$target/bin/python"
    ;;
  pip)
    if [ "${{FAKE_UV_FAIL:-}}" = "pip" ]; then exit 42; fi
    ;;
  *)
    echo "unexpected fake uv command: $*" >&2
    exit 43
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    return fake_uv


def installer_env(tmp_path: Path, origin: Path, fake_uv: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "LATCH_INSTALL_REPOSITORY": origin.as_uri(),
        "LATCH_UV": str(fake_uv),
        "FAKE_UV_LOG": str(tmp_path / "uv.log"),
        "FAKE_QUICKSTART_LOG": str(tmp_path / "quickstart.log"),
    })
    return env


def invoke_installer(
    *,
    tmp_path: Path,
    origin: Path,
    fake_uv: Path,
    extra: tuple[str, ...] = (),
    env_extra: dict[str, str] | None = None,
    use_existing_uv: bool = True,
    shell: str = "bash",
):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    app = tmp_path / "managed root" / "Latch app"
    env = installer_env(tmp_path, origin, fake_uv)
    if not use_existing_uv:
        env.pop("LATCH_UV")
    env.update(env_extra or {})
    result = run(
        shell,
        str(INSTALL_SH),
        "--install-dir",
        str(app),
        "--project",
        str(project),
        *extra,
        cwd=project,
        env=env,
    )
    return result, app, project, env


def read_json_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_posix_bootstrap_fresh_then_idempotent_reconcile(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    first, app, project, env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Latch activation complete" in first.stdout
    assert git(app, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")
    calls = read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))
    assert calls[0]["argv"][0] == "--project"
    assert Path(calls[0]["argv"][1]).resolve() == project.resolve()
    assert calls[0]["argv"][2:] == ["--agents", "codex", "--no-seed"]
    assert calls[0]["latch_home"] == str(app)
    assert calls[0]["latch_python"] == str(app / ".venv" / "bin" / "python")
    uv_first = Path(env["FAKE_UV_LOG"]).read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("venv ") for line in uv_first) == 1
    assert sum(line.startswith("pip install ") for line in uv_first) == 1
    assert "--require-hashes" in uv_first[-1]
    assert uv_first[-1].endswith("requirements.lock")

    second, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "keeping its source revision" in second.stdout
    uv_second = Path(env["FAKE_UV_LOG"]).read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("venv ") for line in uv_second) == 1
    assert sum(line.startswith("pip install ") for line in uv_second) == 2
    assert len(read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))) == 2


@pytest.mark.skipif(sys.platform != "darwin", reason="stock macOS Bash 3.2 regression")
def test_posix_bootstrap_zero_quickstart_args_on_stock_macos_bash(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)

    result, app, project, env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        shell="/bin/bash",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Latch activation complete" in result.stdout
    assert app.is_dir()
    calls = read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))
    assert calls == [{
        "argv": ["--project", str(project)],
        "latch_home": str(app),
        "latch_python": str(app / ".venv" / "bin" / "python"),
    }]


def test_posix_bootstrap_refuses_unowned_existing_directory(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    app = tmp_path / "managed root" / "Latch app"
    app.mkdir(parents=True)
    sentinel = app / "keep-me.txt"
    sentinel.write_text("user-owned\n", encoding="utf-8")

    result, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert result.returncode != 0
    assert "not a Latch Git checkout" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "user-owned\n"


def test_posix_bootstrap_runtime_failure_is_recoverable_without_config_writes(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    failed, app, _, env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={"FAKE_UV_FAIL": "pip"},
    )
    assert failed.returncode != 0
    assert "verified source checkout remains" in failed.stderr
    assert (app / ".git").is_dir()
    assert not Path(env["FAKE_QUICKSTART_LOG"]).exists()
    assert not list(app.parent.glob(".latch-install.*"))

    repaired, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert Path(env["FAKE_QUICKSTART_LOG"]).is_file()


def test_posix_sqlite_vec_preflight_blocks_before_configuration_writes(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    config_file = tmp_path / "home" / ".codex" / "config.toml"
    failed, app, _, env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={
            "FAKE_CONFIG_FILE": str(config_file),
            "FAKE_VEC_PROBE_FAIL": "1",
        },
    )

    assert failed.returncode != 0
    assert "sqlite-vec capability preflight failed" in failed.stderr
    assert "without SQLite extension loading support" in failed.stderr
    assert str(app / ".venv" / "bin" / "python") in failed.stderr
    assert "No project or agent configuration was written" in failed.stderr
    assert (app / ".git").is_dir()
    assert not (app / "src" / "__pycache__").exists()
    assert not config_file.exists()
    assert not Path(env["FAKE_QUICKSTART_LOG"]).exists()

    repaired, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={"FAKE_CONFIG_FILE": str(config_file)},
    )
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert config_file.read_text(encoding="utf-8") == "configured\n"


def test_posix_uv_bootstrap_noise_does_not_pollute_executable_path(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_installer = tmp_path / "fake-uv-installer.sh"
    fake_installer.write_text(
        """#!/usr/bin/env sh
set -eu
printf '%s\\n' 'noisy uv installer status'
sha256sum -b "$FAKE_UV_SOURCE" >/dev/null
mkdir -p "$UV_UNMANAGED_INSTALL"
cp "$FAKE_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"
chmod +x "$UV_UNMANAGED_INSTALL/uv"
""",
        encoding="utf-8",
    )
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -eu
target="${@:$#}"
cp "$FAKE_UV_INSTALLER" "$target"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)

    result, app, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={
            "PATH": str(fake_bin) + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
            "FAKE_UV_INSTALLER": str(fake_installer),
            "FAKE_UV_SOURCE": str(fake_uv),
            "LATCH_UV_INSTALLER_URL": "https://example.invalid/uv-installer.sh",
        },
        use_existing_uv=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "noisy uv installer status" in result.stderr
    assert "noisy uv installer status" not in result.stdout
    assert (app.parent / "bin" / "uv").is_file()


def test_posix_bootstrap_upgrade_is_explicit_and_dirty_safe(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    first, app, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert first.returncode == 0, first.stdout + first.stderr
    old_commit = git(app, "rev-parse", "HEAD")

    refused_ref, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--ref", "main", "--agents", "codex", "--no-seed"),
    )
    assert refused_ref.returncode != 0
    assert "--ref does not change an existing install" in refused_ref.stderr
    assert git(app, "rev-parse", "HEAD") == old_commit

    local = app / "local-note.txt"
    local.write_text("preserve me\n", encoding="utf-8")

    refused, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--upgrade", "--agents", "codex", "--no-seed"),
    )
    assert refused.returncode != 0
    assert "checkout is dirty" in refused.stderr
    assert git(app, "rev-parse", "HEAD") == old_commit
    assert local.read_text(encoding="utf-8") == "preserve me\n"

    local.unlink()
    (origin / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    git(origin, "add", "VERSION")
    git(origin, "commit", "-m", "fixture v2")
    new_commit = git(origin, "rev-parse", "HEAD")
    upgraded, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--upgrade", "--ref", "main", "--agents", "codex", "--no-seed"),
    )
    assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr
    assert git(app, "rev-parse", "HEAD") == new_commit
    assert (app / "VERSION").read_text(encoding="utf-8") == "0.2.0\n"


def test_posix_failed_upgrade_vec_preflight_restores_checkout_and_runtime(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    first, app, _, env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert first.returncode == 0, first.stdout + first.stderr
    old_commit = git(app, "rev-parse", "HEAD")

    (origin / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    git(origin, "add", "VERSION")
    git(origin, "commit", "-m", "fixture v2")
    new_commit = git(origin, "rev-parse", "HEAD")
    assert new_commit != old_commit

    failed, _, _, failed_env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--upgrade", "--ref", "main", "--agents", "codex", "--no-seed"),
        env_extra={"FAKE_VEC_PROBE_FAIL_VERSION": "0.2.0"},
    )

    assert failed.returncode != 0
    assert "sqlite-vec capability preflight failed" in failed.stderr
    assert "upgrade rolled back; the previous checkout remains installed" in failed.stderr
    assert git(app, "rev-parse", "HEAD") == old_commit
    assert (app / "VERSION").read_text(encoding="utf-8") == "0.1.0\n"
    quickstart_log = Path(env["FAKE_QUICKSTART_LOG"])
    assert len(quickstart_log.read_text(encoding="utf-8").splitlines()) == 1
    uv_log = Path(failed_env["FAKE_UV_LOG"])
    uv_calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert sum(call.startswith("pip install ") for call in uv_calls) == 3


def test_posix_fresh_install_honors_immutable_release_ref(tmp_path: Path):
    origin = make_origin(tmp_path)
    release_commit = git(origin, "rev-parse", "HEAD")
    git(origin, "tag", "v0.1.0", release_commit)
    (origin / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    git(origin, "add", "VERSION")
    git(origin, "commit", "-m", "newer main after release")
    assert git(origin, "rev-parse", "HEAD") != release_commit
    fake_uv = make_fake_uv(tmp_path)

    result, app, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={"LATCH_INSTALL_REF": "v0.1.0"},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert git(app, "rev-parse", "HEAD") == release_commit
    assert (app / "VERSION").read_text(encoding="utf-8") == "0.1.0\n"

    rerun, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={"LATCH_INSTALL_REF": "v0.1.0"},
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "keeping its source revision" in rerun.stdout
    assert git(app, "rev-parse", "HEAD") == release_commit


def test_posix_bootstrap_dry_run_has_no_install_side_effect(tmp_path: Path):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    result, app, _, env = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--dry-run", "--agents", "codex"),
    )
    assert result.returncode == 0, result.stderr
    assert "no writes" in result.stdout
    assert "quickstart : --agents codex" in result.stdout
    assert not app.exists()
    assert not Path(env["FAKE_UV_LOG"]).exists()
    assert not Path(env["FAKE_QUICKSTART_LOG"]).exists()


def test_git_bash_default_converges_on_windows_local_app_data(tmp_path: Path):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "uname").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' MINGW64_NT\n",
        encoding="utf-8",
    )
    (fake_bin / "cygpath").write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' '/c/Users/Test User/AppData/Local'\n",
        encoding="utf-8",
    )
    (fake_bin / "uname").chmod(0o755)
    (fake_bin / "cygpath").chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path / "home"),
        "LOCALAPPDATA": r"C:\Users\Test User\AppData\Local",
        "PATH": str(fake_bin) + os.pathsep + env["PATH"],
    })

    result = run(
        "bash", str(INSTALL_SH), "--dry-run", "--project", str(project),
        cwd=project, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "install dir: /c/Users/Test User/AppData/Local/Latch/app" in result.stdout


def test_git_bash_drive_letter_install_dir_stays_absolute(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    result = run(
        "bash",
        str(INSTALL_SH),
        "--dry-run",
        "--project",
        str(project),
        "--install-dir",
        "C:/tools/latch",
        cwd=project,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stderr
    assert "install dir: C:/tools/latch" in result.stdout
    assert f"{project}/C:/tools/latch" not in result.stdout


def test_posix_bootstrap_canonicalizes_equivalent_git_bash_local_origins(
    tmp_path: Path,
):
    origin = make_origin(tmp_path)
    fake_uv = make_fake_uv(tmp_path)
    first, app, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
    )
    assert first.returncode == 0, first.stdout + first.stderr
    git(app, "remote", "set-url", "origin", "C:/Users/Test/origin")

    fake_bin = tmp_path / "git-bash-bin"
    fake_bin.mkdir()
    fake_cygpath = fake_bin / "cygpath"
    fake_cygpath.write_text(
        """#!/usr/bin/env bash
set -eu
[ "$1" = "-am" ]
case "$2" in
  C:/Users/Test/origin|/c/Users/Test/origin)
    printf '%s\\n' 'C:/Users/Test/origin'
    ;;
  *)
    exit 3
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_cygpath.chmod(0o755)

    rerun, _, _, _ = invoke_installer(
        tmp_path=tmp_path,
        origin=origin,
        fake_uv=fake_uv,
        extra=("--agents", "codex", "--no-seed"),
        env_extra={
            "LATCH_INSTALL_REPOSITORY": "/c/Users/Test/origin",
            "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        },
    )

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "keeping its source revision" in rerun.stdout


def test_bootstrap_script_contracts_and_syntax():
    syntax = run("bash", "-n", str(INSTALL_SH))
    assert syntax.returncode == 0, syntax.stderr
    shell = INSTALL_SH.read_text(encoding="utf-8")
    powershell = INSTALL_PS1.read_text(encoding="utf-8")
    for text in (shell, powershell):
        assert "https://github.com/open-latch/latch.git" in text
        assert "https://astral.sh/uv/" in text
        assert "0.11.28" in text
        assert "quickstart.py" in text
        assert "from doctor import OK, _VEC_PROBE, _run_probe" in text
        assert "upgrade refused because the install checkout is dirty" in text
        assert "production KB" in text
        assert "requirements.lock" in text
        assert "requirements-ci.lock" not in text
    assert "UV_UNMANAGED_INSTALL" in shell
    assert "shasum -a 256" in shell
    assert "main() {" in shell
    assert shell.rstrip().endswith('main "$@"')
    assert "UV_UNMANAGED_INSTALL" in powershell
    assert "LOCALAPPDATA" in shell
    assert "LOCALAPPDATA" in powershell
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "one-command-bootstrap-windows:" in workflow
    assert "runs-on: windows-latest" in workflow
    assert (
        "tests/test_bootstrap_install.py::"
        "test_powershell_bootstrap_fresh_then_idempotent_on_windows"
    ) in workflow
    assert (
        "tests/test_bootstrap_install.py::"
        "test_git_bash_bootstrap_fresh_then_idempotent_on_windows"
    ) in workflow
    assert (
        "tests/test_bootstrap_install.py::"
        "test_powershell_iex_failure_preserves_session_and_environment"
    ) in workflow
    assert "one-command-bootstrap-macos:" in workflow
    assert "one-command-bootstrap (macos-latest Bash 3.2)" in workflow
    assert (
        "tests/test_bootstrap_install.py::"
        "test_posix_bootstrap_zero_quickstart_args_on_stock_macos_bash"
    ) in workflow
    assert (
        "tests/test_bootstrap_install.py::"
        "test_posix_sqlite_vec_preflight_blocks_before_configuration_writes"
    ) in workflow
    assert "uv==0.11.28" in workflow
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # The README's advertised pin must track the shipped VERSION, not a
    # placeholder — a stale pin here means users copy an install line for a
    # release that does not exist.
    assert f"LATCH_INSTALL_REF=v{LATCH_VERSION} bash" in readme
    assert f"))) -Ref v{LATCH_VERSION}" in readme
    runtime_inputs = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    ci_inputs = (ROOT / "requirements-ci.txt").read_text(encoding="utf-8")
    runtime_lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    ci_lock = (ROOT / "requirements-ci.lock").read_text(encoding="utf-8")
    for requirement in (
        "mcp==1.28.1",
        "filelock==3.29.7",
        "onnxruntime==1.23.2",
        "tokenizers==0.22.2",
        "numpy==2.4.6",
        "sqlite-vec==0.1.9",
    ):
        assert requirement in runtime_inputs
        assert requirement in ci_inputs
        assert requirement in runtime_lock
        assert requirement in ci_lock
    compatibility_inputs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "filelock>=3.29.7,<4" in compatibility_inputs
    for test_requirement in ("pytest==", "pytest-asyncio==", "iniconfig==", "pluggy=="):
        assert test_requirement not in runtime_lock
    lock_pattern = re.compile(r"^([a-z0-9][a-z0-9._-]*)==([^ ;\\]+)", re.MULTILINE)
    runtime_versions = dict(lock_pattern.findall(runtime_lock))
    ci_versions = dict(lock_pattern.findall(ci_lock))
    assert runtime_versions.items() <= ci_versions.items()
    assert set(ci_versions) - set(runtime_versions) == {
        "iniconfig", "pluggy", "pygments", "pytest", "pytest-asyncio",
    }


def test_posix_truncated_bootstrap_without_entrypoint_has_no_side_effect(tmp_path: Path):
    source = INSTALL_SH.read_text(encoding="utf-8")
    marker = '\nmain "$@"\n'
    assert source.endswith(marker)
    truncated = tmp_path / "truncated-install.sh"
    truncated.write_text(source[:-len(marker)] + "\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    app = tmp_path / "app"

    result = run(
        "bash",
        str(truncated),
        "--install-dir",
        str(app),
        "--project",
        str(project),
        cwd=project,
        env=os.environ.copy(),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not app.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell acceptance path")
def test_powershell_bootstrap_fresh_then_idempotent_on_windows(tmp_path: Path):
    origin = make_origin(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    app = tmp_path / "managed root" / "Latch app"
    fake_uv = tmp_path / "fake-uv.cmd"
    fake_uv.write_text(
        """@echo off
echo %*>>"%FAKE_UV_LOG%"
if "%1"=="venv" (
  "%FAKE_SYSTEM_PYTHON%" -m venv "%~4"
  exit /b %ERRORLEVEL%
)
if "%1"=="pip" exit /b 0
exit /b 43
""",
        encoding="utf-8",
    )
    env = installer_env(tmp_path, origin, fake_uv)
    env["FAKE_SYSTEM_PYTHON"] = sys.executable
    env["LOCALAPPDATA"] = str(tmp_path / "local-app-data")
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "Windows runner has no PowerShell executable"

    def ps_quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def invoke_powershell(
        *,
        ref: str = "main",
        project_path: Path = project,
        dry_run: bool = False,
    ):
        dry_run_arg = " -DryRun" if dry_run else ""
        command = (
            f"try {{ & {ps_quote(INSTALL_PS1)} -InstallDir {ps_quote(app)} "
            f"-Project {ps_quote(project_path)} -Ref {ps_quote(ref)} "
            f"{dry_run_arg} "
            "-QuickstartArgs @('--agents','codex','--no-seed') } "
            "catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
        )
        return run(
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
            cwd=project,
            env=env,
        )

    missing_project = tmp_path / "missing-project"
    missing = invoke_powershell(project_path=missing_project, dry_run=True)
    assert missing.returncode != 0
    assert (
        f"latch install: project directory does not exist: {missing_project}"
        in missing.stdout + missing.stderr
    )
    assert "Resolve-Path" not in missing.stdout + missing.stderr

    project_file = tmp_path / "project-file"
    project_file.write_text("not a directory\n", encoding="utf-8")
    not_directory = invoke_powershell(project_path=project_file, dry_run=True)
    assert not_directory.returncode != 0
    assert (
        f"latch install: project directory does not exist: {project_file}"
        in not_directory.stdout + not_directory.stderr
    )
    assert "Resolve-Path" not in not_directory.stdout + not_directory.stderr

    first = invoke_powershell()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Latch activation complete" in first.stdout
    assert git(app, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")
    assert git(app, "config", "--local", "--get", "latch.installRef") == "main"
    calls = read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))
    assert calls[0]["argv"] == [
        "--project", str(project), "--agents", "codex", "--no-seed",
    ]

    second = invoke_powershell()
    assert second.returncode == 0, second.stdout + second.stderr
    assert "keeping its source revision" in second.stdout
    assert len(read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))) == 2
    uv_calls = Path(env["FAKE_UV_LOG"]).read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("venv ") for line in uv_calls) == 1
    assert sum(line.startswith("pip install ") for line in uv_calls) == 2

    blocked_config = tmp_path / "blocked-config.json"
    env["FAKE_CONFIG_FILE"] = str(blocked_config)
    env["FAKE_VEC_PROBE_FAIL"] = "1"
    blocked = invoke_powershell()
    blocked_output = blocked.stdout + blocked.stderr
    assert blocked.returncode != 0
    assert "sqlite-vec capability preflight failed" in blocked_output
    assert "without SQLite extension loading support" in blocked_output
    assert not blocked_config.exists()
    assert len(read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))) == 2
    env.pop("FAKE_CONFIG_FILE")
    env.pop("FAKE_VEC_PROBE_FAIL")

    refused = invoke_powershell(ref="different-ref")
    assert refused.returncode != 0
    assert "does not change an existing install" in refused.stdout + refused.stderr
    assert git(app, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")
    assert len(read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))) == 2

    git(app, "config", "--local", "--unset", "latch.installRef")
    unrecorded = invoke_powershell()
    assert unrecorded.returncode != 0
    assert "no recorded source ref" in unrecorded.stdout + unrecorded.stderr
    assert git(app, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")
    assert len(read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell iex failure path")
def test_powershell_iex_failure_preserves_session_and_environment(tmp_path: Path):
    origin = make_origin(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    fake_uv = tmp_path / "fake-uv.cmd"
    fake_uv.write_text(
        """@echo off
echo %*>>"%FAKE_UV_LOG%"
if "%1"=="venv" (
  "%FAKE_SYSTEM_PYTHON%" -m venv "%~4"
  exit /b %ERRORLEVEL%
)
if "%1"=="pip" exit /b 0
exit /b 43
""",
        encoding="utf-8",
    )
    env = installer_env(tmp_path, origin, fake_uv)
    env.update({
        "FAKE_SYSTEM_PYTHON": sys.executable,
        "FAKE_QUICKSTART_FAIL": "7",
        "LATCH_HOME": "before-home",
        "LATCH_PYTHON": "before-python",
        "LOCALAPPDATA": str(tmp_path / "local-app-data"),
    })
    shell = shutil.which("pwsh") or shutil.which("powershell")
    assert shell, "Windows runner has no PowerShell executable"

    def ps_quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    command = (
        f"$source = Get-Content -LiteralPath {ps_quote(INSTALL_PS1)} -Raw; "
        "try { Invoke-Expression $source } catch { "
        "Write-Host ('CAUGHT=' + $_.Exception.Message) }; "
        "Write-Host 'SESSION_CONTINUED'; "
        "Write-Host ('LATCH_HOME_AFTER=' + $env:LATCH_HOME); "
        "Write-Host ('LATCH_PYTHON_AFTER=' + $env:LATCH_PYTHON); "
        "exit 0"
    )
    result = run(
        shell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        command,
        cwd=project,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "CAUGHT=latch install: activation stopped with status 7" in result.stdout
    assert "SESSION_CONTINUED" in result.stdout
    assert "LATCH_HOME_AFTER=before-home" in result.stdout
    assert "LATCH_PYTHON_AFTER=before-python" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows Git Bash acceptance path")
def test_git_bash_bootstrap_fresh_then_idempotent_on_windows(tmp_path: Path):
    bash = shutil.which("bash")
    uv = shutil.which("uv")
    assert bash, "Windows runner has no Git Bash executable"
    assert uv, "Windows runner has no uv executable"

    def bash_path(value: str | Path) -> str:
        result = run(bash, "-lc", 'cygpath -u "$1"', "bash", str(value))
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    origin = make_origin(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    app = tmp_path / "managed root" / "Latch app"
    env = installer_env(tmp_path, origin, Path(uv))
    env["LATCH_INSTALL_REPOSITORY"] = bash_path(origin)
    env["LATCH_UV"] = bash_path(uv)

    def invoke_git_bash(extra_env: dict[str, str] | None = None):
        run_env = env.copy()
        run_env.update(extra_env or {})
        return run(
            bash,
            bash_path(INSTALL_SH),
            "--install-dir",
            bash_path(app),
            "--project",
            bash_path(project),
            "--agents",
            "codex",
            "--no-seed",
            cwd=project,
            env=run_env,
        )

    first = invoke_git_bash()
    assert first.returncode == 0, first.stdout + first.stderr
    assert "Latch activation complete" in first.stdout
    assert git(app, "rev-parse", "HEAD") == git(origin, "rev-parse", "HEAD")
    calls = read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))
    assert calls[0]["argv"][0] == "--project"
    assert Path(calls[0]["argv"][1]).resolve() == project.resolve()
    assert calls[0]["argv"][2:] == ["--agents", "codex", "--no-seed"]

    second = invoke_git_bash({"UV_OFFLINE": "1"})
    assert second.returncode == 0, second.stdout + second.stderr
    assert "keeping its source revision" in second.stdout
    assert len(read_json_lines(Path(env["FAKE_QUICKSTART_LOG"]))) == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
