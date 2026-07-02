"""Verification-profile store (slice 1) — src/profiles.py + db._migrate_profiles.

Covers: migration creates + is idempotent; presets materialise idempotently;
config validation (closed-set, completeness); profiles stored UNEMBEDDED;
per-actor bind + resolve; default fallback when unbound or dangling; retire.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

import db        # noqa: E402
import profiles  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _fresh_db():
    tmp = tempfile.mkdtemp(prefix="kb_prof_")
    return tmp, db.connect(tmp)


def _cleanup(tmp, conn):
    try:
        conn.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def _table_names(conn):
    return {
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


# ---------- migration ----------

def test_migration_creates_side_tables():
    tmp, conn = _fresh_db()
    try:
        names = _table_names(conn)
        _assert("profile_config" in names, "profile_config table missing")
        _assert("profile_binding" in names, "profile_binding table missing")
        print("PASS migration_creates_side_tables")
    finally:
        _cleanup(tmp, conn)


def test_migration_idempotent():
    tmp, conn = _fresh_db()
    try:
        db._migrate_profiles(conn)
        db._migrate_profiles(conn)  # second call must not raise
        _assert("profile_config" in _table_names(conn), "table vanished")
        print("PASS migration_idempotent")
    finally:
        _cleanup(tmp, conn)


# ---------- presets ----------

def test_preset_configs_are_valid():
    for name, (_desc, cfg) in profiles.PRESETS.items():
        err = profiles.validate_config(cfg)
        _assert(err is None, f"preset {name} invalid: {err}")
    print("PASS preset_configs_are_valid")


def test_ensure_presets_idempotent():
    tmp, conn = _fresh_db()
    try:
        first = profiles.ensure_presets(conn)
        second = profiles.ensure_presets(conn)
        _assert(first == second, f"preset ids drifted: {first} != {second}")
        active = profiles.list_profiles(conn)
        _assert(len(active) == len(profiles.PRESETS),
                f"expected {len(profiles.PRESETS)} presets, got {len(active)}")
        print("PASS ensure_presets_idempotent")
    finally:
        _cleanup(tmp, conn)


def test_mission_control_is_max_intensity():
    tmp, conn = _fresh_db()
    try:
        profiles.ensure_presets(conn)
        mc = profiles.get_profile_by_name(conn, "mission-control")
        _assert(mc is not None, "mission-control preset not created")
        cfg = mc["config"]
        _assert(cfg["gate_surface"] == "all_moves", cfg)
        _assert(cfg["verdict_posture"] == "blocking_code_class", cfg)
        _assert(cfg["claim_backing_policy"] == "code_trace_required", cfg)
        _assert(cfg["adversary"] == "assumption_hunter", cfg)
        _assert(cfg["user_authority"] == "surface_forks", cfg)
        print("PASS mission_control_is_max_intensity")
    finally:
        _cleanup(tmp, conn)


# ---------- validation ----------

def test_validate_rejects_unknown_value():
    cfg = dict(profiles.PRESETS["trust-and-go"][1])
    cfg["verdict_posture"] = "nuke_it"
    _assert(profiles.validate_config(cfg) is not None, "should reject bad value")
    print("PASS validate_rejects_unknown_value")


def test_validate_rejects_incomplete():
    cfg = {"gate_surface": "all_moves"}
    _assert(profiles.validate_config(cfg) is not None, "should reject incomplete")
    print("PASS validate_rejects_incomplete")


def test_create_rejects_invalid_config():
    tmp, conn = _fresh_db()
    try:
        res = profiles.create_profile(conn, "bad", "x", {"gate_surface": "nope"})
        _assert("error" in res, f"should error: {res}")
        # nothing persisted
        _assert(profiles.get_profile_by_name(conn, "bad") is None, "leaked node")
        print("PASS create_rejects_invalid_config")
    finally:
        _cleanup(tmp, conn)


# ---------- storage shape ----------

def test_profile_is_unembedded():
    tmp, conn = _fresh_db()
    try:
        ids = profiles.ensure_presets(conn)
        for nid in ids.values():
            emb = conn.execute(
                "SELECT embedding FROM nodes WHERE id = ?", (nid,),
            ).fetchone()["embedding"]
            _assert(emb is None, f"profile {nid} should be unembedded")
        print("PASS profile_is_unembedded")
    finally:
        _cleanup(tmp, conn)


# ---------- bind + resolve ----------

def test_bind_and_resolve_by_name():
    tmp, conn = _fresh_db()
    try:
        res = profiles.bind_actor(conn, "pmeyer", name="mission-control")
        _assert(res.get("ok"), f"bind failed: {res}")
        active = profiles.resolve_active_profile(conn, "pmeyer")
        _assert(active["name"] == "mission-control", active)
        _assert(active["bound"] is True, active)
        _assert(active["config"]["verdict_posture"] == "blocking_code_class", active)
        print("PASS bind_and_resolve_by_name")
    finally:
        _cleanup(tmp, conn)


def test_unbound_actor_gets_default():
    tmp, conn = _fresh_db()
    try:
        active = profiles.resolve_active_profile(conn, "stranger")
        _assert(active["bound"] is False, active)
        _assert(active["name"] == profiles.DEFAULT_PROFILE, active)
        _assert(active["config"]["verdict_posture"] == "advisory", active)
        print("PASS unbound_actor_gets_default")
    finally:
        _cleanup(tmp, conn)


def test_dangling_binding_falls_back():
    tmp, conn = _fresh_db()
    try:
        profiles.bind_actor(conn, "pmeyer", name="mission-control")
        mc = profiles.get_profile_by_name(conn, "mission-control")
        profiles.retire_profile(conn, mc["id"])  # binding now dangles
        active = profiles.resolve_active_profile(conn, "pmeyer")
        _assert(active["name"] == profiles.DEFAULT_PROFILE,
                f"dangling binding should fall back to default: {active}")
        print("PASS dangling_binding_falls_back")
    finally:
        _cleanup(tmp, conn)


def test_bind_rejects_non_profile_node():
    tmp, conn = _fresh_db()
    try:
        nid = db.insert_node(conn, kind="fact", title="t", body="b", embedding=None)
        res = profiles.bind_actor(conn, "pmeyer", node_id=nid)
        _assert("error" in res, f"should reject non-profile node: {res}")
        print("PASS bind_rejects_non_profile_node")
    finally:
        _cleanup(tmp, conn)


# ---------- mission-control directive (slice 2) ----------

def test_render_empty_for_trust_and_go():
    cfg = profiles.PRESETS["trust-and-go"][1]
    _assert(profiles.render_mission_control_context(cfg) == "",
            "trust-and-go must render no directive")
    print("PASS render_empty_for_trust_and_go")


def test_render_mission_control_has_rules():
    cfg = profiles.PRESETS["mission-control"][1]
    out = profiles.render_mission_control_context(cfg, move_type="diagnosis", actor="pmeyer")
    _assert("Mission control active" in out, out)
    _assert("pmeyer" in out, "actor should appear")
    _assert("file:line" in out, "code_trace rule should appear for mission-control")
    _assert("diagnose" in out.lower(), "diagnosis tail should appear")
    print("PASS render_mission_control_has_rules")


def test_directive_fires_for_bound_mission_control():
    tmp, conn = _fresh_db()
    try:
        profiles.bind_actor(conn, "pmeyer", name="mission-control")
        out = profiles.mission_control_directive(conn, "why is the fit off?", actor="pmeyer")
        _assert(out and "Mission control active" in out, f"should fire: {out!r}")
        _assert("diagnose" in out.lower(), "diagnosis move should be detected")
        print("PASS directive_fires_for_bound_mission_control")
    finally:
        _cleanup(tmp, conn)


def test_directive_empty_for_unbound_actor():
    tmp, conn = _fresh_db()
    try:
        # No binding, and crucially NO preset rows are created by this call.
        out = profiles.mission_control_directive(conn, "why is the fit off?", actor="dev-a")
        _assert(out == "", f"unbound actor must get no directive: {out!r}")
        n = conn.execute("SELECT COUNT(*) c FROM nodes WHERE kind='profile'").fetchone()["c"]
        _assert(n == 0, f"hot path must not materialise presets for unbound actor (got {n})")
        print("PASS directive_empty_for_unbound_actor")
    finally:
        _cleanup(tmp, conn)


def test_directive_empty_for_bound_trust_and_go():
    tmp, conn = _fresh_db()
    try:
        profiles.bind_actor(conn, "dev-a", name="trust-and-go")
        out = profiles.mission_control_directive(conn, "why is the fit off?", actor="dev-a")
        _assert(out == "", f"trust-and-go binding must get no directive: {out!r}")
        print("PASS directive_empty_for_bound_trust_and_go")
    finally:
        _cleanup(tmp, conn)


def test_bind_defaults_to_current_actor():
    # /mission-control escalates whoever runs it: bind with no actor binds the
    # resolved db._ACTOR, and resolve (also defaulting to _ACTOR) sees it.
    tmp, conn = _fresh_db()
    try:
        res = profiles.bind_actor(conn, name="mission-control")  # no actor
        _assert(res.get("ok"), f"default-actor bind failed: {res}")
        active = profiles.resolve_active_profile(conn)  # defaults to db._ACTOR
        _assert(active["name"] == "mission-control", active)
        _assert(active["actor"] == res["actor"], "actor mismatch between bind/resolve")
        print("PASS bind_defaults_to_current_actor")
    finally:
        _cleanup(tmp, conn)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"\nALL {len(fns)} PROFILE TESTS PASSED")
