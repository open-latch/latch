"""Verification-profile store for latch (slice 1: substrate only).

A *verification profile* is a named, per-user bundle of gate-behaviour settings
— the knob that turns latch's gate from "trust-and-go" (low intensity: an
expert working in their own domain) up to "mission control" (maximum intensity:
a user who cannot verify the agent's claims and must be protected from being led
askew). Mission control is therefore not a bespoke mode but the top of a general
intensity knob; the abstraction is the deliverable, mission control is preset #1
(KB id=1396 / id=1406 / id=1407).

Storage (mirrors the priorities pattern — migration-light, side-table-backed):
  * `kind='profile'` node in the existing `nodes` table — identity + human
    description, auditable/discoverable in the graph. Stored UNEMBEDDED so it
    never pollutes vector_search, kb_gate similarity seeds, or nightly-heal's
    similarity scan (gate.EXCLUDED_SEED_KINDS and the UserPromptSubmit hook
    EXCLUDED_KINDS both list 'profile').
  * Lifecycle rides nodes.status: 'canonical' = active, 'stale' = retired.
  * Typed parameters live in the `profile_config` side table
    (db._migrate_profiles), keyed by the profile node id — NOT crammed into the
    free-text body (the "don't merge into priority rows" line, id=1406).
  * Per-user binding lives in `profile_binding` (actor -> active profile id);
    the actor is the resolved db._ACTOR (CLAUDE_KB_USER/USERNAME/USER, id=1405),
    the SAME value the UserPromptSubmit hook and the MCP server both observe.

Slice 1 is the substrate ONLY: store + presets + binding + resolver. The
consumers — the broadened gate trigger surface, the blocking-on-code-class
posture, and the assumption-hunter adversary — read resolve_active_profile() in
later slices. Building the substrate first is the minimal forward-compatible
interface (id=510): nothing changes runtime behaviour until a consumer reads it.
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

import sqlite3

import db
import move_classifier


PROFILE_KIND = "profile"
ACTIVE_STATUS = "canonical"   # active profile
RETIRED_STATUS = "stale"      # retired — out of default reads, kept for audit


# ---- closed-set parameter vocabularies (structural, not free text) ----
# Closed sets keep a profile judgement-shaped and let downstream consumers
# branch on exact tokens instead of parsing prose.

GATE_SURFACE = frozenset({"implementation_only", "all_moves"})
VERDICT_POSTURE = frozenset({"advisory", "blocking_code_class"})
CLAIM_BACKING = frozenset({"standard", "code_trace_required"})
ADVERSARY_MODE = frozenset({"off", "counter_node", "assumption_hunter"})
USER_AUTHORITY = frozenset({"self_approve", "surface_forks"})

# field name -> allowed value set. Order here is the canonical field order.
_CONFIG_VOCAB: dict[str, frozenset[str]] = {
    "gate_surface": GATE_SURFACE,
    "verdict_posture": VERDICT_POSTURE,
    "claim_backing_policy": CLAIM_BACKING,
    "adversary": ADVERSARY_MODE,
    "user_authority": USER_AUTHORITY,
}
_CONFIG_FIELDS = tuple(_CONFIG_VOCAB.keys())


# ---- built-in presets (materialised lazily as profile nodes) ----
# name -> (description, config). The two poles of the intensity knob (id=1396):
# trust-and-go is the existing expert posture (id=758); mission-control is the
# max-intensity guardrail for a non-technical user (id=1407).
PRESETS: dict[str, tuple[str, dict]] = {
    "trust-and-go": (
        "Expert working in their own domain: gate only on implementation, "
        "advisory verdicts, agent may self-approve forks. The existing latch "
        "posture (KB id=758).",
        {
            "gate_surface": "implementation_only",
            "verdict_posture": "advisory",
            "claim_backing_policy": "standard",
            "adversary": "counter_node",
            "user_authority": "self_approve",
        },
    ),
    "mission-control": (
        "Maximum-intensity guardrail for a user who cannot verify the agent's "
        "claims: gate every epistemic move, hard-stop on unverified "
        "current-value/code claims until file:line is cited, assumption-hunter "
        "adversary, every genuine fork surfaced for the user's ratified call "
        "(KB id=1396 / id=1407).",
        {
            "gate_surface": "all_moves",
            "verdict_posture": "blocking_code_class",
            "claim_backing_policy": "code_trace_required",
            "adversary": "assumption_hunter",
            "user_authority": "surface_forks",
        },
    ),
}

# Actor with no explicit binding falls back to this preset — the conservative,
# zero-friction posture (a brand-new install should not silently run hot).
DEFAULT_PROFILE = "trust-and-go"


# ---------- validation ----------

def validate_config(config: dict) -> str | None:
    """Return an error string if `config` is not a complete, in-vocabulary
    parameter set; None if it is valid."""
    if not isinstance(config, dict):
        return "config must be a dict"
    missing = [f for f in _CONFIG_FIELDS if f not in config]
    if missing:
        return f"missing config fields: {', '.join(missing)}"
    unknown = [k for k in config if k not in _CONFIG_VOCAB]
    if unknown:
        return f"unknown config fields: {', '.join(unknown)}"
    for field, allowed in _CONFIG_VOCAB.items():
        if config[field] not in allowed:
            return (
                f"{field}={config[field]!r} not in {sorted(allowed)}"
            )
    return None


def _config_row_to_dict(row) -> dict:
    return {f: row[f] for f in _CONFIG_FIELDS}


# ---------- create / read ----------

def create_profile(
    conn: sqlite3.Connection,
    name: str,
    description: str,
    config: dict,
    *,
    session_id: str | None = None,
    status: str = ACTIVE_STATUS,
) -> dict:
    """Create a verification profile: an unembedded `kind='profile'` node
    (title=name, body=description) plus its `profile_config` row. Rejects an
    invalid/incomplete config with no write."""
    name = (name or "").strip()
    if not name:
        return {"error": "empty profile name"}
    err = validate_config(config)
    if err:
        return {"error": err}
    nid = db.insert_node(
        conn,
        kind=PROFILE_KIND,
        title=name,
        body=(description or "").strip(),
        status=status,
        session_id=session_id,
        embedding=None,  # surface-only: never embedded
    )
    conn.execute(
        """
        INSERT INTO profile_config
            (profile_node_id, gate_surface, verdict_posture,
             claim_backing_policy, adversary, user_authority)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (nid, *(config[f] for f in _CONFIG_FIELDS)),
    )
    conn.commit()
    return {"id": nid, "ok": True, "name": name, "config": dict(config)}


