"""Recover session attribution for gate calls from Codex's own transcripts.

Codex-hosted gate calls carry no ``session_id``: the host does not expose a
per-request conversation identity to a reused MCP process, and the SessionStart
marker that would supply one is not written on every build (KB id=3152, id=4018).
Without attribution those rows are permanently unlabelable by the correlator.

The v2.6 recovery path is deliberately nonce-only.  A rollout records the
``latch_gate`` tool result (including ``gate_call_id``) inside the thread whose
id is in the transcript.  Historical hash-recovered rows are a frozen pilot
corpus; production code never re-joins them.  A repeated nonce, incomplete
candidate inventory, malformed candidate region, or mixed project proof fails
closed instead of manufacturing confident attribution.

Read-only. Nothing here writes to the KB or to Codex's files.
"""
from __future__ import annotations

import hashlib
import json
import os
import string
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from latch.hosts import codex_transcript  # noqa: E402
from latch.evals import outcome_measurement  # noqa: E402
from latch.proof import project_proof     # noqa: E402


GATE_TOOL_NAMES = frozenset({"latch_gate", "kb_gate"})
GATE_SERVER_NAMES = frozenset({"latch", "claude-kb"})
_CURRENT_MCP_NOT_GATE = "not_gate"
_CURRENT_MCP_INVALID = "invalid"
_CURRENT_MCP_VALID = "valid"

