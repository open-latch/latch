"""Vault-local verbatim request-text store for gate calls.

The structural gate log is hash-only by design (fact id=3091): it carries
``query_hash`` and ``query_chars`` and never the prompt itself. That invariant
is unchanged and this module does not touch it — but it left V5 with no task
prompt to run an arm against, because a 12-char digest cannot be turned back
into the request (Latch 4982). The registered consumer contract (4676 A4, v5
amendment) asks for verbatim text keyed to the digest and length the gate
already stores, so that any candidate text is mechanically self-verifying:

    sha1(text)[:12] == query_hash  and  len(text) == query_chars

So the text gets its own home, deliberately outside the structural log:

* one JSONL record per gate call, in the vault directory beside ``gate.log``;
* mode 0600 under 0700 parents, and gitignored (``GITIGNORE_PATTERN``);
* joinable to its gate.log row by ``(query_hash, ts)`` — the two writes share
  one timestamp rather than stamping themselves independently — and exactly by
  ``gate_call_id``;
* capture is ON by default, because a window that only accumulates for
  operators who remember a flag is the failure 4982 already recorded once.
  ``CAPTURE_ENV`` is the opt-out.

Cleartext prompts are acceptable here on the same footing as the outcome-audit
lineage checkpoint: the artifact never leaves the vault (ruling 4562). Nothing
in this module is readable by the public-safe surface, and callers must keep it
that way — this is the one place raw request text is allowed to land.

That footing is weaker on Windows, and the weakness is inherited rather than
introduced: POSIX mode bits are not a permission model there, so the 0600 this
module requests is advisory and confidentiality rests on the vault directory's
ACLs. The lineage checkpoint has always had the same exposure. Whether Windows
warrants an enforced owner-only ACL, or is declared unsupported for capture, is
a founder call recorded against id=5241 — not something this module decides by
quietly writing nothing.

One property worth knowing before consuming the store: ``gate._query_hash``
folds a whitespace-only request to the empty string, so a blank request's
stored text will *not* reproduce its ``query_hash``. That is faithful, not a
bug — the text is recorded exactly as received rather than normalized to match
the digest — and A4(c)'s 15-word floor excludes such episodes anyway.

Every write is best-effort: a failure here must never change a verdict or cost
the caller its structural log row.
"""
from __future__ import annotations

import json
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import paths


# File basename prefix. Deliberately *not* the ``<stream>-<date>.log`` shape
# `log_utils` rotates: `maintain_log_retention` gzips aged `.log` files with the
# ambient umask, which would republish cleartext prompts as a world-readable
# archive. A distinct extension keeps this store out of that sweep entirely.
STREAM = "gate-request-text"
STORE_SUFFIX = ".jsonl"

# Mirrors the `**/outcome-lineage.json` entry that pins the other vault-local
# private artifact (ruling 4562). Asserted by the test suite, so the ignore rule
# and the emitted filename cannot drift apart.
GITIGNORE_PATTERN = "**/gate-request-text-*.jsonl"

# Default ON, disabled by any explicitly set value other than "1" — the same
# control shape as LATCH_OUTCOME_EVENTS (capture_streams.outcome_events_enabled),
# so operators learn one convention rather than two.
CAPTURE_ENV = "LATCH_REQUEST_TEXT_CAPTURE"


def capture_enabled() -> bool:
    """Return the call-time request-text capture policy."""
    raw = os.environ.get(CAPTURE_ENV)
    if raw is None:
        return True
    return raw.strip() == "1"


def store_path(
    project_path: str | os.PathLike | None = None,
    log_date: date | None = None,
) -> Path:
    """Return ``<vault>/gate-request-text-<YYYY-MM-DD>.jsonl``.

    Daily files match how the gate log is already partitioned, so a consumer
    joining the two only ever has to open one day at a time.
    """
    if log_date is None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        stamp = log_date.strftime("%Y-%m-%d")
    vault = (
        paths.project_dir(os.getcwd())
        if project_path is None
        else paths.project_dir(project_path)
    )
    return vault / f"{STREAM}-{stamp}{STORE_SUFFIX}"


def _mkdir_private(directory: Path) -> None:
    """Create ``directory`` and every missing ancestor at 0o700.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode to the leaf only,
    which can leave a world-readable directory above a private file. Existing
    directories are left alone — the vault's own permissions are the operator's
    to set, not this module's to rewrite.
    """
    missing: list[Path] = []
    probe = directory
    while not probe.exists():
        missing.append(probe)
        if probe == probe.parent:
            break
        probe = probe.parent
    for parent in reversed(missing):
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            continue


