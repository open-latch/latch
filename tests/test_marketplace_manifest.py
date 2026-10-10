"""The Claude Code marketplace manifest must expose only files that run as-is.

Claude Code installs a marketplace plugin by copying the plugin's source
directory into its plugin cache and loading the components it finds there. It
substitutes its own variables (``$ARGUMENTS``, ``${CLAUDE_PLUGIN_ROOT}``) but
none of latch's installer templates: ``<KB_HOME>`` in ``commands/*.md`` is
rendered only by ``install_engine.install_commands()`` into the copies it
writes under ``~/.claude/commands``, and the ``{{...}}`` tokens of
``settings_snippet.json`` only by ``install_engine.load_snippet()``. A file the
plugin serves straight from the repo keeps the literal placeholder, so a
command such as ``bash <KB_HOME>/bin/run_latch_gate.sh`` cannot run.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from latch.install import install_engine as ie  # noqa: E402

MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

# Component locations Claude Code loads from a plugin root even when the
# manifest names no path for them. A manifest path adds to these defaults; it
# does not replace them, so dropping a "commands" key alone still exposes
# <plugin root>/commands/.
DEFAULT_COMPONENT_PATHS = ("commands", "agents", "skills", "hooks/hooks.json", ".mcp.json")
MANIFEST_PATH_KEYS = ("commands", "agents", "skills", "hooks", "mcpServers")

INSTALLER_PLACEHOLDER = re.compile(
    re.escape(ie.COMMAND_PLACEHOLDER) + r"|\{\{[A-Z][A-Z0-9_]*\}\}"
)


def _plugin_root(marketplace_root: Path, marketplace: dict, entry: dict) -> Path:
    source = entry.get("source")
    assert isinstance(source, str), (
        f"plugin {entry.get('name')!r}: only a relative path source can be checked "
        f"here, got {source!r}; extend this test before publishing another source kind"
    )
    plugin_root_prefix = (marketplace.get("metadata") or {}).get("pluginRoot")
    if plugin_root_prefix and not source.startswith("./"):
        source = f"{plugin_root_prefix.rstrip('/')}/{source}"
    root = (marketplace_root / source).resolve()
    assert root.is_relative_to(marketplace_root.resolve()), (
        f"plugin {entry.get('name')!r} source {source!r} leaves the marketplace repo"
    )
    return root


def _manifest_paths(plugin_root: Path, entry: dict) -> list[Path]:
    manifests = [entry]
    plugin_json = plugin_root / ".claude-plugin" / "plugin.json"
    if plugin_json.is_file():
        manifests.append(json.loads(plugin_json.read_text(encoding="utf-8")))
    paths = [plugin_root / rel for rel in DEFAULT_COMPONENT_PATHS]
    for manifest in manifests:
        for key in MANIFEST_PATH_KEYS:
            value = manifest.get(key)
            values = [value] if isinstance(value, str) else value if isinstance(value, list) else []
            paths.extend(plugin_root / rel for rel in values if isinstance(rel, str))
    return paths


def _exposed_files(plugin_root: Path, entry: dict) -> list[Path]:
    files: set[Path] = set()
    for path in _manifest_paths(plugin_root, entry):
        if path.is_file():
            files.add(path)
        elif path.is_dir():
            files.update(p for p in path.rglob("*") if p.is_file())
    return sorted(files)


def _placeholder_offenders(marketplace_root: Path, marketplace: dict) -> list[str]:
    offenders: list[str] = []
    for entry in marketplace.get("plugins", []):
        plugin_root = _plugin_root(marketplace_root, marketplace, entry)
        for path in _exposed_files(plugin_root, entry):
            text = path.read_bytes().decode("utf-8", errors="replace")
            tokens = sorted(set(INSTALLER_PLACEHOLDER.findall(text)))
            if tokens:
                rel = path.relative_to(marketplace_root.resolve()).as_posix()
                offenders.append(f"{entry.get('name')}: {rel} {tokens}")
    return offenders


def test_marketplace_plugins_expose_no_installer_placeholders():
    if not MARKETPLACE.is_file():
        return
    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    offenders = _placeholder_offenders(ROOT, marketplace)
    assert offenders == [], (
        "the Claude marketplace manifest exposes files that still hold installer "
        "placeholders, which Claude Code never substitutes:\n  " + "\n  ".join(offenders)
    )


def test_default_commands_dir_is_exposed_without_a_commands_key(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "gate.md").write_text("bash <KB_HOME>/bin/run.sh\n", encoding="utf-8")
    (commands / "clean.md").write_text("bash ${CLAUDE_PLUGIN_ROOT}/x.sh\n", encoding="utf-8")
    marketplace = {"name": "m", "plugins": [{"name": "p", "source": "./"}]}

    assert _placeholder_offenders(tmp_path, marketplace) == ["p: commands/gate.md ['<KB_HOME>']"]


def test_custom_component_paths_and_settings_tokens_are_checked(tmp_path):
    extra = tmp_path / "plugin" / "extra"
    extra.mkdir(parents=True)
    (extra / "run.md").write_text('"{{PYTHON_PATH}}" x.py\n', encoding="utf-8")
    marketplace = {
        "name": "m",
        "metadata": {"pluginRoot": "./plugin"},
        "plugins": [{"name": "p", "source": ".", "commands": ["./extra/"]}],
    }

    assert _placeholder_offenders(tmp_path, marketplace) == [
        "p: plugin/extra/run.md ['{{PYTHON_PATH}}']"
    ]
