"""Latch's MCP tool registry and heavyweight runtime.

Configured hosts still execute this path, but normal execution dispatches to a
small stdio proxy before importing FastMCP, NumPy, ONNX Runtime, or tokenizers.
One shared daemon imports this module, serves multiple logical MCP sessions,
and owns the warm embed listener/model.  ``LATCH_MCP_LEGACY=1`` preserves the
old one-process-per-stdio-session fallback.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import atexit
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))


def _repair_cursor_wiring_from_mcp_startup() -> None:
    """Repair older managed Cursor wiring before either runtime entrypoint."""
    if os.environ.get("LATCH_ADAPTER") != "cursor":
        return
    try:
        import cursor_wiring

        result = cursor_wiring.repair_from_mcp_startup()
        if result.notice:
            sys.stderr.write("[latch] " + result.notice.strip("_") + "\n")
    except Exception as exc:
        sys.stderr.write(
            "[latch] Cursor wiring check failed; session will continue. "
            f"Rerun bin/install_cursor manually ({exc}).\n"
        )


# Existing installs already launch this path.  Intercept execution before any
# MCP/NumPy/ONNX imports so each host context stays a small stdio proxy.  Imports
# of this module (tests and the shared daemon) still expose the tool registry.
if __name__ == "__main__":
    from windows_stdio import ensure_windows_standard_streams  # noqa: E402

    ensure_windows_standard_streams()
    _repair_cursor_wiring_from_mcp_startup()
    if not os.environ.get("LATCH_MCP_LEGACY"):
        from mcp_proxy import main as _proxy_main  # noqa: E402

        raise SystemExit(_proxy_main())

from mcp.server.fastmcp import FastMCP  # noqa: E402

import artifacts as artifact_store  # noqa: E402
import capture_streams  # noqa: E402
import codex_session  # noqa: E402
import cursor_gate_state  # noqa: E402
import db  # noqa: E402
import embeddings  # noqa: E402
import gate_report  # noqa: E402
import heal  # noqa: E402
import lifecycle_receipts  # noqa: E402
import lockfile  # noqa: E402
import mcp_broker  # noqa: E402
import mcp_runtime  # noqa: E402
import paths  # noqa: E402
import project_direction  # noqa: E402
import priorities  # noqa: E402
import rolling  # noqa: E402
import gate  # noqa: E402
import search  # noqa: E402
import selfheal  # noqa: E402
import verify  # noqa: E402
import workstreams  # noqa: E402

mcp = FastMCP("latch")
PROJECT_CWD = os.getcwd()
_RUNTIME_INIT_LOCK = threading.Lock()
_RUNTIME_INITIALIZED = False
_EMBED_OWNER: tuple[socket.socket, int, str] | None = None
SESSION_ID_ENV_VARS = (
    "LATCH_SESSION_ID",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
)
_LIFECYCLE_OWNED_EDGE_RELATIONS = frozenset({
    "merged_into",
    "closed_in_favor_of",
    "branched_from",
})


def _is_codex_adapter_env(env: Mapping[str, str]) -> bool:
    for name in ("LATCH_MODEL_BACKEND", "LATCH_GATE_BACKEND", "LATCH_MAINTENANCE_BACKEND"):
        if (env.get(name) or "").strip().lower() == "codex":
            return True
    return any((env.get(name) or "").strip() for name in ("CODEX_THREAD_ID", "CODEX_HOME"))


def _is_cursor_adapter_env(env: Mapping[str, str]) -> bool:
    return (env.get("LATCH_ADAPTER") or "").strip().lower() == "cursor"


def _resolve_project_session_id(
    env: Mapping[str, str] | None = None,
    project_cwd: str | os.PathLike | None = None,
) -> str | None:
    """Resolve the adapter session id captured by this MCP server process.

    Claude Code and Codex expose different per-session env vars. Keep the
    neutral latch override first for future adapters, preserve Claude's existing
    behavior next, then fall back to Codex's thread id or SessionStart marker.
    Once resolved, this session-scoped id becomes the common-header fallback for
    structural logs (id=1091 / id=1108).
    """
    source = os.environ if env is None else env
    # Cursor project hooks receive a conversation id, but calls into its reused
    # MCP process do not carry a verified per-request conversation identity.
    # A project marker is therefore only a current-transcript handoff for
    # explicit seed/compact workflows, never provenance for MCP structural
    # rows: two Cursor conversations can interleave in the same project.
    if _is_cursor_adapter_env(source):
        return None
    for name in SESSION_ID_ENV_VARS:
        value = (source.get(name) or "").strip()
        if value:
            return value
    if _is_codex_adapter_env(source):
        return codex_session.read_session_id(project_cwd or PROJECT_CWD)
    return None


PROJECT_SESSION_ID = _resolve_project_session_id()


def _project_cwd() -> str:
    context = mcp_runtime.current_connection()
    return context.project_cwd if context is not None else PROJECT_CWD


def _project_session_id() -> str | None:
    """Return the current adapter session id.

    Codex may launch the MCP server without CODEX_THREAD_ID in its environment.
    In that case the SessionStart hook writes a project-scoped marker. Do not
    cache None: the hook can create the marker after this module imports.

    Cursor can reuse one project MCP process across interleaved conversations,
    and MCP calls do not carry a verified conversation id.  Never infer one
    from the project's last SessionStart marker; unattributed is safer than a
    false cross-conversation attribution.  Claude/Codex environment-derived
    ids retain the existing stable process cache.
    """
    context = mcp_runtime.current_connection()
    if context is not None:
        # None is meaningful here: the proxy deliberately refused a mismatched
        # project-scoped Codex marker instead of misattributing this connection.
        return context.session_id
    global PROJECT_SESSION_ID
    if _is_cursor_adapter_env(os.environ):
        return None
    if PROJECT_SESSION_ID:
        return PROJECT_SESSION_ID
    sid = _resolve_project_session_id()
    if sid:
        PROJECT_SESSION_ID = sid
    return sid

# ---------- MCP payload size guardrails ----------
#
# See docs/claude_kb/mcp_payload_guards.md. Tools that return node-shaped rows
# (latch_search, latch_recent) compact `body` to `body_excerpt` + `body_chars` by
# default; pass `verbose=True` to opt back into full bodies. A boundary safety
# net catches the pathological case where compact mode still exceeds the cap.
COMPACT_LOG_FILE_NAME = "compact_excerpt.log"
# Threshold for the universal safety net. Set well under the FastMCP response
# cap (~100KB at typical density) so we have room to downgrade. Only consulted
# when verbose=False — verbose calls bypass the net entirely.
SAFETY_NET_BYTES = 80_000
# Fallback excerpt length when the safety net force-truncates.
SAFETY_NET_FALLBACK_CHARS = 200
LIFECYCLE_NOTICE_TEXT_CHARS = 600
LIFECYCLE_NOTICE_METADATA_CHARS = 160


def _log_compact(
    *, tool: str, row_count: int, total_bytes: int,
    verbose_requested: bool, safety_net_triggered: bool,
    excerpt_strategy: str,
) -> None:
    """Append one JSONL line per latch_search/latch_recent compact-mode call. Best-
    effort: any error is swallowed so logging never breaks the tool path."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "project": _project_cwd(),
            "tool": tool,
            "row_count": row_count,
            "total_bytes": total_bytes,
            "verbose_requested": verbose_requested,
            "safety_net_triggered": safety_net_triggered,
            "excerpt_strategy": excerpt_strategy,
        }
        path = paths.project_dir(_project_cwd()) / COMPACT_LOG_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _apply_safety_net(rows: list[dict]) -> tuple[list[dict], bool]:
    """If the JSON-encoded payload exceeds SAFETY_NET_BYTES, force-truncate
    every `body_excerpt` to SAFETY_NET_FALLBACK_CHARS in place. Last-line
    defense — most calls never trigger this. Returns (rows, triggered)."""
    try:
        encoded = json.dumps(rows, default=str)
    except Exception:
        return rows, False
    if len(encoded.encode("utf-8")) <= SAFETY_NET_BYTES:
        return rows, False
    for r in rows:
        excerpt = r.get("body_excerpt")
        if isinstance(excerpt, str) and len(excerpt) > SAFETY_NET_FALLBACK_CHARS:
            r["body_excerpt"] = excerpt[:SAFETY_NET_FALLBACK_CHARS].rstrip() + "…"
    r0 = rows[0] if rows else None
    if r0 is not None:
        r0["safety_net_triggered"] = True
    return rows, True


def _compact_search_rows(rows: list[dict]) -> tuple[list[dict], str]:
    """Map hybrid_search rows through db.compact_row, using the FTS5 snippet
    when present and falling back to a prefix excerpt otherwise. Returns
    (compact_rows, strategy) where strategy is one of {snippet, prefix, mixed,
    none}."""
    if not rows:
        return [], "none"
    snippet_count = 0
    out: list[dict] = []
    for r in rows:
        snip = r.pop("_fts_snippet", None) if isinstance(r, dict) else None
        if snip:
            snippet_count += 1
        out.append(db.compact_row(r, snippet_text=snip))
    if snippet_count == len(out):
        strategy = "snippet"
    elif snippet_count == 0:
        strategy = "prefix"
    else:
        strategy = "mixed"
    return out, strategy


def _compact_recent_rows(rows: list[dict]) -> list[dict]:
    """latch_recent has no query, so no FTS snippet — prefix excerpts only."""
    return [db.compact_row(r) for r in rows]


def _activity_node(row: dict | None) -> dict | None:
    """Small node identity for foreground KB activity hints."""
    if not row:
        return None
    out = {k: row.get(k) for k in ("id", "kind", "title", "status") if row.get(k) is not None}
    return out or None


def _activity_nodes(rows: list[dict], limit: int = 5) -> list[dict]:
    return [n for n in (_activity_node(r) for r in rows[:limit]) if n]


