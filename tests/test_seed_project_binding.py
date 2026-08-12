"""Seed previews and apply stay on the project KB captured at invocation."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

import db
import lockfile
import paths
import project_config
import seed


def _init_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _bound_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_config.write_machine_policy(project_config.MACHINE_POLICY_EXPLICIT)
    project = _init_repo(tmp_path / "project")
    vaults = paths.validated_test_root() / "vaults"
    kb_a = vaults / f"seed-a-{tmp_path.name}"
    kb_b = vaults / f"seed-b-{tmp_path.name}"
    for kb_dir in (kb_a, kb_b):
        kb_dir.mkdir(parents=True)
        project_config.mark_kb_target(kb_dir)
    project_config.write_binding(
        project,
        mode=project_config.MODE_LATCHED,
        kb_dir=kb_a,
    )
    return project, kb_a, kb_b


def _source(agent: str = "codex") -> seed.SeedSource:
    return seed.SeedSource(
        id=f"{agent}:seed-binding",
        agent=agent,
        path=f"seed-source:{agent}:binding",
        mtime="2026-08-04T12:00:00+00:00",
        text="[user] We decided that project seed writes remain isolated.",
        content_digest="a" * 64,
        value_score=10.0,
    )


def _candidate(source: seed.SeedSource) -> seed.SeedCandidate:
    return seed.SeedCandidate(
        kind="decision",
        title="Keep seed writes project-local",
        body="Project seed previews and apply stay on their captured KB.",
        confidence=0.95,
        signals=["decision"],
        source_ids=[source.id],
        source_paths=[source.path],
        source_mtimes=[source.mtime],
        source_digests=[source.content_digest],
    )


def _write_preview(
    project: Path,
    source: seed.SeedSource,
    candidate: seed.SeedCandidate,
    snapshot: seed.SeedBindingSnapshot,
) -> str:
    return seed.write_seed_preview(
        project_path=str(project),
        source_choice="codex",
        sources=[source],
        candidates=[candidate],
        llm_estimate=0,
        apply_sources=[source],
        source_failure_codes={},
        workstream_scope="project",
        llm_stats={},
        discovery_stats={},
        llm_refinement_empty=False,
        binding_snapshot=snapshot,
    )


def test_seed_main_rejects_repin_during_source_discovery(
    tmp_path, monkeypatch, capsys,
):
    project, kb_a, kb_b = _bound_project(tmp_path)
    source = _source()
    candidate = _candidate(source)

    def discover_and_repin(**kwargs):
        kwargs["stats"].update({"eligible": 1, "selected": 1})
        project_config.repin_private_scope(project, kb_b)
        return [source]

    monkeypatch.setattr(seed, "discover_sources", discover_and_repin)
    monkeypatch.setattr(
        seed,
        "deterministic_candidates",
        lambda *_args, **_kwargs: [candidate],
    )

    rc = seed.main([
        "--project", str(project),
        "--source", "codex",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--llm", "no",
        "--allow-internal-no-llm",
        "--force-reimport",
        "--format", "json",
    ])

    output = capsys.readouterr()
    assert rc == 1
    assert json.loads(output.out) == {"ok": False, "error": "target_changed"}
    assert "changed during seed" in output.err
    assert not list(kb_a.glob("seed_preview.*.json"))
    assert not list(kb_b.glob("seed_preview.*.json"))


def test_first_project_binding_after_global_access_never_redirects_preview(
    tmp_path, monkeypatch,
):
    project = _init_repo(tmp_path / "legacy-project")
    test_root = paths.validated_test_root()
    assert test_root is not None
    control = test_root / "seed-legacy-transition" / tmp_path.name
    home = tmp_path / "latch-home"
    home.mkdir()
    global_kb = test_root / "vaults" / f"legacy-global-{tmp_path.name}"
    global_kb.mkdir(parents=True)
    (home / "kb_location.json").write_text(
        json.dumps({"kb_dir": str(global_kb)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(project_config.CONTROL_ROOT_ENV, str(control))
    monkeypatch.setenv("LATCH_HOME", str(home))
    project_config.write_machine_policy(project_config.MACHINE_POLICY_SHARED)
    kb_b = paths.validated_test_root() / "vaults" / f"first-bind-{tmp_path.name}"
    kb_b.mkdir(parents=True)
    snapshot = seed.snapshot_seed_binding(str(project))
    assert snapshot.revision == project_config.resolve(project).revision
    assert snapshot.kb_dir == global_kb
    source = _source()
    candidate = _candidate(source)

    transition_started = threading.Event()
    transition_done = threading.Event()
    transition_threads: list[threading.Thread] = []

    def bind_new_target():
        transition_started.set()
        with lockfile.project_access_lock(str(project), exclusive=True):
            project_config.write_machine_policy(
                project_config.MACHINE_POLICY_EXPLICIT
            )
            project_config.create_scope(
                project, policy=project_config.POLICY_PRIVATE
            )
            project_config.authorize_scope(project, kb_dir=kb_b)
        transition_done.set()

    def start_binding_transition(*_args, **_kwargs):
        transition = threading.Thread(target=bind_new_target, daemon=True)
        transition.start()
        transition_threads.append(transition)
        assert transition_started.wait(timeout=1)
        assert not transition_done.is_set()

    monkeypatch.setattr(
        seed, "prune_seed_preview_cache", start_binding_transition,
    )
    digest = _write_preview(project, source, candidate, snapshot)
    transition_threads[0].join(timeout=2)

    assert not transition_threads[0].is_alive()
    assert transition_done.is_set()
    assert seed._seed_preview_path(
        str(project), digest, kb_dir=snapshot.kb_dir,
    ).is_file()
    assert not list(kb_b.glob("seed_preview.*.json"))


def test_copied_stale_preview_cannot_apply_after_repin(tmp_path):
    project, kb_a, kb_b = _bound_project(tmp_path)
    source = _source()
    candidate = _candidate(source)
    snapshot_a = seed.snapshot_seed_binding(str(project))
    digest = _write_preview(project, source, candidate, snapshot_a)
    stale_path = seed._seed_preview_path(
        str(project), digest, kb_dir=kb_a,
    )

    project_config.repin_private_scope(project, kb_b)
    current_path = seed._seed_preview_path(
        str(project), digest, kb_dir=kb_b,
    )
    shutil.copyfile(stale_path, current_path)

    with pytest.raises(seed.SeedPreviewError, match="another project KB binding"):
        seed.load_seed_preview(
            project_path=str(project),
            source_choice="codex",
            preview_digest=digest,
        )


def test_cursor_preview_honors_explicit_session_binding(tmp_path):
    project, _kb_a, kb_b = _bound_project(tmp_path)
    session_id = "cursor-seed-session"
    project_config.record_session_binding(project, session_id)
    snapshot_a = seed.snapshot_seed_binding(
        str(project), session_id=session_id,
    )
    source = _source("cursor")
    candidate = _candidate(source)
    digest = seed.write_cursor_seed_preview(
        project_path=str(project),
        session_id=session_id,
        sources=[source],
        candidates=[candidate],
        llm_estimate=0,
        binding_snapshot=snapshot_a,
    )

    loaded_sources, loaded_candidates, estimate = seed.load_cursor_seed_preview(
        project_path=str(project),
        session_id=session_id,
        preview_digest=digest,
        binding_snapshot=snapshot_a,
    )
    assert len(loaded_sources) == len(loaded_candidates) == 1
    assert estimate == 0

    project_config.repin_private_scope(project, kb_b)
    with pytest.raises(
        seed.SeedBindingChangedError,
        match="older project KB",
    ):
        seed.load_cursor_seed_preview(
            project_path=str(project),
            session_id=session_id,
            preview_digest=digest,
        )


def test_apply_rejects_repin_before_any_database_write(tmp_path):
    project, kb_a, kb_b = _bound_project(tmp_path)
    snapshot_a = seed.snapshot_seed_binding(str(project))
    assert snapshot_a.session_id is None
    project_config.repin_private_scope(project, kb_b)

    with pytest.raises(seed.SeedWriteBlocked) as exc_info:
        seed.apply_candidates(
            [],
            project_path=str(project),
            binding_snapshot=snapshot_a,
        )
    assert exc_info.value.reason == "target_changed"
    assert not (kb_a / "kb.db").exists()
    assert not (kb_b / "kb.db").exists()


def test_apply_rechecks_agent_session_before_database_write(
    tmp_path, monkeypatch,
):
    project, kb_a, kb_b = _bound_project(tmp_path)
    session_id = "revoked-seed-session"
    project_config.record_session_binding(project, session_id)
    monkeypatch.setenv("CODEX_THREAD_ID", session_id)
    snapshot = seed.snapshot_seed_binding(str(project))
    target = project_config.resolve(project)

    assert snapshot.session_id == session_id
    project_config.record_session_boundary(project, session_id)
    assert project_config.resolve(project) == target
    assert project_config.current_session_revision(project, session_id) is None

    def forbidden_connect(*_args, **_kwargs):
        raise AssertionError("revoked seed must fail before opening the database")

    monkeypatch.setattr(db, "connect", forbidden_connect)
    with pytest.raises(seed.SeedWriteBlocked) as exc_info:
        seed.apply_candidates(
            [],
            project_path=str(project),
            binding_snapshot=snapshot,
        )

    assert exc_info.value.reason == "stale_session_binding"
    assert not (kb_a / "kb.db").exists()
    assert not (kb_b / "kb.db").exists()


def test_stale_task_fails_before_auto_source_discovery(
    tmp_path, monkeypatch, capsys,
):
    project, _kb_a, kb_b = _bound_project(tmp_path)
    session_id = "stale-auto-source-task"
    project_config.record_session_binding(project, session_id)
    project_config.repin_private_scope(project, kb_b)
    monkeypatch.setenv("CODEX_THREAD_ID", session_id)

    def forbidden_available_sources(*_args, **_kwargs):
        raise AssertionError("stale tasks must fail before source auto-discovery")

    monkeypatch.setattr(seed, "available_sources", forbidden_available_sources)
    rc = seed.main([
        "--project", str(project),
        "--source", "auto",
        "--lookback-days", "5",
        "--last-sessions", "1",
        "--format", "json",
    ])

    output = capsys.readouterr()
    assert rc == 1
    assert json.loads(output.out) == {
        "ok": False,
        "error": "stale_session_binding",
    }
    assert "older project KB" in output.err


def test_expired_preview_cleanup_rechecks_before_delete(
    tmp_path, monkeypatch,
):
    project, kb_a, kb_b = _bound_project(tmp_path)
    source = _source()
    candidate = _candidate(source)
    snapshot_a = seed.snapshot_seed_binding(str(project))
    digest = _write_preview(project, source, candidate, snapshot_a)
    preview_a = seed._seed_preview_path(
        str(project), digest, kb_dir=kb_a,
    )

    def expire_after_repin(*_args, **_kwargs):
        project_config.repin_private_scope(project, kb_b)
        raise seed.SeedPreviewError("reviewed seed preview expired")

    monkeypatch.setattr(seed, "_validate_seed_preview_age", expire_after_repin)
    with pytest.raises(seed.SeedPreviewError, match="expired"):
        seed.load_seed_preview(
            project_path=str(project),
            source_choice="codex",
            preview_digest=digest,
            binding_snapshot=snapshot_a,
        )

    assert preview_a.is_file()
    assert not list(kb_b.glob("seed_preview.*.json"))


def test_post_apply_cleanup_rechecks_before_delete(tmp_path):
    project, kb_a, kb_b = _bound_project(tmp_path)
    source = _source()
    candidate = _candidate(source)
    snapshot_a = seed.snapshot_seed_binding(str(project))
    digest = _write_preview(project, source, candidate, snapshot_a)
    preview_a = seed._seed_preview_path(
        str(project), digest, kb_dir=kb_a,
    )
    preview_b = seed._seed_preview_path(
        str(project), digest, kb_dir=kb_b,
    )
    preview_b.write_text("new target canary\n", encoding="utf-8")
    project_config.repin_private_scope(project, kb_b)

    seed.remove_seed_preview(
        str(project),
        digest,
        binding_snapshot=snapshot_a,
    )

    assert preview_a.is_file()
    assert preview_b.read_text(encoding="utf-8") == "new target canary\n"


def test_apply_holds_project_access_through_database_open(
    tmp_path, monkeypatch,
):
    project, kb_a, kb_b = _bound_project(tmp_path)
    snapshot = seed.snapshot_seed_binding(str(project))
    original_connect = db.connect
    observed_shared_access = False

    def guarded_connect(*args, **kwargs):
        nonlocal observed_shared_access
        with pytest.raises(RuntimeError, match="cannot upgrade"):
            with lockfile.project_access_lock(str(project), exclusive=True):
                pass
        observed_shared_access = True
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(db, "connect", guarded_connect)
    result = seed.apply_candidates(
        [],
        project_path=str(project),
        binding_snapshot=snapshot,
    )

    assert result.complete
    assert observed_shared_access is True
    assert (kb_a / "kb.db").is_file()
    assert not (kb_b / "kb.db").exists()