def _append_private(path: Path, line: str) -> None:
    """Append one line to a 0600 file, creating it privately if absent.

    The record is handed to a single ``write(2)`` rather than a buffered
    stream: under ``O_APPEND`` one write lands at the end of the file
    indivisibly, so two hosts gating at once cannot splice their records
    together. A record long enough to be split across writes is the one
    residual interleaving risk, and it degrades safely — a spliced line fails
    both the hash and the length check, so a consumer drops that episode
    instead of running an arm against corrupted text.
    """
    _mkdir_private(path.parent)
    # O_NONBLOCK so the open itself cannot hang: opening a FIFO write-only
    # blocks until a reader appears, and this runs inside the gate's response
    # path, where "logging must never break the caller" has to mean never
    # stalling it either — an exception is swallowed, an indefinite block is
    # not. On a regular file the flag is inert.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        # O_CREAT's mode applies only on creation and is masked by the umask,
        # so re-assert it on the descriptor already held rather than on the
        # name (no window for a swapped path). Unix-only; on Windows the
        # create mode is the whole story.
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
        _assert_private(descriptor)
        payload = (line + "\n").encode("utf-8")
        while payload:
            payload = payload[os.write(descriptor, payload):]
    finally:
        os.close(descriptor)


# Whether POSIX permission bits mean anything on this platform. On Windows
# they do not: CPython synthesizes st_mode from the read-only attribute alone
# (0o666 for a writable regular file, 0o444 for a read-only one) and chmod
# moves only that bit, so NO value of S_IMODE can express "owner only" and
# fchmod is absent entirely. Asserting 0600 there would buy no privacy and
# would refuse every write — see `_assert_private`.
_POSIX_MODE_SEMANTICS = os.name == "posix"


def _assert_private(descriptor: int) -> None:
    """Raise unless the open descriptor is a regular file only the owner can read.

    Asserting the mode is not the same as achieving it: `fchmod` can fail
    outright (unsupported filesystem, foreign ownership) and a store created
    before this rule existed may already be 0644. Setting the mode and writing
    regardless would put cleartext prompts in a world-readable file — so the
    mode is verified on the descriptor already held, and a write that cannot
    be made private is abandoned instead.

    Failing closed costs at most one episode's text, which a consumer then
    treats as ineligible (4676 A4(f)). Failing open costs the privacy posture
    the whole store rests on (4562). The missing record is detectable — its
    gate.log row still exists and carries the same query_hash.

    WINDOWS GAP, stated rather than hidden: where mode bits are not a
    permission model, this check verifies only that the target is a regular
    file, and the store's confidentiality rests on the vault directory's own
    ACLs — the same guarantee, and the same limitation, as the outcome-audit
    lineage checkpoint, which has shipped under ruling 4562 with exactly this
    posture. This function deliberately does not decide whether that is good
    enough on Windows; it declines to convert an unenforceable assertion into
    a silent, total loss of capture on a supported platform (id=5241).
    """
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise OSError("request-text store is not a regular file")
    if not _POSIX_MODE_SEMANTICS:
        return
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise OSError(
            "refusing to write request text to a non-private store "
            f"(mode {oct(stat.S_IMODE(info.st_mode))})"
        )


def record(
    *,
    request: str,
    query_hash: str,
    query_chars: int,
    ts: str,
    gate_call_id: str | None = None,
    project_path: str | os.PathLike | None = None,
    session_id: str | None = None,
    host_adapter: str | None = None,
    runtime_version: str | None = None,
    log_date: date | None = None,
) -> None:
    """Persist one verbatim request-text record. Best-effort, never raises.

    ``ts`` is the gate.log row's own timestamp, passed in rather than sampled
    here so ``(query_hash, ts)`` is an exact join key and not an approximate
    one. ``log_date`` likewise comes from the caller and must be derived from
    that same ``ts``: a call straddling UTC midnight would otherwise have each
    writer sample the date on its own and drop the pair into different daily
    files, breaking the one-day join this store exists to serve.

    ``request`` is written exactly as received — never trimmed, capped, or
    normalized, because the consumer verifies it against ``query_hash`` and
    ``query_chars`` and any edit fails that check by design.
    """
    try:
        if not capture_enabled():
            return
        row = {
            "ts": ts,
            "project": paths.sanitize_cwd(
                project_path if project_path is not None else os.getcwd()
            ),
            "session_id": session_id,
            "event_type": STREAM,
            "gate_call_id": gate_call_id,
            "query_hash": query_hash,
            "query_chars": query_chars,
            "host_adapter": host_adapter,
            "runtime_version": runtime_version,
            "request_text": request,
        }
        _append_private(
            store_path(project_path, log_date),
            json.dumps(row, ensure_ascii=False, default=str),
        )
    except Exception:
        pass


def read_day(
    log_date: date,
    project_path: str | os.PathLike | None = None,
) -> list[dict]:
    """Return one day's records, oldest first. Malformed lines are skipped.

    The offline consumer's read side, mirroring ``log_utils.read_log_range``'s
    best-effort posture: the writer swallows failures, so the reader tolerates
    a partial line rather than refusing the whole day.
    """
    path = store_path(project_path, log_date)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
