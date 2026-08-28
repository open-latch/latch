"""Private, atomic, fail-closed snapshots for the A2 predicate policy path.

The module deliberately has no SQLite, model, budget, gate, semantic-search,
or network dependency.  Projection happens outside this boundary.  A caller
passes a no-argument projector and one external freshness source; publication
checks that source immediately before and after projection.  Consumers then
recompute the same fingerprint every time they load a snapshot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Mapping, Sequence

import predicate


SNAPSHOT_VERSION = "predicate-policy-snapshot-v1"
PROJECTION_ENGINE = "predicate-policy-projection-v1"
_PUBLIC_SOURCE_ROOT = Path(__file__).resolve().parent.parent
_MAX_TOKEN_BYTES = 64 * 1024
_SAFE_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SAFE_REASON_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z][a-z0-9_]*)?$")


class SnapshotPublicationError(RuntimeError):
    """A new projection could not be published without stale ambiguity."""


@dataclass(frozen=True)
class LoadedPolicySnapshot:
    """Validated private policy state ready for pure predicate evaluation."""

    policy_domain_id: str
    digest: str
    freshness_token: str
    freshness_source: Mapping[str, object]
    binding_checks: tuple[predicate.CompiledCheck, ...]
    binding_rows: int
    binding_compiled: int
    advisory_rows: int
    uncompilable_rows: int
    advisory_reason_counts: Mapping[str, int]


@dataclass(frozen=True)
class SnapshotLoadResult:
    snapshot: LoadedPolicySnapshot | None
    reason_codes: tuple[str, ...]


def build_policy_snapshot(
    projection: object,
    *,
    freshness_source: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Compile a complete ``PolicyProjection`` into a deterministic document.

    Row dictionaries remain private.  The digest covers all normalized row
    fields (including fields added by future projector versions), compilation
    classification, authority classification, policy-domain identity, source
    freshness evidence, engine version, and snapshot contract version.
    """
    domain = _projection_field(projection, "policy_domain_id")
    if not _opaque_identifier(domain):
        raise ValueError("projection policy_domain_id must be non-empty text")
    assert isinstance(domain, str)
    projection_engine = _projection_field(projection, "engine")
    if projection_engine != PROJECTION_ENGINE:
        raise ValueError("projection engine is not supported by this snapshot contract")
    freshness_token = _projection_field(projection, "freshness_token")
    if not _opaque_identifier(freshness_token):
        raise ValueError("projection freshness_token must be non-empty text")
    assert isinstance(freshness_token, str)

    binding = _compiled_rows(
        _projection_field(projection, "binding_rows"),
        classification="binding",
        policy_domain_id=domain,
    )
    advisory = _compiled_rows(
        _projection_field(projection, "advisory_rows"),
        classification="advisory",
        policy_domain_id=domain,
    )
    reason_counts = _normalized_reason_counts(
        _projection_field(projection, "reason_counts")
    )
    source = (
        {"kind": "unbound"}
        if freshness_source is None
        else _normalize_json(freshness_source)
    )
    if not isinstance(source, dict):
        raise ValueError("freshness source must be an object")

    document: dict[str, object] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "engine": predicate.ENGINE,
        "projection_engine": projection_engine,
        "state": "ready",
        "policy_domain_id": domain,
        "freshness_token": freshness_token,
        "freshness_source": source,
        "binding_rows": binding,
        "advisory_rows": advisory,
        "reason_counts": reason_counts,
    }
    document["digest"] = _document_digest(document)
    return document


