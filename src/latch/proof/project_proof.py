"""Opaque, vault-keyed proof that two observations name the same project.

Project paths are content.  They must be compared canonically for measurement,
but neither raw paths nor a reversible ``sanitize_cwd`` transform belongs in a
structural receipt.  This module keeps the canonical path in memory only and
emits a versioned HMAC fingerprint under a key derived from the selected
vault's immutable identity and the pinned measurement key epoch.

Changing the epoch is deliberately *not* treated as proof that a project is
foreign.  A proof from another epoch is ``key_epoch_mismatch``: an in-scope
loss signal under outcome-measurement contract v2.6 (B9).
"""
from __future__ import annotations

import hashlib
import hmac
import ntpath
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


PROJECT_PROOF_VERSION = "vault-hmac-sha256-v1"
PROJECT_PROOF_DOMAIN = b"latch/outcome-measurement/project-proof\x00"
PROJECT_KEY_ID_DOMAIN = b"latch/outcome-measurement/project-key-id\x00"

PROJECT_MATCH = "match"
PROJECT_FOREIGN = "foreign_project"
PROJECT_KEY_EPOCH_MISMATCH = "key_epoch_mismatch"
PROJECT_PROOF_MISSING = "project_proof_missing"
PROJECT_PROOF_INVALID = "project_proof_invalid"

_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_MINGW_RE = re.compile(r"^/([a-zA-Z])/(.*)$")
_URI_DRIVE_RE = re.compile(r"^/([a-zA-Z]:[\\/].*)$")
_WINDOWS_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_WINDOWS_UNC_RE = re.compile(r"^\\\\(?![?.]\\)[^\\]+\\[^\\]+")


def _windows_namespace_path(value: str) -> str:
    """Collapse replay-safe extended Windows names to their ordinary form."""

    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    extended_unc = "\\\\?\\unc\\"
    if folded.startswith(extended_unc):
        return "\\\\" + normalized[len(extended_unc):]
    extended = "\\\\?\\"
    if folded.startswith(extended):
        candidate = normalized[len(extended):]
        if _WINDOWS_ABS_RE.match(candidate):
            return candidate
    return normalized


def _path_component_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _canonical_existing_posix_spelling(value: str) -> str:
    """Recover on-disk spelling without collapsing a case-sensitive volume.

    ``realpath`` preserves caller-supplied case on case-insensitive APFS/HFS.
    Walking the already-existing path lets those aliases converge while a
    same-file check prevents case-fold collisions from merging distinct paths.
    Any lookup ambiguity or filesystem error keeps the resolved input spelling,
    which is the conservative comparison result.
    """

    if not os.path.exists(value):
        return value
    path = Path(value)
    if not path.is_absolute() or not path.anchor:
        return value

    current = path.anchor
    for component in path.parts[1:]:
        supplied = os.path.join(current, component)
        folded_component = _path_component_key(component)
        exact_match: str | None = None
        folded_matches: list[str] = []
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name == component:
                        exact_match = entry.name
                        break
                    if _path_component_key(entry.name) != folded_component:
                        continue
                    candidate = os.path.join(current, entry.name)
                    try:
                        if os.path.samefile(candidate, supplied):
                            folded_matches.append(entry.name)
                    except OSError:
                        continue
        except OSError:
            return value
        if exact_match is not None:
            current = os.path.join(current, exact_match)
        elif len(folded_matches) == 1:
            current = os.path.join(current, folded_matches[0])
        else:
            return value
    return current


def canonical_project_path(value: str | os.PathLike) -> str:
    """Return the comparison form for ``value``; callers must not persist it.

    Windows paths are normalized lexically even when a report is replayed on a
    POSIX host.  POSIX paths use realpath-style resolution so symlink aliases do
    not produce different project identities.
    """
    raw = str(value)
    mingw = _MINGW_RE.fullmatch(raw)
    if mingw:
        raw = f"{mingw.group(1)}:/{mingw.group(2)}"
    uri_drive = _URI_DRIVE_RE.fullmatch(raw)
    if uri_drive:
        raw = uri_drive.group(1)
    existing_posix_double_slash = (
        os.name != "nt" and raw.startswith("//") and os.path.exists(raw)
    )
    windows_path = _windows_namespace_path(raw)
    if (
        _WINDOWS_ABS_RE.match(windows_path)
        or (
            not existing_posix_double_slash
            and _WINDOWS_UNC_RE.match(windows_path)
        )
    ):
        if os.name == "nt" and os.path.exists(windows_path):
            try:
                windows_path = _windows_namespace_path(
                    str(Path(windows_path).resolve(strict=True))
                )
            except OSError:
                # Lexical normalization remains deterministic and does not
                # manufacture equality when the live filesystem is unavailable.
                pass
        normalized = ntpath.normcase(ntpath.normpath(windows_path))
        return "windows\x00" + normalized

    source = Path(raw).expanduser()
    try:
        normalized = str(source.resolve(strict=False))
    except OSError:
        normalized = os.path.abspath(str(source))
    normalized = os.path.normpath(normalized)
    return "posix\x00" + _canonical_existing_posix_spelling(normalized)


