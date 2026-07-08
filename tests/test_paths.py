"""Unit tests for paths.sanitize_cwd — especially the MINGW-path normalization
that prevents Windows+bash callers from disagreeing with hook-path callers."""
from __future__ import annotations

import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import _isolation  # noqa: F401,E402  (hermetic on a pinned KB for direct runs; see conftest)
import paths  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_normalize_mingw_drive_letter():
    _assert(paths._normalize_input_path("/c/Systems/foo") == "C:/Systems/foo",
            "MINGW-style /c/... should normalize to C:/...")
    _assert(paths._normalize_input_path("/d/Projects/bar") == "D:/Projects/bar",
            "MINGW-style /d/... should normalize to D:/...")
    print("PASS normalize_mingw_drive_letter")


def test_normalize_preserves_native_windows():
    _assert(paths._normalize_input_path("C:/Systems/foo") == "C:/Systems/foo", "native forward-slash untouched")
    _assert(paths._normalize_input_path("C:\\Systems\\foo") == "C:\\Systems\\foo", "native backslash untouched")
    print("PASS normalize_preserves_native_windows")


def test_normalize_preserves_non_mingw_paths():
    # Truly relative or non-windows-style paths shouldn't be rewritten.
    _assert(paths._normalize_input_path("relative/path") == "relative/path", "relative untouched")
    _assert(paths._normalize_input_path("/usr/local/bin") == "/usr/local/bin",
            "multi-char first segment not a drive letter — untouched")
    print("PASS normalize_preserves_non_mingw_paths")


def test_sanitize_cwd_mingw_matches_native():
    """The whole point: Windows+bash $(pwd) agrees with Windows-native path."""
    native = paths.sanitize_cwd("C:/Users/me/myproject")
    mingw = paths.sanitize_cwd("/c/Users/me/myproject")
    _assert(native == mingw,
            f"MINGW and native should sanitize identically: native={native!r} mingw={mingw!r}")
    _assert(native == "c--Users-me-myproject", f"unexpected sanitized form: {native!r}")
    print(f"PASS sanitize_cwd_mingw_matches_native ({native})")


def test_sanitize_cwd_windows_drive_lexical():
    """A Windows drive path sanitizes IDENTICALLY on every OS. On POSIX,
    Path("C:/x").resolve() treats "C:" as relative and mangles it, so sanitize_cwd
    must transform the lexical string. Regression for the macOS failure of
    sanitize_cwd_mingw_matches_native (the _WINDOWS_DRIVE_RE fix)."""
    _assert(paths.sanitize_cwd("C:/Users/me/myproject") == "c--Users-me-myproject",
            "forward-slash drive path")
    _assert(paths.sanitize_cwd("D:\\Foo\\Bar") == "d--Foo-Bar",
            "backslash drive path sanitizes identically")
    print("PASS sanitize_cwd_windows_drive_lexical")


def test_sanitize_cwd_mingw_does_not_double_prefix():
    """Regression: the bug produced `c--c-Users-...`. Make sure it's gone."""
    out = paths.sanitize_cwd("/c/Users/me/myproject")
    _assert(not out.startswith("c--c-"),
            f"sanitize should not double-prefix MINGW paths: {out!r}")
    print("PASS sanitize_cwd_mingw_does_not_double_prefix")


def test_sanitize_cwd_idempotent_on_project_dir():
    """connect(project_dir(cwd)) must resolve to the same KB as connect(cwd).

    Regression: passing an already-sanitized project_dir back into
    sanitize_cwd used to double-sanitize (e.g. produce a nested form like
    `c--installdir-projects-c--Users-me-someproject`), silently creating a
    ghost empty DB.
    """
    cwd = "C:/Users/me/some_project"
    once = paths.sanitize_cwd(cwd)
    pdir = paths.project_dir(cwd)
    twice = paths.sanitize_cwd(pdir)
    _assert(once == twice,
            f"sanitize should be idempotent on project_dir output: once={once!r} twice={twice!r}")
    _assert(twice == "c--Users-me-some_project",
            f"unexpected sanitized form: {twice!r}")
    print(f"PASS sanitize_cwd_idempotent_on_project_dir ({once})")