def publish_policy_snapshot(
    target_path: str | os.PathLike[str],
    *,
    policy_domain_id: str,
    projector: Callable[[], object],
    source_vault_path: str | os.PathLike[str] | None = None,
    freshness_token_path: str | os.PathLike[str] | None = None,
    max_attempts: int = 3,
) -> dict[str, object]:
    """Project and atomically publish one externally fresh private snapshot.

    ``source_vault_path`` fingerprints the DB and its ``-wal``/``-shm``
    sidecars by presence, size, and nanosecond modification time.  A token file
    is the explicit substitute for synthetic or in-memory projections.  The
    source is checked before and after the projector; a changing source is
    retried.  Exhaustion or compilation failure atomically replaces any older
    snapshot with an invalid marker, so an old pass/block never looks current.
    """
    target = _external_target(target_path)
    if not _opaque_identifier(policy_domain_id):
        raise ValueError("policy_domain_id must be non-empty text")
    if not callable(projector):
        raise TypeError("projector must be callable")
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    if (source_vault_path is None) == (freshness_token_path is None):
        raise ValueError(
            "provide exactly one of source_vault_path or freshness_token_path"
        )

    source = _freshness_source(
        source_vault_path=source_vault_path,
        freshness_token_path=freshness_token_path,
    )
    try:
        for _ in range(max_attempts):
            before = _source_fingerprint(source)
            projection = projector()
            projection_domain = _projection_field(projection, "policy_domain_id")
            if projection_domain != policy_domain_id:
                raise ValueError("projection policy domain does not match publication")
            after = _source_fingerprint(source)
            if before != after:
                continue
            source_with_fingerprint = dict(source)
            source_with_fingerprint["fingerprint"] = after
            document = build_policy_snapshot(
                projection, freshness_source=source_with_fingerprint
            )
            # The compiler can be non-trivial over a large bank.  Recheck after
            # it as well; changing evidence may never be published as current.
            if _source_fingerprint(source) != after:
                continue
            _atomic_private_json(target, document)
            return document
    except Exception:
        _best_effort_invalidate(target, policy_domain_id, "publication_failed")
        raise

    _best_effort_invalidate(target, policy_domain_id, "source_changed")
    raise SnapshotPublicationError(
        "freshness source changed during every snapshot publication attempt"
    )


