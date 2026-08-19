"""Static and behavioral guardrails for ratification-bound authority.

The static inventory is deliberately closed: adding another production path
that can mint ``nodes.status == "canonical"`` must fail this test until the
path is classified as human-ratified or machine/evidence lifecycle.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import db
import maintenance


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

JUDGMENT_KINDS = frozenset({"decision", "preference"})
EVIDENCE_PROMOTION_KINDS = frozenset({"fact", "progress"})

_NODE_STATUS_WRITERS = frozenset({
    "insert_node",
    "insert_node_nc",
    "update_node",
    "update_node_nc",
    "insert_with_heal",
    "correct_apply",
    "correct_and_reconcile",
})
_CANONICAL_NAMES = frozenset({
    "ACTIVE_STATUS",
    "corrected_status",
    "status",
    "summary_status",
})
_CANONICAL_SQL = re.compile(
    r"(?:UPDATE\s+nodes\s+SET|INSERT\s+INTO\s+nodes).*?"
    r"(?:['\"]canonical['\"]|\bcanonical\b)",
    re.IGNORECASE | re.DOTALL,
)


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _string_value(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else "{}"
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_value(node.left) + _string_value(node.right)
    return ""


def _may_be_canonical(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == "canonical"
    if isinstance(node, ast.Name):
        return node.id in aliases or node.id in _CANONICAL_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "ACTIVE_STATUS"
    if isinstance(node, ast.IfExp):
        return _may_be_canonical(node.body, aliases) or _may_be_canonical(
            node.orelse, aliases
        )
    if isinstance(node, ast.BoolOp):
        return any(_may_be_canonical(value, aliases) for value in node.values)
    if isinstance(node, (ast.Call, ast.Subscript)):
        # A mapping lookup or helper result used as a node status is dynamic;
        # conservatively treat it as canonical-capable. This catches forms such
        # as node.get("status", "canonical") and lifecycle replay payloads.
        return True
    return False


class _CanonicalSurfaceVisitor(ast.NodeVisitor):
    """Find literal and one-hop indirect canonical node-status writes."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.functions: list[str] = []
        self.alias_scopes: list[set[str]] = [set()]
        self.surfaces: set[tuple[str, str, str, str]] = set()
        self.ratification_writers: set[tuple[str, str, str]] = set()

    @property
    def function(self) -> str:
        return ".".join(self.functions) or "<module>"

    @property
    def aliases(self) -> set[str]:
        return set().union(*self.alias_scopes)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        aliases = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            if arg.arg == "status" or arg.arg.endswith("_status")
        }
        self.alias_scopes.append(aliases)
        if node.name == "insert_node_nc" and "status" in aliases:
            self.surfaces.add(
                (self.relative_path, node.name, "node-status-low-level", "status")
            )
        if node.name == "update_node_nc" and "status" in aliases:
            self.surfaces.add(
                (self.relative_path, node.name, "node-transition-choke", "status")
            )
        self.generic_visit(node)
        self.alias_scopes.pop()
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        if _may_be_canonical(node.value, self.aliases):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.alias_scopes[-1].add(target.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and _may_be_canonical(node.value, self.aliases):
            if isinstance(node.target, ast.Name):
                self.alias_scopes[-1].add(node.target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        dotted = _dotted_name(node.func)
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf == "insert_ratification_nc":
            self.ratification_writers.add(
                (self.relative_path, self.function, dotted)
            )
        if leaf in _NODE_STATUS_WRITERS:
            for keyword in node.keywords:
                if keyword.arg in {"status", "corrected_status"} and _may_be_canonical(
                    keyword.value, self.aliases
                ):
                    expression = ast.unparse(keyword.value)
                    self.surfaces.add(
                        (self.relative_path, self.function, dotted, expression)
                    )
        if leaf in {"execute", "executemany", "executescript"} and node.args:
            sql = _string_value(node.args[0])
            if _CANONICAL_SQL.search(sql):
                self.surfaces.add(
                    (self.relative_path, self.function, f"sql:{leaf}", "canonical")
                )
            elif re.search(
                r"(?:UPDATE\s+nodes\s+SET|INSERT\s+INTO\s+nodes).*?status\s*=\s*\?",
                sql,
                re.IGNORECASE | re.DOTALL,
            ) and len(node.args) > 1:
                # Parameterized lifecycle replay. Its value is data-dependent,
                # so it belongs in the closed inventory even when the current
                # call normally restores "staging" or "stale".
                self.surfaces.add(
                    (self.relative_path, self.function, f"sql:{leaf}", "dynamic-status")
                )
        self.generic_visit(node)


def _canonical_minting_surfaces() -> frozenset[tuple[str, str, str, str]]:
    surfaces: set[tuple[str, str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        visitor = _CanonicalSurfaceVisitor(path.relative_to(ROOT).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        surfaces.update(visitor.surfaces)
    return frozenset(surfaces)


def _ratification_writer_surfaces() -> frozenset[tuple[str, str, str]]:
    writers: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        visitor = _CanonicalSurfaceVisitor(path.relative_to(ROOT).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        writers.update(visitor.ratification_writers)
    return frozenset(writers)


# Frozen after the pre-repair run exposed the complete source-derived set.
# Values are policy classifications, not implementation hints: the DB choke
# remains the runtime authority and all public judgment transitions still need
# one of the two ratified human surfaces.
_CLASSIFIED_CANONICAL_SURFACES: dict[tuple[str, str, str, str], str] = {
    (
        "src/compactor.py",
        "_apply_compaction",
        "db.insert_node",
        "summary_status",
    ): "machine:compactor-progress",
    (
        "src/compactor.py",
        "_apply_compaction",
        "db.update_node",
        "summary_status",
    ): "machine:compactor-progress",
    (
        "src/db.py", "insert_node", "insert_node_nc", "status",
    ): "guarded:db-delegate",
    (
        "src/db.py", "insert_node_nc", "node-status-low-level", "status",
    ): "low-level:legacy-seed",
    (
        "src/db.py", "promote_by_ref_count", "sql:execute", "canonical",
    ): "unattended:evidence-promotion",
    (
        "src/db.py", "update_node", "update_node_nc", "status",
    ): "guarded:db-delegate",
    (
        "src/db.py", "update_node_nc", "node-transition-choke", "status",
    ): "guarded:transition-choke",
    (
        "src/evals.py",
        "_seed_nodes",
        "db.insert_node",
        "node.get('status', 'canonical')",
    ): "fixture:offline-eval",
    (
        "src/heal.py", "prepare_insert_with_heal", "db.insert_node_nc", "status",
    ): "unattended:judgment-birth-refusal",
    (
        "src/mcp_server.py",
        "kb_capture_decision._capture",
        "db.update_node_nc",
        "'canonical'",
    ): "human:ratified-capture-decision",
    (
        "src/mcp_server.py",
        "kb_correct_apply._correct",
        "verify.correct_apply",
        "corrected_status",
    ): "public:judgment-birth-refusal",
    (
        "src/mcp_server.py",
        "kb_insert._insert",
        "heal.insert_with_heal",
        "status",
    ): "public:judgment-birth-refusal",
    (
        "src/mcp_server.py",
        "kb_update._update",
        "db.update_node",
        "status",
    ): "guarded:public-update",
    (
        "src/mcp_server.py",
        "kb_update._update",
        "db.update_node_nc",
        "status",
    ): "human:ratified-latch-update",
    (
        "src/no_history_demo.py",
        "create_fixture",
        "db.insert_node",
        "'canonical'",
    ): "fixture:no-history-demo",
    (
        "src/priorities.py",
        "add_priority",
        "db.insert_node_nc",
        "ACTIVE_STATUS",
    ): "machine:priority",
    (
        "src/profiles.py", "create_profile", "db.insert_node", "status",
    ): "machine:profile",
    (
        "src/tree.py", "build_tree", "db.insert_node", "'canonical'",
    ): "machine:tree-summary",
    (
        "src/verify.py",
        "correct_apply",
        "db.insert_node_nc",
        "corrected_status",
    ): "low-level:correction",
    (
        "src/workstreams.py",
        "_copy_merge_priorities_nc",
        "db.insert_node_nc",
        "priorities.ACTIVE_STATUS",
    ): "machine:priority-copy",
    (
        "src/workstreams.py",
        "_reconcile_lifecycle_integrity_in_transaction",
        "db.update_node_nc",
        "desired",
    ): "machine:workstream-repair",
    (
        "src/workstreams.py",
        "reopen_workstream",
        "db.update_node_nc",
        "restored_status",
    ): "machine:workstream-reopen",
    (
        "src/workstreams.py",
        "unmerge_workstreams",
        "db.update_node_nc",
        "payload.get('source_prior_status') or 'staging'",
    ): "machine:workstream-unmerge",
    (
        "src/workstreams.py",
        "unmerge_workstreams",
        "sql:execute",
        "dynamic-status",
    ): "machine:workstream-unmerge",
}


def test_canonical_minting_surface_registry_is_frozen():
    observed = _canonical_minting_surfaces()
    classified = frozenset(_CLASSIFIED_CANONICAL_SURFACES)
    assert observed == classified, (
        "unclassified canonical-minting surface(s):\n"
        + "\n".join(map(repr, sorted(observed ^ classified)))
    )
    assert set(_CLASSIFIED_CANONICAL_SURFACES.values()) <= {
        "fixture:no-history-demo",
        "fixture:offline-eval",
        "guarded:db-delegate",
        "guarded:public-update",
        "guarded:transition-choke",
        "human:ratified-capture-decision",
        "human:ratified-latch-update",
        "low-level:correction",
        "low-level:legacy-seed",
        "machine:compactor-progress",
        "machine:priority",
        "machine:priority-copy",
        "machine:profile",
        "machine:tree-summary",
        "machine:workstream-reopen",
        "machine:workstream-repair",
        "machine:workstream-unmerge",
        "public:judgment-birth-refusal",
        "unattended:evidence-promotion",
        "unattended:judgment-birth-refusal",
    }


def test_machine_lifecycle_surfaces_are_explicitly_classified():
    classifications = set(_CLASSIFIED_CANONICAL_SURFACES.values())
    assert {
        "machine:priority",
        "machine:profile",
        "machine:tree-summary",
        "machine:compactor-progress",
        "machine:workstream-reopen",
        "machine:workstream-repair",
        "machine:workstream-unmerge",
    } <= classifications


def test_insert_with_heal_remains_a_registered_canonical_surface():
    """Source-derived pin (5648 item 3): insert_with_heal must stay in the
    AST-observed canonical-minting inventory — as a function whose body hits a
    status writer, or as a tracked status-writer callee — so the committing
    wrapper can never silently drop out of guard coverage. Deliberately reads
    `_canonical_minting_surfaces()` (live source scan), not the frozen dict:
    the weak form could be satisfied by editing the dict alone."""
    observed = _canonical_minting_surfaces()
    mentions = {
        surface
        for surface in observed
        if surface[1] == "insert_with_heal"
        or surface[2].rsplit(".", 1)[-1] == "insert_with_heal"
    }
    assert mentions, (
        "insert_with_heal vanished from the source-derived canonical-minting "
        "surface inventory"
    )


def test_exactly_two_public_ratification_writers_are_registered():
    assert _ratification_writer_surfaces() == frozenset({
        (
            "src/mcp_server.py",
            "kb_capture_decision._capture",
            "db.insert_ratification_nc",
        ),
        (
            "src/mcp_server.py",
            "kb_update._update",
            "db.insert_ratification_nc",
        ),
    })


def test_ratification_kind_mapping_is_exact():
    assert db.JUDGMENT_KINDS == JUDGMENT_KINDS
    assert db.EVIDENCE_PROMOTION_KINDS == EVIDENCE_PROMOTION_KINDS


def _connect(tmp_path: Path):
    return db.connect(str(tmp_path))


def _insert_staging(conn, *, kind: str, title: str) -> int:
    node_id = db.insert_node(
        conn,
        kind=kind,
        title=title,
        body=f"{kind} body",
        status="staging",
    )
    conn.execute("UPDATE nodes SET ref_count = 4 WHERE id = ?", (node_id,))
    conn.commit()
    return node_id


def test_ref_count_promotion_is_exactly_the_evidence_lane(tmp_path):
    conn = _connect(tmp_path)
    try:
        kinds = (
            "decision",
            "preference",
            "fact",
            "progress",
            "entity",
            "idea",
            "open_question",
            "priority",
            "workstream",
            "summary",
            "profile",
        )
        ids = {
            kind: _insert_staging(conn, kind=kind, title=f"mixed {kind}")
            for kind in kinds
        }

        promoted = db.promote_by_ref_count(conn, min_ref_count=3)

        assert set(promoted) == {ids["fact"], ids["progress"]}
        statuses = {
            kind: db.get_node(conn, node_id)["status"]
            for kind, node_id in ids.items()
        }
        assert {
            kind for kind, status in statuses.items() if status == "canonical"
        } == EVIDENCE_PROMOTION_KINDS
    finally:
        conn.close()


def test_weekly_maintenance_keeps_decay_contract_and_promotes_only_evidence(tmp_path):
    conn = _connect(tmp_path)
    try:
        ids = {
            kind: _insert_staging(conn, kind=kind, title=f"weekly {kind}")
            for kind in ("decision", "preference", "fact", "progress", "entity")
        }
        before = {
            kind: dict(db.get_node(conn, node_id))
            for kind, node_id in ids.items()
        }
    finally:
        conn.close()

    result = maintenance.run_weekly_maintenance(str(tmp_path))

    assert result == {
        "ok": True,
        "decayed_rows": len(ids),
        "promoted_ids": [ids["fact"], ids["progress"]],
        "promoted_count": 2,
        "factor": maintenance.DECAY_FACTOR,
        "floor": maintenance.DECAY_FLOOR,
        "threshold": maintenance.PROMOTION_THRESHOLD,
    }
    conn = _connect(tmp_path)
    try:
        after = {
            kind: dict(db.get_node(conn, node_id))
            for kind, node_id in ids.items()
        }
        for kind in ids:
            assert after[kind]["ref_count"] == 4
            assert after[kind]["title"] == before[kind]["title"]
            assert after[kind]["body"] == before[kind]["body"]
        assert {
            kind for kind, node in after.items() if node["status"] == "canonical"
        } == EVIDENCE_PROMOTION_KINDS
        assert conn.execute("SELECT COUNT(*) FROM ratification").fetchone()[0] == 0
    finally:
        conn.close()


def test_machine_and_evidence_lifecycle_kinds_remain_ratification_exempt(tmp_path):
    """The DB choke point must not reinterpret machine lifecycle as judgment."""
    conn = _connect(tmp_path)
    try:
        machine_surfaces = {
            "priority": "priority",
            "workstream": "workstream",
            "tree-summary": "summary",
            "compactor-progress": "progress",
            "profile": "profile",
        }
        node_ids = {
            surface: db.insert_node(
                conn,
                kind=kind,
                title=f"{surface} lifecycle",
                body="machine-owned lifecycle fixture",
                status="canonical",
            )
            for surface, kind in machine_surfaces.items()
        }
        assert {
            surface: db.get_node(conn, node_id)["status"]
            for surface, node_id in node_ids.items()
        } == {surface: "canonical" for surface in machine_surfaces}
        assert conn.execute("SELECT COUNT(*) FROM ratification").fetchone()[0] == 0
    finally:
        conn.close()
