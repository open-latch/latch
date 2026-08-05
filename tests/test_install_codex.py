"""Unit tests for the Codex installer config merge."""
from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import install_codex as ic  # noqa: E402


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_render_mcp_block_uses_codex_shape():
    out = ic.render_mcp_block("/PY", "/repo/src/mcp_server.py")
    _assert(ic.BEGIN_MARK in out and ic.END_MARK in out, "managed markers missing")
    _assert("[mcp_servers.latch]" in out, "Codex MCP table missing")
    _assert('command = "/PY"' in out, "python command missing")
    _assert('args = ["/repo/src/mcp_server.py"]' in out, "server args missing")
    _assert("required = true" in out,
            "Codex must not silently start without the Latch MCP tools")
    _assert(tomllib.loads(out)["mcp_servers"]["latch"]["required"] is True,
            "required must be a TOML boolean, not a string")
    _assert("tool_timeout_sec = 300" in out, "Codex gate needs room for backend calls")
    _assert('default_tools_approval_mode = "approve"' in out,
            "approval mode should be server-level")
    _assert("[mcp_servers.latch.env]" in out, "Codex MCP env table missing")
    _assert('LATCH_MODEL_BACKEND = "codex"' in out,
            "Codex install must select the generic Codex model backend")
    _assert('LATCH_GATE_BACKEND = "codex"' in out,
            "Codex install must select the Codex gate backend")
    _assert('LATCH_ADAPTER = "codex"' in out,
            "Codex MCP startup repair needs an explicit host identity")
    _assert(
        f'LATCH_WIRING_VERSION = "{ic.versioning.WIRING_VERSION}"' in out,
        "managed Codex MCP config must carry the wiring version",
    )
    print("PASS render_mcp_block_uses_codex_shape")


def test_merge_config_preserves_unrelated_tables():
    existing = """theme = "dark"

[mcp_servers.node_repl]
command = "/node"
args = []

[plugins."browser@openai-bundled"]
enabled = true
"""
    new, changes = ic.merge_config(existing, "/PY", "/srv.py")
    _assert(changes, "merge should report changes")
    _assert("[mcp_servers.node_repl]" in new, "unrelated MCP server must survive")
    _assert('[plugins."browser@openai-bundled"]' in new,
            "plugin config must survive")
    parsed = tomllib.loads(new)
    _assert(parsed["features"]["hooks"] is True,
            "fresh merge must enable the Codex lifecycle hook feature")
    _assert(new.count("[mcp_servers.latch]") == 1,
            "managed server table should appear once")
    print("PASS merge_config_preserves_unrelated_tables")


def test_merge_config_canonicalizes_lifecycle_hook_features():
    existing = """# user preamble
[features] # user feature table
# preserve this feature comment
hooks = false # lifecycle choice
codex_hooks = false # deprecated choice
web_search_request = true # unrelated feature

[profiles.default]
model = "gpt-test" # unrelated table
"""
    new, changes = ic.merge_config(existing, "/PY", "/srv.py")
    parsed = tomllib.loads(new)
    features = parsed["features"]
    _assert("enabled Codex lifecycle hooks feature" in changes, changes)
    _assert(features["hooks"] is True, features)
    _assert("codex_hooks" not in features, features)
    _assert(features["web_search_request"] is True, features)
    _assert(new.count("hooks = true") == 1, new)
    _assert("# user preamble" in new, new)
    _assert("# preserve this feature comment" in new, new)
    _assert("# lifecycle choice" in new, new)
    _assert("# deprecated choice" in new, new)
    _assert("# unrelated feature" in new, new)
    _assert('[profiles.default]' in new and 'model = "gpt-test"' in new, new)

    again, again_changes = ic.merge_config(new, "/PY", "/srv.py")
    _assert(again == new, "canonical feature merge must be byte-idempotent")
    _assert(again_changes == [], again_changes)
    print("PASS merge_config_canonicalizes_lifecycle_hook_features")