def load_policy_snapshot(
    snapshot_path: str | os.PathLike[str],
    *,
    policy_domain_id: str,
    expected_freshness_token: str | None = None,
) -> SnapshotLoadResult:
    """Load, authenticate, freshness-check, and compile a private snapshot.

    Every failure is represented by aggregate-safe reason codes.  No exception
    text, row text, action text, private path, or freshness-source coordinate is
    returned to a caller that may serialize the result.
    """
    path = Path(snapshot_path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _load_failure("snapshot_missing")
    except OSError:
        return _load_failure("snapshot_unreadable")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _load_failure("snapshot_corrupt")
    if not isinstance(document, dict):
        return _load_failure("snapshot_corrupt")
    if document.get("snapshot_version") != SNAPSHOT_VERSION:
        return _load_failure("wrong_snapshot_version")
    if document.get("engine") != predicate.ENGINE:
        return _load_failure("wrong_predicate_engine")
    if document.get("projection_engine") != PROJECTION_ENGINE:
        return _load_failure("wrong_projection_engine")
    if document.get("state") != "ready":
        return _load_failure("snapshot_invalid")
    if document.get("policy_domain_id") != policy_domain_id:
        return _load_failure("wrong_policy_domain")
    supplied_digest = document.get("digest")
    if not isinstance(supplied_digest, str) or supplied_digest != _document_digest(
        document
    ):
        return _load_failure("snapshot_digest_mismatch")
    freshness_token = document.get("freshness_token")
    if not _opaque_identifier(freshness_token):
        return _load_failure("snapshot_corrupt")
    if (
        expected_freshness_token is not None
        and freshness_token != expected_freshness_token
    ):
        return _load_failure("freshness_token_mismatch")

    source = document.get("freshness_source")
    if not isinstance(source, dict):
        return _load_failure("snapshot_corrupt")
    recorded_fingerprint = source.get("fingerprint")
    if not isinstance(recorded_fingerprint, str):
        return _load_failure("freshness_unproven")
    try:
        current_fingerprint = _source_fingerprint(source)
    except FileNotFoundError:
        return _load_failure("source_missing")
    except (OSError, TypeError, ValueError):
        return _load_failure("freshness_unproven")
    if current_fingerprint != recorded_fingerprint:
        return _load_failure("source_changed")

    binding_records = document.get("binding_rows")
    advisory_records = document.get("advisory_rows")
    reason_counts = document.get("reason_counts")
    if (
        not isinstance(binding_records, list)
        or not isinstance(advisory_records, list)
        or not _valid_reason_counts(reason_counts)
    ):
        return _load_failure("snapshot_corrupt")

    binding_checks: list[predicate.CompiledCheck] = []
    uncompilable = 0
    try:
        for record in binding_records:
            check = _check_record(
                record,
                expected_classification="binding",
                policy_domain_id=policy_domain_id,
            )
            if check.compilable:
                assert isinstance(check, predicate.CompiledCheck)
                binding_checks.append(check)
            else:
                uncompilable += 1
        for record in advisory_records:
            check = _check_record(
                record,
                expected_classification="advisory",
                policy_domain_id=policy_domain_id,
            )
            if not check.compilable:
                uncompilable += 1
    except (AssertionError, KeyError, TypeError, ValueError):
        return _load_failure("snapshot_compiler_mismatch")

    safe_counts = {str(key): int(value) for key, value in reason_counts.items()}
    if uncompilable:
        safe_counts["uncompilable_predicate"] = max(
            safe_counts.get("uncompilable_predicate", 0), uncompilable
        )
    loaded = LoadedPolicySnapshot(
        policy_domain_id=policy_domain_id,
        digest=supplied_digest,
        freshness_token=str(freshness_token),
        freshness_source=source,
        binding_checks=tuple(binding_checks),
        binding_rows=len(binding_records),
        binding_compiled=len(binding_checks),
        advisory_rows=len(advisory_records),
        uncompilable_rows=uncompilable,
        advisory_reason_counts=dict(sorted(safe_counts.items())),
    )
    return SnapshotLoadResult(snapshot=loaded, reason_codes=())


def check_loaded_snapshot_freshness(
    snapshot: LoadedPolicySnapshot,
) -> tuple[str, ...]:
    """Recheck external evidence before every evaluation of a loaded object."""
    source = snapshot.freshness_source
    recorded = source.get("fingerprint")
    if not isinstance(recorded, str):
        return ("freshness_unproven",)
    try:
        current = _source_fingerprint(source)
    except FileNotFoundError:
        return ("source_missing",)
    except (OSError, TypeError, ValueError):
        return ("freshness_unproven",)
    return () if current == recorded else ("source_changed",)


def _projection_field(projection: object, field: str) -> object:
    if isinstance(projection, Mapping):
        if field not in projection:
            raise ValueError(f"projection is missing {field}")
        return projection[field]
    if not hasattr(projection, field):
        raise ValueError(f"projection is missing {field}")
    return getattr(projection, field)


def _compiled_rows(
    rows: object,
    *,
    classification: str,
    policy_domain_id: str,
) -> list[dict[str, object]]:
    if rows is None or isinstance(rows, (str, bytes, Mapping)):
        raise ValueError(f"{classification}_rows must be a sequence")
    if not isinstance(rows, Sequence):
        try:
            rows = tuple(rows)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{classification}_rows must be iterable") from exc
    records: list[dict[str, object]] = []
    for raw_row in rows:
        row = _object_mapping(raw_row)
        row_domain = row.get("policy_domain_id")
        valid_domain = row_domain == policy_domain_id or (
            classification == "advisory" and row_domain is None
        )
        if not valid_domain:
            raise ValueError(
                f"{classification} row has a mismatched policy_domain_id"
            )
        if row.get("classification") != classification:
            raise ValueError(f"{classification} row classification is inconsistent")
        check = predicate.compile_predicate(row)
        records.append(
            {
                "classification": classification,
                "row": row,
                "compiled": _check_shape(check),
            }
        )
    records.sort(
        key=lambda item: (
            _row_sort_id(item["row"]),
            _canonical_bytes(item),
        )
    )
    return records


def _object_mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        raw = dict(value)
    elif is_dataclass(value):
        raw = asdict(value)
    else:
        keys = getattr(value, "keys", None)
        if callable(keys):
            raw = {str(key): value[key] for key in keys()}
        elif hasattr(value, "__dict__"):
            raw = {
                key: item
                for key, item in vars(value).items()
                if not key.startswith("_")
            }
        else:
            raise ValueError("policy row must be mapping-shaped")
    normalized = _normalize_json(raw)
    if not isinstance(normalized, dict):
        raise ValueError("policy row must normalize to an object")
    return normalized


def _normalize_json(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("non-finite values are not allowed in policy snapshots")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("snapshot object keys must be text")
            normalized[key] = _normalize_json(item)
        return dict(sorted(normalized.items()))
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    raise ValueError(f"unsupported snapshot field type: {type(value).__name__}")


def _check_shape(check: predicate.PredicateCheck) -> dict[str, object]:
    shape: dict[str, object] = {
        "compilable": check.compilable,
        "prefix": check.prefix,
        "value": check.value,
    }
    if not check.compilable:
        shape["uncompilable_reason"] = check.uncompilable_reason
    return shape


def _check_record(
    record: object,
    *,
    expected_classification: str,
    policy_domain_id: str,
) -> predicate.PredicateCheck:
    if not isinstance(record, Mapping):
        raise ValueError("snapshot row record must be an object")
    if record.get("classification") != expected_classification:
        raise ValueError("snapshot row classification mismatch")
    row = record.get("row")
    compiled = record.get("compiled")
    if not isinstance(row, Mapping) or not isinstance(compiled, Mapping):
        raise ValueError("snapshot row record is incomplete")
    row_domain = row.get("policy_domain_id")
    if row_domain != policy_domain_id and not (
        expected_classification == "advisory" and row_domain is None
    ):
        raise ValueError("snapshot row policy domain mismatch")
    if row.get("classification") != expected_classification:
        raise ValueError("snapshot row classification mismatch")
    check = predicate.compile_predicate(row)
    if _check_shape(check) != dict(compiled):
        raise ValueError("snapshot compiler output does not match runtime")
    return check


def _row_sort_id(row: object) -> int:
    if not isinstance(row, Mapping):
        return -1
    value = row.get("rejected_path_id", row.get("id", -1))
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _normalized_reason_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("projection reason_counts must be an object")
    result: dict[str, int] = {}
    for raw_key, raw_count in value.items():
        if not isinstance(raw_key, str) or not _SAFE_REASON_CODE_RE.fullmatch(raw_key):
            raise ValueError("reason-count keys must be non-empty text")
        if isinstance(raw_count, bool):
            raise ValueError("reason counts must be non-negative integers")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("reason counts must be non-negative integers") from exc
        if count < 0:
            raise ValueError("reason counts must be non-negative integers")
        result[str(raw_key)] = count
    return dict(sorted(result.items()))


def _valid_reason_counts(value: object) -> bool:
    try:
        _normalized_reason_counts(value)
    except ValueError:
        return False
    return True


def _opaque_identifier(value: object) -> bool:
    return isinstance(value, str) and _SAFE_OPAQUE_ID_RE.fullmatch(value) is not None


def _document_digest(document: Mapping[str, object]) -> str:
    payload = {key: value for key, value in document.items() if key != "digest"}
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _freshness_source(
    *,
    source_vault_path: str | os.PathLike[str] | None,
    freshness_token_path: str | os.PathLike[str] | None,
) -> dict[str, object]:
    if source_vault_path is not None:
        return {
            "kind": "sqlite-fileset-v1",
            "path": str(Path(source_vault_path).expanduser().resolve(strict=True)),
        }
    assert freshness_token_path is not None
    return {
        "kind": "token-file-v1",
        "path": str(Path(freshness_token_path).expanduser().resolve(strict=True)),
    }


def _source_fingerprint(source: Mapping[str, object]) -> str:
    kind = source.get("kind")
    raw_path = source.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("freshness source has no path")
    path = Path(raw_path)
    if kind == "token-file-v1":
        before = _regular_lstat(path)
        if before.st_size > _MAX_TOKEN_BYTES:
            raise ValueError("freshness token is too large")
        payload = path.read_bytes()
        after = _regular_lstat(path)
        if _stat_shape(before) != _stat_shape(after):
            raise OSError("freshness token changed while reading")
        return hashlib.sha256(
            _canonical_bytes(
                {
                    "kind": kind,
                    "size": len(payload),
                    "content_sha256": hashlib.sha256(payload).hexdigest(),
                    "stat": _stat_shape(after),
                }
            )
        ).hexdigest()
    if kind == "sqlite-fileset-v1":
        entries = []
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{path}{suffix}")
            try:
                info = _regular_lstat(candidate)
            except FileNotFoundError:
                if not suffix:
                    raise
                entries.append({"suffix": suffix, "present": False})
            else:
                entries.append(
                    {
                        "suffix": suffix,
                        "present": True,
                        "stat": _stat_shape(info),
                    }
                )
        return hashlib.sha256(
            _canonical_bytes({"kind": kind, "entries": entries})
        ).hexdigest()
    raise ValueError("unknown freshness source kind")


def _regular_lstat(path: Path) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("freshness source must be a regular non-symlink file")
    return info


def _stat_shape(info: os.stat_result) -> dict[str, int]:
    return {
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "ctime_ns": int(info.st_ctime_ns),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def _external_target(path: str | os.PathLike[str]) -> Path:
    target = Path(path).expanduser()
    if not target.name:
        raise ValueError("snapshot target must name a file")
    parent = target.parent.resolve(strict=True)
    resolved = parent / target.name
    if _is_relative_to(resolved, _PUBLIC_SOURCE_ROOT):
        raise ValueError("policy snapshots must stay outside the public source tree")
    if resolved.exists() and resolved.is_symlink():
        raise ValueError("snapshot target must not be a symlink")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _atomic_private_json(path: Path, document: Mapping[str, object]) -> None:
    payload = _canonical_bytes(document) + b"\n"
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        # fdopen takes ownership.  Clear our numeric handle immediately so a
        # concurrent open cannot reuse that number and be closed by cleanup.
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            # Windows ACL inheritance is the platform privacy boundary.  The
            # file still receives Python's closest owner read/write mode.
            if os.name != "nt":
                raise
        _fsync_directory(path.parent)
    finally:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _best_effort_invalidate(path: Path, domain: str, reason_code: str) -> None:
    invalid: dict[str, object] = {
        "snapshot_version": SNAPSHOT_VERSION,
        "engine": predicate.ENGINE,
        "state": "invalid",
        "policy_domain_id": domain,
        "reason_code": reason_code,
    }
    invalid["digest"] = _document_digest(invalid)
    try:
        _atomic_private_json(path, invalid)
    except OSError:
        pass


def _load_failure(*reason_codes: str) -> SnapshotLoadResult:
    return SnapshotLoadResult(
        snapshot=None,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


__all__ = [
    "LoadedPolicySnapshot",
    "PROJECTION_ENGINE",
    "SNAPSHOT_VERSION",
    "SnapshotLoadResult",
    "SnapshotPublicationError",
    "build_policy_snapshot",
    "check_loaded_snapshot_freshness",
    "load_policy_snapshot",
    "publish_policy_snapshot",
]