def _proof_payload(
    proof: Mapping[str, object] | None,
) -> tuple[str, str, str | None, str] | None:
    if not isinstance(proof, Mapping):
        return None
    version = proof.get("version")
    epoch = proof.get("key_epoch")
    key_id = proof.get("key_id")
    fingerprint = proof.get("fingerprint")
    if not all(isinstance(value, str) and value for value in (version, epoch, fingerprint)):
        return None
    if version != PROJECT_PROOF_VERSION or _HEX_64_RE.fullmatch(fingerprint) is None:
        return None
    if key_id is not None and (
        not isinstance(key_id, str) or _HEX_64_RE.fullmatch(key_id) is None
    ):
        return None
    return version, epoch, key_id, fingerprint


@dataclass(frozen=True)
class ProjectProofContext:
    """One vault + key-epoch context for making opaque project proofs."""

    key_epoch: str
    _key: bytes = field(repr=False)
    version: str = PROJECT_PROOF_VERSION
    key_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.version != PROJECT_PROOF_VERSION:
            raise ValueError(f"unsupported project proof version: {self.version}")
        if not isinstance(self.key_epoch, str) or not self.key_epoch.strip():
            raise ValueError("project proof key_epoch is required")
        if not isinstance(self._key, bytes) or len(self._key) < 32:
            raise ValueError("project proof key material must be at least 32 bytes")
        object.__setattr__(
            self,
            "key_id",
            hmac.new(self._key, PROJECT_KEY_ID_DOMAIN, hashlib.sha256).hexdigest(),
        )

    @classmethod
    def from_vault_key(
        cls,
        vault_key: bytes,
        *,
        key_epoch: str,
        vault_id: str = "",
    ) -> "ProjectProofContext":
        """Derive an epoch key from non-persisted vault key material."""
        if not isinstance(vault_key, bytes) or len(vault_key) < 32:
            raise ValueError("vault key material must be at least 32 bytes")
        epoch = str(key_epoch).strip()
        if not epoch:
            raise ValueError("project proof key_epoch is required")
        derivation = (
            PROJECT_PROOF_DOMAIN
            + PROJECT_PROOF_VERSION.encode("ascii")
            + b"\x00"
            + epoch.encode("utf-8")
            + b"\x00"
            + str(vault_id).encode("utf-8")
        )
        key = hmac.new(vault_key, derivation, hashlib.sha256).digest()
        return cls(key_epoch=epoch, _key=key)

    @classmethod
    def from_vault_identity(
        cls,
        identity: object,
        *,
        key_epoch: str,
    ) -> "ProjectProofContext":
        """Build from ``vault_identity.VaultIdentity`` without importing it.

        ``registry_fingerprint`` is immutable vault-local key material.  It is
        consumed in memory and never included in a proof or receipt.
        """
        fingerprint = getattr(identity, "registry_fingerprint", None)
        vault_uuid = getattr(identity, "vault_uuid", None)
        if not isinstance(fingerprint, str) or _HEX_64_RE.fullmatch(fingerprint) is None:
            raise ValueError("vault identity has no valid registry fingerprint")
        if not isinstance(vault_uuid, str) or not vault_uuid:
            raise ValueError("vault identity has no vault UUID")
        return cls.from_vault_key(
            bytes.fromhex(fingerprint),
            key_epoch=key_epoch,
            vault_id=vault_uuid,
        )

    def derive_subkey(self, domain: bytes) -> bytes:
        """Derive a domain-separated subkey from this epoch key.

        Lets callers authenticate their own artifacts under the vault identity
        without handling the epoch key itself, and keeps every such use in a
        separate domain from project proofs and key ids.
        """

        if not isinstance(domain, bytes) or not domain:
            raise ValueError("subkey domain must be non-empty bytes")
        return hmac.new(self._key, domain, hashlib.sha256).digest()

    def prove(self, project_path: str | os.PathLike) -> dict[str, str]:
        canonical = canonical_project_path(project_path)
        fingerprint = hmac.new(
            self._key,
            PROJECT_PROOF_DOMAIN + canonical.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "version": self.version,
            "key_epoch": self.key_epoch,
            "key_id": self.key_id,
            "fingerprint": fingerprint,
        }


def compare_project_proofs(
    candidate: Mapping[str, object] | None,
    target: Mapping[str, object] | None,
) -> str:
    """Compare proofs without ever converting uncertainty into foreignness."""
    if candidate is None or target is None:
        return PROJECT_PROOF_MISSING
    candidate_payload = _proof_payload(candidate)
    target_payload = _proof_payload(target)
    if candidate_payload is None or target_payload is None:
        return PROJECT_PROOF_INVALID
    (
        candidate_version,
        candidate_epoch,
        candidate_key_id,
        candidate_fingerprint,
    ) = candidate_payload
    target_version, target_epoch, target_key_id, target_fingerprint = target_payload
    if candidate_version != target_version:
        return PROJECT_PROOF_INVALID
    if candidate_epoch != target_epoch:
        return PROJECT_KEY_EPOCH_MISMATCH
    if candidate_key_id != target_key_id:
        return PROJECT_KEY_EPOCH_MISMATCH
    if hmac.compare_digest(candidate_fingerprint, target_fingerprint):
        return PROJECT_MATCH
    return PROJECT_FOREIGN
