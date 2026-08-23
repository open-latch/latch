from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db  # noqa: E402
import heal  # noqa: E402
import mcp_server  # noqa: E402


@pytest.fixture
def mcp_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    db.connect(str(project)).close()

    monkeypatch.setattr(mcp_server, "_conn", lambda: db.connect(str(project)))
    monkeypatch.setattr(mcp_server, "_project_cwd", lambda: str(project))
    monkeypatch.setattr(mcp_server, "_project_session_id", lambda: "mcp-session")
    monkeypatch.setattr(mcp_server.paths, "is_unlatched_mode", lambda: False)
    monkeypatch.setattr(
        mcp_server.capture_streams,
        "emit_decision_event",
        lambda **_kwargs: None,
    )
    return project


def _node(project: Path, node_id: int) -> dict:
    conn = db.connect(str(project))
    try:
        row = db.get_node(conn, node_id)
        assert row is not None
        return row
    finally:
        conn.close()


def _ratifications(project: Path) -> list[dict]:
    conn = db.connect(str(project))
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM ratification ORDER BY node_id, id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _node_count(project: Path) -> int:
    conn = db.connect(str(project))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    finally:
        conn.close()


def _force_ratification_insert_failure(project: Path) -> None:
    conn = db.connect(str(project))
    try:
        conn.executescript(
            """
            CREATE TRIGGER force_ratification_failure
            BEFORE INSERT ON ratification
            BEGIN
                SELECT RAISE(ABORT, 'forced ratification failure');
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _force_canonical_promotion_failure(project: Path) -> None:
    conn = db.connect(str(project))
    try:
        conn.executescript(
            """
            CREATE TRIGGER force_canonical_promotion_failure
            BEFORE UPDATE OF status ON nodes
            WHEN NEW.status = 'canonical' AND OLD.status != 'canonical'
            BEGIN
                SELECT RAISE(ABORT, 'forced canonical promotion failure');
            END;
            """
        )
        conn.commit()
    finally:
        conn.close()


def _ratification_write_call_sites() -> set[tuple[str, str]]:
    call_sites: set[tuple[str, str]] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self, relative_path: str) -> None:
            self.relative_path = relative_path
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute):
                leaf = func.attr
            elif isinstance(func, ast.Name):
                leaf = func.id
            else:
                leaf = ""
            if leaf == "insert_ratification_nc":
                call_sites.add((self.relative_path, ".".join(self.functions)))
            self.generic_visit(node)

    for path in sorted((ROOT / "src").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        Visitor(relative).visit(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
    return call_sites


def test_exactly_two_production_functions_write_ratification() -> None:
    assert _ratification_write_call_sites() == {
        ("src/mcp_server.py", "kb_capture_decision._capture"),
        ("src/mcp_server.py", "kb_update._update"),
    }


def test_capture_approve_ratifies_then_promotes_in_one_transaction(
    mcp_vault: Path,
) -> None:
    result = mcp_server.kb_capture_decision(
        title="Ratified decision",
        body="The user approved this exact decision.",
        gate_request="Should this decision govern?",
        human_action="approve",
        session_id="capture-session",
    )

    node_id = int(result["id"])
    assert _node(mcp_vault, node_id)["status"] == "canonical"
    rows = _ratifications(mcp_vault)
    assert len(rows) == 1
    assert rows[0]["node_id"] == node_id
    assert rows[0]["ratifier"] == "capture-session"
    assert rows[0]["action"] == "ratify"
    assert rows[0]["scope"] == "node"
    assert rows[0]["source"] == "capture_decision"


@pytest.mark.parametrize("human_action", ["modify", "override"])
def test_capture_nonapprove_cannot_request_canonical(
    mcp_vault: Path,
    human_action: str,
) -> None:
    result = mcp_server.kb_capture_decision(
        title=f"{human_action} remains proposed",
        body="Only approve grants authority.",
        gate_request="Should this decision govern?",
        human_action=human_action,
        status="canonical",
    )

    assert _node(mcp_vault, int(result["id"]))["status"] == "staging"
    assert _ratifications(mcp_vault) == []


def test_capture_reject_records_rejection_but_cannot_promote(
    mcp_vault: Path,
) -> None:
    result = mcp_server.kb_capture_decision(
        title="Rejected decision",
        body="This proposal must not become authority.",
        gate_request="Should this decision govern?",
        human_action="reject",
        status="canonical",
        session_id="reject-session",
    )

    node_id = int(result["id"])
    assert _node(mcp_vault, node_id)["status"] == "staging"
    rows = _ratifications(mcp_vault)
    assert len(rows) == 1
    assert rows[0]["node_id"] == node_id
    assert rows[0]["ratifier"] == "reject-session"
    assert rows[0]["action"] == "reject"
    assert rows[0]["source"] == "capture_decision"


@pytest.mark.parametrize("human_action", ["approve", "reject"])
def test_capture_ratification_requires_session_before_embedding_or_writes(
    mcp_vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    human_action: str,
) -> None:
    before = _node_count(mcp_vault)
    monkeypatch.setattr(mcp_server, "_project_session_id", lambda: None)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("missing-session capture must stop before side effects")

    monkeypatch.setattr(mcp_server.embeddings, "embed", unexpected_call)
    monkeypatch.setattr(
        mcp_server.capture_streams,
        "emit_decision_event",
        unexpected_call,
    )

    result = mcp_server.kb_capture_decision(
        title="Unattributed decision",
        body="Ratification requires a verified human session.",
        gate_request="Should this decision govern?",
        human_action=human_action,
    )

    assert result["ok"] is False
    assert "verified session identity" in result["error"]
    assert "id" not in result
    assert "decision_logged" not in result
    assert _node_count(mcp_vault) == before
    assert _ratifications(mcp_vault) == []


def test_rejected_capture_can_later_be_ratified_by_latch_update(
    mcp_vault: Path,
) -> None:
    rejected = mcp_server.kb_capture_decision(
        title="Rejected, then reconsidered",
        body="The founder may later ratify this same decision node.",
        gate_request="Should this decision govern?",
        human_action="reject",
        session_id="reject-session",
    )
    node_id = int(rejected["id"])

    promoted = mcp_server.kb_update(node_id, status="canonical")

    assert promoted["ok"] is True
    assert _node(mcp_vault, node_id)["status"] == "canonical"
    rows = _ratifications(mcp_vault)
    assert [row["action"] for row in rows] == ["reject", "ratify"]
    assert [row["source"] for row in rows] == [
        "capture_decision",
        "latch_update",
    ]


def test_capture_absent_action_writes_nothing(mcp_vault: Path) -> None:
    before = _node_count(mcp_vault)
    result = mcp_server.kb_capture_decision(
        title="No human action",
        body="This must not be captured as authority.",
        gate_request="Should this decision govern?",
        human_action=None,  # type: ignore[arg-type]
        status="canonical",
    )

    assert result["ok"] is False
    assert _node_count(mcp_vault) == before
    assert _ratifications(mcp_vault) == []


def test_capture_approve_rolls_back_node_if_ratification_fails(
    mcp_vault: Path,
) -> None:
    before = _node_count(mcp_vault)
    _force_ratification_insert_failure(mcp_vault)

    with pytest.raises(sqlite3.IntegrityError, match="forced ratification failure"):
        mcp_server.kb_capture_decision(
            title="Must roll back",
            body="The ratification insert will fail.",
            gate_request="Should this decision govern?",
            human_action="approve",
            session_id="capture-session",
        )

    assert _node_count(mcp_vault) == before
    assert _ratifications(mcp_vault) == []


def test_capture_approve_rolls_back_node_and_ratification_if_promotion_fails(
    mcp_vault: Path,
) -> None:
    before = _node_count(mcp_vault)
    _force_canonical_promotion_failure(mcp_vault)

    with pytest.raises(sqlite3.IntegrityError, match="promotion failure"):
        mcp_server.kb_capture_decision(
            title="Must fully roll back",
            body="The canonical promotion will fail after ratification.",
            gate_request="Should this decision govern?",
            human_action="approve",
            session_id="capture-session",
        )

    assert _node_count(mcp_vault) == before
    assert _ratifications(mcp_vault) == []


def test_latch_update_staging_decision_ratifies_then_promotes_atomically(
    mcp_vault: Path,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        node_id = db.insert_node(
            conn,
            kind="decision",
            title="Proposed decision",
            body="Awaiting explicit promotion.",
            status="staging",
        )
    finally:
        conn.close()

    result = mcp_server.kb_update(node_id, status="canonical")

    assert result["ok"] is True
    assert _node(mcp_vault, node_id)["status"] == "canonical"
    rows = _ratifications(mcp_vault)
    assert len(rows) == 1
    assert rows[0]["node_id"] == node_id
    assert rows[0]["ratifier"] == "mcp-session"
    assert rows[0]["action"] == "ratify"
    assert rows[0]["scope"] == "node"
    assert rows[0]["source"] == "latch_update"


def test_latch_update_rolls_back_promotion_if_ratification_fails(
    mcp_vault: Path,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        node_id = db.insert_node(
            conn,
            kind="preference",
            title="Proposed preference",
            body="Awaiting explicit promotion.",
            status="staging",
        )
    finally:
        conn.close()
    _force_ratification_insert_failure(mcp_vault)

    with pytest.raises(sqlite3.IntegrityError, match="forced ratification failure"):
        mcp_server.kb_update(node_id, status="canonical")

    assert _node(mcp_vault, node_id)["status"] == "staging"
    assert _ratifications(mcp_vault) == []


def test_latch_update_rolls_back_ratification_if_promotion_fails(
    mcp_vault: Path,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        node_id = db.insert_node(
            conn,
            kind="decision",
            title="Proposed decision",
            body="Awaiting explicit promotion.",
            status="staging",
        )
    finally:
        conn.close()
    _force_canonical_promotion_failure(mcp_vault)

    with pytest.raises(sqlite3.IntegrityError, match="promotion failure"):
        mcp_server.kb_update(node_id, status="canonical")

    assert _node(mcp_vault, node_id)["status"] == "staging"
    assert _ratifications(mcp_vault) == []


def test_latch_update_does_not_ratify_nontransition_or_evidence(
    mcp_vault: Path,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        # Simulate a row written by a pre-V3 runtime. The current public helper
        # correctly rejects new canonical judgment births, while existing
        # canonical state remains grandfathered and editable.
        legacy = int(conn.execute(
            "INSERT INTO nodes(kind, title, body, status) "
            "VALUES('decision', 'Grandfathered authority', "
            "'Already canonical before V3.', 'canonical')"
        ).lastrowid)
        conn.commit()
        fact = db.insert_node(
            conn,
            kind="fact",
            title="Evidence",
            body="Evidence remains low ceremony.",
            status="staging",
        )
    finally:
        conn.close()

    assert mcp_server.kb_update(legacy, title="Edited authority")["ok"] is True
    assert mcp_server.kb_update(fact, status="canonical")["ok"] is True
    assert _node(mcp_vault, legacy)["status"] == "canonical"
    assert _node(mcp_vault, fact)["status"] == "canonical"
    assert _ratifications(mcp_vault) == []


def test_latch_update_refuses_nonstaging_judgment_promotion(
    mcp_vault: Path,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        node_id = db.insert_node(
            conn,
            kind="decision",
            title="Retired decision",
            body="Stale authority cannot be silently revived.",
            status="stale",
        )
    finally:
        conn.close()

    result = mcp_server.kb_update(node_id, status="canonical")

    assert result["ok"] is False
    assert _node(mcp_vault, node_id)["status"] == "stale"
    assert _ratifications(mcp_vault) == []


@pytest.mark.parametrize("kind", sorted(db.JUDGMENT_KINDS))
def test_generic_latch_insert_cannot_mint_canonical_judgment(
    mcp_vault: Path,
    kind: str,
) -> None:
    before = _node_count(mcp_vault)

    result = mcp_server.kb_insert(
        kind=kind,
        title=f"Unauthorized canonical {kind}",
        body="Generic insertion is not a ratification surface.",
        status="canonical",
    )

    assert result["ok"] is False
    assert "ratification" in result["error"]
    assert _node_count(mcp_vault) == before
    assert _ratifications(mcp_vault) == []


@pytest.mark.parametrize("kind", sorted(db.JUDGMENT_KINDS))
def test_correct_apply_cannot_mint_canonical_judgment(
    mcp_vault: Path,
    kind: str,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        bad_node_id = db.insert_node(
            conn,
            kind="fact",
            title="Incorrect evidence",
            body="This fact needs correction.",
            status="canonical",
        )
    finally:
        conn.close()
    before = _node_count(mcp_vault)

    result = mcp_server.kb_correct_apply(
        bad_node_id=bad_node_id,
        mode="supersede",
        title=f"Unauthorized corrected {kind}",
        body="Correction is human-confirmed but not a V3 ratification act.",
        kind=kind,
        corrected_status="canonical",
    )

    assert result["ok"] is False
    assert "ratification" in result["error"]
    assert _node_count(mcp_vault) == before
    assert _node(mcp_vault, bad_node_id)["status"] == "canonical"
    assert _ratifications(mcp_vault) == []


def test_insert_link_fk_failure_leaves_no_node(mcp_vault: Path) -> None:
    """Pinned behavior change (5648 item 5b): a links.dst that violates the
    edges FK must roll the whole latch_insert back — no node may survive the
    failed transaction. Red against e7194b4, where insert_with_heal committed
    the node before the edge insert failed."""
    before = _node_count(mcp_vault)

    with pytest.raises(sqlite3.IntegrityError):
        mcp_server.kb_insert(
            kind="fact",
            title="FK-invalid link",
            body="A dangling links.dst must not strand a committed node.",
            links=[{"dst": 999999, "relation": "related_to"}],
        )

    assert _node_count(mcp_vault) == before


@pytest.mark.parametrize("kind", sorted(db.JUDGMENT_KINDS))
def test_heal_cannot_mint_canonical_judgment(
    mcp_vault: Path,
    kind: str,
) -> None:
    conn = db.connect(str(mcp_vault))
    try:
        before = int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
        with pytest.raises(db.RatificationRequiredError):
            heal.insert_with_heal(
                conn,
                kind=kind,
                title=f"Unauthorized healed {kind}",
                body="Heal is unattended and cannot ratify authority.",
                status="canonical",
                use_llm=False,
                project_path=str(mcp_vault),
            )
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM ratification").fetchone()[0] == 0
    finally:
        conn.close()
