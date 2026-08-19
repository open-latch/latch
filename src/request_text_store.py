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
  ``CAPTURE_ENV`` is the per-process opt-out; a ``request_text_capture`` key in
  the vault ``runtime_settings.json`` is the durable one, honoured with the
  same precedence and fail-closed semantics as the outcome-events control so
  that an opt-out also reaches a long-lived shared MCP runtime (Latch 5300).

Cleartext prompts are acceptable here on the same footing as the outcome-audit
lineage checkpoint: the artifact never leaves the vault (ruling 4562). Nothing
in this module is readable by the public-safe surface, and callers must keep it
that way — this is the one place raw request text is allowed to land.

That footing is weaker on Windows, and the weakness is inherited rather than
introduced: POSIX mode bits are not a permission model there, so the 0600 this
module requests is advisory and confidentiality rests on the vault directory's
ACLs. The lineage checkpoint has always had the same exposure. The founder
ruled (Latch 5300 item 1, 2026-08-19): parity with the lineage checkpoint is
the accepted posture — capture stays alive on Windows, the limitation is
documented rather than implied away, and an enforced owner-only ACL covering
BOTH private artifacts is deliberately deferred and flagged for review after
this hardening pass. No claim here or in the README may be stronger than what
the code enforces.

One property worth knowing before consuming the store: ``gate._query_hash``
folds a whitespace-only request to the empty string, so a blank request's
stored text will *not* reproduce its ``query_hash``. That is faithful, not a
bug — the text is recorded exactly as received rather than normalized to match
the digest — and A4(c)'s 15-word floor excludes such episodes anyway.

