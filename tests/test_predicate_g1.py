"""G1 proof: global tail catch with a bombed model/network path."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
import subprocess
import sys


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"


def _tail_vault(path: Path, count: int = 1001) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE rejected_path (
                id INTEGER PRIMARY KEY,
                node_id INTEGER NOT NULL,
                option TEXT NOT NULL,
                reason TEXT NOT NULL,
                ratifier TEXT,
                decided_at TEXT,
                scope_predicate TEXT,
                source TEXT NOT NULL,
                policy_domain_id TEXT
            );
            CREATE TABLE ratification (
                id INTEGER PRIMARY KEY,
                node_id INTEGER NOT NULL,
                ratifier TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                action TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE edges (
                src INTEGER NOT NULL,
                dst INTEGER NOT NULL,
                relation TEXT NOT NULL,
                status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO nodes (id, kind, status, updated_at) VALUES (?, ?, ?, ?)",
            (1, "decision", "canonical", "2026-08-27 00:00:00"),
        )
        rows = []
        for row_id in range(1, count + 1):
            target = (
                "src/g1-tail-target.py"
                if row_id == count
                else f"src/no-match-{row_id}.py"
            )
            rows.append(
                (
                    row_id,
                    1,
                    f"synthetic rejected option {row_id}",
                    f"synthetic rejection reason {row_id}",
                    "synthetic-founder",
                    "2026-08-27 00:00:00",
                    f"file:{target}",
                    "declared",
                    "synthetic-g1-domain",
                )
            )
        connection.executemany(
            """
            INSERT INTO rejected_path
                (id, node_id, option, reason, ratifier, decided_at,
                 scope_predicate, source, policy_domain_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_g1_tail_predicate_catch_zero_model_calls_log_verified_100_of_100(tmp_path):
    vault_path = tmp_path / "synthetic-vault.sqlite3"
    _tail_vault(vault_path)
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "policy.snapshot.json"
    project_root = tmp_path / "synthetic-project"
    project_root.mkdir()
    action_path = tmp_path / "action.json"
    action_path.write_text(
        json.dumps(
            {
                "policy_domain_id": "synthetic-g1-domain",
                "project_root": str(project_root),
                "cwd": str(project_root),
                "tool_name": "synthetic.write",
                "proposed_file_paths": ["src/g1-tail-target.py"],
                "diff_paths": [],
                "staged_paths": [],
                "import_names": [],
                "api_names": [],
                "evidence_complete": True,
                "evidence_provenance": ["synthetic-g1-test"],
            }
        ),
        encoding="utf-8",
    )

    code = r'''import builtins
import json
from pathlib import Path
import socket
import sqlite3
import sys
import urllib.request

source_root, vault_path, snapshot_path, action_path = sys.argv[1:]
banned_roots = {
    "aiohttp", "anthropic", "budget", "db", "gate", "httpx", "model_backends",
    "openai", "requests", "semantic_search", "socket", "urllib3",
}
banned_modules = {"http.client", "urllib.request"}
baseline_banned = {
    name for name in sys.modules
    if name.split(".", 1)[0] in banned_roots or name in banned_modules
}
events = []
def bomb(kind):
    def invoke(*args, **kwargs):
        events.append({"kind": kind})
        raise AssertionError(f"forbidden policy-path call: {kind}")
    return invoke
socket.socket = bomb("socket.socket")
socket.create_connection = bomb("socket.create_connection")
urllib.request.urlopen = bomb("urllib.request.urlopen")
original_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".", 1)[0]
    if level == 0 and (root in banned_roots or name in banned_modules):
        events.append({"kind": "forbidden_import", "name": name})
        raise AssertionError(f"forbidden policy-path import: {name}")
    return original_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
sys.path.insert(0, source_root)
import predicate_policy
import predicate_snapshot
import predicate_consumer
def projector():
    connection = sqlite3.connect(f"file:{vault_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return predicate_policy.project_policy_domain(
            connection, "synthetic-g1-domain"
        )
    finally:
        connection.close()
predicate_snapshot.publish_policy_snapshot(
    snapshot_path,
    policy_domain_id="synthetic-g1-domain",
    projector=projector,
    source_vault_path=vault_path,
)
action = json.loads(Path(action_path).read_text(encoding="utf-8"))
caught = 0
receipts = []
for _ in range(100):
    result = predicate_consumer.evaluate_policy(snapshot_path, action)
    receipt = result.receipt
    receipts.append(receipt)
    assert receipt["decision"] == "block"
    assert receipt["matched_rejected_path_ids"] == [1001]
    assert receipt["llm_calls"] == 0
    caught += 1
loaded_banned = sorted(
    name for name in sys.modules
    if name.split(".", 1)[0] in banned_roots or name in banned_modules
    if name not in baseline_banned
)
print(json.dumps({
    "caught": caught,
    "event_count": len(events),
    "loaded_banned": loaded_banned,
    "receipt_llm_calls": sorted({row["llm_calls"] for row in receipts}),
}))
'''
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(_SRC),
            str(vault_path),
            str(target),
            str(action_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    proof = json.loads(proc.stdout)
    assert proof == {
        "caught": 100,
        "event_count": 0,
        "loaded_banned": [],
        "receipt_llm_calls": [0],
    }


def test_policy_path_import_graph_has_no_model_budget_network_or_gate_dependency():
    def import_graph(roots: set[str]) -> set[str]:
        queue = list(roots)
        visited: set[str] = set()
        imports: set[str] = set()
        while queue:
            module_name = queue.pop()
            if module_name in visited:
                continue
            visited.add(module_name)
            path = _SRC / f"{module_name}.py"
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    root = name.split(".", 1)[0]
                    imports.add(root)
                    if (_SRC / f"{root}.py").is_file():
                        queue.append(root)
        return imports

    banned = {
        "aiohttp", "anthropic", "budget", "db", "gate", "httpx",
        "mcp_server", "model_backends", "openai", "requests", "socket",
        "urllib", "urllib3",
    }
    consumer_imports = import_graph(
        {"predicate_consumer", "predicate_snapshot", "predicate"}
    )
    assert "sqlite3" not in consumer_imports
    assert consumer_imports.isdisjoint(banned), sorted(consumer_imports & banned)

    projection_imports = import_graph(
        {"predicate_policy", "predicate_snapshot", "predicate_consumer", "predicate"}
    )
    assert "sqlite3" in projection_imports
    assert projection_imports.isdisjoint(banned), sorted(projection_imports & banned)
