"""Resolve KB paths.

KB_ROOT is the latch install root. By default it is auto-detected as the
parent of this file's directory (i.e. the repo root), which lets the tool
run wherever it was cloned without configuration. Set ``LATCH_HOME`` to
override; ``CLAUDE_KB_HOME`` remains a legacy alias for existing installs. This
is useful when the source tree is on a read-only mount and you need projects/logs
to live elsewhere.

**The KB directory is pinned, not derived from the working directory** (KB
decision id=1556). Historically the project DB was selected per-cwd via
``PROJECTS_ROOT / sanitize_cwd(cwd) / kb.db``; that "which DB, inferred from
where I'm standing" model was the single root cause of the entire wrong-DB bug
family (id=302/307/335/1461/1523/1555 — including a session compacted into a
*foreign project's* KB). ``project_dir`` now returns ONE fixed KB directory
chosen at install time, resolved by ``_resolve_pinned_dir()``:

  1. ``LATCH_KB_DIR`` / ``CLAUDE_KB_DIR`` env var (explicit override), else
  2. ``<KB_ROOT>/kb_location.json`` written at install time, else
  3. legacy per-cwd selection (only when neither is configured, so an
     unconfigured clone keeps working exactly as before — no silent regression).

The working directory is retained ONLY as the *scope* signal for artifact
tagging (``artifacts.canonicalize_repo`` has its own canonicalizer); it never
again selects the on-disk DB. Multiple KBs are possible only by explicit opt-in
(a named vault), never inferred from cwd.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path


def _default_kb_root() -> Path:
    # paths.py lives at <KB_ROOT>/src/paths.py
    return Path(__file__).resolve().parent.parent


KB_ROOT = Path(os.environ.get("LATCH_HOME") or os.environ.get("CLAUDE_KB_HOME") or _default_kb_root())
PROJECTS_ROOT = KB_ROOT / "projects"
SCHEMA_PATH = KB_ROOT / "src" / "schema.sql"
DISABLE_FILE = KB_ROOT / "DISABLE"
DISABLE_WRITE_FILE = KB_ROOT / "DISABLE_WRITE"
UNLATCHED_FILE = KB_ROOT / "UNLATCHED"
UNLATCHED_STATE_FILE = KB_ROOT / "UNLATCH_STATE.json"
UNLATCHED_MESSAGE = (
    "Latch is currently UNLATCHED.\n"
    "Latch guidance, gate/search/compact/maintenance, and automatic writes are off "
    "for this latch install.\n"
    "Run /unlatch to re-latch. If LATCH_UNLATCHED is set, unset it too."
)

# Install-time pin file: a small JSON {"kb_dir": "<absolute path>"} written by
# install_engine.py. The single source of truth for "which KB" on a configured
# install. Read via _resolve_pinned_dir(); env var LATCH_KB_DIR / CLAUDE_KB_DIR
# overrides it.
KB_LOCATION_FILE = KB_ROOT / "kb_location.json"

# User-selected prompt/brief intensity. Unlike the KB pin, this file is read
# uncached: retiering should take effect for a long-lived MCP process without a
# restart. Hooks are short-lived processes, but use the same resolver so every
# surface agrees. A missing setting falls back to ``full`` to preserve older
# installs. Invalid explicit configuration never silently escalates beyond a
# valid saved choice; without one it fails safe to ``quiet`` and is surfaced by
# doctor/status.
LATCH_SETTINGS_FILE = KB_ROOT / "latch_settings.json"
LATCH_INTENSITIES = ("quiet", "standard", "full")
LEGACY_LATCH_INTENSITY = "full"
FRESH_INSTALL_LATCH_INTENSITY = "standard"
INVALID_LATCH_INTENSITY_FALLBACK = "quiet"

# Lazily-resolved, cached pinned KB dir. Sentinel ``False`` = "not yet resolved"
# (distinct from a resolved ``None`` meaning "no pin configured → legacy mode").
_PINNED_DIR: "Path | None | bool" = False


def normalize_latch_intensity(value: object) -> str | None:
    """Return a canonical intensity name, or ``None`` for invalid input."""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in LATCH_INTENSITIES else None


def latch_intensity_state(
    *,
    env: "dict[str, str] | os._Environ[str] | None" = None,
    settings_file: Path | None = None,
) -> tuple[str, str, str | None]:
    """Resolve ``(intensity, source, warning)`` without caching.

    Resolution order is ``LATCH_INTENSITY`` > ``latch_settings.json`` > the
    legacy-preserving Full fallback. Invalid explicit configuration never
    breaks a hook; a valid saved choice beats an invalid env override, otherwise
    resolution fails safe to Quiet and returns a visible warning.
    """
    values = os.environ if env is None else env
    path = settings_file or LATCH_SETTINGS_FILE
    saved_value: str | None = None
    saved_warning: str | None = None
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "intensity" not in data:
                saved_warning = f"missing intensity key in {path}"
            else:
                raw_file = data.get("intensity") if isinstance(data, dict) else None
                saved_value = normalize_latch_intensity(raw_file)
                if saved_value is None:
                    saved_warning = f"invalid intensity value {raw_file!r} in {path}"
        elif path.exists() or path.is_symlink():
            saved_warning = f"{path} exists but is not a regular file"
    except (OSError, ValueError) as exc:
        saved_warning = f"could not read {path}: {exc}"

    raw_env = values.get("LATCH_INTENSITY")
    if raw_env is not None:
        normalized = normalize_latch_intensity(raw_env)
        if normalized is not None:
            return normalized, "env", None
        if saved_value is not None:
            return (
                saved_value,
                "settings",
                f"invalid LATCH_INTENSITY={raw_env!r}; using saved {saved_value}",
            )
        return (
            INVALID_LATCH_INTENSITY_FALLBACK,
            "fallback",
            f"invalid LATCH_INTENSITY={raw_env!r}; using safe "
            f"{INVALID_LATCH_INTENSITY_FALLBACK}",
        )

    if saved_value is not None:
        return saved_value, "settings", None
    if saved_warning is not None:
        return (
            INVALID_LATCH_INTENSITY_FALLBACK,
            "fallback",
            f"{saved_warning}; using safe {INVALID_LATCH_INTENSITY_FALLBACK}",
        )

    return LEGACY_LATCH_INTENSITY, "legacy_default", None


def latch_intensity(
    *,
    env: "dict[str, str] | os._Environ[str] | None" = None,
    settings_file: Path | None = None,
) -> str:
    """Return the effective uncached Quiet/Standard/Full intensity."""
    return latch_intensity_state(env=env, settings_file=settings_file)[0]


def latch_intensity_change_hint() -> str:
    """Return install-root-qualified, copyable retier commands for doctors."""
    shell_path = shlex.quote(str(KB_ROOT / "bin" / "latch_intensity.sh"))
    ps_path = str(KB_ROOT / "bin" / "latch_intensity.ps1").replace("'", "''")
    return f"change with bash {shell_path} (or & '{ps_path}' on Windows)"


def configured_latch_intensity(settings_file: Path | None = None) -> str | None:
    """Return the valid file-backed choice, excluding env/default fallbacks."""
    path = settings_file or LATCH_SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return normalize_latch_intensity(data.get("intensity"))


def write_latch_intensity(value: str, settings_file: Path | None = None) -> Path:
    """Atomically persist ``value`` while preserving unrelated settings keys."""
    normalized = normalize_latch_intensity(value)
    if normalized is None:
        raise ValueError(
            f"unsupported Latch intensity {value!r}; choose "
            + ", ".join(LATCH_INTENSITIES)
        )
    path = settings_file or LATCH_SETTINGS_FILE
    data: dict = {}
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict):
            data.update(current)
    except (OSError, ValueError):
        pass
    data["intensity"] = normalized
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


def _resolve_pinned_dir() -> Path | None:
    """The single fixed KB directory, or None when no pin is configured.

    Resolution order (id=1556): LATCH_KB_DIR / CLAUDE_KB_DIR env >
    kb_location.json > None.
    Result is cached for the process; a config change takes effect on the next
    process (hooks are fresh subprocesses; the MCP server is told to restart on
    install, matching LATCH_HOME / CLAUDE_KB_HOME). Reading is defensive — a
    missing or malformed pin file falls through to None (legacy per-cwd), never
    raises."""
    global _PINNED_DIR
    if _PINNED_DIR is not False:
        return _PINNED_DIR  # type: ignore[return-value]
    env = os.environ.get("LATCH_KB_DIR") or os.environ.get("CLAUDE_KB_DIR")
    if env and env.strip():
        _PINNED_DIR = Path(env.strip())
        return _PINNED_DIR
    try:
        if KB_LOCATION_FILE.exists():
            data = json.loads(KB_LOCATION_FILE.read_text(encoding="utf-8"))
            kb_dir = (data or {}).get("kb_dir")
            if isinstance(kb_dir, str) and kb_dir.strip():
                _PINNED_DIR = Path(kb_dir.strip())
                return _PINNED_DIR
    except (OSError, ValueError):
        pass  # malformed/unreadable pin → fall through to legacy
    _PINNED_DIR = None
    return None


def is_unlatched_mode() -> bool:
    """User-facing vanilla-agent escape hatch.

    Unlatched mode is implemented as a thin layer over the full kill switch:
    automatic latch influence is off, while the UNLATCHED sentinel gives hooks and
    status commands enough metadata to show an explicit receipt instead of going
    silently dark.
    """
    if os.environ.get("LATCH_UNLATCHED"):
        return True
    return UNLATCHED_FILE.exists()


def is_disabled() -> bool:
    """Kill-switch: hooks and compactor no-op if DISABLE/UNLATCHED exists or
    the LATCH_DISABLE / CLAUDE_KB_DISABLE env var is set. Recoverable in one
    command: `bash bin/latch_enable.sh` or `/unlatch`."""
    if is_unlatched_mode():
        return True
    if os.environ.get("LATCH_DISABLE") or os.environ.get("CLAUDE_KB_DISABLE"):
        return True
    return DISABLE_FILE.exists()


def is_write_disabled() -> bool:
    """Narrower kill-switch covering write-side hooks (Stop, SessionEnd) only.

    Read-side hooks (SessionStart, UserPromptSubmit) stay live. Used to enable
    the brief + per-prompt context injection without re-enabling the
    Stop->compactor path that fan-out'd in 2026-04-23. Implies is_disabled()."""
    if is_disabled():
        return True
    if os.environ.get("LATCH_DISABLE_WRITE") or os.environ.get("CLAUDE_KB_DISABLE_WRITE"):
        return True
    return DISABLE_WRITE_FILE.exists()


