"""Deterministic epistemic-move classifier for the UserPromptSubmit hook.

No LLM, sub-millisecond regex scan (mirrors the CORRECTION_SIGNAL /
GUIDELINE_SIGNAL pattern in user_prompt_submit.py). Classifies the USER's prompt
by the kind of epistemic move it asks the agent to make, so a mission-control
profile (gate_surface='all_moves') can inject a move-tailored verification
directive — broadening the gate's reach from coding prompts only to hypotheses,
investigations, and conclusions (the moves that led pmeyer astray, KB id=1399).
"Robust deterministic first; LLM only if too blunt" (id=1407 decision #4).

Move types (priority order — first match wins, most safety-critical first):
  diagnosis      — asks the agent to name a cause / conclude about current state
                   or behaviour ("why is X off?", "what's causing …",
                   "the issue is …"). The PBE-111 failure class.
  hypothesis     — floats an unverified guess ("maybe …", "could it be …",
                   "I think it's because …").
  investigation  — asks the agent to look into / debug / check something.
  implementation — asks for a code change (build/add/fix/refactor). Already
                   covered by kb_gate; classified here for completeness.
  other          — none of the above.
"""
# =============================================================================
# EXPERIMENTAL — MISSION CONTROL / VERIFICATION PROFILES (NOT production-ready).
#
# Experimental and NOT recommended for use right now. Tried live on pmeyer's
# workspace (2026-06-10) and not seen to be helpful in practice. Plan of record:
# UNSHIP to a separate branch to continue later — NOT done yet; this stays in
# mainline but flagged. Do not extend or rely on this in mainline behaviour
# until that evaluation lands. See KB decision id=1550 (spine id=1396).
# (This classifier is only invoked via the mission-control directive path.)
# =============================================================================
from __future__ import annotations

import re

# "why is/are/does …", "what's causing …", "root cause", "the problem is …" —
# the agent is being asked to reach a conclusion about current state/behaviour.
DIAGNOSIS = re.compile(
    r"\b(why\s+(is|are|does|do|did|isn'?t|aren'?t|won'?t|can'?t|would|wouldn'?t)|"
    r"what'?s\s+(causing|wrong|happening)|"
    r"what\s+(is|'s)\s+causing|"
    r"what\s+(causes|caused)|"
    r"root\s+cause|"
    r"explain\s+why|"
    r"the\s+(issue|problem|cause|reason|bug)\s+(is|seems|must\s+be|might\s+be|comes)|"
    r"is\s+it\s+because|that'?s\s+why|"
    r"diagnos\w+)\b",
    re.IGNORECASE,
)
HYPOTHESIS = re.compile(
    r"\b(maybe|perhaps|could\s+it\s+be|might\s+be|i\s+(think|suspect|bet|reckon|assume)|"
    r"my\s+(guess|hunch|theory)|probably\s+(because|due)|i'?m\s+guessing|"
    r"seems\s+like\s+it'?s|i\s+wonder\s+(if|whether)|presumably)\b",
    re.IGNORECASE,
)
INVESTIGATION = re.compile(
    r"\b(investigate|look\s+into|dig\s+into|debug|trace\s+(through|the|why)|"
    r"figure\s+out|find\s+out|check\s+(whether|if|the|that)|"
    r"look\s+at\s+(why|whether|the)|inspect|examine)\b",
    re.IGNORECASE,
)
IMPLEMENTATION = re.compile(
    r"\b(build|implement|add|create|write|fix|refactor|change|modify|update|"
    r"rewrite|rename|delete|remove|wire\s+up|extend|patch)\b",
    re.IGNORECASE,
)

# Checked in this order; first hit wins. Diagnosis leads because it is the
# highest-risk move (the unverified-conclusion failure mission control exists to
# stop); implementation trails because kb_gate already covers it.
_ORDER = (
    ("diagnosis", DIAGNOSIS),
    ("hypothesis", HYPOTHESIS),
    ("investigation", INVESTIGATION),
    ("implementation", IMPLEMENTATION),
)


def classify_move(prompt: str) -> dict:
    """Return {move_type, matched} for `prompt`. First match in priority order
    wins; `matched` is the triggering substring (telemetry/debug), None for
    'other'. Pure + deterministic — safe to call on every prompt."""
    p = prompt or ""
    for label, rx in _ORDER:
        m = rx.search(p)
        if m:
            return {"move_type": label, "matched": m.group(0)}
    return {"move_type": "other", "matched": None}