def get_profile(conn: sqlite3.Connection, node_id: int) -> dict | None:
    """Return {id, name, description, status, config} for a profile, or None."""
    node = db.get_node(conn, node_id)
    if node is None or node["kind"] != PROFILE_KIND:
        return None
    row = conn.execute(
        "SELECT * FROM profile_config WHERE profile_node_id = ?", (node_id,),
    ).fetchone()
    return {
        "id": node_id,
        "name": node["title"],
        "description": node["body"],
        "status": node["status"],
        "config": _config_row_to_dict(row) if row else None,
    }


def get_profile_by_name(
    conn: sqlite3.Connection, name: str, *, status: str = ACTIVE_STATUS,
) -> dict | None:
    """Return the active profile whose node title == name, or None."""
    row = conn.execute(
        "SELECT id FROM nodes WHERE kind = ? AND status = ? AND title = ? "
        "ORDER BY id LIMIT 1",
        (PROFILE_KIND, status, name),
    ).fetchone()
    return get_profile(conn, row["id"]) if row else None


def list_profiles(
    conn: sqlite3.Connection, *, include_retired: bool = False,
) -> list[dict]:
    """All active profiles (+ retired when asked), each with its config."""
    statuses = [ACTIVE_STATUS] + ([RETIRED_STATUS] if include_retired else [])
    placeholders = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT id FROM nodes WHERE kind = ? AND status IN ({placeholders}) "
        f"ORDER BY id",
        (PROFILE_KIND, *statuses),
    ).fetchall()
    return [get_profile(conn, r["id"]) for r in rows]


def ensure_presets(
    conn: sqlite3.Connection, *, session_id: str | None = None,
) -> dict[str, int]:
    """Idempotently materialise the built-in presets as profile nodes. Returns
    {preset_name: profile_node_id}. Safe to call on every resolve/bind."""
    out: dict[str, int] = {}
    for name, (desc, cfg) in PRESETS.items():
        existing = get_profile_by_name(conn, name)
        if existing is not None:
            out[name] = existing["id"]
            continue
        res = create_profile(conn, name, desc, cfg, session_id=session_id)
        out[name] = res["id"]
    return out


# ---------- per-user binding + resolution ----------