def is_in_compact() -> bool:
    """Reentrancy guard: true if running inside a compactor-spawned `claude -p`
    session. Hooks must no-op so the compactor's own claude invocation cannot
    recursively trigger further compactions."""
    return bool(os.environ.get("LATCH_IN_COMPACT") or os.environ.get("CLAUDE_KB_IN_COMPACT"))


_MINGW_PATH_RE = re.compile(r"^/([a-zA-Z])/")
# Cursor's hook payload reports ``workspace_roots`` as a URI-style path with a
# leading slash before the drive, e.g. ``/C:/Users/...``. Left as-is this
# sanitizes to a *different* project dir than the MCP daemon's native
# ``C:\Users\...``, splitting hooks (session markers, session rows) and the MCP
# KB into two folders. Strip the spurious leading slash so both agree.
_LEADING_SLASH_DRIVE_RE = re.compile(r"^/([a-zA-Z]:[\\/])")
# A Windows absolute path: drive letter + colon + slash (``C:/`` or ``C:\``).
# These must be sanitized LEXICALLY, because on POSIX ``Path("C:/x").resolve()``
# treats ``C:`` as a relative segment and prepends the cwd — so a Windows path
# would sanitize differently on macOS/Linux than on Windows. Matching this lets
# sanitize_cwd transform the raw string instead of the mangled resolved path.
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _normalize_input_path(cwd: str) -> str:
    """Convert MINGW / MSYS / Git-Bash unix-style Windows paths to native form.

    `$(pwd)` in bash on Windows returns `/c/Foo/Bar` — but `pathlib.Path` on
    Windows resolves that as `C:\\c\\Foo\\Bar` (treating the leading `/c/` as
    a relative path on the current drive), which then sanitizes to a different
    project dir than the Windows-native `C:/Foo/Bar`. Collapse `/c/` -> `C:/`
    before Path sees it so CLI invocations and hook invocations agree.
    """
    m = _MINGW_PATH_RE.match(cwd)
    if m:
        return f"{m.group(1).upper()}:/{cwd[m.end():]}"
    # ``/C:/Users/...`` (Cursor workspace_roots) -> ``C:/Users/...`` so it
    # sanitizes identically to the MCP daemon's native drive path.
    if _LEADING_SLASH_DRIVE_RE.match(cwd):
        return cwd[1:]
    return cwd


