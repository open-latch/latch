"""Ephemeral-vault isolation for the S1a adherence harness (KB id=5134).

The harness's one unrecoverable failure is MEASUREMENT CONTAMINATION. A probe
run that writes a row into the live vault's gate log, or a node into the live
KB, pollutes the V4/T1 denominator and corrupts a measurement that cannot be
restarted: the T1 window has no automatic extension (id=4963).

Three structural facts make this easy to get wrong, and each one is guarded
here rather than assumed:

1. ``paths.project_dir()`` IGNORES its ``cwd`` argument whenever a pin is
   configured (id=1556). Passing a fake ``project_path`` into ``gate.run_gate``
   therefore does NOT redirect the gate log — the row still lands in the live
   vault. Only ``LATCH_KB_DIR`` / ``CLAUDE_KB_DIR`` plus a cache refresh moves
   it, because ``_resolve_pinned_dir`` caches the pin for the process.
2. latch auto-capture writes NODES into whatever vault is live (id=2673), and
   the ``vec_nodes`` index is written on a separate path (id=2651). A
   gate-log-only contamination check is therefore insufficient; the digest
   below covers nodes, vec_nodes and rejected_path as well.
3. The vault is pinned by ``kb_location.json`` (id=2554), so a worktree that
   runs latch code can repoint the live vault. Nothing here ever writes a pin
   file.

DECLARED DEVIATION. The gate ruling on this mission asked for each scratch
vault to be pinned "via its own ``kb_location.json`` under the scratch root".
It is pinned by ``LATCH_KB_DIR`` instead. ``paths.KB_LOCATION_FILE`` is
``<KB_ROOT>/kb_location.json``: a pin file is only consulted when KB_ROOT
itself moves, and moving KB_ROOT breaks ``paths.SCHEMA_PATH`` and the vendor
model lookup. Writing a pin file into a repo tree is also the literal id=2554
hazard. The environment variable outranks the pin file in
``_resolve_pinned_dir``, so this is the stronger lever, not a weaker one — and
``assert_isolated`` proves the repin took effect rather than trusting it.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse
from urllib.request import url2pathname

from latch.store import paths


GIB = 1024 ** 3

#: Charter 5134 item 5(c): abort below this much free space. S1a generates
#: throwaway vaults, which is the exact growth source of the id=3763 disk
#: incident, so the check runs before anything is created.
MIN_FREE_BYTES = 10 * GIB

SCRATCH_ENV = "LATCH_ADHERENCE_SCRATCH"

#: Tables whose contents are digested for the contamination guard. ``nodes``
#: catches auto-capture (id=2673), ``vec_nodes`` catches the separate index
#: write path (id=2651), ``rejected_path`` catches V2's typed-rejection bank.
DIGESTED_TABLES = ("nodes", "vec_nodes", "rejected_path")

#: Environment variables that pin the vault. Cleared and restored as a unit.
_PIN_ENV_VARS = ("LATCH_KB_DIR", "CLAUDE_KB_DIR")

#: Creating a vault also mints a durable identity record in the production
#: registry (``vault_identity``). Left at its default, every ephemeral vault
#: leaks one orphan record into the user's real
#: ``<platform production root>/registry`` that outlives the vault by design —
#: measured at six per run before this was redirected. That is the same
#: throwaway-artefact growth 3865's execution-order note put ahead of S1a, so
#: the registry follows the vault into the scratch root and dies with it.
_STATE_ENV_VARS = ("LATCH_PRODUCTION_DATA_ROOT", "LATCH_VAULT_REGISTRY_ROOT")

#: Windows refuses to delete a read-only file (vault_identity writes registry
#: records read-only) and, for a moment, one another program such as
#: antivirus or the search indexer still holds after latch closed it. Both
#: arrive as PermissionError, so teardown clears the attribute and retries
#: with backoff. POSIX ignores a file's own mode when deleting it, so there a
#: refusal is final and nothing is chmod'ed.
_WINDOWS = os.name == "nt"
_TEARDOWN_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)

#: Captured at import, BEFORE the harness ever repins the process, so that
#: ``live_vault_dir()`` keeps reporting the production vault while an
#: ephemeral pin is active.
_IMPORT_TIME_ENV_PIN = os.environ.get("LATCH_KB_DIR") or os.environ.get("CLAUDE_KB_DIR")


class ContaminationError(RuntimeError):
    """A harness code path resolved or opened something outside its scratch root."""


class PreflightError(RuntimeError):
    """A precondition for running the harness is not met."""


# ---------- locating the production vault ----------

def live_vault_dir() -> Path | None:
    """The production vault directory, or ``None`` when no pin is configured.

    Deliberately resolved from the import-time environment and the on-disk pin
    file rather than from ``paths._resolve_pinned_dir()``, whose cache the
    harness mutates. Under pytest both sources are absent, so this returns
    ``None`` and tests must supply an explicit decoy — which is what keeps the
    suite from ever touching a real vault.
    """
    if _IMPORT_TIME_ENV_PIN and _IMPORT_TIME_ENV_PIN.strip():
        return Path(_IMPORT_TIME_ENV_PIN.strip()).expanduser()
    try:
        if paths.KB_LOCATION_FILE.exists():
            import json

            data = json.loads(paths.KB_LOCATION_FILE.read_text(encoding="utf-8"))
            kb_dir = (data or {}).get("kb_dir")
            if isinstance(kb_dir, str) and kb_dir.strip():
                return Path(kb_dir.strip()).expanduser()
    except (OSError, ValueError):
        pass
    return None


def default_scratch_root() -> Path:
    """Where ephemeral vaults live.

    Under pytest the answer is forced: ``paths.project_dir`` refuses any pin
    that is not strictly inside ``<test_root>/vaults``, so the scratch root has
    to sit there or every repin raises ``UnsafeTestExecutionError``.
    """
    override = os.environ.get(SCRATCH_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser()
    test_root = paths.validated_test_root()
    if test_root is not None:
        return test_root / "vaults" / "adherence"
    return Path(tempfile.gettempdir()) / "latch-adherence"


# ---------- preflight ----------

def free_bytes(path: Path) -> int:
    """Free bytes on the filesystem holding ``path`` (nearest existing parent)."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return int(usage.free)