def _kb_activity(
    *,
    action: str,
    tool: str,
    summary: str,
    nodes: list[dict] | None = None,
    hints: list[str] | None = None,
) -> dict:
    """Deterministic, content-light foreground hint: ids/titles, never bodies."""
    return {
        "label": "Latch KB activity",
        "must_display_to_user": True,
        "action": action,
        "tool": tool,
        "summary": summary,
        "nodes": nodes or [],
        "hints": hints or [],
    }


def _activity_hints(result: dict) -> list[str]:
    labels: list[str] = []
    for key in (
        "plan_freshness_hint",
        "orphan_hint",
        "ship_edge_hint",
        "claim_change_hint",
        "reconciliation_banner",
    ):
        value = result.get(key)
        if value:
            labels.append(key)
    return labels


def _stamp_list_activity(rows: list[dict], activity: dict) -> list[dict]:
    """Preserve list-shaped read tools while carrying foreground metadata."""
    if rows:
        rows[0]["kb_activity"] = activity
    return rows


def _conn():
    return db.connect(_project_cwd())


def _resolve_membership_for_mcp(
    conn, workstream_id: int | None,
) -> tuple[int | None, dict | None, dict | None]:
    """Return ``(active_id, redirect, error_response)`` for MCP writes."""
    if workstream_id is None:
        return None, None, None
    resolution = workstreams.resolve_membership_target(conn, workstream_id)
    if not resolution["ok"]:
        return None, None, {
            "ok": False,
            "error": resolution["error"],
            "workstream_resolution": resolution,
        }
    return (
        int(resolution["resolved_workstream_id"]),
        resolution if resolution.get("redirected") else None,
        None,
    )


def _record_tool_retrievals(conn, rows: list[dict]) -> None:
    """Best-effort event telemetry for MCP read tools.

    Cursor's shared MCP process may not carry a session id; those contacts are
    still useful for project-level lane activity and are intentionally stored
    with a NULL session.  Read results must never fail because telemetry did.
    """
    items = [
        (int(row["id"]), float(row["score"]) if row.get("score") is not None else None)
        for row in rows
        if row.get("id") is not None
    ]
    if not items:
        return
    try:
        db.record_retrieval_events(
            conn,
            source="tool",
            items=items,
            session_id=_project_session_id(),
        )
    except Exception:
        # The DB helper makes SQLite append failures observable via its
        # persistent counter.  This outer rail protects reads from unexpected
        # compatibility/runtime faults too.
        pass


def _record_write_events(conn, node_ids: list[int]) -> None:
    """Best-effort lifecycle contacts for node writes in this MCP session."""
    try:
        db.record_retrieval_events(
            conn,
            source="write",
            items=[(int(node_id), None) for node_id in node_ids],
            session_id=_project_session_id(),
        )
    except Exception:
        pass


def _unlatched_response(tool: str) -> dict:
    return {
        "ok": False,
        "reason": "unlatched",
        "message": paths.UNLATCHED_MESSAGE,
        "tool": tool,
    }


def _writer_lock_busy_response() -> dict:
    """Stable MCP response for a live compactor or lifecycle writer."""
    return {
        "ok": False,
        "reason": "compaction_in_progress",
        "retry_after_s": 10,
        "message": (
            f"A compaction or another live writer has held the project "
            f"lock for >{lockfile.WRITE_LOCK_TIMEOUT_S:.0f}s. This is "
            f"normally a healthy concurrent process, not a fault — don't "
            f"investigate why. Retry this write once; if it is still "
            f"locked, stop and ask the user whether to retry again or "
            f"investigate the lock."
        ),
    }


def _run_public_kb_mutation(
    operation: Callable[[Any], Any],
) -> Any:
    """Serialize a public KB write with lifecycle operations.

    Acquire the shared project writer lock before opening the SQLite
    connection, then keep it through membership/identity resolution and every
    commit performed by ``operation``.  Acquiring in this order closes the
    check/use race left by ``wait_for_compaction``: a lifecycle writer cannot
    change a lane after a generic tool resolves it but before that tool commits.

    The lock is same-thread reentrant.  Low-level helpers therefore remain
    usable from lifecycle transactions that already own it without changing
    the single public lock order (writer lock, then SQLite connection).
    """
    project_path = _project_cwd()
    try:
        with lockfile.writer_lock(project_path):
            with _conn() as conn:
                return operation(conn)
    except lockfile.CompactionInProgressError:
        return _writer_lock_busy_response()


def _kb_activity_carrier(result: Any) -> dict | None:
    """Return the dict whose ``kb_activity`` is visible on this read result."""
    if isinstance(result, dict) and isinstance(result.get("kb_activity"), dict):
        return result
    if (
        isinstance(result, list)
        and result
        and isinstance(result[0], dict)
        and isinstance(result[0].get("kb_activity"), dict)
    ):
        return result[0]
    return None


def _attach_pending_lifecycle_notice(result: Any) -> Any:
    """Attach, validate, then claim one rare lifecycle foreground notice.

    Selection is read-only. The exact item is claimed on a fresh writer
    connection only after a successful result has a JSON-serializable
    ``kb_activity`` carrier. If the claim loses a race or cannot run, restore
    the original activity so the result never implies that an unclaimed notice
    was delivered. Project-direction deliberately does not call this helper.
    """
    if not lifecycle_receipts.RECEIPTS_CHANNEL_LIVE:
        return result
    carrier = _kb_activity_carrier(result)
    if carrier is None:
        return result
    try:
        with db.connect_readonly(_project_cwd()) as conn:
            pending = lifecycle_receipts.pending_surface_items(conn, limit=1)
    except Exception:
        return result
    if not pending:
        return result

    item = pending[0]
    original_activity = carrier["kb_activity"]
    notice = {
        key: (
            str(item[key])[:(
                LIFECYCLE_NOTICE_TEXT_CHARS
                if key == "text"
                else LIFECYCLE_NOTICE_METADATA_CHARS
            )]
            if isinstance(item[key], str)
            else item[key]
        )
        for key in (
            "surface_kind",
            "text",
            "op",
            "op_key",
            "operation_id",
            "candidate_key",
            "workstream_id",
        )
        if item.get(key) is not None
    }
    prepared_activity = dict(original_activity)
    prepared_activity["summary"] = (
        f"{str(original_activity.get('summary') or '').rstrip()} "
        f"Lifecycle {notice['surface_kind']}: {notice['text']}"
    ).strip()
    prepared_activity["lifecycle_notice"] = notice
    carrier["kb_activity"] = prepared_activity
    try:
        # Match the compact-response boundary before making the one-way claim.
        # Oversized base reads remain valid authority reads, but must not
        # consume a notice the host may reject before delivery.
        encoded = json.dumps(result, default=str).encode("utf-8")
        if len(encoded) > SAFETY_NET_BYTES:
            carrier["kb_activity"] = original_activity
            return result
        project_path = _project_cwd()
        with lockfile.writer_lock(project_path, timeout_s=0.0):
            with _conn() as conn:
                claim = lifecycle_receipts.claim_pending_surface_item(
                    conn,
                    item,
                    session_id=_project_session_id(),
                )
        if isinstance(claim, dict) and claim.get("created") is True:
            return result
    except Exception:
        pass
    carrier["kb_activity"] = original_activity
    return result


def _run_public_priority_mutation(
    operation: Callable[[Any], dict],
) -> dict:
    """Compatibility wrapper for the priority-specific public boundary."""
    return _run_public_kb_mutation(operation)


def _priority_edge_error(conn, node_ids) -> dict | None:
    """Reject public graph edges that would make priorities traversable."""
    normalized: set[int] = set()
    for value in node_ids:
        if value is None:
            continue
        if isinstance(value, bool):
            return {
                "ok": False,
                "error": "graph edge node ids must be positive integers",
            }
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str) and value.isdigit() and not value.startswith("0"):
            parsed = int(value)
        else:
            return {
                "ok": False,
                "error": "graph edge node ids must be positive integers",
            }
        if parsed <= 0:
            return {
                "ok": False,
                "error": "graph edge node ids must be positive integers",
            }
        normalized.add(parsed)
    if not normalized:
        return None
    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"SELECT id FROM nodes WHERE kind='priority' "
        f"AND id IN ({placeholders}) ORDER BY id",
        sorted(normalized),
    ).fetchall()
    priority_ids = [int(row["id"]) for row in rows]
    if not priority_ids:
        return None
    return {
        "ok": False,
        "error": (
            "priorities are surface-only and machine-owned; generic graph "
            "edges cannot touch a priority; use the priority service"
        ),
        "priority_node_ids": priority_ids,
    }


def _link_destination_ids(links: list[dict] | None) -> list[Any]:
    return [
        link.get("dst")
        for link in (links or [])
        if isinstance(link, Mapping)
    ]