def sanitize_cwd(cwd: str | os.PathLike) -> str:
    """Convert a Windows path into a safe directory name.

    C:/path/to/your/project -> c--path-to-your-project
    Mirrors the convention Claude Code uses for its own per-project memory dirs.
    Also normalizes MINGW-style `/c/...` paths so bash callers agree with
    hook-path callers.

    Idempotent on already-sanitized inputs: if `cwd` resolves to a direct child
    of PROJECTS_ROOT (i.e. an existing project KB dir), return its folder name
    unchanged. Prevents `connect(project_dir(x))` from creating a ghost dir.
    """
    normalized = _normalize_input_path(str(cwd))
    is_windows_abs = bool(_WINDOWS_DRIVE_RE.match(normalized))
    resolved = Path(normalized).resolve()
    if resolved.parent == PROJECTS_ROOT.resolve():
        return resolved.name
    # For a Windows drive path, transform the LEXICAL `normalized` string — on
    # POSIX `Path(...).resolve()` mangles `C:/x` into a relative path, so using
    # `str(resolved)` would sanitize differently per-OS. The drive-letter regex
    # below is unchanged; only the source string differs.
    base = normalized if is_windows_abs else str(resolved)
    p = base.replace("\\", "/")
    p = re.sub(r"^([a-zA-Z]):/", lambda m: m.group(1).lower() + "--", p)
    p = p.replace("/", "-")
    return p