def test_merge_config_promotes_deprecated_hook_alias():
    existing = """[features]
codex_hooks = true # keep alias rationale
experimental_windows_sandbox = true
"""
    new, _changes = ic.merge_config(existing, "/PY", "/srv.py")
    parsed = tomllib.loads(new)
    _assert(parsed["features"]["hooks"] is True, parsed)
    _assert("codex_hooks" not in parsed["features"], parsed)
    _assert(parsed["features"]["experimental_windows_sandbox"] is True, parsed)
    _assert("# keep alias rationale" in new, new)
    _assert(new.count("hooks = true") == 1, new)
    print("PASS merge_config_promotes_deprecated_hook_alias")


def test_merge_config_supports_quoted_features_table():
    existing = '''["features"] # quoted table
hooks = false # preserve reason
js_repl = true

[profiles.default]
model = "gpt-test"
'''
    new, _changes = ic.merge_config(existing, "/PY", "/srv.py")
    parsed = tomllib.loads(new)
    _assert(parsed["features"]["hooks"] is True, parsed)
    _assert(parsed["features"]["js_repl"] is True, parsed)
    _assert('["features"] # quoted table' in new, new)
    _assert("# preserve reason" in new, new)
    _assert(ic.merge_config(new, "/PY", "/srv.py") == (new, []),
            "quoted-table merge must be idempotent")
    print("PASS merge_config_supports_quoted_features_table")


def test_merge_config_stops_features_at_array_table_boundary():
    existing = """[features]
js_repl = true

[[profiles]]
name = "x"
hooks = false
"""
    new, _changes = ic.merge_config(existing, "/PY", "/srv.py")
    parsed = tomllib.loads(new)
    _assert(parsed["features"]["hooks"] is True, parsed)
    _assert(parsed["features"]["js_repl"] is True, parsed)
    _assert(parsed["profiles"] == [{"name": "x", "hooks": False}], parsed)
    _assert('name = "x"\nhooks = false' in new,
            "foreign array-table hook key must remain untouched")
    print("PASS merge_config_stops_features_at_array_table_boundary")


def test_merge_config_rejects_unsupported_valid_feature_forms_and_multiline():
    cases = {
        "inline": "features = { hooks = false, js_repl = true }\n",
        "dotted": "features.hooks = false\n",
        "multiline": 'notes = """\n[features]\nhooks = false\n"""\n',
    }
    for label, existing in cases.items():
        tomllib.loads(existing)  # Each repro is valid TOML before the merge.
        try:
            ic.merge_config(existing, "/PY", "/srv.py")
        except ic.CodexConfigMergeError as exc:
            detail = str(exc)
        else:
            raise AssertionError(f"{label} config must fail closed")
        expected = "multiline TOML string" if label == "multiline" else "features representation"
        _assert(expected in detail, f"{label}: {detail}")
        _assert("no changes were written" in detail, f"{label}: {detail}")
    print("PASS merge_config_rejects_unsupported_valid_feature_forms_and_multiline")


def test_merge_config_validates_generated_toml():
    original = ic.render_mcp_block
    try:
        ic.render_mcp_block = lambda _python, _server: "[invalid"
        try:
            ic.merge_config("[features]\nhooks = true\n", "/PY", "/srv.py")
        except ic.CodexConfigMergeError as exc:
            _assert("invalid TOML after the proposed merge" in str(exc), exc)
        else:
            raise AssertionError("invalid generated TOML must be rejected")
    finally:
        ic.render_mcp_block = original
    print("PASS merge_config_validates_generated_toml")


def test_merge_config_fail_closes_quoted_bracket_table_scanner_misses():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-quoted-bracket-"))
    cases = {
        "hook trust": """[features]
hooks = true

[mcp_servers.latch]
command = "/old"
args = ["/old.py"]

[hooks.state."/Users/me/proj]x/hooks.json:session_start:0:0"]
trusted_hash = "sha256:test"
""",
        "foreign profile": """[features]
hooks = true

[profiles."a]b"]
hooks = false
keep = 7
""",
    }
    try:
        for label, existing in cases.items():
            tomllib.loads(existing)
            try:
                ic.merge_config(existing, "/PY", "/srv.py")
            except ic.CodexConfigMergeError as exc:
                detail = str(exc)
            else:
                raise AssertionError(f"{label} scanner miss must fail closed")
            _assert("could not prove preservation of unrelated TOML settings" in detail,
                    f"{label}: {detail}")
            _assert("no changes were written" in detail, f"{label}: {detail}")

            config = d / f"{label.replace(' ', '-')}.toml"
            config.write_text(existing, encoding="utf-8")
            ok, status_detail = ic.config_status(config, "/PY", "/srv.py")
            _assert(not ok, f"{label} status must fail closed")
            _assert("cannot be safely merged" in status_detail, status_detail)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("PASS merge_config_fail_closes_quoted_bracket_table_scanner_misses")