# A date-bounded filename walk is not a candidate-complete index: Codex keeps
# resumed threads in their original start-day directory, even when a gate call
# is appended days later.  Discovery therefore enumerates the full rollout
# root and proves that the inventory stayed stable while it was scanned.
CANDIDATE_DISCOVERY_VERSION = "codex-rollout-full-v3"
DEFECT_SCOPE_VERSION = "per-measurement-window-v1"
SCOPABLE_DEFECT_KINDS = (
    "session_identity_conflicts",
    "missing_tool_results",
)
GLOBAL_BLOCKER_FIELDS = (
    "traversal_errors",
    "unreadable_files",
    "unstable_files",
    "content_changed_files",
    "malformed_candidate_regions",
    "unidentified_gate_files",
)


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_zoned_ts(value: object) -> datetime | None:
    """Parse an explicitly zoned timestamp for a scoped refusal receipt."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _format_receipt_ts(value: datetime) -> str:
    utc = value.astimezone(timezone.utc)
    if not utc.microsecond:
        return utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    encoded = utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return encoded[:-1].rstrip("0").rstrip(".") + "Z"


def _with_receipt_hash(receipt: dict) -> dict:
    out = dict(receipt)
    out.pop("receipt_sha256", None)
    encoded = json.dumps(
        out,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    out["receipt_sha256"] = hashlib.sha256(encoded).hexdigest()
    return out


def _receipt_hash_matches(receipt: dict) -> bool:
    claimed = receipt.get("receipt_sha256")
    if not isinstance(claimed, str) or len(claimed) != 64:
        return False
    try:
        return _with_receipt_hash(receipt)["receipt_sha256"] == claimed
    except (TypeError, ValueError):
        return False


def _receipt_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _canonical_defect_ledger(defects: list[dict]) -> list[dict[str, object]]:
    """Return the content-free form committed to by a v3 receipt."""
    rows: list[dict[str, object]] = []
    for defect in defects:
        start = defect.get("start")
        end = defect.get("end")
        rows.append({
            "defect_id": defect.get("defect_id"),
            "kind": defect.get("kind"),
            "start": (
                _format_receipt_ts(start)
                if isinstance(start, datetime)
                and start.tzinfo is not None
                and start.utcoffset() is not None
                else None
            ),
            "end": (
                _format_receipt_ts(end)
                if isinstance(end, datetime)
                and end.tzinfo is not None
                and end.utcoffset() is not None
                else None
            ),
        })
    return rows


def _defect_ledger_sha256(defects: list[dict]) -> str:
    encoded = json.dumps(
        _canonical_defect_ledger(defects),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _enumerate_rollout_paths(root: Path) -> tuple[list[Path], int]:
    """Enumerate the entire rollout root and count traversal failures.

    ``Path.rglob`` does not expose every directory-read failure.  ``os.walk``
    with an error callback lets the candidate-completeness receipt fail closed
    instead of silently presenting a partial candidate set as complete.
    """
    errors = 0

    def _onerror(_error: OSError) -> None:
        nonlocal errors
        errors += 1

    found: list[Path] = []
    try:
        for directory, dirnames, filenames in os.walk(
            root, topdown=True, onerror=_onerror, followlinks=False,
        ):
            dirnames.sort()
            for name in sorted(filenames):
                if name.startswith("rollout-") and name.endswith(".jsonl"):
                    found.append(Path(directory) / name)
    except OSError:
        errors += 1
    return found, errors


def _rollout_paths(
    home: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[Path]:
    """Return every rollout path, irrespective of its start-day directory.

    ``start_date`` and ``end_date`` remain accepted for API compatibility but
    deliberately do not restrict discovery.  Resumed threads append later
    calls to the original file; a date-directory filter missed 98/126 observed
    calls and could manufacture false uniqueness from a partial index.
    """
    del start_date, end_date
    root = (home or codex_transcript.codex_home()) / "sessions"
    if not root.is_dir():
        return []
    return _enumerate_rollout_paths(root)[0]


def _parser_config(
    proof_context: project_proof.ProjectProofContext | None,
    target_project_path: str | Path | None,
) -> outcome_measurement.MeasurementConfig:
    """Minimal config for the shared, bytes-only S2 parser.

    Parsing does not use the runtime pin for classification.  The placeholder
    values merely satisfy the parser's typed boundary; the actual metadata is
    retained on each observation and compared by the correlator.
    """
    target = (
        proof_context.prove(target_project_path)
        if proof_context is not None and target_project_path is not None
        else {
            "version": project_proof.PROJECT_PROOF_VERSION,
            "key_epoch": "attribution-unscoped",
            "fingerprint": "0" * 64,
        }
    )
    epoch = proof_context.key_epoch if proof_context is not None else "attribution-unscoped"
    t0 = datetime(2000, 1, 1, tzinfo=timezone.utc)
    return outcome_measurement.MeasurementConfig(
        t0=t0,
        cap=t0 + timedelta(days=21),
        target_project_proof=target,
        key_epoch=epoch,
        pinned_runtime_version="attribution-parser",
        require_fresh_snapshots=False,
    )


def _read_snapshot(path: Path) -> tuple[bytes | None, str | None, bool]:
    """Read one full-file snapshot and detect concurrent replacement/growth."""
    try:
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
    except OSError:
        return None, None, False
    unstable = (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or getattr(before, "st_ino", None) != getattr(after, "st_ino", None)
    )
    return data, hashlib.sha256(data).hexdigest(), unstable


def _observation_metadata(row: outcome_measurement.Observation) -> dict:
    return {
        "nonce": row.nonce,
        "ts": row.ts,
        "session_id": row.session_id,
        "adapter": row.adapter,
        "attestation": row.attestation,
        "measurement_protocol_version": row.measurement_protocol_version,
        "project_proof": row.project_proof,
        "host_scope_project_proof": row.host_scope_project_proof,
        "key_epoch": row.key_epoch,
        "runtime_version": row.runtime_version,
        "verdict": row.verdict,
        "verdict_id_lists": row.verdict_id_lists,
        "skipped": row.skipped,
        "observable": row.observable,
        "evidence_available": row.evidence_available,
        "progress_inserts": row.progress_inserts,
        "inserts": row.inserts,
        "linked_cited_insert": row.linked_cited_insert,
        "cited_edge_activity": row.cited_edge_activity,
        "touches": row.touches,
        "embedded_conflict_reasons": row.embedded_conflict_reasons,
        "legacy_project": row.legacy_project,
        "hash_annotated": row.hash_annotated,
        "pre_nonce": row.pre_nonce,
        "stream_coordinate": (row.file, row.byte_offset),
    }


def _is_supported_mcp_gate_end(record_type: object, payload: dict) -> bool:
    invocation = payload.get("invocation")
    server = invocation.get("server") if isinstance(invocation, dict) else None
    tool = invocation.get("tool") if isinstance(invocation, dict) else None
    return bool(
        record_type == "event_msg"
        and payload.get("type") == "mcp_tool_call_end"
        and isinstance(server, str)
        and isinstance(tool, str)
        and server in GATE_SERVER_NAMES
        and tool in GATE_TOOL_NAMES
    )


def _gate_result_candidates(
    result: object,
) -> tuple[set[str], set[str], bool]:
    """Collect every structural nonce candidate in a known gate result."""
    nonces: set[str] = set()
    signatures: set[str] = set()
    invalid = False

    def _walk(value: object, depth: int = 0) -> None:
        nonlocal invalid
        if depth > 8:
            invalid = True
            return
        if isinstance(value, str):
            encoded = value.strip()
            if "\nOutput:\n" in value:
                encoded = value.rsplit("\nOutput:\n", 1)[1].strip()
            if not encoded:
                return
            try:
                decoded = json.loads(encoded)
            except json.JSONDecodeError:
                if (
                    encoded.startswith(("{", "["))
                    or '"gate_call_id"' in encoded
                    or '"nonce"' in encoded
                ):
                    invalid = True
                return
            _walk(decoded, depth + 1)
            return
        if isinstance(value, list):
            for item in value:
                _walk(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        aliases = [
            value.get(name)
            for name in ("gate_call_id", "nonce")
            if name in value
        ]
        if aliases:
            if any(not isinstance(alias, str) or not alias for alias in aliases):
                invalid = True
                return
            alias_values = set(aliases)
            if len(alias_values) != 1:
                invalid = True
                return
            nonces.update(alias_values)
            signatures.add(json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ))
        # Only descend through host/result wrapper fields. A decoded pre-nonce
        # gate result is a terminal mapping; its request text is never parsed.
        for key in ("Ok", "content", "text", "output"):
            if key in value:
                _walk(value[key], depth + 1)

    _walk(result)
    return nonces, signatures, invalid


def _current_mcp_gate_end(
    record_type: object,
    payload: dict,
    *,
    timestamp: str | None,
    session_id: str | None,
    session_cwd: str | None,
    file: str,
    byte_offset: int,
    config: outcome_measurement.MeasurementConfig,
    proof_context: project_proof.ProjectProofContext | None,
) -> tuple[str, dict | None]:
    """Decode the current Codex ``mcp_tool_call_end`` gate envelope.

    Codex records the completed nested MCP invocation as an event rather than
    as the outer ``exec`` result. Normalize that atomic event into the direct
    call/result byte shape already owned by the shared S2 parser. The
    normalization is transient and prompt-free after this function returns.

    The status distinguishes unrelated events from a structurally identified
    but malformed gate event. The latter must enter the existing malformed
    receipt count so a hidden duplicate nonce cannot manufacture uniqueness.
    """
    if payload.get("type") != "mcp_tool_call_end":
        return _CURRENT_MCP_NOT_GATE, None
    result_nonces, result_signatures, result_structure_invalid = (
        _gate_result_candidates(payload.get("result"))
    )
    invocation = payload.get("invocation")
    if not isinstance(invocation, dict):
        return (
            (_CURRENT_MCP_INVALID, None)
            if result_structure_invalid or result_nonces
            else (_CURRENT_MCP_NOT_GATE, None)
        )
    server = invocation.get("server")
    tool = invocation.get("tool")
    if isinstance(server, str) and isinstance(tool, str):
        if server not in GATE_SERVER_NAMES or tool not in GATE_TOOL_NAMES:
            return _CURRENT_MCP_NOT_GATE, None
    else:
        return (
            (_CURRENT_MCP_INVALID, None)
            if result_structure_invalid or result_nonces
            else (_CURRENT_MCP_NOT_GATE, None)
        )
    if record_type != "event_msg":
        return _CURRENT_MCP_INVALID, None
    if (
        result_structure_invalid
        or len(result_nonces) > 1
        or len(result_signatures) > 1
    ):
        return _CURRENT_MCP_INVALID, None
    decoded_result = outcome_measurement._gate_result_payload(
        payload.get("result")
    )
    if decoded_result is None:
        result_envelope = payload.get("result")
        if isinstance(result_envelope, dict) and "Err" in result_envelope:
            return _CURRENT_MCP_NOT_GATE, None
        return _CURRENT_MCP_INVALID, None
    result_has_nonce = bool(
        isinstance(decoded_result, dict)
        and (decoded_result.get("gate_call_id") or decoded_result.get("nonce"))
    )
    # Pre-nonce MCP end events are historical input already represented by the
    # outer exec call. This adapter owns only the current exact-nonce encoding.
    if not result_has_nonce:
        return _CURRENT_MCP_NOT_GATE, None
    arguments = invocation.get("arguments")
    request = arguments.get("request") if isinstance(arguments, dict) else None
    call_id = payload.get("call_id")
    if (
        not isinstance(request, str)
        or not request.strip()
        or not isinstance(call_id, str)
        or not call_id
    ):
        return _CURRENT_MCP_INVALID, None

    # Feed the new external envelope through the existing result decoder. This
    # keeps nonce, proof, runtime, verdict, and typed-field semantics in one
    # place without teaching the attribution adapter a second result schema.
    synthetic = (
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": session_cwd},
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": tool,
                "call_id": call_id,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        },
        {
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": payload.get("result"),
            },
        },
    )
    synthetic_bytes = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in synthetic
    ).encode("utf-8")
    observations, markers = outcome_measurement.parse_host_record_bytes(
        synthetic_bytes,
        file=file,
        config=config,
        project_proof_context=proof_context,
        vault_key=None,
    )
    rows = [row for row in observations if row.adapter == "codex"]
    if (
        len(rows) != 1
        or not isinstance(rows[0].nonce, str)
        or not rows[0].nonce
        or any(marker.reason == "schema_invalid" for marker in markers)
    ):
        return _CURRENT_MCP_INVALID, None

    row = rows[0]
    metadata = _observation_metadata(row)
    metadata["stream_coordinate"] = (file, byte_offset)
    call = {
        "ts": _parse_ts(timestamp),
        "gate_call_id": row.nonce,
        "invocation_id": call_id,
        "skipped": row.skipped,
        "host_observation": metadata,
        "call_project_proof": metadata.get("host_scope_project_proof"),
    }
    return _CURRENT_MCP_VALID, call


def _async_exec_cell_id(output: object) -> str | None:
    """Read the corpus-observed asynchronous exec placeholder cell id."""
    if not isinstance(output, str):
        return None
    lines = output.splitlines()
    first_line = lines[0] if lines else ""
    prefix = "Script running with cell ID "
    if not first_line.startswith(prefix):
        return None
    cell_id = first_line[len(prefix):].strip().removesuffix(".")
    return cell_id or None


def _looks_like_async_exec_placeholder(output: object) -> bool:
    if not isinstance(output, str):
        return False
    first_line = (output.splitlines() or [""])[0].casefold()
    return "script running" in first_line and "cell id" in first_line


def _exec_script(payload: dict) -> str | None:
    script = payload.get("input")
    if script is None:
        script = payload.get("arguments")
    if isinstance(script, dict):
        script = script.get("input") or script.get("code")
    return script if isinstance(script, str) else None


def _mcp_invocation_request(payload: dict) -> str | None:
    invocation = payload.get("invocation")
    arguments = invocation.get("arguments") if isinstance(invocation, dict) else None
    request = arguments.get("request") if isinstance(arguments, dict) else None
    return request if isinstance(request, str) and request.strip() else None


def _js_string_token(script: str, start: int) -> tuple[str, int] | None:
    """Decode one quoted JS string without evaluating the surrounding code."""
    quote = script[start]
    if quote not in {"'", '"'}:
        return None
    chars: list[str] = []
    index = start + 1
    escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
        "'": "'",
        '"': '"',
        "\\": "\\",
        "/": "/",
    }
    while index < len(script):
        char = script[index]
        if char == quote:
            try:
                value = "".join(chars)
                value = value.encode("utf-16-le", "surrogatepass").decode(
                    "utf-16-le"
                )
            except UnicodeError:
                return None
            return value, index + 1
        if char in "\r\n":
            return None
        if char != "\\":
            if ord(char) < 0x20:
                return None
            chars.append(char)
            index += 1
            continue
        index += 1
        if index >= len(script):
            return None
        escaped = script[index]
        if escaped in "\r\n":
            if escaped == "\r" and index + 1 < len(script) and script[index + 1] == "\n":
                index += 1
            index += 1
            continue
        if escaped in escapes:
            chars.append(escapes[escaped])
            index += 1
            continue
        if escaped == "x":
            encoded = script[index + 1:index + 3]
            if len(encoded) != 2 or any(c not in string.hexdigits for c in encoded):
                return None
            chars.append(chr(int(encoded, 16)))
            index += 3
            continue
        if escaped == "u":
            if index + 1 < len(script) and script[index + 1] == "{":
                end = script.find("}", index + 2)
                encoded = script[index + 2:end] if end >= 0 else ""
                if (
                    not encoded
                    or len(encoded) > 6
                    or any(c not in string.hexdigits for c in encoded)
                ):
                    return None
                codepoint = int(encoded, 16)
                if codepoint > 0x10FFFF:
                    return None
                chars.append(chr(codepoint))
                index = end + 1
                continue
            encoded = script[index + 1:index + 5]
            if len(encoded) != 4 or any(c not in string.hexdigits for c in encoded):
                return None
            chars.append(chr(int(encoded, 16)))
            index += 5
            continue
        return None
    return None


def _js_tokens(script: str) -> list[tuple[str, str]] | None:
    """Tokenize the small JS subset needed to bind a gate request argument."""
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(script):
        char = script[index]
        if char.isspace():
            index += 1
            continue
        if script.startswith("//", index):
            newline = script.find("\n", index + 2)
            index = len(script) if newline < 0 else newline + 1
            continue
        if script.startswith("/*", index):
            end = script.find("*/", index + 2)
            if end < 0:
                return None
            index = end + 2
            continue
        if char in {"'", '"'}:
            parsed = _js_string_token(script, index)
            if parsed is None:
                return None
            value, index = parsed
            tokens.append(("string", value))
            continue
        if char.isalpha() or char in {"_", "$"}:
            end = index + 1
            while end < len(script) and (
                script[end].isalnum() or script[end] in {"_", "$"}
            ):
                end += 1
            tokens.append(("identifier", script[index:end]))
            index = end
            continue
        if char == "`":
            # Templates can appear elsewhere in the exec program.  Skip the
            # complete literal so incidental text cannot become tokens.  A
            # template used as the gate request itself remains unsupported and
            # therefore fails closed in an ambiguous resumed fold.
            index += 1
            while index < len(script):
                if script[index] == "\\":
                    index += 2
                    continue
                if script[index] == "`":
                    index += 1
                    tokens.append(("template", ""))
                    break
                index += 1
            else:
                return None
            continue
        tokens.append(("punctuation", char))
        index += 1
    return tokens


def _exec_gate_request(script: object) -> str | None:
    """Extract the direct request string from one structural gate invocation."""
    if not isinstance(script, str):
        return None
    tokens = _js_tokens(script)
    if tokens is None:
        return None
    tool_names = {
        "mcp__latch__latch_gate",
        "mcp__latch__kb_gate",
        "mcp__claude_kb__latch_gate",
        "mcp__claude_kb__kb_gate",
    }
    scope_paths: list[tuple[int, ...]] = []
    scope_stack: list[int] = []
    for token_index, token in enumerate(tokens):
        if token == ("punctuation", "}") and scope_stack:
            scope_stack.pop()
        scope_paths.append(tuple(scope_stack))
        if token == ("punctuation", "{"):
            scope_stack.append(token_index)
    requests: list[str] = []
    for index in range(len(tokens) - 4):
        if (
            tokens[index] != ("identifier", "tools")
            or tokens[index + 1] != ("punctuation", ".")
        ):
            continue
        if tokens[index + 2] not in {
            ("identifier", name) for name in tool_names
        }:
            continue
        if tokens[index + 3:index + 5] != [
            ("punctuation", "("),
            ("punctuation", "{"),
        ]:
            continue
        bindings: dict[str, tuple[str, tuple[int, ...], int]] = {}
        invalid_bindings: set[tuple[str, tuple[int, ...]]] = set()
        call_scope = scope_paths[index]
        for binding_index in range(max(0, index - 3)):
            if (
                tokens[binding_index] == ("identifier", "const")
                and tokens[binding_index + 1][0] == "identifier"
                and tokens[binding_index + 2] == ("punctuation", "=")
                and tokens[binding_index + 3][0] == "string"
                and binding_index + 4 < len(tokens)
                and tokens[binding_index + 4] in {
                    ("punctuation", ";"),
                    ("punctuation", ","),
                }
            ):
                name = tokens[binding_index + 1][1]
                binding_scope = scope_paths[binding_index]
                if call_scope[:len(binding_scope)] != binding_scope:
                    continue
                key = (name, binding_scope)
                current = bindings.get(name)
                if current is not None and current[1] == binding_scope:
                    invalid_bindings.add(key)
                if current is None or len(binding_scope) >= len(current[1]):
                    bindings[name] = (
                        tokens[binding_index + 3][1],
                        binding_scope,
                        binding_index,
                    )
        depth = 1
        bracket_depth = 0
        paren_depth = 0
        cursor = index + 5
        found: list[str] = []
        while cursor < len(tokens) and depth:
            token = tokens[cursor]
            if token == ("punctuation", "{"):
                depth += 1
            elif token == ("punctuation", "}"):
                depth -= 1
            elif token == ("punctuation", "["):
                bracket_depth += 1
            elif token == ("punctuation", "]"):
                bracket_depth -= 1
            elif token == ("punctuation", "("):
                paren_depth += 1
            elif token == ("punctuation", ")"):
                paren_depth -= 1
            elif (
                depth == 1
                and bracket_depth == 0
                and paren_depth == 0
                and token in {
                    ("identifier", "request"),
                    ("string", "request"),
                }
                and cursor > 0
                and tokens[cursor - 1] in {
                    ("punctuation", "{"),
                    ("punctuation", ","),
                }
            ):
                if (
                    cursor + 2 < len(tokens)
                    and tokens[cursor + 1] == ("punctuation", ":")
                    and cursor + 3 < len(tokens)
                    and tokens[cursor + 3] in {
                        ("punctuation", ","),
                        ("punctuation", "}"),
                    }
                ):
                    value = tokens[cursor + 2]
                    if value[0] == "string":
                        found.append(value[1])
                    elif (
                        value[0] == "identifier"
                        and value[1] in bindings
                        and (
                            value[1], bindings[value[1]][1]
                        ) not in invalid_bindings
                    ):
                        found.append(bindings[value[1]][0])
                elif (
                    token == ("identifier", "request")
                    and cursor + 1 < len(tokens)
                    and tokens[cursor + 1] in {
                        ("punctuation", ","),
                        ("punctuation", "}"),
                    }
                    and "request" in bindings
                    and (
                        "request", bindings["request"][1]
                    ) not in invalid_bindings
                ):
                    found.append(bindings["request"][0])
            cursor += 1
        if (
            depth != 0
            or bracket_depth != 0
            or paren_depth != 0
            or len(found) != 1
        ):
            return None
        requests.append(found[0])
    return requests[0] if len(requests) == 1 else None


def _wait_cell_id(payload: dict) -> str | None:
    if payload.get("type") != "function_call" or payload.get("name") != "wait":
        return None
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    cell_id = arguments.get("cell_id")
    return cell_id.strip() if isinstance(cell_id, str) and cell_id.strip() else None


def _scan_gate_calls_in_snapshot(
    data: bytes,
    *,
    file: str,
    config: outcome_measurement.MeasurementConfig,
    proof_context: project_proof.ProjectProofContext | None = None,
) -> tuple[list[dict], dict[str, object]]:
    """Parse structural gate calls and fold shared S2 result metadata.

    The shared parser owns all host wrapping semantics.  This scanner only
    records prompt-free call coordinates and validates candidate-bearing call
    arguments so malformed/missing requests make completeness fail closed.
    """
    observations, markers = outcome_measurement.parse_host_record_bytes(
        data,
        file=file,
        config=config,
        project_proof_context=proof_context,
        vault_key=None,
    )
    by_offset: dict[int, list[outcome_measurement.Observation]] = {}
    for row in observations:
        by_offset.setdefault(row.byte_offset, []).append(row)
    out: list[dict] = []
    # The shared parser is authoritative for JSON/schema validity.  Candidate
    # completeness must include every one of its schema-invalid regions, even
    # when the malformed bytes do not happen to contain a gate tool-name token.
    malformed = sum(marker.reason == "schema_invalid" for marker in markers)
    pending_exec_wrappers: dict[str, list[dict]] = {}
    async_exec_wrappers: dict[str, list[dict]] = {}
    waiting_exec_wrappers: dict[str, list[dict]] = {}
    retired_exec_wrappers: list[dict] = []
    zoned_ts_by_offset: dict[int, datetime | None] = {}
    record_coordinates: list[tuple[int, datetime | None]] = []
    result_ts_by_context: dict[tuple, list[datetime | None]] = {}
    result_outputs_by_context: dict[tuple, list[object]] = {}
    calls_by_result_context: dict[tuple, list[dict]] = {}
    direct_gate_contexts: set[tuple] = set()
    active_session_id: str | None = None
    active_session_cwd: str | None = None
    session_generation = 0

    def _active_exec_wrappers() -> list[dict]:
        active: list[dict] = []
        seen: set[int] = set()
        for mapping in (
            waiting_exec_wrappers,
            async_exec_wrappers,
            pending_exec_wrappers,
        ):
            for wrappers in mapping.values():
                for wrapper in wrappers:
                    identity = id(wrapper)
                    if identity not in seen:
                        seen.add(identity)
                        active.append(wrapper)
        return active

    def _drop_exec_wrappers(wrappers: list[dict]) -> None:
        retired = {id(wrapper) for wrapper in wrappers}
        if not retired:
            return
        for mapping in (
            waiting_exec_wrappers,
            async_exec_wrappers,
            pending_exec_wrappers,
        ):
            for key, rows in tuple(mapping.items()):
                kept = [row for row in rows if id(row) not in retired]
                if kept:
                    mapping[key] = kept
                else:
                    mapping.pop(key, None)

    def _retire_exec_wrappers(wrappers: list[dict]) -> None:
        retired_exec_wrappers.extend(
            wrapper
            for wrapper in wrappers
            if all(id(wrapper) != id(row) for row in retired_exec_wrappers)
        )
        _drop_exec_wrappers(wrappers)

    def _result_context(call_id: str) -> tuple:
        return (active_session_cwd, active_session_id, call_id)

    offset = 0
    for line_index, raw_with_end in enumerate(data.splitlines(keepends=True)):
        raw = raw_with_end.rstrip(b"\r\n")
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            offset += len(raw_with_end)
            continue
        if not isinstance(obj, dict):
            offset += len(raw_with_end)
            continue
        zoned_ts_by_offset[offset] = _parse_zoned_ts(obj.get("timestamp"))
        record_coordinates.append((offset, zoned_ts_by_offset[offset]))
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            offset += len(raw_with_end)
            continue
        if obj.get("type") == "session_meta" or payload.get("type") == "session_meta":
            session_generation += 1
            meta = payload if payload else obj
            raw_session = meta.get("id") or meta.get("session_id")
            raw_cwd = meta.get("cwd")
            active_session_id = (
                raw_session.strip()
                if isinstance(raw_session, str) and raw_session.strip()
                else None
            )
            active_session_cwd = (
                raw_cwd.strip()
                if isinstance(raw_cwd, str) and raw_cwd.strip()
                else None
            )
        payload_type = payload.get("type")
        if payload_type in {"function_call_output", "custom_tool_call_output"}:
            result_call_id = payload.get("call_id")
            if isinstance(result_call_id, str) and result_call_id:
                result_context = _result_context(result_call_id)
                result_ts_by_context.setdefault(result_context, []).append(
                    zoned_ts_by_offset[offset]
                )
                result_outputs_by_context.setdefault(result_context, []).append(
                    payload.get("output")
                )
        if payload_type == "custom_tool_call_output":
            output_call_id = payload.get("call_id")
            candidates = (
                pending_exec_wrappers.get(output_call_id, [])
                if isinstance(output_call_id, str) else []
            )
            cell_id = _async_exec_cell_id(payload.get("output"))
            if cell_id is not None and candidates:
                async_exec_wrappers.setdefault(cell_id, []).extend(candidates)
            elif candidates and _looks_like_async_exec_placeholder(
                payload.get("output")
            ):
                malformed += 1
            if isinstance(output_call_id, str):
                pending_exec_wrappers.pop(output_call_id, None)
        elif payload_type in {"function_call", "custom_tool_call"}:
            wait_cell_id = _wait_cell_id(payload)
            if wait_cell_id is not None:
                candidates = async_exec_wrappers.pop(wait_cell_id, [])
                if candidates:
                    waiting_exec_wrappers.setdefault(
                        wait_cell_id, []
                    ).extend(candidates)

        current_status, current_call = _current_mcp_gate_end(
            obj.get("type"),
            payload,
            timestamp=obj.get("timestamp"),
            session_id=active_session_id,
            session_cwd=active_session_cwd,
            file=file,
            byte_offset=offset,
            config=config,
            proof_context=proof_context,
        )
        matched_exec_wrapper = None
        closes_gate_lifecycle = (
            payload_type == "mcp_tool_call_end"
            and (
                current_status != _CURRENT_MCP_NOT_GATE
                or _is_supported_mcp_gate_end(obj.get("type"), payload)
            )
        )
        if closes_gate_lifecycle:
            # A supported pre-nonce end still closes its gate lifecycle, but
            # an interleaved end event for an unrelated MCP tool does not.
            active_wrappers = _active_exec_wrappers()
            if retired_exec_wrappers:
                # End records carry no outer call/cell id.  A resumed file can
                # therefore have both a retired prior-generation wrapper and a
                # newer active wrapper.  Use the exact invocation request only
                # transiently to identify a unique corpus-shaped script; never
                # emit it.  A repeated or unrecognized spelling fails closed.
                request = _mcp_invocation_request(payload)
                wrapper_pool = [*retired_exec_wrappers, *active_wrappers]
                exact_matches = [
                    wrapper
                    for wrapper in wrapper_pool
                    if request is not None
                    and _exec_gate_request(wrapper.get("_exec_script")) == request
                ]
                if len(exact_matches) == 1:
                    matched_exec_wrapper = exact_matches[0]
                    matched_active = any(
                        id(matched_exec_wrapper) == id(wrapper)
                        for wrapper in active_wrappers
                    )
                    _drop_exec_wrappers([matched_exec_wrapper])
                    if matched_active:
                        # A proven result for the new generation establishes
                        # that older carried wrappers were abandoned at the
                        # resume boundary; do not let tombstones poison later
                        # independent gate lifecycles in the same long file.
                        retired_exec_wrappers.clear()
                    else:
                        retired_exec_wrappers[:] = [
                            wrapper
                            for wrapper in retired_exec_wrappers
                            if id(wrapper) != id(matched_exec_wrapper)
                        ]
                else:
                    malformed += 1
                    pending_exec_wrappers.clear()
                    async_exec_wrappers.clear()
                    waiting_exec_wrappers.clear()
                    retired_exec_wrappers.clear()
            elif len(active_wrappers) == 1:
                matched_exec_wrapper = active_wrappers[0]
                _drop_exec_wrappers([matched_exec_wrapper])
            elif len(active_wrappers) > 1:
                malformed += 1
                pending_exec_wrappers.clear()
                async_exec_wrappers.clear()
                waiting_exec_wrappers.clear()
        if current_status == _CURRENT_MCP_INVALID:
            malformed += 1
            offset += len(raw_with_end)
            continue
        if current_status == _CURRENT_MCP_VALID and current_call is not None:
            current_call.update({
                "line_index": line_index,
                "byte_offset": offset,
                "defect_ts": _parse_zoned_ts(obj.get("timestamp")),
                "defect_host_ts": _parse_zoned_ts(obj.get("timestamp")),
            })
            if matched_exec_wrapper is not None:
                matched = matched_exec_wrapper
                matched["gate_call_id"] = current_call["gate_call_id"]
                matched["skipped"] = current_call["skipped"]
                matched["host_observation"] = current_call["host_observation"]
                matched["defect_host_ts"] = current_call["defect_host_ts"]
            else:
                out.append(current_call)
            offset += len(raw_with_end)
            continue
        shared_rows = [
            row for row in by_offset.get(offset, ()) if row.adapter == "codex"
        ]
        direct_gate_call = (
            payload.get("type") in ("function_call", "custom_tool_call")
            and payload.get("name") in GATE_TOOL_NAMES
        )
        # Current Codex wraps MCP calls in an outer ``exec`` custom tool call.
        # The shared parser owns structural recognition of that JavaScript
        # envelope, so this adapter consumes its codex observation at the same
        # pinned byte coordinate instead of searching arbitrary script text.
        shared_codex_gate_call = bool(shared_rows)
        if not direct_gate_call and not shared_codex_gate_call:
            offset += len(raw_with_end)
            continue

        call_id = payload.get("call_id") or payload.get("id")
        if not isinstance(call_id, str) or not call_id:
            malformed += 1
        if direct_gate_call:
            if isinstance(call_id, str) and call_id:
                direct_gate_contexts.add(_result_context(call_id))
            raw_args = payload.get("arguments")
            if not isinstance(raw_args, str):
                raw_args = payload.get("input")
            decoded = None
            if isinstance(raw_args, str) and raw_args:
                try:
                    decoded = json.loads(raw_args)
                except json.JSONDecodeError:
                    decoded = None
            request = decoded.get("request") if isinstance(decoded, dict) else None
            if not isinstance(request, str) or not request.strip():
                malformed += 1

        # One call can have multiple nonidentical results. The shared parser
        # already coalesces byte-identical observations; preserve every row it
        # retains so attribution cannot manufacture nonce uniqueness by
        # collapsing a same-offset conflict.
        for shared in shared_rows or (None,):
            metadata = (
                _observation_metadata(shared) if shared is not None else None
            )
            call = {
                "ts": _parse_ts(obj.get("timestamp")),
                "gate_call_id": shared.nonce if shared is not None else None,
                "invocation_id": call_id,
                "skipped": shared.skipped if shared is not None else None,
                "line_index": line_index,
                "byte_offset": offset,
                "host_observation": metadata,
                "defect_ts": _parse_zoned_ts(obj.get("timestamp")),
                "call_project_proof": (
                    proof_context.prove(active_session_cwd)
                    if proof_context is not None and active_session_cwd
                    else None
                ),
                "_call_session_id": active_session_id,
                "_call_session_cwd": active_session_cwd,
                "_call_session_generation": session_generation,
                "_exec_script": _exec_script(payload),
            }
            out.append(call)
            if isinstance(call_id, str) and call_id:
                result_context = _result_context(call_id)
                call["_result_context"] = result_context
                calls_by_result_context.setdefault(result_context, []).append(call)
            if (
                not direct_gate_call
                and payload.get("type") == "custom_tool_call"
                and payload.get("name") == "exec"
                and shared is not None
                and shared.nonce is None
            ):
                # A resumed transcript can abandon an older async wrapper and
                # later begin a new gate call.  Retain the old wrapper across
                # the session_meta itself so an immediately following end can
                # still expose a cross-session identity contradiction, but
                # retire it once a new gate lifecycle starts in the new
                # session/cwd.  Same-context concurrent wrappers remain
                # active and therefore fail closed as ambiguous at the end.
                _retire_exec_wrappers([
                    wrapper
                    for wrapper in _active_exec_wrappers()
                    if (
                        wrapper.get("_call_session_generation")
                        != session_generation
                        or (
                            wrapper.get("_call_session_id"),
                            wrapper.get("_call_session_cwd"),
                        ) != (active_session_id, active_session_cwd)
                    )
                ])
                outer_call_id = payload.get("call_id") or payload.get("id")
                if isinstance(outer_call_id, str) and outer_call_id:
                    call["_outer_exec_call_id"] = outer_call_id
                    pending_exec_wrappers.setdefault(outer_call_id, []).append(call)
        offset += len(raw_with_end)

    for direct_context in direct_gate_contexts:
        for result_output in result_outputs_by_context.get(direct_context, []):
            nonces, signatures, result_invalid = _gate_result_candidates(
                result_output
            )
            if result_invalid or len(nonces) > 1 or len(signatures) > 1:
                malformed += 1
    if _active_exec_wrappers():
        malformed += 1
    for result_context, calls in calls_by_result_context.items():
        result_coordinates = result_ts_by_context.get(result_context, [])
        if len(result_coordinates) == len(calls):
            for call, result_ts in zip(calls, result_coordinates):
                call.setdefault("defect_host_ts", result_ts)
        else:
            for call in calls:
                call.setdefault("defect_host_ts", None)
    for call in out:
        call.pop("_outer_exec_call_id", None)
        call.pop("_result_context", None)
        call.pop("_call_session_id", None)
        call.pop("_call_session_cwd", None)
        call.pop("_call_session_generation", None)
        call.pop("_exec_script", None)
    missing_result_markers = tuple(
        marker
        for marker in markers
        if marker.reason == "host_call_output_missing"
    )

    def _missing_result_interval(marker) -> dict[str, object]:
        start = zoned_ts_by_offset.get(marker.byte_offset)
        next_coordinates = [
            ts for record_offset, ts in record_coordinates
            if isinstance(marker.byte_offset, int)
            and record_offset > marker.byte_offset
        ]
        end = next_coordinates[0] if next_coordinates else None
        return {
            "start": start,
            "end": end,
            "byte_offset": marker.byte_offset,
        }

    return out, {
        "unreadable": False,
        "unstable": False,
        "malformed_candidate_regions": malformed,
        "missing_tool_results": len(missing_result_markers),
        "missing_tool_result_defects": tuple(
            _missing_result_interval(marker)
            for marker in missing_result_markers
        ),
    }


def _scan_gate_calls_in_transcript(
    path: Path,
) -> tuple[list[dict], dict[str, object]]:
    """Compatibility wrapper; production indexing supplies one pinned snapshot."""
    data, _digest, unstable = _read_snapshot(path)
    if data is None:
        return [], {
            "unreadable": True,
            "unstable": False,
            "malformed_candidate_regions": 0,
            "missing_tool_results": 0,
            "missing_tool_result_defects": (),
        }
    calls, health = _scan_gate_calls_in_snapshot(
        data,
        file=str(path),
        config=_parser_config(None, None),
        proof_context=None,
    )
    health["unstable"] = unstable
    return calls, health


def _gate_calls_in_transcript(path: Path) -> list[dict]:
    return _scan_gate_calls_in_transcript(path)[0]


def _defect_count_row(
    total: int,
    *,
    blocking: int,
    waived: int,
    timestamp_uncertain: int,
) -> dict[str, int]:
    return {
        "total": total,
        "blocking": blocking,
        "waived": waived,
        "timestamp_uncertain": timestamp_uncertain,
    }


def _finalize_base_completeness(
    receipt: dict[str, object],
    defects: list[dict],
) -> dict[str, object]:
    for position, defect in enumerate(defects, start=1):
        defect["defect_id"] = f"D{position:06d}"
    ledger_counts = {
        kind: sum(defect.get("kind") == kind for defect in defects)
        for kind in SCOPABLE_DEFECT_KINDS
    }
    defect_ids = [defect.get("defect_id") for defect in defects]
    reconciled = all(
        ledger_counts[kind] == int(receipt[kind])
        for kind in SCOPABLE_DEFECT_KINDS
    ) and len(defect_ids) == len(set(defect_ids))
    uncertain = {
        kind: sum(
            defect.get("kind") == kind
            and (
                not isinstance(defect.get("start"), datetime)
                or not isinstance(defect.get("end"), datetime)
            )
            for defect in defects
        )
        for kind in SCOPABLE_DEFECT_KINDS
    }
    global_blockers = {
        field: int(receipt[field])
        for field in GLOBAL_BLOCKER_FIELDS
        if int(receipt[field])
    }
    if not bool(receipt.get("root_present")):
        global_blockers["root_absent"] = 1
    if bool(receipt.get("inventory_changed")):
        global_blockers["inventory_changed"] = 1
    if not reconciled:
        global_blockers["defect_ledger_mismatch"] = 1
    timestamp_uncertain = sum(uncertain.values())
    if timestamp_uncertain:
        global_blockers["timestamp_uncertain_defects"] = timestamp_uncertain

    global_complete = not global_blockers
    receipt.update({
        "defect_scope": DEFECT_SCOPE_VERSION,
        "defect_ledger_reconciled": reconciled,
        "defect_ledger_sha256": _defect_ledger_sha256(defects),
        "global_blockers": global_blockers,
        "global_complete": global_complete,
        "measurement_window": None,
        "waived_defects": [],
        "scoped_defects": {
            kind: _defect_count_row(
                int(receipt[kind]),
                blocking=int(receipt[kind]),
                waived=0,
                timestamp_uncertain=uncertain[kind],
            )
            for kind in SCOPABLE_DEFECT_KINDS
        },
        "waiver_reasons": {
            "proven_disjoint_interval": {
                kind: 0 for kind in SCOPABLE_DEFECT_KINDS
            },
        },
        "blocking_reasons": {
            "intersects_measurement_window": {
                kind: max(0, int(receipt[kind]) - uncertain[kind])
                for kind in SCOPABLE_DEFECT_KINDS
            },
            "timestamp_uncertain": uncertain,
        },
    })
    receipt["complete"] = bool(
        global_complete
        and not any(int(receipt[kind]) for kind in SCOPABLE_DEFECT_KINDS)
    )
    return _with_receipt_hash(receipt)


def build_index(
    home: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    *,
    proof_context: project_proof.ProjectProofContext | None = None,
    target_project_path: str | Path | None = None,
) -> dict:
    """Build a candidate-complete index of gate calls in every Codex rollout.

    ``start_date`` and ``end_date`` are compatibility-only report coordinates,
    not rollout-directory or refusal scopes. Discovery always covers the full
    source root because a resumed thread remains in its start-day directory.
    A caller that opts into ratified defect scoping supplies the exact nominal
    measurement window to :func:`attribute`.

    ``candidate_completeness`` is content-free and contains no prompts or paths.
    Attribution refuses an index whose inventory or any full-file digest changed,
    could not be traversed/read, contained malformed candidate arguments, or
    has any unscoped or intersecting defect. Calls, cwd proof, and S2 metadata
    are all parsed from the same byte snapshot; the final digest pass prevents a
    middle rewrite from combining different file generations.

    Only ``by_nonce`` exists.  Historical hash recovery is frozen pilot data and
    is never recomputed. ``session_calls`` remains the exact, prompt-free stream
    for boundaries, including skipped and unmatched calls plus opaque project
    proof so foreign/mixed segments can be excluded.
    """
    del start_date, end_date
    by_nonce: dict[str, list[dict]] = {}
    session_calls: dict[str, list[dict]] = {}
    candidate_defects: list[dict] = []
    root = (home or codex_transcript.codex_home()) / "sessions"
    target_proof = (
        proof_context.prove(target_project_path)
        if proof_context is not None and target_project_path is not None
        else None
    )
    receipt: dict[str, object] = {
        "version": CANDIDATE_DISCOVERY_VERSION,
        "scope": "all_rollouts",
        "root_present": root.is_dir(),
        "enumerated_files": 0,
        "scanned_files": 0,
        "unreadable_files": 0,
        "unstable_files": 0,
        "content_changed_files": 0,
        "malformed_candidate_regions": 0,
        "missing_tool_results": 0,
        "session_identity_conflicts": 0,
        "unidentified_gate_files": 0,
        "traversal_errors": 0,
        "inventory_changed": False,
        "complete": False,
    }
    if not root.is_dir():
        receipt = _finalize_base_completeness(receipt, candidate_defects)
        return {
            "by_nonce": by_nonce,
            "session_calls": session_calls,
            "target_project_proof": target_proof,
            "candidate_completeness": receipt,
            "_candidate_defects": candidate_defects,
        }

    initial_paths, initial_errors = _enumerate_rollout_paths(root)
    receipt["enumerated_files"] = len(initial_paths)
    receipt["traversal_errors"] = initial_errors
    snapshot_hashes: dict[Path, str] = {}
    parser_config = _parser_config(proof_context, target_project_path)
    for path_order, path in enumerate(initial_paths):
        receipt["scanned_files"] = int(receipt["scanned_files"]) + 1
        data, digest, unstable = _read_snapshot(path)
        if data is None or digest is None:
            receipt["unreadable_files"] = int(receipt["unreadable_files"]) + 1
            continue
        snapshot_hashes[path] = digest
        calls, health = _scan_gate_calls_in_snapshot(
            data,
            file=str(path),
            config=parser_config,
            proof_context=proof_context,
        )
        if unstable:
            receipt["unstable_files"] = int(receipt["unstable_files"]) + 1
        receipt["malformed_candidate_regions"] = (
            int(receipt["malformed_candidate_regions"])
            + int(health["malformed_candidate_regions"])
        )
        receipt["missing_tool_results"] = (
            int(receipt["missing_tool_results"])
            + int(health["missing_tool_results"])
        )
        for defect in health.get("missing_tool_result_defects", ()):
            if not isinstance(defect, dict):
                continue
            candidate_defects.append({
                "kind": "missing_tool_results",
                "start": defect.get("start"),
                "end": defect.get("end"),
                "source_order": (
                    path_order,
                    int(defect.get("byte_offset") or 0),
                ),
            })
        session_id = codex_transcript.transcript_session_id_bytes(data)
        if not session_id:
            if calls:
                receipt["unidentified_gate_files"] = (
                    int(receipt["unidentified_gate_files"]) + 1
                )
            continue
        for call in calls:
            host_observation = call.get("host_observation")
            observed_session = (
                host_observation.get("session_id")
                if isinstance(host_observation, dict) else None
            )
            embedded_conflicts = tuple(
                host_observation.get("embedded_conflict_reasons") or ()
                if isinstance(host_observation, dict)
                else ()
            )
            identity_conflict = bool(
                (observed_session and observed_session != session_id)
                or "session_mismatch" in embedded_conflicts
            )
            if identity_conflict:
                receipt["session_identity_conflicts"] = (
                    int(receipt["session_identity_conflicts"]) + 1
                )
                host_ts = call.get("defect_host_ts")
                call_ts = call.get("defect_ts")
                if isinstance(call_ts, datetime) and isinstance(host_ts, datetime):
                    defect_start = min(call_ts, host_ts)
                    defect_end = max(call_ts, host_ts)
                else:
                    defect_start = None
                    defect_end = None
                candidate_defects.append({
                    "kind": "session_identity_conflicts",
                    "start": defect_start,
                    "end": defect_end,
                    "source_order": (path_order, call["byte_offset"]),
                })
            candidate = {
                "session_id": session_id,
                "transcript_path": str(path),
                "ts": call["ts"],
                "gate_call_id": call["gate_call_id"],
                "invocation_id": call.get("invocation_id"),
                "project_proof": call.get("call_project_proof"),
                "host_observation": host_observation,
                "source_order": (path_order, call["byte_offset"]),
            }
            nonce = call["gate_call_id"]
            if nonce:
                by_nonce.setdefault(nonce, []).append(candidate)
            session_calls.setdefault(session_id, []).append({
                "ts": call["ts"],
                "gate_call_id": call["gate_call_id"],
                "invocation_id": call.get("invocation_id"),
                "skipped": call["skipped"],
                "adapter": "codex",
                "project_proof": call.get("call_project_proof"),
                "host_observation": host_observation,
                # Numeric source order preserves (segment_path, byte offset)
                # order without copying prompt text into the structural stream.
                "source_order": (path_order, call["byte_offset"]),
            })

    final_paths, final_errors = _enumerate_rollout_paths(root)
    receipt["traversal_errors"] = int(receipt["traversal_errors"]) + final_errors
    receipt["inventory_changed"] = initial_paths != final_paths
    if not receipt["inventory_changed"]:
        for path in final_paths:
            _data, digest, unstable = _read_snapshot(path)
            if digest is None:
                receipt["unreadable_files"] = int(receipt["unreadable_files"]) + 1
                continue
            if unstable:
                receipt["unstable_files"] = int(receipt["unstable_files"]) + 1
            if snapshot_hashes.get(path) != digest:
                receipt["content_changed_files"] = (
                    int(receipt["content_changed_files"]) + 1
                )
    candidate_defects.sort(key=lambda defect: (
        defect.get("start") is None,
        defect.get("start") or datetime.max.replace(tzinfo=timezone.utc),
        defect.get("end") is None,
        defect.get("end") or datetime.max.replace(tzinfo=timezone.utc),
        defect.get("source_order") or (2**31, 2**63),
        str(defect.get("kind") or ""),
    ))
    receipt = _finalize_base_completeness(receipt, candidate_defects)
    for calls in session_calls.values():
        calls.sort(key=lambda call: (
            call["ts"] is None,
            call["ts"].timestamp() if call["ts"] is not None else float("inf"),
            call["source_order"],
        ))
    return {
        "by_nonce": by_nonce,
        "session_calls": session_calls,
        "target_project_proof": target_proof,
        "candidate_completeness": receipt,
        "_candidate_defects": candidate_defects,
    }


def _partition_by_project(
    candidates: list[dict],
    target_project_proof: dict | None,
    *,
    allow_legacy_unscoped: bool,
) -> tuple[list[dict], bool]:
    """Return proven matches and whether unresolved proof blocks attribution.

    Project partitioning can establish that at least one candidate belongs to
    the target, but it must never establish nonce uniqueness.  Same-nonce
    conflict detection runs over the complete candidate set first.  Missing,
    invalid, or rotated proof could still be the target, so absent a structural
    conflict it blocks attribution instead of making another candidate unique.
    """
    if target_project_proof is None and allow_legacy_unscoped:
        return list(candidates), False
    matched: list[dict] = []
    unresolved = False
    for candidate in candidates:
        status = project_proof.compare_project_proofs(
            candidate.get("project_proof"), target_project_proof,
        )
        if status == project_proof.PROJECT_MATCH:
            matched.append(candidate)
        elif status != project_proof.PROJECT_FOREIGN:
            unresolved = True
    return matched, unresolved


def _freeze_shared_value(value):
    if isinstance(value, dict):
        return tuple(
            sorted((str(key), _freeze_shared_value(item)) for key, item in value.items())
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_shared_value(item) for item in value)
    return value


def _candidate_shared_fields(candidate: dict) -> tuple:
    host = candidate.get("host_observation")
    host = host if isinstance(host, dict) else {}
    semantic_fields = (
        "nonce",
        "session_id",
        "adapter",
        "attestation",
        "measurement_protocol_version",
        "project_proof",
        "host_scope_project_proof",
        "key_epoch",
        "runtime_version",
        "verdict",
        "verdict_id_lists",
        "skipped",
        "observable",
        "evidence_available",
        "progress_inserts",
        "inserts",
        "linked_cited_insert",
        "cited_edge_activity",
        "touches",
        "embedded_conflict_reasons",
        "legacy_project",
        "hash_annotated",
        "pre_nonce",
    )
    return (
        candidate.get("session_id"),
        candidate.get("invocation_id"),
        _freeze_shared_value(candidate.get("project_proof")),
        tuple(
            (name, _freeze_shared_value(host.get(name)))
            for name in semantic_fields
        ),
    )


def _candidate_set_conflicts(candidates: list[dict]) -> tuple[str, ...]:
    """Return closed structural conflict reasons for one exact nonce."""
    reasons: set[str] = set()
    if len({row.get("session_id") for row in candidates}) > 1:
        reasons.add("nonce_in_multiple_sessions")
    if len({row.get("invocation_id") for row in candidates}) > 1:
        reasons.add("nonce_in_multiple_invocations")
    if len({_candidate_shared_fields(row) for row in candidates}) > 1:
        reasons.add("nonidentical_nonce_candidate")
    timestamps = {row.get("ts") for row in candidates}
    if len(timestamps) > 1:
        reasons.add("nonce_timestamp_conflict")
    return tuple(sorted(reasons))


def _v3_base_receipt_is_reconciled(base: dict, defects: list[dict]) -> bool:
    """Recompute every v3 waiver precondition from the private index state."""
    if not _receipt_hash_matches(base):
        return False
    if not all(isinstance(defect, dict) for defect in defects):
        return False

    counts: dict[str, int] = {}
    for field in (
        "enumerated_files",
        "scanned_files",
        *GLOBAL_BLOCKER_FIELDS,
        *SCOPABLE_DEFECT_KINDS,
    ):
        count = _receipt_count(base.get(field))
        if count is None:
            return False
        counts[field] = count

    ledger_counts = {
        kind: sum(defect.get("kind") == kind for defect in defects)
        for kind in SCOPABLE_DEFECT_KINDS
    }
    defect_ids = [defect.get("defect_id") for defect in defects]
    ledger_valid = bool(
        all(
            defect.get("kind") in SCOPABLE_DEFECT_KINDS
            and isinstance(defect.get("defect_id"), str)
            and defect.get("defect_id") == f"D{position:06d}"
            for position, defect in enumerate(defects, start=1)
        )
        and len(defect_ids) == len(set(defect_ids))
        and all(
            ledger_counts[kind] == counts[kind]
            for kind in SCOPABLE_DEFECT_KINDS
        )
        and base.get("defect_ledger_reconciled") is True
        and base.get("defect_ledger_sha256") == _defect_ledger_sha256(defects)
    )

    uncertain = sum(
        not isinstance(defect.get("start"), datetime)
        or defect["start"].tzinfo is None
        or defect["start"].utcoffset() is None
        or not isinstance(defect.get("end"), datetime)
        or defect["end"].tzinfo is None
        or defect["end"].utcoffset() is None
        for defect in defects
    )
    expected_blockers = {
        field: counts[field]
        for field in GLOBAL_BLOCKER_FIELDS
        if counts[field]
    }
    if base.get("root_present") is not True:
        expected_blockers["root_absent"] = 1
    if base.get("inventory_changed") is not False:
        expected_blockers["inventory_changed"] = 1
    if counts["scanned_files"] != counts["enumerated_files"]:
        expected_blockers["scan_count_mismatch"] = 1
    if not ledger_valid:
        expected_blockers["defect_ledger_mismatch"] = 1
    if uncertain:
        expected_blockers["timestamp_uncertain_defects"] = uncertain

    return bool(
        ledger_valid
        and base.get("global_blockers") == expected_blockers
        and base.get("global_complete") is (not expected_blockers)
    )


def _all_blocking_scoped_receipt(
    base: dict,
    *,
    reason: str,
    measurement_window: dict | None = None,
) -> dict:
    out = dict(base)
    scoped = {}
    uncertain = {}
    prior_scoped = base.get("scoped_defects")
    prior_scoped = prior_scoped if isinstance(prior_scoped, dict) else {}
    for kind in SCOPABLE_DEFECT_KINDS:
        total = _receipt_count(base.get(kind)) or 0
        prior = prior_scoped.get(kind)
        prior = prior if isinstance(prior, dict) else {}
        unknown = _receipt_count(prior.get("timestamp_uncertain")) or 0
        scoped[kind] = _defect_count_row(
            total,
            blocking=total,
            waived=0,
            timestamp_uncertain=unknown,
        )
        uncertain[kind] = unknown
    prior_blockers = base.get("global_blockers")
    prior_blockers = prior_blockers if isinstance(prior_blockers, dict) else {}
    out.update({
        "complete": False,
        "scope_error": reason,
        "measurement_window": measurement_window,
        "scoped_defects": scoped,
        "global_blockers": dict(prior_blockers),
        "waived_defects": [],
        "waiver_reasons": {
            "proven_disjoint_interval": {
                kind: 0 for kind in SCOPABLE_DEFECT_KINDS
            },
        },
        "blocking_reasons": {
            reason: {
                kind: scoped[kind]["blocking"]
                for kind in SCOPABLE_DEFECT_KINDS
            },
            "timestamp_uncertain": uncertain,
        },
    })
    return _with_receipt_hash(out)


def candidate_completeness_for_window(
    gate_row: dict,
    index: dict,
    *,
    window_seconds: int,
) -> dict:
    """Return a prompt-free completeness receipt for one nominal window.

    Discovery and nonce candidates remain all-time. Only the two ratified,
    timestamped defect kinds can be waived when their closed coordinate
    interval is proven disjoint from ``[gate_ts, gate_ts + window_seconds]``.
    """
    base = index.get("candidate_completeness")
    if not isinstance(base, dict):
        return _all_blocking_scoped_receipt(
            {"version": CANDIDATE_DISCOVERY_VERSION},
            reason="candidate_receipt_missing",
        )
    if base.get("version") != CANDIDATE_DISCOVERY_VERSION:
        return _all_blocking_scoped_receipt(
            base,
            reason="scoped_receipt_version_unproven",
        )
    if base.get("defect_scope") != DEFECT_SCOPE_VERSION:
        return _all_blocking_scoped_receipt(
            base,
            reason="scoped_receipt_version_unproven",
        )

    if (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds < 0
    ):
        return _all_blocking_scoped_receipt(
            base,
            reason="measurement_window_invalid",
        )
    window_start = _parse_zoned_ts(gate_row.get("ts"))
    if window_start is None:
        return _all_blocking_scoped_receipt(
            base,
            reason="measurement_window_timestamp_invalid",
        )
    try:
        window_end = window_start + timedelta(seconds=window_seconds)
    except OverflowError:
        return _all_blocking_scoped_receipt(
            base,
            reason="measurement_window_invalid",
        )
    window = {
        "start": _format_receipt_ts(window_start),
        "end_inclusive": _format_receipt_ts(window_end),
        "window_seconds": window_seconds,
    }

    defects = index.get("_candidate_defects")
    if not isinstance(defects, list):
        return _all_blocking_scoped_receipt(
            base,
            reason="defect_ledger_missing",
            measurement_window=window,
        )
    if not _v3_base_receipt_is_reconciled(base, defects):
        return _all_blocking_scoped_receipt(
            base,
            reason="candidate_receipt_mismatch",
            measurement_window=window,
        )
    ledger_counts = {kind: 0 for kind in SCOPABLE_DEFECT_KINDS}
    ledger_valid = True
    for defect in defects:
        if not isinstance(defect, dict):
            ledger_valid = False
            continue
        kind = defect.get("kind")
        if kind not in ledger_counts:
            ledger_valid = False
            continue
        ledger_counts[kind] += 1
    if (
        not ledger_valid
        or base.get("defect_ledger_reconciled") is not True
        or any(
            ledger_counts[kind] != int(base.get(kind) or 0)
            for kind in SCOPABLE_DEFECT_KINDS
        )
    ):
        return _all_blocking_scoped_receipt(
            base,
            reason="defect_ledger_mismatch",
            measurement_window=window,
        )

    blocking = {kind: 0 for kind in SCOPABLE_DEFECT_KINDS}
    waived = {kind: 0 for kind in SCOPABLE_DEFECT_KINDS}
    uncertain = {kind: 0 for kind in SCOPABLE_DEFECT_KINDS}
    waived_defects: list[dict[str, str]] = []
    for defect in defects:
        kind = defect["kind"]
        defect_start = defect.get("start")
        defect_end = defect.get("end")
        if (
            not isinstance(defect_start, datetime)
            or defect_start.tzinfo is None
            or defect_start.utcoffset() is None
            or not isinstance(defect_end, datetime)
            or defect_end.tzinfo is None
            or defect_end.utcoffset() is None
        ):
            blocking[kind] += 1
            uncertain[kind] += 1
            continue
        start = min(defect_start, defect_end).astimezone(timezone.utc)
        end = max(defect_start, defect_end).astimezone(timezone.utc)
        if start <= window_end and end >= window_start:
            blocking[kind] += 1
        else:
            waived[kind] += 1
            waived_defects.append({
                "defect_id": defect["defect_id"],
                "kind": kind,
                "reason": "proven_disjoint_interval",
            })

    scoped = {
        kind: _defect_count_row(
            int(base.get(kind) or 0),
            blocking=blocking[kind],
            waived=waived[kind],
            timestamp_uncertain=uncertain[kind],
        )
        for kind in SCOPABLE_DEFECT_KINDS
    }
    out = dict(base)
    out.update({
        "complete": bool(
            base.get("global_complete") is True
            and not any(blocking.values())
        ),
        "scope_error": None,
        "measurement_window": window,
        "scoped_defects": scoped,
        "waived_defects": waived_defects,
        "waiver_reasons": {
            "proven_disjoint_interval": dict(waived),
        },
        "blocking_reasons": {
            "intersects_measurement_window": {
                kind: blocking[kind] - uncertain[kind]
                for kind in SCOPABLE_DEFECT_KINDS
            },
            "timestamp_uncertain": dict(uncertain),
        },
    })
    return _with_receipt_hash(out)


def attribute(
    gate_row: dict,
    index: dict,
    project: str | None = None,
    *,
    target_project_proof: dict | None = None,
    window_seconds: int | None = None,
) -> dict | None:
    """Attribute one session-less gate row by exact nonce, or return None.

    There is intentionally no hash fallback. Historical hash-recovered rows are
    frozen pilots and are consumed as pinned data rather than rejoined live.
    Identical duplicate S2 records coalesce; a nonce in two sessions or any
    non-identical duplicate returns an explicit conflict instead of silently
    selecting the first candidate.

    ``project`` is retained only as a legacy API signal.  Its lossy sanitized
    value is never compared.  Production callers supply an opaque proof (or put
    one in the index via ``target_project_path``); if they pass only the legacy
    string, attribution fails closed.  Calls with neither are the pre-contract
    unscoped pilot path.

    ``window_seconds=None`` preserves the original all-time completeness
    refusal. A nonnegative integer opts into the founder-ratified v3 receipt:
    the exact S1 timestamp defines a conservative closed nominal window, and
    only dated identity-conflict or missing-result intervals proven disjoint
    from that window are waived. Discovery and nonce conflict checks remain
    all-time in either mode.

    On success returns the exact session, transcript coordinate, and folded S2
    host-result metadata. A conflict can carry no session when multiple sessions
    are implicated, but remains identity-proven for audit accounting.
    """
    completeness = index.get("candidate_completeness")
    if window_seconds is not None:
        completeness = candidate_completeness_for_window(
            gate_row,
            index,
            window_seconds=window_seconds,
        )
    if not isinstance(completeness, dict) or completeness.get("complete") is not True:
        return None
    if target_project_proof is None:
        row_proof = gate_row.get("project_proof")
        target_project_proof = (
            row_proof if isinstance(row_proof, dict)
            else index.get("target_project_proof")
        )
    allow_legacy_unscoped = target_project_proof is None and project is None

    nonce = gate_row.get("gate_call_id")
    if not isinstance(nonce, str) or not nonce:
        return None
    all_nonce_hits = list((index.get("by_nonce") or {}).get(nonce) or [])
    if any(
        isinstance(candidate.get("host_observation"), dict)
        and (
            (
                candidate["host_observation"].get("session_id")
                and candidate["host_observation"].get("session_id")
                != candidate.get("session_id")
            )
            or (
                "session_mismatch"
                in candidate["host_observation"].get(
                    "embedded_conflict_reasons", ()
                )
            )
        )
        for candidate in all_nonce_hits
    ):
        return None
    conflicts = _candidate_set_conflicts(all_nonce_hits)
    nonce_hits, project_unresolved = _partition_by_project(
        all_nonce_hits,
        target_project_proof,
        allow_legacy_unscoped=allow_legacy_unscoped,
    )
    if not nonce_hits:
        return None
    if conflicts:
        sessions = {row.get("session_id") for row in all_nonce_hits}
        result = {
            "session_id": next(iter(sessions)) if len(sessions) == 1 else None,
            "transcript_path": (
                all_nonce_hits[0].get("transcript_path")
                if len(sessions) == 1 else None
            ),
            "source": "codex_transcript_nonce",
            "conflict": True,
            "conflict_reasons": conflicts,
            "candidate_completeness": completeness,
        }
        if target_project_proof is not None:
            result["project_check"] = project_proof.PROJECT_MATCH
        return result
    if project_unresolved:
        return None
    hit = min(nonce_hits, key=lambda row: row.get("source_order") or (0, 0))
    result = {
        "session_id": hit["session_id"],
        "transcript_path": hit["transcript_path"],
        "source": "codex_transcript_nonce",
        "host_observation": hit.get("host_observation"),
        "source_order": hit.get("source_order"),
        "project_proof": hit.get("project_proof"),
        "conflict": False,
        "candidate_completeness": completeness,
    }
    if target_project_proof is not None:
        result["project_check"] = project_proof.PROJECT_MATCH
    return result