def project_dir(cwd: str | os.PathLike | None = None) -> Path:
    """The KB directory (holds kb.db, budget.json, the compactor lock, logs).

    When a pin is configured (id=1556) the ``cwd`` argument is IGNORED and the
    one fixed KB directory is returned — this is what makes the wrong-DB bug
    class structurally impossible. ``cwd`` is honored only in legacy
    (unconfigured) mode, where it selects a per-project dir as before."""
    pinned = _resolve_pinned_dir()
    if pinned is not None:
        return pinned
    cwd = cwd or os.getcwd()
    return PROJECTS_ROOT / sanitize_cwd(cwd)


def db_path(cwd: str | os.PathLike | None = None) -> Path:
    return project_dir(cwd) / "kb.db"


def kb_has_evidence(cwd: str | os.PathLike | None = None) -> bool:
    """Whether the selected KB already contains at least one durable node.

    Used only by installer/retier UX to distinguish a genuinely fresh install
    from an older install that predates ``latch_settings.json``. The read-only
    SQLite URI avoids creating a database as a side effect.
    """
    import sqlite3

    candidates = {db_path(cwd), KB_ROOT / "store" / "kb.db"}
    try:
        candidates.update(PROJECTS_ROOT.glob("*/kb.db"))
    except OSError:
        pass
    for path in candidates:
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone()
                if row is not None:
                    return True
            finally:
                conn.close()
        except (OSError, sqlite3.Error):
            continue
    return False


def ensure_project_dir(cwd: str | os.PathLike | None = None) -> Path:
    d = project_dir(cwd)
    d.mkdir(parents=True, exist_ok=True)
    return d
