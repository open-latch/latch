"""Explicit offline entrypoint for one canonical outcome-measurement audit.

Consumes recorded evidence only: it cannot install, run a canary, choose T0,
or translate diagnostic ``gate_outcome`` rows into canonical receipts.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

from filelock import FileLock

sys.path.insert(0, str(Path(__file__).parent))

import outcome_measurement as om  # noqa: E402
import outcome_measurement_runner as runner  # noqa: E402
import paths  # noqa: E402


ENVELOPE_SCHEMA = "latch-outcome-audit-envelope-v1"


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _roots(value: Any, label: str) -> dict[str, tuple[str, ...]]:
    rows = _object(value, label)
    if set(rows) != set(om.SOURCES):
        raise ValueError(f"{label} must contain exactly {', '.join(om.SOURCES)}")
    if any(
        not isinstance(rows[source], list)
        or not rows[source]
        or any(
            not isinstance(item, str) or not item for item in rows[source]
        )
        for source in om.SOURCES
    ):
        raise ValueError(f"{label} roots must be non-empty string lists")
    return {source: tuple(rows[source]) for source in om.SOURCES}


def load_envelope(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load canonical dataclasses without accepting diagnostic aliases."""

    envelope_path = Path(path)
    try:
        payload = _object(
            json.loads(envelope_path.read_text(encoding="utf-8")),
            "audit envelope",
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("audit envelope is unavailable or invalid") from exc
    if payload.pop("schema", None) != ENVELOPE_SCHEMA:
        raise ValueError("audit envelope schema is unsupported")

    config_row = _object(payload.pop("config", None), "config")
    config_row["t0"] = _timestamp(config_row.get("t0"), "config.t0")
    config_row["cap"] = _timestamp(config_row.get("cap"), "config.cap")
    config_row["source_roots"] = _roots(
        config_row.get("source_roots"), "config.source_roots"
    )
    config = om.MeasurementConfig(**config_row)

    capture = om.CapturePin(
        **_object(payload.pop("capture", None), "capture")
    )
    manifest_row = _object(payload.pop("manifest", None), "manifest")
    manifest_row["ratification_node_ids"] = tuple(
        manifest_row.get("ratification_node_ids") or ()
    )
    manifest_row["source_roots"] = _roots(
        manifest_row.get("source_roots"), "manifest.source_roots"
    )
    manifest_row["fixture_hashes"] = _object(
        manifest_row.get("fixture_hashes"), "manifest.fixture_hashes"
    )
    manifest = om.MeasurementManifest(**manifest_row)

    raw_paths = _object(payload.pop("fixture_paths", None), "fixture_paths")
    if set(raw_paths) != set(manifest.fixture_hashes):
        raise ValueError("fixture_paths must match manifest fixture names exactly")
    fixture_paths: dict[str, Path] = {}
    for name, value in raw_paths.items():
        if not isinstance(value, str) or not value:
            raise ValueError("fixture paths must be non-empty strings")
        candidate = Path(value)
        fixture_paths[name] = (
            candidate if candidate.is_absolute() else envelope_path.parent / candidate
        )

    raw_canaries = payload.pop("canaries", None)
    if not isinstance(raw_canaries, list):
        raise ValueError("canaries must be a list")
    canaries = tuple(
        om.CanaryEvidence(**_object(row, "canary")) for row in raw_canaries
    )
    prior_capture_row = payload.pop("prior_capture", None)
    prior_capture = (
        om.CapturePin(**_object(prior_capture_row, "prior_capture"))
        if prior_capture_row is not None
        else None
    )
    if payload:
        raise ValueError(f"unknown audit envelope fields: {', '.join(sorted(payload))}")
    return {
        "source_roots": config.source_roots,
        "config": config,
        "capture": capture,
        "manifest": manifest,
        "fixture_paths": fixture_paths,
        "canaries": canaries,
        "prior_capture": prior_capture,
    }


OUTCOME_LINEAGE_FILENAME = "outcome-lineage.json"


def default_lineage_path(project: str | os.PathLike[str]) -> Path:
    """The checkpoint's sanctioned vault-local home (ruling 4562).

    The checkpoint persists raw source coordinates, so its default location
    is inside the project's vault directory — written 0o600 and gitignored —
    never an operator-invented path that travels.
    """

    return paths.project_dir(os.fspath(project)) / OUTCOME_LINEAGE_FILENAME


def _same_path(left: Path, right: Path) -> bool:
    if left.expanduser().resolve() == right.expanduser().resolve():
        return True
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        # One side does not exist yet; that is an ordinary first-run state and
        # genuinely means "not the same file".
        return False
    except OSError as exc:
        # Anything else (EACCES, ELOOP, ENOTDIR) leaves aliasing undecided.
        # Undecided must never read as "safe to write here".
        raise ValueError(
            f"cannot determine whether {left} and {right} are the same file"
        ) from exc


def _inside_root(path: Path, root: Path) -> bool:
    """Report containment without trusting byte-exact path spelling.

    ``resolve()`` does not case-fold, and APFS/NTFS are case-insensitive, so a
    lexical prefix compare accepts ``.../HOST/report.jsonl`` as outside
    ``.../host`` and lets an audit write its own report into the corpus it is
    measuring.  Comparing real directory identity via ``os.path.samefile``
    settles it on any filesystem: on a case-insensitive one the two spellings
    are the same directory, on a case-sensitive one they genuinely are not.
    """

    resolved_path = path.expanduser().resolve()
    resolved_root = root.expanduser().resolve()
    if resolved_path == resolved_root:
        return True
    if resolved_root.exists() and not resolved_root.is_dir():
        # A file-valued root contains exactly itself, and identity — not
        # spelling — decides: a case-variant or hardlinked output would be
        # committed over the very evidence this run just measured.
        return _same_path(resolved_path, resolved_root)
    try:
        resolved_path.relative_to(resolved_root)
        return True
    except ValueError:
        pass
    # The output usually does not exist yet, so walk up to the ancestors that do
    # and compare directory identity rather than spelling.
    for ancestor in resolved_path.parents:
        if _same_path(ancestor, resolved_root):
            return True
        if ancestor == ancestor.parent:
            break
    return False


def _validate_output_paths(
    *,
    project: str,
    envelope: Path,
    contract: Path,
    fixture_paths: Mapping[str, Path],
    source_roots: Mapping[str, tuple[str, ...]],
    lineage: Path,
    report: Path,
) -> None:
    outputs = {"lineage": lineage, "report": report}
    if _same_path(lineage, report):
        raise ValueError("lineage checkpoint and report paths must differ")
    inputs = {
        "envelope": envelope,
        "contract": contract,
        **{f"fixture:{name}": path for name, path in fixture_paths.items()},
    }
    database = paths.db_path(project)
    inputs.update({
        "project database": database,
        "project database wal": Path(str(database) + "-wal"),
        "project database shm": Path(str(database) + "-shm"),
    })
    sanctioned_lineage = default_lineage_path(project)
    for output_name, output in outputs.items():
        for input_name, input_path in inputs.items():
            if _same_path(output, input_path):
                raise ValueError(f"{output_name} path aliases {input_name}")
        if output_name == "report" and _same_path(output, sanctioned_lineage):
            # The checkpoint is lineage authority: a report aimed at it would
            # destroy the admission history it exists to carry.
            raise ValueError("report path aliases the vault lineage checkpoint")
        if output_name == "lineage" and (
            output.expanduser().resolve()
            == sanctioned_lineage.expanduser().resolve()
        ):
            # The sanctioned vault-local home (ruling 4562). The vault is
            # itself the measured S1 root, and this one inert name is never
            # discoverable as evidence, so exactly this spelling is exempt
            # from the refusal below. Identity (samefile) is deliberately not
            # used: a hardlink of the checkpoint under any other name —
            # including an evidence-shaped one — stays refused.
            continue
        for roots in source_roots.values():
            if any(_inside_root(output, Path(root)) for root in roots):
                raise ValueError(f"{output_name} path is inside a measured root")


def _run_locked(
    args: argparse.Namespace,
    loaded: dict[str, Any],
    fixture_paths: Mapping[str, Path],
) -> int:
    """Run one audit while the caller holds this checkpoint's writer lock."""

    manifest = loaded["manifest"]
    # Guard the object the flag actually governs. Keying this on the report path
    # both blocked the legitimate recovery from a half-committed run and left
    # the reset itself open, since the caller supplies both paths.
    if args.initialize_empty_lineage and args.lineage.exists():
        raise ValueError(
            "cannot initialize empty lineage when a lineage checkpoint exists"
        )
    coordinate = runner.lineage_checkpoint_coordinate(
        loaded["config"], manifest
    )
    mac_key = runner.lineage_checkpoint_mac_key(
        args.project, key_epoch=loaded["config"].key_epoch
    )
    prior = runner.load_lineage_checkpoint(
        args.lineage,
        coordinate_sha256=coordinate,
        mac_key=mac_key,
        contract_sha256=manifest.contract_sha256,
        allow_missing=args.initialize_empty_lineage,
    )
    result = runner.run_pinned_audit(
        project_path=args.project,
        contract_bytes=args.contract.read_bytes(),
        fixture_bytes={
            name: path.read_bytes() for name, path in fixture_paths.items()
        },
        prior_receipts=prior,
        **loaded,
    )
    report = json.loads(result.report)
    invalidated = report["oracles"]["invalidated"]
    if not isinstance(invalidated, bool):
        raise ValueError("canonical report invalidation state is malformed")
    runner.write_canonical_report(args.report, result.report)
    if not invalidated:
        try:
            runner.write_lineage_checkpoint(
                args.lineage,
                result.state,
                coordinate_sha256=coordinate,
                mac_key=mac_key,
                contract_sha256=manifest.contract_sha256,
            )
        except BaseException:
            # A committed report whose lineage never advanced would leave this
            # window unrunnable. Roll the pair back so the run is retryable
            # instead of requiring an operator to re-initialize lineage.
            try:
                args.report.unlink()
            except OSError:
                pass
            raise
    sys.stdout.write(
        json.dumps(
            {
                "ok": not invalidated,
                "report_kind": "canonical_outcome_audit",
                "measurement_protocol_version": (
                    result.state.config.measurement_protocol_version
                ),
                "implementation_commit": (
                    result.state.config.implementation_commit
                ),
                "invalidated": invalidated,
                "invalidation_reasons": report["oracles"][
                    "invalidation_reasons"
                ],
                "lineage_updated": not invalidated,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if not invalidated else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="latch-outcome-audit",
        description="Run one explicit, offline canonical outcome audit.",
    )
    parser.add_argument("--project", required=True)
    for name in ("envelope", "report"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument(
        "--lineage",
        type=Path,
        default=None,
        help=(
            "lineage checkpoint path; defaults to the private vault-local "
            f"{OUTCOME_LINEAGE_FILENAME} inside the project directory"
        ),
    )
    parser.add_argument(
        "--contract",
        type=Path,
        default=None,
        help=(
            "frozen contract bytes; defaults to the packaged artifact at "
            f"{runner.PACKAGED_CONTRACT_RELPATH}"
        ),
    )
    parser.add_argument(
        "--initialize-empty-lineage",
        action="store_true",
        help="allow a missing lineage checkpoint for an explicit first run",
    )
    args = parser.parse_args(argv)
    if args.contract is None:
        args.contract = runner.packaged_contract_path()
    if args.lineage is None:
        args.lineage = default_lineage_path(args.project)
    try:
        loaded = load_envelope(args.envelope)
        fixture_paths = loaded.pop("fixture_paths")
        _validate_output_paths(
            project=args.project,
            envelope=args.envelope,
            contract=args.contract,
            fixture_paths=fixture_paths,
            source_roots=loaded["source_roots"],
            lineage=args.lineage,
            report=args.report,
        )
        lock_path = str(args.lineage.expanduser().resolve()) + ".lock"
        with FileLock(lock_path, timeout=0):
            return _run_locked(args, loaded, fixture_paths)
    except (OSError, TypeError, ValueError, sqlite3.Error) as exc:
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "report_kind": "canonical_outcome_audit",
                    "error": str(exc),
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