def test_merge_config_fail_closes_unremoved_legacy_mcp_forms():
    cases = {
        "quoted table": """[features]
hooks = true

[mcp_servers."claude-kb"]
command = "/old"
args = ["/old.py"]
""",
        "dotted keys": """mcp_servers.claude-kb.command = "/old"
mcp_servers.claude-kb.args = ["/old.py"]

[features]
hooks = true
""",
    }
    for label, existing in cases.items():
        parsed = tomllib.loads(existing)
        _assert("claude-kb" in parsed["mcp_servers"], parsed)
        try:
            ic.merge_config(existing, "/PY", "/srv.py")
        except ic.CodexConfigMergeError as exc:
            detail = str(exc)
        else:
            raise AssertionError(f"{label} legacy MCP form must fail closed")
        _assert("left deprecated latch MCP server name" in detail, f"{label}: {detail}")
        _assert("no changes were written" in detail, f"{label}: {detail}")
    print("PASS merge_config_fail_closes_unremoved_legacy_mcp_forms")


def test_merge_config_replaces_existing_server_tables():
    existing = """[mcp_servers.claude-kb]
command = "/old"
args = ["/old.py"]

[mcp_servers.claude-kb.tools.kb_get]
approval_mode = "approve"

[mcp_servers.claude-kb.env]
LATCH_GATE_BACKEND = "claude"

[mcp_servers.other]
command = "node"
"""
    new, changes = ic.merge_config(existing, "/new/python", "/new/server.py")
    _assert("replaced existing latch-owned MCP server table" in changes,
            f"expected replacement change, got {changes}")
    _assert("/old" not in new, "old server config should be removed")
    _assert("claude-kb.tools.kb_get" not in new,
            "old nested tool table should be removed")
    _assert('LATCH_GATE_BACKEND = "claude"' not in new,
            "old nested env table should be removed")
    _assert('LATCH_MODEL_BACKEND = "codex"' in new,
            "new generic Codex backend env should be installed")
    _assert('LATCH_GATE_BACKEND = "codex"' in new,
            "new Codex backend env should be installed")
    _assert("[mcp_servers.other]" in new, "following unrelated table should survive")
    _assert("[mcp_servers.latch]" in new, "new primary server name should be installed")
    _assert("/new/server.py" in new, "new server path should be installed")
    print("PASS merge_config_replaces_existing_server_tables")


def test_merge_config_preserves_foreign_tables_inside_managed_block():
    existing = f"""theme = "dark"

{ic.BEGIN_MARK}
[mcp_servers.claude-kb]
command = "/old"
args = ["/old.py"]

[mcp_servers.claude-kb.env]
LATCH_GATE_BACKEND = "codex"

[hooks.state]

[hooks.state."/Users/me/.codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:test"
{ic.END_MARK}
"""
    new, changes = ic.merge_config(existing, "/new/python", "/new/server.py")
    _assert("replaced existing latch-managed Codex MCP block" in changes,
            f"expected managed replacement, got {changes}")
    _assert('[hooks.state."/Users/me/.codex/hooks.json:session_start:0:0"]' in new,
            "foreign hook trust state inside marker should be preserved")
    _assert('trusted_hash = "sha256:test"' in new,
            "hook trust hash should be preserved")
    _assert("/old.py" not in new, "old managed MCP server should be removed")
    _assert("/new/server.py" in new, "new server path should be installed")
    _assert(new.count(ic.BEGIN_MARK) == 1 and new.count(ic.END_MARK) == 1,
            "managed markers should appear once")
    print("PASS merge_config_preserves_foreign_tables_inside_managed_block")


