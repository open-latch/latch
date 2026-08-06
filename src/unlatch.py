"""Root-local instruction overrides for Latch Unlatched mode.

Runtime routing is the authority for whether Latch may open a KB.  This module
only keeps the native Claude/Codex instruction surfaces honest: it adds one
managed override to ``CLAUDE.md`` and ``AGENTS.md`` at the explicit scope root.
It never walks upward, edits a sibling, or removes the normal Latch contract.

The small restore receipt lives in Latch's machine-local scope state (via
``project_config.unlatch_state_path``), not in the project.  Instruction files
are handled as bytes so an unlatch/latch round trip preserves UTF-8 BOMs, mixed
content, and CRLF exactly.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

import agents_md_sync
import claude_md_sync
import project_config
from paths import KB_ROOT


STATE_FORMAT = 1
STATE_KIND = "latch-root-instruction-override"
LEGACY_STATE_FILE = KB_ROOT / "UNLATCH_STATE.json"

BEGIN_MARK = (
    "<!-- BEGIN LATCH UNLATCHED OVERRIDE : managed region, "
    "run /latch to re-latch -->"
)
# Kept only so an older marker is diagnosed explicitly instead of overlooked.
LEGACY_BEGIN_MARK = (
    "<!-- BEGIN LATCH UNLATCHED OVERRIDE : managed region, "
    "run /unlatch to re-latch -->"
)
END_MARK = "<!-- END LATCH UNLATCHED OVERRIDE -->"
OVERRIDE_BODY = """\
# Latch is unlatched for this scope

Latch is UNLATCHED at this explicit root and throughout its descendants.
Ignore every Latch-managed instruction in this file or an ancestor while this
block is present. Do not search, gate, capture, compact, maintain, or otherwise
use a Latch KB here. Other project scopes and their KBs are unchanged.