def bind_actor(
    conn: sqlite3.Connection,
    actor: str | None = None,
    *,
    name: str | None = None,
    node_id: int | None = None,
) -> dict:
    """Bind a user (resolved actor string) to an active profile, by preset name
    or explicit node id. Upserts the single per-actor binding row. `actor`
    defaults to the resolved db._ACTOR (the current OS user) when omitted — so
    the `/mission-control` escalation binds whoever runs it."""
    actor = (actor or db._ACTOR or "").strip()
    if not actor:
        return {"error": "empty actor"}
    if node_id is None and name is None:
        return {"error": "pass name= or node_id="}

    if node_id is not None:
        prof = get_profile(conn, node_id)
        if prof is None:
            return {"error": f"node {node_id} is not a profile"}
        if prof["status"] != ACTIVE_STATUS:
            return {"error": f"profile {node_id} is not active"}
    else:
        ensure_presets(conn)  # so preset names resolve
        prof = get_profile_by_name(conn, name)
        if prof is None:
            return {"error": f"no active profile named {name!r}"}

    conn.execute(
        """
        INSERT INTO profile_binding (actor, profile_node_id, bound_at)
        VALUES (?, ?, ?)
        ON CONFLICT(actor) DO UPDATE SET
            profile_node_id = excluded.profile_node_id,
            bound_at        = excluded.bound_at
        """,
        (actor, prof["id"], db._now()),
    )
    conn.commit()
    return {"ok": True, "actor": actor, "profile_id": prof["id"], "name": prof["name"]}


def resolve_active_profile(
    conn: sqlite3.Connection, actor: str | None = None,
) -> dict:
    """The active profile + config for `actor` (defaults to the resolved
    db._ACTOR — the same value the hook and MCP both see). Falls back to the
    DEFAULT_PROFILE preset when the actor has no binding (or a dangling one).
    Always returns a usable config; presets are materialised on demand."""
    actor = (actor or db._ACTOR or "unknown").strip()
    presets = ensure_presets(conn)

    row = conn.execute(
        "SELECT profile_node_id FROM profile_binding WHERE actor = ?", (actor,),
    ).fetchone()
    bound = False
    prof = None
    if row is not None:
        prof = get_profile(conn, row["profile_node_id"])
        if prof is not None and prof["status"] == ACTIVE_STATUS and prof["config"]:
            bound = True
        else:
            prof = None  # dangling/retired binding → fall back

    if prof is None:
        prof = get_profile(conn, presets[DEFAULT_PROFILE])

    return {
        "actor": actor,
        "bound": bound,
        "profile_id": prof["id"],
        "name": prof["name"],
        "config": prof["config"],
    }


def retire_profile(conn: sqlite3.Connection, node_id: int) -> dict:
    """Soft-delete a profile to 'stale' (reversible; row + config persist for
    audit). Bindings pointing at it fall back to the default on next resolve."""
    prof = get_profile(conn, node_id)
    if prof is None:
        return {"error": f"node {node_id} is not a profile"}
    if prof["status"] == RETIRED_STATUS:
        return {"id": node_id, "retired": True, "already": True}
    db.update_node(conn, node_id, status=RETIRED_STATUS)
    return {"id": node_id, "retired": True}


# ---------- mission-control directive (UserPromptSubmit consumer, slice 2) ----------

def render_mission_control_context(
    config: dict, *, move_type: str = "other", actor: str = "",
) -> str:
    """The standing mission-control verification contract injected into the
    UserPromptSubmit hook's additionalContext. Returns '' unless `config` is a
    mission-control-shaped profile (gate_surface='all_moves'). The body adapts to
    the profile's claim_backing_policy / user_authority and to the deterministic
    `move_type` of the prompt. This is the Tier-2 enforcement surface for
    'blocking by contract' — latch has no interceptor (KB id=1398), so the
    injected directive IS the enforcement."""
    if not config or config.get("gate_surface") != "all_moves":
        return ""
    who = f" ({actor})" if actor else ""
    lines = [
        f"## 🔒 Mission control active{who}",
        "This user relies on you to ground every claim — they cannot verify your "
        "reasoning against the code themselves. Standing rules this session:",
        "- **Gate every epistemic move, not just code changes.** A hypothesis, an "
        "investigation plan, and a conclusion each get the scrutiny you give an "
        "implementation — surface your reasoning and the evidence behind it.",
    ]
    if config.get("claim_backing_policy") == "code_trace_required":
        lines.append(
            "- **No unverified current-value / code-behaviour claims.** Before "
            "asserting what a config, parameter, or code path *currently does*, READ "
            "it and cite `file:line`. \"I believe it's set/off/on\" without a "
            "citation is forbidden — read the source first."
        )
    if config.get("user_authority") == "surface_forks":
        lines.append(
            "- **Surface genuine forks for the user's ratified call.** Don't "
            "self-approve a decision that is theirs (their expertise, or a stakeful "
            "/ irreversible choice) — present it and let them decide."
        )
    tail = {
        "diagnosis": "⚠ This prompt asks you to **diagnose / conclude a cause** — "
            "the exact move mission control exists to guard. Verify the relevant "
            "config/code at `file:line` before naming a cause; never infer it.",
        "hypothesis": "This prompt floats a **hypothesis**. Treat it as unverified "
            "until checked against the code/data, and state what evidence would "
            "confirm or kill it.",
        "investigation": "This is an **investigation**. State what you will actually "
            "read or run to ground it, not what you expect to find.",
        "implementation": "**Implementation move** — run `kb_gate` and resolve any "
            "uncovered current-value/code claims via `code_trace` before writing.",
    }.get(move_type)
    if tail:
        lines.append("")
        lines.append(tail)
    return "\n".join(lines)