Every write is best-effort: a failure here must never change a verdict or cost
the caller its structural log row. A REFUSED write is no longer silent, though:
it emits one structural, text-free row on ``SUPPRESSION_STREAM`` — a closed-set
reason code and the join keys the gate row already carries, never the store's
filesystem path, a prompt, or exception prose — so the absence is
self-reporting instead of only detectable by joining against gate.log. And the
store no longer grows without bound: ``maintain_retention`` applies the
structural logs' 30-day/1-year clock, with every artifact it produces created
owner-only and, where mode bits are a permission model, verified so before a
byte of compressed prompt text lands in it; on Windows the paragraph above
governs the archives exactly as it governs the store (Latch 5300).
"""
from __future__ import annotations

import gzip
import json
import os
import re
import stat
import uuid
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

import log_utils
import paths


# File basename prefix. Deliberately *not* the ``<stream>-<date>.log`` shape
# `log_utils` rotates: `maintain_log_retention` gzips aged `.log` files with the
# ambient umask, which would republish cleartext prompts as a world-readable
# archive. A distinct extension keeps this store out of that sweep entirely.
STREAM = "gate-request-text"
STORE_SUFFIX = ".jsonl"

# Mirrors the `**/outcome-lineage.json` entry that pins the other vault-local
# private artifact (ruling 4562). Asserted by the test suite, so the ignore rule
# and the emitted filename cannot drift apart. The trailing wildcard covers the
# retention sweep's `.jsonl.gz` archives and its in-flight `.gz.tmp` files too —
# compressing a gitignored cleartext file must never produce an unignored one.
GITIGNORE_PATTERN = "**/gate-request-text-*.jsonl*"

# Default ON, disabled by any explicitly set value other than "1" — the same
# control shape as LATCH_OUTCOME_EVENTS (capture_streams.outcome_events_enabled),
# so operators learn one convention rather than two.
CAPTURE_ENV = "LATCH_REQUEST_TEXT_CAPTURE"

# The durable, daemon-safe opt-out: a top-level key in the vault
# runtime_settings.json, exactly parallel to the "outcome_events" key. An env
# var only reaches the process it is set in; a long-lived shared MCP runtime
# serving many sessions never sees a shell's export, so a user who opted out
# there would still be captured by the daemon without this (Latch 5300 item 3).
SETTINGS_KEY = "request_text_capture"

_CAPTURE_SETTINGS_CACHE: dict[str, tuple[int, int, int, int, bool]] = {}

# Structural suppression signal (Latch 5300 item 4). A refused write emits one
# row on this stream: a closed-set reason, a count, and the (gate_call_id,
# query_hash, ts) join keys its gate.log row already carries — never a path,
# never a prompt, never exception prose. It rides its OWN daily stream rather
# than gate.log (whose schema is pinned as the correlator's input format) and
# rather than outcome_event (which would subject a privacy signal to the
# unrelated LATCH_OUTCOME_EVENTS opt-out and widen a versioned,
# measurement-adjacent schema mid-window).
SUPPRESSION_STREAM = "request_text_suppression"
SUPPRESSION_REASONS = (
    "non_regular_target",
    "non_private_mode",
    "open_failed",
    "write_failed",
    "unexpected_error",
)

# Daily store files, plain or already archived by maintain_retention.
_STORE_FILE_RE = re.compile(
    rf"^{re.escape(STREAM)}-(?P<date>\d{{4}}-\d{{2}}-\d{{2}})"
    rf"{re.escape(STORE_SUFFIX)}(?P<gz>\.gz)?$"
)

# In-flight compression temps: `<daily-name>.<pid>.<uuid>.gz.tmp`. Kept inside
# the gitignore pattern's coverage, and cleaned by the sweep once stale — a
# crash-orphaned temp holds the same prompts compressed and must not outlive
# the retention promise (nor jam a later run's compression of its day).
_STORE_TEMP_RE = re.compile(
    rf"^{re.escape(STREAM)}-\d{{4}}-\d{{2}}-\d{{2}}"
    rf"{re.escape(STORE_SUFFIX)}\.\d+\.[0-9a-f]{{32}}\.gz\.tmp$"
)

# A temp older than this is an orphan: a live compression holds its temp for
# milliseconds, so one day leaves a margin of ~seven orders of magnitude.
_TEMP_ORPHAN_AGE_S = 24 * 60 * 60


class _WriteRefused(OSError):
    """A store write refused for a reason worth reporting structurally."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def capture_enabled(project_path: str | os.PathLike | None = None) -> bool:
    """Return the call-time request-text capture policy.

    Same precedence and failure posture as
    ``capture_streams.outcome_events_enabled``: an explicitly set ``CAPTURE_ENV``
    wins absolutely; otherwise the vault ``runtime_settings.json`` decides, with
    a missing file meaning the default (ON) and anything suspicious — a symlink,
    a non-regular file, unreadable or invalid JSON, a key that is not exactly
    ``true`` — failing CLOSED, i.e. not capturing. For a cleartext prompt store
    the conservative failure is silence, not capture.
    """
    raw = os.environ.get(CAPTURE_ENV)
    if raw is not None:
        return raw.strip() == "1"
    try:
        settings_path = (
            paths.project_dir(project_path) / paths.VAULT_RUNTIME_SETTINGS_FILENAME
        )
        if settings_path.is_symlink():
            return False
        try:
            info = settings_path.stat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if not stat.S_ISREG(info.st_mode):
            return False
        cache_key = os.path.abspath(os.fspath(settings_path))
        signature = (
            info.st_mtime_ns,
            info.st_ctime_ns,
            info.st_ino,
            info.st_size,
        )
        cached = _CAPTURE_SETTINGS_CACHE.get(cache_key)
        if cached is not None and cached[:4] == signature:
            return cached[4]
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        enabled = (
            isinstance(data, dict)
            and data.get(SETTINGS_KEY, True) is True
        )
        _CAPTURE_SETTINGS_CACHE[cache_key] = (*signature, enabled)
        return enabled
    except Exception:
        return False


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
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _WriteRefused("open_failed", "store could not be opened") from exc
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
        try:
            while payload:
                payload = payload[os.write(descriptor, payload):]
        except OSError as exc:
            raise _WriteRefused("write_failed", "store write failed") from exc
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
        raise _WriteRefused(
            "non_regular_target", "request-text store is not a regular file"
        )
    if not _POSIX_MODE_SEMANTICS:
        return
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise _WriteRefused(
            "non_private_mode",
            "refusing to write request text to a non-private store "
            f"(mode {oct(stat.S_IMODE(info.st_mode))})",
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

    A deliberate opt-out is silence; a REFUSED write is not. The refusal path
    emits one structural row on ``SUPPRESSION_STREAM`` so a consumer can see
    both that an episode's text is missing and why, without joining absence
    against gate.log (Latch 5300 item 4).
    """
    try:
        if not capture_enabled(project_path):
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
        try:
            _append_private(
                store_path(project_path, log_date),
                json.dumps(row, ensure_ascii=False, default=str),
            )
        except Exception as failure:
            _emit_suppression(
                getattr(failure, "reason", "unexpected_error"),
                ts=ts,
                log_date=log_date,
                gate_call_id=gate_call_id,
                query_hash=query_hash,
                project_path=project_path,
                session_id=session_id,
            )
    except Exception:
        pass


def _emit_suppression(
    reason: str,
    *,
    ts: str,
    log_date: date | None,
    gate_call_id: str | None,
    query_hash: str,
    project_path: str | os.PathLike | None,
    session_id: str | None,
) -> None:
    """Emit one text-free suppression row. Best-effort, never raises.

    Carries the shared ``ts``/``log_date`` so the row lands beside — and joins
    exactly to — the gate.log row whose text went missing. Only a closed-set
    reason and the already-structural join keys: no store path, no prompt, no
    exception prose. (The emit helper's common header adds the same sanitized
    ``project`` field every stream row — including the paired gate row —
    already carries; nothing new is disclosed.)
    """
    try:
        log_utils.emit_event(
            SUPPRESSION_STREAM,
            {
                "reason": (
                    reason if reason in SUPPRESSION_REASONS else "unexpected_error"
                ),
                "count": 1,
                "gate_call_id": gate_call_id,
                "query_hash": query_hash,
            },
            project_path=project_path,
            session_id=session_id,
            ts=ts,
            log_date=log_date,
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
    a partial line rather than refusing the whole day. Days past hot retention
    are read transparently from the ``.jsonl.gz`` archive ``maintain_retention``
    produced, so expiring a day never breaks the documented consumer path.
    """
    path = store_path(project_path, log_date)
    archived = path.with_name(path.name + ".gz")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # The sweep may compress the day between an exists() probe and the
        # read, so fall through on absence rather than probing first. A
        # corrupt archive (truncation, foreign bytes) reads as empty — the
        # best-effort posture this docstring promises — hence the guards
        # beyond OSError, which gzip does not confine itself to.
        try:
            with gzip.open(archived, "rt", encoding="utf-8") as stream:
                raw = stream.read()
        except (OSError, EOFError, UnicodeDecodeError, zlib.error):
            return []
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


def maintain_retention(
    project_path: str | os.PathLike | None = None,
) -> dict:
    """Apply the structural logs' 30-day-hot / 1-year-warm clock to the store.

    Deliberately NOT ``log_utils.maintain_log_retention``: that sweep creates
    its gzip archives with the ambient umask, which would republish cleartext
    prompts as a world-readable file. This sweep writes each archive on a
    descriptor opened 0600 and verifies it private with the same check the
    live write path trusts (``_assert_private`` — a mode proof on POSIX, a
    regular-file proof where mode bits are not a permission model) before a
    byte of compressed prompt text lands in it. A file whose archive cannot
    pass that check is left untouched in place and counted as skipped; the
    plain file is removed only after its private archive has been fsynced and
    atomically renamed into position, so a crash leaves the original, or the
    original plus a complete private archive — never a partial or widened one.
    A crash-orphaned temp is cleaned by a later sweep once stale
    (``_TEMP_ORPHAN_AGE_S``), so compressed prompt copies cannot outlive the
    retention promise.

    Today's file is never touched, even if its name parses as a past date
    (clock-skew defence, same rule as the structural sweep). Idempotent.
    Returns a counts dict for the nightly-heal summary, which is where this is
    called from (Latch 5300 item 2).
    """
    result = {"gzipped": 0, "deleted": 0, "skipped": 0, "temps_cleaned": 0}
    vault = (
        paths.project_dir(os.getcwd())
        if project_path is None
        else paths.project_dir(project_path)
    )
    if not vault.is_dir():
        return result
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    for entry in sorted(vault.iterdir()):
        if not entry.is_file():
            continue
        match = _STORE_FILE_RE.match(entry.name)
        if not match:
            # Crash-orphaned compression temps carry the same prompts,
            # compressed. Clean them once stale; a FRESH temp is a concurrent
            # run's in-flight file and is left strictly alone.
            if _STORE_TEMP_RE.match(entry.name) and not entry.is_symlink():
                try:
                    if now.timestamp() - entry.stat().st_mtime > _TEMP_ORPHAN_AGE_S:
                        entry.unlink()
                        result["temps_cleaned"] += 1
                except OSError:
                    result["skipped"] += 1
            continue
        if entry.is_symlink():
            # A store-named symlink is not this module's file. Never compress
            # or delete through it.
            result["skipped"] += 1
            continue
        date_str = match.group("date")
        if date_str == today_str:
            continue
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            result["skipped"] += 1
            continue
        age_days = (now - file_date).days
        is_gz = match.group("gz") == ".gz"
        if not is_gz and age_days > log_utils.HOT_RETENTION_DAYS:
            try:
                _compress_private(entry)
                result["gzipped"] += 1
            except Exception:
                result["skipped"] += 1
        elif is_gz and age_days > log_utils.COLD_RETENTION_DAYS:
            try:
                entry.unlink()
                result["deleted"] += 1
            except Exception:
                result["skipped"] += 1
    return result


def _compress_private(entry: Path) -> None:
    """Gzip ``entry`` in place without ever widening its mode.

    The archive is created 0600 under a temporary name, re-asserted on the
    held descriptor (umask cannot ADD bits to 0600, but ``fchmod`` repairs a
    stray restrictive mask and ``_assert_private`` proves the result), filled,
    fsynced, and atomically renamed over the final ``.gz`` name. Only then is
    the plaintext original removed.
    """
    final = entry.with_name(entry.name + ".gz")
    # No leading dot (the gitignore pattern must keep covering the name) and a
    # uuid alongside the pid, so a crash-orphaned temp can never collide with —
    # and O_EXCL-jam — a later run in a pid-stable environment.
    temporary = entry.with_name(
        f"{entry.name}.{os.getpid()}.{uuid.uuid4().hex}.gz.tmp"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
        _assert_private(descriptor)
        with entry.open("rb") as source, os.fdopen(descriptor, "wb") as raw:
            descriptor = -1
            with gzip.GzipFile(fileobj=raw, mode="wb") as archive:
                while True:
                    chunk = source.read(1 << 16)
                    if not chunk:
                        break
                    archive.write(chunk)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary, final)
        entry.unlink()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