def require_disk_headroom(
    scratch_root: Path, *, minimum: int = MIN_FREE_BYTES
) -> int:
    """Charter item 5(c). Raise ``PreflightError`` below ``minimum``."""
    available = free_bytes(scratch_root)
    if available < minimum:
        raise PreflightError(
            f"insufficient disk headroom for the adherence harness: "
            f"{available / GIB:.1f} GiB free at {scratch_root}, "
            f"{minimum / GIB:.1f} GiB required"
        )
    return available


def _file_digest(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"exists": False}
    h = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                h.update(chunk)
    except OSError as e:
        return {"exists": True, "unreadable": f"{type(e).__name__}"}
    return {
        "exists": True,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": h.hexdigest(),
    }


def _table_digest(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.Error as e:
        # Never silently "pass" on an unreadable table: record why, so a
        # pre-run readable / post-run unreadable flip still shows as a change.
        return {"readable": False, "detail": type(e).__name__}
    h = hashlib.sha256()
    for row in rows:
        for value in row:
            h.update(b"\0")
            if value is None:
                h.update(b"<null>")
            elif isinstance(value, bytes):
                h.update(value)
            else:
                h.update(str(value).encode("utf-8", "replace"))
    return {"readable": True, "rows": len(rows), "sha256": h.hexdigest()}


#: SQLite's write-ahead sidecars. Excluded from the compared file set on
#: purpose: opening a WAL database even in ``mode=ro`` materializes ``-shm``
#: (and ``-wal``) and a read-only connection cannot remove them on close, so a
#: digest that compared them would report a delta caused by the digest itself.
#: Nothing is lost — the per-table digests below read *through* the WAL, so any
#: content change hiding in it is still caught.
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _is_sidecar(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES)


def vault_digest(vault_dir: Path) -> dict[str, Any]:
    """A change-detecting snapshot of a vault directory.

    Covers every durable file in the directory (so gate logs, budget state and
    lineage checkpoints are all included) plus per-table content digests for
    the tables auto-capture actually writes. Opens SQLite strictly read-only,
    which takes only a shared lock and cannot perturb a running daemon.
    """
    vault_dir = Path(vault_dir)
    snapshot: dict[str, Any] = {"vault_dir": str(vault_dir), "exists": vault_dir.is_dir()}
    files: dict[str, Any] = {}
    sidecars: list[str] = []
    if vault_dir.is_dir():
        for entry in sorted(vault_dir.rglob("*")):
            if not entry.is_file():
                continue
            name = str(entry.relative_to(vault_dir))
            if _is_sidecar(name):
                sidecars.append(name)
            else:
                files[name] = _file_digest(entry)
    snapshot["files"] = files
    # Informational only; never compared (see _SQLITE_SIDECAR_SUFFIXES).
    snapshot["sidecars_present"] = sidecars

    tables: dict[str, Any] = {}
    db_file = vault_dir / "kb.db"
    if db_file.exists():
        conn = None
        try:
            conn = sqlite3.connect(f"{db_file.as_uri()}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
            with contextlib.suppress(Exception):
                import sqlite_vec  # type: ignore

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
            for table in DIGESTED_TABLES:
                tables[table] = _table_digest(conn, table)
        except sqlite3.Error as e:
            tables["_error"] = type(e).__name__
        finally:
            if conn is not None:
                conn.close()
    snapshot["tables"] = tables
    return snapshot


def digest_delta(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable description of what changed between two digests."""
    deltas: list[str] = []
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    for name in sorted(set(before_files) | set(after_files)):
        if before_files.get(name) != after_files.get(name):
            if name not in before_files:
                deltas.append(f"file added: {name}")
            elif name not in after_files:
                deltas.append(f"file removed: {name}")
            else:
                deltas.append(f"file changed: {name}")
    before_tables = before.get("tables") or {}
    after_tables = after.get("tables") or {}
    for name in sorted(set(before_tables) | set(after_tables)):
        old, new = before_tables.get(name), after_tables.get(name)
        if old != new:
            old_rows = (old or {}).get("rows")
            new_rows = (new or {}).get("rows")
            deltas.append(f"table changed: {name} (rows {old_rows} -> {new_rows})")
    return deltas


def install_fingerprint(install_root: Path) -> dict[str, Any]:
    """Fingerprint the pieces a run must leave untouched.

    Gate ruling on mission 5134 asked for ``kb_location.json`` and the shared
    daemon's ``RUNTIME_KEY`` to be identical before and after a run. RUNTIME_KEY
    is a content hash over a fixed file set (``mcp_broker._runtime_content_files``),
    so the honest check is to digest that same file set directly — importing
    ``mcp_broker`` here would fingerprint *this worktree*, not the install.
    ``tests/test_latch_bench.py`` fails if the two lists drift apart.
    """
    install_root = Path(install_root)
    tracked = [install_root / "kb_location.json"]
    pkg = install_root / "src" / "latch"
    tracked += [pkg / name for name in _RUNTIME_KEY_PACKAGE_FILES]
    vendor = install_root / "vendor"
    tracked += [
        vendor / name for name in (
            "config.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt",
        )
    ]
    tracked.append(vendor / "model.onnx")
    return {
        "install_root": str(install_root),
        "files": {
            path.relative_to(install_root).as_posix(): (
                # model.onnx is ~90 MB and only its size feeds RUNTIME_KEY.
                {"exists": path.exists(), "size": path.stat().st_size}
                if path.name == "model.onnx" and path.exists()
                else _file_digest(path)
            )
            for path in tracked
        },
    }


#: ``mcp_broker._runtime_content_files()`` in the package layout, relative to
#: ``src/latch``. The vendor tokenizer files follow it in ``install_fingerprint``.
_RUNTIME_KEY_PACKAGE_FILES = (
    "mcp/mcp_broker.py", "mcp/mcp_proxy.py", "mcp/mcp_daemon.py",
    "mcp/mcp_server.py", "mcp/mcp_runtime.py",
    "gate/gate.py", "gate/capture_streams.py",
    "evals/outcome_measurement.py", "evals/outcome_measurement_runner.py",
    "evals/outcome_evidence.py",
    "store/artifacts.py", "proof/project_proof.py",
    "store/paths.py", "store/db.py", "store/vault_identity.py",
    "common/log_utils.py",
)


# ---------- the tripwire ----------

def _sqlite_target(database: Any, uri_flag: bool) -> tuple[Path | None, bool]:
    """Resolve a ``sqlite3.connect`` target to (path, is_readonly)."""
    if isinstance(database, (bytes, bytearray)):
        database = database.decode("utf-8", "replace")
    if not isinstance(database, (str, os.PathLike)):
        return None, False
    text = os.fspath(database)
    if text == ":memory:" or text.startswith("file::memory:"):
        return None, True
    if uri_flag or text.startswith("file:"):
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
        mode = (query.get("mode") or [""])[0]
        # url2pathname, not a bare unquote: on Windows ``as_uri()`` spells a
        # drive path ``file:///C:/...``, and ``/C:/...`` read as a path
        # resolves outside every scratch root. ``db.connect_readonly`` opens
        # exactly such a URI, so the tripwire must read it as SQLite does.
        raw = parsed.path
        if parsed.netloc and parsed.netloc != "localhost":
            raw = f"//{parsed.netloc}{raw}" if raw else parsed.netloc
        raw = url2pathname(raw)
        readonly = mode in ("ro", "memory")
        if mode == "memory":
            return None, True
        return Path(raw), readonly
    return Path(text), False


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(root)))
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def db_open_tripwire(
    *, scratch_root: Path, readonly_roots: tuple[Path, ...] = ()
) -> Iterator[list[dict[str, Any]]]:
    """Fail closed on any database open outside the harness's own scratch root.

    This is the guard that actually proves non-contamination for a run, as
    opposed to inferring it from an unchanged digest: a digest can be
    confounded by concurrent host activity, a refused ``connect`` cannot.

    ``readonly_roots`` names the one bounded exception charter 5134 item 3
    allows — the extractor reading the live vault — and it is honoured only for
    genuinely read-only URI opens. A writable open of the live vault raises.
    """
    opened: list[dict[str, Any]] = []
    real_connect = sqlite3.connect

    def guarded(database: Any, *args: Any, **kwargs: Any):
        uri_flag = bool(kwargs.get("uri", False))
        target, readonly = _sqlite_target(database, uri_flag)
        if target is not None:
            record = {"path": str(target), "readonly": readonly}
            opened.append(record)
            if not _is_inside(target, scratch_root):
                allowed = readonly and any(
                    _is_inside(target, root) for root in readonly_roots
                )
                if not allowed:
                    raise ContaminationError(
                        f"adherence harness attempted to open {target} "
                        f"({'read-only' if readonly else 'writable'}) outside its "
                        f"scratch root {scratch_root}"
                    )
                record["allowed_readonly_exception"] = True
        return real_connect(database, *args, **kwargs)

    sqlite3.connect = guarded  # type: ignore[assignment]
    try:
        yield opened
    finally:
        sqlite3.connect = real_connect  # type: ignore[assignment]


# ---------- the ephemeral vault ----------

def assert_isolated(scratch_root: Path) -> Path:
    """Prove the process pin currently resolves inside ``scratch_root``.

    Called after every repin and before anything connects. This is the check
    that catches the id=1556 trap: ``project_dir`` ignoring a ``cwd`` argument
    and quietly handing back the production vault.
    """
    resolved = paths.project_dir()
    if not _is_inside(resolved, scratch_root):
        raise ContaminationError(
            f"vault pin did not take effect: paths.project_dir() resolved to "
            f"{resolved}, which is outside the scratch root {scratch_root}"
        )
    return resolved


def purge_scratch_path(target: Path, *, scratch_root: Path) -> None:
    """Recursive delete, restricted to the harness's own scratch root.

    ``tests/test_destructive_path_guard.py`` forbids runtime code from calling
    ``shutil.rmtree`` on a KB/vault target outside a capability boundary, and
    it is right to: this harness creates and destroys vaults in a loop, which
    is the single worst place for a path bug. This is that boundary. Teardown
    goes through it, so a target that is not strictly inside the scratch root —
    or that is the production vault — raises instead of being removed.
    """
    resolved = Path(os.path.realpath(target))
    root = Path(os.path.realpath(scratch_root))
    if resolved == root or not _is_inside(resolved, root):
        raise ContaminationError(
            f"refusing to recursively delete {resolved}: outside the harness "
            f"scratch root {root}"
        )
    live = live_vault_dir()
    if live is not None and resolved == Path(os.path.realpath(live)):
        raise ContaminationError(
            f"refusing to recursively delete the production vault at {resolved}"
        )
    leftovers: list[str] = []

    def _on_error(func, path, exc: BaseException) -> None:
        # See _WINDOWS: anything not recovered is recorded, the walk carries
        # on, and the existence check below fails the teardown closed.
        if _WINDOWS and func in (os.unlink, os.remove) and isinstance(exc, PermissionError):
            for delay in (0, *_TEARDOWN_RETRY_DELAYS):
                time.sleep(delay)
                try:
                    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
                    func(path)
                    return
                except FileNotFoundError:
                    return
                except OSError as retry_exc:
                    exc = retry_exc
        leftovers.append(f"{path} ({type(exc).__name__})")

    if sys.version_info >= (3, 12):
        shutil.rmtree(resolved, onexc=_on_error)
    else:
        shutil.rmtree(
            resolved, onerror=lambda func, path, exc_info: _on_error(func, path, exc_info[1]),
        )
    if os.path.lexists(resolved):
        raise ContaminationError(
            f"teardown left {resolved} behind: "
            + ("; ".join(leftovers[:5]) or "the directory still exists")
        )


@contextlib.contextmanager
def ephemeral_vault(
    *,
    scratch_root: Path | None = None,
    label: str = "vault",
    keep: bool = False,
) -> Iterator[Path]:
    """Repin the process at a fresh, empty vault; tear it down on exit.

    One vault per call site, never shared between arms, so nothing an arm
    writes can reach the next arm — the fresh-vault-per-run fix id=2673 landed
    for the demo launchers, applied to measurement.
    """
    root = Path(scratch_root) if scratch_root is not None else default_scratch_root()
    root.mkdir(parents=True, exist_ok=True)
    vault = root / f"{label}-{uuid.uuid4().hex[:12]}"
    vault.mkdir(parents=True)
    state_root = root / "state"
    state_root.mkdir(parents=True, exist_ok=True)

    saved_env = {
        name: os.environ.get(name) for name in (*_PIN_ENV_VARS, *_STATE_ENV_VARS)
    }
    saved_pin = paths._PINNED_DIR
    try:
        os.environ.pop("CLAUDE_KB_DIR", None)
        os.environ["LATCH_KB_DIR"] = str(vault)
        # Redirected before the first connect, because the identity record is
        # written during vault creation and cannot be recalled afterwards.
        os.environ["LATCH_PRODUCTION_DATA_ROOT"] = str(state_root)
        os.environ["LATCH_VAULT_REGISTRY_ROOT"] = str(state_root / "registry")
        paths.refresh_pinned_dir()
        assert_isolated(root)
        yield vault
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        paths._PINNED_DIR = saved_pin
        if not keep:
            purge_scratch_path(vault, scratch_root=root)


@contextlib.contextmanager
def scratch_session(
    *, scratch_root: Path | None = None, keep: bool = False
) -> Iterator[Path]:
    """One scratch root for a whole run, with teardown (charter item 5(b))."""
    root = Path(scratch_root) if scratch_root is not None else default_scratch_root()
    session_root = root / f"run-{uuid.uuid4().hex[:12]}"
    session_root.mkdir(parents=True)
    try:
        yield session_root
    finally:
        if not keep:
            purge_scratch_path(session_root, scratch_root=root)