Run `/latch` from this scope to re-enable its remembered Latch binding.
"""

_SURFACES = (("claude", "CLAUDE.md"), ("agents", "AGENTS.md"))
_UTF8_BOM = b"\xef\xbb\xbf"
_UNSUPPORTED_BOMS = (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")
_BEGIN_BYTES = BEGIN_MARK.encode("ascii")
_LEGACY_BEGIN_BYTES = LEGACY_BEGIN_MARK.encode("ascii")
_END_BYTES = END_MARK.encode("ascii")


def _error(message: str) -> project_config.ProjectConfigError:
    return project_config.ProjectConfigError(message)


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _b64(body: bytes) -> str:
    return base64.b64encode(body).decode("ascii")


def _unb64(value: Any, *, field: str, state_file: Path) -> bytes:
    if not isinstance(value, str):
        raise _error(f"instruction override state has invalid {field}: {state_file}")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise _error(
            f"instruction override state has invalid {field}: {state_file}"
        ) from exc


def _override_block(
    *, begin_mark: str = BEGIN_MARK, newline: str = "\n"
) -> str:
    """Render the owned block; retained as a small test/migration helper."""
    if newline not in ("\n", "\r\n", "\r"):
        raise ValueError("unsupported newline")
    lines = [begin_mark, *OVERRIDE_BODY.rstrip("\n").split("\n"), END_MARK, ""]
    return newline.join(lines)


def _override_bytes(newline: bytes) -> bytes:
    if newline not in (b"\n", b"\r\n", b"\r"):
        raise ValueError("unsupported newline")
    return _override_block(newline=newline.decode("ascii")).encode("utf-8")


def _newline_for(body: bytes) -> bytes:
    """Choose a style for only the new block; existing bytes stay untouched."""
    probe = body[len(_UTF8_BOM):] if body.startswith(_UTF8_BOM) else body
    crlf = probe.find(b"\r\n")
    lf = probe.find(b"\n")
    cr = probe.find(b"\r")
    candidates = [(offset, value) for offset, value in (
        (crlf, b"\r\n"), (lf, b"\n"), (cr, b"\r")
    ) if offset >= 0]
    if not candidates:
        return b"\n"
    # At a CRLF offset, prefer the two-byte sequence over its embedded LF/CR.
    first = min(offset for offset, _value in candidates)
    if crlf == first:
        return b"\r\n"
    if lf == first:
        return b"\n"
    return b"\r"


def _separator_for(body: bytes, newline: bytes) -> bytes:
    if not body or body == _UTF8_BOM:
        return b""
    if body.endswith((b"\n", b"\r")):
        return newline
    return newline + newline


def _marker_present(body: bytes) -> bool:
    return any(marker in body for marker in (
        _BEGIN_BYTES, _LEGACY_BEGIN_BYTES, _END_BYTES,
        b"<!-- BEGIN LATCH UNLATCHED OVERRIDE",
    ))


def _find_valid_override(body: bytes) -> tuple[int, bytes] | None:
    """Return the sole exact override or reject every partial/changed variant."""
    matches: list[tuple[int, bytes]] = []
    for newline in (b"\n", b"\r\n", b"\r"):
        block = _override_bytes(newline)
        cursor = 0
        while True:
            offset = body.find(block, cursor)
            if offset < 0:
                break
            matches.append((offset, block))
            cursor = offset + len(block)
    if not matches:
        if _marker_present(body):
            raise _error(
                "instruction file contains a partial, legacy, or tampered "
                "Latch unlatched override"
            )
        return None
    # An exact block plus any extra marker is also ambiguous/tampered.
    if (
        len(matches) != 1
        or body.count(_BEGIN_BYTES) != 1
        or body.count(_END_BYTES) != 1
        or _LEGACY_BEGIN_BYTES in body
    ):
        raise _error("instruction file contains multiple or tampered Latch overrides")
    return matches[0]


def _has_override(text: str) -> bool:
    return _find_valid_override(text.encode("utf-8")) is not None


def _strip_override(text: str) -> tuple[str, bool]:
    body = text.encode("utf-8")
    found = _find_valid_override(body)
    if found is None:
        return text, False
    offset, block = found
    newline = b"\r\n" if b"\r\n" in block else b"\r" if b"\r" in block else b"\n"
    restored, _separator = _strip_recovered(body, offset, block, newline)
    return restored.decode("utf-8"), True


def _legacy_norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _managed_mark_pairs(kind: str) -> list[tuple[str, str]]:
    """Managed-region markers needed only for verified legacy recovery."""
    if kind == "agents":
        pairs = [(agents_md_sync.BEGIN_MARK, agents_md_sync.END_MARK)]
        legacy_begin = getattr(agents_md_sync, "LEGACY_BEGIN_MARK", None)
        legacy_end = getattr(agents_md_sync, "LEGACY_END_MARK", None)
        if legacy_begin and legacy_end:
            pairs.append((legacy_begin, legacy_end))
        return pairs
    if kind == "claude":
        return [(claude_md_sync.BEGIN_MARK, claude_md_sync.END_MARK)]
    raise _error(f"legacy instruction receipt has an unsafe kind: {kind!r}")


def _restore_metadata(kind: str, path: Path) -> dict[str, str]:
    """Return the old receipt fields for tests and legacy-state validation."""
    norm = _legacy_norm(path.read_text(encoding="utf-8"))
    for begin, end in _managed_mark_pairs(kind):
        if begin not in norm or end not in norm:
            continue
        before, rest = norm.split(begin, 1)
        body, _after = rest.split(end, 1)
        return {
            "managed_block": begin + body + end,
            "restore_prefix": before.rstrip("\n"),
        }
    return {}


def _legacy_has_managed_region(kind: str, text: str) -> bool:
    norm = _legacy_norm(text)
    return any(begin in norm and end in norm for begin, end in _managed_mark_pairs(kind))


def _strip_legacy_override(text: str, path: Path) -> tuple[str, bool]:
    """Strip one exact legacy marker pair without accepting partial variants."""
    norm = _legacy_norm(text)
    begins = norm.count(LEGACY_BEGIN_MARK)
    ends = norm.count(END_MARK)
    if not begins and not ends:
        return norm, False
    if begins != 1 or ends != 1 or BEGIN_MARK in norm:
        raise _error(f"legacy Latch override is partial or tampered in {path}")
    before, rest = norm.split(LEGACY_BEGIN_MARK, 1)
    if END_MARK not in rest:
        raise _error(f"legacy Latch override is partial or tampered in {path}")
    _owned, after = rest.split(END_MARK, 1)
    before = before.rstrip("\n")
    after = after.lstrip("\n")
    if before and after:
        return before + "\n\n" + after, True
    return before + after, True


def _legacy_restore_managed_block(
    *, kind: str, path: Path, text: str, record: dict[str, Any]
) -> str:
    if _legacy_has_managed_region(kind, text):
        return text
    block = record.get("managed_block")
    prefix = record.get("restore_prefix")
    if not isinstance(block, str) or not isinstance(prefix, str):
        raise _error(
            "legacy global Unlatch receipt cannot prove the managed instruction "
            f"block for {path}; restore it manually before clearing UNLATCHED"
        )
    if not any(
        block.count(begin) == 1 and block.count(end) == 1
        for begin, end in _managed_mark_pairs(kind)
    ):
        raise _error(f"legacy managed instruction block is invalid for {path}")

    content = _legacy_norm(text)
    block = block.rstrip("\n")
    if prefix:
        if content.startswith(prefix):
            remainder = content[len(prefix):].lstrip("\n")
            restored = prefix.rstrip("\n") + "\n\n" + block
            if remainder:
                restored += "\n\n" + remainder.rstrip("\n")
        else:
            restored = content.rstrip("\n") + "\n\n" + block
    else:
        remainder = content.lstrip("\n")
        restored = block
        if remainder:
            restored += "\n\n" + remainder.rstrip("\n")
    return restored.rstrip("\n") + "\n"


def _plain_metadata(path: Path) -> os.stat_result | None:
    """Return a safe regular-file identity, rejecting links before mutation."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _error(f"could not inspect instruction state file {path}: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise _error(f"refusing linked or non-regular file: {path}")
    return metadata


