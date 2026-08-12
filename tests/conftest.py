"""pytest session setup: bind the suite to a disposable authenticated KB root.

**pytest is the supported test runner for latch.** This conftest creates a
capability-bound temporary root before any test module loads.  Every KB path,
including paths resolved by child processes, stays under that root.

Directly-executed test scripts are refused unless they explicitly load the same
bootstrap before importing runtime modules.
"""
import json
import hashlib
import sys
from pathlib import Path

import pytest

# tests/ on sys.path so the shared _isolation shim imports under pytest too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _isolation  # noqa: E402  (import side-effect: isolates configuration)


@pytest.fixture(autouse=True)
def isolated_scope_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Give every test its own unchanged global Shared installation.

    Scope mode is intentionally machine-wide in production.  Sharing it across
    otherwise isolated pytest cases makes one scope test silently change the
    routing policy of hundreds of unit tests, so keep that control plane as
    isolated as the disposable vaults.  Tests which need project-scoped behavior
    replace this mode in their own fixture.

    This is deliberately normal pre-scoping product wiring: one persisted
    install pin and no project binding.  The suite must not depend on a test-only
    data-plane route that production callers could activate with environment
    variables.
    """
    import paths
    import project_config
    import install_engine

    test_root = paths.validated_test_root()
    assert test_root is not None
    key = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()
    install_home = tmp_path / "autouse-latch-home"
    install_home.mkdir(exist_ok=True)
    vault = test_root / "vaults" / "autouse" / key
    vault.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        project_config.CONTROL_ROOT_ENV,
        str(test_root / "scope-control" / key),
    )
    monkeypatch.setenv("LATCH_HOME", str(install_home))
    monkeypatch.setattr(paths, "KB_LOCATION_FILE", install_home / "kb_location.json")
    monkeypatch.setattr(
        install_engine,
        "KB_LOCATION_PATH",
        install_home / "kb_location.json",
    )
    monkeypatch.setattr(paths, "_PINNED_DIR", False)
    (install_home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(vault)}) + "\n",
        encoding="utf-8",
    )
    project_config.write_machine_policy(project_config.MACHINE_POLICY_SHARED)


@pytest.fixture
def compatibility_scope_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Path]:
    """Provide the unchanged global Shared policy used by existing installs.

    The fixture name is retained temporarily because many runtime suites consume
    it only as an isolated global KB; it no longer creates compatibility state.
    """
    import paths
    import project_config

    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "compatibility-tests" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir(exist_ok=True)
    vault = test_root / "vaults" / "compatibility" / tmp_path.name
    vault.mkdir(parents=True, exist_ok=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(vault)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    monkeypatch.delenv("CLAUDE_KB_HOME", raising=False)
    monkeypatch.delenv("LATCH_KB_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_KB_DIR", raising=False)
    project_config.write_machine_policy(project_config.MACHINE_POLICY_SHARED)
    return {"control": control, "home": home, "vault": vault}
