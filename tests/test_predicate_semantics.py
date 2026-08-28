"""Acceptance tests for the A2-F2 host-neutral action envelope.

All policy and path values are synthetic.  The matrix deliberately includes
the four false-positive traps reproduced while chartering A2-full.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"


def _predicate_module():
    sys.path.insert(0, str(_SRC))
    try:
        return importlib.import_module("predicate")
    finally:
        sys.path.remove(str(_SRC))


def _row(scope_predicate: str, row_id: int) -> dict[str, object]:
    return {
        "id": row_id,
        "node_id": 10_000 + row_id,
        "option": f"synthetic option {row_id}",
        "scope_predicate": scope_predicate,
        "reason": f"synthetic reason {row_id}",
        "source": "declared",
    }


def _context(predicate, **overrides):
    values = {
        "policy_domain_id": "synthetic-domain-a",
        "project_root": "/workspace/project-a",
        "cwd": "/workspace/project-a",
        "tool_name": "synthetic_mutation",
        "proposed_file_paths": (),
        "diff_paths": (),
        "staged_paths": (),
        "import_names": (),
        "api_names": (),
        "evidence_complete": True,
        "evidence_provenance": {"adapter": "synthetic-v1"},
    }
    values.update(overrides)
    return predicate.ToolCallContext(**values)


@pytest.mark.parametrize(
    ("scope_predicate", "context_overrides"),
    [
        ("file:src", {"proposed_file_paths": ("src/pkg/widget.py",)}),
        ("file:src/pkg/widget.py", {"proposed_file_paths": ("src/pkg/widget.py",)}),
        ("glob:src/*.py", {"proposed_file_paths": ("src/widget.py",)}),
        ("glob:src/**/*.py", {"proposed_file_paths": ("src/pkg/widget.py",)}),
        (
            "file:src/pkg/widget.py",
            {
                "cwd": "/workspace/project-a/tools",
                "proposed_file_paths": ("src/pkg/widget.py",),
            },
        ),
        ("package:synthetic.widgets", {"import_names": ("synthetic.widgets.client",)}),
        ("import:synthetic.widgets", {"import_names": ("synthetic.widgets.client",)}),
        ("api:SyntheticClient.create", {"api_names": ("SyntheticClient.create",)}),
        (
            "file:src/pkg/widget.py",
            {
                "project_root": r"C:\Workspace\Project-A",
                "cwd": r"c:\workspace\project-a",
                "proposed_file_paths": (r"C:\WORKSPACE\PROJECT-A\SRC\PKG\WIDGET.PY",),
            },
        ),
        (
            "file:src/pkg/widget.py",
            {
                "project_root": r"\\server\share\Project-A",
                "cwd": r"\\SERVER\SHARE\project-a",
                "proposed_file_paths": (r"\\server\share\project-a\SRC\PKG\WIDGET.PY",),
            },
        ),
        (
            "file:src/pkg/widget.py",
            {
                "project_root": "//server/share/Project-A",
                "cwd": "//SERVER/SHARE/project-a",
                "proposed_file_paths": ("//server/share/project-a/SRC/PKG/WIDGET.PY",),
            },
        ),
    ],
)
def test_predicate_positive_matrix(scope_predicate, context_overrides):
    predicate = _predicate_module()
    check = predicate.compile_predicate(_row(scope_predicate, row_id=1))

    verdict = predicate.evaluate([check], _context(predicate, **context_overrides))

    assert verdict["decision"] == "block"
    assert [match["rejected_path_id"] for match in verdict["matches"]] == [1]
    assert predicate.context_evidence_issues(
        _context(predicate, **context_overrides)
    ) == ()


def test_predicate_negative_controls_posix_windows():
    predicate = _predicate_module()

    # Trap 1: a whole-segment file match in another repository must not block.
    foreign_posix = predicate.evaluate(
        [predicate.compile_predicate(_row("file:src", row_id=11))],
        _context(
            predicate,
            proposed_file_paths=("/workspace/project-b/src/widget.py",),
        ),
    )
    assert foreign_posix["decision"] == "flag"
    assert foreign_posix["matches"] == []

    foreign_windows_context = _context(
        predicate,
        project_root=r"C:\Workspace\Project-A",
        cwd=r"C:\Workspace\Project-A",
        proposed_file_paths=(r"D:\Elsewhere\src\widget.py",),
    )
    foreign_windows = predicate.evaluate(
        [predicate.compile_predicate(_row("file:src", row_id=12))],
        foreign_windows_context,
    )
    assert foreign_windows["decision"] == "flag"
    assert foreign_windows["matches"] == []
    assert "foreign_path:proposed_file_paths" in predicate.context_evidence_issues(
        foreign_windows_context
    )

    for rooted_path in (r"\src\widget.py", "/src/widget.py", r"\\server"):
        current_drive_rooted_context = _context(
            predicate,
            project_root=r"C:\Workspace\Project-A",
            cwd=r"C:\Workspace\Project-A",
            proposed_file_paths=(rooted_path,),
        )
        current_drive_rooted = predicate.evaluate(
            [predicate.compile_predicate(_row("file:src/widget.py", row_id=121))],
            current_drive_rooted_context,
        )
        assert current_drive_rooted["decision"] == "flag"
        assert current_drive_rooted["matches"] == []
        assert (
            "foreign_path:proposed_file_paths"
            in predicate.context_evidence_issues(current_drive_rooted_context)
        )

    traversal_context = _context(
        predicate,
        proposed_file_paths=("src/../project-b/src/widget.py",),
    )
    traversal = predicate.evaluate(
        [predicate.compile_predicate(_row("file:src", row_id=13))],
        traversal_context,
    )
    assert traversal["decision"] == "flag"
    assert "path_traversal:proposed_file_paths" in predicate.context_evidence_issues(
        traversal_context
    )

    # Trap 2: a single glob star never leaks across a path separator.
    recursive_glob_leak = predicate.evaluate(
        [predicate.compile_predicate(_row("glob:src/*.py", row_id=14))],
        _context(predicate, proposed_file_paths=("src/pkg/widget.py",)),
    )
    assert recursive_glob_leak["decision"] == "pass"
    assert recursive_glob_leak["matches"] == []

    # Trap 3: package predicates consume structured modules, not filenames.
    prose_path_package = predicate.evaluate(
        [predicate.compile_predicate(_row("package:cache", row_id=15))],
        _context(predicate, proposed_file_paths=("docs/cache.md",)),
    )
    assert prose_path_package["decision"] == "pass"
    assert prose_path_package["matches"] == []

    # Trap 4: API predicates consume structured API evidence, not shell prose.
    echo_and_comment_api = predicate.evaluate(
        [
            predicate.compile_predicate(
                _row("api:SyntheticClient.create", row_id=16)
            )
        ],
        _context(
            predicate,
            command_text=(
                "echo 'SyntheticClient.create' # SyntheticClient.create"
            ),
        ),
    )
    assert echo_and_comment_api["decision"] == "flag"
    assert echo_and_comment_api["matches"] == []
    assert "mutation_footprint_missing" in predicate.context_evidence_issues(
        _context(
            predicate,
            command_text="echo 'SyntheticClient.create' # SyntheticClient.create",
        )
    )


@pytest.mark.parametrize(
    ("project_root", "canonical_path", "alias_paths"),
    (
        (
            r"C:\repo",
            r"C:\repo\src\blocked.txt",
            (
                r"C:\repo\src\blocked.txt.",
                "C:\\repo\\src\\blocked.txt ",
                r"C:\repo\src\blocked.txt:payload",
                r"src\blocked.txt.",
                "src\\blocked.txt ",
                r"src\blocked.txt:payload",
            ),
        ),
        (
            r"\\server\share\repo",
            r"\\server\share\repo\src\blocked.txt",
            (
                r"\\server\share\repo\src\blocked.txt.",
                "\\\\server\\share\\repo\\src\\blocked.txt ",
                r"\\server\share\repo\src\blocked.txt:payload",
                r"src\blocked.txt.",
                "src\\blocked.txt ",
                r"src\blocked.txt:payload",
            ),
        ),
    ),
    ids=("drive", "unc"),
)
@pytest.mark.parametrize(
    "field_name",
    ("proposed_file_paths", "diff_paths", "staged_paths"),
)
def test_windows_filename_aliases_flag(
    project_root,
    canonical_path,
    alias_paths,
    field_name,
):
    predicate = _predicate_module()
    check = predicate.compile_predicate(_row("file:src/blocked.txt", row_id=17))

    canonical_context = _context(
        predicate,
        project_root=project_root,
        cwd=project_root,
        **{field_name: (canonical_path,)},
    )
    canonical = predicate.evaluate((check,), canonical_context)
    assert canonical["decision"] == "block"

    for alias_path in alias_paths:
        alias_context = _context(
            predicate,
            project_root=project_root,
            cwd=project_root,
            **{field_name: (alias_path,)},
        )
        verdict = predicate.evaluate((check,), alias_context)
        assert verdict["decision"] == "flag"
        assert verdict["matches"] == []
        assert (
            f"noncanonical_windows_path:{field_name}"
            in predicate.context_evidence_issues(alias_context)
        )


@pytest.mark.parametrize(
    ("project_root", "cwd", "reason_code"),
    (
        (r"C:\repo.", r"C:\repo.", "noncanonical_windows_path:project_root"),
        (r"C:\repo", r"C:\repo.", "noncanonical_windows_path:cwd"),
    ),
)
def test_windows_noncanonical_root_or_cwd_flags(
    project_root,
    cwd,
    reason_code,
):
    predicate = _predicate_module()
    check = predicate.compile_predicate(_row("file:src/blocked.txt", row_id=18))
    context = _context(
        predicate,
        project_root=project_root,
        cwd=cwd,
        proposed_file_paths=(r"src\unrelated.txt",),
    )

    verdict = predicate.evaluate((check,), context)

    assert verdict["decision"] == "flag"
    assert reason_code in predicate.context_evidence_issues(context)


@pytest.mark.parametrize("suffix", (".", ":payload"))
def test_posix_windows_alias_characters_are_literal(suffix):
    predicate = _predicate_module()
    relative_path = f"src/blocked.txt{suffix}"
    check = predicate.compile_predicate(_row(f"file:{relative_path}", row_id=19))

    verdict = predicate.evaluate(
        (check,),
        _context(predicate, proposed_file_paths=(relative_path,)),
    )

    assert verdict["decision"] == "block"


def test_posix_trailing_space_is_not_a_windows_alias():
    predicate = _predicate_module()
    check = predicate.compile_predicate(_row("file:src/blocked.txt", row_id=20))
    context = _context(
        predicate,
        proposed_file_paths=("src/blocked.txt ",),
    )

    verdict = predicate.evaluate((check,), context)

    assert verdict["decision"] == "pass"
    assert predicate.context_evidence_issues(context) == ()


def test_dirty_diff_and_staged_paths_are_evaluated():
    predicate = _predicate_module()
    checks = [
        predicate.compile_predicate(_row("file:src/dirty.py", row_id=21)),
        predicate.compile_predicate(_row("glob:config/**/*.toml", row_id=22)),
    ]

    dirty = predicate.evaluate(
        checks,
        _context(predicate, diff_paths=("src/dirty.py",)),
    )
    staged = predicate.evaluate(
        checks,
        _context(predicate, staged_paths=("config/env/prod.toml",)),
    )

    assert dirty["decision"] == "block"
    assert [match["rejected_path_id"] for match in dirty["matches"]] == [21]
    assert staged["decision"] == "block"
    assert [match["rejected_path_id"] for match in staged["matches"]] == [22]


def test_opaque_or_incomplete_action_flags():
    predicate = _predicate_module()
    nonmatching = [
        predicate.compile_predicate(_row("file:src/blocked.py", row_id=31))
    ]

    incomplete_context = _context(predicate, evidence_complete=False)
    incomplete = predicate.evaluate(nonmatching, incomplete_context)
    assert incomplete["decision"] == "flag"
    assert incomplete["matches"] == []
    assert predicate.context_evidence_issues(incomplete_context) == (
        "evidence_incomplete",
    )

    opaque_context = _context(
        predicate,
        evidence_complete=True,
        evidence_provenance=None,
        command_text="opaque mutation text",
    )
    opaque = predicate.evaluate(nonmatching, opaque_context)
    assert opaque["decision"] == "flag"
    assert "evidence_provenance_missing" in predicate.context_evidence_issues(
        opaque_context
    )

    nominally_complete_but_opaque = _context(
        predicate,
        command_text="opaque mutation text with no structured footprint",
    )
    opaque_verdict = predicate.evaluate(
        nonmatching,
        nominally_complete_but_opaque,
    )
    assert opaque_verdict["decision"] == "flag"
    assert "mutation_footprint_missing" in predicate.context_evidence_issues(
        nominally_complete_but_opaque
    )

    conflicting_context = _context(
        predicate,
        file_paths=("src/legacy.py",),
        proposed_file_paths=("src/canonical.py",),
    )
    conflicting = predicate.evaluate(nonmatching, conflicting_context)
    assert conflicting["decision"] == "flag"
    assert "conflicting_proposed_file_paths" in predicate.context_evidence_issues(
        conflicting_context
    )

    malformed_context = _context(
        predicate,
        import_names=("not a module!",),
        api_names=("not an api!",),
    )
    malformed = predicate.evaluate(nonmatching, malformed_context)
    assert malformed["decision"] == "flag"
    assert set(predicate.context_evidence_issues(malformed_context)) >= {
        "malformed_import_names",
        "malformed_api_names",
    }

    relative_cwd_context = _context(predicate, cwd="tools")
    assert predicate.evaluate(nonmatching, relative_cwd_context)["decision"] == "flag"
    assert "malformed_cwd" in predicate.context_evidence_issues(
        relative_cwd_context
    )

    malformed_domain_context = _context(
        predicate,
        policy_domain_id="path/domain",
        proposed_file_paths=("src/unrelated.py",),
    )
    assert predicate.evaluate(nonmatching, malformed_domain_context)[
        "decision"
    ] == "flag"
    assert "policy_domain_missing" in predicate.context_evidence_issues(
        malformed_domain_context
    )


def test_glob_matching_is_bounded_for_redundant_and_repeated_globstars():
    code = r'''
import sys
sys.path.insert(0, sys.argv[1])
import predicate

def row(scope_predicate, row_id):
    return {
        "id": row_id,
        "node_id": 20000 + row_id,
        "option": "synthetic option",
        "scope_predicate": scope_predicate,
        "reason": "synthetic reason",
        "source": "declared",
    }

context = predicate.ToolCallContext(
    policy_domain_id="synthetic-domain-a",
    project_root="/workspace/project-a",
    cwd="/workspace/project-a",
    tool_name="synthetic_mutation",
    proposed_file_paths=("/".join(["a"] * 96 + ["y"]),),
    diff_paths=(),
    staged_paths=(),
    import_names=(),
    api_names=(),
    evidence_complete=True,
    evidence_provenance=("synthetic-bounded-glob",),
)
repeated = predicate.compile_predicate(
    row("glob:" + "/".join(["**"] * 64 + ["z"]), 1)
)
assert repeated.compilable
assert predicate.evaluate((repeated,), context)["decision"] == "pass"

redundant = predicate.compile_predicate(row("glob:" + "*" * 64 + "z", 2))
assert not redundant.compilable
assert "whole path segment" in redundant.uncompilable_reason
'''
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code, str(_SRC)],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "scope_predicate",
    (
        "glob:./src/*.py",
        "glob:src/./*.py",
        "glob:src//*.py",
        "glob:src/*.py/",
        "file:.",
        "file:./src",
        "file:src/./example.py",
        "file:src//example.py",
        "file:src/",
    ),
)
def test_noncanonical_path_predicates_are_uncompilable(scope_predicate):
    predicate = _predicate_module()

    check = predicate.compile_predicate(_row(scope_predicate, row_id=51))

    assert not check.compilable
    assert "non-canonical path segment" in check.uncompilable_reason


def test_dot_prefixed_path_segments_remain_canonical():
    predicate = _predicate_module()
    check = predicate.compile_predicate(_row("file:.config/settings", row_id=52))

    verdict = predicate.evaluate(
        (check,),
        _context(
            predicate,
            proposed_file_paths=(".config/settings/local.toml",),
        ),
    )

    assert check.compilable
    assert verdict["decision"] == "block"