def _read_regular(
    path: Path, *, required: bool = False
) -> tuple[bytes, int, os.stat_result] | None:
    """Read one stable, unlinked regular file without following a symlink."""
    before = _plain_metadata(path)
    if before is None:
        if required:
            raise _error(f"required instruction file is missing: {path}")
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _error(f"could not safely open instruction file {path}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or identity != (current.st_dev, current.st_ino)
            or identity != (before.st_dev, before.st_ino)
        ):
            raise _error(f"instruction file identity changed or is linked: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = path.lstat()
        if (
            after.st_nlink != 1
            or (after.st_dev, after.st_ino) != identity
        ):
            raise _error(f"instruction file identity changed while reading: {path}")
        return b"".join(chunks), stat.S_IMODE(opened.st_mode), opened
    finally:
        os.close(fd)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_bytes(
    path: Path,
    body: bytes,
    *,
    mode: int,
    expected: os.stat_result | None,
) -> None:
    """Replace only the exact identity preflighted by the caller."""
    current = _plain_metadata(path)
    if expected is None:
        if current is not None:
            raise _error(f"instruction file appeared during transition: {path}")
    elif current is None or (
        current.st_dev,
        current.st_ino,
    ) != (expected.st_dev, expected.st_ino):
        raise _error(f"instruction file identity changed during transition: {path}")

    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            try:
                os.chmod(temp, mode)
            except OSError:
                pass
            os.fsync(handle.fileno())
        # Close the race between the first identity check and os.replace.
        again = _plain_metadata(path)
        if expected is None:
            if again is not None:
                raise _error(f"instruction file appeared during transition: {path}")
        elif again is None or (
            again.st_dev,
            again.st_ino,
            again.st_nlink,
        ) != (expected.st_dev, expected.st_ino, 1):
            raise _error(f"instruction file identity changed during transition: {path}")
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _unlink_exact(path: Path, expected_body: bytes) -> None:
    loaded = _read_regular(path, required=True)
    assert loaded is not None
    body, _mode, before = loaded
    if body != expected_body:
        raise _error(f"instruction file changed before removal: {path}")
    current = path.lstat()
    if (
        current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise _error(f"instruction file identity changed before removal: {path}")
    path.unlink()
    _fsync_directory(path.parent)


def _project_root(project: Path) -> Path:
    root = Path(project_config.project_root(project)).expanduser()
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise _error(f"project scope root does not resolve: {root}: {exc}") from exc
    if not root.is_dir() or root == Path(root.anchor):
        raise _error(f"unsafe project scope root: {root}")
    return root


def _state_file(root: Path, *, legacy_state: bool = False) -> Path:
    # ``legacy_state`` remains accepted for old wrappers, but new transitions
    # always use the scope's machine-local receipt.
    del legacy_state
    path = Path(project_config.unlatch_state_path(root)).expanduser()
    if not path.is_absolute():
        raise _error(f"instruction override state path must be absolute: {path}")
    return path


def _ensure_state_parent(root: Path, state_file: Path) -> None:
    ensure = getattr(project_config, "ensure_state_dir", None)
    if ensure is not None:
        ensure(root)
    parent = state_file.parent
    if not parent.exists() and not parent.is_symlink():
        parent.mkdir(parents=True, mode=0o700)
    if parent.is_symlink() or not parent.is_dir():
        raise _error(f"instruction override state directory is unsafe: {parent}")


def _state_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format": STATE_FORMAT,
        "kind": STATE_KIND,
        "instruction_files": records,
    }


def _record(
    *,
    kind: str,
    name: str,
    created: bool,
    mode: int,
    original: bytes,
    masked: bytes,
    separator: bytes,
    block: bytes,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": name,
        "created": created,
        "mode": mode,
        "original_sha256": _sha256(original),
        "masked_sha256": _sha256(masked),
        "separator_b64": _b64(separator),
        "block_b64": _b64(block),
    }


def _valid_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_state(payload: Any, state_file: Path) -> list[dict[str, Any]]:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"format", "kind", "instruction_files"}
        or payload.get("format") != STATE_FORMAT
        or payload.get("kind") != STATE_KIND
        or not isinstance(payload.get("instruction_files"), list)
    ):
        raise _error(f"instruction override state is malformed: {state_file}")
    raw_records = payload["instruction_files"]
    if len(raw_records) != len(_SURFACES):
        raise _error(f"instruction override state is incomplete: {state_file}")
    expected = {name: kind for kind, name in _SURFACES}
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    required = {
        "kind", "path", "created", "mode", "original_sha256",
        "masked_sha256", "separator_b64", "block_b64",
    }
    for raw in raw_records:
        if not isinstance(raw, dict) or set(raw) != required:
            raise _error(f"instruction override state has an invalid record: {state_file}")
        name = raw.get("path")
        kind = raw.get("kind")
        if name not in expected or kind != expected.get(name) or name in seen:
            raise _error(f"instruction override state targets an unsafe path: {state_file}")
        created = raw.get("created")
        mode = raw.get("mode")
        if type(created) is not bool or type(mode) is not int or not 0 <= mode <= 0o7777:
            raise _error(f"instruction override state has invalid metadata: {state_file}")
        if not _valid_hash(raw.get("original_sha256")) or not _valid_hash(
            raw.get("masked_sha256")
        ):
            raise _error(f"instruction override state has invalid hashes: {state_file}")
        separator = _unb64(raw.get("separator_b64"), field="separator", state_file=state_file)
        block = _unb64(raw.get("block_b64"), field="block", state_file=state_file)
        valid_blocks = {_override_bytes(value) for value in (b"\n", b"\r\n", b"\r")}
        if block not in valid_blocks:
            raise _error(f"instruction override state contains a tampered block: {state_file}")
        newline = b"\r\n" if b"\r\n" in block else b"\r" if b"\r" in block else b"\n"
        if separator not in (b"", newline, newline + newline):
            raise _error(f"instruction override state has an invalid separator: {state_file}")
        if created and (
            raw["original_sha256"] != _sha256(b"")
            or separator
        ):
            raise _error(f"instruction override state has invalid created-file metadata: {state_file}")
        seen.add(name)
        records.append({**raw, "separator": separator, "block": block})
    return records