def test_is_write_disabled_via_env_var():
    """Disable env vars disable hooks without touching sentinel files."""
    # Snapshot + clear ambient state so the test is deterministic regardless
    # of whether the DISABLE_WRITE file exists on this machine.
    file_existed = paths.DISABLE_WRITE_FILE.exists()
    backup = paths.DISABLE_WRITE_FILE.read_text(encoding="utf-8") if file_existed else None
    unlatched_existed = paths.UNLATCHED_FILE.exists()
    unlatched_backup = paths.UNLATCHED_FILE.read_text(encoding="utf-8") if unlatched_existed else None
    if file_existed:
        paths.DISABLE_WRITE_FILE.unlink()
    if unlatched_existed:
        paths.UNLATCHED_FILE.unlink()
    saved = os.environ.pop("CLAUDE_KB_DISABLE_WRITE", None)
    saved_latch = os.environ.pop("LATCH_DISABLE_WRITE", None)
    saved_global = os.environ.pop("CLAUDE_KB_DISABLE", None)
    saved_latch_global = os.environ.pop("LATCH_DISABLE", None)
    saved_unlatched = os.environ.pop("LATCH_UNLATCHED", None)
    try:
        _assert(paths.is_write_disabled() is False,
                "no flag set → write hooks should be live")
        os.environ["LATCH_DISABLE_WRITE"] = "1"
        _assert(paths.is_write_disabled() is True,
                "LATCH_DISABLE_WRITE set → write hooks should be disabled")
        _assert(paths.is_disabled() is False,
                "narrow flag must NOT disable read hooks")
        del os.environ["LATCH_DISABLE_WRITE"]
        os.environ["CLAUDE_KB_DISABLE_WRITE"] = "1"
        _assert(paths.is_write_disabled() is True,
                "legacy env var set → write hooks should be disabled")
        del os.environ["CLAUDE_KB_DISABLE_WRITE"]
        # Global DISABLE implies write-disabled too.
        os.environ["LATCH_DISABLE"] = "1"
        _assert(paths.is_write_disabled() is True,
                "LATCH_DISABLE should imply write-disabled")
        del os.environ["LATCH_DISABLE"]
        os.environ["CLAUDE_KB_DISABLE"] = "1"
        _assert(paths.is_write_disabled() is True,
                "legacy global disable should imply write-disabled")
    finally:
        os.environ.pop("LATCH_DISABLE_WRITE", None)
        os.environ.pop("CLAUDE_KB_DISABLE_WRITE", None)
        os.environ.pop("LATCH_DISABLE", None)
        os.environ.pop("CLAUDE_KB_DISABLE", None)
        if saved is not None:
            os.environ["CLAUDE_KB_DISABLE_WRITE"] = saved
        if saved_latch is not None:
            os.environ["LATCH_DISABLE_WRITE"] = saved_latch
        if saved_global is not None:
            os.environ["CLAUDE_KB_DISABLE"] = saved_global
        if saved_latch_global is not None:
            os.environ["LATCH_DISABLE"] = saved_latch_global
        if saved_unlatched is not None:
            os.environ["LATCH_UNLATCHED"] = saved_unlatched
        if file_existed and backup is not None:
            paths.DISABLE_WRITE_FILE.write_text(backup, encoding="utf-8")
        if unlatched_existed and unlatched_backup is not None:
            paths.UNLATCHED_FILE.write_text(unlatched_backup, encoding="utf-8")
    print("PASS is_write_disabled_via_env_var")


