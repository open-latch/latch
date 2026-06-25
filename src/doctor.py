#!/usr/bin/env python3
"""latch doctor — cross-platform install verifier.

Run after installing latch (and as the final step of any installer) to confirm
the environment can actually load and run the tool. It exists to turn the
install failures that otherwise surface as *confusing crashes — or, worse,
silent dead tools* into clear, actionable diagnoses:

  1. Half-finished ``pip install`` — a wheel failed mid-run and aborted the
     rest, leaving some of the five deps (mcp, onnxruntime, tokenizers, numpy,
     sqlite-vec) missing or with broken C-extensions (e.g. numpy's
     ``No module named 'numpy._core._multiarray_umath'``). The UserPromptSubmit
     hook then crashes on every prompt. An ``import`` probe catches this.

  2. CPU-architecture mismatch (macOS / Apple Silicon) — a venv built with an
     Intel (x86_64) Python under Rosetta. sqlite-vec's prebuilt x86_64 binary
     (vec0) uses CPU instructions Rosetta 2 cannot emulate -> SIGILL ("Python
     quit unexpectedly") at extension-load time, on SessionStart. db.py's
     ``try/except`` around ``sqlite_vec.load`` CANNOT catch this: a signal is
     not a Python exception, so it surfaces as a hard process crash rather than
     the intended brute-force-cosine fallback.

  3. MCP server not wired into Claude Code — the environment is healthy but the
     server was never registered with the ``claude mcp`` registry (e.g. an old
     install that only wrote ``mcpServers`` into settings.json, which Claude
     Code does not read). Hooks still fire, so the install looks half-alive
     while the kb_* tools silently never connect. ``check_mcp_wiring`` catches
     this; remedy is ``bash bin/install_engine.sh``.

  4. Slash commands not installed — the engine is wired but commands/*.md were
     never copied into ~/.claude/commands/, so /kb-compact et al. error
     'Unknown skill'. ``check_commands_installed`` catches this; remedy is the
     same ``bash bin/install_engine.sh`` (which now installs them).

  5. KB directory not pinned — an install from before the pin fix (id=1556)
     selects the DB from the launch cwd (paths.py legacy per-cwd mode), so work
     can silently land in / be read from the wrong KB, and multiple per-cwd KBs
     accumulate. ``check_kb_pin`` DETECTS the unpinned/split state and tells the
     user to lock to one KB (``--kb-dir``). It does NOT merge KBs — consolidating
     split history is left to the user; doctor only identifies and recommends.

Design notes (why it is shaped this way):
  * The orchestrator imports the STANDARD LIBRARY ONLY, so it runs even on the
    broken venv it is meant to diagnose — importing numpy here would crash the
    doctor itself.
  * The risky checks (sqlite-vec extension load, ONNX embedder) run in a
    SUBPROCESS via the *same* interpreter (``sys.executable``). A SIGILL kills
    the child; the parent reads the negative return code and reports the cause
    instead of dying alongside it. An in-process ``try/except`` would not
    survive the signal.
  * The arch check uses ``sysctl sysctl.proc_translated`` / ``hw.optional.arm64``
    rather than comparing ``platform.machine()`` to ``uname -m`` — under Rosetta
    *both* report ``x86_64``, so that naive comparison never fires.

Exit code: 0 = healthy; non-zero = at least one hard check FAILED.

Usage:
    python src/doctor.py [--json] [--skip-embed] [--no-arch]
or via the wrappers:
    bash bin/latch_doctor.sh
    .\\bin\\latch_doctor.ps1   (PowerShell)
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import install_engine

MIN_PY = (3, 11)
REQUIRED_MODULES = ["mcp", "onnxruntime", "tokenizers", "numpy", "sqlite_vec"]
SRC_DIR = Path(__file__).resolve().parent
EMBED_DIM = 384

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", SKIP: "[SKIP]"}

ARCH_REMEDY = (
    "On Apple Silicon this is almost always an Intel (x86_64) Python running "
    "under Rosetta. Rebuild the venv with a NATIVE arm64 Python >= 3.11:\n"
    "    uv python install 3.11\n"
    "    uv venv --python 3.11 .venv && source .venv/bin/activate\n"
    "    uv pip install -r requirements.txt\n"
    "(or use arm64 Homebrew /opt/homebrew/bin/python3, or python.org), then "
    "repoint LATCH_PYTHON / CLAUDE_KB_PYTHON / ~/.claude/settings.json at that interpreter."
)


# --------------------------------------------------------------------------- #
# Subprocess probe helpers
# --------------------------------------------------------------------------- #
def _signal_name(rc: int) -> str:
    """rc<0 means the child was killed by signal -rc (POSIX)."""
    try:
        return signal.Signals(-rc).name
    except ValueError:
        return f"signal {-rc}"


def _stderr_tail(text: str | None, limit: int = 400) -> str:
    text = (text or "").strip()
    return text[-limit:] if text else ""


# Single subprocess that tries each required import in turn, flushing a line per
# module. If one import crashes the interpreter (signal), earlier flushed lines
# survive in stdout and the parent infers the rest from a negative return code.
_IMPORT_PROBE = (
    "import importlib, sys\n"
    "for m in %r:\n"
    "    try:\n"
    "        importlib.import_module(m)\n"
    "        print('OK\\t' + m); sys.stdout.flush()\n"
    "    except BaseException as e:\n"
    "        print('ERR\\t' + m + '\\t' + repr(e)[:300]); sys.stdout.flush()\n"
) % (REQUIRED_MODULES,)

# Exercises the exact path that SIGILLs on a Rosetta arch mismatch: load the
# sqlite-vec extension, build a vec0 table, and run a KNN distance query (the
# SIMD code Rosetta can't emulate). db.py:_load_vec does the load; this goes one
# step further and actually queries.
_VEC_PROBE = (
    "import sqlite3, struct, sqlite_vec\n"
    "c = sqlite3.connect(':memory:')\n"
    "c.enable_load_extension(True)\n"
    "sqlite_vec.load(c)\n"
    "c.enable_load_extension(False)\n"
    "c.execute('CREATE VIRTUAL TABLE t USING vec0(e float[4] distance_metric=cosine)')\n"
    "c.execute('INSERT INTO t(rowid, e) VALUES (1, ?)', [struct.pack('4f', 0.1, 0.2, 0.3, 0.4)])\n"
    "q = struct.pack('4f', 0.1, 0.2, 0.3, 0.39)\n"
    "rows = list(c.execute('SELECT rowid FROM t WHERE e MATCH ? ORDER BY distance LIMIT 1', [q]))\n"
    "assert rows and rows[0][0] == 1, rows\n"
    "print('VEC_OK')\n"
)


def _embed_probe(src_dir: Path) -> str:
    # Loads the vendored ONNX model via onnxruntime + tokenizers and embeds one
    # string end-to-end. Catches onnxruntime arch mismatches too (also signal-
    # crashes, not exceptions). Run in a subprocess for the same reason as vec.
    return (
        "import sys\n"
        f"sys.path.insert(0, {str(src_dir)!r})\n"
        "import embeddings\n"
        "v = embeddings.embed('latch doctor health check')\n"
        f"assert v.shape == ({EMBED_DIM},), v.shape\n"
        "print('EMBED_OK')\n"
    )


def _run_probe(code: str, ok_token: str, timeout: float,
               arch_hint: bool) -> tuple[str, str]:
    """Run `code` in a child interpreter; classify the outcome.

    arch_hint: append the Rosetta remediation when the child crashes on macOS
    (the signal-crash signature of an arch mismatch)."""
    try:
        p = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return FAIL, f"timed out after {timeout:.0f}s"
    if p.returncode == 0 and ok_token in (p.stdout or ""):
        return OK, ""
    if p.returncode < 0:
        msg = f"process crashed ({_signal_name(p.returncode)}, rc={p.returncode})"
        if arch_hint and platform.system() == "Darwin":
            msg += "\n       " + ARCH_REMEDY.replace("\n", "\n       ")
        return FAIL, msg
    tail = _stderr_tail(p.stderr)
    return FAIL, f"exited rc={p.returncode}" + (f": {tail}" if tail else "")


# --------------------------------------------------------------------------- #
# Individual checks  ->  list of (name, level, detail)
# --------------------------------------------------------------------------- #
def check_python_version(allow_old: bool) -> tuple[str, str, str]:
    v = sys.version_info
    cur = f"{v.major}.{v.minor}.{v.micro}"
    name = f"Python >= {MIN_PY[0]}.{MIN_PY[1]}"
    if (v.major, v.minor) >= MIN_PY:
        return name, OK, f"{cur} ({sys.executable})"
    detail = (f"found {cur} at {sys.executable}; latch is tested on >= 3.11. "
              "Use a newer interpreter or set LATCH_PYTHON (legacy: CLAUDE_KB_PYTHON) to one.")
    if allow_old:
        return name, WARN, detail + " [overridden by LATCH_DOCTOR_ALLOW_OLD_PYTHON]"
    return name, FAIL, detail


def _sysctl(key: str) -> str | None:
    try:
        out = subprocess.run(["sysctl", "-n", key],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def check_arch() -> tuple[str, str, str]:
    name = "CPU architecture"
    sysname = platform.system()
    machine = platform.machine()
    if sysname != "Darwin":
        # Windows has no Rosetta; the rare ARM64-Windows + x86-Python case is
        # tolerated by emulation, so report-only here.
        return name, OK, f"{sysname} {machine} (no Rosetta layer; arch check N/A)"
    translated = _sysctl("sysctl.proc_translated")   # "1" under Rosetta, "0" native, None on Intel Mac
    arm_hw = _sysctl("hw.optional.arm64") == "1"
    if translated == "1" or (arm_hw and machine == "x86_64"):
        detail = (f"interpreter reports machine={machine} but the hardware is "
                  f"Apple Silicon (proc_translated={translated}, hw.optional.arm64="
                  f"{'1' if arm_hw else '0'}). This Python runs under Rosetta and "
                  "sqlite-vec WILL SIGILL.\n       " + ARCH_REMEDY.replace("\n", "\n       "))
        return name, FAIL, detail
    return name, OK, f"Darwin {machine} (native; proc_translated={translated})"


def check_imports() -> list[tuple[str, str, str]]:
    p = subprocess.run([sys.executable, "-c", _IMPORT_PROBE],
                       capture_output=True, text=True, timeout=120)
    seen: dict[str, tuple[str, str]] = {}
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t")
        if parts and parts[0] == "OK" and len(parts) >= 2:
            seen[parts[1]] = (OK, "")
        elif parts and parts[0] == "ERR" and len(parts) >= 3:
            seen[parts[1]] = (FAIL, parts[2])
    rows: list[tuple[str, str, str]] = []
    for m in REQUIRED_MODULES:
        if m in seen:
            lvl, detail = seen[m]
            rows.append((f"import {m}", lvl, detail))
        elif p.returncode < 0:
            rows.append((f"import {m}", FAIL,
                         f"interpreter crashed ({_signal_name(p.returncode)}) before this import reported"))
        else:
            rows.append((f"import {m}", FAIL, "not installed (pip install incomplete?)"))
    return rows


def check_mcp_wiring() -> tuple[str, str, str]:
    """Is the latch MCP server actually registered with Claude Code?

    This is the WIRING check, distinct from the environment checks above. The
    env can be perfectly healthy (deps import, sqlite-vec loads, embedder runs)
    while the kb_* tools are completely dead — because Claude Code reads MCP
    servers ONLY from the `claude mcp` registry (~/.claude.json via
    `claude mcp add`) or a project .mcp.json, NOT from `mcpServers` in
    settings.json. A settings.json-only install leaves hooks firing (so it
    looks half-alive) while no tool ever connects. This check catches exactly
    that gap.

    Advisory by design (WARN, not FAIL): doctor's exit code is about the
    ENVIRONMENT, and doctor may legitimately be run before the engine is wired.
    A WARN flags the missing wiring loudly without failing a pre-wiring env
    verify. Remedy in all not-OK cases: `bash bin/install_engine.sh`.
    """
    name = "MCP server wiring"
    claude = shutil.which("claude")
    if not claude:
        return name, SKIP, "claude CLI not on PATH; cannot verify MCP registration"
    outputs: dict[str, str] = {}
    registered: list[str] = []
    connected: list[str] = []
    for server in install_engine.ALL_SERVER_NAMES:
        try:
            p = subprocess.run([claude, "mcp", "get", server],
                               capture_output=True, text=True, timeout=30)
        except Exception as e:
            return name, WARN, f"could not query `claude mcp get {server}` ({e!r})"
        out = (p.stdout or "") + (p.stderr or "")
        outputs[server] = out
        if p.returncode == 0 and server in out:
            registered.append(server)
            low = out.lower()
            if "connected" in low and "not connected" not in low and "failed" not in low:
                connected.append(server)
    if not registered:
        return name, WARN, ("latch is NOT registered with Claude Code — the kb_* tools "
                            "will never connect even though this environment is healthy. "
                            "Fix: bash bin/install_engine.sh")
    if connected:
        primary = install_engine.SERVER_NAME
        if primary in connected:
            detail = "registered as 'latch' (user scope) and connected"
            legacy_connected = [s for s in connected if s != primary]
            if legacy_connected:
                detail += f"; legacy alias also connected: {', '.join(legacy_connected)}"
            return name, OK, detail
        return name, OK, (f"registered via legacy alias {connected[0]!r} and connected; "
                          f"fresh installs use {primary!r}")
    return name, WARN, (
        f"registered as {', '.join(registered)} but not connected — check the interpreter "
        "path it points at (see the env checks above), then restart Claude Code"
    )


def check_commands_installed() -> tuple[str, str, str]:
    """Are latch's slash commands copied into Claude Code's commands dir?

    install_engine (and the standalone install_commands.{sh,ps1}) copy
    commands/*.md into ~/.claude/commands/ with the <KB_HOME> placeholder
    resolved. If that step was skipped, /kb-compact et al. error 'Unknown skill'
    even though the engine + MCP are wired — the gap that bit the 2026-06-07 Mac
    install (id=1468 #1).

    WARN (not FAIL), like the MCP-wiring check: this is WIRING, not ENVIRONMENT,
    and the doctor may legitimately run before the commands are installed. The
    WARN surfaces the gap loudly without failing a pre-wiring env verify. Checks
    presence + that <KB_HOME> was resolved (a literal placeholder = broken copy).
    Runtime health of what the commands invoke is covered by the env checks above
    + the fixed bin wrappers (id=1467) — this is not a presence-only false PASS.
    Honors CLAUDE_COMMANDS_DIR.
    """
    name = "slash commands installed"
    src = SRC_DIR.parent / "commands"
    dest = Path(os.environ.get("CLAUDE_COMMANDS_DIR") or (Path.home() / ".claude" / "commands"))
    context = (
        f"source={src}; dest={dest}; HOME={Path.home()}; "
        f"CLAUDE_COMMANDS_DIR={os.environ.get('CLAUDE_COMMANDS_DIR') or '<unset>'}"
    )
    if not src.is_dir():
        return name, SKIP, f"no commands/ source at {src} ({context})"
    expected = sorted(p.name for p in src.glob("*.md"))
    if not expected:
        return name, SKIP, f"no command files in {src} ({context})"
    missing = [n for n in expected if not (dest / n).exists()]
    if missing:
        head = ", ".join(missing[:3]) + ("..." if len(missing) > 3 else "")
        return name, WARN, (f"{len(missing)}/{len(expected)} not in {dest} (e.g. {head}) - "
                            "/kb-compact will error 'Unknown skill'. "
                            f"{context}. Fix: bash bin/install_engine.sh")
    unresolved = []
    for n in expected:
        try:
            if "<KB_HOME>" in (dest / n).read_text(encoding="utf-8"):
                unresolved.append(n)
        except OSError:
            pass
    if unresolved:
        head = ", ".join(unresolved[:3]) + ("..." if len(unresolved) > 3 else "")
        return name, WARN, (f"{len(unresolved)} command(s) still contain a literal <KB_HOME> "
                            f"placeholder (e.g. {head}) - {context}. "
                            "Re-run bash bin/install_engine.sh")
    return name, OK, f"{len(expected)} command(s) in {dest} ({context})"


def check_claude_md_contract() -> tuple[str, str, str]:
    """Is latch's KB-workflow contract wired into this project's CLAUDE.md?

    CLAUDE.md and AGENTS.md both render from the shared claude_md_snippet.md
    (the single contract source). Claude sessions only receive latch's KB-first /
    gate / activity-surfacing instructions when that managed region is present.
    First-wiring is deliberately interactive (latch never silently edits a
    project's CLAUDE.md), so the engine installer does not force it — this check
    surfaces an unwired/drifted contract so the user can run the wiring step.

    Project-scoped: evaluates ./CLAUDE.md in the current working directory.
    WARN (never FAIL), like the other wiring checks; the deterministic
    PostToolUse hook surfaces KB activity even when the contract is unwired, so
    this is a nudge, not a blocker.
    """
    name = "CLAUDE.md contract"
    target = Path.cwd() / "CLAUDE.md"
    try:
        sys.path.insert(0, str(SRC_DIR))
        import claude_md_sync
        status = claude_md_sync.evaluate(target)
    except Exception as e:
        return name, SKIP, f"could not evaluate {target} ({e!r})"
    fix = "Fix: bash bin/install_claude_md.sh"
    if status == claude_md_sync.OK:
        return name, OK, f"latch contract region present + in sync ({target})"
    if status == claude_md_sync.DRIFT:
        return name, WARN, f"managed region in {target} has drifted from the snippet. {fix}"
    if status == claude_md_sync.MISSING:
        return name, WARN, (f"{target} exists but has no latch contract region — "
                            f"Claude sessions get no KB-workflow instructions. {fix}")
    return name, WARN, (f"no CLAUDE.md in {target.parent} — Claude sessions get no latch "
                        f"KB-workflow contract (the deterministic hook still surfaces "
                        f"activity). {fix}")


# --------------------------------------------------------------------------- #
# KB-directory pin / split-KB detection (id=1556)
# --------------------------------------------------------------------------- #
def _kb_home() -> Path:
    """The latch install root (holds projects/ and kb_location.json).

    Mirrors paths.KB_ROOT WITHOUT importing the package, so the orchestrator
    stays stdlib-only and runs on a broken venv: LATCH_HOME / CLAUDE_KB_HOME env,
    else the repo root (parent of src/)."""
    return Path(os.environ.get("LATCH_HOME") or os.environ.get("CLAUDE_KB_HOME") or SRC_DIR.parent)


def _read_pin(kb_home: Path) -> str | None:
    """The kb_dir recorded in kb_location.json, or None if absent/malformed."""
    try:
        data = json.loads((kb_home / "kb_location.json").read_text(encoding="utf-8"))
        kb_dir = (data or {}).get("kb_dir")
        return kb_dir.strip() if isinstance(kb_dir, str) and kb_dir.strip() else None
    except (OSError, ValueError):
        return None


def _discover_kb_dirs(kb_home: Path) -> list[tuple[Path, int]]:
    """Every projects/*/ holding a kb.db, as (dir, kb.db size in bytes).

    Size is a cheap proxy for "how much is in this KB" — a plain stat, no sqlite
    open — so this stays fast even with thousands of legacy per-cwd dirs. Real
    node counts are computed later for only the handful we actually display."""
    out: list[tuple[Path, int]] = []
    try:
        entries = list((kb_home / "projects").iterdir())
    except OSError:
        return out
    for d in entries:
        db = d / "kb.db"
        try:
            if db.is_file():
                out.append((d, db.stat().st_size))
        except OSError:
            continue
    return out


def _node_count(db: Path) -> int:
    """COUNT(*) of nodes, read-only; -1 if unreadable. Plain table read — no
    sqlite-vec extension load — so it never trips the Rosetta arch crash."""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return -1


def _pin_command(target: Path | str) -> str:
    # Both wrappers forward args verbatim to src/install_engine.py, whose parser
    # accepts ONLY --kb-dir; the PowerShell example therefore also uses --kb-dir
    # (a PowerShell-style -KbDir would be rejected by the Python argparse).
    return (f'bash bin/install_engine.sh --kb-dir "{target}"\n'
            f'    (PowerShell: .\\bin\\install_engine.ps1 --kb-dir "{target}")')


def _safe_resolve(p: Path | str) -> str:
    try:
        return str(Path(p).resolve())
    except Exception:
        return str(p)


def _format_top_kbs(kbs: list[tuple[Path, int]]) -> tuple[str, "Path | None", int]:
    """Listing of the largest KBs by real node count. Opens only the largest-by-
    file-size few (size tracks content closely; file size alone mis-ranks
    near-empty DBs), so it stays fast with thousands of dirs. Returns
    (listing, biggest_dir_or_None, shown_count)."""
    candidates = sorted(kbs, key=lambda x: -x[1])[:20]
    counted = sorted(((d, _node_count(d / "kb.db")) for d, _ in candidates),
                     key=lambda x: -x[1])
    top = counted[:5]
    listing = "\n".join(
        (f"      - {d.name}  ({c} node{'' if c == 1 else 's'})" if c >= 0
         else f"      - {d.name}  (unreadable)")
        for d, c in top
    )
    return listing, (top[0][0] if top else None), len(top)


def check_kb_pin() -> tuple[str, str, str]:
    """Is the KB directory pinned to ONE fixed location (id=1556)?

    An UNPINNED install selects the DB from the directory it is launched in
    (paths.py legacy per-cwd mode), so a different launch dir means a different
    KB — a session's work can silently land in, or be read from, the wrong KB,
    and per-cwd KBs pile up. Fresh installs are pinned automatically; installs
    from before the fix are not. This DETECTS the unpinned/split state and shows
    how to lock to one KB going forward.

    Even WITH a pin in place, if other KB dirs remain under projects/ this WARNs:
    future routing is safe, but pre-pin history sits stranded outside the pinned
    KB, so "healthy" would undersell the consolidation work still pending.

    It deliberately does NOT merge KBs: consolidating already-split history is a
    judgement call left to the user (do it by hand, or with Claude's help).
    Doctor only identifies the problem and recommends. WARN, never FAIL — an
    unpinned install still runs; the risk is silent splitting / stranded history,
    not a dead environment."""
    name = "KB directory pin"
    kb_home = _kb_home()
    env_pin = (os.environ.get("LATCH_KB_DIR") or os.environ.get("CLAUDE_KB_DIR") or "").strip()
    file_pin = _read_pin(kb_home)
    kbs = _discover_kb_dirs(kb_home)
    n = len(kbs)

    pin = env_pin or file_pin
    if pin:
        src = ("LATCH_KB_DIR" if os.environ.get("LATCH_KB_DIR")
               else "CLAUDE_KB_DIR" if os.environ.get("CLAUDE_KB_DIR")
               else "kb_location.json")
        pin_res = _safe_resolve(pin)
        others = [(d, s) for (d, s) in kbs if _safe_resolve(d) != pin_res]
        if not others:
            return name, OK, f"pinned -> {pin} ({src}); cwd cannot select the DB"
        # Future writes converge on the pin, but old per-cwd KBs still hold history.
        listing, _biggest, shown = _format_top_kbs(others)
        more = f"\n      …and {len(others) - shown} more" if len(others) > shown else ""
        return name, WARN, (
            f"pinned -> {pin} ({src}) — future writes are SAFE. But {len(others)} other "
            "KB dir(s) remain under projects/, holding history stranded outside the "
            "pinned KB. latch will NOT merge them; consolidate by hand if you need "
            "that history:\n"
            f"{listing}{more}"
        )

    # No pin: legacy per-cwd mode is live.
    if n <= 1:
        target = kbs[0][0] if kbs else "<your KB dir>"
        return name, WARN, (
            "NOT pinned — legacy per-cwd mode is active, so the KB is chosen from "
            "whatever directory you launch in; a different launch dir is a different "
            "KB. Lock to one now to prevent future splits:\n"
            f"    {_pin_command(target)}"
        )

    # n > 1 unpinned: a split already exists.
    listing, biggest, shown = _format_top_kbs(kbs)
    more = f"\n      …and {n - shown} more" if n > shown else ""
    return name, WARN, (
        f"NOT pinned AND {n} separate KB dirs exist under projects/ — your work may "
        "be split across them (each launch dir gets its own KB). latch will NOT "
        "merge them. Lock to ONE going forward (usually the largest), then "
        "consolidate the rest by hand if needed:\n"
        f"{listing}{more}\n"
        f"    {_pin_command(biggest)}"
    )


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run_all(skip_embed: bool, no_arch: bool, allow_old_py: bool,
            no_mcp: bool = False, no_commands: bool = False,
            no_pin: bool = False) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = [check_python_version(allow_old_py)]
    if no_arch:
        results.append(("CPU architecture", SKIP, "skipped (--no-arch)"))
    else:
        results.append(check_arch())
    imports = check_imports()
    results.extend(imports)
    imports_ok = all(lvl == OK for _, lvl, _ in imports)
    # The load/run probes need the modules present; skip (don't FAIL twice) if
    # imports already failed — the import failure is the actionable root cause.
    if imports_ok:
        results.append(("sqlite-vec extension load", *_run_probe(_VEC_PROBE, "VEC_OK", 30, arch_hint=True)))
        if skip_embed:
            results.append(("ONNX embedder", SKIP, "skipped (--skip-embed)"))
        else:
            results.append(("ONNX embedder", *_run_probe(_embed_probe(SRC_DIR), "EMBED_OK", 120, arch_hint=True)))
    else:
        results.append(("sqlite-vec extension load", SKIP, "skipped: required imports failed"))
        results.append(("ONNX embedder", SKIP, "skipped: required imports failed"))
    if no_mcp:
        results.append(("MCP server wiring", SKIP, "skipped (--no-mcp)"))
    else:
        results.append(check_mcp_wiring())
    if no_commands:
        results.append(("slash commands installed", SKIP, "skipped (--no-commands)"))
    else:
        results.append(check_commands_installed())
    results.append(check_claude_md_contract())
    if no_pin:
        results.append(("KB directory pin", SKIP, "skipped (--no-pin)"))
    else:
        results.append(check_kb_pin())
    return results


def render(results: list[tuple[str, str, str]]) -> int:
    print(f"\nlatch doctor - {platform.system()} {platform.machine()} - {sys.executable}\n")
    width = max(len(name) for name, _, _ in results)
    failed = 0
    for name, level, detail in results:
        if level == FAIL:
            failed += 1
        line = f"  {_MARK[level]} {name.ljust(width)}"
        if detail:
            head, *rest = detail.split("\n")
            line += f"  {head}"
            print(line)
            for r in rest:
                print(f"  {' ' * (len(_MARK[level]) + 1 + width)}{r}")
        else:
            print(line)
    print()
    if failed:
        print(f"FAILED - {failed} check(s) need attention before latch will work. "
              "See the lines above.\n")
    else:
        warns = sum(1 for _, lvl, _ in results if lvl == WARN)
        suffix = f" ({warns} warning(s))" if warns else ""
        print(f"OK - environment looks healthy for latch.{suffix}\n")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="latch install verifier (doctor).")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of a table")
    ap.add_argument("--skip-embed", action="store_true", help="skip the ONNX embedder probe (faster)")
    ap.add_argument("--no-arch", action="store_true", help="skip the macOS Rosetta/arch check")
    ap.add_argument("--no-mcp", action="store_true", help="skip the MCP server wiring check")
    ap.add_argument("--no-commands", action="store_true", help="skip the slash-commands-installed check")
    ap.add_argument("--no-pin", action="store_true", help="skip the KB-directory pin / split-KB check")
    args = ap.parse_args(argv)
    allow_old_py = bool(os.environ.get("LATCH_DOCTOR_ALLOW_OLD_PYTHON"))
    results = run_all(args.skip_embed, args.no_arch, allow_old_py, args.no_mcp,
                      args.no_commands, args.no_pin)
    if args.json:
        payload = {
            "system": platform.system(),
            "machine": platform.machine(),
            "executable": sys.executable,
            "python": platform.python_version(),
            "checks": [{"name": n, "level": l, "detail": d} for n, l, d in results],
            "ok": all(l != FAIL for _, l, _ in results),
        }
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1
    return render(results)


if __name__ == "__main__":
    sys.exit(main())