def _read_state(state_file: Path) -> tuple[dict[str, Any] | None, int | None]:
    loaded = _read_regular(state_file)
    if loaded is None:
        return None, None
    body, mode, _metadata = loaded
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _error(
            f"instruction override state is unreadable or malformed: {state_file}: {exc}"
        ) from exc
    _validate_state(payload, state_file)
    return payload, mode


def _write_state(root: Path, state_file: Path, payload: dict[str, Any]) -> None:
    _ensure_state_parent(root, state_file)
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if _plain_metadata(state_file) is not None:
        raise _error(f"instruction override state appeared during transition: {state_file}")
    _atomic_bytes(state_file, body, mode=0o600, expected=None)


def _remove_state(state_file: Path) -> None:
    loaded = _read_regular(state_file, required=True)
    assert loaded is not None
    _body, _mode, before = loaded
    current = state_file.lstat()
    if (
        current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise _error(f"instruction override state changed before removal: {state_file}")
    state_file.unlink()
    _fsync_directory(state_file.parent)


def _reject_unsupported_encoding(path: Path, body: bytes) -> None:
    if any(body.startswith(mark) for mark in _UNSUPPORTED_BOMS) or b"\x00" in body:
        raise _error(f"instruction file is not UTF-8 text: {path}")


def _strip_recovered(
    body: bytes,
    offset: int,
    block: bytes,
    newline: bytes,
) -> tuple[bytes, bytes]:
    """Safely remove a valid orphan block when its machine receipt was lost."""
    prefix = body[:offset]
    suffix = body[offset + len(block):]
    # Without a receipt one separator newline is the only lossless conservative
    # assumption. A no-newline original may retain one trailing newline.
    separator = newline if prefix.endswith(newline) else b""
    base = prefix[:-len(separator)] if separator else prefix
    if suffix and base and not base.endswith((b"\n", b"\r")) and not suffix.startswith((b"\n", b"\r")):
        base += newline
    return base + suffix, separator


def _strip_recorded(body: bytes, record: dict[str, Any], path: Path) -> bytes:
    found = _find_valid_override(body)
    if found is None:
        if _sha256(body) == record["original_sha256"]:
            return body  # already restored during an interrupted enable
        raise _error(f"Latch override is missing or tampered in {path}")
    offset, actual = found
    block = record["block"]
    separator = record["separator"]
    if actual != block:
        raise _error(f"Latch override does not match its machine receipt in {path}")
    prefix = body[:offset]
    if separator and not prefix.endswith(separator):
        raise _error(f"Latch override separator was tampered in {path}")
    base = prefix[:-len(separator)] if separator else prefix
    suffix = body[offset + len(block):]
    newline = b"\r\n" if b"\r\n" in block else b"\r" if b"\r" in block else b"\n"
    if suffix and base and not base.endswith((b"\n", b"\r")) and not suffix.startswith((b"\n", b"\r")):
        base += newline
    restored = base + suffix
    if _sha256(body) == record["masked_sha256"] and _sha256(restored) != record[
        "original_sha256"
    ]:
        raise _error(f"instruction override receipt is inconsistent for {path}")
    return restored


def _disable_records(root: Path, state_file: Path) -> tuple[list[dict[str, Any]], bool]:
    payload, _mode = _read_state(state_file)
    if payload is not None:
        return _validate_state(payload, state_file), False

    records: list[dict[str, Any]] = []
    for kind, name in _SURFACES:
        path = root / name
        loaded = _read_regular(path)
        if loaded is None:
            original = b""
            mode = 0o644
            created = True
            newline = b"\n"
            separator = b""
            block = _override_bytes(newline)
            masked = block
        else:
            body, mode, _metadata = loaded
            _reject_unsupported_encoding(path, body)
            found = _find_valid_override(body)
            created = False
            if found is not None:
                offset, block = found
                newline = b"\r\n" if b"\r\n" in block else b"\r" if b"\r" in block else b"\n"
                original, separator = _strip_recovered(body, offset, block, newline)
                masked = body
            else:
                original = body
                newline = _newline_for(body)
                block = _override_bytes(newline)
                separator = _separator_for(body, newline)
                masked = body + separator + block
        records.append(_record(
            kind=kind,
            name=name,
            created=created,
            mode=mode,
            original=original,
            masked=masked,
            separator=separator,
            block=block,
        ))
    return _validate_state(_state_payload(records), state_file), True


def disable(project: Path, *, legacy_state: bool = False) -> list[str]:
    """Install root-local overrides without changing any other instruction file."""
    root = _project_root(Path(project))
    state_file = _state_file(root, legacy_state=legacy_state)
    records, is_new = _disable_records(root, state_file)
    messages: list[str] = []
    if is_new:
        serializable = [{key: value for key, value in record.items() if key not in {"block", "separator"}} for record in records]
        _write_state(root, state_file, _state_payload(serializable))
        messages.append(f"wrote machine-local instruction receipt {state_file}")

    # Validate both files before the first write. Existing exact blocks are
    # accepted; an interrupted prior disable can resume from original bytes.
    actions: list[tuple[Path, bytes, int, os.stat_result | None]] = []
    for record in records:
        path = root / record["path"]
        loaded = _read_regular(path)
        if loaded is None:
            metadata = None
            if not record["created"]:
                raise _error(f"instruction file disappeared during unlatch: {path}")
            current = b""
            current_mode = record["mode"]
        else:
            current, current_mode, metadata = loaded
            _reject_unsupported_encoding(path, current)
        found = _find_valid_override(current)
        if found is not None:
            if found[1] != record["block"]:
                raise _error(f"Latch override does not match its machine receipt in {path}")
            _strip_recorded(current, record, path)
            continue
        if _sha256(current) != record["original_sha256"]:
            raise _error(f"instruction file changed before its override was installed: {path}")
        masked = current + record["separator"] + record["block"]
        if _sha256(masked) != record["masked_sha256"]:
            raise _error(f"instruction override receipt is inconsistent for {path}")
        actions.append((path, masked, current_mode, metadata))

    for path, masked, mode, metadata in actions:
        _atomic_bytes(path, masked, mode=mode, expected=metadata)
        messages.append(f"added root-local unlatched override to {path}")
    return messages


def _legacy_ancestor_instruction_paths(project: Path) -> set[Path]:
    allowed: set[Path] = set()
    current = project
    while True:
        for _kind, name in _SURFACES:
            allowed.add(current / name)
        if current.parent == current:
            break
        current = current.parent
    return allowed


def _enable_legacy_global() -> list[str]:
    """Recover the old install-wide Unlatch receipt, or fail closed.

    Old releases recorded absolute ancestor instruction paths in one
    ``UNLATCH_STATE.json``.  The caller's current repository is not authority:
    only the project embedded in that receipt and its exact ancestor
    ``CLAUDE.md``/``AGENTS.md`` paths may be touched.
    """
    loaded = _read_regular(LEGACY_STATE_FILE)
    if loaded is None:
        raise _error(
            "legacy global UNLATCHED is active but UNLATCH_STATE.json is "
            "missing; refusing to clear it automatically. Restore any legacy "
            "instruction masks manually, then remove the global sentinel"
        )
    state_body, _state_mode, _state_metadata = loaded
    try:
        payload = json.loads(state_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _error(
            f"legacy global Unlatch receipt is unreadable: {LEGACY_STATE_FILE}"
        ) from exc
    if not isinstance(payload, dict):
        raise _error(f"legacy global Unlatch receipt is malformed: {LEGACY_STATE_FILE}")
    raw_project = payload.get("project")
    raw_records = payload.get("instruction_files")
    if not isinstance(raw_project, str) or not isinstance(raw_records, list):
        raise _error(f"legacy global Unlatch receipt is malformed: {LEGACY_STATE_FILE}")
    project = Path(raw_project).expanduser()
    if not project.is_absolute():
        raise _error("legacy global Unlatch receipt has a non-absolute project")
    try:
        project = project.resolve(strict=True)
    except OSError as exc:
        raise _error(
            "legacy global Unlatch receipt's recorded project no longer exists; "
            "restore its instruction masks manually before clearing UNLATCHED"
        ) from exc
    if not project.is_dir() or project == Path(project.anchor):
        raise _error("legacy global Unlatch receipt has an unsafe project root")
    allowed = _legacy_ancestor_instruction_paths(project)

    actions: list[tuple[Path, bytes, int, os.stat_result]] = []
    messages: list[str] = []
    seen: set[Path] = set()
    for raw_record in raw_records:
        if not isinstance(raw_record, dict):
            raise _error(f"legacy global Unlatch receipt is malformed: {LEGACY_STATE_FILE}")
        kind = raw_record.get("kind")
        raw_path = raw_record.get("path")
        had_managed = raw_record.get("had_managed_region")
        if kind not in {"claude", "agents"} or not isinstance(raw_path, str):
            raise _error(f"legacy global Unlatch receipt is malformed: {LEGACY_STATE_FILE}")
        if type(had_managed) is not bool:
            raise _error(f"legacy global Unlatch receipt is malformed: {LEGACY_STATE_FILE}")
        path = Path(raw_path).expanduser()
        if not path.is_absolute() or path != path.resolve(strict=False):
            raise _error(
                f"legacy instruction path escapes its project boundary: {path}"
            )
        expected_name = "CLAUDE.md" if kind == "claude" else "AGENTS.md"
        if path.name != expected_name or path not in allowed or path in seen:
            raise _error(
                f"legacy instruction path escapes its project boundary: {path}"
            )
        seen.add(path)
        file_loaded = _read_regular(path, required=True)
        assert file_loaded is not None
        body, mode, metadata = file_loaded
        _reject_unsupported_encoding(path, body)
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _error(f"legacy instruction file is not UTF-8 text: {path}") from exc
        restored, removed = _strip_legacy_override(text, path)
        if had_managed:
            restored = _legacy_restore_managed_block(
                kind=kind, path=path, text=restored, record=raw_record
            )
        restored_body = restored.encode("utf-8")
        if restored_body != body:
            actions.append((path, restored_body, mode, metadata))
            if removed:
                messages.append(f"removed legacy global unlatched override from {path}")
            if had_managed:
                messages.append(f"restored legacy managed instruction region in {path}")

    for path, body, mode, metadata in actions:
        _atomic_bytes(path, body, mode=mode, expected=metadata)
    _remove_state(LEGACY_STATE_FILE)
    messages.append(f"removed verified legacy receipt {LEGACY_STATE_FILE}")
    return messages


def enable(project: Path, *, legacy_state: bool = False) -> list[str]:
    """Remove only exact owned root overrides and preserve every other byte."""
    if legacy_state:
        return _enable_legacy_global()
    root = _project_root(Path(project))
    state_file = _state_file(root, legacy_state=legacy_state)
    payload, _state_mode = _read_state(state_file)
    messages: list[str] = []

    if payload is None:
        # Explicit relatch can recover a valid accidentally committed override,
        # but never guesses around a changed/partial managed block.
        recovery: list[tuple[Path, bytes, bytes, int, os.stat_result]] = []
        for _kind, name in _SURFACES:
            path = root / name
            loaded = _read_regular(path)
            if loaded is None:
                continue
            body, mode, metadata = loaded
            _reject_unsupported_encoding(path, body)
            found = _find_valid_override(body)
            if found is None:
                continue
            offset, block = found
            newline = b"\r\n" if b"\r\n" in block else b"\r" if b"\r" in block else b"\n"
            restored, _separator = _strip_recovered(body, offset, block, newline)
            recovery.append((path, body, restored, mode, metadata))
        for path, _body, restored, mode, metadata in recovery:
            _atomic_bytes(path, restored, mode=mode, expected=metadata)
            messages.append(f"removed recovered root-local override from {path}")
        return messages

    records = _validate_state(payload, state_file)
    actions: list[tuple[Path, bytes, bytes, int, os.stat_result, bool]] = []
    for record in records:
        path = root / record["path"]
        loaded = _read_regular(path)
        if loaded is None:
            if record["created"]:
                continue  # already deleted during an interrupted enable
            raise _error(f"instruction file recorded for restore is missing: {path}")
        body, mode, metadata = loaded
        _reject_unsupported_encoding(path, body)
        restored = _strip_recorded(body, record, path)
        if restored == body:
            continue
        delete_created = bool(record["created"] and not restored)
        actions.append((path, body, restored, mode, metadata, delete_created))

    # All blocks and paths are validated before any user file changes.
    for path, body, restored, mode, metadata, delete_created in actions:
        if delete_created:
            _unlink_exact(path, body)
            messages.append(f"removed Latch-created root instruction file {path}")
        else:
            _atomic_bytes(path, restored, mode=mode, expected=metadata)
            messages.append(f"removed root-local unlatched override from {path}")
    _remove_state(state_file)
    messages.append(f"removed machine-local instruction receipt {state_file}")
    return messages


def status(project: Path, *, legacy_state: bool = False) -> list[str]:
    """Report and validate the root-local instruction override state."""
    root = _project_root(Path(project))
    state_file = _state_file(root, legacy_state=legacy_state)
    payload, _state_mode = _read_state(state_file)
    records = _validate_state(payload, state_file) if payload is not None else []
    by_name = {record["path"]: record for record in records}
    messages: list[str] = []
    if payload is not None:
        messages.append(f"machine-local instruction receipt: {state_file}")
    for _kind, name in _SURFACES:
        path = root / name
        loaded = _read_regular(path)
        if loaded is None:
            record = by_name.get(name)
            if record is not None and not record["created"]:
                raise _error(f"instruction file recorded for restore is missing: {path}")
            continue
        body, _mode, _metadata = loaded
        _reject_unsupported_encoding(path, body)
        found = _find_valid_override(body)
        record = by_name.get(name)
        if record is None:
            if found is not None:
                messages.append(f"recoverable root-local override without receipt: {path}")
            continue
        if found is None:
            if _sha256(body) != record["original_sha256"]:
                raise _error(f"Latch override is missing or tampered in {path}")
            messages.append(f"root-local override pending or already restored: {path}")
        elif found[1] != record["block"]:
            raise _error(f"Latch override does not match its machine receipt in {path}")
        else:
            _strip_recorded(body, record, path)
            messages.append(f"root-local unlatched override present in {path}")
    return messages


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Manage root-local instruction overrides for Unlatched mode."
    )
    ap.add_argument("mode", choices=("off", "on", "status"))
    ap.add_argument("--project", default=".", help="explicit Latch scope root")
    ap.add_argument("--legacy-state", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    project = Path(args.project)
    try:
        if args.mode == "off":
            messages = disable(project, legacy_state=args.legacy_state)
        elif args.mode == "on":
            messages = enable(project, legacy_state=args.legacy_state)
        else:
            messages = status(project, legacy_state=args.legacy_state)
    except (project_config.ProjectConfigError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for message in messages:
        print(f"  {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