def test_unlatched_mode_disables_full_latch():
    unlatched_existed = paths.UNLATCHED_FILE.exists()
    disable_existed = paths.DISABLE_FILE.exists()
    unlatched_backup = paths.UNLATCHED_FILE.read_text(encoding="utf-8") if unlatched_existed else None
    disable_backup = paths.DISABLE_FILE.read_text(encoding="utf-8") if disable_existed else None
    saved_unlatched = os.environ.pop("LATCH_UNLATCHED", None)
    saved_disable = os.environ.pop("LATCH_DISABLE", None)
    saved_legacy_disable = os.environ.pop("CLAUDE_KB_DISABLE", None)
    try:
        if unlatched_existed:
            paths.UNLATCHED_FILE.unlink()
        if disable_existed:
            paths.DISABLE_FILE.unlink()
        _assert(paths.is_unlatched_mode() is False,
                "unlatched mode should be off with no env var or sentinel")
        _assert(paths.is_disabled() is False,
                "latched and no DISABLE should leave hooks enabled")

        os.environ["LATCH_UNLATCHED"] = "1"
        _assert(paths.is_unlatched_mode() is True,
                "LATCH_UNLATCHED should mark Unlatched mode active")
        _assert(paths.is_disabled() is True,
                "Unlatched mode must fully disable latch influence")
        _assert(paths.is_write_disabled() is True,
                "Unlatched mode should imply write-disabled")
        del os.environ["LATCH_UNLATCHED"]

        paths.UNLATCHED_FILE.write_text("test unlatched\n", encoding="utf-8")
        _assert(paths.is_unlatched_mode() is True,
                "UNLATCHED sentinel should mark Unlatched mode active")
        _assert(paths.is_disabled() is True,
                "UNLATCHED sentinel should fully disable latch influence")
    finally:
        os.environ.pop("LATCH_UNLATCHED", None)
        os.environ.pop("LATCH_DISABLE", None)
        os.environ.pop("CLAUDE_KB_DISABLE", None)
        if saved_unlatched is not None:
            os.environ["LATCH_UNLATCHED"] = saved_unlatched
        if saved_disable is not None:
            os.environ["LATCH_DISABLE"] = saved_disable
        if saved_legacy_disable is not None:
            os.environ["CLAUDE_KB_DISABLE"] = saved_legacy_disable
        if paths.UNLATCHED_FILE.exists():
            paths.UNLATCHED_FILE.unlink()
        if paths.DISABLE_FILE.exists():
            paths.DISABLE_FILE.unlink()
        if unlatched_existed and unlatched_backup is not None:
            paths.UNLATCHED_FILE.write_text(unlatched_backup, encoding="utf-8")
        if disable_existed and disable_backup is not None:
            paths.DISABLE_FILE.write_text(disable_backup, encoding="utf-8")
    print("PASS unlatched_mode_disables_full_latch")


def test_latch_kb_dir_precedes_legacy_env_pin():
    saved_latch = os.environ.pop("LATCH_KB_DIR", None)
    saved_legacy = os.environ.pop("CLAUDE_KB_DIR", None)
    old_pin = paths._PINNED_DIR
    try:
        os.environ["CLAUDE_KB_DIR"] = "/tmp/legacy-kb"
        os.environ["LATCH_KB_DIR"] = "/tmp/latch-kb"
        paths._PINNED_DIR = False
        _assert(str(paths._resolve_pinned_dir()) == "/tmp/latch-kb",
                "LATCH_KB_DIR should take precedence over legacy CLAUDE_KB_DIR")
        print("PASS latch_kb_dir_precedes_legacy_env_pin")
    finally:
        paths._PINNED_DIR = old_pin
        os.environ.pop("LATCH_KB_DIR", None)
        os.environ.pop("CLAUDE_KB_DIR", None)
        if saved_latch is not None:
            os.environ["LATCH_KB_DIR"] = saved_latch
        if saved_legacy is not None:
            os.environ["CLAUDE_KB_DIR"] = saved_legacy


if __name__ == "__main__":
    test_normalize_mingw_drive_letter()
    test_normalize_preserves_native_windows()
    test_normalize_preserves_non_mingw_paths()
    test_sanitize_cwd_mingw_matches_native()
    test_sanitize_cwd_windows_drive_lexical()
    test_sanitize_cwd_mingw_does_not_double_prefix()
    test_sanitize_cwd_idempotent_on_project_dir()
    test_is_write_disabled_via_env_var()
    test_unlatched_mode_disables_full_latch()
    test_latch_kb_dir_precedes_legacy_env_pin()
    print("\nAll paths tests pass.")