def _start_embed_listener(project_cwd: str) -> None:
    """Bind 127.0.0.1:0, write discovery file, accept embed RPCs in daemon
    threads, and kick off a model pre-load so the first real request doesn't
    block on cold-load. Best-effort — failures are logged via stderr only,
    since the MCP tool surface still works without it (just slower hooks).
    """
    global _EMBED_OWNER
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(8)
        host, port = sock.getsockname()

        token = secrets.token_hex(16)
        paths.ensure_project_dir(project_cwd)
        disc_path = mcp_broker.embed_discovery_path()
        payload = json.dumps({
            "runtime_key": mcp_broker.RUNTIME_KEY,
            "host": host,
            "port": port,
            "token": token,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        tmp_path = disc_path.with_name(f".{disc_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, disc_path)
        _EMBED_OWNER = (sock, os.getpid(), token)
    except Exception as e:
        sys.stderr.write(f"[latch] embed listener bind failed: {e}\n")
        return

    def _handle(client: socket.socket) -> None:
        try:
            client.settimeout(10.0)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 1024 * 1024:
                    client.sendall(b'{"error":"oversize"}\n')
                    return
            line = bytes(buf).split(b"\n", 1)[0]
            if not line:
                return
            try:
                req = json.loads(line.decode("utf-8"))
            except ValueError:
                client.sendall(b'{"error":"bad_json"}\n')
                return
            if req.get("token") != token:
                client.sendall(b'{"error":"bad_token"}\n')
                return
            op = req.get("op")
            if op == "ping":
                client.sendall(b'{"ok":true}\n')
                return
            if op != "embed":
                client.sendall(b'{"error":"unknown_op"}\n')
                return
            text = req.get("text", "")
            if not isinstance(text, str):
                client.sendall(b'{"error":"text_not_string"}\n')
                return
            mcp_runtime.touch_daemon()
            vec = embeddings.embed(text)
            mcp_runtime.touch_daemon()
            client.sendall(json.dumps({"vec": vec.tolist()}).encode("utf-8") + b"\n")
        except Exception as e:
            try:
                client.sendall(json.dumps({"error": str(e)}).encode("utf-8") + b"\n")
            except Exception:
                pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def _accept_loop() -> None:
        while True:
            try:
                client, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(client,), daemon=True).start()

    threading.Thread(target=_accept_loop, daemon=True).start()
    # NOTE: model pre-warm is done synchronously on the main thread before
    # mcp.run() (see __main__ block). Doing it on a daemon thread races the
    # Windows DLL loader with FastMCP's asyncio init and deadlocks scipy's
    # C-extension imports, which jams Thread.start() process-wide.


def shutdown_runtime() -> None:
    """Remove process-owned discovery and close the embed listener."""
    global _EMBED_OWNER
    owner, _EMBED_OWNER = _EMBED_OWNER, None
    if owner is None:
        return
    sock, pid, token = owner
    try:
        sock.close()
    except OSError:
        pass
    mcp_broker.remove_embed_discovery_if_owner(pid=pid, token=token)


atexit.register(shutdown_runtime)


async def run_shared_session(read_stream, write_stream) -> None:
    """Compatibility boundary for FastMCP's custom-stream server runner.

    FastMCP does not expose a public custom-stream entry point in the pinned
    release.  Keep the private lookup isolated here, validate its shape, and
    cover this adapter with a focused test so dependency upgrades fail loudly.
    """
    server = getattr(mcp, "_mcp_server", None)
    run = getattr(server, "run", None)
    options = getattr(server, "create_initialization_options", None)
    if server is None or not callable(run) or not callable(options):
        raise RuntimeError(
            "installed mcp package is incompatible with latch's shared runtime; "
            "install the version range pinned in requirements.txt"
        )
    await run(read_stream, write_stream, options())


@mcp.tool(name="latch_search")
@mcp.tool(name="kb_search")
def kb_search(
    query: str,
    kind: str | None = None,
    limit: int = 10,
    verbose: bool = False,
) -> dict | list[dict]:
    """Hybrid search (FTS keyword + semantic) over the project KB.

    Args:
        query: free-text query
        kind:  optional filter — fact | decision | progress | entity | preference | open_question
        limit: max results (default 10)
        verbose: if False (default), return compact rows with `body_excerpt`
            (FTS5 snippet of the matched span when available, prefix
            otherwise) and `body_chars` (true body length). If True, return
            full `body`. Compact mode keeps responses under the MCP tool-
            result cap; drill into specific nodes via `latch_get(<id>)`.

    Returns: list of node dicts. Compact-mode rows include `id`, `kind`,
    `title`, `body_excerpt`, `body_chars`, `status`, `score`, `updated_at`,
    plus the standard metadata fields. When non-empty, the first row carries
    `kb_activity` with a foreground summary agents must show; a rare pending
    lifecycle receipt or suggestion may be attached there after the read
    transaction closes. Full bodies are still on disk —
    `body_chars > len(body_excerpt)` signals truncation.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_search")
    with _conn() as conn:
        results = search.hybrid_search(
            conn, query, kind=kind, limit=limit, scope_repo=_project_cwd()
        )
        if results:
            db.bump_focus_for_nodes(conn, [r["id"] for r in results])
            _record_tool_retrievals(conn, results)
    activity = _kb_activity(
        action="read",
        tool="latch_search",
        summary=(
            f"Read {len(results)} KB search result(s)"
            + (f" for kind={kind}" if kind else "")
            + "."
        ),
        nodes=_activity_nodes(results),
    )
    if verbose:
        for r in results:
            r.pop("_fts_snippet", None)
        return _attach_pending_lifecycle_notice(
            _stamp_list_activity(results, activity)
        )
    compact, strategy = _compact_search_rows(results)
    compact, triggered = _apply_safety_net(compact)
    _log_compact(
        tool="latch_search", row_count=len(compact),
        total_bytes=len(json.dumps(compact, default=str).encode("utf-8")),
        verbose_requested=False, safety_net_triggered=triggered,
        excerpt_strategy=strategy,
    )
    return _attach_pending_lifecycle_notice(
        _stamp_list_activity(compact, activity)
    )


@mcp.tool(name="latch_get")
@mcp.tool(name="kb_get")
def kb_get(
    node_id: int,
    include_neighbors: bool = True,
    resolve_workstream: bool = True,
) -> dict:
    """Fetch a single node, optionally with its 1-hop neighbors. Bumps ref_count.

    Also returns `reconciliation_banner`: a list of `{linked_id, kind, title,
    status}` entries surfaced when this node has outgoing `reconciled_by`
    edges to non-stale nodes. Non-empty means this node's framing has been
    partially updated by a newer canonical decision — the reader MUST also
    fetch the reconciling nodes before treating this body as authoritative.
    See CLAUDE.md "KB read hygiene" mandate. Empty list means no
    reconciliation has been declared.

    Distinct from `supersedes` (full replacement, marks old stale). A node
    surfaced via `reconciliation_banner` remains canonical and factually true
    in its own scope — only the framing has been constrained or updated.

    A successful result may also carry one rare pending lifecycle receipt or
    suggestion in `kb_activity`. It is selected read-only and claimed only
    after the result is prepared on a fresh connection.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_get")
    with _conn() as conn:
        node = db.get_node(conn, node_id)
        if not node:
            return {"error": f"node {node_id} not found"}
        workstream_resolution = None
        resolved_node_id = int(node_id)
        if resolve_workstream and node["kind"] == "workstream":
            resolution = workstreams.resolve_active(conn, node_id)
            if resolution["state"] == "merged" and resolution["active_id"] is not None:
                resolved_node_id = int(resolution["active_id"])
                redirected = db.get_node(conn, resolved_node_id)
                if redirected is None:  # fail closed on a corrupted identity chain
                    return {"error": f"merged workstream {node_id} has no active target"}
                node = redirected
                merge_evidence = workstreams.merge_receipts_for_path(
                    conn, resolution["path"],
                )
                durable_receipt = next(
                    (
                        item["receipt"] for item in merge_evidence
                        if item.get("receipt")
                    ),
                    None,
                )
                workstream_resolution = {
                    "requested_workstream_id": int(node_id),
                    "resolved_workstream_id": resolved_node_id,
                    "state": "merged",
                    "path": resolution["path"],
                    "receipt": (
                        durable_receipt
                        or f"latch redirected merged workstream {int(node_id)} to "
                           f"active workstream {resolved_node_id}."
                    ),
                    "merge_evidence": merge_evidence,
                }
        node.pop("embedding", None)
        if include_neighbors:
            node["neighbors"] = db.neighbors(conn, resolved_node_id)
        node["reconciliation_banner"] = db.reconciliation_banner(conn, resolved_node_id)
        if workstream_resolution is not None:
            node["workstream_resolution"] = workstream_resolution
        db.bump_ref_count(conn, [resolved_node_id])
        db.bump_focus_for_nodes(conn, [resolved_node_id])
        _record_tool_retrievals(conn, [node])
        node["kb_activity"] = _kb_activity(
            action="read",
            tool="latch_get",
            summary=f"Read KB {node['kind']} node id={resolved_node_id}: {node['title']}.",
            nodes=[_activity_node(node)],
            hints=_activity_hints(node),
        )
    return _attach_pending_lifecycle_notice(node)


@mcp.tool(name="latch_recent")
@mcp.tool(name="kb_recent")
def kb_recent(
    session_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    created_by: str | None = None,
    limit: int = 20,
    verbose: bool = False,
) -> dict | list[dict]:
    """Raw chronology of recently updated nodes, optionally filtered.

    Pass `created_by="alice"` (or any user identifier) to see what a specific
    user has been working on. Attribution is metadata only — never used as
    input to ranking or arbitration.

    This is chronological evidence, not a synthesized catch-up or project-
    direction view. For "where did we leave off?", "catch me up", "resume", or
    "what's next", first call `latch_project_direction(compact=True)`, add
    `latch_recent(kind="progress", limit=3)` for raw chronology, then fetch the
    report's `foregrounded_item.id` with `latch_get`.

    `verbose` (default False): compact rows return `body_excerpt` (prefix
    excerpt — kb_recent has no query, so no FTS snippet is available) +
    `body_chars`. Pass `verbose=True` for full `body`. Compact mode keeps the
    response under the MCP tool-result cap; drill into specific nodes via
    `latch_get(<id>)`.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_recent")
    with _conn() as conn:
        rows = db.recent_nodes(
            conn, session_id=session_id, kind=kind, status=status,
            created_by=created_by, limit=limit,
        )
        _record_tool_retrievals(conn, rows)
    activity = _kb_activity(
        action="read",
        tool="latch_recent",
        summary=f"Read {len(rows)} recent KB node(s).",
        nodes=_activity_nodes(rows),
    )
    if verbose:
        for r in rows:
            r.pop("embedding", None)
        return _attach_pending_lifecycle_notice(
            _stamp_list_activity(rows, activity)
        )
    compact = _compact_recent_rows(rows)
    compact, triggered = _apply_safety_net(compact)
    _log_compact(
        tool="latch_recent", row_count=len(compact),
        total_bytes=len(json.dumps(compact, default=str).encode("utf-8")),
        verbose_requested=False, safety_net_triggered=triggered,
        excerpt_strategy="prefix",
    )
    return _attach_pending_lifecycle_notice(
        _stamp_list_activity(compact, activity)
    )


@mcp.tool(name="latch_project_direction")
@mcp.tool(name="kb_project_direction")
def kb_project_direction(
    limit: int = 3,
    member_limit: int = 20,
    unanchored_limit: int = 5,
    compact: bool = False,
) -> dict:
    """Read-only project-direction report.

    Assembles the current workstream spine from existing KB primitives:
    active/recent workstreams, governing decisions with derived authority
    tiers, backlog/open items, constraints, recent progress, artifact
    coordinates, recent unanchored evidence, active priorities, a labeled
    declared-or-inferred next action, omitted counts, and a foregrounded item.
    This is a report layer over current nodes/edges/focus/artifact tables, not
    a broad storage or retrieval rebuild.

    For "where did we leave off?", "catch me up", "resume", or "what's next",
    use `compact=True`. Compact mode clamps caller-supplied limits and trims
    every repeated section under a hard payload ceiling. Complete the catch-up
    with `latch_recent(kind="progress", limit=3)` and `latch_get` on
    `foregrounded_item.id`. The tool remains strictly read-only in both modes
    and never claims lifecycle notices.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_project_direction")
    with _conn() as conn:
        report = project_direction.assemble_project_direction(
            conn,
            limit=limit,
            member_limit=member_limit,
            unanchored_limit=unanchored_limit,
            compact=compact,
        )
    report["kb_activity"] = _kb_activity(
        action="read",
        tool="latch_project_direction",
        summary=report["summary"],
        nodes=[
            {
                "id": row["id"],
                "kind": "workstream",
                "title": row["title"],
                "status": row["status"],
            }
            for row in report.get("workstreams", [])[:5]
        ],
    )
    if compact:
        report = project_direction.enforce_compact_report_bytes(report)
    return report


@mcp.tool(name="latch_gate_report")
@mcp.tool(name="kb_gate_report")
def kb_gate_report(
    days: int = 14,
    start: str | None = None,
    end: str | None = None,
    limit: int = 10,
) -> dict:
    """Read-only report over recent gate activity.

    Summarizes existing structural logs (`gate`, `adversary`, `decision`, and
    `gate_outcome`) plus current KB node metadata. It does not run a new gate,
    read raw prompt text, read node bodies, or write new KB rows.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_gate_report")
    start_date = gate_report._parse_date(start) if start else None
    end_date = gate_report._parse_date(end) if end else None
    with _conn() as conn:
        report = gate_report.assemble_gate_report(
            conn,
            project_path=_project_cwd(),
            start=start_date,
            end=end_date,
            days=days,
            limit=limit,
        )
    report["kb_activity"] = _kb_activity(
        action="read",
        tool="latch_gate_report",
        summary=report["summary"],
        nodes=[
            {
                "id": row["id"],
                "kind": row["kind"],
                "title": row["title"],
                "status": row["status"],
            }
            for row in report.get("top_evidence_nodes", [])[:5]
            if row.get("status") != "missing"
        ],
    )
    return report


@mcp.tool(name="latch_pm_preview")
@mcp.tool(name="kb_pm_preview")
def latch_pm_preview(
    title: str,
    body: str,
    kind: str = "decision",
    status: str = "staging",
    links: list[dict] | None = None,
    workstream_id: int | None = None,
) -> dict:
    """Render and digest an exact PM candidate without writing to the KB.

    Cursor's PM workflow uses this read-only tool result as the displayed
    preview. The postToolUse hook verifies this result against the structured
    tool input before arming one content-bound latch_insert confirmation.
    """
    try:
        return cursor_gate_state.pm_preview_payload({
            "kind": kind,
            "title": title,
            "body": body,
            "status": status,
            "links": links,
            "workstream_id": workstream_id,
        })
    except ValueError as exc:
        return {
            "ok": False,
            "operation": "latch-pm",
            "phase": "prepare",
            "write_performed": False,
            "error": str(exc),
        }


@mcp.tool(name="latch_insert")
@mcp.tool(name="kb_insert")
def kb_insert(
    kind: str,
    title: str,
    body: str,
    status: str = "staging",
    session_id: str | None = None,
    links: list[dict] | None = None,
    workstream_id: int | None = None,
    artifacts: list[dict] | None = None,
) -> dict:
    """Insert a node and optional outgoing edges.

    `links` is a list of {"dst": <node_id>, "relation": "<verb>"} entries.
    Use this inline during a session to capture facts/decisions as you learn them.
    Default status is 'staging'; promote to 'canonical' on review.

    `workstream_id` (optional) tags the new node as belonging to a workstream
    (kind='workstream' node). Used by step 9 focus auto-bump and latch_gate
    traversal seeding. Pass the id of the relevant workstream when known;
    otherwise leave NULL — orphan nodes are tolerated.

    `artifacts` (optional) records the repo(s)/file(s) this node's work touched —
    a list of {"repo": "<path>", "path": "<file>"|null} entries (a bare repo
    string is also accepted). Stored as provenance coordinates (see artifacts.py).
    When omitted, a coarse repo=<this project's cwd> stamp is recorded; pass
    explicit `artifacts` for accurate multi-repo / file-level provenance — e.g.
    when working on another repo from this project's folder. The linked
    coordinate ids are returned under the `artifacts` key.

    For `relation`, prefer the canonical traversal vocabulary when an edge
    fits cleanly: `supersedes`, `replaces`, `constrains`, `motivates`,
    `tested_against`, `depends_on`. Free-form verbs (`related_to`,
    `implements`, `answers`, etc.) are also accepted. The system canonicalizes
    known synonyms (`relates_to` → `related_to`; `requires` → `depends_on`)
    on insert.

    Runs on-insert heal: if a node above the similarity threshold exists, an
    LLM arbitrator decides supersede vs keep_both. Supersede marks the old
    node stale and adds a `supersedes` edge; keep_both adds a `related_to`
    edge. Result includes `heal`, `matched_id`, and `similarity` when a
    near-duplicate was found.

    Also returns `plan_freshness_hint`: a list of `{linked_id, relation, kind,
    title}` entries surfaced when this is a `kind="progress"` node linking to
    a plan-shaped node (kind in {progress, decision, workstream}) via
    `implements` / `advances` / `depends_on`. When non-empty, the agent MUST
    reflect the new ship state in each listed `linked_id`'s body: prefer
    `latch_append` (delta-only — no full-body resend/re-embed) for workstream/
    progress nodes, and `latch_update` for a decision/plan node — see the
    "KB write hygiene" mandate. Empty list means no nudge applies.

    Also returns `orphan_hint`: a list of `{referenced_id, body_excerpt}`
    entries for `id=X` mentions in the body that lack an active edge to/from
    the new node. When non-empty, `latch_link` each (or drop the stale mention)
    before moving on — see "Body-id mentions must be edges". (id=1149 Part 2.)
    Kind-scoped to spec kinds (idea/open_question/decision) per id=1194 §1/§2,
    so index/summary kinds (workstream/progress/fact/entity) no longer over-fire.

    Also returns `ship_edge_hint`: non-empty when this is a `progress` node
    linking to a spec node (idea/open_question/decision) via `related_to` — a
    likely mis-typed ship edge that should be implements/advances/depends_on so
    plan-freshness can track the spec's body freshness. (id=1194 §4.)
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_insert")
    if kind == "workstream":
        return {
            "ok": False,
            "error": (
                "workstream lifecycle is machine-owned; use the internal "
                "workstream OPEN operation"
            ),
        }
    if kind == "priority":
        return {
            "ok": False,
            "error": (
                "priority ordering and lifecycle are machine-owned; use "
                "latch_priority_add"
            ),
        }

    def _insert(conn) -> dict:
        priority_error = _priority_edge_error(
            conn, _link_destination_ids(links),
        )
        if priority_error is not None:
            return priority_error
        resolved_workstream_id, workstream_resolution, scope_error = (
            _resolve_membership_for_mcp(conn, workstream_id)
        )
        if scope_error is not None:
            return scope_error
        result = heal.insert_with_heal(
            conn, kind=kind, title=title, body=body, status=status,
            session_id=session_id or _project_session_id(),
            links=links, use_llm=True,
            workstream_id=resolved_workstream_id, project_path=_project_cwd(),
            # Evidence contract: pass intended artifacts so on-insert heal sees the
            # new node's repo scope BEFORE arbitration (provenance is attached just
            # below, after this returns). Evidence only — never blocks the insert.
            artifacts=artifacts,
        )
        new_id = result.get("id")
        if new_id is not None:
            db.bump_focus_for_nodes(conn, [new_id])
            _record_write_events(conn, [new_id])
            captured = artifact_store.capture_for_node(
                conn, new_id, artifacts=artifacts, project_cwd=_project_cwd(),
            )
            if captured:
                result["artifacts"] = captured
            result["kb_activity"] = _kb_activity(
                action="write",
                tool="latch_insert",
                summary=f"Tracked KB {kind} node id={new_id}: {title}.",
                nodes=[_activity_node({
                    "id": new_id, "kind": kind, "title": title, "status": status,
                })],
                hints=_activity_hints(result),
            )
        if workstream_resolution is not None:
            result["workstream_resolution"] = workstream_resolution
        return result

    return _run_public_kb_mutation(_insert)


@mcp.tool(name="latch_update")
@mcp.tool(name="kb_update")
def kb_update(
    node_id: int,
    title: str | None = None,
    body: str | None = None,
    status: str | None = None,
    workstream_id: int | None = None,
) -> dict:
    """Update fields on an existing node. Re-embeds if title or body changes.

    Also returns `orphan_hint`: a list of `{referenced_id, body_excerpt}`
    entries for `id=X` mentions in the effective body that lack an active edge
    to/from this node. When non-empty, `latch_link` each (or drop the stale
    mention) before moving on — see "KB write hygiene" / "Body-id mentions
    must be edges". Empty list means every mention is edged. (id=1149 Part 2.)

    Also returns `claim_change_hint`: non-None when an in-place body edit looks
    like a CLAIM change on a canonical fact/decision (material embedding shift,
    old body not preserved). Policy id=1174 routes claim changes through
    `latch_correct` so the transition stays auditable; this surfaces the nudge at
    write time (spec id=1175). It is a NUDGE, not a block — the write proceeds;
    the agent/human decides whether to redo it via `latch_correct_plan`. None when
    the edit is a non-claim edit (banner/typo/status promotion) or a non-
    fact/decision / non-canonical node.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_update")

    def _update(conn) -> dict:
        node = db.get_node(conn, node_id)
        if not node:
            return {"error": f"node {node_id} not found"}
        if node["kind"] == "workstream":
            return {
                "ok": False,
                "error": (
                    "workstream title, body, status, and membership are "
                    "machine-owned; use latch_append for rolling state or the "
                    "internal lifecycle service"
                ),
            }
        if node["kind"] == "priority":
            return {
                "ok": False,
                "error": (
                    "priority membership and state are owned by the priority "
                    "service; use latch_priority_add, latch_priority_reorder, "
                    "or latch_priority_retire"
                ),
            }
        # Membership resolution is a write-boundary for an explicitly requested
        # move, not a prerequisite for editing the node's other fields.  In
        # particular, historical knowledge remains editable after its lane is
        # CLOSED, and a plain correction must not silently reparent a member of
        # a merged-away lane.  This mirrors latch_append: resolve only the
        # identity that the caller actually asked the operation to target.
        resolved_workstream_id = None
        workstream_resolution = None
        if workstream_id is not None:
            resolved_workstream_id, workstream_resolution, scope_error = (
                _resolve_membership_for_mcp(conn, workstream_id)
            )
            if scope_error is not None:
                return scope_error
        new_vec = None  # raw vector — kept for the claim-change guard
        new_blob = None
        if title is not None or body is not None:
            t = title if title is not None else node["title"]
            b = body if body is not None else node["body"]
            new_vec = embeddings.embed(f"{t}\n\n{b}")
            new_blob = embeddings.to_blob(new_vec)
        # Capture pre-update scalars the guard needs BEFORE db.update_node
        # mutates the row (capture-before-mutation, id=1121).
        old_body = node["body"]
        old_kind = node["kind"]
        old_status = node["status"]
        old_embedding = node["embedding"]
        if workstream_id is not None:
            db.set_node_workstream_nc(conn, [node_id], resolved_workstream_id)
        db.update_node(conn, node_id, title=title, body=body, status=status, embedding=new_blob)
        db.bump_focus_for_nodes(conn, [node_id])
        effective_body = body if body is not None else old_body
        # Pass the node kind so the shared helper applies the same kind-scope
        # the insert path does — fixes the second over-fire site (id=1158
        # noted this branch duplicated the computation; it now routes through
        # the one kind-filtered helper). (id=1194 §1.)
        orphan_hint = heal.compute_orphan_hint(
            conn, node_id, effective_body, old_kind,
        )
        # Claim-change guard (spec id=1175, enforces policy id=1174): only when
        # the body actually changed. record_claim_change emits one structural
        # claim_change.log row and returns the nudge (or None). Uses pre-update
        # status/kind/embedding — a staging→canonical promotion in the same call
        # reads the OLD status (staging) and is exempt, per the v1 spec scope.
        claim_change_hint = None
        if body is not None and body != old_body:
            claim_change_hint = verify.record_claim_change(
                node_id=node_id, kind=old_kind, status=old_status,
                old_embedding_blob=old_embedding, old_body=old_body,
                new_body=body, new_vec=new_vec,
                project_path=_project_cwd(), session_id=_project_session_id(),
            )
        _record_write_events(conn, [node_id])
        result = {
            "id": node_id, "ok": True,
            "orphan_hint": orphan_hint,
            "claim_change_hint": claim_change_hint,
            "kb_activity": _kb_activity(
                action="write",
                tool="latch_update",
                summary=(
                    f"Updated KB {old_kind} node id={node_id}: "
                    f"{title or node['title']}."
                ),
                nodes=[_activity_node({
                    "id": node_id,
                    "kind": old_kind,
                    "title": title or node["title"],
                    "status": status or old_status,
                })],
                hints=_activity_hints({
                    "orphan_hint": orphan_hint,
                    "claim_change_hint": claim_change_hint,
                }),
            ),
        }
        if workstream_resolution is not None:
            result["workstream_resolution"] = workstream_resolution
        return result

    return _run_public_kb_mutation(_update)


_APPENDABLE_KINDS = {"workstream", "progress"}


def _kb_append_impl(conn, node_id: int, text: str, *, reembed: bool, date: str) -> dict:
    """Core of latch_append, decoupled from the MCP/lock/clock plumbing so it is
    unit-testable against a temp-DB connection. Appends `text` to the node's
    rolling region (top of body, newest-first, capped) without re-sending or
    (by default) re-embedding the body. Returns orphan_hint via the canonical
    kind-scoped helper (id=1194) — empty for living-summary kinds by design."""
    if not text or not text.strip():
        return {"ok": False, "error": "text is empty"}
    node = db.get_node(conn, node_id)
    if not node:
        return {"ok": False, "error": f"node {node_id} not found"}
    if node["kind"] not in _APPENDABLE_KINDS:
        return {
            "ok": False,
            "error": (
                f"latch_append is for living-summary kinds {sorted(_APPENDABLE_KINDS)}; "
                f"node {node_id} is '{node['kind']}'. Use latch_update for a non-claim "
                f"state edit, or latch_correct_plan/apply for a claim change (id=1174)."
            ),
        }
    new_body = rolling.apply(node["body"] or "", text, date=date)
    new_blob = None
    if reembed:
        new_blob = embeddings.to_blob(embeddings.embed(f"{node['title']}\n\n{new_body}"))
    db.update_node(conn, node_id, body=new_body, embedding=new_blob)
    db.bump_focus_for_nodes(conn, [node_id])
    _record_write_events(conn, [node_id])
    orphan_hint = heal.compute_orphan_hint(conn, node_id, new_body, node["kind"])
    return {
        "id": node_id,
        "ok": True,
        "orphan_hint": orphan_hint,
        "kb_activity": _kb_activity(
            action="write",
            tool="latch_append",
            summary=(
                f"Appended latest KB state to {node['kind']} node "
                f"id={node_id}: {node['title']}."
            ),
            nodes=[_activity_node(node)],
            hints=_activity_hints({"orphan_hint": orphan_hint}),
        ),
    }


@mcp.tool(name="latch_append")
@mcp.tool(name="kb_append")
def kb_append(node_id: int, text: str, reembed: bool = False) -> dict:
    """Append `text` as the newest entry in a node's rolling "Latest" region
    (top of body, newest-first, capped at 3) WITHOUT re-sending or re-embedding
    the existing body. The cheap way to freshen a workstream/plan "where are we"
    surface — send only the delta line, not the whole body.

    Use this instead of latch_update to satisfy the plan-freshness mandate (id=824):
    a full latch_update of a large workstream body costs ~5-10K tokens (fetch +
    resend + regenerate, measured in id=1509); latch_append costs only the delta.

    Scoped to living-summary kinds (workstream/progress). For claim-bearing
    fact/decision nodes use latch_update (non-claim edits) or
    latch_correct_plan/apply (claim changes, id=1174) — append is for STATE,
    not claim evolution.

    `reembed=False` by default: the base-body embedding is the node's semantic
    anchor and a "Latest:" touch shouldn't churn it (id=1320). Pass reembed=True
    only when the appended content should shift the node's retrieval.

    Returns `orphan_hint` synchronously (A1 contract, id=825) via the canonical
    kind-scoped helper; it is empty for living-summary kinds by design (id=1194),
    so the filter is NOT bypassed.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_append")
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _append(conn) -> dict:
        requested = db.get_node(conn, node_id)
        if requested is None:
            return {"ok": False, "error": f"node {node_id} not found"}
        workstream_resolution = None
        append_id = int(node_id)
        if requested["kind"] == "workstream":
            append_id, workstream_resolution, scope_error = (
                _resolve_membership_for_mcp(conn, node_id)
            )
            if scope_error is not None:
                return scope_error
            assert append_id is not None  # guaranteed by successful resolution
        result = _kb_append_impl(
            conn, append_id, text, reembed=reembed, date=date,
        )
        if workstream_resolution is not None:
            result["workstream_resolution"] = workstream_resolution
        return result

    return _run_public_kb_mutation(_append)


@mcp.tool(name="latch_link")
@mcp.tool(name="kb_link")
def kb_link(src: int, dst: int, relation: str) -> dict:
    """Add a typed edge between two nodes (idempotent). Re-linking a previously
    tombstoned edge re-activates it in place — the edge row is audit-stable.

    Also returns `ship_edge_hint`: non-empty when `src` is a `progress` node
    and this edge is a `related_to` to a spec node (idea/open_question/
    decision) — a likely mis-typed ship edge that should be implements/
    advances/depends_on so plan-freshness can track the spec body. The agent
    should re-link with the dependency relation (latch_unlink the related_to,
    latch_link the right one). Empty list otherwise. (id=1194 §4.)
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_link")
    canonical_relation = db.canonicalize_relation(str(relation))
    if canonical_relation in _LIFECYCLE_OWNED_EDGE_RELATIONS:
        return {
            "ok": False,
            "error": (
                f"relation '{canonical_relation}' is machine-owned by the "
                "workstream lifecycle service"
            ),
        }

    def _link(conn) -> dict:
        priority_error = _priority_edge_error(conn, [src, dst])
        if priority_error is not None:
            return priority_error
        db.add_edge(
            conn, src=src, dst=dst, relation=relation,
            project_path=_project_cwd(), session_id=_project_session_id(),
        )
        src_node = db.get_node(conn, src)
        ship_edge_hint = (
            heal.compute_ship_edge_hint(conn, src, src_node["kind"])
            if src_node else []
        )
        return {"ok": True, "ship_edge_hint": ship_edge_hint}

    return _run_public_kb_mutation(_link)


@mcp.tool(name="latch_unlink")
@mcp.tool(name="kb_unlink")
def kb_unlink(src: int, dst: int, relation: str) -> dict:
    """Tombstone an edge — soft-delete that mirrors the node-stale idiom.

    The edge row persists for audit, but every edge-walking read site
    (`latch_get` neighbors, reconciliation_banner, gate traversal,
    plan_freshness_hint, UserPromptSubmit graph hop) filters it out. Use when
    a body refactor invalidates an existing edge so the body and edge
    structure stay in sync.

    Idempotent — calling on a missing or already-tombstoned edge is a no-op.
    Relation is canonicalized before lookup (same map `latch_link` uses), so
    `latch_unlink(a, b, "relates_to")` hits the canonical `related_to` row.
    Re-linking the same triple via `latch_link` reactivates the tombstoned row.

    Returns `{"ok": True, "tombstoned": 0|1}` — `tombstoned=1` if an active
    edge was flipped, `0` if no-op.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_unlink")
    canonical_relation = db.canonicalize_relation(str(relation))
    if canonical_relation in _LIFECYCLE_OWNED_EDGE_RELATIONS:
        return {
            "ok": False,
            "error": (
                f"relation '{canonical_relation}' is machine-owned by the "
                "workstream lifecycle service"
            ),
        }

    def _unlink(conn) -> dict:
        n = db.tombstone_edge(conn, src=src, dst=dst, relation=relation)
        return {"ok": True, "tombstoned": n}

    return _run_public_kb_mutation(_unlink)


def _gate_status(verdict: dict) -> str:
    """One-line, human-facing health of a gate call so a None verdict is
    surfaced rather than silently read as PROCEED (KB id=1415). 'OK' when a
    recommendation came back; otherwise it names why none did and what to do."""
    if verdict.get("recommendation") is not None:
        return "OK"
    if verdict.get("reason") == "unlatched":
        return (
            "SKIPPED - Latch is currently UNLATCHED. Run /unlatch to re-latch. "
            "If LATCH_UNLATCHED is set, unset it too."
        )
    if verdict.get("timed_out"):
        return ("DEGRADED — classifier timed out; no gate judgment this call. "
                "Proceed on KB-first context and tell the user the gate was "
                "skipped. If this recurs, the gate prompt may be oversized for "
                "this KB (id=1415).")
    if verdict.get("skipped"):
        return ("SKIPPED — gate disabled, daily budget cap, or in-compact; no "
                "gate judgment. Proceed on KB-first context.")
    return (f"DEGRADED — no verdict ({verdict.get('error') or 'unknown error'}); "
            "proceed on KB-first context and note the gate was unavailable.")


@mcp.tool(name="latch_gate")
@mcp.tool(name="kb_gate")
def kb_gate(request: str, max_chains: int = 5, verbose: bool = False) -> dict:
    """Gate judgment on a coding/build/implement/refactor request.

    Hybrid-searches the KB (including stale nodes), seeds traversal from
    workstreams currently in focus, walks 1–2 hops over the canonical
    relations + related_to, then asks an LLM classifier to recommend one of
    {PROCEED | MODIFY | DO_NOT_PROCEED | NEEDS_HUMAN_JUDGMENT} with cited
    node ids and a recommended better-next-action.

    **Invoke this autonomously** on user prompts that look like coding /
    build / implement / refactor / add / extend requests, immediately BEFORE
    the first material implementation mutation. Gate once per materially
    scoped request, not unchanged substeps or verification. If planning-only
    work later becomes implementation, gate then. This is the next layer on
    top of the existing "KB-first every prompt" rule — not a replacement. If
    the verdict is PROCEED, continue normally. If it is MODIFY /
    DO_NOT_PROCEED, surface the recommendation and cited nodes to the user
    before acting (side-note v1 — the agent does not auto-redirect).

    Skip this tool for: explanation requests, status questions, search
    queries, debugging an error in code you already wrote, or any
    non-implementation prompt.

    Returns (compact form, default — fits well under MCP tool-result cap):
      {
        "request": <str>,
        "gate_status": <str>,                 # "OK", else "SKIPPED/DEGRADED — ..." when verdict is None — surface it; never read None as PROCEED (id=1415)
        "verdict": {                          # see parse_classifier_output
          "recommendation": "PROCEED" | "MODIFY" | "DO_NOT_PROCEED" | "NEEDS_HUMAN_JUDGMENT" | None,
          "summary": <str>, "decision_chain": [<id>...],
          "abandoned_paths": [...], "active_constraints": [...],
          "current_direction": [...], "risk_if_proceed": <str>,
          "better_next_action": <str>, "evidence_nodes": [<id>...],
          "load_bearing_claims": [{"claim","evidence_type","evidence_ref","gap_type"}...],
          "uncovered_claims": [{"claim","gap_type","suggested_remedy"}...],  # evidence_type=none gaps — resolve each (hop_deeper/code_trace/flag_to_user) before acting; id=1220/id=1253
          "error": <str | None>,
          "skipped": <bool>,                  # only present when skipped
          "adversary": {                      # PROCEED-only skeptic; present only when CLAUDE_KB_ADVERSARY=1 (id=1343)
            "objection": <str>,               # cite-or-PROCEED: empty unless a counter node is cited
            "counter_node_id": <id | None>,   # the node that refutes/re-scopes the plan, or None
            "verdict_delta": "none" | "MODIFY" | "DO_NOT_PROCEED",  # advisory — verdict is NOT auto-flipped (side-note v1)
            "design_decision_questions": [{"question","stake","options_hint":[...]}, ...]  # genuine user-call forks
          }
        },
        "findings": {                         # user-visible Latch gate block to display before implementation
          "label": "Latch gate findings",
          "must_display_to_user": true,
          "source": "latch_gate",
          "recommendation": <same as verdict.recommendation>,
          "summary": <same as verdict.summary>,
          "evidence_nodes": [{"id","kind","title","status"}, ...],
          "receipt": {
            "summary": "Latch ran the gate ...; cited node status carries current authority.",
            "source": "latch_gate",
            "used": {"decision_chain": <int>, "evidence_nodes": <int>, ...},
            "authority": <how to read current authority/status + rationale basis>
          },
          "why_it_matters": <same as receipt.summary>,
          "better_next_action": <same as verdict.better_next_action>,
          "uncovered_claims": [...]
        },
        "evidence": [{"id","kind","title","status","workstream_id"}, ...],  # cited only
        "chain_summary": {                    # drill-in pointers, no bodies
          "seed_count":   <int>,
          "seed_ids":     [<id>, ...],
          "reachable_ids": [<id>, ...]        # all node ids in the assembled chain
        }
      }

    Pass `verbose=True` to also include the full `chains` field with body
    excerpts (for debugging / direct chain inspection). The default elides
    `chains` because traversal at hop=2 over many seeds can produce 60–90k
    char payloads that exceed the MCP tool-result cap. The agent should
    drill into specific nodes via `latch_get(<id>)` instead.

    Budget-gated: counts toward the daily LLM cap that the compactor and
    nightly heal share. Verdict is None with `skipped=True` when the cap
    is hit, the kill switch is on, or the call is inside a compactor's
    own session (reentrancy guard).
    """
    if paths.is_unlatched_mode():
        verdict = gate.unlatched_verdict()
        gate_status = _gate_status(verdict)
        return {
            "request": request,
            "gate_status": gate_status,
            "verdict": verdict,
            "findings": gate.format_gate_findings(
                verdict, [], gate_status=gate_status,
            ),
            "evidence": [],
            "chain_summary": {
                "seed_count": 0,
                "seed_ids": [],
                "reachable_ids": [],
            },
        }
    with _conn() as conn:
        full = gate.run_gate(
            conn, request, project_path=_project_cwd(), max_chains=max_chains,
            session_id=_project_session_id(),
        )
    if verbose:
        return full
    chains_assembly = full.get("chains") or {}
    seeds = chains_assembly.get("seeds") or []
    verdict = full.get("verdict") or {}
    gate_status = _gate_status(verdict)
    return {
        "request": full.get("request", request),
        "gate_status": gate_status,
        "verdict": verdict,
        "findings": gate.format_gate_findings(
            verdict, full.get("evidence") or [], gate_status=gate_status,
        ),
        "evidence": full.get("evidence") or [],
        "chain_summary": {
            "seed_count": len(seeds),
            "seed_ids": [s["id"] for s in seeds],
            "reachable_ids": chains_assembly.get("evidence_node_ids") or [],
        },
    }


@mcp.tool(name="latch_capture_decision")
@mcp.tool(name="kb_capture_decision")
def kb_capture_decision(
    title: str,
    body: str,
    gate_request: str,
    human_action: str,
    confidence_tier: str = "explicit_user",
    provenance: str = "inline_capture",
    cited_node_ids: list[int] | None = None,
    was_confirmed: bool = True,
    status: str = "staging",
    links: list[dict] | None = None,
    workstream_id: int | None = None,
    session_id: str | None = None,
) -> dict:
    """Capture a decision the user made in response to a latch_gate verdict.

    The Type-1 (explicit/confirmed) leg of the decision-capture pipeline
    (KB id=1279 / id=1350 / scope id=1784). Call this AFTER the user acts on a
    gate verdict — approve / modify / reject / override — so the ratified
    judgment becomes, in one atomic step, BOTH (a) a `kind="decision"` KB node
    (the content the user ratified) AND (b) a structural `decision.log` RL row
    (content-free: ids, closed-set labels, a join hash).

    This honours the human-confirmed-mutation rule (id=1151): materialise only
    what the user actually decided — never fabricate decision wording they did
    not approve. A bare PROCEED that surfaced nothing new needs no node; for
    that case emit signal only by calling the lower-level capture path, not this
    tool.

    Args:
        title / body: the decision node's content (human-ratified).
        gate_request: the original gate request text. Hashed (never stored) into
            the `query_hash` join key so this decision.log row joins back to the
            gate.log / adversary.log rows for the same prompt. Uses gate's own
            hash so the keys match exactly.
        human_action: a member of `capture_streams.HUMAN_ACTIONS`
            (approve | modify | reject | override) — what the user did with the
            verdict. The gold RL label; "override" (proceeding against the gate,
            or rejecting a PROCEED) is the highest-signal row.
        confidence_tier: `capture_streams.CONFIDENCE_TIERS` member —
            `explicit_user` when the user stated the decision, `agent_confirmed`
            when the agent composed it and the user confirmed.
        provenance: `capture_streams.DECISION_PROVENANCES` member — the trigger
            (`adversary_fork` / `gate_question` / `inline_capture`).
        cited_node_ids: gate evidence node ids to link (`related_to`) from the
            new decision — the KB context the decision was made against.
        was_confirmed: whether the user confirmed the node's wording (Type-1).
        status / links / workstream_id / session_id: as `latch_insert`.

    Returns the `latch_insert`-shaped result plus `decision_logged` (bool) and the
    echoed `human_action`. Validates the three closed-set labels first and
    returns `{ok: False, error: ...}` on a bad label WITHOUT writing anything.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_capture_decision")
    # Validate closed-set labels up front — a bad label must not write a half row.
    if human_action not in capture_streams.HUMAN_ACTIONS:
        return {"ok": False,
                "error": f"human_action must be one of {capture_streams.HUMAN_ACTIONS}"}
    if confidence_tier not in capture_streams.CONFIDENCE_TIERS:
        return {"ok": False,
                "error": f"confidence_tier must be one of {capture_streams.CONFIDENCE_TIERS}"}
    if provenance not in capture_streams.DECISION_PROVENANCES:
        return {"ok": False,
                "error": f"provenance must be one of {capture_streams.DECISION_PROVENANCES}"}

    # Cited gate-evidence nodes become `related_to` edges from the decision, so
    # the node carries the KB context it was made against. Extra links append.
    edges: list[dict] = [
        {"dst": int(n), "relation": "related_to"} for n in (cited_node_ids or [])
    ]
    if links:
        edges.extend(links)

    sid = session_id or _project_session_id()

    def _capture(conn) -> tuple[dict, dict | None] | dict:
        priority_error = _priority_edge_error(
            conn, _link_destination_ids(edges),
        )
        if priority_error is not None:
            return priority_error
        resolved_workstream_id, workstream_resolution, scope_error = (
            _resolve_membership_for_mcp(conn, workstream_id)
        )
        if scope_error is not None:
            return scope_error
        result = heal.insert_with_heal(
            conn, kind="decision", title=title, body=body, status=status,
            session_id=sid, links=edges or None, use_llm=True,
            workstream_id=resolved_workstream_id, project_path=_project_cwd(),
        )
        new_id = result.get("id")
        if new_id is not None:
            db.bump_focus_for_nodes(conn, [new_id])
            _record_write_events(conn, [new_id])
        return result, workstream_resolution

    locked_result = _run_public_kb_mutation(_capture)
    if isinstance(locked_result, dict):
        # Either membership resolution failed or the shared writer lock timed
        # out.  Both fail before the post-commit decision event is emitted.
        return locked_result
    result, workstream_resolution = locked_result
    new_id = result.get("id")

    # Emit the structural RL row AFTER the node exists (point-in-time, id=1108).
    # decision.log is a file write, not a DB write — do it outside the conn.
    # Best-effort: emit_decision_event never raises, so logging can't break the
    # capture that already succeeded.
    decision_logged = False
    if new_id is not None:
        capture_streams.emit_decision_event(
            node_ids=[new_id],
            confidence_tier=confidence_tier,
            provenance=provenance,
            was_confirmed=was_confirmed,
            human_action=human_action,
            query_hash=gate._query_hash(gate_request),
            project_path=_project_cwd(),
            session_id=sid,
        )
        decision_logged = True

    result["decision_logged"] = decision_logged
    result["human_action"] = human_action
    if workstream_resolution is not None:
        result["workstream_resolution"] = workstream_resolution
    if new_id is not None:
        result["kb_activity"] = _kb_activity(
            action="write",
            tool="latch_capture_decision",
            summary=f"Captured user-ratified KB decision id={new_id}: {title}.",
            nodes=[_activity_node({
                "id": new_id, "kind": "decision", "title": title, "status": status,
            })],
            hints=_activity_hints(result),
        )
    return result


@mcp.tool(name="latch_verify")
@mcp.tool(name="kb_verify")
def kb_verify(node_id: int) -> dict:
    """Deterministic, no-LLM authority check on a single KB node.

    The lightweight tier of the two-tier validation model (latch_gate is the
    heavyweight tier). Returns one of:

      OK         — node is current and unconstrained; safe to cite.
      RECONCILED — still canonical/staging but has an outbound `reconciled_by`
                   edge: true in its own scope, but a newer node constrains
                   its framing. Fetch both before acting (`reconciled_by` ids
                   returned).
      STALE      — `status='stale'` or superseded by an active incoming
                   supersedes/replaces edge (`superseded_by` ids returned).
                   Do not cite as current truth.
      NOT_FOUND  — no such node.

    Sub-millisecond. Use to confirm a node id is still authoritative before
    relying on it — e.g. when a cited node looks suspect, or before a
    correction.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_verify")
    with _conn() as conn:
        return verify.verify(conn, node_id)


@mcp.tool(name="latch_correct_plan")
@mcp.tool(name="kb_correct_plan")
def kb_correct_plan(bad_node_id: int, max_hops: int = 2) -> dict:
    """Phase 1 of a structured KB correction — READ-ONLY, no mutation.

    Call this when the user has told you a KB node is wrong / stale /
    hallucinated (or you've confirmed it contradicts reality). Returns:

      * `snapshot` — the bad node's pre-mutation state (status, ref_count, age);
      * `blast_radius` — every node reachable over canonical relations +
        related_to (the side-effect set), each tagged via_relation/direction/hop;
      * `framing_carrier_candidates` — inbound canonical-relation neighbors,
        the prime candidates for a `reconciled_by` edge to the correction;
      * `recommended_mode` + `recommendation_note` — supersede-vs-reconcile
        guidance (you make the final call).

    Then surface the plan to the user, decide `mode` and the `reconcile_ids`
    subset, and call `latch_correct_apply` ONLY after the user confirms. Mutation
    is never auto-fired — a misclassification must not cascade stale-marks.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_correct_plan")
    with _conn() as conn:
        return verify.correct_plan(conn, bad_node_id, max_hops=max_hops)


@mcp.tool(name="latch_correct_apply")
@mcp.tool(name="kb_correct_apply")
def kb_correct_apply(
    bad_node_id: int,
    mode: str,
    title: str,
    body: str,
    kind: str = "decision",
    corrected_status: str = "canonical",
    reconcile_ids: list[int] | None = None,
    workstream_id: int | None = None,
    links: list[dict] | None = None,
    trigger: str | None = None,
    prompt_hash: str | None = None,
) -> dict:
    """Phase 2 — apply a HUMAN-CONFIRMED correction atomically.

    Only call after `latch_correct_plan` and explicit user confirmation.

    `mode`:
      * "supersede" — the bad node is wholly wrong/hallucinated. Inserts the
        corrected node, wires `corrected --supersedes--> bad`, then marks the
        bad node `stale` (body left untouched — staling preserves the
        decision-change history; do NOT rewrite the old body).
      * "reconcile" — the bad node is true in its own scope but its framing
        was over-applied. Links `bad --reconciled_by--> corrected`; the bad
        node stays canonical and surfaces the correction via its banner.

    `reconcile_ids` — the judged subset of blast-radius framing-carriers that
    actually carried the bad framing forward; each gets a `reconciled_by` edge
    to the corrected node. Pass only the real carriers, not every neighbor.

    `trigger` — optional structural label for the RL log: "user_assertion"
    or "agent_self_contradiction". `prompt_hash` — optional sha1[:12] of the
    triggering prompt (hashed, never raw text).

    Emits one structural `correction.log` row (a human-labeled "the KB was
    wrong" RL reward signal) and returns `corrected_node_id` + an
    `orphan_hint` for the corrected body. Honor the orphan_hint as with
    latch_insert / latch_update.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_correct_apply")

    def _correct(conn) -> dict:
        resolved_workstream_id, workstream_resolution, scope_error = (
            _resolve_membership_for_mcp(conn, workstream_id)
        )
        if scope_error is not None:
            return scope_error
        result = verify.correct_apply(
            conn, bad_node_id,
            mode=mode, title=title, body=body, kind=kind,
            corrected_status=corrected_status, reconcile_ids=reconcile_ids,
            workstream_id=resolved_workstream_id, links=links,
            trigger=trigger, prompt_hash=prompt_hash,
            session_id=_project_session_id(), project_path=_project_cwd(),
        )
        cid = result.get("corrected_node_id")
        if cid is not None:
            db.bump_focus_for_nodes(conn, [cid])
            _record_write_events(conn, [cid])
        if workstream_resolution is not None:
            result["workstream_resolution"] = workstream_resolution
        return result

    return _run_public_kb_mutation(_correct)


@mcp.tool(name="latch_priority_add")
@mcp.tool(name="kb_priority_add")
def kb_priority_add(
    text: str,
    note: str | None = None,
    rank: int | None = None,
    workstream_id: int | None = None,
) -> dict:
    """Add a standing priority.

    Overall priorities (workstream_id omitted) are 'top of mind' directives
    latch weighs on EVERY latch_gate, regardless of whether a given prompt is
    about them (e.g. security review, cross-platform installability).
    Workstream priorities (workstream_id=<workstream node id>) are additive
    guidance weighed only when the current gate request resolves to that
    workstream. Both remain visible through `latch_priority_list`.

    `text` is the directive itself (keep it short — it is shown in full in the
    gate prompt and priority list). `note` is optional extra rationale stored
    in the body.

    Ranking: omit `rank` (the common case) and the priority **floats** — it
    stacks onto the top of the unlocked region (newest-first) and never displaces
    a priority the user has explicitly locked. Pass `rank` (1 = top) ONLY when
    you can judge where this directive sits in importance relative to the current
    active set — that **locks** it at that absolute slot. If the slot is already
    locked by another priority, no write happens and a `{conflict: ...}` is
    returned: surface it and ask the user how to reorder rather than guessing.

    Capped at MAX_ACTIVE active priorities per scope (default 5 overall and 5
    per workstream, override via CLAUDE_KB_PRIORITY_CAP): returns an error with
    the current set if that scope's cap is reached — retire one first via
    latch_priority_retire. Priorities are project/workstream scoped (not per-user)
    and never participate in retrieval or traversal.
    """
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_priority_add")
    return _run_public_priority_mutation(
        lambda conn: priorities.add_priority(
            conn, text, note=note, rank=rank, workstream_id=workstream_id,
        ),
    )


@mcp.tool(name="latch_priority_list")
@mcp.tool(name="kb_priority_list")
def kb_priority_list(
    include_retired: bool = False, workstream_id: int | None = None,
) -> dict | list[dict]:
    """List active priorities in effective P1..PN order for one scope.

    Omit workstream_id to list overall priorities. Pass a workstream node id to
    list priorities scoped to that workstream. Locked priorities keep their
    slot; floating ones are newest-first. Each row carries `rank` (the locked
    slot, null when floating), `locked`, `scope`, and `workstream_id`. Pass
    include_retired=True for the audit/graveyard view — the active set followed
    by retired priorities, most-recently-graveyarded first, each carrying its
    `retired_at` date."""
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_priority_list")
    with _conn() as conn:
        rows = priorities.list_priorities(
            conn, include_retired=include_retired, workstream_id=workstream_id,
        )
    return [
        {
            "id": r["id"],
            "text": r["title"],
            "status": r["status"],
            "scope": r.get("scope"),
            "workstream_id": r.get("workstream_id"),
            "workstream_title": r.get("workstream_title"),
            "rank": r.get("rank"),
            "locked": r.get("locked", False),
            "position": r.get("effective_rank"),
            "retired_at": r.get("retired_at"),
            "created_by": r.get("created_by"),
            "created_at": r.get("created_at"),
        }
        for r in rows
    ]


@mcp.tool(name="latch_priority_reorder")
@mcp.tool(name="kb_priority_reorder")
def kb_priority_reorder(node_id: int, new_rank: int | None = None) -> dict:
    """Re-rank an active priority. Pass `new_rank` (1 = top) to **lock** it at
    that absolute slot; pass null to **unlock** it back to floating (recency-
    ordered). Ranking is within the priority's own scope: overall priorities
    only collide with overall priorities, and a workstream priority only
    collides with priorities in the same workstream. Locking onto a slot held
    by a floating priority is fine (it reflows); locking onto a slot held by
    another locked priority returns a `{conflict: ...}` — surface it and ask the
    user which one should move."""
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_priority_reorder")
    return _run_public_priority_mutation(
        lambda conn: priorities.reorder_priority(conn, node_id, new_rank),
    )


@mcp.tool(name="latch_priority_retire")
@mcp.tool(name="kb_priority_retire")
def kb_priority_retire(node_id: int) -> dict:
    """Retire (soft-delete) a priority so it stops appearing in gates and active
    priority listings, moving it to the graveyard with the date it was retired
    (`retired_at`). Reversible — the node persists as 'stale' for audit; never
    hard-deleted. Remaining active priorities renumber to close the gap."""
    if paths.is_unlatched_mode():
        return _unlatched_response("latch_priority_retire")
    return _run_public_priority_mutation(
        lambda conn: priorities.retire_priority(conn, node_id),
    )


# The verification-profile engine (src/profiles.py — per-user gate presets
# from trust-and-go up to mission-control) remains wired into the gate and the
# hooks.  Its experimental kb_/latch_profile_* MCP verbs were removed from the
# tool surface: profile bindings are a deliberate per-user configuration
# mutation, not something an agent should reach for mid-session.
#
# The standalone latch_/kb_embed MCP verb was also removed from the surface:
# embedding is automatic on every write and through the TCP embed listener, so
# no agent ever needs to call it directly.  The listener and write-path embed
# calls are unaffected.


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == "darwin" else value * 1024
    except (ImportError, OSError, ValueError):
        return None


@mcp.tool(name="latch_runtime_status")
@mcp.tool(name="kb_runtime_status")
def kb_runtime_status() -> dict:
    """Diagnose MCP/model ownership without scanning private host state.

    Reports the shared owner, connection attribution source, activity/lease
    counts, model-load state, and approximate process footprint.  Tokens and
    full connection inventories are intentionally excluded.
    """
    daemon = mcp_runtime.daemon_snapshot()
    connection = mcp_runtime.connection_snapshot()
    embed_owner: dict[str, Any] | None = None
    try:
        payload = json.loads(mcp_broker.embed_discovery_path().read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            embed_owner = {
                key: payload.get(key)
                for key in ("pid", "host", "port", "started_at")
                if payload.get(key) is not None
            }
    except (OSError, ValueError):
        pass
    context_state = mcp_runtime.current_connection()
    policy = (
        {
            "cap": context_state.proxy_cap,
            "retire_idle_s": context_state.proxy_retire_idle_s,
            "heartbeat_s": context_state.proxy_heartbeat_s,
            "stale_s": context_state.proxy_stale_s,
        }
        if context_state is not None
        else mcp_broker.proxy_policy()
    )
    lease_state = mcp_broker.proxy_lease_state(policy=policy)
    proxy_inventory = list(lease_state["live"])
    legacy_inventory = list(lease_state.get("legacy_incompatible") or [])
    unassociated_inventory = list(
        lease_state.get("unassociated_capable") or []
    )
    other_owner_inventory = list(lease_state.get("other_live_owner") or [])
    observed_inventory = list(lease_state.get("observed_live") or [])
    dropped_retrieval_events = None
    stale_tree_signals = None
    try:
        with _conn() as conn:
            dropped_retrieval_events = db.retrieval_events_dropped(conn)
            row = conn.execute(
                "SELECT value FROM latch_meta WHERE key = 'stale_tree_signal'"
            ).fetchone()
            stale_tree_signals = int(row["value"]) if row is not None else 0
    except Exception:
        pass
    return {
        "mode": "shared_daemon" if daemon is not None else "legacy_stdio",
        "process_pid": os.getpid(),
        "project_cwd": _project_cwd(),
        "session_id": _project_session_id(),
        "connection": connection,
        "daemon": daemon,
        "proxy_pool": {
            **policy,
            "live_leases": len(proxy_inventory),
            "legacy_incompatible_leases": len(legacy_inventory),
            "unassociated_capable_leases": len(unassociated_inventory),
            "other_live_owner_leases": len(other_owner_inventory),
            "other_live_owner_count": len(
                lease_state.get("other_owner_runtime_keys") or []
            ),
            "observed_live_leases": len(observed_inventory),
            "stale_leases": int(lease_state.get("stale_count") or 0),
            "max_stale_lease_age_s": float(
                lease_state.get("max_stale_age_s") or 0.0
            ),
            "scope": "owner_runtime_key",
            "owner_runtime_key": lease_state.get("owner_runtime_key"),
            "alias_runtime_key_count": len(
                lease_state.get("alias_runtime_keys") or []
            ),
            "unassociated_runtime_key_count": len(
                lease_state.get("unassociated_runtime_keys") or []
            ),
            "bounded": bool(int(policy["cap"])),
        },
        "embedding": {
            "model_loaded": embeddings.is_loaded(),
            "heavy_model_owner_count": 1 if embeddings.is_loaded() else 0,
            "listener": embed_owner,
        },
        "process_peak_rss_bytes": _peak_rss_bytes(),
        "lifecycle": mcp_broker.lifecycle_summary(
            hours=24, lease_state=lease_state, policy=policy
        ),
        "retrieval_events": {
            "dropped": dropped_retrieval_events,
            "retention_days": 90,
        },
        "workstream_lifecycle": {
            "stale_tree_signal": stale_tree_signals,
            "receipts_live": lifecycle_receipts.RECEIPTS_CHANNEL_LIVE,
        },
        "maintenance": paths.maintenance_runner_status(_project_cwd()),
        "recovery": (
            "Idle owners are reclaimed; the stdio proxy reconnects and replays MCP initialization. "
            "In-flight calls are never automatically replayed."
            if daemon is not None
            else "Legacy mode owns one model per stdio process; restart after installing shared runtime."
        ),
    }


def initialize_runtime(project_cwd: str, *, start_embed_listener: bool) -> None:
    """Initialize heavyweight process-owned services exactly once.

    The shared daemon calls this before serving readiness probes. Legacy fallback
    calls the same path, preserving availability when broker startup fails.
    """
    global _RUNTIME_INITIALIZED
    if paths.is_unlatched_mode() or _RUNTIME_INITIALIZED:
        return
    with _RUNTIME_INIT_LOCK:
        if _RUNTIME_INITIALIZED:
            return
        if start_embed_listener:
            _start_embed_listener(project_cwd)
        try:
            embeddings.embed("latch embed pre-warm")
        except Exception as e:
            sys.stderr.write(f"[latch] embed pre-warm failed: {e}\n")
        _RUNTIME_INITIALIZED = True


def prune_hidden_surface_tools(server: FastMCP) -> list[str]:
    """Apply the trimmed-surface hide policy to the legacy fallback registry.

    Runs ONLY on the legacy one-process-per-session fallback, where this
    process serves exactly one install and its env is authoritative.  It shares
    the exact hide policy the stdio proxy uses (``mcp_proxy.is_hidden_from_listing``)
    so the legacy advertised surface matches the proxy-filtered surface: kb_*
    aliases AND off-surface latch_* diagnostics such as ``latch_runtime_status``.
    The shared daemon must never call this: its registry serves every install on
    the machine, so per-install trimming happens in ``mcp_proxy`` instead.

    Returns the tool names removed.
    """
    from mcp_proxy import is_hidden_from_listing

    removed: list[str] = []
    for tool in list(server._tool_manager.list_tools()):
        if is_hidden_from_listing(tool.name):
            server.remove_tool(tool.name)
            removed.append(tool.name)
    return removed


if __name__ == "__main__":
    initialize_runtime(PROJECT_CWD, start_embed_listener=True)
    selfheal.maybe_trigger(PROJECT_CWD)
    from mcp_proxy import trimmed_tool_surface

    if trimmed_tool_surface():
        prune_hidden_surface_tools(mcp)
    mcp.run()