def test_config_status_accepts_legacy_server_name():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-"))
    try:
        p = d / "config.toml"
        p.write_text("""[features]
hooks = true

[mcp_servers.claude-kb]
command = "/PY"
args = ["/srv.py"]
required = true
""", encoding="utf-8")
        ok, detail = ic.config_status(p, "/PY", "/srv.py")
        _assert(ok, f"legacy Codex config should remain supported: {detail}")
        _assert("legacy server name" in detail, detail)
        print("PASS config_status_accepts_legacy_server_name")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_config_status_rejects_optional_legacy_server_name():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-"))
    try:
        p = d / "config.toml"
        p.write_text("""[features]
hooks = true

[mcp_servers.claude-kb]
command = "/PY"
args = ["/srv.py"]
""", encoding="utf-8")
        ok, detail = ic.config_status(p, "/PY", "/srv.py")
        _assert(not ok, "optional legacy MCP config must not report healthy")
        _assert("missing or drifted" in detail, detail)
        print("PASS config_status_rejects_optional_legacy_server_name")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_config_status_rejects_missing_disabled_or_deprecated_hooks_with_legacy_mcp():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-"))
    try:
        p = d / "config.toml"
        cases = (
            ("missing", ""),
            ("disabled", "[features]\nhooks = false\n\n"),
            ("deprecated", "[features]\ncodex_hooks = true\n\n"),
        )
        legacy = """[mcp_servers.claude-kb]
command = "/PY"
args = ["/srv.py"]
"""
        for label, features in cases:
            p.write_text(features + legacy, encoding="utf-8")
            ok, detail = ic.config_status(p, "/PY", "/srv.py")
            _assert(not ok, f"{label} hooks must fail despite a matching legacy MCP block")
            _assert("lifecycle hooks" in detail, detail)
        print("PASS config_status_rejects_missing_disabled_or_deprecated_hooks_with_legacy_mcp")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_merge_config_idempotent():
    new1, changes1 = ic.merge_config("", "/PY", "/srv.py")
    _assert(changes1, "first merge should change")
    _assert(tomllib.loads(new1)["features"]["hooks"] is True, new1)
    new2, changes2 = ic.merge_config(new1, "/PY", "/srv.py")
    _assert(new2 == new1, "second merge should be byte-identical")
    _assert(changes2 == [], f"second merge should report no changes, got {changes2}")
    print("PASS merge_config_idempotent")