def mission_control_directive(
    conn: sqlite3.Connection, prompt: str, *, actor: str | None = None,
) -> str:
    """Hot-path entry for the UserPromptSubmit hook. Returns the mission-control
    directive for `prompt` IFF the resolved actor is bound to a mission-control
    profile; '' otherwise. Deliberately lightweight for the common case: a single
    indexed lookup on `profile_binding`, and NO ensure_presets / writes — an
    unbound actor (the default, incl. every fresh install) short-circuits to ''.
    Only a user who has been explicitly bound pays the full resolve + render."""
    actor = (actor or db._ACTOR or "unknown").strip()
    row = conn.execute(
        "SELECT profile_node_id FROM profile_binding WHERE actor = ?", (actor,),
    ).fetchone()
    if row is None:
        return ""  # unbound → default trust-and-go → no directive, no writes
    prof = get_profile(conn, row["profile_node_id"])
    if prof is None or prof["status"] != ACTIVE_STATUS or not prof["config"]:
        return ""  # dangling / retired binding
    if prof["config"].get("gate_surface") != "all_moves":
        return ""  # bound, but not a mission-control-intensity profile
    move = move_classifier.classify_move(prompt)["move_type"]
    return render_mission_control_context(
        prof["config"], move_type=move, actor=actor,
    )


def claim_backing_requires_code_trace(
    conn: sqlite3.Connection, actor: str | None = None,
) -> bool:
    """True iff the resolved actor's bound profile requires code-trace backing
    (i.e. mission control, `claim_backing_policy='code_trace_required'`). This is
    the gate for Slice 3-B's Stop-hook cite detector: only such actors get their
    output scanned. Mirrors `active_adversary_mode` / `mission_control_directive`
    — a single indexed lookup, NO ensure_presets / writes, unbound actors
    short-circuit to False so detection is a byte-identical no-op for everyone
    not explicitly bound (KB id=1436)."""
    actor = (actor or db._ACTOR or "unknown").strip()
    row = conn.execute(
        "SELECT profile_node_id FROM profile_binding WHERE actor = ?", (actor,),
    ).fetchone()
    if row is None:
        return False
    prof = get_profile(conn, row["profile_node_id"])
    if prof is None or prof["status"] != ACTIVE_STATUS or not prof["config"]:
        return False
    return prof["config"].get("claim_backing_policy") == "code_trace_required"


def render_cite_correction_directive(n_flagged: int = 1) -> str:
    """The advisory next-turn nudge surfaced by the UserPromptSubmit hook when
    the previous turn's Stop-hook scan flagged uncited current-value/code claims
    (Slice 3-B, on-hit posture = advisory, not a forced block). Generic by
    construction — the detector logs counts only, never the claim text (id=1108).
    """
    n = max(1, int(n_flagged))
    plural = "s" if n != 1 else ""
    return (
        "## 🔎 Cite-presence check — your previous turn\n\n"
        f"Mission control's deterministic detector flagged **{n}** current-value / "
        f"code / config conclusion{plural} in your last turn stated WITHOUT a "
        "`file:line` citation. This user can't verify your claims against the code "
        "themselves (KB id=1399), so before you build on or repeat that "
        "conclusion: READ the relevant source and cite `file:line` (e.g. "
        "`config.toml:42`). If the flag was a false positive — the statement "
        "wasn't actually a claim about current code/config state — say so in one "
        "line and carry on."
    )


def active_adversary_mode(
    conn: sqlite3.Connection, actor: str | None = None,
) -> str:
    """The adversary discipline for the resolved actor's bound profile —
    'assumption_hunter' only when the bound profile's `adversary` says so,
    else 'counter_node' (the shipped default). Unbound actors short-circuit to
    'counter_node' on a single indexed lookup with NO writes, so the gate's
    default adversary path is byte-identical for everyone not explicitly bound
    (the regression the Slice-3 gate verdict flagged). Consumed by
    gate.run_gate to pick the adversary system prompt (KB id=1420)."""
    actor = (actor or db._ACTOR or "unknown").strip()
    row = conn.execute(
        "SELECT profile_node_id FROM profile_binding WHERE actor = ?", (actor,),
    ).fetchone()
    if row is None:
        return "counter_node"
    prof = get_profile(conn, row["profile_node_id"])
    if prof is None or prof["status"] != ACTIVE_STATUS or not prof["config"]:
        return "counter_node"
    return "assumption_hunter" if prof["config"].get("adversary") == "assumption_hunter" else "counter_node"
