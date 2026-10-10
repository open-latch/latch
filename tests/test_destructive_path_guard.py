from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_runtime_recursive_delete_cannot_accept_kb_or_vault_targets():
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "shutil"
                and func.attr == "rmtree"
                and path.name != "vault_identity.py"
            ):
                target = ast.dump(node.args[0]).lower() if node.args else ""
                if any(word in target for word in ("project", "vault", "kb_dir", "database")):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], (
        "recursive deletion of a KB/vault target is allowed only inside "
        f"vault_identity's capability boundary: {offenders}"
    )


def test_tests_never_recursively_delete_resolved_kb_paths():
    offenders: list[str] = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        if path == Path(__file__).resolve():
            continue
        source = path.read_text(encoding="utf-8")
        if any(
            token in source
            for token in (
                "shutil.rmtree(project_dir",
                "shutil.rmtree(paths.project_dir",
                "shutil.rmtree(kb_dir",
            )
        ):
            offenders.append(path.name)
            continue
        tree = ast.parse(source, filename=str(path))
        resolved_names: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "paths"
                and func.attr in {"project_dir", "ensure_project_dir"}
            ):
                resolved_names.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "shutil"
                and func.attr == "rmtree"
            ):
                continue
            target = node.args[0]
            if isinstance(target, ast.Name) and target.id in resolved_names:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_uninstall_has_no_production_data_delete_primitive():
    source = (ROOT / "src" / "latch" / "install" / "uninstall_engine.py").read_text(encoding="utf-8")
    assert "shutil.rmtree(projects" not in source
    assert "--purge to delete" not in source
