"""Host-parameterized, content-free empirical timeout probe receipts."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Callable, Mapping


TIMEOUT_PROBE_CONTRACT = "latch-timeout-probe-v1"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TimeoutProbeSpec:
    fixture_id: str
    host_id: str
    host_version: str
    hook_event: str
    platform: str
    timeout_seconds: float


@dataclass(frozen=True)
class TimeoutObservation:
    timed_out: bool
    action_continued: bool
    receipt_observed: bool


@dataclass(frozen=True)
class TimeoutProbeReceipt:
    contract: str
    fixture_id: str
    host_id: str
    host_version: str
    hook_event: str
    platform: str
    timeout_seconds: float
    timed_out: bool
    action_continued: bool
    receipt_observed: bool
    observation_digest: str

    def to_json_object(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "fixture_id": self.fixture_id,
            "host_id": self.host_id,
            "host_version": self.host_version,
            "hook_event": self.hook_event,
            "platform": self.platform,
            "timeout_seconds": self.timeout_seconds,
            "timed_out": self.timed_out,
            "action_continued": self.action_continued,
            "receipt_observed": self.receipt_observed,
            "observation_digest": self.observation_digest,
        }


ProbeDriver = Callable[[TimeoutProbeSpec], TimeoutObservation | Mapping[str, object]]


def run_timeout_probe(
    spec: TimeoutProbeSpec,
    driver: ProbeDriver,
) -> TimeoutProbeReceipt:
    """Run one host driver and seal its structural timeout observation.

    C1 owns only the host-neutral harness.  A host adapter supplies the actual
    empirical driver in its separately chartered acceptance mission.
    """
    _validate_spec(spec)
    observed = driver(spec)
    if isinstance(observed, Mapping):
        if set(observed) != {
            "timed_out",
            "action_continued",
            "receipt_observed",
        }:
            raise ValueError("timeout observation has an invalid shape")
        observation = TimeoutObservation(
            timed_out=_strict_bool(observed["timed_out"], "timed_out"),
            action_continued=_strict_bool(
                observed["action_continued"], "action_continued"
            ),
            receipt_observed=_strict_bool(
                observed["receipt_observed"], "receipt_observed"
            ),
        )
    elif isinstance(observed, TimeoutObservation):
        observation = observed
        for field_name in (
            "timed_out",
            "action_continued",
            "receipt_observed",
        ):
            _strict_bool(getattr(observation, field_name), field_name)
    else:
        raise ValueError("timeout driver returned an invalid observation")

    payload = _receipt_payload(spec, observation)
    return TimeoutProbeReceipt(
        **payload,
        observation_digest=_digest(payload),
    )


def valid_timeout_receipt(receipt: object) -> bool:
    """Return whether a receipt is structurally valid and digest-consistent."""
    if not isinstance(receipt, TimeoutProbeReceipt):
        return False
    try:
        spec = TimeoutProbeSpec(
            fixture_id=receipt.fixture_id,
            host_id=receipt.host_id,
            host_version=receipt.host_version,
            hook_event=receipt.hook_event,
            platform=receipt.platform,
            timeout_seconds=receipt.timeout_seconds,
        )
        _validate_spec(spec)
        observation = TimeoutObservation(
            timed_out=_strict_bool(receipt.timed_out, "timed_out"),
            action_continued=_strict_bool(
                receipt.action_continued, "action_continued"
            ),
            receipt_observed=_strict_bool(
                receipt.receipt_observed, "receipt_observed"
            ),
        )
    except (TypeError, ValueError):
        return False
    payload = _receipt_payload(spec, observation)
    return (
        receipt.contract == TIMEOUT_PROBE_CONTRACT
        and isinstance(receipt.observation_digest, str)
        and _DIGEST_RE.fullmatch(receipt.observation_digest) is not None
        and receipt.observation_digest == _digest(payload)
    )


def _receipt_payload(
    spec: TimeoutProbeSpec,
    observation: TimeoutObservation,
) -> dict[str, object]:
    return {
        "contract": TIMEOUT_PROBE_CONTRACT,
        "fixture_id": spec.fixture_id,
        "host_id": spec.host_id,
        "host_version": spec.host_version,
        "hook_event": spec.hook_event,
        "platform": spec.platform,
        "timeout_seconds": spec.timeout_seconds,
        "timed_out": observation.timed_out,
        "action_continued": observation.action_continued,
        "receipt_observed": observation.receipt_observed,
    }


def _validate_spec(spec: TimeoutProbeSpec) -> None:
    for field_name in ("fixture_id", "host_id", "hook_event", "platform"):
        value = getattr(spec, field_name)
        if not isinstance(value, str) or _TOKEN_RE.fullmatch(value) is None:
            raise ValueError(f"{field_name} must be a bounded opaque token")
    if (
        not isinstance(spec.host_version, str)
        or _VERSION_RE.fullmatch(spec.host_version) is None
    ):
        raise ValueError("host_version must be a normalized numeric version")
    if (
        isinstance(spec.timeout_seconds, bool)
        or not isinstance(spec.timeout_seconds, (int, float))
        or not math.isfinite(float(spec.timeout_seconds))
        or float(spec.timeout_seconds) <= 0
    ):
        raise ValueError("timeout_seconds must be a positive finite number")


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be boolean")
    return value


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "TIMEOUT_PROBE_CONTRACT",
    "TimeoutObservation",
    "TimeoutProbeReceipt",
    "TimeoutProbeSpec",
    "run_timeout_probe",
    "valid_timeout_receipt",
]
