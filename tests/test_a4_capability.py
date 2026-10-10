"""A4-C1 capability declaration and empirical-timeout acceptance contract."""
from __future__ import annotations

from dataclasses import replace
import importlib
from pathlib import Path
import sys

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"


def _module(name: str):
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module(f"latch.enforcement.{name}")
    finally:
        sys.path.remove(str(_SRC))


def _declaration(**updates):
    declaration = {
        "contract": "latch-capability-declaration-v1",
        "host_id": "codex",
        "minimum_host_version": "0.124.0",
        "hook_event": "pre_tool_use",
        "deny_mechanisms": ["permission-decision-deny", "exit-2"],
        "claimed_mode": "enforce",
        "tool_coverage": {
            "intercepted": ["local-side-effecting-tools"],
            "not_intercepted": ["hosted-tools"],
            "complete": True,
        },
        "timeout": {
            "basis": "empirical",
            "behavior": "fail-open",
            "receipt_on_timeout": False,
            "fixture_ids": ["codex-timeout-posix-v1"],
        },
        "platform_caveats": ["native-windows-pending"],
        "vocabulary": ["allow", "deny"],
        "corpus_fixture_ids": ["codex-pre-tool-use-corpus-v1"],
        "second_line_required": True,
    }
    declaration.update(updates)
    return declaration


def _runtime(capability, **updates):
    values = {
        "host_id": "codex",
        "host_version": "0.124.0",
        "platform": "posix",
        "legacy_feature_flags": (),
    }
    values.update(updates)
    return capability.RuntimeFacts(**values)


def _probe(timeout_harness, **updates):
    values = {
        "fixture_id": "codex-timeout-posix-v1",
        "host_id": "codex",
        "host_version": "0.124.0",
        "hook_event": "pre_tool_use",
        "platform": "posix",
        "timeout_seconds": 0.01,
    }
    values.update(updates)
    spec = timeout_harness.TimeoutProbeSpec(**values)
    return timeout_harness.run_timeout_probe(
        spec,
        lambda _spec: timeout_harness.TimeoutObservation(
            timed_out=True,
            action_continued=True,
            receipt_observed=False,
        ),
    )


def _evidence(capability, receipt, **updates):
    values = {
        "timeout_receipts": (receipt,),
        "corpus_fixture_ids": ("codex-pre-tool-use-corpus-v1",),
        "corpus_manifest_digest": "a" * 64,
        "corpus_candidate_count": 3,
        "corpus_complete": True,
        "second_line_available": True,
        "second_line_scope": "self-hosted",
    }
    values.update(updates)
    return capability.EnforcementEvidence(**values)


def test_declaration_schema_and_version_floor_gate():
    capability = _module("capability")
    timeout_harness = _module("timeout_harness")

    parsed = capability.parse_declaration(_declaration())
    assert parsed.to_json_object() == _declaration()

    receipt = _probe(timeout_harness)
    evidence = _evidence(capability, receipt)
    ready = capability.assess_capability(parsed, _runtime(capability), evidence)
    assert ready.effective_mode == "enforce"
    assert ready.enforce_ready is True
    assert ready.reason_codes == ()

    below_floor = capability.assess_capability(
        parsed,
        _runtime(capability, host_version="0.123.9"),
        evidence,
    )
    assert below_floor.effective_mode == "observe"
    assert below_floor.enforce_ready is False
    assert "host_version_below_minimum" in below_floor.reason_codes

    legacy_alias = capability.assess_capability(
        parsed,
        _runtime(capability, legacy_feature_flags=("features.codex_hooks",)),
        evidence,
    )
    assert legacy_alias.effective_mode == "observe"
    assert "legacy_codex_hooks_flag_present" in legacy_alias.reason_codes

    with pytest.raises(
        capability.CapabilityDeclarationError,
        match="Codex minimum_host_version",
    ):
        capability.parse_declaration(
            _declaration(minimum_host_version="0.123.0")
        )

    missing_coverage = _declaration()
    missing_coverage["tool_coverage"] = {
        "intercepted": ["local-side-effecting-tools"],
        "complete": True,
    }
    with pytest.raises(capability.CapabilityDeclarationError):
        capability.parse_declaration(missing_coverage)

    overlapping_coverage = _declaration()
    overlapping_coverage["tool_coverage"] = {
        "intercepted": ["synthetic.tool"],
        "not_intercepted": ["synthetic.tool"],
        "complete": True,
    }
    with pytest.raises(
        capability.CapabilityDeclarationError,
        match="both intercepted and not",
    ):
        capability.parse_declaration(overlapping_coverage)


def test_enforce_requires_empirical_timeout_fixtures_and_second_line():
    capability = _module("capability")
    timeout_harness = _module("timeout_harness")
    declaration = capability.parse_declaration(_declaration())
    runtime = _runtime(capability)
    receipt = _probe(timeout_harness)

    cases = (
        (
            _evidence(capability, receipt, timeout_receipts=()),
            "empirical_timeout_missing",
        ),
        (
            _evidence(capability, receipt, corpus_fixture_ids=()),
            "corpus_conformance_missing",
        ),
        (
            _evidence(capability, receipt, corpus_complete=False),
            "corpus_conformance_incomplete",
        ),
        (
            _evidence(capability, receipt, second_line_available=False),
            "second_line_missing",
        ),
        (
            _evidence(capability, receipt, second_line_scope="hosted"),
            "second_line_scope_unsupported",
        ),
        (
            _evidence(
                capability,
                replace(receipt, host_id="claude-code"),
            ),
            "empirical_timeout_invalid",
        ),
    )
    for evidence, expected_reason in cases:
        assessed = capability.assess_capability(declaration, runtime, evidence)
        assert assessed.effective_mode == "observe"
        assert assessed.enforce_ready is False
        assert expected_reason in assessed.reason_codes

    complete = capability.assess_capability(
        declaration,
        runtime,
        _evidence(capability, receipt),
    )
    assert complete.effective_mode == "enforce"
    assert complete.enforce_ready is True

    documented_only = capability.parse_declaration(
        _declaration(
            timeout={
                "basis": "documented",
                "behavior": "fail-open",
                "receipt_on_timeout": False,
                "fixture_ids": ["codex-timeout-posix-v1"],
            }
        )
    )
    assessed = capability.assess_capability(
        documented_only,
        runtime,
        _evidence(capability, receipt),
    )
    assert assessed.effective_mode == "observe"
    assert "empirical_timeout_not_declared" in assessed.reason_codes


def test_ask_is_never_load_bearing():
    capability = _module("capability")

    with pytest.raises(
        capability.CapabilityDeclarationError,
        match="vocabulary",
    ):
        capability.parse_declaration(
            _declaration(vocabulary=["allow", "ask", "deny"])
        )

    with pytest.raises(
        capability.CapabilityDeclarationError,
        match="deny_mechanisms",
    ):
        capability.parse_declaration(_declaration(deny_mechanisms=["ask"]))

    assert capability.canonical_hook_decision("allow") == "allow"
    assert capability.canonical_hook_decision("deny") == "deny"
    with pytest.raises(ValueError, match="allow or deny"):
        capability.canonical_hook_decision("ask")
