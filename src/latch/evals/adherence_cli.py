"""CLI for the S1a adherence harness (KB id=5134).

    bin/latch_bench.sh preflight
    bin/latch_bench.sh run --tier smoke
    bin/latch_bench.sh extract --out /outside/the/repo/scored.jsonl

(``bin/latch_bench.ps1`` on Windows.) Every subcommand that can touch a
database runs inside the tripwire from ``adherence_vault``: an open outside the
run's scratch root raises rather than being noticed afterwards in a digest.
"""
from __future__ import annotations
if __package__ in (None, ""):
    import sys, pathlib
    sys.path.insert(0, str(next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "src")))

import argparse
import json
import sys
from pathlib import Path

from latch.evals import adherence
from latch.evals import adherence_elicit
from latch.evals import adherence_extract
from latch.evals import adherence_vault
from latch.store import paths


SMOKE_FIXTURE = paths.KB_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


def _harness_commit() -> str | None:
    """Protocol §4.1 records which harness produced a run. Best-effort: a run
    from an exported tree is still valid, it just cannot name its commit."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(paths.KB_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def _resolve_live_vault(args: argparse.Namespace) -> Path | None:
    """The vault the guards watch.

    Explicit ``--live-vault`` wins, deliberately: pointing the guard at the
    production vault must not require pinning this process at it, because a
    process pinned at the live vault is one missed repin away from writing to
    it.
    """
    if getattr(args, "live_vault", None):
        return Path(args.live_vault).expanduser()
    return adherence_vault.live_vault_dir()


def _guard_snapshot(live: Path | None) -> dict:
    """Everything a run must leave untouched (charter item 5, gate ruling b)."""
    return {
        "live_vault_dir": str(live) if live else None,
        "live_vault": adherence_vault.vault_digest(live) if live else None,
        "install": adherence_vault.install_fingerprint(Path(paths.KB_ROOT)),
    }


def _guard_report(before: dict, after: dict) -> dict:
    live_deltas: list[str] = []
    if before.get("live_vault") and after.get("live_vault"):
        live_deltas = adherence_vault.digest_delta(
            before["live_vault"], after["live_vault"]
        )
    install_changed = before.get("install") != after.get("install")
    return {
        "live_vault_dir": before.get("live_vault_dir"),
        "live_vault_deltas": live_deltas,
        "live_vault_unchanged": not live_deltas,
        "install_fingerprint_unchanged": not install_changed,
        # A delta here is not automatically this harness's doing: the founder's
        # own sessions write to the same vault while a run is in flight. The
        # authoritative non-contamination evidence is `databases_opened`, which
        # is process-local and cannot be confounded.
        "interpretation": (
            "live-vault deltas are attributable to this run only if "
            "databases_opened lists a live-vault path"
        ),
    }


def cmd_preflight(args: argparse.Namespace) -> int:
    scratch_root = (
        Path(args.scratch_root) if args.scratch_root
        else adherence_vault.default_scratch_root()
    )
    try:
        free = adherence_vault.require_disk_headroom(
            scratch_root, minimum=int(args.min_free_gib * adherence_vault.GIB)
        )
    except adherence_vault.PreflightError as e:
        print(f"adherence: {e}", file=sys.stderr)
        return 2
    snapshot = _guard_snapshot(_resolve_live_vault(args))
    print(json.dumps({
        "scratch_root": str(scratch_root),
        "free_gib": round(free / adherence_vault.GIB, 2),
        "min_free_gib": args.min_free_gib,
        "live_vault_dir": snapshot["live_vault_dir"],
        "live_vault_tables": (
            {
                name: table.get("rows")
                for name, table in (snapshot["live_vault"] or {}).get("tables", {}).items()
            }
            if snapshot["live_vault"] else None
        ),
        "install_root": snapshot["install"]["install_root"],
    }, indent=2, sort_keys=True))
    return 0


SCORED_MIN_N = 5


def _enforce_scored_tier(
    args: argparse.Namespace, fixture_paths: list[Path], protocol: dict | None = None,
) -> list[str]:
    """Refuse a scored run that is not the pre-registered experiment.

    Review round 1 (id=5217 finding 3) found ``--tier scored`` was a label on
    the output and nothing else: its defaults were still the committed smoke
    fixture, scripted elicitation and n=1, so "the scored run" could be
    performed, reported and cited while being none of those things. The
    protocol's §9 parameters are conditions, so they are enforced here.
    """
    problems: list[str] = []
    if args.elicitor != "live":
        problems.append(
            "scored runs require --elicitor live; the scripted elicitor replays "
            "fixture text and measures nothing about latch"
        )
    if args.n < SCORED_MIN_N:
        problems.append(
            f"scored runs require -n >= {SCORED_MIN_N} (protocol §9, id=3789's "
            f"n>=5); got {args.n}"
        )
    if not args.fixture:
        problems.append(
            "scored runs require an explicit --fixture; there is deliberately no "
            "scored default"
        )
    if not args.output:
        problems.append(
            "scored runs require --output, and it must live outside the repo tree"
        )
    else:
        try:
            adherence_extract.validate_destination(Path(args.output))
        except adherence_extract.ExtractError as e:
            problems.append(str(e))
    if not args.model and not args.allow_host_resolved_model:
        problems.append(
            "scored runs require --model, or --allow-host-resolved-model to "
            "accept the declared §4.1 limitation explicitly"
        )
    protocol = protocol or adherence.protocol_record()
    if not protocol["matches_pin"]:
        problems.append(
            f"{protocol['path']} does not match its pinned SHA-256 "
            f"(found {protocol['sha256']}, pinned {protocol['pinned_sha256']}); "
            "a scored run measures the pre-registered protocol or nothing"
        )
    return problems


def _enforce_scored_corpus(
    args: argparse.Namespace, specs: list, fixture_paths: list
) -> list[str]:
    """Content-and-structure corpus check, run after the specs load."""
    return adherence.validate_scored_corpus(
        specs, fixture_paths, smoke_fixture=Path(SMOKE_FIXTURE),
    )


def _enforce_scored_contamination(args: argparse.Namespace, scratch_root: Path) -> list[str]:
    """One real canary call in the exact run configuration.

    A vault digest structurally cannot see prompt contamination (id=5302
    finding F6), so a scored run proves the child is clean before it collects
    any data — not afterwards, when the data would already be biased.
    """
    if args.skip_contamination_check:
        return [
            "contamination check was skipped; a scored run may not skip it"
        ]
    probe_root = Path(scratch_root) / "contamination-probe"
    probe_root.mkdir(parents=True, exist_ok=True)
    result = adherence_elicit.contamination_probe(
        backend=args.backend, workspace=probe_root,
        model=args.model, timeout_s=args.timeout_s,
    )
    if result["clean"]:
        return []
    return [f"contamination check failed: {r}" for r in result["reasons"]]


def cmd_run(args: argparse.Namespace) -> int:
    fixture_paths = [Path(p) for p in (args.fixture or [SMOKE_FIXTURE])]
    # Read once, before the run: what is checked is what gets recorded.
    protocol = adherence.protocol_record()
    if args.tier == "scored":
        problems = _enforce_scored_tier(args, fixture_paths, protocol)
        if problems:
            print("adherence: refusing this scored run:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2
    try:
        specs = adherence.load_probe_sets(fixture_paths)
    except adherence.AdherenceError as e:
        print(f"adherence: {e}", file=sys.stderr)
        return 2

    if args.tier == "scored":
        problems = _enforce_scored_corpus(args, specs, fixture_paths)
        if problems:
            print("adherence: refusing this scored run:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2

    scratch_root = (
        Path(args.scratch_root) if args.scratch_root
        else adherence_vault.default_scratch_root()
    )
    try:
        adherence_vault.require_disk_headroom(
            scratch_root, minimum=int(args.min_free_gib * adherence_vault.GIB)
        )
    except adherence_vault.PreflightError as e:
        print(f"adherence: {e}", file=sys.stderr)
        return 2

    if args.tier == "scored":
        problems = _enforce_scored_contamination(args, scratch_root)
        if problems:
            print("adherence: refusing this scored run:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 2

    if args.elicitor == "scripted":
        elicitor: object = adherence.ScriptedElicitor(specs)
    else:
        try:
            elicitor = adherence.LiveElicitor(
                backend=args.backend, model=args.model, timeout_s=args.timeout_s
            )
        except adherence.AdherenceError as e:
            print(f"adherence: {e}", file=sys.stderr)
            return 2

    live = _resolve_live_vault(args)
    before = _guard_snapshot(live)
    with adherence_vault.scratch_session(scratch_root=scratch_root, keep=args.keep) as session:
        with adherence_vault.db_open_tripwire(
            scratch_root=session, readonly_roots=((live,) if live else ())
        ) as opened:
            result = adherence.run(
                specs,
                elicitor=elicitor,
                scratch_root=session,
                n_per_cell=args.n,
                seed=args.seed,
            )
    after = _guard_snapshot(live)

    result["guards"] = {
        **_guard_report(before, after),
        "databases_opened": opened,
        "scratch_root": str(scratch_root),
        "disk_free_gib": round(
            adherence_vault.free_bytes(scratch_root) / adherence_vault.GIB, 2
        ),
    }
    result["fixtures"] = [str(p) for p in fixture_paths]
    result["tier"] = args.tier
    result["parameters"]["harness_commit"] = _harness_commit()
    result["parameters"]["protocol"] = protocol
    if args.tier == "smoke":
        result["claim_note"] = (
            "SMOKE TIER — proves the instrument runs and grades deterministically. "
            "It is not evidence about latch and no claim may cite it."
        )

    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    vault_dir = (
        Path(args.vault) if args.vault else adherence_vault.live_vault_dir()
    )
    if vault_dir is None:
        print("adherence: no vault configured; pass --vault", file=sys.stderr)
        return 2
    try:
        with adherence_vault.db_open_tripwire(
            # The extractor's whole job is reading the live vault, so it gets
            # the one bounded read-only exception charter item 3 allows.
            scratch_root=Path(args.out).expanduser().resolve().parent,
            readonly_roots=(vault_dir,),
        ):
            summary = adherence_extract.extract(
                vault_dir,
                Path(args.out),
                limit=args.limit,
                source=args.source,
            )
    except (adherence_extract.ExtractError, adherence_vault.ContaminationError) as e:
        print(f"adherence: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_contamination_check(args: argparse.Namespace) -> int:
    scratch_root = (
        Path(args.scratch_root) if args.scratch_root
        else adherence_vault.default_scratch_root()
    )
    probe_root = scratch_root / "contamination-probe"
    probe_root.mkdir(parents=True, exist_ok=True)
    result = adherence_elicit.contamination_probe(
        backend=args.backend, workspace=probe_root,
        model=args.model, timeout_s=args.timeout_s,
    )
    printable = {k: v for k, v in result.items() if k != "argv"}
    print(json.dumps(printable, indent=2, sort_keys=True))
    return 0 if result["clean"] else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="adherence",
        description="E3/E4 adherence harness — V7's instrument (KB id=5134)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--scratch-root", default=None)
    common.add_argument(
        "--live-vault",
        default=None,
        help="vault the contamination guards watch (never pinned, only watched)",
    )
    common.add_argument(
        "--min-free-gib",
        type=float,
        default=adherence_vault.MIN_FREE_BYTES / adherence_vault.GIB,
        help=(
            "disk headroom floor; the default is the charter's 10 GiB. Lower it "
            "only for tests that are exercising isolation rather than capacity."
        ),
    )

    p_pre = sub.add_parser("preflight", parents=[common], help="check run preconditions")
    p_pre.set_defaults(func=cmd_preflight)

    p_run = sub.add_parser("run", parents=[common], help="run E3/E4 over a tier")
    p_run.add_argument("--tier", choices=("smoke", "scored"), default="smoke")
    p_run.add_argument("--fixture", action="append", default=None)
    p_run.add_argument("--elicitor", choices=("scripted", "live"), default="scripted")
    p_run.add_argument("--backend", default="claude")
    p_run.add_argument("--model", default=None)
    p_run.add_argument("--timeout-s", type=int, default=120)
    p_run.add_argument(
        "--allow-host-resolved-model",
        action="store_true",
        help=(
            "accept protocol §4.1's declared limitation that the backend "
            "resolves its own model. Required for a scored run without --model."
        ),
    )
    p_run.add_argument("-n", type=int, default=1, help="runs per (probe x arm) cell")
    p_run.add_argument("--seed", type=int, default=0)
    p_run.add_argument(
        "--skip-contamination-check",
        action="store_true",
        help=(
            "skip the pre-run canary. Permitted for exploratory runs; a scored "
            "run refuses to start with it set."
        ),
    )
    p_run.add_argument("--keep", action="store_true", help="keep the scratch session")
    p_run.add_argument("--output", type=Path, default=None)
    p_run.set_defaults(func=cmd_run)

    p_con = sub.add_parser(
        "contamination-check", parents=[common],
        help="prove an elicitation child sees no ambient project context",
    )
    p_con.add_argument("--backend", default="claude")
    p_con.add_argument("--model", default=None)
    p_con.add_argument("--timeout-s", type=int, default=240)
    p_con.set_defaults(func=cmd_contamination_check)

    p_ext = sub.add_parser("extract", help="read-only mine of the rejected_path bank")
    p_ext.add_argument("--vault", default=None)
    p_ext.add_argument("--out", required=True, help="destination OUTSIDE the repo tree")
    p_ext.add_argument("--limit", type=int, default=None)
    p_ext.add_argument("--source", default=None, choices=("declared", "backfill"))
    p_ext.set_defaults(func=cmd_extract)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
