"""Committed smoke fixtures and the live-content grep guard (charter item 2a).

The committed tier is synthetic and must stay that way. It ships in a public
repo, so a fixture that quoted a real KB body would publish private judgment —
the failure id=4781 exists to prevent — and would also destroy the tier's
purpose, which is to exercise the instrument rather than to be evidence.
"""
import json
import re
from pathlib import Path

import pytest

from latch.evals import adherence


REPO_ROOT = Path(__file__).resolve().parent.parent
SMOKE_FIXTURE = REPO_ROOT / "benchmarks" / "fixtures" / "adherence_smoke_v1.jsonl"


def _text_fields(spec):
    """Every human-authored string in a probe set. Deliberately not the whole
    JSON: schema values like status="canonical" are vocabulary, not content."""
    yield spec["decision"]["title"]
    yield spec["decision"]["text"]
    for node in spec.get("context_nodes") or []:
        yield node["title"]
        yield node["body"]
    for option in adherence.rejected_options_of(spec):
        yield option["option"]
        yield option.get("reason") or ""
    for probe in adherence.probes_of(spec):
        yield probe["request"]
        for answer in (probe.get("scripted_responses") or {}).values():
            yield answer


@pytest.fixture
def specs():
    return adherence.load_probe_sets([SMOKE_FIXTURE])


def test_the_smoke_fixture_loads_and_validates(specs):
    assert len(specs) == 3
    assert {s["set_id"] for s in specs} == {
        "smoke-ledger-threads", "smoke-ledger-orm", "smoke-ledger-parser"
    }


def test_every_set_has_one_e3_and_one_e4(specs):
    for spec in specs:
        probes = adherence.probes_of(spec)
        assert [p["probe_type"] for p in probes] == ["E3", "E4"]


def test_every_arm_has_a_scripted_response(specs):
    """A missing response would surface as an error row rather than a count."""
    for spec in specs:
        for probe in adherence.probes_of(spec):
            assert set(probe["scripted_responses"]) == set(adherence.ARMS), probe["probe_id"]


# ---------- the grep guard ----------

#: Vocabulary that only appears in this project's own KB and tooling. A hit in
#: an authored fixture string means live content leaked in. Personal account
#: names are not listed here: naming them in a public test would be the leak,
#: and the repo-wide public release check already rejects them in every file.
_LIVE_VOCABULARY = (
    "latch", "workstream", "rejected_path", "kb_gate",
    "ratifier", "ratified", "founder", "open-latch",
)

#: KB citation syntax, e.g. "id=4781" or "KB id=3948".
_NODE_CITATION = re.compile(r"\bid\s*=\s*\d+", re.IGNORECASE)


def test_committed_fixture_contains_no_live_kb_vocabulary(specs):
    offenders = []
    for spec in specs:
        for text in _text_fields(spec):
            lowered = text.lower()
            for token in _LIVE_VOCABULARY:
                if token in lowered:
                    offenders.append((spec["set_id"], token, text[:80]))
    assert offenders == [], f"live-vault vocabulary in a committed fixture: {offenders}"


def test_committed_fixture_contains_no_kb_node_citations(specs):
    offenders = [
        (spec["set_id"], text[:80])
        for spec in specs
        for text in _text_fields(spec)
        if _NODE_CITATION.search(text)
    ]
    assert offenders == [], f"KB node citations in a committed fixture: {offenders}"


def test_committed_fixture_carries_no_latch_managed_markers():
    raw = SMOKE_FIXTURE.read_text(encoding="utf-8")
    assert adherence.contains_latch_bytes(raw) == []


def test_the_grep_guard_would_actually_catch_a_leak():
    """Non-vacuity: the guard is only worth having if it fires."""
    leaked = "We ratified this in the latch workstream (id=4781)."
    lowered = leaked.lower()
    assert any(token in lowered for token in _LIVE_VOCABULARY)
    assert _NODE_CITATION.search(leaked)


# ---------- schema validation ----------

def _minimal_spec():
    return {
        "set_id": "x",
        "decision": {"title": "t", "text": "body"},
        "e3": {"probe_id": "x-e3", "request": "r", "forbidden_paths": ["a.py"]},
        "e4": {"probe_id": "x-e4", "request": "r"},
    }


def test_validation_requires_a_declared_forbidden_surface_on_e3():
    spec = _minimal_spec()
    spec["e3"].pop("forbidden_paths")
    with pytest.raises(adherence.AdherenceError) as excinfo:
        adherence.validate_probe_set(spec, "where")
    assert "pre-registered" in str(excinfo.value)


def test_validation_refuses_a_forbidden_surface_on_the_negative_control():
    spec = _minimal_spec()
    spec["e4"]["forbidden_paths"] = ["a.py"]
    with pytest.raises(adherence.AdherenceError) as excinfo:
        adherence.validate_probe_set(spec, "where")
    assert "negative control" in str(excinfo.value)


def test_validation_rejects_an_edge_to_an_unknown_ref():
    spec = _minimal_spec()
    spec["edges"] = [{"src": "decision", "dst": "ghost", "relation": "related_to"}]
    with pytest.raises(adherence.AdherenceError):
        adherence.validate_probe_set(spec, "where")


def test_an_unauthored_draft_can_never_be_scored(tmp_path):
    """The extractor emits drafts; scoring one would measure placeholder text."""
    draft = _minimal_spec()
    draft["draft"] = True
    path = tmp_path / "draft.jsonl"
    path.write_text(json.dumps(draft) + "\n", encoding="utf-8")
    with pytest.raises(adherence.AdherenceError) as excinfo:
        adherence.load_probe_sets([path])
    assert "unauthored draft" in str(excinfo.value)


def test_missing_fixture_is_an_error(tmp_path):
    with pytest.raises(adherence.AdherenceError):
        adherence.load_probe_sets([tmp_path / "nope.jsonl"])