def test_write_config_backs_up_existing():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-"))
    try:
        p = d / "config.toml"
        p.write_text('theme = "dark"\n', encoding="utf-8")
        ic.write_config(p, 'theme = "light"\n')
        _assert((d / "config.toml.latchbak").exists(), "backup should exist")
        _assert((d / "config.toml.latchbak").read_text(encoding="utf-8") ==
                'theme = "dark"\n', "backup should hold old content")
        _assert(p.read_text(encoding="utf-8") == 'theme = "light"\n',
                "config should be updated")
        print("PASS write_config_backs_up_existing")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_main_refuses_unsupported_config_without_backup_or_write():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-config-refusal-"))
    config = d / "config.toml"
    original = "features = { hooks = false, js_repl = true }\n"
    original_pin = ic.install_engine.pin_kb_dir
    pin_calls: list[bool] = []
    try:
        config.write_text(original, encoding="utf-8")
        ic.install_engine.pin_kb_dir = lambda _value, _dry: (
            pin_calls.append(True) or ("OK", "unexpected")
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ic.main([
                "--python", sys.executable,
                "--config", str(config),
                "--hooks", str(d / "hooks.json"),
                "--skills-dir", str(d / "skills"),
                "--agents-md", str(d / "AGENTS.md"),
                "--skip-agents",
                "--skip-hooks",
                "--skip-skills",
                "--suppress-seed-output",
            ])
        text = output.getvalue()
        _assert(rc == 2, f"unsupported config should fail with status 2, got {rc}")
        _assert("Codex config merge refused" in text, text)
        _assert("No Codex configuration changes were written" in text, text)
        _assert(config.read_text(encoding="utf-8") == original,
                "unsupported config must remain byte-identical")
        _assert(not config.with_suffix(".toml.latchbak").exists(),
                "refused merge must not create a backup")
        _assert(not pin_calls, "refused merge must stop before KB pin mutation")
    finally:
        ic.install_engine.pin_kb_dir = original_pin
        shutil.rmtree(d, ignore_errors=True)
    print("PASS main_refuses_unsupported_config_without_backup_or_write")


def test_codex_skills_sync_status_and_collision():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-skills-"))
    try:
        skills = d / ".agents" / "skills"
        changes = ic.sync_codex_skills(skills)
        _assert(len(changes) == len(ic.CODEX_SKILL_NAMES), changes)

        compact = skills / "source-command-latch-compact" / "SKILL.md"
        metadata = (
            skills
            / "source-command-latch-compact"
            / "agents"
            / "openai.yaml"
        )
        _assert(ic.CODEX_SKILL_MARKER in compact.read_text(encoding="utf-8"),
                "installed skill should carry the ownership marker")
        _assert(metadata.read_text(encoding="utf-8") == (
            ic.CODEX_SKILLS_SRC
            / "source-command-latch-compact"
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8"), "skill metadata should be installed")
        review = skills / "source-command-latch-review" / "SKILL.md"
        review_text = review.read_text(encoding="utf-8")
        _assert("<KB_HOME>" not in review_text,
                "installed review skill should embed the Latch app path")
        _assert(str(ic.KB_HOME).replace("\\", "/") in review_text,
                "installed review skill should resolve the Latch app path")
        ok, detail = ic.codex_skills_status(skills)
        _assert(ok, detail)
        _assert(ic.sync_codex_skills(skills) == [], "second sync should be idempotent")

        legacy_root = d / "legacy" / "skills"
        legacy_compact = legacy_root / "source-command-latch-compact"
        shutil.copytree(
            ic.CODEX_SKILLS_SRC / "source-command-latch-compact", legacy_compact
        )
        ic.sync_codex_skills(legacy_root)
        adopted = legacy_compact / "SKILL.md"
        _assert(ic.CODEX_SKILL_MARKER in adopted.read_text(encoding="utf-8"),
                "an exact manual copy should be adopted as latch-owned")
        _assert(adopted.with_name("SKILL.md.latchbak").is_file(),
                "adopting a manual copy should preserve a backup")

        collision_root = d / "collision" / "skills"
        collision = collision_root / "source-command-latch-gate" / "SKILL.md"
        collision.parent.mkdir(parents=True)
        collision.write_text(
            "---\nname: source-command-latch-gate\ndescription: user owned\n---\n",
            encoding="utf-8",
        )
        try:
            ic.sync_codex_skills(collision_root)
        except ic.CodexSkillCollisionError:
            pass
        else:
            raise AssertionError("installer should refuse a user-owned skill collision")
        _assert(not (collision_root / "source-command-latch-compact").exists(),
                "collision preflight should prevent partial skill installation")
        print("PASS codex_skills_sync_status_and_collision")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_render_review_skill_shell_quotes_adversarial_latch_path():
    root = Path(tempfile.mkdtemp(prefix="latch-codex-review-render-"))
    latch_home = root / 'Latch Space $cash $(touch SHOULD_NOT_EXIST) "double" `tick` apostrophe\'s 雪'
    runner = latch_home / "bin" / "latch-review"
    if os.name != "nt":
        runner.parent.mkdir(parents=True)
        runner.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        runner.chmod(0o755)
    original_home = ic.KB_HOME
    try:
        ic.KB_HOME = latch_home
        body = ic.render_codex_skill("source-command-latch-review")
        _assert(not ic.install_engine.unresolved_command_placeholders(body), body)
        _assert("LATCH_REVIEW_POSIX_LITERAL" not in body, body)
        _assert("LATCH_REVIEW_POWERSHELL_LITERAL" not in body, body)
        _assert("latch-review-source-template-guard:start" in body, body)
        _assert(
            "Continue below only when both installer tokens have already been rendered"
            in body,
            body,
        )
        normalized_home = str(latch_home).replace("\\", "/")
        expected_runner = normalized_home + "/bin/latch-review"
        _assert(f"latch_runner={shlex.quote(expected_runner)}" in body, body)
        _assert("${LATCH_HOME" not in body, body)
        _assert("${CLAUDE_KB_HOME" not in body, body)
        expected_ps = "'" + (
            normalized_home + "/bin/latch-review.ps1"
        ).replace("'", "''") + "'"
        _assert(f"$latchReview = {expected_ps}" in body, body)
        _assert("& $latchReview <resolved target arguments>" in body, body)

        if os.name != "nt":
            match = re.search(r"```bash\n(.*?)\n```", body, re.DOTALL)
            _assert(match is not None, "missing rendered Bash block")
            command = match.group(1).replace(
                "<resolved target arguments>", "--commit " + "a" * 40
            )
            result = subprocess.run(
                ["bash", "-c", command], cwd=root, capture_output=True, text=True
            )
            _assert(result.returncode == 0, result.stderr)
            _assert(result.stdout.splitlines() == ["--commit", "a" * 40], result.stdout)
            _assert(not (root / "SHOULD_NOT_EXIST").exists(),
                    "shell interpolation from the install path must never execute")

            runner.unlink()
            missing = subprocess.run(
                ["bash", "-c", command], cwd=root, capture_output=True, text=True
            )
            _assert(missing.returncode == 1, missing.stderr)
            _assert(
                "unavailable at the pinned path" in missing.stderr
                and "rerun bin/install_codex" in missing.stderr,
                missing.stderr,
            )
    finally:
        ic.KB_HOME = original_home
        shutil.rmtree(root, ignore_errors=True)
    print("PASS render_review_skill_shell_quotes_adversarial_latch_path")


def test_main_installs_and_checks_codex_skills():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-main-skills-"))
    original_pin = ic.install_engine.pin_kb_dir
    try:
        ic.install_engine.pin_kb_dir = lambda _value, _dry: ("OK", "pinned")
        config = d / "config.toml"
        skills = d / ".agents" / "skills"
        common = [
            "--python", sys.executable,
            "--config", str(config),
            "--skills-dir", str(skills),
            "--skip-agents",
            "--skip-hooks",
        ]
        rc = ic.main(common + ["--no-seed-prompt", "--suppress-seed-output"])
        _assert(rc == 0, f"Codex skill install should complete, got {rc}")
        rc = ic.main(common + ["--check"])
        _assert(rc == 0, f"Codex skill check should pass, got {rc}")
        print("PASS main_installs_and_checks_codex_skills")
    finally:
        ic.install_engine.pin_kb_dir = original_pin
        shutil.rmtree(d, ignore_errors=True)


def test_no_seed_prompt_prints_seed_handoff_unless_suppressed():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-seed-output-"))
    original_pin = ic.install_engine.pin_kb_dir
    try:
        ic.install_engine.pin_kb_dir = lambda _value, _dry: ("OK", "pinned")
        config = d / "config.toml"
        hooks = d / "hooks.json"
        agents = d / "AGENTS.md"

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ic.main([
                "--python",
                sys.executable,
                "--config",
                str(config),
                "--hooks",
                str(hooks),
                "--agents-md",
                str(agents),
                "--skip-agents",
                "--skip-hooks",
                "--skip-skills",
                "--no-seed-prompt",
            ])
        text = output.getvalue()
        _assert(rc == 0, f"Codex installer should complete, got {rc}:\n{text}")
        _assert(text.count("Seed latch from prior work") == 1,
                f"--no-seed-prompt should still print standalone seed handoff:\n{text}")
        _assert("--source codex --backend codex --apply" in text,
                f"Codex handoff must select the Codex model backend:\n{text}")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = ic.main([
                "--python",
                sys.executable,
                "--config",
                str(config),
                "--hooks",
                str(hooks),
                "--agents-md",
                str(agents),
                "--skip-agents",
                "--skip-hooks",
                "--skip-skills",
                "--no-seed-prompt",
                "--suppress-seed-output",
            ])
        text = output.getvalue()
        _assert(rc == 0, f"suppressed Codex installer should complete, got {rc}:\n{text}")
        _assert("Seed latch from prior work" not in text,
                f"--suppress-seed-output should silence Codex seed handoff:\n{text}")
        print("PASS no_seed_prompt_prints_seed_handoff_unless_suppressed")
    finally:
        ic.install_engine.pin_kb_dir = original_pin
        shutil.rmtree(d, ignore_errors=True)


def test_interactive_seed_offer_uses_codex_backend():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-seed-backend-"))
    original_offer = ic.install_engine.offer_seed_after_install
    original_pin = ic.install_engine.pin_kb_dir
    calls: list[dict] = []
    try:
        ic.install_engine.offer_seed_after_install = lambda **kwargs: calls.append(kwargs)
        ic.install_engine.pin_kb_dir = lambda _value, _dry: ("OK", "pinned")
        rc = ic.main([
            "--python",
            sys.executable,
            "--config",
            str(d / "config.toml"),
            "--hooks",
            str(d / "hooks.json"),
            "--skills-dir",
            str(d / ".agents" / "skills"),
            "--agents-md",
            str(d / "AGENTS.md"),
            "--skip-agents",
            "--skip-hooks",
        ])
        _assert(rc == 0, f"Codex installer should complete, got {rc}")
        _assert(len(calls) == 1, f"expected one seed offer, got {calls}")
        _assert(calls[0]["source"] == "codex", calls[0])
        _assert(calls[0]["backend"] == "codex", calls[0])
    finally:
        ic.install_engine.offer_seed_after_install = original_offer
        ic.install_engine.pin_kb_dir = original_pin
        shutil.rmtree(d, ignore_errors=True)
    print("PASS interactive_seed_offer_uses_codex_backend")


def test_pin_conflict_stops_before_codex_config_write():
    d = Path(tempfile.mkdtemp(prefix="latch-codex-pin-conflict-"))
    config = d / "config.toml"
    original_pin = ic.install_engine.pin_kb_dir
    try:
        for level in ("ERROR", "FAIL"):
            ic.install_engine.pin_kb_dir = lambda _value, _dry, level=level: (
                level, "effective target is unsafe or conflicts with existing pin"
            )
            rc = ic.main([
                "--python", sys.executable,
                "--config", str(config),
                "--hooks", str(d / "hooks.json"),
                "--skills-dir", str(d / ".agents" / "skills"),
                "--agents-md", str(d / "AGENTS.md"),
                "--skip-agents",
                "--skip-hooks",
                "--suppress-seed-output",
            ])
            _assert(rc == 2, f"{level} pin should fail with status 2, got {rc}")
            _assert(
                not config.exists(),
                f"{level} pin must stop before Codex config writes",
            )
    finally:
        ic.install_engine.pin_kb_dir = original_pin
        shutil.rmtree(d, ignore_errors=True)
    print("PASS pin_conflict_stops_before_codex_config_write")


if __name__ == "__main__":
    test_render_mcp_block_uses_codex_shape()
    test_merge_config_preserves_unrelated_tables()
    test_merge_config_canonicalizes_lifecycle_hook_features()
    test_merge_config_promotes_deprecated_hook_alias()
    test_merge_config_supports_quoted_features_table()
    test_merge_config_stops_features_at_array_table_boundary()
    test_merge_config_rejects_unsupported_valid_feature_forms_and_multiline()
    test_merge_config_validates_generated_toml()
    test_merge_config_fail_closes_quoted_bracket_table_scanner_misses()
    test_merge_config_fail_closes_unremoved_legacy_mcp_forms()
    test_merge_config_replaces_existing_server_tables()
    test_merge_config_preserves_foreign_tables_inside_managed_block()
    test_config_status_accepts_legacy_server_name()
    test_config_status_rejects_optional_legacy_server_name()
    test_config_status_rejects_missing_disabled_or_deprecated_hooks_with_legacy_mcp()
    test_merge_config_idempotent()
    test_write_config_backs_up_existing()
    test_main_refuses_unsupported_config_without_backup_or_write()
    test_codex_skills_sync_status_and_collision()
    test_render_review_skill_shell_quotes_adversarial_latch_path()
    test_main_installs_and_checks_codex_skills()
    test_no_seed_prompt_prints_seed_handoff_unless_suppressed()
    test_interactive_seed_offer_uses_codex_backend()
    test_pin_conflict_stops_before_codex_config_write()
    print("\nAll install_codex tests pass.")
