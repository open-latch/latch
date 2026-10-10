"""Deterministic cite-presence detector — Slice 3-B of mission control.

No LLM, pure regex scan (mirrors move_classifier.py: closed regex set,
sub-millisecond, safe to call on every turn). The third leg of mission
control's "blocking = contract + detection + adversary" (KB id=1398):
mechanically catch when an assistant turn asserts a *current-value / code /
config* conclusion WITHOUT a nearby `file:line` citation — the uncited
``current_value_or_code`` claim class that led pmeyer astray (KB id=1399), the
same gap_type the kb_gate citation pass routes to ``code_trace`` (id=1253).

latch registers no PostToolUse/output hook (id=1398), so this runs *post-hoc*
in the Stop hook over the just-finished assistant message. It cannot pre-block;
it flags for the advisory next-turn nudge surfaced via the UserPromptSubmit
``additionalContext`` seam, and emits a structural ``detection.log`` signal.
On-hit posture is **advisory** (not a forced ``decision:block``), decided
2026-06-07 — see the verification-profiles plan (id=1395 / id=1436).

Detection model — window-local:
  1. Strip fenced code blocks (```` ``` ````): code samples are not conclusions,
     and their punctuation/colons would spuriously trip the claim/cite regexes.
     Inline code spans are KEPT — they often carry the `file:line` cite itself.
  2. Split the prose into windows (paragraphs; each bullet / numbered line is
     its own window). A cite three paragraphs away must NOT excuse an uncited
     claim, so the cite has to sit in the SAME window as the claim.
  3. Within each window: a CODE_CLAIM assertion with no CITE in that window is
     flagged.

Deterministic + imprecise BY DESIGN. It feeds an advisory nudge, not a hard
gate, and every mission-control turn emits an n_claims / n_flagged row so the
detector's precision can be measured from real data before any tightening
(mirrors the orphan_hint precision dogfood, id=1197). Scoped to
mission-control-bound actors by the caller (Stop hook); byte-identical no-op
for everyone else.
"""
# =============================================================================
# EXPERIMENTAL — MISSION CONTROL / VERIFICATION PROFILES (NOT production-ready).
#
# Experimental and NOT recommended for use right now. Tried live on pmeyer's
# workspace (2026-06-10) and not seen to be helpful in practice. Plan of record:
# UNSHIP to a separate branch to continue later — NOT done yet; this stays in
# mainline but flagged. Do not extend or rely on this in mainline behaviour
# until that evaluation lands. See KB decision id=1550 (spine id=1396).
# =============================================================================
from __future__ import annotations

import re

# A satisfying citation: a path-ish token ending in a known code/config
# extension, followed by a line locator (`:42`, `#L42`, `:42-51`). A bare
# filename with NO line does NOT clear a claim — the mission-control contract
# asks for `file:line` specifically (profiles.render_mission_control_context),
# because a non-technical user cannot verify a vague filename. Matches both
# prose (`gate.py:91`) and the markdown-link text form (`[gate.py:91](...)`).
CITE = re.compile(
    r"\b[\w./\\-]+\.(?:py|cs|ts|tsx|js|jsx|json|toml|ya?ml|md|sql|sh|ps1|bat|"
    r"cfg|ini|conf|c|cc|cpp|h|hpp|go|rs|java|rb|php|swift|kt|scala|xml|html|css)"
    r"[#:]L?\d+",
    re.IGNORECASE,
)

# Assertive present-tense claims about what a config / parameter / code path
# CURRENTLY is or does. Anchored on config/code nouns + a value-binding or
# behaviour verb to keep general English ("the test is slow") from tripping it.
# First match per window is enough to flag; `n_claims` counts windows with a
# claim. Tuned conservative-ish, but precision is to be measured (see header).
CODE_CLAIM = re.compile(
    r"(?:"
    # (a) a config/code noun asserted to hold a current value / state
    r"\b(?:config\w*|parameter|param|setting|option|toggle|threshold|flag|"
    r"variable|env(?:ironment)?\s+var\w*|model[_\s]?tag|field|constant|default|"
    r"value)\b[^.\n]{0,40}?\b(?:is|are|=|==|set\s+to|defaults?\s+to|"
    r"currently|equals?|holds?)\b"
    r"|"
    # (b) a named code element asserted to behave a certain way
    r"\bthe\s+(?:code|function|method|class|module|loader|fitter|hook|handler|"
    r"script|query|regex|migration|endpoint|route|service)\b[^.\n]{0,40}?"
    r"\b(?:returns?|does|doesn'?t|don'?t|calls?|sets?|reads?|writes?|uses?|"
    r"checks?|handles?|registers?|loads?|parses?|invokes?|raises?|throws?|"
    r"defaults?)\b"
    r"|"
    # (c) explicit on/off / hardcoded / commented-out current-state assertions
    r"\b(?:is|are|it'?s|they'?re)\s+(?:currently\s+)?(?:set\s+to|enabled|"
    r"disabled|turned\s+(?:on|off)|hard-?coded|commented\s+out|wired\s+(?:up|to)|"
    r"hooked\s+up)\b"
    r")",
    re.IGNORECASE,
)

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _strip_code_fences(text: str) -> str:
    """Remove fenced code blocks (not conclusions; their syntax false-trips)."""
    return _FENCE.sub(" ", text or "")


def _windows(text: str) -> list[str]:
    """Split prose into claim/cite windows: blank-line paragraphs, with each
    bullet / numbered list line broken out as its own window (so a cite on one
    bullet can't cover an uncited claim on a sibling bullet)."""
    windows: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.splitlines()
        if any(_BULLET.match(ln) for ln in lines):
            windows.extend(ln for ln in lines if ln.strip())
        elif block.strip():
            windows.append(block)
    return windows


def scan_message(text: str) -> dict:
    """Scan one assistant message for uncited current-value/code/config claims.

    Returns:
        n_claims:  windows containing a current-value/code claim.
        n_flagged: of those, how many lacked a `file:line` cite in-window.
        flagged:   the triggering claim substrings (for tests/debug ONLY — the
                   caller logs counts, never this text; structural-only,
                   id=1108).
    Pure + deterministic. Empty/whitespace input → all zeros.
    """
    stripped = _strip_code_fences(text or "")
    n_claims = 0
    flagged: list[str] = []
    for w in _windows(stripped):
        m = CODE_CLAIM.search(w)
        if not m:
            continue
        n_claims += 1
        if not CITE.search(w):
            flagged.append(m.group(0).strip())
    return {
        "n_claims": n_claims,
        "n_flagged": len(flagged),
        "has_uncited_claim": bool(flagged),
        "flagged": flagged,
    }
