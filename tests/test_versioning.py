from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import versioning  # noqa: E402


def test_three_version_concepts_are_separate_and_valid():
    assert versioning.LATCH_VERSION == "0.1.0"
    assert versioning.KB_SCHEMA_VERSION == 2
    assert versioning.WIRING_VERSION == 3
    assert versioning.check_tag("v0.1.0")[0]
    assert not versioning.check_tag("v0.1.1")[0]


def test_stable_release_check_rejects_prerelease_or_build_version(monkeypatch):
    monkeypatch.setattr(versioning, "LATCH_VERSION", "0.2.0-beta.1")
    assert not versioning.check_tag("v0.2.0-beta.1")[0]
    monkeypatch.setattr(versioning, "LATCH_VERSION", "0.2.0+build.7")
    assert not versioning.check_tag("v0.2.0+build.7")[0]


def test_version_payload_has_support_coordinates():
    data = versioning.payload()
    assert data["latch_version"] == "0.1.0"
    assert data["kb_schema_version"] == 2
    assert data["wiring_version"] == 3
    assert data["install_root"] == str(ROOT)
